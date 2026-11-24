"""Repository SSH setup and public-key display; permissions remain server-side."""
from pathlib import Path

from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, Select, Static, TextArea

from . import host_platform, sandbox_ssh, git_settings
from .file_browser import PathField
from .tui_widgets import EntryDialog, Feedback, Checkbox


class GitIdentityFields(Vertical):
    DEFAULT_CSS = '''
    GitIdentityFields { height: auto; }
    GitIdentityFields #git-identity-fields { height: auto; margin: 0 0 0 4; }
    GitIdentityFields #git-identity-fields.-identity-disabled { opacity: 50%; }
    '''

    def __init__(self, value=None, defaults=False):
        super().__init__()
        self.value = value
        self.defaults = defaults

    def compose(self):
        with Vertical(classes='setting'):
            yield Checkbox('Set Git commit identity', value=self.value is not None, id='git-identity-enabled')
            scope = 'new sandboxes' if self.defaults else 'this sandbox'
            yield Static(f'Sets the global Git author for {scope} at startup. Each repository can override it. '
                         'SSH access uses its own key.', classes='setting-help checkbox-help')
        with Vertical(id='git-identity-fields'):
            with Vertical(classes='setting'):
                yield Label('Commit author name')
                yield Input((self.value or {}).get('name', ''), id='git-author-name')
            with Vertical(classes='setting'):
                yield Label('Commit author email')
                yield Input((self.value or {}).get('email', ''), id='git-author-email')
                yield Static('Use an address you want recorded in Git history.', classes='setting-help')
                yield Feedback('', id='git-identity-error', markup=False)

    def on_mount(self):
        self.sync_fields()

    @on(Checkbox.Changed, '#git-identity-enabled')
    def sync_fields(self):
        enabled = self.query_one('#git-identity-enabled', Checkbox).value
        for field in self.query(Input):
            field.disabled = not enabled
        self.query_one('#git-identity-fields').set_class(not enabled, '-identity-disabled')
        section = next((node for node in self.ancestors if isinstance(node, Collapsible)), None)
        if section:
            section.title = 'Git commit identity · ' + ('set' if enabled else 'not set')

    @on(Input.Changed)
    def clear_error(self, event):
        event.input.remove_class('-invalid')
        self.query_one(Feedback).update('')

    def collect(self):
        self.query_one(Feedback).update('')
        if not self.query_one('#git-identity-enabled', Checkbox).value:
            return None
        try:
            return git_settings.identity({'name': self.query_one('#git-author-name', Input).value.strip(),
                                          'email': self.query_one('#git-author-email', Input).value.strip()})
        except ValueError as error:
            section = next((node for node in self.ancestors if isinstance(node, Collapsible)), None)
            if section:
                section.collapsed = False
            self.query_one(Feedback).update('Error: ' + str(error))
            invalid = '#git-author-name' if not self.query_one('#git-author-name', Input).value.strip() or 'name' in str(error) else '#git-author-email'
            self.query_one(invalid, Input).add_class('-invalid')
            self.call_after_refresh(self.query_one(Feedback).scroll_visible, animate=False)
            raise


