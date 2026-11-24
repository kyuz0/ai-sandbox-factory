"""Select a saved chat, inspect conversion changes and copy it to another sandbox."""
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, Static

from . import primary_workspace, session_transfer
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

    def __init__(self, name):
        super().__init__()
        self.source_name = name
        self.transfer = None
        self.busy = False
        self.previewed = False

    def compose(self):
        records = [record for record in self.app.records if record['name'] != self.source_name
                   and record.get('config', {}).get('engine')
                   and primary_workspace.agent(record['config']) in session_transfer.TARGETS]
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
        self.load_chats()

    @on(Button.Pressed, '#copy-back')
    def back_pressed(self):
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
    def cancel_pressed(self):
        self.action_cancel()

    def on_unmount(self):
        if self.transfer:
            self.transfer.close()

    @work(thread=True, exit_on_error=False)
    def load_chats(self):
        self.busy = True
        try:
            service = self.app.service
            transfer = session_transfer.Transfer(service.chat_config(self.source_name), service.state / self.source_name)
        except Exception as error:
            self.app.call_from_thread(self.failed, str(error))
        else:
            self.app.call_from_thread(self.loaded, transfer)

    def loaded(self, transfer):
        self.transfer = transfer
        self.busy = False
        rows = [row for row in transfer.rows if not row.get('error')]
        control = self.query_one('#copy-chat', Select)
        control.set_options([(row['title'], row['path']) for row in rows])
        control.prompt = 'Choose a chat'
        control.disabled = not rows
        errors = [f'{row["path"]}: {row["error"]}' for row in transfer.rows if row.get('error')]
        if errors:
            self.query_one('#copy-error', Feedback).update('Some chats could not be read.\n' + '\n'.join(errors))
        elif not rows:
            self.query_one('#copy-error', Feedback).update('This sandbox has no saved conversations to copy.')
        self.changed()

    @on(Select.Changed)
    def changed(self):
        self.previewed = False
        self.query_one('#copy-preview', Static).update('')
        self.query_one('#copy-preview').display = False
        button = self.query_one('#copy-next', Button)
        button.label = 'Preview copy'
        button.disabled = self.busy or any(self.query_one(key, Select).value is Select.NULL
                                          for key in ('#copy-chat', '#copy-target'))

    def failed(self, message):
        self.busy = False
        self.query_one('#copy-error', Feedback).update('Error · ' + message)
        self.query_one('#copy-error').scroll_visible(animate=False)
        self.query_one('#copy-cancel', Button).disabled = False
        self.query_one('#copy-back', Button).disabled = False
        self.query_one('#copy-next', Button).disabled = self.transfer is None
        self.query_one('#copy-chat', Select).disabled = self.transfer is None
        self.query_one('#copy-target', Select).disabled = False

    @on(Button.Pressed, '#copy-next')
    def next_pressed(self):
        if self.busy:
            return
        self.busy = True
        self.query_one('#copy-next', Button).disabled = True
        self.query_one('#copy-cancel', Button).disabled = True
        self.query_one('#copy-back', Button).disabled = True
        self.query_one('#copy-chat', Select).disabled = True
        self.query_one('#copy-target', Select).disabled = True
        self.query_one('#copy-error', Feedback).update('')
        self.perform(self.previewed, self.query_one('#copy-chat', Select).value,
                     self.query_one('#copy-target', Select).value)

    @work(thread=True, exit_on_error=False)
    def perform(self, apply, path, destination):
        try:
            if apply:
                self.transfer.apply()
                message = (f'Created a new chat in {destination}.\n'
                           'Open that sandbox and select the copied chat from its history.')
            else:
                target = self.app.service.chat_config(destination)
                result = self.transfer.preview(path, target, self.app.service.state / destination)
                source = result['source']
                source_name = primary_workspace.AGENTS[self.transfer.agent]
                target_name = primary_workspace.AGENTS[primary_workspace.agent(target)]
                title = next(row['title'] for row in self.transfer.rows if row.get('path') == path)
                lines = [title, f'{source_name} → {target_name} in {destination}.',
                         f'{source["events"].get("message", 0)} messages in the source chat.']
                if self.transfer.config['workspace'] != target['workspace']:
                    lines.append('This sandbox uses a different project folder. Earlier files and edits may be absent.')
                dropped = result.get('dropped_events', {})
                if any(dropped.values()):
                    lines.append('Content changed or omitted during conversion:')
                lines.extend(conversion_change(key, count) for key, count in dropped.items() if count)
                lines.extend(str(warning.get('message', warning)) for warning in result.get('warnings', []))
                message = '\n'.join(lines)
        except Exception as error:
            self.app.call_from_thread(self.failed, str(error))
        else:
            self.app.call_from_thread(self.finished, apply, message)

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
