"""Profile setup and status rendering, including dismissed asynchronous queries."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Select, Static, Checkbox
from ai_sandbox_factory import sandbox, profile_services
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, DEFAULT_SETTINGS, NewSandbox, SettingsForm, CreateFromSandbox, preset
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
                        review = '\n'.join(str(widget.render()) for widget in form.query('#review-profile-services Static'))
                        self.assertIn(profile_services.data_guidance(), review)
                        self.assertNotIn('workspace rebuild', review)
                        row = form.query_one('#review-profile-services Bullet')
                        marker = row.query_one('.bullet-marker')
                        body = row.query_one('.bullet-text')
                        self.assertEqual(body.region.x, marker.region.x + 2)
                        self.assertEqual(body.region.y, marker.region.y)
                        self.assertGreater(body.region.height, 1)

    async def test_copy_and_rebuild_explain_data_and_process_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                records = [{'name':name, 'dev':'running', 'config':{'primary_agent':'pi', 'engine':'podman',
                            'profile':sandbox.load_profile(profile)}}
                           for name, profile in (('database-project','developer-postgres'), ('plain-project','base'))]
                screen = CreateFromSandbox(records)
                await app.push_screen(screen)
                await pilot.pause()
                self.assertGreaterEqual(screen.query_one('#copy-source-label').region.y,
                                        screen.query_one('#copy-sandbox-fields').content_region.y)
                self.assertEqual(screen.query_one('#copy-keys').region.y
                                 - screen.query_one('#copy-project-help').region.bottom, 1)
                self.assertTrue(screen.query_one('#copy-data-help').display)
                self.assertIn('not copied', str(screen.query_one('#copy-data-help', Static).render()))
                screen.query_one('#copy-source', Select).value = 'plain-project'
                await pilot.pause()
                self.assertFalse(screen.query_one('#copy-data-help').display)
                await pilot.press('escape')
                for agent in ('pi','codex','claude','agy'):
                    record = copy.deepcopy(records[0])
                    record['config']['primary_agent'] = agent
                    app.perform('rebuild', record=record)
                    await pilot.pause()
                    message = app.screen.message
                    self.assertIn('If the sandbox is running, work in its coding-agent container stops after the build', message)
                    self.assertIn('If the sandbox is stopped, it stays stopped.', message)
                    self.assertIn('Profile services and their application data are kept', message)
                    self.assertNotIn('Fetch current profile sources', message)
                    await pilot.press('escape')

    async def test_rebuild_names_the_actual_profile_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                profile = sandbox.load_profile('developer-postgres')
                record = {'name':'example', 'dev':'stopped', 'config':{'profile':profile, 'primary_agent':'pi'}}
                for installed in (True, False):
                    with patch('ai_sandbox_factory.sandbox_tui.profile_titles',
                               return_value={'developer-postgres':'Developer + PostgreSQL'} if installed else {}):
                        app.perform('rebuild', record=record)
                        await pilot.pause()
                        self.assertIn('the installed profile' if installed else 'its saved profile', app.screen.message)
                        self.assertIn('If the sandbox is stopped, it stays stopped.', app.screen.message)
                        self.assertEqual(app.focused.id, 'yes')
                        with patch.object(app, 'start_rebuild') as rebuild:
                            await pilot.press('enter')
                            await pilot.pause()
                            rebuild.assert_called_once_with('example', ['recreate', 'example', '--profile',
                                'developer-postgres' if installed else profile['path']])

    async def test_wrapped_features_summary_preserves_keyboard_and_live_values(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            for size in ((80,24),(120,40),(220,50)):
                with self.subTest(size=size):
                    app = Cockpit(Path(tmp), FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await app.workers.wait_for_complete()
                        draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='example', workspace='/home/example/project')
                        screen = SettingsForm(draft)
                        await app.push_screen(screen)
                        section = screen.query_one('#skills-section')
                        title = section.query_one('CollapsibleTitle')
                        title.focus()
                        title.scroll_visible(top=True, animate=False)
                        await pilot.pause()
                        self.assertIn('Browser on', title.label.plain)
                        self.assertGreater(title.region.height, 1)
                        self.assertTrue(section.collapsed)
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertFalse(section.collapsed)
                        screen.query_one('#browser', Checkbox).value = False
                        await pilot.pause()
                        self.assertIn('Browser off', title.label.plain)
                        title.focus()
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertTrue(section.collapsed)
                        self.assertIn('Browser off', title.label.plain)

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
