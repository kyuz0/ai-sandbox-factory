"""Native Codex choices must survive launches and Factory configuration changes."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import tomlkit
import subprocess
import queue
import threading
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'))
from codex_client import configure, main
from unittest.mock import patch


class ClientSettings(unittest.TestCase):
    def test_full_profile_reaches_the_cli_override_with_valid_toml(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'context.md'
            for context in ('FIRST_PROFILE_REVISION\nEmoji: 😀\nControl: \x7f\n',
                            'SECOND_PROFILE_REVISION\nUse the database.', ''):
                path.write_text(context)
                with patch('codex_client.prepare'), patch('codex_client.Path', return_value=path), \
                        patch('codex_client.os.umask'), patch('codex_client.os.execv') as execute, \
                        patch.object(sys, 'argv', ['codex', '--version']):
                    main()
                argv = execute.call_args.args[1]
                self.assertEqual(argv[:2], ['codex', '-c'])
                self.assertEqual(argv[-1], '--version')
                self.assertEqual(tomlkit.parse(argv[2])['developer_instructions'], context)

    def test_pinned_codex_parses_full_instructions_above_trusted_project_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, project = root / 'home', root / 'project'
            home.mkdir(); project.mkdir(); (project / '.codex').mkdir()
            (project / '.codex/config.toml').write_text('developer_instructions="PROJECT_OVERRIDE"\n')
            (home / 'config.toml').write_text('[projects.' + json.dumps(str(project)) + ']\ntrust_level="trusted"\n')
            profile = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/profiles/developer-postgres/profile.md'
            context = profile.read_text(encoding='utf-8') + '\nEmoji: 😀\nControl: \x7f\n'
            path = root / 'context.md'
            path.write_text(context)
            with patch('codex_client.prepare'), patch('codex_client.Path', return_value=path), \
                    patch('codex_client.os.umask'), patch('codex_client.os.execv') as execute, \
                    patch.object(sys, 'argv', ['codex', 'app-server']):
                main()
            args = execute.call_args.args[1][1:]
            process = subprocess.Popen(['/usr/local/bin/factory-agent', *args], cwd=project,
                env={**os.environ, 'CODEX_HOME':str(home)}, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            responses = queue.Queue()
            def read_responses():
                for line in process.stdout:
                    responses.put(line)
                responses.put(None)
            reader = threading.Thread(target=read_responses, daemon=True)
            reader.start()
            try:
                for identifier, method, params in (
                    (1, 'initialize', {'clientInfo':{'name':'fixture','version':'1'},
                                       'capabilities':{'experimentalApi':True}}),
                    (2, 'config/read', {'includeLayers':True, 'cwd':str(project)}),
                ):
                    process.stdin.write(json.dumps({'id':identifier,'method':method,'params':params}) + '\n')
                    process.stdin.flush()
                    while True:
                        try:
                            line = responses.get(timeout=60)
                        except queue.Empty:
                            self.fail(f'Codex did not respond to {method}; process status {process.poll()}')
                        self.assertTrue(line, f'Codex exited before {method}: {process.poll()}')
                        response = json.loads(line)
                        if response.get('id') == identifier:
                            break
                    self.assertNotIn('error', response)
                self.assertEqual(response['result']['config']['developer_instructions'], context)
                self.assertEqual((project / '.codex/config.toml').read_text(),
                                 'developer_instructions="PROJECT_OVERRIDE"\n')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                reader.join(timeout=15)
                self.assertFalse(reader.is_alive(), 'Codex response reader did not stop')
                process.stdin.close(); process.stdout.close()

    def test_real_os_argument_limit_has_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'context.md'
            # Exceed the actual total argv limit, including on large-page kernels.
            path.write_text('x' * (os.sysconf('SC_ARG_MAX') + 1))
            driver = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                      "import codex_client; source = Path(sys.argv[2]); "
                      "codex_client.prepare = lambda: None; codex_client.Path = lambda value: source; "
                      "sys.argv=['codex', '--version']; codex_client.main()")
            result = subprocess.run([sys.executable, '-c', driver,
                str(Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'), str(path)],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            for text in ('Cannot start Codex', 'operating system', 'bytes', 'context.md', 'Shorten the profile'):
                self.assertIn(text, result.stderr)
            self.assertNotIn('Traceback', result.stderr)

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
