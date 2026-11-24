"""Saved profile instructions reach Pi, Claude Code and Antigravity."""
from contextlib import ExitStack
import importlib.util
import io
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import primary_agents, sandbox

SOURCE = sandbox.ROOT / 'containers/primary'


def client(name):
    spec = importlib.util.spec_from_file_location('profile_test_' + name, SOURCE / (name + '_client.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileInstructions(unittest.TestCase):
    def test_pi_prefers_saved_instructions_and_reports_a_damaged_saved_profile(self):
        # bootstrap.py initializes the whole container on import. Load only its
        # file reader so the test cannot write host Pi configuration.
        source = sandbox.ROOT / 'containers/dev/bootstrap.py'
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'profile_instructions')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, image = root / 'saved', root / 'image.md'
            image.write_text('IMAGE_FALLBACK')
            namespace = {'Path':lambda path: saved if path.endswith('/profile') else image}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
            read = namespace['profile_instructions']
            self.assertEqual(read(), 'IMAGE_FALLBACK')
            saved.mkdir()
            profile = saved / 'profile.md'
            profile.write_text('FULL_SAVED_PROFILE 😀\n')
            self.assertEqual(read(), 'FULL_SAVED_PROFILE 😀')
            for damaged in ('missing', 'invalid UTF-8'):
                if damaged == 'missing':
                    profile.unlink()
                else:
                    profile.write_bytes(b'\xff')
                with self.subTest(damaged=damaged), self.assertRaisesRegex(SystemExit,
                        'Cannot read profile instructions.*profile.md.*Restore the saved profile'):
                    read()

    def test_saved_profile_changes_replace_context_in_each_client_without_changing_user_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'profile'
            profile.mkdir()
            project = root / 'project'
            project.mkdir()
            for name in ('AGENTS.md', 'CLAUDE.md'):
                (project / name).write_text('USER_PROJECT_RULES\n')
            for agent in ('claude', 'agy'):
                with self.subTest(agent=agent), patch.dict(os.environ):
                    module = client(agent)
                    state, home = root / agent, root / (agent + '-home')
                    home.mkdir()
                    rules = home / ('CLAUDE.md' if agent == 'claude' else 'AGENTS.md')
                    rules.write_text('USER_GLOBAL_RULES\n')
                    config = {'name':'fixture', 'primary_agent':agent, 'workspace':str(project),
                              'profile':{'name':'fixture', 'path':str(profile)}}
                    original_path = module.Path
                    # The production entry points read their normal absolute runtime
                    # paths; only their filesystem and auth response are synthetic.
                    files = {
                        '/run/primary-runtime/context.md': state / 'primary-runtime/context.md',
                        '/run/primary-runtime/settings.json': root / 'settings.json',
                        '/run/primary-editor/claude-ca.pem': root / 'public-ca.pem',
                        '/etc/ssl/certs/ca-certificates.crt': root / 'system-ca.pem',
                    }
                    for name in ('public-ca.pem', 'system-ca.pem'):
                        (root / name).write_bytes(b'SYNTHETIC_CERTIFICATE\n')
                    (root / 'settings.json').write_text(json.dumps({'working_directory':'/workspace',
                        'browser':False, 'native_approval':'manual'}))
                    def routed(value):
                        return files.get(str(value), original_path(value))
                    for revision in ('FIRST_PROFILE_REVISION', 'SECOND_PROFILE_REVISION', ''):
                        text = ('# Profile instructions\n' + revision + '\nUse the project database.\n') if revision else ''
                        (profile / 'profile.md').write_text(text)
                        primary_agents.write_context(config, state)
                        expected = (state / 'primary-runtime/context.md').read_text()
                        with ExitStack() as patches:
                            patches.enter_context(patch.object(module, 'Path', side_effect=routed))
                            patches.enter_context(patch.object(sys, 'argv', [agent, '--version']))
                            patches.enter_context(patch.object(module.os, 'umask'))
                            execute = patches.enter_context(patch.object(module.os, 'execv'))
                            if agent == 'claude':
                                patches.enter_context(patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(home)))
                                opener = patches.enter_context(patch.object(module, 'build_opener'))
                                opener.return_value.open.side_effect = lambda *a: io.BytesIO(b'{"auth":null,"generation":"fixture"}')
                            else:
                                patches.enter_context(patch.object(module, 'prepare', return_value=home))
                            module.main()
                        actual = rules.read_text()
                        self.assertIn(expected.rstrip(), actual)
                        self.assertIn('USER_GLOBAL_RULES', actual)
                        if revision:
                            self.assertEqual(actual.count(revision), 1)
                        if revision != 'FIRST_PROFILE_REVISION':
                            self.assertNotIn('FIRST_PROFILE_REVISION', actual)
                        if not revision:
                            self.assertNotIn('SECOND_PROFILE_REVISION', actual)
            for name in ('AGENTS.md', 'CLAUDE.md'):
                self.assertEqual((project / name).read_text(), 'USER_PROJECT_RULES\n')


if __name__ == '__main__':
    unittest.main()
