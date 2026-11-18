"""Model credential selection shared by hosted and plugin providers."""
import asyncio
from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from . import inference, provider_credentials
from .tui_widgets import Checkbox, Feedback


def key_caption(label=None):
    return 'API key · ' + (label or 'None assigned')


def assigned_key_label(status, route):
    assignment = status.get('assignments', {}).get(route)
    return assignment['label'] if assignment else 'Unnamed key' if route in status.get('configured', ()) else None


def assigned_key_caption(status, route):
    return key_caption(assigned_key_label(status, route))


def choice_key_label(binding, choice):
    if not choice:
        return None
    if ref := choice.get('ref'):
        try:
            label = provider_credentials.entries(binding).get(ref, {}).get('label')
        except ValueError:
            label = None
        return label or 'Saved key'
    return choice.get('label') or 'New key'


def choice_key_caption(binding, choice):
    return key_caption(choice_key_label(binding, choice))


class CredentialFields(Vertical):
    DEFAULT_CSS = '''
    CredentialFields, CredentialFields Vertical { height: auto; }
    CredentialFields #credential-remember { margin: 0 0 1 0; }
    '''

    def __init__(self, key_id, *, binding=None, choice=None):
        super().__init__(id=key_id + '-fields')
        self.key_id = key_id
        self.binding = binding
        self.choice = choice or {}
        self.saved = {}

    def compose(self):
        with Vertical(id='credential-saved'):
            yield Label('Choose a key')
            yield Select([('Enter a new key', 'new')], allow_blank=False, id='saved-key')
            yield Button('Forget saved key…', id='forget-key')
        with Vertical(id='credential-new'):
            yield Label('API key')
            yield Input(self.choice.get('key', ''), password=True, id=self.key_id, classes='credential-key-input')
            yield Checkbox('Remember on this computer', value=bool(self.choice.get('label')), id='remember-key')
            with Vertical(id='credential-remember'):
                yield Label('Save as', id='replace-key-title')
                yield Select([], prompt='Save as a new key', id='replace-key')
                yield Label('Key name', id='key-label-title')
                yield Input(self.choice.get('label', ''), id='key-label', placeholder='Work')
                yield Static('Other sandboxes keep their current keys.', id='replace-key-help', classes='setting-help')
        yield Feedback('', id='credential-error', markup=False)

    def on_mount(self):
        self.refresh_choices(self.choice.get('ref'), self.choice.get('replace'))

    def set_binding(self, binding):
        if binding == self.binding:
            return
        self.binding = binding
        self.refresh_choices()

    def refresh_choices(self, selected=None, replacement=None):
        try:
            self.saved = provider_credentials.entries(self.binding) if self.binding else {}
        except ValueError as error:
            self.saved = {}
            self.query_one('#credential-error', Feedback).update(str(error))
        choices = [(Text(row['label']), ref) for ref, row in self.saved.items()]
        saved = self.query_one('#saved-key', Select)
        saved.set_options([('Enter a new key', 'new'), *choices])
        saved.value = selected if selected in self.saved else 'new'
        replace = self.query_one('#replace-key', Select)
        replace.set_options([(Text('Replace ' + row['label']), ref) for ref, row in self.saved.items()])
        replace.value = replacement if replacement in self.saved else Select.NULL
        self.query_one('#credential-saved').display = bool(choices)
        self.sync()

    @on(Select.Changed, '#saved-key')
    @on(Select.Changed, '#replace-key')
    @on(Checkbox.Changed, '#remember-key')
    def sync(self):
        have_selection = self.query_one('#saved-key', Select).value in self.saved
        remember = self.query_one('#remember-key', Checkbox)
        remember.display = bool(self.binding)
        self.query_one('#credential-new').display = not have_selection
        self.query_one('#forget-key', Button).display = have_selection
        self.query_one('#credential-remember').display = bool(self.binding and remember.value)
        replace = self.query_one('#replace-key', Select)
        replace.display = bool(self.saved)
        self.query_one('#replace-key-title').display = bool(self.saved)
        for selector in ('#key-label', '#key-label-title'):
            self.query_one(selector).display = replace.value is Select.NULL
        self.query_one('#replace-key-help').display = replace.value is not Select.NULL

    def focus_key(self):
        self.query_one('#' + self.key_id, Input).focus()

    def collect(self):
        ref = self.query_one('#saved-key', Select).value
        if self.binding and ref in self.saved:
            return {'ref': ref}
        key = self.query_one('#' + self.key_id, Input).value.strip()
        remember = self.binding and self.query_one('#remember-key', Checkbox).value
        if self.binding and (key or remember):
            try:
                provider_credentials.validate_key(key)
            except ValueError:
                self.focus_key()
                raise
        if not remember:
            return {'key': key} if key else {}
        replace = self.query_one('#replace-key', Select).value
        name = self.query_one('#key-label', Input)
        label = (self.saved[replace]['label'] if replace in self.saved
                 else name.value.strip())
        if not label:
            name.focus()
            raise ValueError('Enter a name for the saved key.')
        return {'key': key, 'label': label, **({'replace': replace} if replace in self.saved else {})}

    @on(Button.Pressed, '#forget-key')
    def confirm_forget(self, event):
        event.stop()
        ref = self.query_one('#saved-key', Select).value
        if ref in self.saved:
            from .sandbox_tui import Confirm
            label = self.saved[ref]['label']
            self.app.push_screen(Confirm(f'Forget {label} on this computer?\nExisting sandbox connections keep their keys.', 'Forget'),
                                 lambda yes: self.forget(ref) if yes else None)

    @work(exclusive=True)
    async def forget(self, ref):
        try:
            await asyncio.to_thread(provider_credentials.forget, ref)
            self.refresh_choices()
        except (ValueError, OSError) as error:
            self.query_one('#credential-error', Feedback).update(str(error))


