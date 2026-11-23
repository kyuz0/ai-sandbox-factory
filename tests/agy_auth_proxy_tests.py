"""Offline Antigravity transport checks using the pinned mitmproxy runtime."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qsl, urlencode

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from mitmproxy import http
from mitmproxy.test import tflow
from agy_auth import AuthStore
from agy_auth_proxy import AntigravityProxy
from test_agy_auth import profile


class AntigravityProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = profile()
        self.store = AuthStore(self.temp.name, read=lambda: copy.deepcopy(self.profile), write=self.write)
        self.public = self.store.public()['auth']['token']
        self.proxy = AntigravityProxy(self.store)

    def write(self, value):
        self.profile = copy.deepcopy(value)

    def flow(self, url, method='GET', content=b'', headers=None):
        flow = tflow.tflow()
        flow.request = http.Request.make(method, url, content, headers or {})
        return flow

    async def test_native_request_preserves_features_and_literal_prompt_fields(self):
        mask = self.public['access_token']
        body = json.dumps({'arbitraryNativeFeature': {'prompt': mask}}).encode()
        flow = self.flow('https://daily-cloudcode-pa.googleapis.com/v1internal:newNativeFeature?x=1',
                         'POST', body, {'authorization': 'Bearer ' + mask, 'x-literal': mask})
        await self.proxy.request(flow)
        self.assertEqual(flow.request.headers['authorization'], 'Bearer private-google-access')
        self.assertEqual(flow.request.headers['x-literal'], mask)
        self.assertEqual(flow.request.content, body)
        self.assertIsNone(flow.response)

    async def test_other_hosts_ports_protocols_and_google_apis_never_receive_subscription_token(self):
        mask = self.public['access_token']
        for url in ('https://attacker.invalid/', 'http://daily-cloudcode-pa.googleapis.com/v1internal:test',
                    'https://daily-cloudcode-pa.googleapis.com.attacker.invalid/v1internal:test',
                    'https://daily-cloudcode-pa.googleapis.com:444/v1internal:test',
                    'https://www.googleapis.com/drive/v3/files', 'https://play.googleapis.com/other'):
            flow = self.flow(url, headers={'authorization': 'Bearer ' + mask})
            await self.proxy.request(flow)
            self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + mask)

    async def test_native_aicode_method_and_remote_resource_paths(self):
        for path in ('/v1alpha:fetchConfig', '/v1alpha/remote:connect'):
            flow = self.flow('https://aicode.googleapis.com' + path,
                             headers={'authorization': 'Bearer ' + self.public['access_token']})
            await self.proxy.request(flow)
            self.assertEqual(flow.request.headers['authorization'], 'Bearer private-google-access')
            self.assertEqual(flow.request.path, path)

    async def test_refresh_substitutes_only_refresh_field_and_masks_new_tokens(self):
        mask = self.public['refresh_token']
        flow = self.flow('https://oauth2.googleapis.com/token', 'POST',
            urlencode({'grant_type': 'refresh_token', 'refresh_token': mask, 'scope': mask}).encode(),
            {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        payload = dict(parse_qsl(flow.request.get_text()))
        self.assertEqual(payload['refresh_token'], 'private-google-refresh')
        self.assertEqual(payload['scope'], mask)
        flow.response = http.Response.make(200, json.dumps({'access_token': 'rotated-private-access',
            'refresh_token': 'rotated-private-refresh', 'expires_in': 3600}), {'content-type': 'application/json'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('rotated-private', flow.response.get_text())
        self.assertEqual(self.profile['token']['access_token'], 'rotated-private-access')
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_malformed_refresh_reports_the_rejected_field(self):
        for value in ([], {}, None, ''):
            flow = self.flow('https://oauth2.googleapis.com/token', 'POST',
                json.dumps({'grant_type': 'refresh_token', 'refresh_token': value}).encode(),
                {'content-type': 'application/json'})
            await self.proxy.request(flow)
            self.assertEqual(flow.response.status_code, 502)
            self.assertIn('refresh_token must be a nonempty string', flow.response.get_text())
            self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_fresh_login_masks_all_issued_subscription_tokens(self):
        self.profile = None
        flow = self.flow('https://oauth2.googleapis.com/token', 'POST', b'grant_type=authorization_code&code=fixture',
                         {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, json.dumps({'access_token': 'new-private-access',
            'refresh_token': 'new-private-refresh', 'expires_in': 3600,
            'diagnostic': {'echo': 'new-private-access and new-private-refresh'}}), {'content-type': 'application/json'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('new-private', flow.response.get_text())
        self.assertEqual(self.profile['token']['refresh_token'], 'new-private-refresh')

    async def test_failure_status_diagnostics_and_request_id_survive_redaction(self):
        for oauth in (False, True):
            flow = self.flow('https://oauth2.googleapis.com/token' if oauth else
                'https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent', 'POST',
                b'grant_type=refresh_token&refresh_token=' + self.public['refresh_token'].encode() if oauth else b'{}',
                {'content-type': 'application/x-www-form-urlencoded', 'authorization': 'Bearer ' + self.public['access_token']})
            await self.proxy.request(flow)
            flow.response = http.Response.make(401, json.dumps({'error': {'code': 401,
                'message': 'Credential private-google-access expired; sign in again.'}}),
                {'x-request-id': 'fixture-request', 'content-type': 'application/json'})
            self.proxy.responseheaders(flow)
            self.proxy.response(flow)
            self.assertEqual(flow.response.status_code, 401)
            self.assertIn('expired; sign in again', flow.response.get_text())
            self.assertNotIn('private-google-access', flow.response.get_text())
            self.assertEqual(flow.response.headers['x-request-id'], 'fixture-request')
            self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_stream_boundary_and_compressed_body_cannot_leak_real_token(self):
        flow = self.flow('https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent',
                         headers={'authorization': 'Bearer ' + self.public['access_token']})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, b'', {'content-type': 'text/event-stream'})
        self.proxy.responseheaders(flow)
        redact = flow.response.stream
        result = redact(b'data: private-google-') + redact(b'access\n\n') + redact(b'')
        self.assertNotIn(b'private-google-access', result)
        self.assertIn(self.public['access_token'].encode(), result)
        flow.response = http.Response.make(500, b'upstream diagnostic private-google-access',
                                            {'content-encoding': 'gzip'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertEqual(flow.response.status_code, 500)
        self.assertIn('upstream diagnostic', flow.response.get_text())
        self.assertNotIn('private-google-access', flow.response.get_text())


if __name__ == '__main__':
    unittest.main()
