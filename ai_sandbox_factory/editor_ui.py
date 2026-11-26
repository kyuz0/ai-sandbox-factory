"""Shared editor controls for sandbox creation and settings."""
import secrets

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Collapsible, Footer, Header, Input, Label, OptionList, Select, Static

from . import editor_settings
from .file_browser import PathField
from .settings_catalog import CATEGORIES
from .tui_widgets import Checkbox, Feedback


class EditorFieldError(ValueError):
    """The field already displays this validation error beside its control."""


class EditorFields(Vertical):
    DEFAULT_CSS = '''
    EditorFields { max-width: 80; }
    EditorFields, #editor-connection, #editor-password-fields, #editor-tls-fields { height: auto; }
    #editor-password-fields { margin: 1 0 0 2; }
    #editor-show-password { margin-top: 1; }
    #editor-generate { background: #424850; color: #f1f3f5; }
    #editor-mode { max-width: 40; }
    #editor-port { width: 12; }
    #editor-https-help { margin: 0 0 1 0; }
    EditorFields > .setting { margin: 0 0 1 0; }
    EditorFields > .last-editor-field { margin: 0; }
    '''

    def __init__(self, value=None, *, connection_settings=True, agent='pi'):
        super().__init__(classes='spaced-form-fields')
        self.value = editor_settings.validate(value)
        self.connection_settings = connection_settings
        self.agent = agent
        self.modes = editor_settings.modes(agent)

    def compose(self):
        with Vertical(classes='setting'):
            yield Label('Client')
            yield Select([(title, key) for key, title in self.modes.items()],
                         value=self.value['mode'], allow_blank=False, id='editor-mode')
            yield Static('', id='editor-description', classes='setting-help')
        with Vertical(classes='setting', id='editor-upload-fields'):
            yield Checkbox('Allow file uploads', value=self.value['allow_uploads'], id='editor-uploads')
        if not self.connection_settings:
            return
        with Collapsible(title='Browser connection', collapsed=True, id='editor-connection'):
            with Vertical(classes='setting'):
                yield Label('Listen address')
                yield Input(self.value['address'], id='editor-address', placeholder='Automatic (this computer only)')
                yield Static('A custom address requires a password.', classes='setting-help')
            with Vertical(classes='setting'):
                yield Label('Port · leave empty to choose automatically')
                yield Input(str(self.value['port']) if self.value['port'] else '', id='editor-port')
                yield Feedback('', id='editor-port-error', classes='error', markup=False)
            with Vertical(classes='setting'):
                yield Checkbox('Require a password', value=bool(self.value['password_hash']), id='editor-auth')
                with Vertical(id='editor-password-fields'):
                    yield Label('Password · leave empty to keep the saved password' if self.value['password_hash'] else 'Password')
                    with Horizontal(classes='path-row'):
                        yield Input(password=True, id='editor-password')
                        yield Button('Generate', id='editor-generate')
                    yield Checkbox('Show password', id='editor-show-password')
            with Vertical(id='editor-tls-fields'):
                yield Static('Access from another computer also requires HTTPS.', id='editor-https-help', classes='setting-help')
                with Vertical(classes='setting'):
                    yield Label('HTTPS certificate file on this computer')
                    yield PathField('editor-certificate', self.value.get('certificate', ''), title='Choose HTTPS certificate', mode='file')
                with Vertical(classes='setting'):
                    yield Label('HTTPS private key file on this computer')
                    yield PathField('editor-private-key', self.value.get('private_key', ''), title='Choose HTTPS private key', mode='file')
                    yield Static('Use a certificate trusted by the connecting browser for the listen address.', classes='setting-help')

    def on_mount(self):
        self.sync()

    @on(Select.Changed, '#editor-mode')
    @on(Checkbox.Changed, '#editor-auth')
    @on(Input.Changed, '#editor-address')
    @on(Input.Changed, '#editor-port')
    def sync(self):
        import ipaddress
        mode = self.query_one('#editor-mode', Select).value
        from .primary_workspace import AGENTS
        name = AGENTS[self.agent]
        self.query_one('#editor-description', Static).update({
            'browser': ('Open VS Code and FrostPi in your browser.' if self.agent == 'pi' else
                        f'Open VS Code with {name} in your browser.'),
            'pi': f'Run {name} in this terminal. Exit {name} to return here.',
        }[mode])
        for section in self.screen.query('#editor-section'):
            section.title = CATEGORIES['editor-settings'].label + ' · ' + self.modes[mode]
        self.query_one('#editor-upload-fields').display = mode == 'browser'
        if not self.connection_settings:
            self.space_fields()
            return
        self.query_one('#editor-connection').display = mode == 'browser'
        self.query_one('#editor-password-fields').display = self.query_one('#editor-auth', Checkbox).value
        try:
            remote = not ipaddress.ip_address(self.query_one('#editor-address', Input).value or '127.0.0.1').is_loopback
        except ValueError:
            remote = True
        self.query_one('#editor-tls-fields').display = remote
        address = self.query_one('#editor-address', Input).value.strip()
        password = self.query_one('#editor-auth', Checkbox).value
        self.query_one('#editor-connection', Collapsible).title = ('Browser connection · ' +
            ('custom address' if address else 'localhost') + (' · password' if password else ' · no password'))
        self.space_fields()

    def space_fields(self):
        visible = [child for child in self.children if child.display]
        for child in self.children:
            child.set_class(bool(visible) and child is visible[-1], 'last-editor-field')

    def control_values(self):
        return tuple((field.id, field.value) for field in self.query('Input, Checkbox, Select')
                     if field.id != 'editor-show-password')

    @on(Checkbox.Changed, '#editor-show-password')
    def show_password(self, event):
        self.query_one('#editor-password', Input).password = not event.value

    @on(Input.Changed, '#editor-port')
    def clear_port_error(self):
        self.query_one('#editor-port', Input).remove_class('-invalid')
        self.query_one('#editor-port-error', Feedback).update('')

    @on(Button.Pressed, '#editor-generate')
    def generate(self, event):
        event.stop()
        self.query_one('#editor-password', Input).value = secrets.token_urlsafe(24)
        self.query_one('#editor-show-password', Checkbox).value = True

    def collect(self):
        mode = self.query_one('#editor-mode', Select).value
        choices = dict(mode=mode, allow_uploads=self.query_one('#editor-uploads', Checkbox).value)
        if not self.connection_settings:
            return editor_settings.validate(choices)
        if mode != 'browser':
            return editor_settings.validate({**self.value, **choices})
        def text(name): return self.query_one('#editor-' + name, Input).value.strip()
        port_input = self.query_one('#editor-port', Input)
        port_error = self.query_one('#editor-port-error', Feedback)
        port_input.remove_class('-invalid')
        port_error.update('')
        try:
            port = int(text('port') or '0')
            if not 0 <= port <= 65535:
                raise ValueError
        except ValueError:
            message = 'Enter a port from 1 to 65535.'
            port_error.update(message)
            port_input.add_class('-invalid')
            port_input.focus()
            self.call_after_refresh(port_error.scroll_visible, animate=False)
            raise EditorFieldError(message) from None
        digest = ''
        if self.query_one('#editor-auth', Checkbox).value:
            password = self.query_one('#editor-password', Input).value
            digest = editor_settings.password_hash(password) if password else self.value['password_hash']
            if not digest:
                raise ValueError('Enter or generate an editor password')
        value = editor_settings.validate(dict(**choices, address=text('address'), port=port, password_hash=digest,
            certificate=text('certificate'), private_key=text('private-key')))
        editor_settings.tls_context(value)
        return value


