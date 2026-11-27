"""Keep operational TUI actions connected to documented CLI paths."""
from contextlib import redirect_stdout
import io
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import sandbox, settings_catalog
from ai_sandbox_factory.sandbox_tui import MoreActions

# CLI command, documentation section. Navigation opens the same grouped operations.
ACTIONS = {
    'open': ('open', 'create-and-open'),
    'launch-with': ('open', 'create-and-open'),
    'settings': ('show', 'inspect-and-change-settings'),
    'cli-commands': ('show', 'get-commands-from-the-tui'),
    'start': ('up', 'create-and-open'),
    'stop': ('down', 'create-and-open'),
    'rebuild': ('recreate', 'copies-and-profiles'),
    'install-system-packages': ('install-system-packages', 'inspect-and-change-settings'),
    'stop-editor': ('browser-editor', 'inspect-and-change-settings'),
    'profile-information': ('show', 'copies-and-profiles'),
    'profile-guide': ('show', 'copies-and-profiles'),
    'containers': ('show', 'inspect-and-change-settings'),
    'logs': ('logs', 'inspect-and-change-settings'),
    'shell': ('shell', 'inspect-and-change-settings'),
    'connections': ('exec', 'inspect-and-change-settings'),
    'export-chats': ('chat', 'chats'),
    'import-chats': ('chat', 'chats'),
    'copy-chat': ('chat', 'chats'),
    'delete': ('delete', 'create-and-open'),
    'refresh-creation': ('list', 'inspect-and-change-settings'),
    'alerts': ('show', 'inspect-and-change-settings'),
    'ssh-key-alert': ('ssh-unlock', 'models-and-keys'),
}
NAVIGATION = {'diagnostics': 'opens the troubleshooting actions',
              'chats': 'opens chat history actions'}
SETTINGS = {
    'models': ('models', 'models-and-keys'),
    'editor-settings': ('editor', 'inspect-and-change-settings'),
    'folders': ('update', 'inspect-and-change-settings'),
    'network-settings': ('update', 'inspect-and-change-settings'),
    'approval-settings': ('update', 'inspect-and-change-settings'),
    'agent-tools': ('update', 'inspect-and-change-settings'),
    'system-prompt': ('prompt', 'libraries-and-defaults'),
    'advisors': ('update', 'models-and-keys'),
    'communications': ('communication-key', 'models-and-keys'),
    'skills': ('skill', 'libraries-and-defaults'),
    'connectors': ('update', 'inspect-and-change-settings'),
    'document-search': ('update', 'inspect-and-change-settings'),
    'resources': ('update', 'inspect-and-change-settings'),
}
LIBRARIES = {'profile', 'provider', 'skill', 'prompt', 'defaults', 'self-update', 'copy',
             'profile-fork', 'profile-review', 'profile-apply',
             'service-data-list', 'service-data-export', 'service-data-restore'}


class CliParity(unittest.TestCase):
    def test_conditional_actions_have_a_cli_operation_or_navigation_reason(self):
        seen = set()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / 'start-here.md').write_text('Profile guide')
            for agent, running, editor, creation in product(
                    ('pi', 'codex', 'claude', 'agy'), (False, True),
                    (False, True), (None, 'running', 'interrupted')):
                record = {'name': 'example', 'dev': 'running' if running else 'exited',
                          'creation': creation, 'ssh_key_state': 'locked',
                          'config': {'name': 'example', 'engine': 'podman', 'primary_agent': agent,
                                     'profile': {'path': str(path), 'name': 'example', 'digest': 'abc'}}}
                menu = MoreActions(record, browser_running=editor)
                actions = {action for _, action in menu.actions}
                actions.update(action for _, rows in menu.pages.values() for _, action in rows)
                self.assertFalse(actions - ACTIONS.keys() - NAVIGATION.keys(),
                                 f'Unmapped actions for {agent}: {actions}')
                seen.update(actions)
            self.assertTrue(set(ACTIONS) - {'ssh-key-alert'} <= seen)

    def test_settings_categories_are_all_mapped(self):
        self.assertEqual(set(SETTINGS), set(settings_catalog.CATEGORIES))

    def test_mapped_commands_parse_and_documentation_sections_exist(self):
        guide = (sandbox.ROOT.parent / 'docs/cli.md').read_text()
        sections = {line[3:].lower().replace(' ', '-') for line in guide.splitlines() if line.startswith('## ')}
        paths = [*ACTIONS.values(), *SETTINGS.values()]
        for _, section in paths:
            self.assertIn(section, sections)
        for command in sorted({item[0] for item in paths} | LIBRARIES):
            with self.subTest(command=command), patch('sys.argv', ['ai-sandbox-factory', command, '--help']), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    sandbox.main()
                self.assertEqual(result.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
