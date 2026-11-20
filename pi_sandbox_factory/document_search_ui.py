"""Configure document search without changing the sandbox until the parent form applies."""
import asyncio
import subprocess
from typing import Optional

from textual import events, on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from . import embedding_models
from .sandbox_options import embedding_model, embedding_url
from .settings_catalog import CATEGORIES
from .tui_widgets import Feedback

DOCUMENT_SEARCH_MODES = {'off': 'Keyword search', 'endpoint': 'Semantic search', 'local': 'Existing offline model'}
CUSTOM = '@custom'
TYPED = object()


def search_summary(selected, sources):
    mode = DOCUMENT_SEARCH_MODES[selected['knowledge_embedding']]
    provider = selected.get('knowledge_embedding_provider')
    if selected['knowledge_embedding'] != 'endpoint' or not provider:
        return mode
    label = sources.get(provider, {}).get('label', 'Custom server' if provider == CUSTOM else provider)
    return f'{mode} · {label}'


class DocumentSearchDialog(ModalScreen[Optional[dict]]):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, current, allow_offline, config, credentials, saved_config):
        super().__init__()
        self.current = current
        self.allow_offline = allow_offline
        self.config = config
        self.credentials = credentials
        self.saved_config = saved_config
        self.sources = embedding_models.sources(config)
        provider = current.get('knowledge_embedding_provider')
        if not provider and current['knowledge_embedding_url']:
            matches = [key for key, source in self.sources.items() if source['url'] == current['knowledge_embedding_url']]
            provider = matches[0] if len(matches) == 1 else CUSTOM
        self.initial_provider = provider or next(iter(self.sources), CUSTOM)
        self.active_provider = None
        self.catalogues = {}
        self.model_choices = {self.initial_provider: current['knowledge_embedding_model']}
        self.missing_provider = self.initial_provider != CUSTOM and self.initial_provider not in self.sources

    def compose(self):
        modes = [('Semantic search with pi-knowledge' if mode == 'endpoint' else label, mode)
                 for mode, label in DOCUMENT_SEARCH_MODES.items() if mode != 'local' or self.allow_offline]
        providers = [(value['label'], key) for key, value in self.sources.items()]
        if self.missing_provider:
            providers.append((f'{self.initial_provider} (not configured)', self.initial_provider))
        providers.append(('Custom server', CUSTOM))
        with Vertical(id='confirm-box'):
            yield Label(CATEGORIES['document-search'].label)
            with VerticalScroll(id='document-search-fields', classes='spaced-form-fields'):
                with Vertical(classes='setting', id='embedding-mode-setting'):
                    yield Label('Search mode')
                    yield Select(modes, value=self.current['knowledge_embedding'], allow_blank=False, id='knowledge-embedding')
                    yield Static('', id='knowledge-model-help', classes='setting-help', markup=False)
                with Vertical(id='embedding-server-fields'):
                    with Vertical(classes='setting', id='embedding-provider-setting'):
                        yield Label('Embedding provider')
                        yield Select(providers, value=self.initial_provider, allow_blank=False, id='embedding-provider')
                        yield Feedback('', id='embedding-provider-error', markup=False)
                    with Vertical(classes='setting', id='embedding-url-setting'):
                        yield Label('Embedding server URL')
                        url = self.current['knowledge_embedding_url'] if self.initial_provider == CUSTOM else ''
                        endpoint = Input(url, placeholder='https://server.example/v1', id='embedding-url')
                        endpoint.tooltip = 'OpenAI-compatible API base URL, including its API path.'
                        yield endpoint
                        yield Feedback('', id='embedding-url-error', markup=False)
                    with Vertical(classes='setting', id='embedding-catalogue-setting'):
                        yield Label('Embedding model')
                        yield Select([], prompt='Choose a model', id='embedding-catalogue')
                    with Horizontal(id='embedding-catalogue-status'):
                        yield Static('', id='embedding-catalogue-message', classes='setting-help', markup=False)
                        yield Button('Retry', id='retry-embedding-catalogue')
                    with Vertical(classes='setting', id='embedding-model-setting'):
                        yield Label('Embedding model ID')
                        yield Input(self.current['knowledge_embedding_model'], placeholder='Model ID from your provider', id='embedding-model')
                        yield Feedback('', id='embedding-model-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='cancel-document-search')
                yield Button('Save', id='save-document-search', variant='primary')

    def on_mount(self):
        self.configure_mode()
        self.query_one('#knowledge-embedding').focus()

    @on(Button.Pressed, '#cancel-document-search')
    def action_cancel(self):
        self.dismiss(None)

    @on(Select.Changed, '#knowledge-embedding')
    @on(Select.Changed, '#embedding-provider')
    def configure_mode(self):
        mode = self.query_one('#knowledge-embedding', Select).value
        self.set_class(mode != 'endpoint', '-simple-mode')
        self.query_one('#embedding-server-fields').display = mode == 'endpoint'
        provider = self.query_one('#embedding-provider', Select).value
        self.set_class(mode == 'endpoint' and provider == CUSTOM, '-custom-server')
        self.update_heading()
        self.query_one('#embedding-provider-setting').display = bool(self.sources or self.missing_provider)
        self.query_one('#embedding-url-setting').display = provider == CUSTOM
        source = self.sources.get(provider)
        self.query_one('#knowledge-model-help').update({
            'off': 'Pi searches file names and text. No embedding model is needed.',
            'local': 'This sandbox already has an offline embedding model.',
            'endpoint': 'An embedding model can be used to find passages even when they use different words from your search.',
        }[mode])
        missing = provider != CUSTOM and not source
        self.query_one('#embedding-provider-error', Feedback).update(
            'This provider is no longer configured. Choose another provider or a custom server.' if missing else '')
        active = provider if mode == 'endpoint' else None
        if active != self.active_provider:
            if self.active_provider is not None:
                self.model_choices[self.active_provider] = self.model_id()
            self.active_provider = active
            self.query_one('#embedding-url-error', Feedback).update('')
            self.query_one('#embedding-model-error', Feedback).update('')
            self.show_models(self.catalogues.get(active, []), self.model_choices.get(active, ''))
            if active in self.catalogues and not self.catalogues[active]:
                self.show_empty_catalogue()
            else:
                self.status('')
                if active in self.sources and active not in self.catalogues:
                    self.load_catalogue(active)
        self.validate_save()

    def update_heading(self):
        compact_custom = self.app.size.height < 32 and self.has_class('-custom-server')
        box = self.query_one('#confirm-box', Vertical)
        box.border_title = CATEGORIES['document-search'].label if compact_custom else ''
        box.query_one(Label).display = not compact_custom

    def on_resize(self):
        self.update_heading()

    def model_id(self):
        picker = self.query_one('#embedding-catalogue', Select)
        if self.query_one('#embedding-catalogue-setting').display and picker.value != TYPED:
            return '' if picker.value is Select.NULL else str(picker.value)
        return self.query_one('#embedding-model', Input).value.strip()

    def show_models(self, models, selected):
        picker = self.query_one('#embedding-catalogue', Select)
        self.query_one('#embedding-catalogue-setting').display = bool(models)
        self.query_one('#embedding-model', Input).value = selected
        if models:
            choices = [(model, model) for model in models]
            if selected and selected not in models:
                choices.insert(0, (selected, selected))
            picker.set_options([*choices, ('Enter model ID…', TYPED)])
            picker.value = selected or Select.NULL
        self.model_selection_changed()

    @on(Select.Changed, '#embedding-catalogue')
    def model_selection_changed(self):
        picker = self.query_one('#embedding-catalogue', Select)
        manual = not self.query_one('#embedding-catalogue-setting').display or picker.value == TYPED
        self.query_one('#embedding-model-setting').display = manual and (
            self.active_provider == CUSTOM or self.active_provider in self.sources)
        self.validate_save()

    def status(self, message, *, retry=False):
        self.query_one('#embedding-catalogue-status').display = bool(message)
        self.query_one('#embedding-catalogue-message').update(message)
        self.query_one('#retry-embedding-catalogue').display = retry

    def show_empty_catalogue(self):
        self.status('No embedding models listed. Enter an embedding model ID or retry.', retry=True)

    @on(Button.Pressed, '#retry-embedding-catalogue')
    def retry(self):
        if self.active_provider in self.sources:
            self.load_catalogue(self.active_provider)

    @work(exclusive=True, group='embedding-catalogue')
    async def load_catalogue(self, provider):
        self.status('Loading embedding models…')
        try:
            models = await asyncio.to_thread(embedding_models.catalogue, self.config, provider,
                                            choice=self.credentials.get(provider), saved_config=self.saved_config)
        except (OSError, ValueError, subprocess.SubprocessError):
            if self.is_mounted and provider == self.active_provider:
                self.status('Could not load models. Enter an embedding model ID or retry.', retry=True)
            return
        if not self.is_mounted or provider != self.active_provider:
            return
        self.catalogues[provider] = models
        typed = self.model_id()
        typing = self.focused is self.query_one('#embedding-model', Input) and bool(typed)
        self.show_models(models, typed)
        if models:
            if typing:
                self.query_one('#embedding-catalogue', Select).value = TYPED
            self.status('')
        else:
            self.show_empty_catalogue()

    @on(Input.Changed, '#embedding-url')
    @on(Input.Changed, '#embedding-model')
    def field_changed(self, event):
        self.validate_save(field=event.input if event.input.value else None)

    @on(events.DescendantBlur, '#embedding-url')
    @on(events.DescendantBlur, '#embedding-model')
    def field_blurred(self, event):
        self.validate_save(field=event.widget)

    def validate_save(self, field=None):
        mode = self.query_one('#knowledge-embedding', Select).value
        provider = self.query_one('#embedding-provider', Select).value
        valid = True
        if mode == 'endpoint':
            valid = provider == CUSTOM or provider in self.sources
            checks = [('model', self.model_id(), embedding_model)]
            if provider == CUSTOM:
                checks.append(('url', self.query_one('#embedding-url', Input).value.strip(), embedding_url))
            for name, value, validator in checks:
                error = ''
                try:
                    validator(value)
                except ValueError as exc:
                    valid = False
                    error = str(exc)
                if field is not None and field.id == 'embedding-' + name:
                    feedback = self.query_one('#embedding-' + name + '-error', Feedback)
                    newly_invalid = bool(error) and not feedback.display
                    feedback.update(error)
                    if newly_invalid and field.has_focus:
                        self.call_after_refresh(field.parent.scroll_visible, animate=False)
        self.set_class(any(feedback.display and all(parent.display for parent in feedback.ancestors)
                           for feedback in self.query(Feedback)), '-field-error')
        self.query_one('#save-document-search', Button).disabled = not valid

    @on(Button.Pressed, '#save-document-search')
    def save(self):
        mode = self.query_one('#knowledge-embedding', Select).value
        values = dict(knowledge_embedding=mode, knowledge_embedding_provider='',
                      knowledge_embedding_url='', knowledge_embedding_model='')
        if mode == 'endpoint':
            provider = self.query_one('#embedding-provider', Select).value
            if provider != CUSTOM and provider not in self.sources:
                return
            url = self.query_one('#embedding-url', Input).value if provider == CUSTOM else self.sources[provider]['url']
            values.update(knowledge_embedding_provider=provider, knowledge_embedding_url=embedding_url(url.strip()),
                          knowledge_embedding_model=embedding_model(self.model_id()))
        self.dismiss(values)
