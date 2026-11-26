"""Reopenable details and picker for Factory jobs."""
from typing import Optional

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Label, OptionList, RichLog, Static

from .sandbox_jobs import Job
from .tui_widgets import Feedback


class JobsMenu(ModalScreen[Optional[Job]]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, jobs):
        super().__init__()
        self.jobs = sorted(reversed(jobs), key=lambda job: job.result is not None)
        self.labels = []

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Jobs', classes='title')
            if self.jobs:
                yield OptionList(id='job-choices')
            else:
                yield Static('No jobs in this session.')
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='jobs-close')
                if self.jobs:
                    yield Button('Open details', id='jobs-open', variant='primary')

    def on_mount(self):
        if self.jobs:
            self.refresh_jobs()
            self.set_interval(0.2, self.refresh_jobs)

    def refresh_jobs(self):
        statuses = [job.status + (f' · {job.followup[0]}' if job.followup else '') for job in self.jobs]
        labels = [f'{job.title} · {status}' for job, status in zip(self.jobs, statuses)]
        if labels != self.labels:
            choices = self.query_one(OptionList)
            selected = choices.highlighted or 0
            choices.set_options(Text(f'{job.title}\n  {status}', overflow='ellipsis', no_wrap=True)
                                for job, status in zip(self.jobs, statuses))
            choices.highlighted = selected
            self.labels = labels

    @on(OptionList.OptionSelected, '#job-choices')
    def selected(self, event):
        event.stop()
        self.dismiss(self.jobs[event.option_index])

    @on(Button.Pressed, '#jobs-open')
    def open_details(self, event):
        event.stop()
        index = self.query_one(OptionList).highlighted
        if index is not None:
            self.dismiss(self.jobs[index])

    @on(Button.Pressed, '#jobs-close')
    def close(self, event):
        event.stop()
        self.action_close()

    def action_close(self):
        self.dismiss(None)


class JobDetails(Screen):
    BINDINGS = [Binding('escape', 'hide_details', 'Hide details'), Binding('escape', 'back', 'Back')]

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.position = 0
        self.timer = None
        self._finished = False

    @property
    def finished(self):
        return self.job.result is not None

    @property
    def succeeded(self):
        return self.job.result == 'Complete'

    def check_action(self, action, parameters):
        if action == 'hide_details':
            return not self.finished
        if action == 'back':
            return self.finished
        return True

    def compose(self):
        with Vertical(id='operation-body'):
            yield Static(self.job.title, id='operation-title', markup=False, classes='title')
            yield Static(self.job.status, id='operation-status', markup=False)
            yield Static('', id='operation-next', markup=False)
            yield Feedback('', id='job-log-error', markup=False)
            yield RichLog(id='operation-log', markup=False, wrap=True, min_width=1)
        with Horizontal(classes='wizard-actions job-actions'):
            yield Button('Hide details', id='job-hide')
            yield Button('Edit configuration', id='job-edit')
            yield Button('Retry creation', id='job-retry', variant='primary')
            yield Button('Continue setup', id='job-continue', variant='primary')
            if self.job.cancel_label:
                yield Button(self.job.cancel_label, id='job-cancel')
        yield Footer()

    def on_mount(self):
        self.query_one(RichLog).border_title = 'Output'
        self.query_one('#job-hide', Button).focus()
        self.timer = self.set_interval(0.1, self.refresh_job)
        self.refresh_job()

    def refresh_job(self):
        output = self.query_one(RichLog)
        lines, self.position = self.job.read(self.position)
        follow = output.is_vertical_scroll_end
        if lines:
            output.write(Text('\n'.join(lines)), expand=True, shrink=True, scroll_end=follow, animate=False)
        status = self.query_one('#operation-status', Static)
        status.update(self.job.status)
        status.set_class(self.job.result == 'Failed', 'failed')
        status.set_class(self.job.result == 'Complete', 'complete')
        notice = self.query_one('#operation-next', Static)
        help_text = self.job.notice
        if not self.finished:
            help_text = 'The job keeps running when you hide details. Reopen it from Jobs.'
            if self.job.applying:
                help_text = 'Cancellation is unavailable while applying changes. Hide details and reopen from Jobs.'
        notice.update(help_text)
        notice.display = bool(help_text)
        if self._finished != self.finished:
            self._finished = self.finished
            self.query_one('#job-hide', Button).label = 'Back' if self.finished else 'Hide details'
            self.refresh_bindings()
        self.query_one('#job-edit', Button).display = self.job.edit is not None
        self.query_one('#job-retry', Button).display = self.job.retry is not None
        followup = self.query_one('#job-continue', Button)
        followup.display = bool(self.job.followup)
        if self.job.followup:
            followup.label = self.job.followup[0]
        if self.job.cancel_label:
            self.query_one('#job-cancel', Button).display = self.job.result is None
            self.query_one('#job-cancel', Button).disabled = not self.job.can_cancel
        self.query_one('#job-log-error', Feedback).update(self.job.log_error or '')

    @on(Button.Pressed, '#job-hide')
    def hide(self, event):
        event.stop()
        self.action_hide_details()

    def action_hide_details(self):
        self.app.pop_screen()

    action_back = action_hide_details

    @on(Button.Pressed, '#job-edit')
    @on(Button.Pressed, '#job-retry')
    def retry_or_edit(self, event):
        event.stop()
        action = self.job.retry if event.button.id == 'job-retry' else self.job.edit
        if action:
            self.job.retry = self.job.edit = None
            self.app.pop_screen()
            action()

    @on(Button.Pressed, '#job-continue')
    def continue_setup(self, event):
        event.stop()
        self.app.resume_job(self.job)

    @on(Button.Pressed, '#job-cancel')
    def cancel(self, event):
        event.stop()
        if self.job.can_cancel:
            from .sandbox_tui import Confirm
            keep_label = 'Keep creating' if self.job.cancel_label == 'Cancel creation' else 'Keep updating'
            self.app.push_screen(Confirm(self.job.cancel_question, self.job.cancel_label,
                confirm_by_default=False, cancel_label=keep_label), self.confirm_cancel)

    def confirm_cancel(self, confirmed):
        if not confirmed:
            return
        if not self.job.cancel():
            self.notify('This job can no longer be cancelled. See its current step above.')
        if self.is_attached:
            self.refresh_job()