class SSHDestinationFields(Vertical):
    DEFAULT_CSS = 'SSHDestinationFields { height: auto; }'

    def __init__(self, settings=None, *, creating=False):
        super().__init__()
        self.settings = settings or {'hosts': []}
        self.original = self.format_hosts(self.settings)
        self.imported = None
        self.creating = creating

    @staticmethod
    def format_hosts(settings):
        return '\n'.join(f'{h["alias"]} {h["user"]} {h["hostname"]} {h["port"]}' for h in settings['hosts'])

    def compose(self):
        with Vertical(classes='setting'):
            yield Label('Destinations · alias username hostname-or-IP port')
            yield TextArea(self.original, id='ssh-hosts')
            yield Feedback('', id='ssh-destinations-error', markup=False)
            if not self.creating:
                yield Static('Apply destinations before generating or importing a key.', classes='setting-help')
        with Vertical(classes='setting'):
            yield Button('Add repository…', id='ssh-repository')
        with Collapsible(title='Import host SSH aliases', collapsed=True):
            with Vertical(classes='setting'):
                yield Label('Aliases from ~/.ssh/config · separated by spaces')
                with Horizontal(classes='path-row'):
                    yield Input(id='ssh-aliases')
                    yield Button('Import', id='ssh-import-aliases')
                yield Feedback('', id='ssh-aliases-error', markup=False)
        with Collapsible(title='Trusted SSH host keys', collapsed=True):
            with Vertical(classes='setting'):
                yield Label('Trusted known_hosts file')
                yield PathField('ssh-known-hosts', str(Path.home() / '.ssh/known_hosts'),
                                title='Choose trusted known_hosts file', mode='file')
                yield Static('Verify unknown hosts in your host terminal before saving destinations.', classes='setting-help')

    @on(Button.Pressed, '#ssh-repository')
    def repository(self, event):
        event.stop()
        self.app.push_screen(RepositorySetup(), self.add_repository)

    @on(Button.Pressed, '#ssh-import-aliases')
    def import_aliases(self, event):
        event.stop()
        feedback = self.query_one('#ssh-aliases-error', Feedback)
        try:
            settings = sandbox_ssh.import_aliases(self.query_one('#ssh-aliases', Input).value, Path.home() / '.ssh/config')
        except (OSError, ValueError) as error:
            feedback.update(str(error))
            feedback.scroll_visible(animate=False)
            return
        text = self.format_hosts(settings)
        self.query_one('#ssh-hosts', TextArea).load_text(text)
        self.imported = (text, settings)
        feedback.update('')
        self.notify('Imported destinations and trusted host keys.')

    def add_repository(self, value):
        if not value:
            return
        field = self.query_one('#ssh-hosts', TextArea)
        rows = [row for row in field.text.splitlines() if row.strip()]
        existing = [row for row in rows if row.split()[0] == 'repository']
        if existing and [row.split() for row in existing] != [value['line'].split()]:
            self.notify('The repository alias already points elsewhere. Edit Destinations to change it.', severity='error')
        elif existing:
            self.notify('Destination already present.')
        else:
            field.load_text('\n'.join(rows + [value['line']]))

    def collect(self):
        text = self.query_one('#ssh-hosts', TextArea).text
        feedback = self.query_one(Feedback)
        feedback.update('')
        try:
            if self.imported and self.imported[0] == text:
                return self.imported[1]
            if text == self.original:
                return self.settings
            return sandbox_ssh.parse_hosts(text, self.query_one('#ssh-known-hosts', Input).value)
        except (ValueError, OSError) as error:
            feedback.update(str(error))
            raise


class CreateSSHFields(Vertical):
    DEFAULT_CSS = 'CreateSSHFields { height: auto; }'

    def __init__(self, draft):
        super().__init__()
        self.draft = draft

    def compose(self):
        yield SSHDestinationFields(self.draft.get('ssh'), creating=True)
        with Vertical(classes='setting'):
            yield Label('SSH key')
            yield Select([('Set up later', 'later'), ('Generate repository key', 'generate'),
                          ('Import and unlock', 'import')], value=self.draft.get('_ssh_key', 'later'), allow_blank=False, id='create-ssh-key')
            yield Static('The private key stays in a separate SSH container.', classes='setting-help')
        with Vertical(id='create-ssh-import', classes='setting'):
            yield Label('Private key file')
            yield PathField('create-ssh-key-file', self.draft.get('_ssh_key_file', ''), title='Choose SSH private key', mode='file',
                            initial=Path.home() / '.ssh', placeholder='~/.ssh/id_ed25519')
            yield Static('Encrypted keys prompt for their passphrase when the sandbox is ready.', classes='setting-help')
            yield Feedback('', id='create-ssh-key-file-error', markup=False)
        with Vertical(id='create-ssh-duration-setting', classes='setting'):
            yield Label('Keep SSH key unlocked for')
            yield Input(self.draft.get('_ssh_key_duration', sandbox_ssh.DEFAULT_UNLOCK_DURATION), id='create-ssh-duration')
            yield Static('Enter 8h for eight hours or 2d for two days.', classes='setting-help')
            yield Feedback('', id='create-ssh-duration-error', markup=False)

    def on_mount(self):
        self.key_changed()

    @on(Select.Changed, '#create-ssh-key')
    def key_changed(self):
        importing = self.query_one('#create-ssh-key', Select).value == 'import'
        self.query_one('#create-ssh-import').display = importing
        self.query_one('#create-ssh-duration-setting').display = importing

    def collect(self, draft):
        for feedback in self.query(Feedback):
            feedback.update('')
        field = self.query_one('#ssh-hosts', TextArea)
        try:
            settings = self.query_one(SSHDestinationFields).collect()
            action = self.query_one('#create-ssh-key', Select).value
            if action != 'later' and not settings['hosts']:
                raise ValueError('Add an SSH destination before setting up a key.')
            result = {'ssh': settings, '_ssh_key': action}
            if action == 'import':
                field = self.query_one('#create-ssh-key-file', Input)
                path = field.value.strip()
                if not path:
                    raise ValueError('Choose a private key file.')
                result['_ssh_key_file'] = str(sandbox_ssh.key_source(draft, path))
                field = self.query_one('#create-ssh-duration', Input)
                duration = field.value.strip().lower()
                sandbox_ssh.unlock_seconds(duration)
                result['_ssh_key_duration'] = duration
            return result
        except (ValueError, OSError) as error:
            feedback_id = 'ssh-destinations-error' if field.id == 'ssh-hosts' else f'{field.id}-error'
            message = self.query_one('#' + feedback_id, Feedback)
            message.update(str(error))
            field.focus(scroll_visible=False)
            group = next(node for node in field.ancestors if node.has_class('setting'))
            target = group if group in message.ancestors else message
            self.call_after_refresh(target.scroll_visible, top=True, animate=False, immediate=True)
            raise


