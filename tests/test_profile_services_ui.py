"""Profile setup and status rendering, including dismissed asynchronous queries."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Select, Static
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, DEFAULT_SETTINGS, NewSandbox, SettingsForm, preset
from test_tui import FakeService


class ProfileServicesUI(unittest.IsolatedAsyncioTestCase):
    async def test_profile_selection_for_every_agent_and_service_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            for agent in ('pi', 'codex', 'claude', 'agy'):
                with self.subTest(agent=agent), patch('ai_sandbox_factory.updates.available_update', return_value=None):
                    app = Cockpit(Path(tmp), FakeService([]))
                    async with app.run_test(size=(80, 24)) as pilot:
                        await app.workers.wait_for_complete()
                        new = NewSandbox()
                        await app.push_screen(new)
                        new.query_one('#primary-agent', Select).value = agent
                        await pilot.pause()
                        self.assertTrue(new.query_one('#profile-setting').display)
                        new.query_one('#profile', Select).value = 'developer-postgres'
                        await pilot.pause()
                        draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='example', primary_agent=agent, workspace='/home/example/project')
                        form = SettingsForm(draft)
                        await app.push_screen(form)
                        await pilot.pause()
                        self.assertIn('Developer + PostgreSQL', str(form.query_one('#review-project', Static).render()))
                        self.assertIn('database', ' '.join(str(widget.render()) for widget in form.query('#review-profile-services Static')))
                        self.assertEqual(form.query_one('#apply', Button).region.height, 1)

    async def test_dismissed_status_result_cannot_update_another_page(self):
        entered, release = threading.Event(), threading.Event()
        def status(config):
            entered.set(); release.wait()
            return [{'name': 'database', 'running': True}]
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None), \
                patch('ai_sandbox_factory.profile_services.status', side_effect=status):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                screen = AppliedProfile({'name': 'example', 'config': {'profile': sandbox.load_profile('developer-postgres')}}, Path(tmp))
                try:
                    await app.push_screen(screen)
                    await asyncio.to_thread(entered.wait)
                    await pilot.press('escape')
                    self.assertNotIn(screen, app.screen_stack)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen, app.screen_stack[0])
