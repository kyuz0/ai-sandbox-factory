import asyncio
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import PropertyMock, patch

from textual.widgets import Button, Input, OptionList, RichLog, Static

from ai_sandbox_factory.models_ui import ModelDialog
from ai_sandbox_factory.sandbox_jobs import Job, RebuildJob
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails, JobsMenu
from ai_sandbox_factory.sandbox_tui import Cockpit, Confirm, CreationRequest, InstallSystemPackages, MoreActions, SandboxTable
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


async def wait_for(pilot, condition, *, timeout=12):
    async def until_ready():
        while not condition():
            await pilot.pause(0.05)
    await asyncio.wait_for(until_ready(), timeout=timeout)


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
    log = screen.query_one_optional(RichLog)
    return '\n'.join(line.text for line in log.lines) if log is not None else ''


class SandboxJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_package_menu_validates_input_and_starts_a_sandbox_job(self):
        service = JobsService()
        calls = []

        def run_update(args, log, update):
            calls.append(args)
            log('Packages installed.')

        service.run_update = run_update
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), service)
            async with app.run_test(size=(80, 24)) as pilot:
                await wait_for(pilot, lambda: len(app.records) == 2)
                app.query_one(SandboxTable).focus()
                await pilot.press('a')
                menu = app.screen
                index = menu.actions.index(('Install system packages…', 'install-system-packages'))
                menu.query_one(OptionList).highlighted = index
                await pilot.press('enter')
                self.assertIsInstance(app.screen, InstallSystemPackages)
                field = app.screen.query_one('#system-package-names', Input)
                self.assertIs(app.screen.focused, field)
                warning = str(app.screen.query_one('#system-packages-warning', Static).render())
                for phrase in ('scripts can run as root', 'sandbox stays running'):
                    self.assertIn(phrase, warning)
                field.value = 'jq --allow-unauthenticated'
                await pilot.press('enter')
                self.assertIn('Enter Debian package names', str(app.screen.query_one('#system-packages-error').render()))
                self.assertFalse(calls)
                await pilot.press('escape')
                self.assertFalse(calls)
                app.perform('install-system-packages', record=app.selected())
                await pilot.pause()
                app.screen.query_one(Input).value = 'ripgrep,jq jq'
                await pilot.click('#system-packages-install')
                await wait_for(pilot, lambda: bool(app.jobs) and app.jobs[0].result is not None)
                self.assertEqual(calls, [['install-system-packages', 'alpha', 'jq', 'ripgrep']])
                self.assertEqual(app.jobs[0].targets, ('alpha',))
                self.assertEqual(app.jobs[0].result, 'Complete')

    async def test_successful_delete_clears_previous_setup_without_removing_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), JobsService())
            async with app.run_test(size=(80, 24)) as pilot:
                await wait_for(pilot, lambda: len(app.records) == 2)
                for name in ('alpha', 'beta'):
                    job = Job(name, 'Create · ' + name, 'Creating sandbox')
                    job.run(lambda log: log('Sandbox ready'))
                    job.followup = ('Choose a model', lambda: None)
                    app.jobs.append(job)
                alpha, beta = app.jobs
                deletion = app.operation('Deleting alpha', ['delete', 'alpha'])
                await wait_for(pilot, lambda: deletion.result == 'Complete' and deletion not in app.job_completions)
                self.assertIsNone(alpha.followup)
                self.assertEqual(beta.followup[0], 'Choose a model')
                self.assertIn('Sandbox ready', alpha.read(0)[0])

    async def test_creation_keeps_model_choice_pending_when_hidden(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            service = JobsService()
            started, release = Event(), Event()

            def create(draft, key, log, cancellation):
                cancellation.progress(3, 5, 'Building images')
                log('Podman example STEP 99/99')
                started.set()
                release.wait()
                service.records.append(dict(records()[0], name=draft['name']))
                log('The fixture sandbox is ready.')

            service.create = create
            service.plugin_model = lambda *args, **kwargs: {'models': []}
            app = Cockpit(Path(tmp), service)
            draft = {'name': 'new-sandbox', 'workspace': tmp, 'options': {'approval': 'manual'},
                     '_provider_credential_choices': {'chaucer': {}}, 'open_editor': False}
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await wait_for(pilot, lambda: len(app.records) == 2)
                    job = app.start_creation(CreationRequest(draft, {}, 'Chaucer model catalogue', False))
                    await wait_for(pilot, started.is_set)
                    self.assertEqual(job.status, 'Step 3 of 5 · Building images')
                    self.assertEqual(app.screen.active_bindings['escape'].binding.description, 'Hide details')
                    await pilot.press('escape')
                    self.assertIs(app.screen, app.screen_stack[0])
                    table = app.query_one(SandboxTable)
                    self.assertEqual(str(table.get_cell('new-sandbox', 'dev')), 'Creating')
                    self.assertEqual(str(table.get_cell('new-sandbox', 'open')).strip(), 'View job (3/5)')
                    self.assertEqual(str(app.query_one('#jobs', Button).label), 'Jobs… (1)')
                    table.move_cursor(row=table.get_row_index('new-sandbox'), column=1)
                    table.focus()
                    selected = table.cursor_coordinate
                    job.set_progress(4, 5, 'Starting the sandbox')
                    await wait_for(pilot, lambda: str(table.get_cell('new-sandbox', 'open')).strip() == 'View job (4/5)')
                    self.assertEqual(table.cursor_coordinate, selected)
                    await pilot.press('enter')
                    self.assertIs(app.screen.job, job)
                    await pilot.press('escape')
                    app.show_sandbox_actions('beta')
                    await pilot.pause()
                    other = app.screen
                    release.set()
                    await wait_for(pilot, lambda: job.followup is not None)
                    self.assertIs(app.screen, other)
                    self.assertTrue(app.check_action('quit', ()))
                    await pilot.press('escape')
                    await wait_for(pilot, lambda: draft['name'] in table.rows)
                    self.assertEqual(str(table.get_cell(draft['name'], 'open')).strip(), 'Choose a model')
                    cell = table.get_cell_coordinate(draft['name'], 'open')
                    table.move_cursor(row=cell.row, column=cell.column)
                    table.focus()
                    await pilot.press('enter')
                    await wait_for(pilot, lambda: isinstance(app.screen, ModelDialog))
                    self.assertEqual(app.screen.provider, 'chaucer')
                    self.assertEqual(app.screen.plugin_label, 'Chaucer model catalogue')
                    await pilot.click('#model-later')
                    self.assertEqual(str(table.get_cell(draft['name'], 'open')).strip(), 'Choose a model')
                    await pilot.press('j', 'enter')
                    await wait_for(pilot, lambda: isinstance(app.screen, JobDetails))
                    self.assertIs(app.screen.job, job)
                    await wait_for(pilot, lambda: 'STEP 99/99' in output(app.screen))
                    self.assertEqual(job.result, 'Complete')
                    await pilot.click('#job-continue')
                    await wait_for(pilot, lambda: isinstance(app.screen, ModelDialog))
                    await pilot.click('#model-later')
                    self.assertEqual(job.followup[0], 'Choose a model')
                    await pilot.press('j', 'enter')
                    await wait_for(pilot, lambda: isinstance(app.screen, JobDetails))
                    await pilot.click('#job-continue')
                    await wait_for(pilot, lambda: isinstance(app.screen, ModelDialog))
                    await pilot.click('#model-cancel')
                    self.assertIsNone(job.followup)
                    self.assertIs(app.screen, app.screen_stack[0])
                    self.assertEqual(str(table.get_cell(draft['name'], 'open')).strip(), 'Open')
                    self.assertFalse(service.calls)
                finally:
                    release.set()
                    await app.workers.wait_for_complete()

    async def test_hidden_chaucer_choice_is_saved_before_opening_requested_client(self):
        service = JobsService()
        release = Event()
        model = {'id': 'new-default', 'name': 'New default', 'contextWindow': 8192,
                 'maxTokens': 2048, 'input': ['text'], 'reasoning': False}

        def create(draft, key, log, cancellation):
            release.wait()
            service.records.append(dict(records()[0], name=draft['name']))

        def models(name, action, *, provider, **kwargs):
            self.assertEqual(provider, 'chaucer')
            if action == 'catalogue':
                return {'models': [model]}
            service.calls.append(['select', name, provider, kwargs['model']])
            return {}

        service.create = create
        service.plugin_model = models
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), service)
            draft = {'name': 'new-sandbox', 'workspace': tmp, 'options': {'approval': 'manual'},
                     '_provider_credential_choices': {'chaucer': {}}, 'open_editor': True}
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await wait_for(pilot, lambda: len(app.records) == 2)
                    job = app.start_creation(CreationRequest(draft, {}, 'Chaucer model catalogue', False))
                    await pilot.press('escape')
                    release.set()
                    await wait_for(pilot, lambda: job.followup and len(app.records) == 3)
                    record = next(row for row in app.records if row['name'] == job.name)
                    with patch.object(app, 'interactive') as interactive:
                        app.perform('open', record=record)
                        app.perform('open', record=record)
                        await wait_for(pilot, lambda: isinstance(app.screen, ModelDialog)
                                       or any(call[0] == 'open' for call in service.calls))
                        self.assertIsInstance(app.screen, ModelDialog)
                        await pilot.pause()
                        await pilot.click('#model-later')
                        self.assertFalse(service.calls)
                        interactive.assert_not_called()

                        app.perform('launch-with', record=record)
                        await pilot.pause()
                        choices = app.screen.query_one('#editor-choices', OptionList)
                        choices.highlighted = list(app.screen.modes).index('pi')
                        await pilot.press('enter')
                        await wait_for(pilot, lambda: isinstance(app.screen, ModelDialog) and bool(app.screen.models))
                        self.assertFalse(service.calls)
                        interactive.assert_not_called()
                        app.screen.query_one('#model-results', OptionList).highlighted = 0
                        await wait_for(pilot, lambda: not app.screen.query_one('#model-save', Button).disabled)
                        await pilot.click('#model-save')
                        await wait_for(pilot, lambda: interactive.called)
                        self.assertEqual(service.calls, [['select', job.name, 'chaucer', 'new-default']])
                        interactive.assert_called_once_with('pi', job.name)
                finally:
                    release.set()
                    await app.workers.wait_for_complete()

    async def test_followup_badge_changes_without_the_dashboard_timer(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), JobsService())
            release = Event()
            schedule = app.set_interval

            def schedule_without_job_poll(interval, callback, *args, **kwargs):
                timer = schedule(interval, callback, *args, **kwargs)
                if callback == app.poll_jobs:
                    timer.pause()
                return timer

            with patch.object(app, 'set_interval', side_effect=schedule_without_job_poll):
                async with app.run_test(size=(80, 24)) as pilot:
                    try:
                        await wait_for(pilot, lambda: len(app.records) == 2)
                        badge = app.query_one('#jobs', Button)
                        reviewed = []
                        job = app.start_task('alpha', 'Sandbox task', lambda log: release.wait(10),
                                             on_success=reviewed.append, followup_label='Review result')
                        await pilot.press('escape')
                        self.assertEqual(str(badge.label), 'Jobs… (1)')
                        release.set()
                        await app.workers.wait_for_complete()
                        self.assertEqual(str(badge.label), 'Jobs… (0) · To review')
                        app.show_job(job)
                        await pilot.pause()
                        await pilot.click('#job-continue')
                        await wait_for(pilot, lambda: reviewed == [True])
                        self.assertEqual(str(badge.label), 'Jobs… (0)')
                    finally:
                        release.set()
                        await app.workers.wait_for_complete()

    async def test_cancellation_refused_after_apply_starts_refreshes_details(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), JobsService())
            release = Event()
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await wait_for(pilot, lambda: len(app.records) == 2)
                    job = app.start_job(RebuildJob('alpha'), lambda log, update: release.wait(10))
                    await pilot.pause()
                    details = app.screen
                    details.timer.pause()
                    button = details.query_one('#job-cancel', Button)
                    self.assertFalse(button.disabled)
                    await pilot.click('#job-cancel')
                    with patch.object(type(job.update), 'applying', new_callable=PropertyMock, return_value=True), \
                         patch.object(details, 'notify', wraps=details.notify) as notify:
                        await pilot.click('#yes')
                        self.assertTrue(button.disabled)
                        self.assertIn('Applying changes', str(details.query_one('#operation-status', Static).render()))
                        notify.assert_called_once_with('This job can no longer be cancelled. See its current step above.')
                    self.assertTrue(job.update.can_cancel)
                finally:
                    release.set()
                    await app.workers.wait_for_complete()
                self.assertEqual(job.result, 'Complete')
                self.assertFalse(button.display)

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
                    for shortcut in ('q', 'ctrl+q'):
                        await pilot.press(shortcut)
                        self.assertEqual(app.screen.heading, 'Jobs still running')
                        self.assertIn('won’t restore these jobs or their output', app.screen.message)
                        self.assertTrue(app.jobs_active())
                        await pilot.press('escape')
                    self.assertIs(app.screen, app.screen_stack[0])
                    beta = await begin(pilot, app, 'beta')
                    await wait_for(pilot, service.started['beta'].is_set)
                    await pilot.click('#job-hide')
                    self.assertEqual(str(app.query_one('#jobs', Button).label), 'Jobs… (2)')
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
                    self.assertIn('Update · alpha · Cancelled', app.screen.labels)
                    await pilot.press('enter')
                    self.assertIs(app.screen.job, beta)
                    await wait_for(pilot, lambda: 'beta: building the sandbox' in output(app.screen))
                    self.assertNotIn('alpha:', output(app.screen))
                    await pilot.press('escape')
                    selected = app.selected_name()
                    service.finish['beta'].set()
                    await wait_for(pilot, lambda: beta.result == 'Failed' and not app.job_completions)
                    self.assertEqual(str(app.query_one('#jobs', Button).label), 'Jobs… (0)')
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
