import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from ai_sandbox_factory.creation import ResourceConflicts
from ai_sandbox_factory.sandbox_tui import Cockpit, ConfirmCreationConflicts, DEFAULT_SETTINGS, SettingsForm, preset
from test_tui import FakeService


class CreationConflictUI(unittest.IsolatedAsyncioTestCase):
    async def test_existing_resources_need_both_confirmations_before_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = FakeService([])
            found = ResourceConflicts('podman', None, (('stopped-codex', 'a' * 64),),
                                      (('codex-agent-home', 'volume-generation'),))
            available = ResourceConflicts('podman', None, (), ())
            diagnostic = ('Could not remove all the listed podman resources (exit 125). '
                          'Check the remaining resources or go back to choose another name.\n'
                          'Error: volume codex-agent-home is still in use by container fixture.')
            service.check_creation = Mock(side_effect=[found, found, found, ValueError(diagnostic), found, available])
            app = Cockpit(root / 'state', service)
            draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name='codex-test', workspace=str(root), engine='podman')
            submitted = []
            with patch('ai_sandbox_factory.updates.available_update', return_value=None):
                async with app.run_test(size=(80, 24)) as pilot:
                    await app.workers.wait_for_complete()
                    form = SettingsForm(draft, on_create=submitted.append)
                    await app.push_screen(form)
                    for cancel in ('containers', 'volumes', 'engine error', None):
                        before = service.check_creation.call_count
                        form.query_one('#apply', Button).press()
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ConfirmCreationConflicts)
                        self.assertIn('stopped-codex', str(app.screen.query_one('#creation-conflict-list', Static).render()))
                        self.assertEqual(app.focused.id, 'cancel')
                        if cancel == 'containers':
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertIs(app.screen, form)
                            self.assertFalse(submitted)
                            self.assertEqual(service.check_creation.call_args.args[1], None)
                            continue
                        await pilot.click('#yes')
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ConfirmCreationConflicts)
                        self.assertEqual(app.focused.id, 'cancel')
                        message = str(app.screen.query_one('#creation-conflict-list', Static).render())
                        self.assertIn('codex-test', message)
                        self.assertIn('codex-agent-home', message)
                        self.assertIn('saved chats, sign-in data and editor settings', ' '.join(message.split()))
                        self.assertIn('podman volume export VOLUME', ' '.join(message.split()))
                        self.assertFalse(submitted)
                        self.assertEqual(service.check_creation.call_count, before + 1)
                        self.assertIsNone(service.check_creation.call_args.args[1])
                        if cancel == 'volumes':
                            await pilot.press('escape')
                            await pilot.pause()
                            self.assertIs(app.screen, form)
                        else:
                            await pilot.click('#yes')
                            await pilot.pause()
                            await app.workers.wait_for_complete()
                            await pilot.pause()
                            self.assertEqual(service.check_creation.call_args.args[1], found)
                            if cancel == 'engine error':
                                self.assertIs(app.screen, form)
                                self.assertFalse(submitted)
                                feedback = form.query_one('#form-error', Static)
                                scroll = form.query_one(VerticalScroll)
                                self.assertIn('exit 125', str(feedback.render()))
                                self.assertLessEqual(feedback.region.bottom, scroll.content_region.bottom)
                                self.assertFalse(form.query_one('#apply', Button).disabled)
                                continue
                            self.assertEqual([request.draft['name'] for request in submitted], ['codex-test'])
