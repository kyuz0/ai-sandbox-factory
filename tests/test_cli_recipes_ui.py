"""CLI preview is optional, preserves forms and records the submitted request."""
import asyncio
import copy
from threading import Event
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Input, Select, TextArea

from ai_sandbox_factory import cli_recipes, operation_locks, operations, sandbox
from ai_sandbox_factory.cli_recipes_ui import CLICommands
from ai_sandbox_factory.sandbox_jobs import Job
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails
from ai_sandbox_factory.sandbox_tui import Cockpit, MoreActions, SettingsForm
from test_tui import FakeService


class CommandsUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        locks = self.root / 'locks'
        locks.mkdir()
        self.enterContext(patch.object(operation_locks, 'private_runtime_dir', return_value=locks))
        self.state = self.root / 'state'
        (self.state / 'demo').mkdir(parents=True)
        (self.state / 'demo' / 'allowlist.txt').write_text('')
        self.enterContext(patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'library'),
                                                  'XDG_CONFIG_HOME': str(self.root / 'preferences')}))
        self.enterContext(patch('ai_sandbox_factory.updates.available_update', return_value=None))

    async def test_preview_preserves_creation_choices_and_layout(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(self.state, FakeService([]))
            async with app.run_test(size=size) as pilot:
                await app.workers.wait_for_complete()
                draft = operations.preset('base', app.defaults)
                draft.update(name='demo', workspace=str(self.workspace), primary_agent='codex')
                form = SettingsForm(draft)
                await app.push_screen(form)
                form.query_one('#memory', Input).value = '12g'
                form.query_one('#settings-cli', Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, CLICommands)
                commands = app.screen
                area = commands.query_one(TextArea)
                self.assertEqual(commands.query_one('#cli-close').region.bottom,
                                 commands.query_one('#cli-box').content_region.bottom)
                self.assertEqual(area.region.bottom + 1, commands.query_one('#cli-close').region.y)
                self.assertIn('12g', area.text)
                for selector in ('#cli-copy', '#cli-save', '#cli-close'):
                    button = commands.query_one(selector)
                    self.assertEqual(button.region.height, 1)
                    self.assertTrue(commands.region.contains_region(button.region))
                    self.assertTrue(commands.query_one('#cli-box').content_region.contains_region(button.region))
                clipboard = Mock()
                with patch.object(app, 'copy_to_clipboard', clipboard):
                    commands.query_one('#cli-copy', Button).press()
                    await pilot.pause()
                clipboard.assert_called_once_with(area.text)
                destination = self.root / 'recipe.sh'
                commands.save_script(destination)
                self.assertEqual(destination.read_text(), '#!/bin/sh\n' + area.text)
                commands.save_script(self.root / 'missing' / 'recipe.sh')
                self.assertIn('Could not save', str(commands.query_one('#cli-feedback').render()))
                await pilot.press('escape')
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one('#memory', Input).value, '12g')
                self.assertEqual(app.jobs, [])
                for selector in ('#back', '#settings-cli', '#apply'):
                    button = form.query_one(selector)
                    self.assertEqual(button.region.height, 1)
                    self.assertTrue(form.region.contains_region(button.region))
                self.assertLessEqual(form.query_one('#settings-cli').region.right, form.query_one('#apply').region.x)

    async def test_resource_preview_and_completed_job_retain_live_request(self):
        service = FakeService([])
        service.update_settings = Mock()
        config = {'name': 'demo', 'workspace': str(self.workspace), 'primary_agent': 'codex', 'engine': 'podman',
                  'profile': sandbox.load_profile('base'), 'memory': '8g', 'image_source': 'prebuilt'}
        service.records = [dict(name='demo', profile='base', engine='podman', dev='running', proxy='running',
                               workspace=str(self.workspace), config=config)]
        app = Cockpit(self.state, service)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            draft = {**config, 'profile': config['profile']['path']}
            form = SettingsForm(draft, editing=True, setting='resources')
            await app.push_screen(form)
            form.query_one('#image-source', Select).value = 'local'
            preview = form.command_recipe()
            self.assertEqual(json.loads(preview.steps[0].stdin), {'image_source': 'local'})
            form.query_one('#apply', Button).press()
            await app.workers.wait_for_complete()
            await pilot.pause()
            job = app.jobs[-1]
            self.assertEqual(job.result, 'Complete')
            self.assertEqual(job.cli_recipe, preview)
            config['image_source'] = 'local'
            view = app.screen
            self.assertIsInstance(view, JobDetails)
            view.timer.pause()
            view.refresh_job()
            self.assertTrue(view.query_one('#job-cli').display)
            view.query_one('#job-cli', Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, CLICommands)
            self.assertEqual(app.screen.recipe, preview)
            service.update_settings.assert_called_once()

    async def test_recipe_job_button_is_absent_until_success_and_on_failure(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            job = Job('demo', 'Apply changes', 'Working')
            self.addCleanup(job.close)
            job.cli_recipe = cli_recipes.update('demo', {'image_source': 'local'}, self.state)
            screen = JobDetails(job)
            await app.push_screen(screen)
            screen.timer.pause()
            self.assertFalse(screen.query_one('#job-cli').display)
            job.result = 'Failed'
            screen.refresh_job()
            self.assertFalse(screen.query_one('#job-cli').display)
            job.result = 'Complete'
            screen.refresh_job()
            self.assertTrue(screen.query_one('#job-cli').display)

    async def test_actions_exports_without_changing_or_starting_sandbox(self):
        profile = sandbox.snapshot_profile(sandbox.load_profile('base'), self.state / 'demo')
        config = {'name': 'demo', 'workspace': str(self.workspace), 'primary_agent': 'codex', 'engine': 'podman',
                  'profile': profile, 'network': 'allowlist', 'memory': '8g', 'subagents': False}
        (self.state / 'demo' / 'config.json').write_text(json.dumps(config))
        record = dict(name='demo', dev='stopped', config=config)
        service = FakeService([])
        service.cli_recipe = operations.Service(self.state).cli_recipe
        app = Cockpit(self.state, service)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            menu = MoreActions(record)
            self.assertIn((cli_recipes.LABEL, 'cli-commands'), menu.actions)
            original = copy.deepcopy(config)
            app.perform('cli-commands', record=record)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, CLICommands)
            text = app.screen.query_one(TextArea).text
            self.assertIn('demo-copy', text)
            self.assertIn('--reuse-config-revision', text)
            self.assertEqual(config, original)
            self.assertFalse(app.jobs)

    async def test_dismissed_loader_cannot_replace_a_new_dialog_on_success_or_failure(self):
        for fail in (False, True):
            entered, release = Event(), Event()
            recipe = cli_recipes.update('demo', {'image_source': 'local'}, self.state)
            def loader():
                entered.set()
                release.wait()
                if fail:
                    raise ValueError('Synthetic credential status transport failure')
                return recipe
            app = Cockpit(self.state, FakeService([]))
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                old = CLICommands(loader=loader)
                await app.push_screen(old)
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                    self.assertTrue(old.query_one('#cli-copy', Button).disabled)
                    await pilot.press('escape')
                    current = CLICommands(cli_recipes.update('demo', {'memory': '12g'}, self.state))
                    await app.push_screen(current)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen, current)
                self.assertIn('12g', current.query_one(TextArea).text)
                self.assertFalse(current.query_one('#cli-feedback').display)

    async def test_real_pi_form_has_short_commands_without_empty_advisors(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            draft = operations.preset('base', app.defaults)
            draft.update(name='demo', workspace=str(self.workspace))
            form = SettingsForm(draft)
            await app.push_screen(form)
            recipe = form.command_recipe()
            self.assertIsNone(recipe.steps[0].stdin)

    async def test_unexpected_loader_error_and_noop_have_no_copy_or_save_action(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            def broken():
                raise KeyError('manifest')
            failed = CLICommands(loader=broken)
            await app.push_screen(failed)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn('KeyError', str(failed.query_one('#cli-feedback').render()))
            self.assertIs(app.focused, failed.query_one('#cli-close'))
            for selector in ('#cli-copy', '#cli-save'):
                self.assertTrue(failed.query_one(selector, Button).disabled)
            await pilot.press('escape')
            empty = CLICommands(cli_recipes.Recipe('No changes to apply', (), str(self.state)))
            await app.push_screen(empty)
            await pilot.pause()
            for selector in ('#cli-copy', '#cli-save'):
                self.assertTrue(empty.query_one(selector, Button).disabled)
            self.assertIs(app.focused, empty.query_one('#cli-close'))

    async def test_application_exit_with_a_saved_setup_read_in_flight(self):
        entered, release, returned = Event(), Event(), Event()
        recipe = cli_recipes.update('demo', {'image_source': 'local'}, self.state)
        def loader():
            entered.set()
            release.wait()
            returned.set()
            return recipe
        app = Cockpit(self.state, FakeService([]))
        try:
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await app.push_screen(CLICommands(loader=loader))
                self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                app.exit()
                async def exited():
                    while app.is_running:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(exited(), 10)
                release.set()
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(returned.wait, 10))
