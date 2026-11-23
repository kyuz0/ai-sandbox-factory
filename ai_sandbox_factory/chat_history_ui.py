"""Export and import Pi and FrostPi chats from the sandbox list."""

from pathlib import Path
import subprocess

from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from . import chat_history, primary_workspace
from .file_browser import PathField
from .tui_widgets import Feedback


EXPORT = 'Export chat history'
IMPORT = 'Import chat history'


def chat_count(count):
    return f'{count} chat' + ('' if count == 1 else 's')


def backup_message(path, count):
    path = Path(path)
    return f'Saved {chat_count(count)}\nFolder · {path.parent}\nFile · {path.name}'


class ChatTransfer(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    .chat-result { margin: 1 0 0 0; }
    .chat-path-label { margin: 1 0 0 0; }
    ChatTransfer Feedback { margin: 1 0 0 0; text-style: bold; }
    """

    def __init__(self, name, *, importing=False, return_to_delete=False):
        super().__init__()
        self.sandbox_name = name
        self.importing = importing
        self.return_to_delete = return_to_delete
        self.busy = False
        self.saved = False
        self.suggested_path = '' if importing else str(chat_history.suggested_file(name))

    def compose(self):
        with Vertical(id='confirm-box'):
            title = f'{IMPORT} into {self.sandbox_name}' if self.importing else f'{EXPORT} from {self.sandbox_name}'
            yield Label(title, markup=False)
            if self.importing:
                yield Static('Choose a Factory backup (.zip) or a Pi chat file (.jsonl).\n'
                             'Chats already saved in this sandbox are skipped.', markup=False)
            else:
                yield Static('Save Pi and FrostPi conversations, including branches, to a ZIP file on this computer.', markup=False)
            with Vertical(id='chat-transfer-fields', classes='setting'):
                label = 'File to import' if self.importing else 'Save backup as'
                yield Static(label, classes='chat-path-label', markup=False)
                folder = chat_history.backup_folder()
                yield PathField('chat-transfer-path', self.suggested_path, browse_id='chat-transfer-browse',
                                title='Choose chat backup or Pi chat file' if self.importing else 'Choose where to save the backup',
                                mode='file' if self.importing else 'save',
                                file_extensions={'.zip', '.jsonl'} if self.importing else {'.zip'},
                                initial=folder, places={'Chat backups': folder})
            yield Feedback(id='chat-transfer-error', markup=False)
            yield Static('', id='chat-transfer-result', classes='chat-result', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='chat-transfer-cancel')
                yield Button('Import chats' if self.importing else 'Save backup', id='chat-transfer-apply', variant='primary')

    def on_mount(self):
        self.query_one('#chat-transfer-result', Static).display = False
        self.query_one('#chat-transfer-path', Input).focus()

    def action_cancel(self):
        if not self.busy:
            self.dismiss()

    @on(Button.Pressed, '#chat-transfer-cancel')
    def cancel_pressed(self):
        self.action_cancel()

    @on(Input.Submitted, '#chat-transfer-path')
    @on(Button.Pressed, '#chat-transfer-apply')
    def submit(self):
        if self.busy or self.saved:
            return
        field = self.query_one('#chat-transfer-path', Input)
        feedback = self.query_one('#chat-transfer-error', Feedback)
        path = field.value.strip()
        if not path:
            feedback.update('Choose a file to import.' if self.importing else 'Enter a backup filename.')
            field.focus()
            return
        self.busy = True
        self.query_one('#chat-transfer-fields').disabled = True
        self.query_one('#chat-transfer-cancel', Button).disabled = True
        self.query_one('#chat-transfer-apply', Button).disabled = True
        feedback.update('')
        result = self.query_one('#chat-transfer-result', Static)
        result.update('Importing chats…' if self.importing else 'Saving backup…')
        result.display = True
        self.run_transfer(path)

    @work(thread=True, exit_on_error=False)
    def run_transfer(self, path):
        try:
            if self.importing:
                first, second = self.app.service.import_chats(self.sandbox_name, path)
            else:
                first, second = self.app.service.export_chats(self.sandbox_name, path)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            message = ('Could not reach the container engine. Check that it’s running.'
                       if isinstance(error, subprocess.CalledProcessError) else str(error))
            self.app.call_from_thread(self.failed, message)
        else:
            self.app.call_from_thread(self.completed, first, second)

    def failed(self, message):
        self.busy = False
        self.query_one('#chat-transfer-result', Static).display = False
        self.query_one('#chat-transfer-fields').disabled = False
        self.query_one('#chat-transfer-cancel', Button).disabled = False
        self.query_one('#chat-transfer-apply', Button).disabled = False
        message = ''.join(c for c in message if c.isprintable() or c == '\n')
        self.query_one('#chat-transfer-error', Feedback).update('Error · ' + message)
        self.query_one('#chat-transfer-path', Input).focus()

    def completed(self, first, second):
        self.busy = False
        self.saved = True
        if self.importing:
            if first:
                message = f'Imported {chat_count(first)}.'
                if second:
                    message += f'\nSkipped {chat_count(second)} already in this sandbox.'
                message += '\nOpen them with Resume session in FrostPi or /resume in Pi.'
            else:
                message = f'This sandbox already has all {chat_count(second)} in the selected file.'
        else:
            message = backup_message(first, second)
            if self.return_to_delete:
                self.dismiss(message)
                return
        result = self.query_one('#chat-transfer-result', Static)
        result.update(message)
        result.add_class('chat-success')
        self.query_one('#chat-transfer-fields').display = False
        self.query_one('#chat-transfer-apply', Button).display = False
        done = self.query_one('#chat-transfer-cancel', Button)
        done.label = 'Done'
        done.disabled = False
        done.focus()


class DeleteSandbox(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    DeleteSandbox .chat-note, DeleteSandbox .chat-result { margin: 1 0 0 0; }
    """

    def __init__(self, name, *, has_config, agent='pi'):
        super().__init__()
        self.sandbox_name = name
        self.has_config = has_config
        self.agent = agent
        self.can_export = has_config and agent == 'pi'

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(f'Delete sandbox · {self.sandbox_name}', markup=False)
            if self.has_config:
                if self.can_export:
                    yield Static('Saved Pi and FrostPi chats will be permanently deleted. '
                                 'Export chat history to keep a copy on this computer.', markup=False)
                else:
                    yield Static(f'Saved {primary_workspace.AGENTS[self.agent]} chats will be permanently deleted.', markup=False)
            yield Static('Your workspace files stay on this computer.', classes='chat-note', markup=False)
            if self.has_config:
                yield Static('This sandbox’s saved sign-ins, keys, settings and editor data will be deleted. '
                             'Original sign-ins and keys outside this sandbox are kept.', markup=False)
            yield Static('', id='chat-backup-status', classes='chat-result chat-success', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='cancel')
                if self.can_export:
                    yield Button(f'{EXPORT}…', id='delete-export-chats', variant='primary')
                yield Button('Delete sandbox', id='yes', variant='error')

    def on_mount(self):
        self.query_one('#chat-backup-status', Static).display = False
        self.query_one('#cancel', Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, '#cancel')
    def cancel_pressed(self):
        self.action_cancel()

    @on(Button.Pressed, '#yes')
    def delete_pressed(self):
        self.dismiss(True)

    @on(Button.Pressed, '#delete-export-chats')
    def export_pressed(self):
        if not self.can_export:
            return
        self.app.push_screen(ChatTransfer(self.sandbox_name, return_to_delete=True), self.show_backup)

    def show_backup(self, message):
        if message:
            status = self.query_one('#chat-backup-status', Static)
            status.update(message)
            status.display = True
            backup = self.query_one('#delete-export-chats', Button)
            backup.label = 'Export again…'
            backup.variant = 'default'
        self.query_one('#cancel', Button).focus()
