"""Native Antigravity settings preserve other customizations."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from subprocess import CompletedProcess
from urllib.error import HTTPError
from http.client import IncompleteRead
import io
import os

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from agy_client import configure_browser, configure_hub_permissions, configure_search, keyring, native_preferences, public_auth, read_configuration, search_policy


class AntigravityClientTests(unittest.TestCase):
    def test_hub_manual_preset_and_factory_grant_changes_preserve_user_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            path = home / 'config/config.json'
            path.write_text(json.dumps({'userSettings': {'theme': 'native-user-theme',
                'globalPermissionGrants': {'allow': ['command(git status)'], 'deny': ['command(rm)']}}}))
            configure_hub_permissions(home, {'native_approval': 'automatic', 'browser': True})
            automatic = json.loads(path.read_text())['userSettings']
            self.assertIn('command(*)', automatic['globalPermissionGrants']['allow'])
            self.assertIn('mcp(factory_workspace/browser)', automatic['globalPermissionGrants']['allow'])
            configure_hub_permissions(home, {'native_approval': 'manual', 'browser': True})
            manual = json.loads(path.read_text())['userSettings']
            self.assertEqual(manual['permissionPreset'], 'AGENT_PERMISSION_PRESET_REQUEST_REVIEW')
            self.assertEqual(manual['cascadeAutoExecutionPolicy'], 'CASCADE_COMMANDS_AUTO_EXECUTION_OFF')
            self.assertFalse(manual['enableTerminalSandbox'])
            self.assertEqual(manual['globalPermissionGrants'], {'allow': ['command(git status)'], 'deny': ['command(rm)']})
            self.assertEqual(manual['theme'], 'native-user-theme')
            manual['permissionPreset'] = 'AGENT_PERMISSION_PRESET_DEFAULT'
            path.write_text(json.dumps({'userSettings': manual}))
            configure_hub_permissions(home, {'native_approval': 'manual', 'browser': True})
            self.assertEqual(json.loads(path.read_text())['userSettings']['permissionPreset'], 'AGENT_PERMISSION_PRESET_DEFAULT')

    def test_invalid_existing_config_names_the_file_and_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            for name, configure in [('hooks.json', configure_search), ('mcp_config.json', lambda home: configure_browser(home, True))]:
                path = home / 'config' / name
                path.write_text('{broken')
                with self.assertRaises(ValueError) as failed:
                    configure(home)
                self.assertIn(str(path), str(failed.exception))
                self.assertIn('JSONDecodeError', str(failed.exception))
                self.assertIn('line 1 column 2', str(failed.exception))
                self.assertEqual(path.read_text(), '{broken')

    def test_missing_managed_settings_explains_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with self.assertRaises(ValueError) as failed:
                read_configuration(path)
            self.assertIn(str(path), str(failed.exception))
            self.assertIn('Reopen the sandbox from Factory', str(failed.exception))

    def test_keyring_probe_retains_exit_cause_and_socket(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch('agy_client.BUS', Path(directory) / 'bus'), \
                patch('agy_client.socket.socket') as socket_mock, patch('agy_client.subprocess.run',
                    return_value=CompletedProcess([], 7, '', 'Connection refused by the session bus')):
            socket_mock.return_value.__enter__.return_value.connect.return_value = None
            with self.assertRaises(OSError) as failed:
                keyring()
            self.assertIn(str(Path(directory) / 'bus'), str(failed.exception))
            self.assertIn('exited 7', str(failed.exception))
            self.assertIn('Connection refused by the session bus', str(failed.exception))

    def test_credential_service_failure_retains_http_body_and_request_id(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('http://factory-agy.internal/auth', 503, 'Unavailable', {},
            io.BytesIO(b'{"error":"Secret Service connection reset","request_id":"fixture-request"}'))
        with self.assertRaises(OSError) as failed:
            public_auth(opener)
        for expected in ('HTTP 503', 'connection reset', 'fixture-request', 'factory-agy.internal/auth'):
            self.assertIn(expected, str(failed.exception))

    def test_truncated_credential_error_keeps_status_partial_body_and_closes_response(self):
        class InterruptedBody(io.BytesIO):
            def read(self, *args):
                raise IncompleteRead(b'{"error":"connection reset"', 32)
        body = InterruptedBody()
        opener = Mock()
        opener.open.side_effect = HTTPError('http://factory-agy.internal/auth', 502, 'Bad Gateway', {}, body)
        with self.assertRaises(OSError) as failed:
            public_auth(opener)
        self.assertIn('HTTP 502', str(failed.exception))
        self.assertIn('connection reset', str(failed.exception))
        self.assertIn('Error body incomplete', str(failed.exception))
        self.assertTrue(body.closed)

    def test_supported_native_modes_keep_approval_and_container_boundaries_separate(self):
        base = {'working_directory': '/workspace/project', 'browser': True}
        automatic = native_preferences({**base, 'native_approval': 'automatic'})
        manual = native_preferences({**base, 'native_approval': 'manual'})
        self.assertEqual(automatic['agentMode'], 'accept-edits')
        self.assertEqual(manual['agentMode'], '')
        self.assertFalse(automatic['enableTerminalSandbox'])
        self.assertFalse(manual['enableTerminalSandbox'])
        self.assertEqual(manual['toolPermission'], 'request-review')
        self.assertNotIn('permissions', manual)
        self.assertIn('mcp(factory_workspace/browser)', automatic['permissions']['allow'])
        self.assertEqual(automatic['trustedWorkspaces'], ['/workspace/project'])

    def test_browser_native_mcp_toggle_keeps_other_servers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            path = home / 'config/mcp_config.json'
            custom = {'mcpServers': {'user_server': {'command': 'example-server'}}}
            path.write_text(json.dumps(custom))
            configure_browser(home, True)
            enabled = json.loads(path.read_text())
            self.assertEqual(enabled['mcpServers']['user_server'], custom['mcpServers']['user_server'])
            self.assertEqual(enabled['mcpServers']['factory_workspace']['args'],
                             ['STDIO', 'UNIX-CONNECT:/run/primary-workspace/tools.sock'])
            configure_browser(home, False)
            self.assertEqual(json.loads(path.read_text()), custom)

    def test_search_defaults_off_and_toggles_without_blocking_web_fetch(self):
        search = {'toolCall': {'name': 'search_web'}}
        self.assertEqual(search_policy(search, {})['decision'], 'deny')
        self.assertIn('Enable Web Search', search_policy(search, {})['reason'])
        self.assertEqual(search_policy(search, {'agy_web_search': True}), {'decision': 'allow'})
        self.assertEqual(search_policy({'toolCall': {'name': 'read_url_content'}}, {}), {'decision': 'allow'})
        self.assertEqual(search_policy({'toolCall': {'name': 'run_command'}}, {}), {'decision': 'allow'})

    def test_native_hook_preserves_user_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            path = home / 'config/hooks.json'
            existing = {'user-linter': {'PostToolUse': [{'matcher': 'write_to_file', 'hooks': [{'command': './lint'}]}]}}
            path.write_text(json.dumps(existing))
            configure_search(home)
            first = path.read_bytes()
            configure_search(home)
            self.assertEqual(first, path.read_bytes())
            configured = json.loads(first)
            self.assertEqual(configured['user-linter'], existing['user-linter'])
            self.assertEqual(configured['factory-native-web-search']['PreToolUse'][0]['matcher'], '^search_web$')


if __name__ == '__main__':
    unittest.main()
