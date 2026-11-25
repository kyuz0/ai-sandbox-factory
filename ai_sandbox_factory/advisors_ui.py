"""Shared advisor selection and explicit host-login import controls."""
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static
from .advisor_credentials import LABELS
from .tui_widgets import Checkbox, Command


class AdvisorModelHelp(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]
    DEFAULT_CSS = """
    AdvisorModelHelp { align: center middle; }
    AdvisorModelHelp #confirm-box { width: 74; max-width: 96%; max-height: 94%; }
    AdvisorModelHelp VerticalScroll { height: auto; max-height: 58vh; }
    .compact-terminal AdvisorModelHelp #confirm-box { height: auto; max-height: 100%; padding: 1 2 0 2; }
    .compact-terminal AdvisorModelHelp VerticalScroll { height: auto; max-height: 58vh; }
    AdvisorModelHelp .setting:last-child { margin-bottom: 0; }
    AdvisorModelHelp .setting > Label { text-style: bold; }
    AdvisorModelHelp .setting { height: auto; margin: 0 0 1 0; }
    """

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('How Pi chooses an advisor model', classes='title')
            with VerticalScroll():
                with Vertical(classes='setting'):
                    yield Static('Pi follows your chat preference first, then the model set in advisor settings. For Codex and Claude, '
                                 'Pi compares model descriptions and prefers newer versions when equally suitable. '
                                 'Without a clear match, or for Antigravity, Pi uses the provider’s default.')
                with Vertical(classes='setting'):
                    yield Label('In a Pi chat', classes='section-heading')
                    for command, meaning in (
                        ('/advisor status', 'See models and quota'),
                        ('/advisor preference', 'See this chat’s preference'),
                        ('/advisor preference clear', 'Clear this chat’s preference'),
                    ):
                        yield Static(Text.assemble((command.ljust(28), 'bold'), meaning))
                with Vertical(classes='setting'):
                    yield Label('Save a preference for this chat', classes='section-heading')
                    yield Command('/advisor preference Use Claude Haiku for quick questions')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Close', variant='primary', id='advisor-help-close')

    @on(Button.Pressed, '#advisor-help-close')
    def action_close(self):
        self.dismiss()


class AdvisorChoices(Vertical):
    DEFAULT_CSS = """
    AdvisorChoices { height: auto; }
    """

    def __init__(self, providers, prefix):
        super().__init__()
        self.providers = providers
        self.prefix = prefix

    def compose(self):
        for provider, label in LABELS.items():
            with Vertical(classes='setting'):
                yield Checkbox(label, value=provider in self.providers, id=self.prefix + provider)
                copy = Checkbox('Copy credentials from host', id=self.prefix + 'copy-' + provider,
                                disabled=provider not in self.providers, classes='copy-login')
                copy.display = provider in self.providers
                yield copy
        yield Button('How Pi chooses an advisor model', classes='advisor-model-help')

    @on(Button.Pressed, '.advisor-model-help')
    def open_model_help(self, event):
        event.stop()
        self.app.push_screen(AdvisorModelHelp())

    @on(Checkbox.Changed)
    def changed(self, event):
        for provider in LABELS:
            if event.checkbox.id == self.prefix + provider:
                field = self.query_one('#' + self.prefix + 'copy-' + provider, Checkbox)
                field.disabled = not event.value
                field.display = event.value
                if not event.value:
                    field.value = False

    def selected(self):
        return [p for p in LABELS if self.query_one('#' + self.prefix + p, Checkbox).value]

    def imports(self):
        return [p for p in self.selected() if self.query_one('#' + self.prefix + 'copy-' + p, Checkbox).value]
