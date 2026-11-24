import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from ai_sandbox_factory import prompt_library, sandbox
from ai_sandbox_factory.prompt_ui import EditPrompt, PromptChoice, SandboxPromptScreen
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, PackagesScreen, SettingsForm, preset
from ai_sandbox_factory.tui_widgets import Feedback
from test_tui import FakeService


class SystemPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(self.root / 'library')))
        self.enterContext(patch('ai_sandbox_factory.sandbox_tui.load_defaults', return_value=DEFAULT_SETTINGS))
        self.enterContext(patch('ai_sandbox_factory.updates.available_update', return_value=None))

    def test_editing_or_removing_library_entry_preserves_sandbox_copy(self):
        prompt_library.save('Research', 'Use primary sources.')
        entries = prompt_library.entries()
        key, value = next(iter(entries.items()))
        state = self.root / 'sandbox'
        prompt_library.assign(state, value)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            prompt_library.save('research', 'Duplicate.')
        prompt_library.save('Research', 'Read the original report.', key)
        prompt_library.remove(key)
        self.assertEqual(prompt_library.selection(state), value)
        self.assertFalse(prompt_library.entries())
        self.assertEqual((state / prompt_library.SANDBOX_FILE).stat().st_mode & 0o777, 0o600)

    def test_create_and_edit_from_library_then_assign_without_container_operation(self):
        asyncio.run(self.check_library_and_assignment())

    async def check_library_and_assignment(self):
        state = self.root / 'sandbox'
        app = Cockpit(self.root, FakeService([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await app.push_screen(PackagesScreen(initial='system-prompts'))
            await pilot.pause()
            await pilot.click('#package-install')
            await pilot.pause()
            self.assertIsInstance(app.screen, EditPrompt)
            await pilot.click('#save-prompt')
            await pilot.pause()
            error = app.screen.query_one('#prompt-name-error', Feedback)
            self.assertIn('Enter a name.', str(error.render()))
            self.assertIs(app.focused, app.screen.query_one('#prompt-name', Input))
            app.screen.query_one(Input).value = 'Research'
            app.screen.query_one(TextArea).load_text('Use primary sources.')
            app.screen.query_one('#save-prompt', Button).press()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, PackagesScreen)
            key, value = next(iter(prompt_library.entries().items()))
            self.assertEqual(value['text'], 'Use primary sources.')
            await pilot.click('.edit-system-prompt')
            await pilot.pause()
            app.screen.query_one(TextArea).load_text('Cite the report.')
            await pilot.click('#save-prompt')
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(prompt_library.entries()[key]['text'], 'Cite the report.')
            await app.pop_screen()

            draft = preset('base', DEFAULT_SETTINGS)
            draft.update(name='sandbox', workspace=str(self.root))
            await app.push_screen(SettingsForm(draft))
            await pilot.pause()
            section = app.screen.query_one('#system-prompt-section', Collapsible)
            self.assertTrue(section.collapsed)
            self.assertEqual(section.title, 'Custom system prompt add-on · No add-on')
            section.collapsed = False
            await pilot.pause()
            choice = app.screen.query_one(PromptChoice)
            self.assertIsNone(choice.selected())
            self.assertEqual(choice.choices()[0], ('No add-on', ''))
            self.assertIn('Pi uses its usual system prompt',
                          app.screen.query_one('#system-prompt-help', Static).render().plain)
            app.screen.query_one('#system-prompt-choice', Select).value = key
            await pilot.pause()
            self.assertEqual(section.title, 'Custom system prompt add-on · Research')
            self.assertEqual(app.screen.query_one(PromptChoice).selected(), prompt_library.entries()[key])
            await app.pop_screen()

            await app.push_screen(SandboxPromptScreen('sandbox', state, True))
            await pilot.pause()
            self.assertEqual(app.screen.query_one('#system-prompt-timing', Static).render().plain,
                             'Pi reads the add-on when a new turn starts. You can keep the chat open.')
            app.screen.query_one('#system-prompt-choice', Select).value = key
            await pilot.pause()
            await pilot.click('#edit-prompt-copy')
            await pilot.pause()
            app.screen.query_one(TextArea).load_text('Cite the source and date.')
            await pilot.click('#save-prompt')
            await pilot.pause()
            await pilot.click('#sandbox-prompt-save')
            await pilot.pause()
            self.assertEqual(prompt_library.selection(state)['text'], 'Cite the source and date.')
            self.assertEqual(prompt_library.entries()[key]['text'], 'Cite the report.')
            self.assertTrue((state / 'activity/edited.json').is_file())
            await app.push_screen(SandboxPromptScreen('sandbox', state, True))
            await pilot.pause()
            app.screen.query_one('#system-prompt-choice', Select).value = ''
            await pilot.press('escape')
            await pilot.pause()
            self.assertIsNotNone(prompt_library.selection(state))

            await app.push_screen(SandboxPromptScreen('sandbox', state, True))
            await pilot.pause()
            app.screen.query_one('#system-prompt-choice', Select).value = ''
            await pilot.click('#sandbox-prompt-save')
            await pilot.pause()
            self.assertIsNone(prompt_library.selection(state))
            self.assertFalse(app.service.calls)

    def test_cli_creation_persists_prompt_and_prepare_options_keeps_it(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        source = self.root / 'prompt.json'
        value = {'name': 'Research', 'text': 'Use primary sources.'}
        source.write_text(json.dumps(value))
        state_root = self.root / 'state'
        args = ['pi-sandbox-factory', '--state-dir', str(state_root), 'create', 'sandbox',
                '--profile', 'base', '--workspace', str(workspace), '--engine', 'docker',
                '--network', 'unrestricted', '--system-prompt-file', str(source)]
        with (
            patch('sys.argv', args),
            patch.object(sandbox, 'choose_engine', return_value='docker'),
            patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=self.root),
            patch.object(sandbox.host_platform, 'check_engine', return_value=None),
        ):
            sandbox.main()
        state = state_root / 'sandbox'
        config = json.loads((state / 'config.json').read_text())
        sandbox.prepare_options(config, state)
        self.assertEqual(prompt_library.selection(state), value)
