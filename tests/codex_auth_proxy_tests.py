"""Run with the pinned mitmproxy dependency installed; all credentials are synthetic."""
import json
import asyncio
import gzip
from pathlib import Path
import tempfile
import unittest
import sys

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from mitmproxy import http, websocket
from mitmproxy.test import tflow
from codex_auth import AuthStore, atomic_json
from codex_auth_proxy import NETWORK, SubscriptionProxy
from test_codex_auth import login, token


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(self.temp.name)
        atomic_json(self.store.path, login())
        self.public = self.store.public()
        self.proxy = SubscriptionProxy(self.store)

    def flow(self, url, method='GET', content=b'', headers=None):
        flow = tflow.tflow()
        flow.request = http.Request.make(method, url, content, headers or {})
        return flow

    async def test_native_resource_requests_keep_their_path_and_body(self):
        mask = self.public['auth']['tokens']['access_token']
        flow = self.flow('https://chatgpt.com/backend-api/a-new-native-feature?x=1', 'POST', b'unchanged',
            {'authorization': 'Bearer ' + mask})
        await self.proxy.request(flow)
        self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + login()['tokens']['access_token'])
        self.assertEqual(flow.request.content, b'unchanged')
        self.assertEqual(flow.request.path, '/backend-api/a-new-native-feature?x=1')
        self.assertIsNone(flow.response)

    async def test_literal_prompt_headers_and_body_never_receive_private_credentials(self):
        mask = self.public['auth']['tokens']['access_token']
        real = login()['tokens']['access_token']
        body = json.dumps({'input': [{'role': 'user', 'content': mask}]}).encode()
        flow = self.flow('https://chatgpt.com/backend-api/codex/responses', 'POST', body,
                         {'authorization': 'Bearer ' + mask, 'x-literal-prompt': mask})
        await self.proxy.request(flow)
        self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + real)
        self.assertEqual(flow.request.headers['x-literal-prompt'], mask)
        self.assertEqual(flow.request.content, body)
        self.assertNotIn(real.encode(), flow.request.content)
        for value in ('prefix Bearer ' + mask, 'Bearer ' + mask + ' suffix'):
            literal = self.flow('https://chatgpt.com/backend-api/codex/responses',
                                headers={'authorization': value})
            await self.proxy.request(literal)
            self.assertEqual(literal.request.headers['authorization'], value)

    async def test_oauth_replaces_only_the_grants_token_field(self):
        from urllib.parse import urlencode, parse_qsl
        for grant, field, token_name in [
            ('refresh_token', 'refresh_token', 'refresh_token'),
        ]:
            with self.subTest(grant=grant):
                mask = self.public['auth']['tokens'][token_name]
                body = {'grant_type': grant, field: mask, 'scope': mask, 'client_id': mask}
                flow = self.flow('https://auth.openai.com/oauth/token', 'POST', urlencode(body).encode(),
                                 {'content-type': 'application/x-www-form-urlencoded'})
                await self.proxy.request(flow)
                changed = dict(parse_qsl(flow.request.content.decode()))
                self.assertEqual(changed[field], login()['tokens'][token_name])
                self.assertEqual(changed['scope'], mask)
                self.assertEqual(changed['client_id'], mask)
                self.proxy.release(flow)

    async def test_optional_api_key_exchange_cannot_replace_subscription_or_create_a_key(self):
        before = self.store.path.read_bytes()
        before_public = self.store.public()
        mask = before_public['auth']['tokens']['id_token']
        for content_type in ('application/json', 'application/x-www-form-urlencoded'):
            with self.subTest(content_type=content_type):
                from urllib.parse import urlencode
                payload = {'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                           'requested_token': 'openai-api-key', 'subject_token': mask,
                           'subject_token_type': 'urn:ietf:params:oauth:token-type:id_token'}
                body = json.dumps(payload).encode() if content_type == 'application/json' else urlencode(payload).encode()
                flow = self.flow('https://auth.openai.com/oauth/token', 'POST', body,
                                 {'content-type': content_type})
                await self.proxy.request(flow)
                self.assertEqual(flow.response.status_code, 400)  # Local response prevents upstream issuance.
                self.assertIn('Continue signing in with ChatGPT', flow.response.get_text())
                self.assertEqual(flow.request.content, body)
                self.proxy.responseheaders(flow)
                self.proxy.response(flow)
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertEqual(self.store.public(), before_public)
                self.assertNotIn(login()['tokens']['id_token'], flow.response.get_text())
                # Even an unexpected successful response must not be treated as
                # subscription tokens or pass a newly issued API key to Codex.
                flow.response = http.Response.make(200, json.dumps({'access_token': 'synthetic-issued-api-key'}),
                                                   {'content-type': 'application/json'})
                self.proxy.responseheaders(flow)
                self.proxy.response(flow)
                self.assertEqual(flow.response.status_code, 502)
                self.assertNotIn('synthetic-issued-api-key', flow.response.get_text())
                self.assertEqual(self.store.path.read_bytes(), before)
        # A subscription authorization-code exchange still persists native login
        # tokens, as native browser sign-in does after optional API-key failure.
        flow = self.flow('https://auth.openai.com/oauth/token', 'POST', b'grant_type=authorization_code&code=fixture',
                         {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        self.assertIsNone(flow.response)
        exchanged = login()['tokens'] | {'access_token': token('new-subscription-access')}
        flow.response = http.Response.make(200, json.dumps(exchanged), {'content-type': 'application/json'})
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertEqual(json.loads(self.store.path.read_text())['tokens']['access_token'], exchanged['access_token'])
        self.assertNotIn(exchanged['access_token'], flow.response.get_text())

    async def test_redirect_and_custom_provider_cannot_receive_real_auth(self):
        mask = self.public['auth']['tokens']['access_token']
        for url in ('https://attacker.invalid/collect', 'http://chatgpt.com/collect',
                    'https://chatgpt.com.attacker.invalid/collect', 'https://chatgpt.com:444/collect'):
            flow = self.flow(url, headers={'authorization': 'Bearer ' + mask})
            await self.proxy.request(flow)
            self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + mask)

    async def test_catalog_injects_only_recognized_placeholders_on_credential_hosts(self):
        mask = self.public['auth']['tokens']['access_token']
        real = login()['tokens']['access_token']
        for host in NETWORK['credential_hosts']:
            with self.subTest(host=host):
                flow = self.flow('https://' + host + '/native-feature',
                    headers={'authorization': 'Bearer ' + mask, 'x-custom': 'custom-credential'})
                await self.proxy.request(flow)
                self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + real)
                self.assertEqual(flow.request.headers['x-custom'], 'custom-credential')
                custom = self.flow('https://' + host + '/native-feature',
                    headers={'authorization': 'Bearer custom-bearer'})
                await self.proxy.request(custom)
                self.assertEqual(custom.request.headers['authorization'], 'Bearer custom-bearer')

    async def test_catalog_transport_does_not_expand_credential_boundary(self):
        mask = self.public['auth']['tokens']['access_token']
        hosts = set(NETWORK['transport_hosts']) | set(NETWORK['transport_domains'])
        hosts |= {'sub.' + domain for domain in NETWORK['transport_domains']}
        for host in hosts - set(NETWORK['credential_hosts']):
            with self.subTest(host=host):
                flow = self.flow('https://' + host + '/native-feature',
                    headers={'authorization': 'Bearer ' + mask})
                await self.proxy.request(flow)
                self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + mask)

    async def test_all_credential_hosts_require_https_and_standard_port(self):
        mask = self.public['auth']['tokens']['access_token']
        for host in NETWORK['credential_hosts']:
            for url in ('http://' + host + '/', 'http://' + host + ':443/',
                        'https://' + host + ':444/', 'https://' + host + '.attacker.invalid/'):
                with self.subTest(url=url):
                    flow = self.flow(url, headers={'authorization': 'Bearer ' + mask})
                    await self.proxy.request(flow)
                    self.assertEqual(flow.request.headers['authorization'], 'Bearer ' + mask)

    async def test_only_public_stub_is_readable_and_not_writable(self):
        original = self.store.path.read_bytes()
        flow = self.flow('http://factory-codex.internal/auth')
        await self.proxy.request(flow)
        self.assertEqual(json.loads(flow.response.content), self.public)
        for method in ('POST', 'PUT', 'DELETE'):
            flow = self.flow('http://factory-codex.internal/auth', method, b'overwrite')
            await self.proxy.request(flow)
            self.assertEqual(flow.response.status_code, 404)
        self.assertEqual(self.store.path.read_bytes(), original)

    async def test_refresh_request_and_response_keep_real_tokens_private(self):
        body = {'grant_type': 'refresh_token', 'refresh_token': self.public['auth']['tokens']['refresh_token']}
        flow = self.flow('https://auth.openai.com/oauth/token', 'POST', json.dumps(body).encode(),
                         {'content-type': 'application/json'})
        await self.proxy.request(flow)
        self.assertEqual(json.loads(flow.request.content)['refresh_token'], login()['tokens']['refresh_token'])
        real = {'access_token': token('refreshed-access'), 'refresh_token': 'refreshed-private'}
        flow.response = http.Response.make(200, json.dumps(real))
        self.proxy.responseheaders(flow)
        self.assertFalse(flow.response.stream)
        self.proxy.response(flow)
        self.assertNotIn('refreshed-private', flow.response.get_text())
        self.assertNotIn('refreshed-access', flow.response.get_text())
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_refresh_error_retains_reason_without_credential(self):
        body = {'grant_type': 'refresh_token', 'refresh_token': self.public['auth']['tokens']['refresh_token']}
        flow = self.flow('https://auth.openai.com/oauth/token', 'POST', json.dumps(body).encode(),
                         {'content-type': 'application/json'})
        await self.proxy.request(flow)
        flow.response = http.Response.make(401, 'refresh_token_expired: private-refresh-secret')
        self.proxy.response(flow)
        self.assertEqual(flow.response.status_code, 401)
        self.assertIn('refresh_token_expired', flow.response.get_text())
        self.assertNotIn('private-refresh-secret', flow.response.get_text())
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_streaming_replies_are_not_feature_filtered(self):
        mask = self.public['auth']['tokens']['access_token']
        flow = self.flow('https://chatgpt.com/backend-api/codex/responses', headers={'authorization': 'Bearer ' + mask})
        await self.proxy.request(flow)
        flow.response = http.Response.make(200, b'', {'content-type': 'text/event-stream'})
        self.proxy.responseheaders(flow)
        self.assertEqual(flow.response.stream(b'data: {"native":"event"}\n\n'), b'data: {"native":"event"}\n\n')

    async def test_websocket_messages_preserve_unknown_native_features(self):
        mask = self.public['auth']['tokens']['access_token']
        real = login()['tokens']['access_token']
        flow = self.flow('https://chatgpt.com/backend-api/codex/responses', headers={'authorization': 'Bearer ' + mask})
        await self.proxy.request(flow)
        flow.websocket = websocket.WebSocketData()
        for from_client, source, expected in [(True, mask, mask), (False, real, mask)]:
            flow.websocket.messages.append(websocket.WebSocketMessage(1, from_client,
                json.dumps({'newFeature': True, 'token': source}).encode()))
            self.proxy.websocket_message(flow)
            self.assertEqual(json.loads(flow.websocket.messages[-1].content), {'newFeature': True, 'token': expected})

    async def test_repeated_headers_and_oauth_error_headers_survive(self):
        mask = self.public['auth']['tokens']['access_token']
        flow = self.flow('https://auth.openai.com/oauth/token', 'POST', b'grant_type=authorization_code&code=fixture',
            {'content-type': 'application/x-www-form-urlencoded', 'authorization': 'Bearer ' + mask})
        await self.proxy.request(flow)
        flow.response = http.Response.make(401, b'precise upstream reason',
            [(b'set-cookie', b'first=1'), (b'set-cookie', b'second=2'),
             (b'x-detail', login()['tokens']['access_token'].encode())])
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        self.assertEqual(flow.response.headers.get_all('set-cookie'), ['first=1', 'second=2'])
        self.assertEqual(flow.response.headers['x-detail'], mask)
        self.assertEqual(flow.response.content, b'precise upstream reason')

    async def test_concurrent_refresh_uses_latest_rotated_refresh_token(self):
        old_mask = self.public['auth']['tokens']['refresh_token']
        def refresh():
            return self.flow('https://auth.openai.com/oauth/token', 'POST',
                json.dumps({'grant_type': 'refresh_token', 'refresh_token': old_mask}).encode(),
                {'content-type': 'application/json'})
        first, second = refresh(), refresh()
        await self.proxy.request(first)
        waiting = asyncio.create_task(self.proxy.request(second))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        first.response = http.Response.make(200, json.dumps({'access_token': token('new'), 'refresh_token': 'new-refresh'}))
        self.proxy.response(first)
        await waiting
        self.assertEqual(json.loads(second.request.content)['refresh_token'], 'new-refresh')
        self.proxy.error(second)
        self.assertFalse(self.proxy.refresh_lock.locked())

    async def test_general_destinations_follow_configured_network_mode(self):
        flow = self.flow('https://example.org/file')
        self.proxy.requestheaders(flow)
        self.assertEqual(flow.server_conn.via, ('http', ('127.0.0.1', 3129)))
        self.proxy.restricted = False
        self.proxy.requestheaders(flow)
        self.assertIsNone(flow.server_conn.via)

    async def test_first_oauth_sign_in_masks_tokens_without_existing_auth_file(self):
        self.store.path.unlink()
        flow = self.flow('https://auth.openai.com/oauth/token', 'POST',
            b'grant_type=authorization_code&code=synthetic-code',
            {'content-type': 'application/x-www-form-urlencoded'})
        await self.proxy.request(flow)
        real = login()['tokens']
        flow.response = http.Response.make(200, json.dumps(real))
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        returned = json.loads(flow.response.content)
        persisted = json.loads(self.store.path.read_text())['tokens']
        for name in ('access_token', 'refresh_token', 'id_token'):
            self.assertNotEqual(returned[name], real[name])
            self.assertEqual(persisted[name], real[name])
            self.assertEqual(self.store.substitutions()[returned[name]], real[name])

    async def test_compressed_upstream_error_preserves_reason_and_redacts_tokens(self):
        mask = self.public['auth']['tokens']['access_token']
        real = login()['tokens']['access_token']
        flow = self.flow('https://chatgpt.com/backend-api/codex/responses',
            headers={'authorization': 'Bearer ' + mask})
        await self.proxy.request(flow)
        flow.response = http.Response.make(403, b'', {'content-encoding': 'gzip'})
        flow.response.raw_content = gzip.compress(('precise upstream error: ' + real).encode())
        self.proxy.responseheaders(flow)
        self.proxy.response(flow)
        decoded = gzip.decompress(flow.response.raw_content).decode()
        self.assertEqual(flow.response.status_code, 403)
        self.assertEqual(decoded, 'precise upstream error: ' + mask)

    async def test_transport_diagnostic_names_path_without_query_credentials(self):
        from mitmproxy.flow import Error
        flow = self.flow('https://chat.openai.com/ces/v1/telemetry/intake?private=synthetic-query-secret', 'POST')
        flow.error = Error('Upstream proxy refused HTTP CONNECT request: 403 Forbidden')
        with self.assertLogs(level='ERROR') as captured:
            self.proxy.error(flow)
        diagnostic = '\n'.join(captured.output)
        self.assertIn('POST chat.openai.com/ces/v1/telemetry/intake', diagnostic)
        self.assertIn(flow.id, diagnostic)
        self.assertIn('403 Forbidden', diagnostic)
        self.assertNotIn('synthetic-query-secret', diagnostic)


if __name__ == '__main__':
    unittest.main()
