import json
import os
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pi_sandbox_factory import primary_agents, sandbox


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
                    patch.object(primary_agents.host_platform, 'is_mac', return_value=False), \
                    patch.object(primary_agents.advisor_credentials, 'read_host', return_value=fake_credential), \
                    patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                config = {**self.config, 'primary_agent': provider}
                primary_agents.copy_host(config, Path('/private-state'))
                command = run.call_args.args[0]
                self.assertEqual(command[:4], ['podman', 'exec', '-i', primary_agents.names(config)[0]])
                self.assertEqual(run.call_args.kwargs['input'], fake_credential)
                self.assertNotIn(fake_credential, ' '.join(command))

    def test_native_codex_login_initializes_a_fresh_private_home(self):
        launcher = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(launcher['command'].__globals__, {'HOME': home}):
                args = launcher['command']('codex', 'login', {'browser': False}, '')
            self.assertEqual(args[-2:], ['login', '--device-auth'])
            self.assertTrue((home / '.codex').is_dir())

    def test_each_native_launcher_receives_only_current_connector_grants(self):
        from pi_sandbox_factory import connector_settings
        launcher = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))
        command = launcher['command']
        with TemporaryDirectory() as temp:
            state = Path(temp)
            (state / 'primary-agent').mkdir()
            prompt = state / 'primary-agent/context.md'
            prompt.touch()
            host = {'alias': 'build', 'hostname': 'build.example', 'user': 'tester', 'port': 22}
            self.addCleanup(os.chdir, Path.cwd())
            for provider in ('codex', 'claude', 'agy'):
                with self.subTest(provider=provider):
                    config = {**self.config, 'primary_agent': provider, 'ssh': {'hosts': [host]}}
                    connector_settings.refresh_agent_context(config, state)
                    settings = {'browser': False, 'working_directory': str(state / 'shadow')}
                    with patch.dict(command.__globals__, {'HOME': state, 'WORKSPACE': state / 'shadow'}), patch('subprocess.run'):
                        args = command(provider, 'chat', settings, prompt.read_text())
                    if provider == 'agy':
                        instructions = (state / '.gemini/AGENTS.md').read_text()
                    elif provider == 'claude':
                        instructions = args[args.index('--append-system-prompt') + 1]
                    else:
                        instructions = json.loads(next(arg.split('=', 1)[1] for arg in args
                                                       if arg.startswith('developer_instructions=')))
                    self.assertIn('build.example', instructions)
                    self.assertIn('/run/primary-runtime/connectors.md', instructions)
                    self.assertIn('ssh-add -l', instructions)
                    connector_settings.refresh_agent_context({**config, 'ssh': {'hosts': []}}, state)
                    self.assertNotIn('build.example', prompt.read_text())
                    self.assertFalse((state / 'primary-runtime/connectors.md').exists())

    def test_native_review_defaults_apply_to_terminal_and_editor(self):
        command = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))['command']
        self.addCleanup(os.chdir, Path.cwd())
        with TemporaryDirectory() as temp, patch.dict(command.__globals__, {'HOME': Path(temp), 'WORKSPACE': Path(temp) / 'workspace'}):
            settings = {'browser': False, 'working_directory': str(Path(temp) / 'workspace')}
            for action in ('open', 'editor'):
                with self.subTest(action=action):
                    codex = command('codex', action, settings, '')
                    self.assertIn('approvals_reviewer="auto_review"', codex)
                    self.assertIn('approval_policy="on-request"', codex)
                    self.assertIn('sandbox_mode="workspace-write"', codex)
                    self.assertNotIn('never', codex)
                    claude = command('claude', action, settings, '')
                    self.assertEqual(claude[claude.index('--permission-mode') + 1], 'auto')
                    self.assertNotIn('--allowedTools', claude)
                    self.assertNotIn('--dangerously-skip-permissions', claude)

    def test_workspace_trust_uses_selected_folder_without_disabling_approvals(self):
        command = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))['command']
        self.addCleanup(os.chdir, Path.cwd())
        with TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.dict(command.__globals__, {'HOME': Path(temp), 'WORKSPACE': Path(temp) / 'workspace'}), patch('subprocess.run'):
            cwd = Path(temp) / 'workspace' / 'selected folder 🌱'
            settings = {'browser': False, 'working_directory': str(cwd)}
            for action in ('open', 'editor'):
                for provider in ('codex', 'claude', 'agy'):
                    with self.subTest(provider=provider, action=action):
                        os.chdir(temp)
                        args = command(provider, action, settings, '')
                        self.assertEqual(Path.cwd(), cwd)
                        self.assertEqual(list(cwd.iterdir()), [])
                        if provider == 'codex':
                            table = next(value for value in args if value.startswith('projects='))
                            self.assertEqual(table, 'projects={' + json.dumps(str(cwd), ensure_ascii=False) +
                                             '={trust_level="trusted"}}')
                            self.assertIn('approval_policy="on-request"', args)
                        elif provider == 'claude':
                            self.assertEqual(os.environ['CLAUDE_CODE_SANDBOXED'], '1')
                            self.assertIn('--restricted', args)
                            self.assertEqual(args[args.index('--tools') + 1], '')
                            self.assertEqual(args[args.index('--permission-mode') + 1], 'auto')
                        else:
                            configured = json.loads(args[args.index('--settings') + 1])
                            self.assertEqual(configured['trustedWorkspaces'], [str(cwd)])
                            self.assertIn('command(*)', configured['permissions']['deny'])
                        self.assertNotIn('--dangerously-skip-permissions', args)

    def test_workspace_trust_preserves_native_state_and_rejects_path_escape(self):
        prepare = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))['workspace_directory']
        self.addCleanup(os.chdir, Path.cwd())
        with TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            root.mkdir()
            with patch.dict(prepare.__globals__, {'WORKSPACE': root}):
                settings = {'working_directory': str(root / 'src')}
                prepare(settings)
                prepare({'working_directory': str(root)})  # Existing empty child folders are allowed.
                marker = root / 'client-state.json'
                marker.write_text('native client state')
                prepare(settings)
                self.assertEqual(marker.read_text(), 'native client state')
                marker.unlink()
                link = root / 'outside'
                link.symlink_to(temp, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'symbolic link'):
                    prepare({'working_directory': str(link)})
                link.unlink()
                for path in (str(Path(temp)), str(root / '..' / 'outside')):
                    with self.assertRaisesRegex(ValueError, 'configured workspace'):
                        prepare({'working_directory': path})

    def test_login_does_not_grant_workspace_trust(self):
        command = runpy.run_path(str(primary_agents.SOURCE / 'launch.py'))['command']
        with TemporaryDirectory() as temp, patch.dict(os.environ), patch('subprocess.run'), \
                patch.dict(command.__globals__, {'HOME': Path(temp)}):
            os.environ.pop('CLAUDE_CODE_SANDBOXED', None)
            for provider in ('codex', 'claude', 'agy'):
                args = command(provider, 'login', {'browser': False}, '')
                self.assertNotIn('CLAUDE_CODE_SANDBOXED', os.environ)
                self.assertNotIn('trustedWorkspaces', ' '.join(args))
                self.assertNotIn('trust_level', ' '.join(args))

    def test_executable_guidance_matches_active_choices(self):
        with TemporaryDirectory() as temp:
            config = {**self.config, 'working_directory': 'src',
                      'extra_folders': [{'source': '/tmp/documents', 'name': 'docs', 'read_only': True}]}
            text = primary_agents.guidance(config, Path(temp))
            self.assertIn('/workspace/src', text)
            self.assertIn('/mnt/docs" (read-only)', text)
            self.assertIn('workspace_shell', text)
            self.assertIn('configured allowlist', text)
            self.assertNotIn('browser tool', text)
            self.assertNotIn('AWS CLI', text)
            self.assertNotIn('bash_escalate', text)
            config.update(options={'browser': True}, aws_access={'role': 'sample', 'region': 'eu-west-1',
                          'grants': [{'Action': 'ec2:DescribeRegions', 'Resource': '*'}]})
            enabled = primary_agents.guidance(config, Path(temp))
            self.assertIn('browser tool', enabled)
            self.assertIn('AWS CLI', enabled)


if __name__ == '__main__':
    unittest.main()
