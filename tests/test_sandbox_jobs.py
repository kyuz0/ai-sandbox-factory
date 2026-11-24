import asyncio
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from textual.widgets import Button, OptionList, RichLog

from ai_sandbox_factory.sandbox_jobs_ui import JobDetails, JobsMenu
from ai_sandbox_factory.sandbox_tui import Cockpit, Confirm, MoreActions, SandboxTable
from ai_sandbox_factory.updating import UpdateCancelled
from test_tui import FakeService


def records():
    return [dict(name=name, dev='running', proxy='running', engine='podman', profile='base', workspace='/fixture',
                 config={'engine': 'podman', 'workspace': '/fixture'}) for name in ('alpha', 'beta')]


class JobsService(FakeService):
    def __init__(self):
        super().__init__(records())
        self.started = {name: Event() for name in ('alpha', 'beta')}
        self.finish = {name: Event() for name in self.started}

    def run_update(self, args, log, update):
        _, name, *_ = args
        self.calls.append(args)
        log(f'{name}: building the sandbox')
        self.started[name].set()
        while not self.finish[name].wait(0.02):
            if not update.can_cancel:
                raise UpdateCancelled()
        if name == 'beta':
            raise ValueError('Podman build exited with status 125: fixture filesystem refused the image')
        log(f'{name}: finished building')


async def wait_for(pilot, condition):
    async def until_ready():
        while not condition():
            await pilot.pause(0.05)
    await asyncio.wait_for(until_ready(), timeout=12)


async def begin(pilot, app, name):
    table = app.query_one(SandboxTable)
    table.move_cursor(row=table.get_row_index(name), column=0)
    table.focus()
    await pilot.press('a')
    menu = app.screen
    assert isinstance(menu, MoreActions) and menu.sandbox_name == name, type(menu)
    choices = menu.query_one(OptionList)
    choices.highlighted = next(i for i, (_, action) in enumerate(menu.actions) if action == 'rebuild')
    await pilot.press('enter')
    assert isinstance(app.screen, Confirm), type(app.screen)
    await pilot.press('enter')
    await wait_for(pilot, lambda: isinstance(app.screen, JobDetails))
    return app.screen.job


def output(screen):
    return '\n'.join(line.text for line in screen.query_one(RichLog).lines)


class SandboxJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_hide_open_another_job_reopen_output_and_confirm_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            service = JobsService()
            app = Cockpit(Path(tmp), service)
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await wait_for(pilot, lambda: len(app.records) == 2)
                    alpha = await begin(pilot, app, 'alpha')
                    await wait_for(pilot, service.started['alpha'].is_set)
                    await pilot.press('escape')
                    self.assertFalse(app.check_action('quit', ()))
                    self.assertIs(app.screen, app.screen_stack[0])
                    beta = await begin(pilot, app, 'beta')
                    await wait_for(pilot, service.started['beta'].is_set)
                    await pilot.click('#job-hide')
                    self.assertIn('2 active', str(app.query_one('#jobs', Button).label))
                    app.perform('stop', record=app.records[0])
                    await wait_for(pilot, lambda: isinstance(app.screen, JobDetails))
                    await pilot.pause()
                    self.assertIs(app.screen.job, alpha)
                    self.assertFalse(any(action[0] == 'down' for action in service.calls))
                    await pilot.click('#job-cancel')
                    self.assertEqual(app.focused.id, 'cancel')
                    await pilot.press('enter')
                    self.assertTrue(alpha.update.can_cancel)
                    await pilot.click('#job-cancel')
                    await pilot.click('#yes')
                    await wait_for(pilot, lambda: alpha.result == 'Cancelled')
                    await pilot.press('escape')
                    await pilot.press('j')
                    self.assertIsInstance(app.screen, JobsMenu)
                    self.assertIn('alpha · Cancelled', app.screen.labels)
                    await pilot.press('enter')
                    self.assertIs(app.screen.job, beta)
                    await wait_for(pilot, lambda: 'beta: building the sandbox' in output(app.screen))
                    self.assertNotIn('alpha:', output(app.screen))
                    await pilot.press('escape')
                    selected = app.selected_name()
                    service.finish['beta'].set()
                    await wait_for(pilot, lambda: beta.result == 'Failed' and app.completed_rebuilds == 2)
                    self.assertIs(app.screen, app.screen_stack[0])
                    self.assertEqual(app.selected_name(), selected)
                    await pilot.click('#jobs')
                    await pilot.press('enter')
                    await wait_for(pilot, lambda: 'status 125' in output(app.screen))
                    self.assertIn('fixture filesystem refused the image', ' '.join(output(app.screen).split()))
                    self.assertTrue(app.check_action('quit', ()))
                    self.assertEqual([call[1] for call in service.calls], ['alpha', 'beta'])
                finally:
                    for event in service.finish.values():
                        event.set()
                    await app.workers.wait_for_complete()
