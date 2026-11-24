"""Browser defaults and explicit choices across supported coding agents."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_options import options


class BrowserDefaultsTests(unittest.TestCase):
    def test_default_enables_browser_and_preserves_explicit_saved_choices(self):
        self.assertTrue(options()['browser'])
        for enabled in (True, False):
            saved = {'browser': enabled, 'native_approval': 'manual'}
            self.assertEqual(options(saved)['browser'], enabled)
            self.assertEqual(saved, {'browser': enabled, 'native_approval': 'manual'})

    def test_cli_creation_defaults_and_opt_out_preserve_options_file_choices(self):
        profile = sandbox.load_profile('base')
        cases = [(None, [], True), (None, ['--no-browser'], False),
                 (False, [], False), (True, [], True),
                 (False, ['--browser'], True), (True, ['--no-browser'], False)]
        for agent in ('pi', 'codex', 'claude', 'agy'):
            for saved, flags, expected in cases:
                with self.subTest(agent=agent, saved=saved, flags=flags), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    workspace = root / 'workspace'
                    workspace.mkdir()
                    args = ['ai-sandbox-factory', '--state-dir', str(root / 'state'),
                            'create', 'fixture', '--workspace', str(workspace),
                            '--engine', 'docker', '--primary-agent', agent, *flags]
                    if saved is not None:
                        selected = root / 'options.json'
                        selected.write_text(json.dumps({'browser': saved}))
                        args.extend(['--options-file', str(selected)])
                    with patch('sys.argv', args), \
                            patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=root), \
                            patch.object(sandbox, 'choose_engine', return_value='docker'), \
                            patch.object(sandbox.host_platform, 'check_engine', return_value=None), \
                            patch.object(sandbox.os, 'getuid', return_value=1000), \
                            patch.object(sandbox.os, 'getgid', return_value=1000), \
                            patch.object(sandbox, 'snapshot_profile', return_value=profile), \
                            redirect_stdout(io.StringIO()):
                        sandbox.main()
                    config = json.loads((root / 'state/fixture/config.json').read_text())
                    self.assertEqual(config['options']['browser'], expected)
                    self.assertEqual(config['primary_agent'], agent)


if __name__ == '__main__':
    unittest.main()
