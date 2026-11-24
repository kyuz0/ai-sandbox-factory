import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_sandbox_factory import primary_agents, sandbox


class PrimaryAgentIsolation(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        (Path(workspace.name) / 'src').mkdir()
        self.config = {'name': 'test', 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                       'primary_agent': 'claude', 'workspace': workspace.name}

    def test_saved_codex_ca_and_relay_do_not_report_ready_before_proxy_listens(self):
        with TemporaryDirectory() as temp, patch.object(primary_agents, 'volume', return_value=True), \
                patch.object(sandbox, 'inspect', return_value=None), \
                patch.object(sandbox, 'image_names', return_value=('worker-image', 'proxy-image')), \
                patch.object(sandbox, 'run'), patch.object(primary_agents, '_wait') as wait, \
                socket.socket() as proxy, socket.socket(socket.AF_UNIX) as relay:
            root = Path(temp)
            primary_agents.up({**self.config, 'primary_agent': 'codex'}, root)
            check = wait.call_args.args[2]
            (root / 'ready').touch()
            (root / 'ca.pem').write_text('saved public certificate')
            relay.bind(str(root / 'relay.sock'))
            proxy.bind(('127.0.0.1', 0))
            for source, target in {
                '/tmp/factory-primary-ready': str(root / 'ready'),
                '/run/primary-egress/proxy.sock': str(root / 'relay.sock'),
                '/run/primary-editor/auth.sock': str(root / 'relay.sock'),
                '/run/primary-editor/codex-ca.pem': str(root / 'ca.pem'),
                '/127.0.0.1/3130': '/127.0.0.1/' + str(proxy.getsockname()[1]),
            }.items():
                check = check.replace(source, target)
            self.assertNotEqual(subprocess.run(['bash', '-c', check], capture_output=True).returncode, 0)
            proxy.listen()
            self.assertEqual(subprocess.run(['bash', '-c', check], capture_output=True).returncode, 0)

    def test_controller_and_workspace_share_only_tool_transport(self):
        with TemporaryDirectory() as temp, patch.object(primary_agents, 'volume', return_value=False), \
                patch.object(sandbox, 'inspect', return_value=None), \
                patch.object(sandbox, 'image_names', return_value=('worker-image', 'proxy-image')), \
                patch.object(sandbox, 'run') as run, patch.object(primary_agents, '_wait'):
            state = Path(temp)
            primary_agents.up(self.config, state)
            worker = primary_agents.mounts(self.config, state)
            args = run.call_args.args[0]
            controller, proxy, socket, home, egress = primary_agents.names(self.config)
            self.assertEqual(args[3:5], ['--name', controller])
            self.assertIn('--network=none', args)
            self.assertIn(f'{home}:/home/node', args)
            self.assertIn(f'{egress}:/run/primary-egress:ro', args)
            self.assertIn(f'{socket}:/run/primary-workspace:ro', args)
            self.assertIn(f'{socket}:/run/primary-workspace', worker)
            editor = primary_agents.editor_volume(self.config)
            self.assertIn(f'{editor}:/run/primary-editor', args)
            self.assertIn(f'{editor}:/run/primary-editor:ro', worker)
            self.assertFalse(any(home in mount or egress in mount for mount in worker))
            controller_mounts = [args[i + 1] for i, arg in enumerate(args) if arg == '-v']
            self.assertFalse(any('/workspace' in mount.split(':')[1:2] for mount in controller_mounts))
            self.assertEqual(json.loads((state / 'primary-runtime' / 'cwd.json').read_text()), '/workspace')
            self.assertEqual(primary_agents.resources(self.config), {
                'container': {controller, proxy}, 'volume': {socket, home, egress, primary_agents.editor_volume(self.config)},
            })

    def test_copied_sign_in_goes_only_to_private_controller_stdin(self):
        fake_credential = 'opaque-private-token'
        for provider in ('codex', 'claude', 'agy'):
            with self.subTest(provider=provider), patch.object(primary_agents, 'up'), \
                    patch.object(primary_agents, 'refresh_workspace_settings') as refresh, \
                    patch.object(primary_agents.host_platform, 'is_mac', return_value=False), \
                    patch.object(primary_agents.advisor_credentials, 'read_host', return_value=fake_credential), \
                    patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                config = {**self.config, 'primary_agent': provider}
                primary_agents.copy_host(config, Path('/private-state'))
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ['podman', 'exec', '-i', primary_agents.names(config)[0]])
                self.assertEqual(run.call_args.kwargs['input'], fake_credential)
                self.assertNotIn(fake_credential, ' '.join(command))
                refresh.assert_called_once_with(config, refresh_auth=True)

    def test_native_terminal_and_login_run_client_in_workspace(self):
        for provider, login in (('claude', ['auth', 'login']), ('agy', ['--factory-login'])):
            for action, suffix in (('open', []), ('login', login)):
                with self.subTest(provider=provider, action=action), patch('sys.stdin.isatty', return_value=True), \
                        patch.object(sandbox, 'run') as run:
                    config = {**self.config, 'primary_agent': provider, 'working_directory': 'src'}
                    primary_agents._terminal(config, action)
                    self.assertEqual(run.call_args.args[0], [
                        'podman', 'exec', '-it', '-w', '/workspace/src', sandbox.names(config)[0],
                        '/usr/local/bin/' + provider, *suffix])

    def test_native_editor_opens_before_account_sign_in(self):
        from ai_sandbox_factory import browser_editor
        for provider in ('codex', 'claude', 'agy'):
            with self.subTest(provider=provider), patch.object(sandbox, 'up') as up, \
                    patch.object(primary_agents, 'has_sign_in', side_effect=AssertionError('Native editor owns sign-in')), \
                    patch.object(browser_editor, 'open_browser_editor') as editor:
                config = {**self.config, 'primary_agent': provider}
                sandbox.open_selected(config, Path('/private-state'), mode='browser')
                up.assert_called_once_with(config, Path('/private-state'))
                editor.assert_called_once_with(config, Path('/private-state'), background=True)

    def test_claude_workspace_prepares_masked_auth_and_only_optional_browser_transport(self):
        for browser in (False, True):
            with self.subTest(browser=browser), patch.object(sandbox, 'run') as run, \
                    patch.object(primary_agents, '_wait') as wait, \
                    patch('ai_sandbox_factory.codex_history.migrate'):
                config = {**self.config, 'options': {'browser': browser}}
                primary_agents.worker_start(config, 'worker')
                self.assertIn(['podman', 'exec', 'worker', 'python3',
                               '/opt/primary/claude_client.py', '--factory-prepare'],
                              [call.args[0] for call in run.call_args_list])
                starts = [call.args[0] for call in run.call_args_list if '-d' in call.args[0]]
                self.assertEqual(bool(starts), browser)
                self.assertIn('/dev/tcp/127.0.0.1/3130', wait.call_args_list[0].args[2])

    def test_saving_native_approval_never_operates_on_containers(self):
        with TemporaryDirectory() as temp, patch.object(sandbox, 'run', side_effect=AssertionError('No runtime operation')):
            state = Path(temp)
            sandbox.apply_settings(self.config, state, {'native_approval': 'manual'})
            saved = json.loads((state / 'config.json').read_text())
            self.assertEqual(saved['options']['native_approval'], 'manual')
            self.assertEqual(saved['workspace'], self.config['workspace'])
            with self.assertRaises(ValueError):
                sandbox.apply_settings(saved, state, {'native_approval': 'invalid'})
            self.assertEqual(json.loads((state / 'config.json').read_text()), saved)

    def test_codex_web_search_defaults_and_validation(self):
        from ai_sandbox_factory.sandbox_options import options
        for key in ('codex_web_search', 'claude_web_search', 'agy_web_search'):
            self.assertIs(options()[key], False)
            for value in (False, True):
                self.assertIs(options({key: value})[key], value)
            for value in (0, 1, 'true', None):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key + ' must be a boolean'):
                    options({key: value})

    def test_native_web_search_saves_runtime_choice_without_recreation(self):
        for provider in ('claude', 'agy'):
            key = provider + '_web_search'
            for enabled in (False, True):
                with self.subTest(provider=provider, enabled=enabled), TemporaryDirectory() as temporary, \
                        patch.object(sandbox, 'run', side_effect=AssertionError('No container mutation')), \
                        patch.object(sandbox, 'inspect', return_value=None):
                    state = Path(temporary)
                    sandbox.apply_settings({**self.config, 'primary_agent': provider}, state, {key: enabled})
                    saved = json.loads((state / 'config.json').read_text())
                    self.assertIs(saved['options'][key], enabled)
                    runtime = json.loads((state / 'primary-runtime/settings.json').read_text())
                    self.assertIs(runtime[key], enabled)
                    guide = (state / 'primary-runtime/context.md').read_text()
                    expected = 'enabled' if enabled else 'turned off'
                    self.assertIn(f'Factory has {expected} built-in Web Search', guide)
                    for other in ('codex', 'claude', 'agy'):
                        if other != provider:
                            self.assertNotIn(other + '_web_search', runtime)

    def test_running_claude_preferences_refresh_without_restart_and_failure_keeps_saved_choice(self):
        with patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            primary_agents.refresh_workspace_settings(self.config)
            self.assertEqual(run.call_args.args[0], ['podman', 'exec', sandbox.names(self.config)[0],
                                                    'python3', '/opt/primary/claude_client.py', '--factory-prepare'])
        with patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 7, '', 'Permission denied: /home/node/.claude/settings.json')):
            with self.assertRaisesRegex(ValueError, r'Cannot update Claude Code.*exit 7.*Permission denied.*settings.json'):
                primary_agents.refresh_workspace_settings(self.config)
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            original = {**self.config, 'options': {'claude_web_search': False}}
            (state / 'config.json').write_text(json.dumps(original))
            with patch.object(primary_agents, 'refresh_workspace_settings', side_effect=ValueError('Native settings file is read-only')):
                with self.assertRaisesRegex(ValueError, 'read-only'):
                    sandbox.apply_settings(original, state, {'claude_web_search': True})
            self.assertEqual(json.loads((state / 'config.json').read_text()), original)
            self.assertIs(json.loads((state / 'primary-runtime/settings.json').read_text())['claude_web_search'], False)
            self.assertIn('turned off built-in Web Search', (state / 'primary-runtime/context.md').read_text())

    def test_codex_web_search_is_saved_and_passed_only_to_codex_runtime(self):
        with TemporaryDirectory() as temporary, patch.object(sandbox, 'run', side_effect=AssertionError('No runtime operation')):
            state = Path(temporary)
            config = {**self.config, 'primary_agent': 'codex'}
            sandbox.apply_settings(config, state, {'codex_web_search': False})
            saved = json.loads((state / 'config.json').read_text())
            self.assertIs(saved['options']['codex_web_search'], False)
            runtime = json.loads((state / 'primary-runtime/settings.json').read_text())
            self.assertIs(runtime['codex_web_search'], False)
            context = state / 'primary-runtime/context.md'
            self.assertIn('turned off built-in Web Search', context.read_text())
            self.assertEqual(saved['workspace'], config['workspace'])
            with self.assertRaisesRegex(ValueError, 'must be a boolean'):
                sandbox.apply_settings(saved, state, {'codex_web_search': 'invalid'})
            self.assertEqual(json.loads((state / 'config.json').read_text()), saved)
            sandbox.apply_settings(saved, state, {'codex_web_search': True})
            self.assertIn('enabled built-in Web Search', context.read_text())
        for agent in ('pi', 'claude', 'agy'):
            with self.subTest(agent=agent), TemporaryDirectory() as temporary:
                state = Path(temporary)
                primary_agents.prepare_workspace({**self.config, 'primary_agent': agent,
                                                  'options': {'codex_web_search': False}}, state)
                runtime = state / 'primary-runtime/settings.json'
                if agent in ('claude', 'agy'):
                    self.assertNotIn('codex_web_search', json.loads(runtime.read_text()))
                else:
                    self.assertFalse(runtime.exists())
                with self.assertRaisesRegex(ValueError, 'only for Codex'):
                    sandbox.apply_settings({**self.config, 'primary_agent': agent}, state, {'codex_web_search': False})

    def test_native_context_and_settings_keep_workspace_grants(self):
        from ai_sandbox_factory import connector_settings
        for provider in ('codex', 'claude', 'agy'):
            with self.subTest(provider=provider), TemporaryDirectory() as temp:
                state = Path(temp)
                host = {'alias': 'build', 'hostname': 'build.example', 'user': 'tester', 'port': 22}
                config = {**self.config, 'primary_agent': provider, 'ssh': {'hosts': [host]},
                          'working_directory': 'src', 'options': {'native_approval': 'manual', 'browser': False}}
                primary_agents.prepare_workspace(config, state)
                primary_agents.write_context(config, state)
                runtime = state / 'primary-runtime'
                settings = json.loads((runtime / 'settings.json').read_text())
                self.assertEqual(settings['native_approval'], 'manual')
                self.assertEqual(settings['working_directory'], '/workspace/src')
                self.assertFalse(settings['browser'])
                instructions = (runtime / 'context.md').read_text()
                self.assertIn('native tools', instructions)
                self.assertIn('Actions → Install system packages…', instructions)
                self.assertNotIn('workspace_shell', instructions)
                connector_settings.refresh_agent_context(config, state)
                self.assertIn('build.example', (runtime / 'context.md').read_text())
                connector_settings.refresh_agent_context({**config, 'ssh': {'hosts': []}}, state)
                self.assertNotIn('build.example', (runtime / 'context.md').read_text())

    def test_browser_mcp_does_not_remap_native_file_and_shell_tools(self):
        for provider in ('codex', 'claude', 'agy'):
            with self.subTest(provider=provider):
                messages = '\n'.join(json.dumps(message) for message in (
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                     'params': {'name': 'workspace_shell', 'arguments': {'command': 'echo unexpected'}}},
                )) + '\n'
                result = subprocess.run(['node', str(primary_agents.SOURCE / 'worker-tools.mjs')],
                    input=messages, text=True, capture_output=True, check=True,
                    env={**os.environ, 'FACTORY_PRIMARY_AGENT': provider})
                responses = {item['id']: item for item in map(json.loads, result.stdout.splitlines())}
                self.assertNotIn('workspace_shell', [tool['name'] for tool in responses[1]['result']['tools']])
                self.assertTrue(responses[2]['result']['isError'])
                self.assertIn('Unknown workspace tool', str(responses[2]))

    def test_executable_guidance_matches_active_choices(self):
        with TemporaryDirectory() as temp:
            config = {**self.config, 'working_directory': 'src', 'options': {'browser': False},
                      'extra_folders': [{'source': '/tmp/documents', 'name': 'docs', 'read_only': True}]}
            text = primary_agents.guidance(config, Path(temp))
            self.assertIn('/workspace/src', text)
            self.assertIn('/mnt/docs" (read-only)', text)
            self.assertIn('native tools', text)
            self.assertNotIn('workspace_shell', text)
            self.assertIn('configured allowlist', text)
            self.assertIn('allow the specific hostname and port', text)
            self.assertIn('without sudo', text)
            self.assertIn('wait for confirmation', text)
            self.assertIn('Settings → Browser', text)
            self.assertNotIn('browser tool', text)
            self.assertNotIn('AWS CLI', text)
            self.assertNotIn('bash_escalate', text)
            config.update(network='unrestricted', options={'browser': True}, aws_access={'role': 'sample', 'region': 'eu-west-1',
                          'grants': [{'Action': 'ec2:DescribeRegions', 'Resource': '*'}]})
            enabled = primary_agents.guidance(config, Path(temp))
            self.assertIn('browser tool', enabled)
            self.assertIn('AWS CLI', enabled)
            self.assertIn('Network is Unrestricted', enabled)
            self.assertNotIn('allow the specific hostname and port', enabled)


if __name__ == '__main__':
    unittest.main()
