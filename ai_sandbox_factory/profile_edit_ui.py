"""Create an editable profile and review the exact files Factory will apply."""
from pathlib import Path

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from . import profile_edit
from .file_browser import PathField
from .tui_widgets import Bullet, Feedback


def consequences(record):
    notes = []
    # The runtime may change while review runs or this screen is open.
    notes.append(f'If {record["name"]} is running, its agent work, terminals and browser processes stop when its coding-agent container is replaced.')
    notes.append(f'If {record["name"]} is stopped, it stays stopped.')
    notes += ['Project files, private home, saved chats and sign-ins are kept.',
              'Existing profile services and their application data are kept.',
              'Packages installed manually in the container must be added to the Dockerfile to survive replacement.']
    return notes


class EditProfile(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, *, error='', destination=None):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record, self.error = record, error
        self.destination = destination or str(Path.home() / 'sandbox-profiles' / record['name'])

    def compose(self):
        yield Header()
        yield Label('Edit profile · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form spaced-form-fields'):
            editable = self.record['config'].get(profile_edit.EDIT)
            if editable:
                with Vertical(classes='setting'):
                    yield Label('Editable profile folder')
                    yield Static(editable['path'], markup=False)
                    yield Static('Inside the sandbox: ' + profile_edit.PATH, classes='setting-help', markup=False)
                yield Static('Ask your coding agent to edit the profile. Editing files does not change the running sandbox.', classes='section-intro')
                yield Static('Review changes to validate the files and see exactly what will be applied.', classes='section-intro')
            else:
                yield Static('Create a separate, editable copy of this sandbox’s saved profile. The original profile and project files stay separate.', classes='section-intro')
                with Vertical(classes='setting'):
                    yield Label('New profile folder on this computer')
                    yield PathField('profile-edit-folder', self.destination, title='Choose an empty profile folder')
                    yield Feedback(self.error, id='profile-edit-error', markup=False)
                    yield Static('Choose a new or empty folder outside the project. Your agent will edit it at ' + profile_edit.PATH + '.',
                                 classes='setting-help', markup=False)
                yield Static('Attaching the copy replaces the coding-agent container. You will review the consequences before continuing.', classes='section-intro')
            if editable:
                yield Feedback(self.error, id='profile-edit-error', markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='profile-edit-back')
            yield Button('Review changes…' if editable else 'Create editable copy…', id='profile-edit-go', variant='primary')
        yield Footer()

    def on_mount(self):
        self.query_one('#profile-edit-go' if self.record['config'].get(profile_edit.EDIT) else '#profile-edit-folder').focus()

    @on(Button.Pressed, '#profile-edit-back')
    def action_back(self):
        self.app.pop_screen()

    @on(Button.Pressed, '#profile-edit-go')
    def proceed(self):
        app, record = self.app, self.record
        name = record['name']
        if record['config'].get(profile_edit.EDIT):
            app.pop_screen()
            def resume(result):
                app.push_screen(ReviewProfile(record, result))
            app.start_task(name, 'Review profile changes · ' + name,
                           lambda log: app.service.review_profile(name), working='Validating profile files',
                           on_success=resume, followup_label='Review changes',
                           on_failure=lambda error: app.push_screen(EditProfile(record, error=error)), failure_label='Edit profile')
            return
        destination = self.query_one('#profile-edit-folder', Input).value.strip()
        if not destination:
            self.query_one('#profile-edit-error', Feedback).update('Enter a new or empty host folder for the editable profile.')
            self.query_one('#profile-edit-folder', Input).focus()
            return
        self.query_one('#profile-edit-error', Feedback).update('')
        from .sandbox_tui import Confirm
        message = f'Create an editable profile for {name}?'
        def confirmed(yes):
            if yes:
                app.pop_screen()
                app.start_settings_job(name, 'Create editable profile · ' + name,
                    lambda log, update: app.service.run_update(['profile-fork', name, '--directory', destination], log, update),
                    cooperative=True, success_message='Editable profile ready. Open the coding agent again to edit it.')
        app.push_screen(Confirm(message, 'Create editable copy', confirm_by_default=True, notes=consequences(record)), confirmed)


class ReviewProfile(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, review):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record, self.review = record, review

    def compose(self):
        yield Header()
        yield Label('Review profile changes · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form'):
            with Vertical(classes='setting'):
                yield Label('Review ID')
                yield Static(self.review['revision'], markup=False)
            with Vertical(classes='setting'):
                yield Label('Editable profile folder')
                yield Static(self.review['folder'], markup=False)
            if not self.review['changes']:
                yield Static('The edited profile matches the applied revision. There is nothing to apply.')
            else:
                with Vertical(classes='setting'):
                    yield Label('Changed files')
                    for item in self.review['changes']:
                        yield Bullet(item)
                if self.review['added_services']:
                    with Vertical(classes='setting'):
                        yield Label('New profile services')
                        for name in self.review['added_services']:
                            yield Bullet(name + ' — initializes separate application data')
                with Vertical(classes='setting'):
                    yield Label('When you apply')
                    yield Bullet('Factory builds images and checks new services before replacing the coding-agent container.')
                    for note in consequences(self.record):
                        yield Bullet(note)
                    yield Bullet('You can cancel preparation. Factory stops the active build or removes newly prepared service resources before reporting cancellation.')
                with Vertical(classes='setting'):
                    yield Label('File changes')
                    yield Static(self.review['diff'], markup=False, id='profile-edit-diff')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='profile-review-back')
            yield Button('Apply profile changes', id='profile-review-apply', variant='primary', disabled=not self.review['changes'])
        yield Footer()

    def on_mount(self):
        self.query_one('#profile-review-apply' if self.review['changes'] else '#profile-review-back').focus()

    @on(Button.Pressed, '#profile-review-back')
    def action_back(self):
        self.app.pop_screen()

    @on(Button.Pressed, '#profile-review-apply')
    def apply_changes(self):
        app, name = self.app, self.record['name']
        from .sandbox_tui import Confirm
        def confirmed(yes):
            if yes:
                app.pop_screen()
                app.start_settings_job(name, 'Apply profile changes · ' + name,
                    lambda log, update: app.service.run_update(['profile-apply', name, '--revision', self.review['revision']], log, update),
                    cooperative=True, success_message='Profile changes applied. Open the coding agent again to use them.')
        app.push_screen(Confirm(f'Apply the reviewed profile changes to {name}? Factory builds and checks new services before replacing the coding-agent container.',
                                'Apply profile changes',confirm_by_default=True,notes=consequences(self.record)),confirmed)
