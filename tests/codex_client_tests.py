"""Native Codex choices must survive launches and Factory configuration changes."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'))
from codex_client import configure


class ClientSettings(unittest.TestCase):
    def test_fresh_default_matches_native_workspace_write_preset(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            configure(home, dict(working_directory='/workspace', native_approval='automatic',
                browser=False, write_roots=['/workspace']))
            config = tomlkit.parse((home / 'config.toml').read_text())
            self.assertEqual(config['approval_policy'], 'on-request')
            self.assertEqual(config['sandbox_mode'], 'workspace-write')
            self.assertEqual(config['approvals_reviewer'], 'auto_review')
            self.assertEqual(config['web_search'], 'disabled')
            policy = config['sandbox_workspace_write']
            self.assertEqual(policy['writable_roots'], [])
            for option in ('network_access', 'exclude_slash_tmp', 'exclude_tmpdir_env_var'):
                self.assertFalse(policy.get(option, False))

    def test_factory_defaults_preserve_native_model_reasoning_and_user_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            settings = dict(working_directory='/workspace', native_approval='automatic',
                            browser=True, write_roots=['/workspace', '/mnt/code'])
            configure(home, settings)
            path = home / 'config.toml'
            config = tomlkit.parse(path.read_text())
            self.assertEqual(config['approvals_reviewer'], 'auto_review')
            self.assertEqual(config['projects']['/workspace']['trust_level'], 'trusted')
            self.assertEqual(config['sandbox_workspace_write']['writable_roots'], ['/mnt/code'])
            self.assertIn('factory_workspace', config['mcp_servers'])
            config.update(model='native-selected-model', model_reasoning_effort='high', approvals_reviewer='user')
            config['mcp_servers']['user-added'] = {'url': 'https://example.invalid/mcp'}
            path.write_text(tomlkit.dumps(config))
            original = path.read_bytes()
            configure(home, settings)
            self.assertEqual(path.read_bytes(), original)
            configure(home, settings | {'browser': False, 'native_approval': 'manual', 'write_roots': ['/workspace']})
            changed = tomlkit.parse(path.read_text())
            self.assertEqual(changed['model'], 'native-selected-model')
            self.assertEqual(changed['model_reasoning_effort'], 'high')
            self.assertEqual(changed['approvals_reviewer'], 'user')
            self.assertNotIn('factory_workspace', changed['mcp_servers'])
            self.assertIn('user-added', changed['mcp_servers'])
            self.assertEqual(changed['sandbox_workspace_write']['writable_roots'], [])

    def test_workspace_parent_remains_writable_with_a_nested_starting_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            configure(home, dict(working_directory='/workspace/project',
                native_approval='automatic', browser=False, write_roots=['/workspace', '/mnt/code']))
            config = tomlkit.parse((home / 'config.toml').read_text())
            self.assertEqual(config['sandbox_workspace_write']['writable_roots'], ['/workspace', '/mnt/code'])

    def test_changing_only_the_starting_directory_updates_writable_roots(self):
        from ai_sandbox_factory import primary_agents

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, workspace = root / 'codex', root / 'workspace'
            home.mkdir()
            (workspace / 'project').mkdir(parents=True)
            config = dict(primary_agent='codex', workspace=str(workspace), extra_folders=[
                dict(name='code', read_only=False), dict(name='reference', read_only=True),
            ])
            path = home / 'config.toml'
            for starting_folder, roots in (
                ('.', ['/mnt/code']),
                ('project', ['/workspace', '/mnt/code']),
                ('.', ['/mnt/code']),
            ):
                primary_agents.prepare_workspace(config | {'working_directory': starting_folder}, root)
                settings = json.loads((root / 'primary-runtime/settings.json').read_text())
                configure(home, settings)
                saved = tomlkit.parse(path.read_text())
                self.assertEqual(saved['sandbox_workspace_write']['writable_roots'], roots)
                self.assertEqual(saved['projects'][settings['working_directory']]['trust_level'], 'trusted')

    def test_web_search_toggle_applies_only_when_factory_choice_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            settings = dict(working_directory='/workspace', native_approval='automatic',
                browser=False, write_roots=['/workspace'], codex_web_search=True)
            configure(home, settings)
            path = home / 'config.toml'
            config = tomlkit.parse(path.read_text())
            self.assertEqual(config['web_search'], 'cached')
            config['web_search'] = 'live'
            path.write_text(tomlkit.dumps(config))
            configure(home, settings)
            self.assertEqual(tomlkit.parse(path.read_text())['web_search'], 'live')
            configure(home, settings | {'browser': True})
            self.assertEqual(tomlkit.parse(path.read_text())['web_search'], 'live')
            configure(home, settings | {'codex_web_search': False})
            self.assertEqual(tomlkit.parse(path.read_text())['web_search'], 'disabled')
            configure(home, settings)
            self.assertEqual(tomlkit.parse(path.read_text())['web_search'], 'cached')

    def test_unchecked_web_search_is_disabled_on_first_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            configure(home, dict(working_directory='/workspace', codex_web_search=False))
            self.assertEqual(tomlkit.parse((home / 'config.toml').read_text())['web_search'], 'disabled')

    def test_factory_first_prepare_disables_web_search_by_default(self):
        from ai_sandbox_factory import primary_agents
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            primary_agents.prepare_workspace({'primary_agent': 'codex', 'workspace': temporary}, state)
            settings = json.loads((state / 'primary-runtime/settings.json').read_text())
            self.assertIs(settings['codex_web_search'], False)
            home = state / 'codex-home'
            home.mkdir()
            configure(home, settings)
            self.assertEqual(tomlkit.parse((home / 'config.toml').read_text())['web_search'], 'disabled')

    def test_absent_saved_toggle_does_not_reset_native_choice_on_later_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            settings = dict(working_directory='/workspace', browser=False)
            configure(home, settings)
            path = home / 'config.toml'
            config = tomlkit.parse(path.read_text())
            config['web_search'] = 'live'
            path.write_text(tomlkit.dumps(config))
            configure(home, settings | {'browser': True, 'codex_web_search': False})
            self.assertEqual(tomlkit.parse(path.read_text())['web_search'], 'live')


if __name__ == '__main__':
    unittest.main()
