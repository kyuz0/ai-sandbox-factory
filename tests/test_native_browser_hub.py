"""The native panel must retain the selected editor's browser boundary."""
import unittest
from urllib.parse import quote

from pi_sandbox_factory.native_browser_hub import AntigravityRelay
from pi_sandbox_factory.browser_editor import validate_head


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
        for path in ('/proxy/12346/', '/proxy/12345', '/proxy/123456/'):
            self.assertIsNone(self.relay.redirect(self.head(path), headers))
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
        self.assertIn(b'\r\nX-Factory-Editor-Origin: https://127.0.0.2:8000\r\n\r\n', forwarded)


if __name__ == '__main__':
    unittest.main()
