"""Reopenable details and picker for dashboard jobs."""
from typing import Optional

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Label, OptionList, RichLog, Static

from .sandbox_jobs import RebuildJob
from .tui_widgets import Feedback


class JobsMenu(ModalScreen[Optional[RebuildJob]]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, jobs):
        super().__init__()
        self.jobs = list(reversed(jobs))
        self.labels = []

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Jobs')
            if self.jobs:
                yield OptionList(id='job-choices')
            else:
                yield Static('No jobs in this session.')
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='jobs-close')

    def on_mount(self):
        if self.jobs:
            self.refresh_jobs()
            self.set_interval(0.2, self.refresh_jobs)

    def refresh_jobs(self):
        labels = [f'{job.name} · {job.phase}' for job in self.jobs]
        if labels != self.labels:
            choices = self.query_one(OptionList)
            selected = choices.highlighted or 0
            choices.set_options(Text(label) for label in labels)
            choices.highlighted = selected
            self.labels = labels

    @on(OptionList.OptionSelected, '#job-choices')
    def selected(self, event):
        event.stop()
        self.dismiss(self.jobs[event.option_index])

    @on(Button.Pressed, '#jobs-close')
    def close(self, event):
        event.stop()
        self.action_close()

    def action_close(self):
        self.dismiss(None)


class JobDetails(Screen):
    BINDINGS = [Binding('escape', 'hide_details', 'Hide details', show=False)]

    def __init__(self, job):
        super().__init__()
        self.job = job
        self.position = 0
        self.timer = None

    def compose(self):
        with Vertical(id='operation-body'):
            yield Static('Update · ' + self.job.name, id='operation-title', markup=False, classes='title')
            yield Static(self.job.phase, id='operation-status', markup=False)
            yield Feedback('', id='job-log-error', markup=False)
            yield RichLog(id='operation-log', markup=False, wrap=True, min_width=1)
        with Horizontal(classes='wizard-actions job-actions'):
            yield Button('Hide details', id='job-hide')
            yield Button('Cancel update', id='job-cancel')
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
        phase = self.job.phase
        status.update(phase)
        status.set_class(phase == 'Failed', 'failed')
        status.set_class(phase == 'Complete', 'complete')
        self.query_one('#job-hide', Button).label = 'Back' if self.job.result else 'Hide details'
        self.query_one('#job-cancel', Button).display = self.job.result is None
        self.query_one('#job-cancel', Button).disabled = not self.job.update.can_cancel
        self.query_one('#job-log-error', Feedback).update(self.job.log_error or '')
        if self.job.result and not lines:
            self.timer.stop()

    @on(Button.Pressed, '#job-hide')
    def hide(self, event):
        event.stop()
        self.action_hide_details()

    def action_hide_details(self):
        self.app.pop_screen()

    @on(Button.Pressed, '#job-cancel')
    def cancel(self, event):
        event.stop()
        if self.job.update.can_cancel:
            from .sandbox_tui import Confirm
            self.app.push_screen(Confirm(f'Cancel the update for {self.job.name}? '
                'The sandbox will keep its current settings and containers.', 'Cancel update',
                confirm_by_default=False, cancel_label='Keep updating'), self.confirm_cancel)

    def confirm_cancel(self, confirmed):
        if confirmed:
            self.job.cancel()