class CreateSSHDialog(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    AUTO_FOCUS = '#ssh-hosts'
    DEFAULT_CSS = """
    CreateSSHDialog { align: center middle; background: #000000 70%; }
    CreateSSHDialog > Vertical { width: 82; max-width: 95%; height: 32; max-height: 95%;
                                 padding: 1 2; background: $panel; border: solid $primary; }
    CreateSSHDialog VerticalScroll { height: 1fr; scrollbar-size-vertical: 1; }
    CreateSSHDialog .dialog-actions { margin: 1 0 0 0; }
    """

    def __init__(self, draft, title):
        super().__init__()
        self.draft = draft
        self.title_text = title

    def compose(self):
        identity = self.draft.get('options', {}).get('git_identity')
        with Vertical():
            yield Label(self.title_text, classes='title')
            with VerticalScroll(classes='spaced-form-fields'):
                yield CreateSSHFields(self.draft)
                with Collapsible(title='Git commit identity', collapsed=True, id='git-identity-section'):
                    yield GitIdentityFields(identity)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='create-ssh-cancel')
                yield Button('Save', id='create-ssh-save', variant='primary')

    def on_mount(self):
        self.set_focus(self.query_one('#ssh-hosts', TextArea), scroll_visible=False)
        self.call_after_refresh(self.query_one(VerticalScroll).scroll_home, animate=False)

    @on(Button.Pressed, '#create-ssh-cancel')
    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, '#create-ssh-save')
    def save(self):
        try:
            values = self.query_one(CreateSSHFields).collect(self.draft)
            identity = self.query_one(GitIdentityFields).collect()
        except (OSError, ValueError):
            return  # The field that failed validation displays the error.
        self.dismiss((values, identity))


