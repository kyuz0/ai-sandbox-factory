import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import primary_workspace, sandbox


class PrimaryWorkspaceTests(unittest.TestCase):
    def test_cli_tool_changes_invalidate_each_native_workspace_image(self):
        original = sandbox.tree_digest
        cli_source = sandbox.ROOT / 'containers/dev/cli-tools'
        config = {'uid': 1000, 'gid': 1000, 'options': {'browser': False}}
        for agent in ('codex', 'claude', 'agy'):
            with self.subTest(agent=agent):
                config['primary_agent'] = agent
                before = primary_workspace.image(config)
                with patch.object(sandbox, 'tree_digest', side_effect=lambda path:
                                  'changed-tools' if path == cli_source else original(path)):
                    self.assertNotEqual(before, primary_workspace.image(config))

    def test_legacy_metadata_keeps_pi_and_existing_options(self):
        legacy = {'name': 'existing', 'uid': 1000, 'gid': 1000,
                  'options': {'browser': True}, 'profile': sandbox.load_profile('base')}
        updated = primary_workspace.normalize(legacy)
        self.assertEqual(updated['primary_agent'], 'pi')
        self.assertEqual(updated['primary_auth'], 'login')
        self.assertNotIn('primary_agent', legacy)
        self.assertEqual(sandbox.image_names(updated), sandbox.image_names(legacy))

    def test_private_paths_cannot_be_mounted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            credential_root = root / 'custom-sign-in'
            credential_root.mkdir()
            workspace = root / 'project'
            workspace.mkdir()
            config = {'workspace': workspace}
            with patch.dict(os.environ, {'CODEX_HOME': str(credential_root)}):
                primary_workspace.validate_mounts(config, state)
                link = workspace / 'linked-sign-in'
                link.symlink_to(credential_root)
                for value in (
                    {**config, 'workspace': credential_root},
                    {**config, 'workspace': state / 'sandbox'},
                    {**config, 'extra_folders': [{'source': link}]},
                    {**config, 'env_file': credential_root / 'env'},
                ):
                    with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'sign-ins'):
                        primary_workspace.validate_mounts(value, state)

    def test_copy_is_explicit_and_only_runs_once_until_reapplied(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = {'primary_agent': 'codex', 'primary_auth': 'copy'}
            with patch('ai_sandbox_factory.primary_agents.copy_host',
                       side_effect=[ValueError('Unavailable'), None]) as copy:
                primary_workspace.initial_sign_in({'primary_agent': 'codex'}, state)
                copy.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'Unavailable'):
                    primary_workspace.initial_sign_in(config, state)
                self.assertFalse((state / 'primary-sign-in-copied').exists())
                primary_workspace.initial_sign_in(config, state)
                primary_workspace.initial_sign_in(config, state)
                self.assertEqual(copy.call_count, 2)
