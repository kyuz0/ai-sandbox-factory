"""Update status remains separate from Open and Alerts; help is optional."""
import copy
from unittest.mock import Mock, patch

from textual.widgets import Button, DataTable, Static
import unittest
import test_tui as fixtures
from test_tui import FakeService
from ai_sandbox_factory.sandbox_tui import Cockpit, FactoryUpdateDetails
from ai_sandbox_factory.container_updates_ui import ContainerUpdates


class ContainerUpdateUITests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TUITests.setUp
    tearDown = fixtures.TUITests.tearDown
    ready = fixtures.TUITests.ready
    async def test_updates_column_review_help_and_cancel_at_supported_sizes(self):
        self.records[0]['updates'] = dict(status='available', roles=['gateway'], workspace=False,
            changes=[{'role': 'gateway', 'label': 'Network gateways'}], revision='review-1',
            effects=['Only the listed Factory helper containers restart.'])
        self.records[1]['updates'] = dict(status='custom', reason='You maintain this custom profile’s software and services.')
        for size in ((80, 24), (120, 40), (220, 50)):
            with self.subTest(size=size):
                app = Cockpit(self.root, FakeService(self.records))
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    table = app.query_one('#sandboxes', DataTable)
                    self.assertIn('Available', str(table.get_cell('alpha', 'updates')))
                    self.assertIn('Custom profile', str(table.get_cell('beta', 'updates')))
                    self.assertIn('Open', str(table.get_cell('alpha', 'open')))
                    column = table.get_cell_coordinate('alpha', 'updates')
                    table.move_cursor(row=column.row, column=column.column)
                    table.focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ContainerUpdates)
                    self.assertEqual(app.focused.id, 'container-updates-apply')
                    self.assertTrue(app.screen.region.contains_region(app.focused.region))
                    self.assertEqual(app.focused.region.height, 1)
                    text = '\n'.join(str(w.render()) for w in app.screen.query(Static))
                    self.assertNotIn('compares the versions', text)
                    self.assertNotIn('Packages installed', text)
                    await pilot.click('#container-updates-help')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, FactoryUpdateDetails)
                    self.assertIn('compares the versions', app.screen.message)
                    await pilot.press('escape')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ContainerUpdates)
                    await pilot.press('escape')
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ContainerUpdates)
                    self.assertFalse(app.service.calls)

    async def test_current_has_no_empty_action_and_custom_has_no_apply(self):
        self.records[0]['updates'] = {'status': 'current'}
        self.records[1]['updates'] = {'status': 'custom', 'reason': 'You maintain this custom profile.'}
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            self.assertEqual(app.query_one(DataTable).get_cell('alpha', 'updates'), '')
            app.perform('container-updates', record=self.records[1])
            await pilot.pause()
            self.assertFalse(app.screen.query('#container-updates-apply'))
            self.assertEqual(app.focused.id, 'container-updates-back')

    async def test_apply_uses_exact_reviewed_revision_without_extra_confirmation(self):
        record = self.records[0]
        record['updates'] = dict(status='available', roles=['workspace'], workspace=True,
            changes=[{'role': 'workspace', 'label': 'Main agent sandbox'}], revision='review-1',
            effects=['Running work stops after preparation.'])
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            app.push_screen(ContainerUpdates(record))
            await pilot.pause()
            with patch.object(app, 'start_settings_job') as start:
                await pilot.press('enter')
                await pilot.pause()
                start.assert_called_once()
                task = start.call_args.args[2]
                app.service.run_update = Mock()
                log, update = Mock(), Mock()
                task(log, update)
                app.service.run_update.assert_called_once_with(['update-containers', 'alpha', '--revision', 'review-1'], log, update)

    async def test_completed_job_clears_update_and_failed_review_keeps_it(self):
        from test_sandbox_jobs import wait_for
        from ai_sandbox_factory.sandbox_jobs_ui import JobDetails
        for fails in (False, True):
            with self.subTest(fails=fails):
                record = copy.deepcopy(self.records[0])
                record['updates'] = dict(status='available', roles=['gateway'], workspace=False,
                    changes=[{'role': 'gateway', 'label': 'Network gateways'}], revision='review-1',
                    effects=['Only the listed Factory helper containers restart.'])
                service = FakeService([record])
                error = 'The container update plan changed. Run update-containers --preview again.'
                def run_update(command, log, update):
                    if fails:
                        raise ValueError(error)
                    service.records[0]['updates'] = {'status': 'current'}
                    log('Containers updated.')
                service.run_update = run_update
                app = Cockpit(self.root, service)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(app, pilot)
                    app.perform('container-updates', record=record)
                    await pilot.pause()
                    await pilot.press('enter')
                    await wait_for(pilot, lambda: bool(app.jobs) and app.jobs[0].result is not None)
                    self.assertIsInstance(app.screen, JobDetails)
                    job = app.jobs[0]
                    if fails:
                        self.assertEqual(job.result, 'Failed')
                        self.assertIn(error, job.error)
                        await wait_for(pilot, lambda: error in ' '.join(line.text.strip() for line in app.screen.query_one('#operation-log').lines))
                        self.assertIn('Available', str(app.screen_stack[0].query_one(DataTable).get_cell('alpha', 'updates')))
                    else:
                        self.assertEqual(job.result, 'Complete')
                        await wait_for(pilot, lambda: app.screen_stack[0].query_one(DataTable).get_cell('alpha', 'updates') == '')