class EditorScreen(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, name):
        super().__init__()
        self.sandbox_name = name

    def compose(self):
        yield Header()
        with VerticalScroll(classes='form'):
            yield Label(CATEGORIES['editor-settings'].label + ' · ' + self.sandbox_name, classes='title')
            state = self.app.state_root / self.sandbox_name
            from .primary_workspace import agent
            import json
            config = json.loads((state / 'config.json').read_text()) if (state / 'config.json').exists() else {}
            self.original_settings = editor_settings.load(state)
            yield EditorFields(self.original_settings, agent=agent(config))
            from . import browser_editor
            self.editor_running = browser_editor.active(state)
            yield Static('• Saving changes closes the editor and interrupts any active reply.\n• Saved chats are kept.',
                         id='editor-restart-notice', classes='setting-help')
            yield Feedback('', id='editor-error', classes='error', markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='editor-back')
            yield Button('Save', id='editor-save', variant='primary')
        yield Footer()

    def on_mount(self):
        self.original_controls = self.query_one(EditorFields).control_values()
        self.query_one('#editor-restart-notice').display = False
        self.query_one('#editor-mode', Select).focus()

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    def changes(self):
        if hasattr(self, 'original_controls'):
            self.query_one('#editor-restart-notice').display = (
                self.editor_running and self.query_one(EditorFields).control_values() != self.original_controls)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'editor-back':
            self.action_back()
        elif event.button.id == 'editor-save':
            try:
                value = self.query_one(EditorFields).collect()
                changes = {key: setting for key, setting in value.items()
                           if setting != self.original_settings[key]}
                if not changes:
                    self.action_back()
                    return
                active = self.app.active_job(self.sandbox_name)
                if active:
                    self.app.show_job(active)
                    return
                def apply(log):
                    result = app.service.save_editor(self.sandbox_name, changes, log)
                    log('Editor settings saved.')
                    if result['editor_stopped']:
                        log('Choose Open, then select your saved chat from the history.')
                app = self.app
                app.pop_screen()
                app.start_task(self.sandbox_name, 'Save editor settings', apply,
                               working='Saving editor settings')
            except (ValueError, OSError) as error:
                feedback = self.query_one('#editor-error', Feedback)
                feedback.update('' if isinstance(error, EditorFieldError) else str(error))
                if not isinstance(error, EditorFieldError):
                    self.call_after_refresh(feedback.scroll_visible, animate=False)

    def action_back(self):
        self.app.pop_screen()


class OpenWith(ModalScreen):
    BINDINGS = [('escape', 'back', 'Back')]
    DEFAULT_CSS = '''
    OpenWith { align: center middle; background: #000000 70%; }
    OpenWith #editor-choices { border: none; padding: 0; margin: 1 0 0 0; }
    OpenWith #editor-choices > .option-list--option { padding: 0; }
    '''

    def __init__(self, *, agent='pi'):
        super().__init__()
        self.modes = editor_settings.modes(agent)

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Open with', classes='title')
            yield OptionList(*self.modes.values(), id='editor-choices')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='editor-cancel')

    @on(OptionList.OptionSelected)
    def selected(self, event):
        self.dismiss(list(self.modes)[event.option_index])

    @on(Button.Pressed)
    def cancel(self, event):
        event.stop()
        self.dismiss(None)

    def action_back(self):
        self.dismiss(None)