class RepositorySetup(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    RepositorySetup { align: center middle; background: #000000 70%; }
    RepositorySetup > Vertical { width: 86; max-width: 95%; height: 24; max-height: 95%; padding: 1 2; background: $panel; border: solid $primary; }
    RepositorySetup VerticalScroll { height: 1fr; }
    RepositorySetup Static { height: auto; }
    RepositorySetup .dialog-actions { height: auto; margin-top: 1; }
    """

    def compose(self):
        with Vertical():
            yield Label('Repository SSH setup', classes='title')
            with VerticalScroll():
                with Vertical(classes='setting'):
                    yield Label('GitHub or GitLab project URL')
                    yield Input(placeholder='https://github.com/owner/project', id='repository-url')
                    yield Static('Self-hosted GitLab is supported. For custom SSH ports, edit Destinations after adding.', classes='setting-help')
                with Vertical(classes='setting', id='repository-clone'):
                    yield Label('Clone inside the sandbox')
                    yield Static('', id='repository-preview', markup=False)
                with Vertical(classes='setting'):
                    yield Button('Open deploy-key settings', id='repository-settings', disabled=True)
                    yield Static('For read-only access, register a separate deploy key with write access disabled.', classes='setting-help')
                yield Feedback('', id='repository-error', classes='error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='repository-cancel')
                yield Button('Add destination', id='repository-add', variant='primary')

    def on_mount(self):
        self.preview()

    def value(self):
        return sandbox_ssh.repository_destination(self.query_one('#repository-url', Input).value.strip())

    @on(Input.Changed, '#repository-url')
    def preview(self):
        try:
            value = self.value()
            self.query_one('#repository-error', Static).update('')
            self.query_one('#repository-preview', Static).update(value['clone'])
            self.query_one('#repository-clone').display = True
            self.query_one('#repository-settings', Button).disabled = False
        except ValueError:
            self.query_one('#repository-preview', Static).update('')
            self.query_one('#repository-clone').display = False
            self.query_one('#repository-settings', Button).disabled = True

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'repository-cancel':
            self.action_cancel()
            return
        try:
            value = self.value()
            if event.button.id == 'repository-settings':
                host_platform.open_url(value['settings_url'])
            elif event.button.id == 'repository-add':
                self.dismiss(value)
        except (ValueError, OSError) as error:
            self.query_one('#repository-error', Static).update(str(error))

    def action_cancel(self):
        self.dismiss(None)


class PublicKey(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]
    DEFAULT_CSS = """
    PublicKey { align: center middle; background: #000000 70%; }
    PublicKey > Vertical { width: 86; max-width: 95%; height: 19; max-height: 95%; padding: 1 2; background: $panel; border: solid $primary; }
    PublicKey VerticalScroll { height: 1fr; }
    PublicKey #key-public { height: 5; min-height: 5; max-height: 5; }
    PublicKey Static { height: auto; }
    PublicKey .dialog-actions { height: auto; margin-top: 1; }
    """

    def __init__(self, name, generate=False):
        super().__init__()
        self.sandbox_name, self.generate = name, generate
        self.public_key = ''
        self.busy = True

    def compose(self):
        with Vertical():
            yield Label('SSH public key', classes='title')
            with VerticalScroll():
                with Vertical(classes='setting'):
                    yield Static('Generating a key in the SSH companion…' if self.generate else 'Reading the unlocked key…', id='key-status', markup=False)
                    yield TextArea(read_only=True, highlight_cursor_line=False, id='key-public')
                    yield Static('The Git server sets this key’s permissions. For read-only repository access, register a separate deploy key with write access disabled.', classes='setting-help')
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='key-close', disabled=True)
                yield Button('Copy public key', id='key-copy', disabled=True, variant='primary')

    def on_mount(self):
        self.load_key()

    def load_key(self):
        app = self.app
        if self.generate:
            name = self.sandbox_name
            def generate(log):
                public = app.service.ssh_key(name, True)
                log(public)
                return public
            def generated(public):
                if self.is_mounted and app.screen is self:
                    self.loaded(public, '')
                else:
                    screen = PublicKey(name)
                    screen.preloaded_key = public
                    app.push_screen(screen)
            def failed(error):
                if self.is_mounted and app.screen is self:
                    self.loaded('', error)
            job = app.start_task(name, 'Generate SSH key', generate, working='Generating SSH key',
                                 on_success=generated, followup_label='Show public key',
                                 on_failure=failed, failure_label='Return to SSH key')
            self.busy = False
            self.query_one('#key-close', Button).disabled = False
            if job is None:
                self.loaded('', 'SSH key generation did not start. Open Jobs to check this sandbox’s current operation.')
        elif hasattr(self, 'preloaded_key'):
            self.loaded(self.preloaded_key, '')
        else:
            self.read_key()

    @work(thread=True, exit_on_error=False)
    def read_key(self):
        app = self.app
        try:
            public = app.service.ssh_key(self.sandbox_name, False)
        except Exception as error:
            app.call_from_thread(self.loaded, '', str(error))
        else:
            app.call_from_thread(self.loaded, public, '')

    def loaded(self, public, error):
        if not self.is_attached:
            return
        self.busy = False
        self.public_key = public
        self.query_one('#key-public', TextArea).load_text(public)
        self.query_one('#key-status', Static).update(error or 'Public key ready. Its private key stays in the SSH companion.')
        close = self.query_one('#key-close', Button)
        copy = self.query_one('#key-copy', Button)
        close.disabled = False
        copy.disabled = not bool(public)
        (copy if public else close).focus()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'key-copy':
            self.app.copy_to_clipboard(self.public_key)
            self.notify('Public key sent to the terminal clipboard.')
        elif event.button.id == 'key-close':
            self.action_close()

    def action_close(self):
        if not self.busy:
            self.dismiss(bool(self.public_key))
