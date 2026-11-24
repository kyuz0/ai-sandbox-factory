"""Edit the AWS grant in Factory. Only the host uses the selected profile."""

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from . import aws_access, aws_profiles
from .tui_widgets import Checkbox, EntryDialog, Feedback


class AWSAccessDialog(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, current=None, *, duplicate=False):
        super().__init__()
        self.current = current or {}
        self.original = self.current if self.current.get('profile_id') and not duplicate else None
        self.title_text = 'Duplicate AWS access profile' if duplicate else 'Edit AWS access profile' if self.original else 'New AWS access profile'
        self.initial_name = self.current.get('profile_name', '')
        if duplicate and self.initial_name:
            self.initial_name += ' copy'

    def on_mount(self):
        self.set_focus(self.query_one('#aws-name', Input), scroll_visible=False)
        self.call_after_refresh(self.query_one('#aws-fields').scroll_home, animate=False)

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(self.title_text)
            with VerticalScroll(id='aws-fields', classes='spaced-form-fields'):
                for key, title, example in [
                    ('name', 'Profile name', 'Research'),
                    ('profile', 'AWS profile on this computer', 'my-work-profile'),
                    ('role', 'IAM role ARN', 'arn:aws:iam::123456789012:role/Sandbox'),
                    ('region', 'AWS region', 'eu-west-1'),
                ]:
                    with Vertical(classes='setting'):
                        yield Label(title)
                        yield Input(self.initial_name if key == 'name' else self.current.get(key, ''), placeholder=example, id='aws-' + key)
                        yield Feedback('', id='aws-' + key + '-error', markup=False)
                with Vertical(classes='setting'):
                    yield Label('Allowed actions and resources')
                    lines = []
                    for rule in self.current.get('grants', []):
                        for action in aws_access.permission_values(rule['Action']):
                            lines.extend(action + ' ' + resource for resource in aws_access.permission_values(rule['Resource']))
                    yield TextArea('\n'.join(lines), id='aws-grants')
                    yield Static('Enter one AWS action and resource per line, such as ec2:DescribeRegions *.',
                                 classes='setting-help')
                    yield Feedback('', id='aws-grants-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='aws-cancel')
                yield Button('Save', id='aws-save', variant='primary')

    @on(Button.Pressed, '#aws-cancel')
    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, '#aws-save')
    def save(self):
        for message in self.query(Feedback):
            message.update('')
        try:
            value = {key:self.query_one('#aws-' + key, Input).value.strip() for key in ('profile','role','region')}
            grants = []
            for line in self.query_one('#aws-grants', TextArea).text.splitlines():
                if line.strip():
                    fields = line.split(maxsplit=1)
                    if len(fields) != 2:
                        raise ValueError('Enter an action and a resource on each line.')
                    grants.append({'Action':fields[0], 'Resource':fields[1]})
            saved = aws_profiles.save(self.query_one('#aws-name', Input).value, {**value, 'grants': grants},
                                      original=self.original)
            self.dismiss(saved)
        except (ValueError, OSError) as exc:
            field = self.query_one('#aws-' + getattr(exc, 'field', 'grants'))
            self.query_one('#' + field.id + '-error', Feedback).update(str(exc))
            self.set_focus(field, scroll_visible=False)
            self.call_after_refresh(field.parent.scroll_visible, top=True, animate=False)


class AWSProfileTargets(EntryDialog):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, profile, targets):
        super().__init__()
        self.profile = profile
        self.targets = targets

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Apply profile update')
            with VerticalScroll(classes='aws-profile-body'):
                yield Static(f'Choose which sandboxes receive “{self.profile["profile_name"]}”.',
                             markup=False, classes='section-intro')
                for name in self.targets:
                    yield Checkbox(name, name=name, classes='aws-target')
            with Horizontal(classes='dialog-actions'):
                yield Button('Skip', id='aws-skip')
                yield Button('Apply to selected', id='aws-apply-selected', disabled=True, variant='primary')

    def on_mount(self):
        self.query_one(Checkbox).focus()

    @on(Checkbox.Changed)
    def target_changed(self):
        self.query_one('#aws-apply-selected', Button).disabled = not any(box.value for box in self.query(Checkbox))

    @on(Button.Pressed, '#aws-apply-selected')
    def apply(self):
        self.dismiss({box.name: self.targets[box.name] for box in self.query(Checkbox) if box.value})

    @on(Button.Pressed, '#aws-skip')
    def action_close(self):
        self.dismiss({})


