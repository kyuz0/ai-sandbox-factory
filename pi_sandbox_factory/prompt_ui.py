"""Manage reusable prompts and the copy assigned to a sandbox."""

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, TextArea

from . import activity, prompt_library
from .settings_catalog import SYSTEM_PROMPT_LABEL, SYSTEM_PROMPT_LIBRARY_LABEL
from .tui_widgets import Feedback


class EditPrompt(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, title, initial, save):
        super().__init__()
        self.title_text = title
        self.initial = initial or {'name': '', 'text': ''}
        self.save = save

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(self.title_text)
            with VerticalScroll(id='prompt-fields'):
                with Vertical(classes='setting'):
                    yield Label('Name')
                    yield Input(self.initial['name'], id='prompt-name')
                    yield Feedback('', id='prompt-name-error')
                with Vertical(classes='setting'):
                    yield Label('Instructions')
                    yield TextArea(self.initial['text'], highlight_cursor_line=False, id='prompt-text')
                    yield Feedback('', id='prompt-text-error')
                yield Feedback('', id='prompt-error')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='cancel-prompt')
                yield Button('Save', id='save-prompt', variant='primary')

    def on_mount(self):
        self.query_one('#prompt-name', Input).focus()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'cancel-prompt':
            self.action_cancel()
            return
        try:
            for feedback in self.query(Feedback):
                feedback.update('')
            for field in self.query('.prompt-invalid'):
                field.remove_class('prompt-invalid')
            value = prompt_library.prompt({'name': self.query_one(Input).value, 'text': self.query_one(TextArea).text})
            self.save(value)
        except prompt_library.InvalidPrompt as error:
            field = self.query_one('#prompt-' + error.field)
            field.add_class('prompt-invalid')
            field.focus()
            feedback = self.query_one(f'#prompt-{error.field}-error', Feedback)
            feedback.update(str(error))
            feedback.scroll_visible(animate=False)
            return
        except (OSError, ValueError) as error:
            feedback = self.query_one('#prompt-error', Feedback)
            feedback.update(str(error))
            feedback.scroll_visible(animate=False)
            return
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class PromptLibrary(Vertical):
    def __init__(self):
        super().__init__()
        self.prompts = {}

    def compose(self):
        yield Static('Save instructions you want to reuse. Choose a prompt when you create a sandbox or in its Settings.',
                     classes='section-intro')
        yield Vertical(id='system-prompt-list', classes='package-list')

    async def refresh_library(self):
        listing = self.query_one('#system-prompt-list', Vertical)
        await listing.remove_children()
        try:
            self.prompts = prompt_library.entries()
        except (OSError, ValueError) as error:
            self.prompts = {}
            await listing.mount(Static(str(error), markup=False))
            return
        if not self.prompts:
            await listing.mount(Static('No system prompts saved.', classes='setting-help'))
            return
        for key, value in sorted(self.prompts.items(), key=lambda item: item[1]['name'].casefold()):
            await listing.mount(Vertical(
                Label(value['name'], classes='package-title', markup=False),
                Horizontal(Button('Edit…', name=key, classes='edit-system-prompt'),
                           Button('Remove…', name=key, classes='remove-system-prompt'), classes='buttons package-actions'),
                classes='package-row',
            ))

    def edit(self, key=None):
        initial = self.prompts.get(key)
        self.app.push_screen(EditPrompt('Edit system prompt' if initial else 'New system prompt', initial,
                                        lambda value: prompt_library.save(**value, key=key)), self.refresh_after)

    def refresh_after(self, changed):
        if changed:
            self.run_worker(self.refresh_library(), exclusive=True)

    @on(Button.Pressed, '.edit-system-prompt')
    def edit_pressed(self, event):
        event.stop()
        self.edit(event.button.name)

    @on(Button.Pressed, '.remove-system-prompt')
    def remove_pressed(self, event):
        from .sandbox_tui import Confirm

        event.stop()
        key = event.button.name
        name = self.prompts[key]['name']

        def remove(confirmed):
            if confirmed:
                try:
                    prompt_library.remove(key)
                except (OSError, ValueError) as error:
                    self.notify(str(error), severity='error')
                else:
                    self.refresh_after(True)

        self.app.push_screen(Confirm(f'Remove “{name}” from the library? Existing sandboxes keep their saved copies.',
                                     'Remove', confirm_by_default=False, title='Remove system prompt'), remove)