class CredentialDialog(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = '''
    CredentialDialog { align: center middle; background: #000000 70%; }
    #credential-dialog { width: 72; max-width: 94%; height: auto; max-height: 95%; padding: 1 2;
                         background: #34383d; border: solid #f2b544; }
    #credential-dialog-fields { height: auto; min-height: 1; }
    #credential-actions { height: auto; }
    #disconnect-key { dock: left; }
    '''

    def __init__(self, provider, binding, *, choice=None, active=False):
        super().__init__()
        self.provider, self.binding, self.choice, self.active = provider, binding, choice, active

    def compose(self):
        with Vertical(id='credential-dialog', classes='key-dialog'):
            yield Label('API key · ' + self.provider, classes='title')
            with VerticalScroll(id='credential-dialog-fields', classes='key-dialog-fields'):
                yield CredentialFields('managed-key', binding=self.binding, choice=self.choice)
            yield Feedback('', id='credential-dialog-error', markup=False)
            with Horizontal(classes='buttons dialog-actions', id='credential-actions'):
                if self.active:
                    yield Button('Disconnect key…', id='disconnect-key')
                yield Button('Cancel', id='credential-cancel')
                yield Button('Use key', id='credential-apply', variant='primary')

    def action_cancel(self):
        self.dismiss(None)

    @on(Select.Changed, '#saved-key')
    def fit_saved_key(self, event):
        fields = self.query_one(CredentialFields)
        self.query_one('#credential-dialog').set_class(event.value in fields.saved, '-saved-key-selected')

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'credential-cancel':
            self.action_cancel()
        elif event.button.id == 'credential-apply':
            try:
                fields = self.query_one(CredentialFields)
                choice = fields.collect()
                if not choice:
                    fields.focus_key()
                    raise ValueError('Enter an API key.')
                self.dismiss(choice)
            except ValueError as error:
                self.query_one('#credential-dialog-error', Feedback).update(str(error))
        elif event.button.id == 'disconnect-key':
            from .sandbox_tui import Confirm
            self.app.push_screen(Confirm(f'Disconnect {self.provider} from this sandbox?', 'Disconnect'),
                                 lambda yes: self.dismiss({'disconnect': True}) if yes else None)


LOCAL_KEYS_LABEL = 'Local server keys…'


class LocalKeysDialog(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = '''
    LocalKeysDialog { align: center middle; background: #000000 70%; }
    #local-keys { width: 72; max-width: 94%; height: auto; padding: 1 2;
                  background: #34383d; border: solid #f2b544; }
    #local-keys .buttons { margin: 0; }
    #local-keys .dialog-actions { margin: 1 0 0 0; }
    '''

    def __init__(self, ports, *, draft=None, status=None):
        super().__init__()
        self.ports = ports
        self.choices = dict(draft or {})
        self.editing = draft is None
        self.status = status or {}

    def compose(self):
        with Vertical(id='local-keys'):
            yield Label('Local server keys', classes='title')
            yield Label('Server port · API key')
            yield Select(self.port_options(), value=self.ports[0],
                         allow_blank=False, id='local-key-port')
            with Vertical(classes='setting'):
                with Horizontal(classes='buttons'):
                    yield Button('Choose key…', id='local-key-choose')
                    yield Button('Disconnect key…' if self.editing else 'Clear key', id='local-key-clear')
                yield Static('Changes apply to the running sandbox.' if self.editing else
                             'Keys take effect when you create the sandbox.', classes='setting-help')
            with Horizontal(classes='dialog-actions'):
                if not self.editing:
                    yield Button('Cancel', id='local-key-cancel')
                yield Button('Close' if self.editing else 'Done', id='local-key-done', variant='primary')

    def port_options(self):
        return [(Text(f'{port} · {self.port_key_label(port) or "None assigned"}'), port)
                for port in self.ports]

    def port_key_label(self, port):
        if self.editing:
            return assigned_key_label(self.status, inference.local_route_id(port))
        return choice_key_label(self.binding(port), self.choices.get(port))

    def action_cancel(self):
        self.dismiss(None)

    def on_mount(self):
        self.show_port()

    @on(Select.Changed, '#local-key-port')
    def show_port(self):
        port = self.query_one('#local-key-port', Select).value
        if port is Select.NULL:
            return
        active = bool(self.port_key_label(port))
        self.query_one('#local-key-choose', Button).label = 'Change key…' if active else 'Choose key…'
        self.query_one('#local-key-clear', Button).display = active

    def binding(self, port):
        return provider_credentials.scope('local', inference.local_route(port))

    def selected(self, port, choice):
        if choice is None:
            return
        if self.editing:
            self.dismiss((port, choice))
            return
        if choice.get('disconnect'):
            self.choices.pop(port, None)
        else:
            self.choices[port] = choice
        picker = self.query_one('#local-key-port', Select)
        picker.set_options(self.port_options())
        picker.value = port
        self.show_port()
        self.query_one('#local-key-choose', Button).focus()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        port = self.query_one('#local-key-port', Select).value
        if event.button.id == 'local-key-done':
            self.dismiss(None if self.editing else self.choices)
        elif event.button.id == 'local-key-cancel':
            self.action_cancel()
        elif event.button.id == 'local-key-clear':
            if self.editing:
                from .sandbox_tui import Confirm
                self.app.push_screen(Confirm(f'Disconnect the API key for port {port}?', 'Disconnect'),
                                     lambda yes: self.selected(port, {'disconnect': True}) if yes else None)
            else:
                self.selected(port, {'disconnect': True})
        else:
            self.app.push_screen(CredentialDialog(f'Local server · port {port}', self.binding(port),
                choice=self.choices.get(port)), lambda choice: self.selected(port, choice))
