"""Native sign-in transport must not become the project shell's proxy."""
import ast
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers'
PROXIES = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')


class WorkspaceNetworkEnvironmentTests(unittest.TestCase):
    def test_guidance_distinguishes_container_access_from_native_sign_in(self):
        from ai_sandbox_factory import sandbox_options, web_access
        for enabled in (False, True):
            selected = sandbox_options.options({'web_access': {'enabled': enabled}})
            unrestricted = sandbox_options.guidance(selected, network='unrestricted')
            restricted = sandbox_options.guidance(selected, network='allowlist')
            self.assertIn('Use ordinary network connections', unrestricted)
            self.assertNotIn('requires that destination to be allowed', unrestricted)
            self.assertNotIn('If a page is blocked', unrestricted)
            self.assertIn('configured allowlist', restricted)
            if enabled:
                self.assertIn('requires that destination to be allowed', restricted)
                self.assertIn('without adding a Restricted gateway proxy', unrestricted)

    def test_project_bash_uses_current_mode_without_changing_parent_transport(self):
        for mode in ('allowlist', 'unrestricted'):
            for flags in (['-c'], ['--noprofile', '--norc', '-c']):
                with self.subTest(mode=mode, flags=flags):
                    env = {**os.environ, 'BASH_ENV': str(ROOT / 'primary-worker/shell-network.sh'),
                           'SANDBOX_NETWORK_MODE': mode, **dict.fromkeys(PROXIES, 'http://127.0.0.1:3130')}
                    result = subprocess.check_output(['bash', *flags,
                        'python3 -c "import os,json;print(json.dumps(dict(os.environ)))"'], env=env, text=True)
                    actual = json.loads(result)
                    for key in PROXIES:
                        self.assertEqual(actual.get(key), 'http://127.0.0.1:3128' if mode == 'allowlist' else None)
                        self.assertEqual(env[key], 'http://127.0.0.1:3130')

    def test_explicit_project_proxy_is_preserved(self):
        env = {**os.environ, 'BASH_ENV': str(ROOT / 'primary-worker/shell-network.sh'),
               'SANDBOX_NETWORK_MODE': 'unrestricted', **dict.fromkeys(PROXIES, 'http://project-proxy:8080')}
        result = json.loads(subprocess.check_output(['bash', '-c',
            'python3 -c "import os,json;print(json.dumps(dict(os.environ)))"'], env=env, text=True))
        for key in PROXIES:
            self.assertEqual(result[key], env[key])

    def test_editor_terminals_follow_mode_while_preserving_custom_proxy(self):
        source = ROOT / 'editor/launch.py'
        tree = ast.parse(source.read_text())
        statement = next(node for node in tree.body if isinstance(node, ast.If)
            and "terminal.integrated.env.linux" in ast.unparse(node))
        code = compile(ast.Module(body=[statement], type_ignores=[]), str(source), 'exec')
        for agent in ('pi', 'codex', 'claude', 'agy'):
            for mode in ('allowlist', 'unrestricted'):
                existing = {'terminal.integrated.env.linux': {'HTTPS_PROXY': 'http://custom:8080',
                    'http_proxy': 'http://127.0.0.1:3130', 'UNRELATED': 'kept'}}
                with patch.dict(os.environ, {'SANDBOX_NETWORK_MODE': mode}):
                    exec(code, {'existing': existing, 'settings': {'agent': agent}, 'os': os})
                env = existing['terminal.integrated.env.linux']
                self.assertEqual(env['UNRELATED'], 'kept')
                self.assertEqual(env['HTTPS_PROXY'], 'http://custom:8080')
                expected = ('http://127.0.0.1:3130' if agent == 'pi' else
                            'http://127.0.0.1:3128' if mode == 'allowlist' else None)
                self.assertEqual(env['http_proxy'], expected)


if __name__ == '__main__':
    unittest.main()