class PromptChoice(Vertical):
    def __init__(self, current, *, help_text, editable=False, agent='Pi'):
        super().__init__(classes='setting')
        self.current = current
        self.help_text = help_text
        self.editable = editable
        self.agent = agent
        self.prompts = {}
        self.error = ''
        try:
            self.prompts = prompt_library.entries()
        except (OSError, ValueError) as error:
            self.error = str(error)

    def choices(self):
        available = sorted(self.prompts.items(), key=lambda item: item[1]['name'].casefold())
        result = [('None', '')]
        if self.current:
            result.append((self.current['name'] + ' · sandbox copy', 'saved'))
        return result + [(value['name'], key) for key, value in available if value != self.current]

    def compose(self):
        yield Label(SYSTEM_PROMPT_LABEL)
        yield Select(self.choices(), value='saved' if self.current else '', allow_blank=False, id='system-prompt-choice')
        yield Static(self.help_text, classes='setting-help')
        if self.error:
            yield Feedback(self.error, markup=False)
        elif not self.prompts:
            yield Static(f'Add reusable prompts in Library → {SYSTEM_PROMPT_LIBRARY_LABEL}.', classes='setting-help')
        with Vertical(id='system-prompt-preview'):
            yield Label(f'Instructions for {self.agent} (preview)')
            yield TextArea('', read_only=True, show_cursor=False, highlight_cursor_line=False)
            if self.editable:
                yield Button('Edit sandbox copy…', id='edit-prompt-copy')

    def selected(self):
        key = self.query_one(Select).value
        return self.current if key == 'saved' else self.prompts.get(key)

    def on_mount(self):
        self.preview()

    @on(Select.Changed)
    def preview(self):
        value = self.selected()
        self.query_one('#system-prompt-preview').display = value is not None
        self.query_one(TextArea).load_text(value['text'] if value else '')

    @on(Button.Pressed, '#edit-prompt-copy')
    def edit_copy(self, event):
        event.stop()
        self.app.push_screen(EditPrompt('Edit sandbox copy', self.selected(), self.replace_copy))

    def replace_copy(self, value):
        self.current = value
        choice = self.query_one(Select)
        choice.set_options(self.choices())
        choice.value = 'saved'
        self.preview()


class SandboxPromptScreen(Screen):
    BINDINGS = [('escape', 'back', 'Cancel')]

    def __init__(self, name, state, supported, *, agent='Pi'):
        super().__init__()
        self.sandbox_name = name
        self.state = state
        self.current = prompt_library.selection(state)
        self.supported = supported
        self.agent = agent

    def compose(self):
        yield Header()
        yield Label(SYSTEM_PROMPT_LABEL + ' · ' + self.sandbox_name, classes='title focused-settings-title', markup=False)
        with VerticalScroll(classes='form'):
            if not self.supported:
                yield Static('This sandbox needs a newer image. Choose Actions → Update sources and recreate…',
                             id='prompt-update-notice', markup=False)
            help_text = ('Changes apply the next time Pi starts a reply, including in open chats.' if self.supported
                         else 'Save stores a copy of the prompt for after the update.')
            if self.agent != 'Pi':
                help_text = f'The instructions apply when you next open {self.agent}.'
            yield PromptChoice(self.current, help_text=help_text, editable=True, agent=self.agent)
            yield Feedback('', id='sandbox-prompt-error')
        with Horizontal(classes='wizard-actions'):
            yield Button('Cancel', id='prompt-back')
            yield Button('Save', id='sandbox-prompt-save', variant='primary')
        yield Footer()

    def on_mount(self):
        self.query_one('#system-prompt-choice', Select).focus()

    @on(Button.Pressed, '#prompt-back')
    def action_back(self):
        self.dismiss()

    @on(Button.Pressed, '#sandbox-prompt-save')
    def save(self):
        try:
            changed = prompt_library.assign(self.state, self.query_one(PromptChoice).selected())
            if changed:
                activity.record(self.state, 'edited')
        except (OSError, ValueError) as error:
            self.query_one('#sandbox-prompt-error', Feedback).update(str(error))
            return
        if changed:
            message = ('System prompt saved. It applies when Pi starts its next reply.' if self.supported else
                       'System prompt saved. Update sources to use it.')
            if self.agent != 'Pi':
                message = f'System prompt saved. Reopen {self.agent} to use it.'
            self.app.notify(message)
        self.dismiss()
