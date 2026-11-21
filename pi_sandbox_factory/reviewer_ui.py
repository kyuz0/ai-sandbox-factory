"""Select a reviewer from the models available inside this Pi sandbox."""
import asyncio
import subprocess

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from .hosted_models import PROVIDERS
from .tui_widgets import Checkbox, EntryDialog


class ReviewerPicker(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    ReviewerPicker { align: center middle; }
    ReviewerPicker > Vertical { width: 76; max-width: 96%; height: 26; max-height: 96%;
        padding: 1 2; background: #34383d; border: solid #f2b544; }
    ReviewerPicker .review-fields { height: 1fr; }
    ReviewerPicker OptionList { height: 1fr; min-height: 1; }
    ReviewerPicker .setting { height: auto; }
    """

    def __init__(self, models, current):
        super().__init__()
        self.models = models
        self.current = current
        ids = sorted({m['provider'] for m in models})
        self.providers = [(PROVIDERS.get(p, p), p) for p in ids]
        self.provider = current.partition('/')[0] if current.partition('/')[0] in ids else ids[0]

    def compose(self):
        with Vertical():
            yield Label('Select a model for approvals', classes='title')
            with Vertical(classes='review-fields'):
                with Vertical(classes='setting'):
                    yield Label('Provider')
                    yield Select(self.providers, value=self.provider, allow_blank=False, id='review-provider')
                with Vertical(classes='setting'):
                    yield Label('Search models')
                    yield Input(id='review-search')
                yield OptionList(id='review-results')
                yield Static('No matching models.', id='review-empty')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='review-cancel')
                yield Button('Use for approvals', id='review-use', variant='primary')

    def on_mount(self):
        if self.current != 'current' or len(self.providers) == 1:
            self.query_one(Input).focus()
        else:
            self.query_one(Select).focus()

    @on(Select.Changed, '#review-provider')
    def provider_changed(self):
        self.query_one(Input).value = ''
        self.filter_models()

    @on(Input.Changed)
    def filter_models(self):
        provider = self.query_one(Select).value
        query = self.query_one(Input).value.casefold()
        models = [m for m in self.models if m['provider'] == provider and
                  query in (m['id'] + ' ' + m['name']).casefold()]
        results = self.query_one(OptionList)
        results.clear_options()
        results.add_options([Option(Text(m['id']), id=m['provider'] + '/' + m['id']) for m in models])
        if models:
            results.highlighted = next((i for i, m in enumerate(models)
                                        if provider + '/' + m['id'] == self.current), 0)
        results.display = bool(models)
        self.query_one('#review-empty').display = not models
        self.query_one('#review-use', Button).disabled = not models

    @on(OptionList.OptionSelected, '#review-results')
    def use_highlighted_model(self, event):
        self.dismiss(event.option.id)

    def action_cancel(self):
        self.dismiss(None)

    @on(Input.Submitted, '#review-search')
    def confirm_search(self):
        results = self.query_one(OptionList)
        if results.highlighted is not None:
            self.dismiss(results.get_option_at_index(results.highlighted).id)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'review-cancel':
            self.action_cancel()
        else:
            results = self.query_one(OptionList)
            if results.highlighted is not None:
                self.dismiss(results.get_option_at_index(results.highlighted).id)


class ReviewerFields(Vertical):
    DEFAULT_CSS = """
    ReviewerFields { height: auto; }
    ReviewerFields #review-selection { height: auto; padding-left: 2; }
    ReviewerFields #review-selection Button { width: auto; }
    ReviewerFields #review-status, ReviewerFields #review-reopen { padding-left: 2; }
    """

    def __init__(self, name, selected, *, save_on_choice=False):
        super().__init__(classes='setting')
        self.sandbox_name = name
        self.save_on_choice = save_on_choice
        self.selected = selected
        self.original = selected
        self.models = []
        self.status = ''

    def compose(self):
        yield Checkbox('Use current model for approvals', value=self.selected == 'current',
                       disabled=self.selected == 'current', id='review-current')
        if self.save_on_choice:
            yield Static('Pi uses your chat model by default. Your choice here is saved immediately.',
                         classes='setting-help checkbox-help')
        with Vertical(id='review-selection'):
            yield Static('', id='review-selected', markup=False)
            yield Button('Select model…', id='review-choose', disabled=True)
        yield Static('', id='review-status', classes='setting-help', markup=False)
        yield Static('Reopen Pi to use this choice.', id='review-reopen', classes='setting-help')

    def on_mount(self):
        self.update_controls()

    def update_controls(self):
        current = self.selected == 'current'
        check = self.query_one(Checkbox)
        with check.prevent(Checkbox.Changed):
            check.value = current
        check.disabled = current and len(self.models) < 2
        self.query_one('#review-selection').display = not current
        provider, _, model = self.selected.partition('/')
        self.query_one('#review-selected', Static).update(f'{PROVIDERS.get(provider, provider)} · {model}')
        self.query_one('#review-choose', Button).disabled = not self.models
        status = self.query_one('#review-status', Static)
        status.update(self.status)
        status.display = bool(self.status)
        self.query_one('#review-reopen').display = not self.save_on_choice and self.selected != self.original

    @work(exclusive=True, group='review-models')
    async def load(self):
        if not self.sandbox_name:
            self.status = 'Pi can list available models once the sandbox is running. You can also choose one later in Settings → Approvals & sandbox.'
        else:
            self.status = 'Loading available models…'
            self.update_controls()
            try:
                self.models = await asyncio.to_thread(self.app.service.approval_models, self.sandbox_name)
                refs = {m['provider'] + '/' + m['id'] for m in self.models}
                if self.selected != 'current' and self.selected not in refs:
                    self.status = ('The selected model is unavailable. Choose another model or use the current model.'
                                   if refs else 'The selected model is unavailable. Use the current model or connect its provider.')
                elif len(refs) < 2:
                    self.status = 'Connect another model in Model Providers to choose a different reviewer.'
                else:
                    self.status = ''
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                self.models = []
                self.status = str(error)
        self.update_controls()

    async def select_model(self, model):
        if self.save_on_choice and model is not None and model != self.selected:
            self.disabled = True
            try:
                await asyncio.to_thread(self.app.service.save_approval_model, self.sandbox_name, model, lambda _: None)
                self.notify('Approval model saved.')
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                self.status = str(error)
                model = None
            finally:
                self.disabled = False
        if model is not None:
            self.selected = model
            self.status = ''
        self.update_controls()

    def choose(self):
        self.app.push_screen(ReviewerPicker(self.models, self.selected), self.select_model)

    @on(Checkbox.Changed, '#review-current')
    async def changed(self, event):
        event.stop()
        if event.value:
            await self.select_model('current')
        elif len(self.models) >= 2:
            self.choose()

    @on(Button.Pressed, '#review-choose')
    def open_picker(self, event):
        event.stop()
        self.choose()
