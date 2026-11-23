"""Native panel assets expose a public connection token and one owning editor."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'ai_sandbox_factory/containers/primary'))
from agy_editor_assets import CONFIG, ORIGIN_CHECK, Rejected, render


class AntigravityAssets(unittest.TestCase):
    def test_html_drops_private_configuration_and_external_preconnects(self):
        html = ('<script>window.__APP_CONFIG__ = ' + json.dumps({
            'productName': 'antigravity', 'csrfToken': 'private-test-token', 'appVersion': '1',
            'unexpectedCredential': 'private-test-token', 'allowedDomains': ['unrelated.example']}) +
            ';</script><link rel="preconnect" href="https://unrelated.example"><script src="/main.js"></script>')
        body = render('/', html.encode(), 'public-test-token', 'http://127.2.3.4:1234').decode()
        self.assertNotIn('private-test-token', body)
        self.assertNotIn('unrelated.example', body)
        config = json.loads(CONFIG.search(body).group(1))
        self.assertEqual(config['factoryEditorOrigin'], 'http://127.2.3.4:1234')
        self.assertEqual(config['csrfToken'], 'public-test-token')
        self.assertEqual(body.count('nonce="public-test-token"'), 2)

    def test_native_origin_check_adds_only_the_configured_editor(self):
        fixture = (ORIGIN_CHECK + '))return!0;').encode()
        result = render('/main.js', fixture, 'public').decode()
        self.assertIn('a===window.__APP_CONFIG__?.factoryEditorOrigin', result)
        self.assertNotIn('127.', result)
        for changed in (b'new native implementation', fixture + fixture):
            with self.assertRaises(Rejected):
                render('/main.js', changed, 'public')

    def test_asset_bytes_and_missing_origin_are_preserved(self):
        self.assertEqual(render('/jetbox.css', b'body{}', 'public'), b'body{}')
        html = b'<script>window.__APP_CONFIG__={"appVersion":"1"};</script>'
        self.assertNotIn(b'factoryEditorOrigin', render('/', html, 'public'))
        for origin in ('https://example/path', 'javascript:alert(1)', 'https://user:password@example'):
            with self.assertRaises(Rejected):
                render('/', html, 'public', origin)


if __name__ == '__main__':
    unittest.main()
