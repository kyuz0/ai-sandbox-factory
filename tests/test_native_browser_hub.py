"""The native panel must retain the selected editor's browser boundary."""
import asyncio
import gzip
import sys
import unittest
from urllib.parse import quote

from ai_sandbox_factory.native_browser_hub import AntigravityRelay
from ai_sandbox_factory.browser_editor import validate_head


class NativeHubBoundary(unittest.TestCase):
    def setUp(self):
        self.relay = AntigravityRelay('a' * 64, '$argon2id$fixture+hash', 'https://127.0.0.2:8000')
        self.relay.host = '127.0.0.2:9000'
        self.relay.scheme = 'https'
        self.relay.worker_port = 12345
        self.cookie = 'code-server-session=' + quote(self.relay.password_hash, safe='')

    def head(self, path='/', **headers):
        values = {'Host': self.relay.host, 'Cookie': self.cookie, **headers}
        return ('GET ' + path + ' HTTP/1.1\r\n' + ''.join(
            key + ': ' + value + '\r\n' for key, value in values.items()) + '\r\n').encode()

    def test_editor_cookie_is_required_and_unambiguous(self):
        self.relay.validate(self.head())
        for cookie in ('', 'code-server-session=wrong', self.cookie + '; ' + self.cookie,
                       'code-server-session=%E2%98%83'):
            with self.subTest(cookie=cookie), self.assertRaises(ValueError):
                self.relay.validate(self.head(Cookie=cookie))

    def test_only_panel_document_allows_same_site_embedding(self):
        navigation = {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate',
                      'Sec-Fetch-Dest': 'iframe'}
        self.relay.validate(self.head(**navigation))
        for path in ('/connect-websocket', '/ReadFile', '/main.js'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.relay.validate(self.head(path, **navigation))
        with self.assertRaises(ValueError):
            validate_head(self.head(**navigation), self.relay.host, 'https')
        with self.assertRaises(ValueError):
            self.relay.validate(self.head(**{**navigation, 'Sec-Fetch-Site': 'cross-site'}))
        with self.assertRaises(ValueError):
            self.relay.validate(self.head(Origin='https://another-sandbox:9000'))

    def test_redirect_is_fixed_to_its_own_panel(self):
        headers = {'cookie': self.cookie}
        self.assertEqual(self.relay.redirect(self.head('/proxy/12345/?folder=x'), headers),
                         'https://127.0.0.2:9000/?folder=x')
        for path in ('/proxy/12345', '/proxy/not-a-port/'):
            self.assertIsNone(self.relay.redirect(self.head(path), headers))
        self.assertEqual(self.relay.redirect(self.head('/proxy/23456/'), headers), 'https://127.0.0.2:9000/')
        self.assertEqual(self.relay.worker_port, 23456)
        for port in ('0', '65536'):
            with self.assertRaisesRegex(ValueError, 'between 1 and 65535'):
                self.relay.redirect(self.head('/proxy/' + port + '/'), headers)
        with self.assertRaises(ValueError):
            self.relay.redirect(self.head('/proxy/12345/'), {})
        with self.assertRaises(ValueError):
            self.relay.redirect(self.head('/proxy/12345/').replace(b'GET ', b'POST ', 1), headers)

    def test_embedding_is_restricted_to_the_owning_editor(self):
        result = self.relay.response_headers(['HTTP/1.1 200 OK', "Content-Security-Policy: default-src 'none'",
                                             'Content-Type: text/html'])
        self.assertEqual(result[1], "Content-Security-Policy: default-src 'none'; frame-ancestors 'self' https://127.0.0.2:8000")
        self.assertEqual(result[2], 'Content-Type: text/html')

    def test_browser_cannot_choose_its_own_editor_origin(self):
        with self.assertRaises(ValueError):
            self.relay.validate(self.head(**{'X-Factory-Editor-Origin': 'https://unrelated.example'}))
        forwarded = self.relay.request_head(self.head())
        self.assertIn(b'Host: 127.0.0.1:12345\r\n', forwarded)
        self.assertNotIn(self.relay.host.encode(), forwarded)
        forwarded = self.relay.request_head(self.head(Origin='https://' + self.relay.host))
        self.assertIn(b'Origin: http://127.0.0.1:12345\r\n', forwarded)

    def test_only_native_parent_origin_check_changes_and_protocols_stay_unfiltered(self):
        original = b'function check(a,b){if(a===b||a.startsWith("vscode-webview://"))return true;}nativeRpc();'
        patch = self.relay.response_body_transform(self.head('/main.js'), 200, {})
        expected = original.replace(b'if(a===b', b'if(a==="https://127.0.0.2:8000"||a===b')
        self.assertEqual(patch(original), expected)
        compressed = self.relay.response_body_transform(self.head('/main.js'), 200, {'content-encoding': 'gzip'})
        self.assertEqual(gzip.decompress(compressed(gzip.compress(original))), expected)
        for path in ('/', '/main.css', '/api/Anything', '/connect-websocket'):
            self.assertIsNone(self.relay.response_body_transform(self.head(path), 200, {}))
        with self.assertRaisesRegex(ValueError, 'changed its parent-origin check'):
            patch(b'changed native frontend')
        with self.assertRaisesRegex(ValueError, 'Cannot decode.*gzip'):
            compressed(b'not gzip')
        forwarded = self.relay.request_head(self.head('/main.js', **{'Accept-Encoding': 'gzip'}))
        self.assertIn(b'Accept-Encoding: identity\r\n', forwarded)
        self.assertNotIn(b'Accept-Encoding: gzip', forwarded)

    def test_native_csp_is_narrowed_to_selected_editor(self):
        headers = self.relay.response_headers([
            "Content-Security-Policy: frame-ancestors 'self' http://localhost:* vscode-webview:; report-uri https://example.test/report"])
        self.assertEqual(headers, ["Content-Security-Policy: report-uri https://example.test/report; frame-ancestors 'self' https://127.0.0.2:8000"])


class NativeHubFraming(unittest.IsolatedAsyncioTestCase):
    async def test_native_chunked_frontend_adaptation_preserves_http_framing(self):
        relay = AntigravityRelay('a' * 64, '', 'http://127.0.0.2:8000')
        source = b'if(a===b||a.startsWith("vscode-webview://"))nativeRpc();'
        code = ('import sys; s=sys.stdin.buffer; line=s.readline(); '
                '\nwhile s.readline()!=b"\\r\\n": pass\n'
                'body=' + repr(source) + '\n'
                'sys.stdout.buffer.write(b"HTTP/1.1 200 OK\\r\\nTransfer-Encoding: chunked\\r\\nConnection: close\\r\\n\\r\\n"+'
                'hex(len(body))[2:].encode()+b"\\r\\n"+body+b"\\r\\n0\\r\\n\\r\\n");sys.stdout.flush()')
        async def connect():
            return await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', code,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        relay.connector = connect
        server = await asyncio.start_server(relay.handle, '127.0.0.1', 0)
        relay.host = '127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(f'GET /main.js HTTP/1.1\r\nHost: {relay.host}\r\n\r\n'.encode())
            await writer.drain()
            response = await reader.read()
            head, body = response.split(b'\r\n\r\n', 1)
            self.assertIn(b'200 OK', head)
            self.assertNotIn(b'Transfer-Encoding', head)
            self.assertIn(('Content-Length: ' + str(len(body))).encode(), head)
            self.assertEqual(body, source.replace(b'if(a===b', b'if(a==="http://127.0.0.2:8000"||a===b'))
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            await relay.close()


if __name__ == '__main__':
    unittest.main()
