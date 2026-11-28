import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_sandbox_factory import sandbox, system_packages


class SystemPackagesTests(unittest.TestCase):
    config = {'name': 'example', 'engine': 'podman', 'network': 'allowlist'}
    container = {'Id': 'fixture-id', 'State': {'Running': True}, 'Config': {
        'Env': ['LD_AUDIT=/workspace/fixture.so', 'APT_CONFIG=/workspace/apt.conf', 'SECRET=fixture-secret'],
    }}

    def test_accepts_names_and_rejects_apt_flags_and_other_expressions(self):
        self.assertEqual(system_packages.package_names(['jq', 'g++', 'jq', 'python3.13']), ['g++', 'jq', 'python3.13'])
        for value in ('--allow-unauthenticated', 'curl-', 'curl=2', 'curl:amd64', '/tmp/pkg.deb', 'https://example.invalid', 'jq;id'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Debian package names') as raised:
                system_packages.package_names([value])
            self.assertIn(repr(value),str(raised.exception))

    def test_host_exec_passes_only_clean_environment_and_keeps_existing_egress(self):
        command = system_packages.command(self.config, self.container, ['jq'])
        self.assertEqual(command[:6], ['podman', 'exec', '--user', '0:0', '--workdir', '/'])
        for entry in ('LD_AUDIT=', 'APT_CONFIG=', 'SECRET=', 'http_proxy=http://127.0.0.1:3128'):
            self.assertIn(entry, command)
        self.assertNotIn('fixture-secret', ' '.join(command))
        self.assertNotIn('--privileged', command)
        self.assertEqual(command[-2:], ['factory-apt', 'jq'])
        self.assertIn('CapBnd:', system_packages.INSTALL)
        self.assertIn('NoNewPrivs:', system_packages.INSTALL)
        self.assertIn('--no-remove', system_packages.INSTALL)
        unrestricted = system_packages.command({**self.config, 'network': 'unrestricted'}, self.container, ['jq'])
        self.assertFalse(any('http_proxy=' in argument for argument in unrestricted))

    def test_stopped_or_cancelled_job_does_not_start_or_touch_the_container(self):
        with patch.object(sandbox, 'run') as run, patch.object(sandbox, 'inspect', return_value=None) as inspect:
            with self.assertRaisesRegex(ValueError, 'Step 1 of 2.*Start the sandbox'):
                system_packages.install(self.config, ['jq'], None)
            with TemporaryDirectory() as directory:
                inspect.return_value = self.container
                system_packages.install(self.config, ['jq'], Path(directory))
            run.assert_not_called()

    def test_package_error_exposes_phase_exit_status_and_repo_settings(self):
        with patch.object(sandbox, 'inspect', return_value=self.container), \
                patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 100)) as run:
            with self.assertRaisesRegex(ValueError, 'Step 2 of 2.*could not install jq.*100.*Network and Web Search'):
                system_packages.install(self.config, ['jq'], None)
            run.assert_called_once_with(system_packages.command(self.config, self.container, ['jq']), check=False)
