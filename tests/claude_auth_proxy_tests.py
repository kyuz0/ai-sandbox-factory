"""Offline Claude authentication transport checks with synthetic credentials."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from mitmproxy import http
from mitmproxy.test import tflow
from claude_auth import AuthStore
from claude_auth_proxy import SubscriptionProxy
from codex_auth import atomic_json
from test_claude_auth import login


class ClaudeProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(self.temp.name)
        atomic_json(self.store.path, login())
        self.public = self.store.public()['auth']['claudeAiOauth']
        self.proxy = SubscriptionProxy(self.store)

    def flow(self, url, method='GET', content=b'', headers=None):
        flow = tflow.tflow()
        flow.request = http.Request.make(method, url, content, headers or {})
        return flow

    async def test_native_requests_keep_path_body_and_non_auth_headers(self):
        mask = self.public['accessToken']
        body = json.dumps({'messages': [{'role': 'user', 'content': mask}]}).encode()
        flow = self.flow('https://api.anthropic.com/v1/messages?beta=true', 'POST', body,
                         {'authorization': 'Bearer ' + mask, 'x-prompt': mask})
        await self.proxy.request(flow)
        self.assertEqual(flow.request.headers['authorization'], 'Bearer fixture-private-access')
        self.assertEqual(flow.request.headers['x-prompt'], mask)
        self.assertEqual(flow.request.content, body)
        self.assertEqual(flow.request.path, '/v1/messages?beta=true')

    async def test_project_destinations_follow_workspace_mode_not_the_sign_in_gateway(self):
        for restricted in (True, False):
            proxy = SubscriptionProxy(self.store, restricted)
            for host in ('api.anthropic.com', 'claude.ai', 'platform.claude.com', 'console.anthropic.com'):
                flow = self.flow('https://' + host + '/')
                proxy.route(flow)
                self.assertEqual(flow.server_conn.via, ('http', ('127.0.0.1', 3128)))
            for host in ('storage.googleapis.com', 'code.claude.com', 'claude.com',
                         'api.anthropic.com.example.test', 'localhost'):
                flow = self.flow('https://' + host + '/')
                proxy.route(flow)
                self.assertEqual(flow.server_conn.via,
                                 ('http', ('127.0.0.1', 3129)) if restricted else None)

    async def test_untrusted_origins_and_ports_never_receive_credentials(self):
        mask = self.public['accessToken']
        for url in ('https://attacker.invalid/', 'http://api.anthropic.com/',
                    'https://api.anthropic.com.attacker.invalid/', 'https://api.anthropic.com:444/'):
            flow = self.flow(url, headers={'authorization': 'Bearer ' + mask})
            await self.proxy.request(flow)
            self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + mask)

    async def test_refresh_replaces_only_credential_field(self):
        mask = self.public['refreshToken']
        flow = self.flow('https://platform.claude.com/v1/oauth/token', 'POST',
            json.dumps({'grant_type': 'refresh_token', 'refresh_token': mask, 'scope': mask}).encode(),
            {'content-type': 'application/json'})
        await self.proxy.request(flow)
        payload = json.loads(flow.request.content)
        self.assertEqual(payload['refresh_token'], 'fixture-private-refresh')
        self.assertEqual(payload['scope'], mask)
        flow.response = http.Response.make(200, json.dumps({'access_token': 'rotated-private-access',
            'refresh_token': 'rotated-private-refresh', 'expires_in': 3600}), {'content-type': 'application/json'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('rotated-private', flow.response.get_text())
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_fresh_native_oauth_login_never_returns_real_tokens(self):
        self.store.path.unlink()
        flow = self.flow('https://platform.claude.com/v1/oauth/token', 'POST',
            b'grant_type=authorization_code&code=fixture-code',
            {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, json.dumps({'access_token': 'new-private-access',
            'refresh_token': 'new-private-refresh', 'expires_in': 3600,
            'account': {'uuid': 'fixture-account'}}), {'content-type': 'application/json'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('new-private', flow.response.get_text())
        self.assertEqual(json.loads(self.store.path.read_text())['claudeAiOauth']['accessToken'], 'new-private-access')

    async def test_upstream_401_and_refresh_errors_keep_diagnostics_without_credentials(self):
        for url, content in [('https://api.anthropic.com/v1/messages', b'{}'),
            ('https://platform.claude.com/v1/oauth/token', json.dumps({'grant_type': 'refresh_token',
             'refresh_token': self.public['refreshToken']}).encode())]:
            flow = self.flow(url, 'POST', content, {'content-type': 'application/json',
                'authorization': 'Bearer ' + self.public['accessToken']})
            await self.proxy.request(flow)
            flow.response = http.Response.make(401, json.dumps({'error': {'type': 'authentication_error',
                'message': 'Credential fixture-private-access was revoked; sign in again.'}}),
                {'content-type': 'application/json', 'x-request-id': 'fixture-request'})
            self.proxy.responseheaders(flow)
            self.proxy.response(flow)
            self.assertEqual(flow.response.status_code, 401)
            self.assertIn('was revoked; sign in again', flow.response.get_text())
            self.assertIn('authentication_error', flow.response.get_text())
            self.assertNotIn('fixture-private-access', flow.response.get_text())
            self.assertEqual(flow.response.headers['x-request-id'], 'fixture-request')
            self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_streaming_chunks_and_compressed_errors_redact_secrets(self):
        flow = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, b'', {'content-type': 'text/event-stream'})
        self.proxy.responseheaders(flow)
        stream = flow.response.stream
        result = stream(b'data: fixture-pri') + stream(b'vate-access\n\n') + stream(b'')
        self.assertNotIn(b'fixture-private-access', result)
        self.assertIn(self.public['accessToken'].encode(), result)
        flow.response = http.Response.make(400, b'fixture-private-access exact upstream reason')
        flow.response.encode('gzip')
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('fixture-private-access', flow.response.get_text())
        self.assertIn('exact upstream reason', flow.response.get_text())

    async def test_malformed_gzip_keeps_upstream_status_and_redacts_raw_error(self):
        flow = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(flow)
        flow.response = http.Response.make(502, b'fixture-private-access upstream emitted invalid gzip')
        flow.response.headers['content-encoding'] = 'gzip'
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertEqual(flow.response.status_code, 502)
        self.assertNotIn('fixture-private-access', flow.response.get_text())
        self.assertIn('upstream emitted invalid gzip', flow.response.get_text())

    async def test_reflected_credentials_are_redacted_from_response_headers(self):
        flow = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(flow)
        flow.response = http.Response.make(401, b'revoked', {'x-diagnostic': 'Bearer fixture-private-access'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertEqual(flow.response.status_code, 401)
        self.assertIn(self.public['accessToken'], flow.response.headers['x-diagnostic'])

    async def test_malformed_oauth_response_reports_cause_and_releases_refresh(self):
        for content in (b'not json', b'[]', b'{}'):
            flow = self.flow('https://platform.claude.com/v1/oauth/token', 'POST',
                json.dumps({'grant_type': 'refresh_token', 'refresh_token': self.public['refreshToken']}).encode(),
                {'content-type': 'application/json'})
            await self.proxy.request(flow)
            flow.response = http.Response.make(200, content, {'content-type': 'application/json'})
            self.proxy.responseheaders(flow)
            self.proxy.response(flow)
            self.assertEqual(flow.response.status_code, 502)
            self.assertIn('Claude credential service:', flow.response.get_text())
            self.assertIn('request_id', flow.response.get_text())
            self.assertFalse(self.proxy.refresh_lock.locked())
        followup = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(followup)
        self.assertEqual(followup.request.headers['authorization'], 'Bearer fixture-private-access')

    async def test_logout_stops_future_authentication_but_keeps_inflight_redaction(self):
        flow = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(flow)
        logout = self.flow('http://factory-claude.internal/logout', 'POST')
        await self.proxy.request(logout)
        self.assertEqual(logout.response.status_code, 200)
        self.assertIsNone(self.store.public()['auth'])
        flow.response = http.Response.make(401, b'fixture-private-access revoked')
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('fixture-private-access', flow.response.get_text())
        next_request = self.flow('https://api.anthropic.com/v1/messages', headers={
            'authorization': 'Bearer ' + self.public['accessToken']})
        await self.proxy.request(next_request)
        self.assertEqual(next_request.request.headers['authorization'], 'Bearer ' + self.public['accessToken'])

    async def test_refresh_finishing_after_logout_cannot_restore_subscription(self):
        refresh = self.flow('https://platform.claude.com/v1/oauth/token', 'POST',
            json.dumps({'grant_type': 'refresh_token', 'refresh_token': self.public['refreshToken']}).encode(),
            {'content-type': 'application/json'})
        await self.proxy.request(refresh)
        self.store.clear()
        refresh.response = http.Response.make(200, json.dumps({'access_token': 'new-private-access',
            'refresh_token': 'new-private-refresh'}), {'content-type': 'application/json'})
        self.proxy.responseheaders(refresh)
        self.proxy.response(refresh)
        self.assertEqual(refresh.response.status_code, 502)
        self.assertIn('sign-in changed', refresh.response.get_text())
        self.assertNotIn('new-private', refresh.response.get_text())
        self.assertIsNone(self.store.public()['auth'])
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_new_oauth_token_reflection_is_redacted_in_unknown_metadata(self):
        flow = self.flow('https://platform.claude.com/v1/oauth/token', 'POST',
            b'grant_type=authorization_code&code=fixture-code',
            {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, json.dumps({'access_token': 'new-private-access',
            'refresh_token': 'new-private-refresh', 'diagnostic': {
                'detail': 'Bearer new-private-access; refresh new-private-refresh; keep upstream detail'}}),
            {'content-type': 'application/json', 'x-debug': 'new-private-access'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertNotIn('new-private', flow.response.get_text())
        self.assertNotIn('new-private', flow.response.headers['x-debug'])
        self.assertIn('keep upstream detail', flow.response.get_text())


if __name__ == '__main__':
    unittest.main()