class AWSProfileChoices(Vertical):
    """Shared by the library and the per-sandbox profile picker."""
    def __init__(self, current=None):
        super().__init__(classes='setting')
        self.current = current or {}
        self.profiles = {}

    def compose(self):
        yield Label('AWS access profile')
        yield Select([], prompt='Choose a profile', id='aws-choice')
        yield Static('', markup=False, classes='setting-help', id='aws-preview')
        with Horizontal(classes='buttons aws-profile-actions'):
            yield Button('New…', id='aws-new')
            yield Button('Edit…', id='aws-edit')
            yield Button('Duplicate…', id='aws-copy')
        yield Button('Apply saved update…', id='aws-rollout')
        yield Feedback('', id='aws-library-error', markup=False)

    def on_mount(self):
        self.reload(self.current.get('profile_id'))

    def reload(self, key=None):
        error = self.query_one(Feedback)
        try:
            self.profiles = aws_profiles.entries()
            error.update('')
        except (OSError, ValueError) as exc:
            self.profiles = {}
            error.update(str(exc))
        choice = self.query_one(Select)
        choice.set_options([(v['profile_name'], k) for k, v in
                            sorted(self.profiles.items(), key=lambda item: item[1]['profile_name'].casefold())])
        choice.disabled = not self.profiles
        if key in self.profiles:
            choice.value = key
        self.show_profile()

    @on(Select.Changed, '#aws-choice')
    def show_profile(self):
        value = self.profiles.get(self.query_one(Select).value)
        self.query_one('#aws-edit', Button).disabled = not value
        self.query_one('#aws-copy', Button).disabled = not (value or self.current)
        rollout = self.query_one('#aws-rollout', Button)
        try:
            rollout.display = bool(value and aws_profiles.pending(self.app.state_root, value))
        except (OSError, ValueError) as exc:
            self.query_one(Feedback).update(f'Could not read sandbox assignments. {exc}')
            rollout.display = False
        if value:
            rules = []
            for rule in value['grants']:
                actions = ', '.join(aws_access.permission_values(rule['Action']))
                resources = ', '.join('All resources' if resource == '*' else resource
                                      for resource in aws_access.permission_values(rule['Resource']))
                rules.append(actions + ' · ' + resources)
            preview = '\n'.join([value['profile'] + ' · ' + value['region'], value['role'],
                                 '', 'Allowed actions and resources', *rules])
        else:
            preview = '' if self.profiles else 'No saved AWS access profiles.'
        self.query_one('#aws-preview', Static).update(preview)
        if isinstance(self.screen, AWSProfilePicker):
            self.screen.query_one('#aws-assign', Button).disabled = not value or value == self.screen.current

    @on(Button.Pressed, '.aws-profile-actions Button')
    def edit(self, event):
        event.stop()
        value = self.profiles.get(self.query_one(Select).value)
        editing = event.button.id == 'aws-edit'
        duplicate = event.button.id == 'aws-copy'
        initial = None
        if editing:
            initial = value
        elif duplicate:
            initial = value or self.current

        def saved(profile):
            if profile is None:
                return
            self.reload(profile['profile_id'])
            if editing:
                self.choose_targets(profile)

        self.app.push_screen(AWSAccessDialog(initial, duplicate=duplicate), saved)

    @on(Button.Pressed, '#aws-rollout')
    def rollout(self, event):
        event.stop()
        self.choose_targets(self.profiles[self.query_one(Select).value])

    def choose_targets(self, profile):
        try:
            targets = aws_profiles.pending(self.app.state_root, profile)
        except (OSError, ValueError) as exc:
            self.query_one(Feedback).update(f'Profile saved. Could not read sandbox assignments. {exc}')
            return
        if not targets:
            self.notify('Profile saved.')
            return

        def chosen(selected):
            if selected:
                service = self.app.service
                def apply(log):
                    aws_profiles.apply_selected(service, profile, selected, log)
                    log('Selected sandboxes updated.')
                def refreshed(_):
                    if self.is_mounted:
                        self.show_profile()
                self.app.start_task('AWS access profiles', 'Update AWS access', apply,
                                    working='Updating AWS access', targets=set(selected),
                                    on_success=refreshed, followup_label='Return to AWS profiles')

        self.app.push_screen(AWSProfileTargets(profile, targets), chosen)


class AWSProfilePicker(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, current=None, *, sandbox_name=None):
        super().__init__(classes='has-assignment' if current else None)
        self.current = current or {}
        self.sandbox_name = sandbox_name

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('AWS access')
            if self.current:
                yield Static('Assigned to this sandbox · ' + profile_label(self.current),
                             classes='section-intro', markup=False, id='aws-assigned')
            with VerticalScroll(classes='aws-profile-body'):
                yield AWSProfileChoices(self.current)
            with Horizontal(classes='dialog-actions'):
                if self.current:
                    yield Button('Disconnect…' if self.sandbox_name else 'Remove', id='aws-disconnect')
                yield Button('Cancel', id='aws-cancel')
                yield Button('Use profile', id='aws-assign', variant='primary', disabled=True)

    def on_mount(self):
        choices = self.query_one(AWSProfileChoices)
        self.set_focus(choices.query_one(Select) if choices.profiles else choices.query_one('#aws-new'),
                       scroll_visible=False)
        self.call_after_refresh(self.query_one('.aws-profile-body').scroll_home, animate=False)

    @on(Button.Pressed, '#aws-cancel')
    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, '#aws-disconnect')
    def disconnect(self):
        if not self.sandbox_name:
            self.dismiss({})
            return
        from .sandbox_tui import Confirm
        def disconnect_if_confirmed(confirmed):
            if confirmed:
                self.dismiss({})

        self.app.push_screen(Confirm(
            'This sandbox will lose AWS access. You can use the saved profile again.',
            'Disconnect', title='Disconnect AWS from ' + self.sandbox_name + '?', confirm_by_default=False),
            disconnect_if_confirmed)

    def on_screen_resume(self):
        if self.sandbox_name and self.is_mounted and self.query('#aws-assigned'):
            try:
                self.current = self.app.service.config(self.sandbox_name).get('aws_access', {})
            except (OSError, ValueError) as exc:
                self.query_one(Feedback).update(str(exc))
                return
            self.query_one('#aws-assigned', Static).update('Assigned to this sandbox · ' + profile_label(self.current))
            self.query_one(AWSProfileChoices).show_profile()

    @on(Button.Pressed, '#aws-assign')
    def assign(self):
        choices = self.query_one(AWSProfileChoices)
        self.dismiss(choices.profiles[choices.query_one(Select).value])


def profile_label(current):
    return current.get('profile_name') or current.get('profile', 'Off')
