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
KEYWORD_HELP = 'Keyword search matches file names and text without an embedding model (used for semantic search).'
SEMANTIC_HELP = 'Semantic search uses an embedding model to find related passages, even when the wording differs.'


def fit_dialog_fields(dialog):
    box = dialog.query_one('#confirm-box', Vertical)
    fields = box.query_one(VerticalScroll)
    available = int(box.styles.max_height.resolve(dialog.size, dialog.app.size))
    reserved = box.styles.gutter.height + sum(
        child.outer_size.height + child.styles.margin.height for child in box.children if child is not fields
    )
    fields.styles.max_height = max(0, available - reserved)


class DocumentSearchHelp(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('How to search documents')
            with VerticalScroll(id='document-search-help-fields'):
                for title, text in (
                    ('1. Add your documents', 'Open Folders and add the document folder. Leave Read-only checked to protect the originals. For example, a folder named reference appears at /mnt/reference.'),
                    ('2. Choose how to search', KEYWORD_HELP + '\n\n' + SEMANTIC_HELP),
                    ('3. Ask Pi', 'For Keyword search, ask your question directly. For Semantic search, ask “Index the documents in /mnt/reference so we can search them.” Then ask your question and request source citations. To update the index after files change, ask Pi to refresh it.'),
                    ('Which profile should I use?', 'Document search works with every Pi profile. For document research, Local Knowledge adds a skill and instructions that guide Pi to index, search and cite your documents.'),
                    ('Where is the index saved?', 'The index and document excerpts are saved in this sandbox’s private home. Switching to Keyword search keeps the index. The source folder can remain read-only.'),
                    ('Which providers receive document content?', 'Your chosen embedding provider receives passages during indexing and queries during search. Your chat model receives your conversation and the document content Pi uses to answer.'),
                ):
                    with Vertical(classes='setting'):
                        yield Label(title)
                        yield Static(text, markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='close-document-search-help', variant='primary')

    def on_mount(self):
        self.call_after_refresh(fit_dialog_fields, self)

    def on_resize(self):
        self.call_after_refresh(fit_dialog_fields, self)

    @on(Button.Pressed, '#close-document-search-help')
    def action_close(self):
        self.dismiss()


def search_summary(selected, sources):
    return DOCUMENT_SEARCH_MODES[selected['knowledge_embedding']]


def search_description(selected, sources):
    mode = selected['knowledge_embedding']
    if mode == 'off':
        return 'Keyword search is selected. Choose Configure… to change the search mode.'
    if mode == 'local':
        return 'An existing offline model is selected. Choose Configure… to change the search mode.'
    model = selected['knowledge_embedding_model']
    provider = selected.get('knowledge_embedding_provider')
    source = sources.get(provider)
    location = source['label'] if source else selected['knowledge_embedding_url']
    return f'Selected embedding model: “{model}” from {location}. Choose Configure… to change the model or search mode.'


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
        self.unlisted_plugins = [row['manifest'].get('description') or row['name']
                                 for row in config.get('model_providers', []) if row['name'] not in self.sources
                                 and row['name'] != 'local']
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
        modes = [(label, mode)
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
                        yield Input(self.current['knowledge_embedding_model'], id='embedding-model')
                        yield Feedback('', id='embedding-model-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='cancel-document-search')
                yield Button('Save', id='save-document-search', variant='primary')

    def on_mount(self):
        self.configure_mode()
        self.query_one('#knowledge-embedding').focus()
        self.call_after_refresh(fit_dialog_fields, self)

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
        self.query_one('#embedding-provider-setting').display = bool(self.sources or self.missing_provider)
        self.query_one('#embedding-url-setting').display = provider == CUSTOM
        source = self.sources.get(provider)
        help_text = {
            'off': KEYWORD_HELP,
            'local': 'This sandbox already has an offline embedding model.',
            'endpoint': SEMANTIC_HELP,
        }[mode]
        if mode == 'endpoint' and self.unlisted_plugins:
            for plugin in self.unlisted_plugins:
                help_text += f'\nThis version of {plugin} does not list embedding models.'
            help_text += ' If the plugin supports embeddings, update it in Library → Providers.'
            if self.saved_config:
                help_text += ' Then choose Actions… → Update sources and recreate… for this sandbox.'
        self.query_one('#knowledge-model-help').update(help_text)
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

    def on_resize(self):
        self.call_after_refresh(fit_dialog_fields, self)

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

    def set_focus(self, widget, scroll_visible=True, from_app_focus=False):
        # Textual centers the input on a later frame. Keep its label and
        # validation message in view together when the dialog scrolls.
        field = isinstance(widget, Input) and widget.id in ('embedding-url', 'embedding-model')
        super().set_focus(widget, scroll_visible=scroll_visible and not field, from_app_focus=from_app_focus)
        if field and scroll_visible:
            self.call_after_refresh(self.reveal_field, widget)

    def reveal_field(self, field):
        viewport = self.query_one('#document-search-fields', VerticalScroll)
        # The model is the final field. Anchor to its end so the validation
        # message remains visible when adding it changes the scroll range.
        viewport.anchor(field.id == 'embedding-model')
        if field.id != 'embedding-model':
            field.parent.scroll_visible(animate=False, immediate=True)

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
                    if newly_invalid:
                        self.call_after_refresh(self.reveal_field, field)
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
