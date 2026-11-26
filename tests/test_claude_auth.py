"""Claude's native workspace processes receive opaque credentials only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from claude_auth import AuthStore
from codex_auth import atomic_json
import claude_client


def login(account='fixture-account'):
    return {'claudeAiOauth': {'accessToken': 'fixture-private-access',
        'refreshToken': 'fixture-private-refresh', 'expiresAt': 2100000000000,
        'refreshTokenExpiresAt': 2200000000000, 'scopes': ['user:inference', 'user:profile'],
        'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x', 'accountUuid': account,
        'organizationUuid': 'fixture-org'}}


class ClaudeAuthTests(unittest.TestCase):
    def test_sandbox_update_hooks_preserve_user_hooks_and_do_not_duplicate(self):
        home = Path(self.temp.name) / 'native'
        home.mkdir()
        original = {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': './project-hook'}]}]}
        (home / 'settings.json').write_text(json.dumps({'hooks': original}))
        settings = {'working_directory': '/workspace', 'browser': False}
        claude_client.configure(home, settings)
        first = (home / 'settings.json').read_bytes()
        claude_client.configure(home, settings)
        self.assertEqual((home / 'settings.json').read_bytes(), first)
        hooks = json.loads(first)['hooks']
        self.assertEqual(hooks['UserPromptSubmit'][0], original['UserPromptSubmit'][0])
        for event, stage in [('UserPromptSubmit', 'before'), ('Stop', 'after')]:
            commands = [row['command'] for group in hooks[event] for row in group['hooks']]
            self.assertIn(f'exec /usr/bin/python3 -I /opt/sandbox/sandbox-update.py claude {stage}', commands)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(self.temp.name)
        atomic_json(self.store.path, login())

    def test_host_copy_keeps_subscription_metadata_and_masks_credentials(self):
        private = login()['claudeAiOauth']
        public = self.store.public()
        oauth = public['auth']['claudeAiOauth']
        for key in ('accountUuid', 'organizationUuid', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier'):
            self.assertEqual(oauth[key], private[key])
        for key in ('accessToken', 'refreshToken'):
            self.assertNotIn(private[key], json.dumps(public))
            self.assertEqual(self.store.substitutions()[oauth[key]], private[key])
        self.assertEqual(AuthStore(self.temp.name).public(), public)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_refresh_rotates_without_exposing_tokens_and_keeps_running_clients(self):
        before = self.store.public()
        masks = before['auth']['claudeAiOauth']
        result = self.store.capture({'access_token': 'rotated-private-access',
            'refresh_token': 'rotated-private-refresh', 'expires_in': 3600,
            'refresh_token_expires_in': 7200, 'scope': 'user:inference'})
        self.assertNotIn('rotated-private', json.dumps(result))
        self.assertEqual(self.store.substitutions()[masks['accessToken']], 'rotated-private-access')
        self.assertEqual(self.store.substitutions()[masks['refreshToken']], 'rotated-private-refresh')
        self.assertEqual(self.store.public()['generation'], before['generation'])
        self.assertEqual(self.store.public()['auth']['claudeAiOauth']['subscriptionType'], 'max')
        import time
        expiry = self.store.public()['auth']['claudeAiOauth']['refreshTokenExpiresAt']
        self.assertGreater(expiry, time.time() * 1000 + 7190000)
        self.assertLess(expiry, time.time() * 1000 + 7210000)

    def test_new_native_sign_in_stores_oauth_response_only_in_companion(self):
        self.store.path.unlink()
        self.assertIsNone(self.store.public()['auth'])
        result = self.store.capture({'access_token': 'new-private-access', 'refresh_token': 'new-private-refresh',
            'expires_in': 3600, 'scope': 'user:inference user:profile',
            'account': {'uuid': 'new-account'}, 'organization': {'uuid': 'new-org'}}, new_login=True)
        self.assertNotIn('new-private', json.dumps(result))
        private = json.loads(self.store.path.read_text())['claudeAiOauth']
        self.assertEqual(private['accessToken'], 'new-private-access')
        self.assertEqual(private['accountUuid'], 'new-account')
        self.assertEqual(self.store.public()['auth']['claudeAiOauth']['organizationUuid'], 'new-org')

    def test_account_change_revokes_previous_placeholders(self):
        before = self.store.public()
        atomic_json(self.store.path, login('different-account'))
        after = self.store.public()
        self.assertNotEqual(before['generation'], after['generation'])
        self.assertNotIn(before['auth']['claudeAiOauth']['accessToken'], self.store.substitutions())

    def test_unknown_private_fields_are_not_published(self):
        data = login()
        data['claudeAiOauth']['futureCredential'] = 'private-future-credential'
        atomic_json(self.store.path, data)
        self.assertNotIn('private-future', json.dumps(self.store.public()))

    def test_failed_oauth_capture_preserves_previous_credentials(self):
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'omitted access_token'):
            self.store.capture({'error': 'fixture-rejected'}, new_login=True)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_native_flags_and_customization_are_preserved(self):
        supplied = ['--model', 'sonnet', '--effort', 'high', '--permission-mode', 'plan',
            '--mcp-config', 'project-mcp.json', '--plugin-dir', './plugin', '-p', 'Read an image']
        result = claude_client.arguments({'native_approval': 'automatic'}, supplied, 'workspace context')
        self.assertEqual(result, supplied)
        self.assertNotIn('--tools', result)
        self.assertNotIn('--restricted', result)

    def test_native_cli_commands_and_extension_wrapper_path(self):
        for supplied in (['auth', 'login'], ['plugin', 'list', '--json'], ['--version']):
            self.assertEqual(claude_client.arguments({}, supplied, ''), supplied)
        result = claude_client.arguments({}, ['/extension/native-binary/claude', '--input-format', 'stream-json'], 'context')
        self.assertEqual(result, ['--input-format', 'stream-json'])
        self.assertNotIn('/extension/native-binary/claude', result)
        self.assertIn('stream-json', result)

    def test_configure_keeps_native_settings_and_other_trusted_projects(self):
        home = Path(self.temp.name) / '.claude'
        home.mkdir(exist_ok=True)
        config = {'model': 'opus', 'projects': {'/other': {'hasTrustDialogAccepted': False}},
                  'mcpServers': {'user-server': {'command': 'user-command'}}}
        atomic_json(home / '.claude.json', config)
        settings = {'working_directory': '/workspace', 'browser': True, 'native_approval': 'automatic'}
        claude_client.configure(home, settings)
        actual = json.loads((home / '.claude.json').read_text())
        self.assertEqual(actual['model'], 'opus')
        self.assertEqual(actual['projects']['/other'], config['projects']['/other'])
        self.assertTrue(actual['projects']['/workspace']['hasTrustDialogAccepted'])
        self.assertEqual(actual['mcpServers']['user-server'], config['mcpServers']['user-server'])
        claude_client.configure(home, {**settings, 'browser': False})
        actual = json.loads((home / '.claude.json').read_text())
        self.assertNotIn('factory_workspace', actual['mcpServers'])
        self.assertIn('user-server', actual['mcpServers'])

    def test_native_saved_approval_choice_survives_unchanged_factory_settings(self):
        home = Path(self.temp.name) / '.claude'
        home.mkdir(exist_ok=True)
        settings = {'working_directory': '/workspace', 'native_approval': 'automatic', 'browser': False}
        claude_client.configure(home, settings)
        native = home / 'settings.json'
        self.assertEqual(json.loads(native.read_text())['permissions']['defaultMode'], 'auto')
        atomic_json(native, {'permissions': {'defaultMode': 'plan'}, 'model': 'sonnet'})
        claude_client.configure(home, settings)
        self.assertEqual(json.loads(native.read_text())['permissions']['defaultMode'], 'plan')
        claude_client.configure(home, {**settings, 'native_approval': 'manual'})
        saved = json.loads(native.read_text())
        self.assertEqual(saved['permissions']['defaultMode'], 'default')
        self.assertEqual(saved['model'], 'sonnet')

    def test_starting_subfolder_trust_includes_mounted_project_root(self):
        home = Path(self.temp.name) / '.claude'
        home.mkdir(exist_ok=True)
        claude_client.configure(home, {'working_directory': '/workspace/src café', 'browser': False})
        projects = json.loads((home / '.claude.json').read_text())['projects']
        self.assertTrue(projects['/workspace']['hasTrustDialogAccepted'])
        self.assertTrue(projects['/workspace/src café']['hasTrustDialogAccepted'])
        self.assertNotIn('/mnt', projects)

    def test_managed_context_preserves_user_instructions_and_updates_only_its_block(self):
        home = Path(self.temp.name) / '.claude'
        home.mkdir(exist_ok=True)
        (home / 'CLAUDE.md').write_text('User instructions.\n')
        claude_client.instructions(home, 'First managed context.')
        claude_client.instructions(home, 'Second managed context.')
        actual = (home / 'CLAUDE.md').read_text()
        self.assertIn('User instructions.', actual)
        self.assertIn('Second managed context.', actual)
        self.assertNotIn('First managed context.', actual)
        self.assertEqual(actual.count('<!-- Factory managed instructions -->'), 1)

    def test_logout_removes_only_the_sandbox_copy_and_persists_signed_out_state(self):
        before = self.store.public()
        self.store.clear()
        self.assertFalse(self.store.path.exists())
        self.assertIsNone(AuthStore(self.temp.name).public()['auth'])
        self.assertNotEqual(self.store.public()['generation'], before['generation'])
        self.assertFalse(self.store.substitutions())

    def test_web_search_defaults_off_without_disabling_web_fetch(self):
        home = Path(self.temp.name) / '.claude'
        home.mkdir(exist_ok=True)
        atomic_json(home / 'settings.json', {'permissions': {'allow': ['WebSearch', 'WebFetch'],
                    'deny': ['Bash(rm *)'], 'ask': ['Edit']}, 'model': 'sonnet'})
        settings = {'working_directory': '/workspace', 'native_approval': 'automatic'}
        claude_client.configure(home, settings)
        current = json.loads((home / 'settings.json').read_text())
        self.assertEqual(current['permissions']['deny'], ['Bash(rm *)', 'WebSearch'])
        self.assertEqual(current['permissions']['allow'], ['WebSearch', 'WebFetch'])
        self.assertEqual(current['permissions']['ask'], ['Edit'])
        claude_client.configure(home, {**settings, 'claude_web_search': True})
        current = json.loads((home / 'settings.json').read_text())
        self.assertEqual(current['permissions']['deny'], ['Bash(rm *)'])
        self.assertEqual(current['model'], 'sonnet')

    def test_logout_notifies_companion_only_after_native_success_with_launch_flags(self):
        for code in (0, 1):
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"signed_out":true}')))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch.object(sys, 'argv', ['claude', '/extension/claude', '--safe-mode', 'auth', 'logout']), \
                    patch.object(claude_client, 'prepare', return_value={}), \
                    patch.object(claude_client.subprocess, 'run', return_value=Mock(returncode=code)) as native, \
                    patch.object(claude_client, 'build_opener', return_value=opener), \
                    self.assertRaises(SystemExit) as stopped:
                claude_client.main()
            self.assertEqual(stopped.exception.code, code)
            self.assertEqual(native.call_args.args[0], ['/usr/local/bin/factory-agent', '--safe-mode', 'auth', 'logout'])
            self.assertEqual(opener.open.call_count, 1 if code == 0 else 0)
            if code == 0:
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, 'http://factory-claude.internal/logout')
                self.assertEqual(request.method, 'POST')


if __name__ == '__main__':
    unittest.main()
