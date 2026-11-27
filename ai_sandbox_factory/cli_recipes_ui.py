"""Optional, read-only CLI recipes; opening this view never applies a change."""
from pathlib import Path
from rich.console import Console
from rich.text import Text

from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from .file_browser import FolderPicker
from .tui_widgets import Feedback


class CLICommands(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]
    DEFAULT_CSS = '''
    CLICommands { align: center middle; background: #000000 70%; }
    CLICommands #cli-box { width: 110; max-width: 100%; height: 90%; border: solid $accent; padding: 1 2; }
    CLICommands #cli-title { height: auto; margin: 0 0 1 0; text-style: bold; color: $accent; }
    CLICommands #cli-notes { height: auto; margin: 0 0 1 0; }
    CLICommands #cli-text { height: 1fr; border: none; }
    CLICommands #cli-feedback { max-height: 25%; overflow-y: auto; }
    CLICommands #cli-actions { height: 1; margin: 1 0 0 0; align-horizontal: right; }
    CLICommands #cli-actions Button { height: 1; min-height: 1; min-width: 0; width: auto; padding: 0 2; margin: 0 0 0 1; border: none; }
    '''

    def __init__(self, recipe=None, *, loader=None):
        super().__init__()
        self.recipe = recipe
        self.loader = loader

    def compose(self):
        with Vertical(id='cli-box'):
            yield Static('CLI commands' + (' · ' + self.recipe.title if self.recipe else ''), id='cli-title', markup=False)
            yield Static('\n'.join(self.recipe.notes) if self.recipe else 'Reading saved settings…', id='cli-notes', markup=False)
            yield TextArea.code_editor('', language='bash', read_only=True, show_line_numbers=False,
                                       soft_wrap=False, highlight_cursor_line=False, id='cli-text')
            yield Feedback('', id='cli-feedback', markup=False)
            with Horizontal(id='cli-actions'):
                yield Button('Copy commands', id='cli-copy', disabled=not (self.recipe and self.recipe.steps))
                yield Button('Save script…', id='cli-save', disabled=not (self.recipe and self.recipe.steps))
                yield Button('Close', id='cli-close', variant='primary')

    def on_mount(self):
        if self.loader:
            self.load_recipe()
        else:
            self.loaded(self.recipe, '')

    @work(thread=True, exit_on_error=False)
    def load_recipe(self):
        app = self.app
        try:
            recipe = self.loader()
        except Exception as error:
            recipe, message = None, (f'Could not prepare CLI commands: {type(error).__name__}: {error}. '
                                     'Close this view and check the saved sandbox configuration.')
        else:
            message = ''
        if app.is_running:
            try:
                app.call_from_thread(self.loaded, recipe, message)
            except RuntimeError:
                if app.is_running:
                    raise

    def loaded(self, recipe, error):
        if not self.is_attached:
            return
        self.recipe = recipe
        self.query_one('#cli-title', Static).update('CLI commands' + (' · ' + recipe.title if recipe else ''))
        notes = self.query_one('#cli-notes', Static)
        notes.update('\n'.join(recipe.notes) if recipe else '')
        notes.display = bool(recipe and recipe.notes)
        self.query_one('#cli-feedback', Feedback).update(error)
        for selector in ('#cli-copy', '#cli-save'):
            self.query_one(selector, Button).disabled = not (recipe and recipe.steps)
        self.render_commands()
        self.call_after_refresh(self.focus_action)

    def focus_action(self):
        if self.is_attached:
            self.query_one('#cli-copy' if self.recipe and self.recipe.steps else '#cli-close', Button).focus()

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.render_commands)

    def render_commands(self):
        if not self.is_attached or self.recipe is None:
            return
        area = self.query_one('#cli-text', TextArea)
        width = (area.content_size.width or self.size.width - 6) - area.styles.scrollbar_size_vertical
        text = self.recipe.render(width=width)
        if area.text != text:
            area.load_text(text)
        notes = '\n'.join(self.recipe.notes)
        error = str(self.query_one('#cli-feedback', Feedback).render())
        lines = lambda value: len(Text(value).wrap(Console(width=width), width)) if value else 0
        content_height = 9 + len(text.splitlines()) + (lines(notes) + 1 if notes else 0) + lines(error)
        self.query_one('#cli-box').styles.height = min(round(self.size.height * .9), content_height)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'cli-close':
            self.action_close()
        elif event.button.id == 'cli-copy':
            self.app.copy_to_clipboard(self.query_one('#cli-text', TextArea).text)
            self.notify('Commands sent to the terminal clipboard.')
        elif event.button.id == 'cli-save':
            self.app.push_screen(FolderPicker(Path.home(), mode='save', title='Save CLI script',
                                             filename='sandbox.sh'), self.save_selected)

    def save_selected(self, value):
        if not value or not self.is_attached:
            return
        path = Path(value)
        if path.exists():
            from .sandbox_tui import Confirm
            self.app.push_screen(Confirm(f'Replace the script at {path}?', 'Replace script',
                                         confirm_by_default=False),
                                 lambda yes: self.save_script(path) if yes and self.is_attached else None)
        else:
            self.save_script(path)

    def save_script(self, path):
        try:
            path.write_text('#!/bin/sh\n' + self.query_one('#cli-text', TextArea).text)
        except OSError as error:
            self.query_one('#cli-feedback', Feedback).update(f'Could not save {path}: {error}. Choose a writable file.')
            self.render_commands()
        else:
            self.notify(f'Script saved to {path}. Run it with sh.')

    def action_close(self):
        self.dismiss(None)
