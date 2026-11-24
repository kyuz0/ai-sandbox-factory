"""Select a saved chat, inspect conversion changes and copy it to another sandbox."""
from dataclasses import dataclass
import subprocess

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, Static

from . import primary_workspace, session_transfer
from .chat_history_ui import transfer_error
from .tui_widgets import Feedback


def conversion_change(key, count):
    kind, _, reason = key.partition(':')
    names = {'thinking': 'Thinking traces', 'opaque': 'Other agent data',
             'context': 'Attached content', 'compaction': 'Compaction data',
             'tool_call': 'Tool calls', 'tool_result': 'Tool results',
             'session': 'Chat details', 'message': 'Messages', 'timestamp': 'Timestamps'}
    explanation = {'unsupported': 'cannot carry over', 'boundary_metadata': 'rewritten for the destination',
                   'namespace': 'tool names adjusted', 'image': 'image content changed or omitted',
                   'ui_only_projection': 'display-only content omitted'}
    return f'{names.get(kind, kind.replace("_", " ").capitalize())} · {count} · {explanation.get(reason, reason.replace("_", " "))}'


@dataclass
class CopyPreview:
    transfer: session_transfer.Transfer
    path: str
    destination: str
    message: str

    def close(self):
        self.transfer.close()


class ContinueChat(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    ContinueChat { align: center middle; background: #000000 70%; }
    ContinueChat #confirm-box {
        width: 100; max-width: 95%; height: 90%; max-height: 36;
        padding: 1 2; background: #34383d; border: solid #f2b544;
    }
    ContinueChat #confirm-box > Label { height: auto; margin: 0 0 1 0; color: #f2b544; text-style: bold; }
    ContinueChat #copy-fields { height: 1fr; padding: 0 1 0 0; scrollbar-size-vertical: 1; }
    ContinueChat #copy-intro { margin: 0 0 1 0; }
    ContinueChat .setting { height: auto; margin: 0 0 1 0; }
    ContinueChat .setting Static { margin: 0 0 1 0; }
    ContinueChat Feedback { height: auto; margin: 1 0; color: #ff9184; text-style: bold; }
    ContinueChat:nocolor Feedback { color: white; text-style: bold reverse; }
    ContinueChat #copy-preview { margin: 1 0; }
    ContinueChat .dialog-actions { height: 1; margin: 1 0 0 0; align-horizontal: right; }
    ContinueChat Button { width: auto; height: 1; padding: 0 2; margin: 0 0 0 2; border: none; }
    ContinueChat Button.-primary { background: #f2b544; color: #17191b; }
    ContinueChat Button:disabled { opacity: 40%; }
    ContinueChat Button:focus { background: #d4e0e7; color: #171a1d; text-style: bold underline; }
    """

    def __init__(self, name, *, transfer=None, rows=None, path=None, destination=None, preview=None, result=None, error=None):
        super().__init__()
        self.source_name = name
        self.transfer = transfer
        self.initial_rows = rows
        self.selected_path = path
        self.destination = destination
        self.initial_preview = preview
        self.initial_result = result
        self.initial_error = error
        self.busy = False
        self.previewed = False
        self.restoring = True

    def compose(self):
        records = [record for record in self.app.records if record['name'] != self.source_name
                   and record.get('config', {}).get('engine')
                   and primary_workspace.agent(record['config']) in session_transfer.TARGETS]
        self.available_targets = {record['name'] for record in records}
        with Vertical(id='confirm-box'):
            yield Label('Continue in another sandbox', markup=False)
            with VerticalScroll(id='copy-fields'):
                yield Static('Stop both sandboxes first. The selected conversation will be copied into a new chat.',
                             id='copy-intro', markup=False)
                with Vertical(classes='setting'):
                    yield Static('Chat to copy', markup=False)
                    yield Select([], prompt='Loading saved chats…', id='copy-chat', disabled=True)
                with Vertical(classes='setting'):
                    yield Static('Destination sandbox', markup=False)
                    yield Select([(f'{row["name"]} · {primary_workspace.AGENTS[primary_workspace.agent(row["config"])]}',
                                   row['name']) for row in records], prompt='Choose a sandbox', id='copy-target')
                yield Static('Conversation text and saved tool output will be shared with the destination agent.',
                             id='copy-sharing', markup=False)
                yield Static('', id='copy-preview', markup=False)
                yield Feedback(id='copy-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='copy-cancel')
                yield Button('Back', id='copy-back')
                yield Button('Preview copy', id='copy-next', variant='primary', disabled=True)

    def on_mount(self):
        self.query_one('#copy-back').display = False
        self.query_one('#copy-preview').display = False
        if self.transfer is None and self.initial_result is None and self.initial_error is None:
            self.call_after_refresh(self.load_chats)
        else:
            self.call_after_refresh(self.restore)

    def restore(self):
        if self.transfer is not None:
            self.loaded(self.transfer)
        elif self.initial_rows is not None:
            self.populate(self.initial_rows)
        rows = self.transfer.rows if self.transfer is not None else self.initial_rows or []
        paths = {row['path'] for row in rows if not row.get('error')}
        if self.selected_path in paths:
            self.query_one('#copy-chat', Select).value = self.selected_path
        if self.destination in self.available_targets:
            self.query_one('#copy-target', Select).value = self.destination
        self.call_after_refresh(self.finish_restore)

    def finish_restore(self):
        self.restoring = False
        self.changed()
        if self.initial_error:
            self.failed(self.initial_error)
        if self.initial_preview:
            self.finished(False, self.initial_preview)
        elif self.initial_result:
            self.finished(True, self.initial_result)

    @on(Button.Pressed, '#copy-back')
    def back_pressed(self, event):
        event.stop()
        if self.busy:
            return
        self.show_selection(True)
        self.changed()

    def show_selection(self, visible):
        for selector in ('#copy-intro', '.setting'):
            for widget in self.query(selector):
                widget.display = visible
        self.query_one('#copy-back').display = not visible

    def action_cancel(self):
        if not self.busy:
            self.dismiss()

    @on(Button.Pressed, '#copy-cancel')
    def cancel_pressed(self, event):
        event.stop()
        self.action_cancel()

    def on_unmount(self):
        if self.transfer:
            self.transfer.close()

    def load_chats(self):
        app, name = self.app, self.source_name
        conflict = app.active_job(name)
        if conflict:
            self.restoring = False
            self.failed(f'{conflict.title} is already running. Wait for it to finish, then try again.')
            return
        path, destination, error = self.selected_path, self.destination, self.initial_error
        selected = self.query_one('#copy-chat', Select).value
        target = self.query_one('#copy-target', Select).value
        if selected is not Select.NULL:
            path = selected
        if target is not Select.NULL:
            destination = target
        rows = self.initial_rows
        service = app.service

        def load(log):
            log(f'Reading saved chats from {name}…')
            try:
                transfer = session_transfer.Transfer(service.chat_config(name), service.state / name)
            except subprocess.CalledProcessError as error:
                raise ValueError(transfer_error(error)) from error
            log(f'Read {len(transfer.rows)} saved chat entries.')
            return transfer

        def loaded(transfer):
            app.push_screen(ContinueChat(name, transfer=transfer, path=path, destination=destination, error=error))

        self.dismiss()
        app.start_task(name, 'Read saved chats · ' + name, load, working='Reading saved chats',
                             on_success=loaded, followup_label='Choose a chat',
                             on_failure=lambda message: app.push_screen(ContinueChat(name, rows=rows, path=path, destination=destination,
                                                                                   error=message)),
                             failure_label='View error')

    def loaded(self, transfer):
        self.transfer = transfer
        self.populate(transfer.rows)

    def populate(self, entries):
        self.busy = False
        rows = [row for row in entries if not row.get('error')]
        control = self.query_one('#copy-chat', Select)
        control.set_options([(row['title'], row['path']) for row in rows])
        control.prompt = 'Choose a chat'
        control.disabled = not rows
        if rows:
            control.focus()
        errors = [f'{row["path"]}: {row["error"]}' for row in entries if row.get('error')]
        if errors:
            self.query_one('#copy-error', Feedback).update('Some chats could not be read.\n' + '\n'.join(errors))
        elif not rows:
            self.query_one('#copy-error', Feedback).update('This sandbox has no saved conversations to copy.')
        self.changed()

    @on(Select.Changed)
    def changed(self, event=None):
        if self.restoring:
            return
        if (event is not None and self.previewed
                and self.query_one('#copy-chat', Select).value == self.selected_path
                and self.query_one('#copy-target', Select).value == self.destination):
            return
        self.previewed = False
        self.query_one('#copy-preview', Static).update('')
        self.query_one('#copy-preview').display = False
        button = self.query_one('#copy-next', Button)
        button.label = 'Preview copy' if self.transfer is not None else 'Retry loading chats'
        button.disabled = self.busy or (self.transfer is not None and any(
            self.query_one(key, Select).value is Select.NULL for key in ('#copy-chat', '#copy-target')))

    def failed(self, message):
        self.busy = False
        self.query_one('#copy-error', Feedback).update('Error · ' + message)
        self.query_one('#copy-error').scroll_visible(animate=False)
        self.query_one('#copy-cancel', Button).disabled = False
        self.query_one('#copy-back', Button).disabled = False
        self.query_one('#copy-next', Button).disabled = False
        if self.transfer is None:
            self.query_one('#copy-next', Button).label = 'Retry loading chats'
        self.query_one('#copy-chat', Select).disabled = self.transfer is None
        self.query_one('#copy-target', Select).disabled = False

    @on(Button.Pressed, '#copy-next')
    def next_pressed(self, event):
        event.stop()
        if self.busy:
            return
        if self.transfer is None:
            self.load_chats()
            return
        path = self.query_one('#copy-chat', Select).value
        destination = self.query_one('#copy-target', Select).value
        if path is Select.NULL or destination is Select.NULL:
            return
        app, service, name = self.app, self.app.service, self.source_name
        conflict = app.active_job(name) or app.active_job(destination)
        if conflict:
            self.failed(f'{conflict.title} is already running. Wait for it to finish, then try again.')
            return
        transfer, apply = self.transfer, self.previewed
        rows = list(transfer.rows)
        self.transfer = None  # The app-owned job now owns the immutable snapshot.

        def perform(log):
            try:
                if apply:
                    log(f'Creating the copied chat in {destination}…')
                    transfer.apply()
                    message = (f'Created a new chat in {destination}.\n'
                               'Open that sandbox and select the copied chat from its history.')
                    log(message)
                    return message
                log(f'Preparing a chat copy from {name} to {destination}…')
                target = service.chat_config(destination)
                result = transfer.preview(path, target, service.state / destination)
                source = result['source']
                source_name = primary_workspace.AGENTS[transfer.agent]
                target_name = primary_workspace.AGENTS[primary_workspace.agent(target)]
                title = next(row['title'] for row in transfer.rows if row.get('path') == path)
                lines = [title, f'{source_name} → {target_name} in {destination}.',
                         f'{source["events"].get("message", 0)} messages in the source chat.']
                if transfer.config['workspace'] != target['workspace']:
                    lines.append('This sandbox uses a different project folder. Earlier files and edits may be absent.')
                dropped = result.get('dropped_events', {})
                if any(dropped.values()):
                    lines.append('Content changed or omitted during conversion:')
                lines.extend(conversion_change(key, count) for key, count in dropped.items() if count)
                lines.extend(str(warning.get('message', warning)) for warning in result.get('warnings', []))
                message = '\n'.join(lines)
                log(message)
                return CopyPreview(transfer, path, destination, message)
            except BaseException as error:
                if not apply:
                    transfer.close()
                if isinstance(error, subprocess.CalledProcessError):
                    raise ValueError(transfer_error(error)) from error
                raise
            finally:
                if apply:
                    transfer.close()

        def completed(value):
            if apply:
                app.push_screen(ContinueChat(name, result=value))
            else:
                app.push_screen(ContinueChat(name, transfer=value.transfer, path=value.path,
                                            destination=value.destination, preview=value.message))

        self.dismiss()
        job = app.start_task(name, ('Copy chat' if apply else 'Preview chat copy') + f' · {name} → {destination}',
                             perform, working='Copying chat' if apply else 'Preparing preview',
                             on_success=completed, followup_label='View result' if apply else 'Review copy',
                             on_failure=lambda error: app.push_screen(ContinueChat(name, rows=rows, path=path,
                                                                                 destination=destination, error=error)),
                             failure_label='Try again', targets=(name, destination))
        if job is None:
            transfer.close()

    def finished(self, applied, message):
        self.busy = False
        self.query_one('#copy-preview', Static).update(message)
        self.query_one('#copy-preview').display = True
        self.show_selection(False)
        self.query_one('#copy-cancel', Button).disabled = False
        self.query_one('#copy-back', Button).disabled = False
        button = self.query_one('#copy-next', Button)
        if applied:
            self.query_one('#copy-sharing').display = False
            self.query_one('#copy-back').display = False
            button.display = False
            self.query_one('#copy-cancel', Button).label = 'Done'
            self.query_one('#copy-cancel', Button).focus()
            self.query_one('#copy-preview').styles.margin = (0, 0, 1, 0)
            self.query_one('#copy-chat', Select).disabled = True
            self.query_one('#copy-target', Select).disabled = True
        else:
            self.previewed = True
            self.query_one('#copy-chat', Select).disabled = False
            self.query_one('#copy-target', Select).disabled = False
            button.label = 'Create copy'
            button.disabled = False
            button.focus()
            self.query_one('#copy-fields', VerticalScroll).scroll_home(animate=False)
