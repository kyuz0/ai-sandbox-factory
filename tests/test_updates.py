import io
import sys
import unittest
from unittest.mock import patch

from ai_sandbox_factory import updates


class UpdateTests(unittest.TestCase):
    def test_latest_stable_tag_and_comparison(self):
        payload=b'[{"name":"v2026.11.6.120000"},{"name":"v2026.11.7.90000"},{"name":"v2027.1.1rc1"},{"name":"junk"},null]'
        with patch.object(updates.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(payload)) as request:
            self.assertEqual(updates.available_update('2026.11.6.120000'),'2026.11.7.90000')
            self.assertIsNone(updates.available_update('2026.11.7.90000'))
            self.assertIsNone(updates.available_update('2027.1.1'))
            self.assertEqual(request.call_args.args[0].full_url,updates.TAGS_URL)
            self.assertEqual(request.call_args.kwargs['timeout'],5)

    def test_offline_and_malformed_replies_are_quiet(self):
        with patch.object(updates.urllib.request,'urlopen',side_effect=OSError('offline')):
            self.assertIsNone(updates.available_update('1.0'))
        for value in (b'{}',b'null',b'not json',b'[]'):
            with patch.object(updates.urllib.request,'urlopen',return_value=io.BytesIO(value)):
                self.assertIsNone(updates.available_update('1.0'))

    def test_upgrade_command_and_pipx_detection(self):
        self.assertEqual(updates.UPGRADE_COMMAND,('pipx','upgrade','ai-sandbox-factory'))
        with patch.object(updates.Path,'is_file',return_value=True):
            self.assertTrue(updates.pipx_install())

    def test_upgrade_retains_stdout_and_stderr_when_the_command_fails(self):
        command = (sys.executable, '-c', 'import sys; print("Installing", flush=True); '
                   'print("Connection refused", file=sys.stderr); sys.exit(3)')
        output = []
        with patch.object(updates, 'UPGRADE_COMMAND', command):
            with self.assertRaisesRegex(ValueError, 'pipx exited with status 3'):
                updates.upgrade(output.append)
        self.assertEqual(output[1:], ['Installing', 'Connection refused'])

    def test_relaunch_preserves_inventory(self):
        from ai_sandbox_factory import sandbox_tui
        with patch.object(sandbox_tui.Cockpit,'run',return_value=updates.RELAUNCH), patch.object(sandbox_tui.os,'execv') as execute:
            sandbox_tui.launch('/tmp/factory-test-state')
        argv=execute.call_args.args[1]
        self.assertEqual(argv[1:3], ['-I', '-c'])
        self.assertIn("run_module('ai_sandbox_factory',", argv[3])
        self.assertEqual(argv[4:], ['--state-dir', '/tmp/factory-test-state', 'tui'])
