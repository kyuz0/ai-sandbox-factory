import json
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pi_sandbox_factory import primary_agents, sandbox


class PrimaryAgentIsolation(unittest.TestCase):
    def setUp(self):
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
            self.assertFalse(any(home in mount or egress in mount for mount in worker))
            controller_mounts = [args[i + 1] for i, arg in enumerate(args) if arg == '-v']
            self.assertFalse(any('/workspace' in mount.split(':')[1:2] for mount in controller_mounts))
            self.assertEqual(json.loads((state / 'primary-runtime' / 'cwd.json').read_text()), '/workspace')
            self.assertEqual(primary_agents.resources(self.config), {
                'container': {controller, proxy}, 'volume': {socket, home, egress},
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
            config.update(options={'browser': True}, aws_access={'role': 'sample'})
            enabled = primary_agents.guidance(config, Path(temp))
            self.assertIn('browser tool', enabled)
            self.assertIn('AWS CLI', enabled)


if __name__ == '__main__':
    unittest.main()
