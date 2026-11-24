"""Export and import chats from the sandbox list."""

from pathlib import Path
import subprocess

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from . import chat_history, primary_workspace
from .file_browser import PathField
from .tui_widgets import Feedback


EXPORT = 'Export chat history'
IMPORT = 'Import chat history'


def transfer_error(error):
    """Keep engine exit status and captured diagnostics in the job output."""
    parts = [str(error)]
    if isinstance(error, subprocess.CalledProcessError):
        for output in (error.stdout, error.stderr):
            if output:
                parts.append(output.decode(errors='replace') if isinstance(output, bytes) else output)
    return '\n'.join(parts)


def chat_count(count):
    return f'{count} chat' + ('' if count == 1 else 's')


def backup_message(path, count):
    path = Path(path)
    saved = 'Saved chat history' if count is None else f'Saved {chat_count(count)}'
    return f'{saved}\nFolder · {path.parent}\nFile · {path.name}'


def delete_confirmation(app, name, agent, message=None):
    screen = DeleteSandbox(name, has_config=True, agent=agent, backup=message)
    app.push_screen(screen, lambda yes: app.operation('Deleting ' + name, ['delete', name]) if yes else None)


class ChatTransfer(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    .chat-result { margin: 1 0 0 0; }
    .chat-path-label { margin: 1 0 0 0; text-style: bold; }
    #chat-transfer-filename { text-wrap: nowrap; text-overflow: ellipsis; }
    ChatTransfer Feedback { margin: 1 0 0 0; text-style: bold; }
    ChatTransfer #chat-transfer-content { height: auto; max-height: 55vh; scrollbar-size-vertical: 1; }
    .compact-terminal ChatTransfer.has-error #confirm-box { height: 90%; }
    .compact-terminal ChatTransfer.has-error #chat-transfer-content { height: 1fr; max-height: 100%; }
    """

    def __init__(self, name, *, importing=False, return_to_delete=False, agent='pi',
                 path=None, result=None, error=None):
        super().__init__()
        self.sandbox_name = name
        self.importing = importing
        self.return_to_delete = return_to_delete
        self.agent = agent
        self.busy = False
        self.saved = False
        self.suggested_path = path if path is not None else ('' if importing else str(chat_history.suggested_file(name)))
        self.initial_result = result
        self.initial_error = error

    def compose(self):
        with Vertical(id='confirm-box'):
            title = f'{IMPORT} into {self.sandbox_name}' if self.importing else f'{EXPORT} from {self.sandbox_name}'
            yield Label(title, markup=False)
            with VerticalScroll(id='chat-transfer-content'):
                if self.agent != 'pi':
                    name = primary_workspace.AGENTS[self.agent]
                    help_text = (f'Choose a {name} backup. Stop this sandbox and restore before opening {name}.'
                                 if self.importing else
                                 f'Save all {name} chats to a ZIP file. Stop this sandbox before exporting.')
                    yield Static(help_text, id='chat-transfer-help', markup=False)
                elif self.importing:
                    yield Static('Choose a Factory backup (.zip) or a Pi chat file (.jsonl).\n'
                                 'Chats already saved in this sandbox are skipped.', id='chat-transfer-help', markup=False)
                else:
                    yield Static('Save Pi and FrostPi conversations, including branches, to a ZIP file on this computer.',
                                 id='chat-transfer-help', markup=False)
                with Vertical(id='chat-transfer-fields', classes='setting'):
                    label = 'File to import' if self.importing else 'Save backup as'
                    yield Static(label, classes='chat-path-label', markup=False)
                    folder = chat_history.backup_folder()
                    pi_import = self.importing and self.agent == 'pi'
                    picker_title = 'Choose where to save the backup'
                    if self.importing:
                        picker_title = 'Choose chat backup or Pi chat file' if pi_import else 'Choose chat backup'
                    yield PathField('chat-transfer-path', self.suggested_path, browse_id='chat-transfer-browse',
                                    title=picker_title,
                                    mode='file' if self.importing else 'save',
                                    placeholder='Choose a backup file' if self.importing else '',
                                    file_extensions={'.zip', '.jsonl'} if pi_import else {'.zip'},
                                    initial=folder, places={'Chat backups': folder})
                    yield Static('', id='chat-transfer-filename', markup=False)
                yield Feedback(id='chat-transfer-error', markup=False)
                yield Static('', id='chat-transfer-result', classes='chat-result', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='chat-transfer-cancel')
                yield Button('Import chats' if self.importing else 'Save backup', id='chat-transfer-apply', variant='primary')

    def on_mount(self):
        self.query_one('#chat-transfer-result', Static).display = False
        field = self.query_one('#chat-transfer-path', Input)
        field.cursor_position = len(field.value)
        field.focus()
        self.show_filename(field.value)
        if self.initial_result is not None:
            self.completed(*self.initial_result)
        elif self.initial_error:
            self.failed(self.initial_error)

    def show_filename(self, value):
        name = Path(value.strip()).name
        label = self.query_one('#chat-transfer-filename', Static)
        label.display = bool(name)
        label.update(f'File · {name}' if name else '')

    @on(Input.Changed, '#chat-transfer-path')
    def path_changed(self, event):
        self.show_filename(event.value)

    def action_cancel(self):
        if not self.busy:
            app = self.app
            self.dismiss()
            if self.return_to_delete and not any(isinstance(screen, DeleteSandbox)
                                                and screen.sandbox_name == self.sandbox_name
                                                for screen in app.screen_stack):
                delete_confirmation(app, self.sandbox_name, self.agent)

    @on(Button.Pressed, '#chat-transfer-cancel')
    def cancel_pressed(self, event):
        event.stop()
        self.action_cancel()

    @on(Input.Submitted, '#chat-transfer-path')
    @on(Button.Pressed, '#chat-transfer-apply')
    def submit(self, event):
        event.stop()
        if self.busy or self.saved:
            return
        field = self.query_one('#chat-transfer-path', Input)
        feedback = self.query_one('#chat-transfer-error', Feedback)
        path = field.value.strip()
        if not path:
            feedback.update('Choose a file to import.' if self.importing else 'Enter a backup filename.')
            field.focus()
            return
        app = self.app
        conflict = app.active_job(self.sandbox_name)
        if conflict:
            feedback.update(f'{conflict.title} is already running. Wait for it to finish, then try again.')
            return
        service = app.service
        name, importing, agent = self.sandbox_name, self.importing, self.agent
        return_to_delete = self.return_to_delete

        def deletion(message=None):
            delete_confirmation(app, name, agent, message)

        def form(*, result=None, error=None):
            screen = ChatTransfer(name, importing=importing, return_to_delete=return_to_delete,
                                  agent=agent, path=path, result=result, error=error)
            app.push_screen(screen)

        def transfer(log):
            log(f'Importing chats into {name} from {path}' if importing else f'Saving chat history from {name} to {path}')
            try:
                result = service.import_chats(name, path) if importing else service.export_chats(name, path)
            except subprocess.CalledProcessError as error:
                raise ValueError(transfer_error(error)) from error
            log(f'Imported chat history into {name}.' if importing else backup_message(*result))
            return result

        def completed(result):
            if return_to_delete:
                deletion(backup_message(*result))
            else:
                form(result=result)

        self.dismiss()
        if return_to_delete and isinstance(app.screen, DeleteSandbox) and app.screen.sandbox_name == name:
            app.screen.dismiss(False)
        app.start_task(name, f'{IMPORT if importing else EXPORT} · {name}', transfer,
                             working='Importing chats' if importing else 'Saving backup',
                             on_success=completed, followup_label='View result',
                             on_failure=lambda error: form(error=error), failure_label='Try again')

    def failed(self, message):
        self.add_class('has-error')
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
            if self.agent != 'pi':
                message = 'Restored chat history. Open this sandbox and choose a chat from the agent’s history.'
            elif first:
                message = f'Imported {chat_count(first)}.'
                if second:
                    message += f'\nSkipped {chat_count(second)} already in this sandbox.'
                message += '\nOpen them with Resume session in FrostPi or /resume in Pi.'
            else:
                message = f'This sandbox already has all {chat_count(second)} in the selected file.'
        else:
            message = backup_message(first, second)
        result = self.query_one('#chat-transfer-result', Static)
        result.update(message)
        result.display = True
        result.add_class('chat-success')
        self.query_one('#chat-transfer-help').display = False
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

    def __init__(self, name, *, has_config, agent='pi', backup=None):
        super().__init__()
        self.sandbox_name = name
        self.has_config = has_config
        self.agent = agent
        self.can_export = has_config
        self.backup = backup

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(f'Delete sandbox · {self.sandbox_name}', markup=False)
            if self.has_config:
                name = 'Pi and FrostPi' if self.agent == 'pi' else primary_workspace.AGENTS[self.agent]
                yield Static(f'Saved {name} chats will be permanently deleted. '
                             'Export chat history to keep a copy on this computer.', markup=False)
            yield Static('Your workspace files stay on this computer.', classes='chat-note', markup=False)
            if self.has_config:
                yield Static('This sandbox’s saved sign-ins, keys, settings and editor data will be deleted. '
                             'Original sign-ins and keys outside this sandbox are kept.', classes='chat-note', markup=False)
            yield Static('', id='chat-backup-status', classes='chat-result chat-success', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='cancel')
                if self.can_export:
                    yield Button(f'{EXPORT}…', id='delete-export-chats', variant='primary')
                yield Button('Delete sandbox', id='yes', variant='error')

    def on_mount(self):
        self.query_one('#chat-backup-status', Static).display = False
        if self.backup:
            self.show_backup(self.backup)
        self.query_one('#cancel', Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, '#cancel')
    def cancel_pressed(self, event):
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, '#yes')
    def delete_pressed(self, event):
        event.stop()
        self.dismiss(True)

    @on(Button.Pressed, '#delete-export-chats')
    def export_pressed(self, event):
        event.stop()
        if not self.can_export:
            return
        self.app.push_screen(ChatTransfer(self.sandbox_name, return_to_delete=True, agent=self.agent), self.show_backup)

    def show_backup(self, message):
        if message:
            status = self.query_one('#chat-backup-status', Static)
            status.update(message)
            status.display = True
            backup = self.query_one('#delete-export-chats', Button)
            backup.label = 'Export again…'
            backup.variant = 'default'
        self.query_one('#cancel', Button).focus()
