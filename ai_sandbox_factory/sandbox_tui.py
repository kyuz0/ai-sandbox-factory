"""Terminal interface for AI Sandbox Factory."""
from . import activity
from . import cli_recipes
from .cli_recipes_ui import CLICommands
import json
import copy
import asyncio
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from textwrap import shorten
import pyfiglet
import shutil
from rich.style import Style
from rich.text import Text
from typing import NamedTuple, Optional
from inspect import isawaitable

from . import sandbox, editor_settings, sandbox_ssh, prompt_library, system_packages, profile_spec, profile_services, profile_runtime, profile_edit
from .profile_edit_ui import EditProfile
from .service_data_ui import SavedServiceData
from . import chat_history
from .sandbox_jobs import CreationJob, Job, RebuildJob
from .sandbox_jobs_ui import JobDetails, JobsMenu
from .chat_history_ui import ChatTransfer, DeleteSandbox, EXPORT, IMPORT
from .advisors_ui import AdvisorChoices
from .editor_ui import EditorFields, EditorScreen, OpenWith
from .settings_catalog import (
    ADVISORS_LABEL, ADVISORS_PATH, CATEGORIES, EDIT_FORMS, Category,
    HOSTED_PROVIDERS_LABEL, INSTALL_SYSTEM_PACKAGES, LOCAL_MODELS_LABEL, LOCAL_MODELS_PATH,
    MODEL_PROVIDERS_PATH, NETWORK_LABEL, NETWORK_MODE_LABEL, NETWORK_PATH,
    NO_SYSTEM_PROMPT_ADD_ON, PROVIDER_PLUGINS_LABEL, SYSTEM_PROMPT_LABEL, SYSTEM_PROMPT_LIBRARY_LABEL, WEB_SEARCH_LABEL,
)
from .folders_ui import FolderFields
from .file_browser import FolderPicker, PathField
from .communications_ui import CommunicationsForm, communications_summary
from .connector_dialogs import ConnectorSettings, ExternalConnectorDialog
from .connectors import ADAPTERS, SSH_GIT_LABEL
from .models_ui import HostedProviders, ModelDialog, hosted_provider_names, local_models_title
from .credentials_ui import (CredentialDialog, LocalKeysDialog, LOCAL_KEYS_LABEL,
                             assigned_key_label, choice_key_caption, key_caption)
from .repository_ui import SSHDestinationFields
from .connector_fields import ConnectorFields, connector_summary
from . import hosted_models, inference, provider_credentials
from .tui_widgets import WrappingCollapsible, Bullet, Command, Details, EntryDialog, Checkbox, Feedback
from .aws_ui import AWSProfileChoices, AWSProfilePicker
from .document_search_ui import CUSTOM, DocumentSearchDialog, DocumentSearchHelp, search_description, search_summary
from . import embedding_models
from . import sandbox_skills
from .primary_workspace import AGENTS as PRIMARY_AGENTS, DEFAULT_AUTH, WEB_SEARCH_OPTIONS, agent as coding_agent
from .skills_ui import SkillLibrary, SkillSelection, saved_skills_summary
from .prompt_ui import PromptChoice, PromptLibrary, SandboxPromptScreen
from .reviewer_ui import ReviewerFields
from . import image_sources, settings_effects
from . import curlie_catalog
from . import host_platform, registry, model_providers, updates, web_access, network_groups, communications
from . import operation_locks
from .updating import Update
from .sandbox_options import (APPROVAL_MODES, DEFAULTS, MAIN_AGENT_SANDBOX, SKILLS, PROFILE_SKILLS, options,
                              endpoint_rule, memory_limit, write_json, SANDBOX_NAME,
                              SANDBOX_NAME_ERROR, SANDBOX_NAME_LIMIT)

try:
    from textual import events, on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.coordinate import Coordinate
    from textual.message import Message
    from textual.screen import ModalScreen, Screen
    from textual.strip import Strip
    from textual.theme import Theme
    from textual.worker import get_current_worker
    from textual.widgets import (Button, Collapsible, DataTable, Footer, Header,
                                 Input, Label, Markdown, RichLog, Select, Static, TextArea, OptionList,
                                 TabbedContent, TabPane, Tabs)
except ImportError as error:
    raise SystemExit('Install AI Sandbox Factory with pipx install git+https://github.com/kyuz0/ai-sandbox-factory.git.') from error

CHOOSE_MODEL = 'Choose a model'
SIGN_IN_LABEL = 'Sign-in'
SIGN_IN_OPTIONS = [
    ('Copy sign-in from this computer', 'copy'),
    ('Sign in inside the sandbox', 'login'),
]
SIGN_IN_HELP = {
    'login': 'The first launch opens the agent’s sign-in flow.',
    'copy': 'Uses the account signed in on this computer and its subscription.',
}
PRIMARY_CATEGORIES = {
    'primary-sign-in': Category(SIGN_IN_LABEL, 'primary-sign-in-section'),
    'editor-settings': CATEGORIES['editor-settings'],
    'folders': CATEGORIES['folders'],
    'network-settings': Category(NETWORK_LABEL, 'network-section', 'network'),
    'skills': CATEGORIES['skills'],
    'primary-approvals': Category('Approvals', 'primary-approvals-section', 'native-approval'),
    'primary-browser': Category('Browser', 'primary-browser-section', 'primary-browser'),
    'system-prompt': CATEGORIES['system-prompt'],
    'connectors': CATEGORIES['connectors'],
    'resources': CATEGORIES['resources'],
}


def primary_categories(agent):
    categories = {key: category for key, category in PRIMARY_CATEGORIES.items()
                  if key != 'editor-settings' or len(editor_settings.modes(agent)) > 1}
    if agent in WEB_SEARCH_OPTIONS:
        categories['network-settings'] = Category(CATEGORIES['network-settings'].label, 'network-section', agent + '-web-search')
    return categories

def can_copy_primary_sign_in(agent):
    return not (host_platform.is_mac() and agent in ('claude', 'agy'))


def settings_client(config, state):
    return editor_settings.load(state)['mode'] if coding_agent(config) == 'pi' else None


from .operations import (
    Service, DEFAULT_SETTINGS, profile_titles, installed_profile_title, SandboxAlert, sandbox_alerts,
    safe_text, defaults_path, search_keys_path, save_defaults_with_search, save_web_defaults,
    load_defaults, preset, copy_draft, shares_editable_profile, has_web_search_keys, copy_for_agent,
)


class WebsiteGroups(Vertical):
    DEFAULT_CSS = """
    WebsiteGroups, .website-category-group { height: auto; }
    .website-category-heading { height: 1; margin: 0 0 1 0; }
    .website-category-heading > Label { width: 22; }
    .website-category-heading > Checkbox { width: auto; margin: 0; }
    WebsiteGroups #refresh-curlie { margin: 1 0 0 0; }
    """
    GROUPS = (
        ('dev', 'Developer services', ('dev-',)),
        ('auth', 'Authentication', ('auth-',)),
        ('ut1', 'Topics', ('ut1-', 'curlie-')),
    )

    def __init__(self, names, *, assigned=None):
        super().__init__()
        self.names = names
        self.assigned = assigned
        self.available_list = None

    def compose(self):
        with Collapsible(title='Website categories · optional', collapsed=True):
            yield Static('Selected categories allow HTTP and HTTPS traffic. The catalog combines lists from '
                         'V2Fly, UT1 Toulouse and Curlie with lists curated by Factory.',
                         id='website-category-sources', classes='section-intro')
            catalog = network_groups.catalog()
            for prefix, title, sources in self.GROUPS:
                entries = [(name, group) for name, group in catalog.items() if name.startswith(sources)]
                with Vertical(id='website-category-' + prefix, classes='website-category-group'):
                    with Horizontal(classes='website-category-heading'):
                        yield Label(title, classes='group-title')
                        yield Checkbox('Allow all', value=all(name in self.names for name, _ in entries),
                                       id='category-all-' + prefix)
                    if prefix == 'ut1':
                        yield Static('News and press combines UT1 Toulouse and Curlie. Financial information, '
                                     'Banking and Library resources use UT1 Toulouse. The other topics use Curlie.',
                                     id='website-topic-sources', classes='section-intro')
                        yield Static('UT1 Toulouse updates are checked when you start a sandbox or apply '
                                     'network rules, at most once a day.',
                                     classes='section-intro')
                        with Vertical(classes='setting'):
                            yield Label('Curlie website list')
                            yield Static('', id='curlie-status', classes='setting-help')
                            yield Button('Download Curlie list', id='refresh-curlie')
                            yield Feedback('', id='curlie-error', markup=False)
                        with Vertical(classes='setting', id='use-newer-curlie-setting'):
                            yield Checkbox('Use newer list', id='use-newer-curlie')
                            yield Static('Choose Apply to use the newer list in this sandbox.',
                                         classes='setting-help checkbox-help')
                    for name, group in entries:
                        with Vertical(classes='setting'):
                            yield Checkbox(group['title'], value=name in self.names, id='category-' + name,
                                           classes='website-category-member')
                            yield Static(group['description'], classes='setting-help checkbox-help')

    def on_mount(self):
        self.available_list = curlie_catalog.metadata()
        self.update_title()
        self.show_curlie_status()

    def show_curlie_status(self):
        def date(info):
            return datetime.fromtimestamp(info['downloaded_at']).strftime('%d %b %Y at %H:%M')

        latest = self.available_list
        used = self.assigned
        selected = self.uses_curlie()
        newer = bool(used and latest and latest['downloaded_at'] > used['downloaded_at'])
        if used:
            text = f'This sandbox uses the list downloaded on {date(used)}.'
            if newer:
                text += f'\nA newer list downloaded on {date(latest)} is available.'
        elif latest:
            text = (f'This sandbox will use the list downloaded on {date(latest)}.' if selected else
                    f'The saved list was downloaded on {date(latest)}.')
        elif selected and not self.screen.defaults_mode:
            action = 'apply these settings' if self.screen.editing else 'create this sandbox'
            text = f'The list downloads when you {action}. You can download it in advance.'
        else:
            text = 'There’s no saved Curlie list. You can download it in advance.'
        self.query_one('#use-newer-curlie-setting').display = newer and selected
        button = self.query_one('#refresh-curlie', Button)
        if not button.disabled:
            if self.query_one('#curlie-error', Feedback).display:
                button.label = 'Retry'
            else:
                button.label = 'Check for newer list' if latest else 'Download Curlie list'
        self.query_one('#curlie-status', Static).update(text)
        self.screen.update_curlie_download_notice()

    @on(Button.Pressed, '#refresh-curlie')
    def check_curlie(self, event):
        event.stop()
        button = event.button
        if button.disabled or self.screen is not self.app.screen:
            return
        self.query_one('#curlie-error', Feedback).update('')
        if self.available_list:
            button.disabled = True
            button.label = 'Checking…'
            self.find_curlie_update()
        else:
            self.confirm_curlie_download()

    @work(exit_on_error=False)
    async def find_curlie_update(self):
        button = self.query_one('#refresh-curlie', Button)
        try:
            update = await asyncio.to_thread(curlie_catalog.check)
            if not self.is_attached:
                return
            if update and self.screen is self.app.screen:
                size = update.get('size') or ''
                self.confirm_curlie_download(f'about {int(size) / 1_000_000:.0f} MB' if size.isdecimal()
                                            else curlie_catalog.DOWNLOAD_SIZE)
            elif not update:
                self.notify('You have the latest Curlie list.')
        except ValueError as error:
            if self.is_attached:
                self.query_one('#curlie-error', Feedback).update(str(error))
        finally:
            if self.is_attached:
                button.disabled = False
                self.show_curlie_status()

    def confirm_curlie_download(self, size=curlie_catalog.DOWNLOAD_SIZE):
        latest = self.available_list
        message = (f'Curlie has a newer list. Download it ({size})?' if latest else
                   f'Download the Curlie website list ({size})?')
        if self.assigned:
            message += '\nThis sandbox keeps its list until you select Use newer list and choose Apply.'
        else:
            message += '\nNew sandboxes will use the saved list.'
        self.app.push_screen(Confirm(message, 'Download newer list' if latest else 'Download list',
            confirm_by_default=True, cancel_label='Keep saved list' if latest else 'Cancel'),
            lambda yes: self.download_curlie() if yes else None)

    def download_curlie(self):
        app, owner = self.app, self.screen

        def loaded(version):
            if app.screen is owner:
                self.available_list = version
                self.query_one('#use-newer-curlie', Checkbox).value = False
                self.show_curlie_status()

        app.start_task(None, 'Download Curlie website list', lambda log: curlie_catalog.refresh(force=True, log=log),
                       working='Downloading website list', on_success=loaded, followup_label='Return to website groups',
                       targets=('factory:curlie',), success_message='The saved list is ready for new sandboxes.')

    def curlie_revision(self):
        if not self.available_list or not self.uses_curlie():
            return None
        control = self.query_one('#use-newer-curlie', Checkbox)
        if not self.assigned or (control.parent.display and control.value):
            return self.available_list['revision']
        return None

    def uses_curlie(self):
        return any(network_groups.catalog()[name].get('curlie') for name in self.selected())

    def needs_curlie_download(self):
        return not self.available_list and not self.assigned and self.uses_curlie()

    @on(Checkbox.Changed)
    def selection_changed(self, event):
        control = event.checkbox
        if control.id == 'use-newer-curlie':
            return
        group = control.query_ancestor('.website-category-group')
        bulk = group.query_one('.website-category-heading Checkbox', Checkbox)
        members = group.query('Checkbox.website-category-member')
        if control is bulk:
            for member in members:
                with member.prevent(Checkbox.Changed):
                    member.value = event.value
        else:
            with bulk.prevent(Checkbox.Changed):
                bulk.value = all(member.value for member in members)
        self.update_title()
        self.show_curlie_status()

    def update_title(self):
        count = len(self.selected())
        self.query_one(Collapsible).title = 'Website categories · ' + (f'{count} selected' if count else 'optional')

    def selected(self):
        return [name for name in network_groups.catalog() if self.query_one('#category-' + name, Checkbox).value]


class SearchProviderDialog(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    SearchProviderDialog { align: center middle; background: #000000 70%; }
    #search-provider-dialog { width: 72; max-width: 95%; height: 25; max-height: 95%; padding: 1 2; background: #34383d; border: solid #f2b544; }
    #search-provider-dialog Static { height: auto; }
    #search-provider-fields { height: 1fr; }
    """

    def __init__(self, providers, name=None, selected_mode='auto'):
        super().__init__()
        self.providers, self.provider_name, self.selected_mode = providers, name, selected_mode
        self.original = providers.get(name, {})

    def compose(self):
        choices = [name for name in web_access.SERVICES if name == self.provider_name or name not in self.providers]
        name = self.provider_name or choices[0]
        with Vertical(id='search-provider-dialog'):
            yield Label('Edit search provider' if self.provider_name else 'Add search provider', classes='title')
            with VerticalScroll(id='search-provider-fields'):
                yield Label('Search provider')
                yield Select([(web_access.SERVICES[n][0], n) for n in choices], value=name, allow_blank=False, disabled=bool(self.provider_name), id='search-provider-name')
                with Vertical(id='search-endpoint-field', classes='setting'):
                    yield Label('Endpoint URL')
                    yield Input(self.original.get('endpoint', ''), id='search-endpoint')
                    yield Static('', id='search-endpoint-help', classes='setting-help')
                with Vertical(id='search-model-field', classes='setting'):
                    yield Label('Search model · optional')
                    yield Input(self.original.get('model', ''), id='search-model')
                with Vertical(id='search-key-field', classes='setting'):
                    yield Label('API key · leave empty to keep the saved key' if self.original.get('has_key') else 'API key', id='search-key-label')
                    yield Input(password=True, id='search-key')
                    yield Static('', id='search-key-status', classes='setting-help')
                    yield Checkbox('Remove saved key', id='search-remove-key')
            yield Feedback('', id='search-provider-error', markup=False, classes='error')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='search-cancel')
                if self.provider_name:
                    yield Button('Remove provider', id='search-remove')
                yield Button('Save', id='search-save', variant='primary')

    def on_mount(self):
        self.sync_fields()

    @on(Select.Changed, '#search-provider-name')
    def sync_fields(self):
        name = self.query_one('#search-provider-name', Select).value
        self.query_one('#search-endpoint-field').display = name in ('openai', 'searxng')
        self.query_one('#search-model-field').display = name == 'openai'
        self.query_one('#search-key-field').display = name != 'searxng'
        self.query_one('#search-endpoint-help', Static).update('Required: your SearXNG server URL.' if name == 'searxng' else 'Optional: a custom OpenAI Responses URL.')
        self.query_one('#search-remove-key', Checkbox).display = bool(self.original.get('has_key'))
        self.query_one('#search-key-status', Static).update('API key configured' if self.original.get('has_key') else 'Optional: leave empty to use Exa without a key.' if name == 'exa' else 'API key missing · inactive')

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'search-cancel':
            self.dismiss(None)
            return
        name = self.query_one('#search-provider-name', Select).value
        if event.button.id == 'search-remove':
            if name == self.selected_mode:
                self.query_one('#search-provider-error', Static).update('Choose another search mode before removing this provider.')
            else:
                self.dismiss((name, None, None))
            return
        try:
            endpoint = self.query_one('#search-endpoint', Input).value.strip() if name in ('openai', 'searxng') else ''
            has_key = self.original.get('has_key', False) and endpoint == self.original.get('endpoint', '')
            key = None
            if name != 'searxng':
                entered = self.query_one('#search-key', Input).value.strip()
                removed = self.query_one('#search-remove-key', Checkbox).value
                if removed and entered:
                    raise ValueError('Enter a replacement key or select Remove saved key.')
                if entered or removed:
                    key = '' if removed else entered
                    has_key = bool(key)
            row = web_access.provider_settings(name, {'endpoint': endpoint, 'has_key': has_key,
                'model': self.query_one('#search-model', Input).value if name == 'openai' else ''})
            self.dismiss((name, row, key))
        except ValueError as error:
            self.query_one('#search-provider-error', Static).update(str(error))


class CopyWebAccess(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    CopyWebAccess { align: center middle; background: #000000 70%; }
    #copy-web-dialog { width: 72; max-width: 95%; height: auto; max-height: 95%; padding: 1 2; background: #34383d; border: solid #f2b544; }
    #copy-web-dialog Static { height: auto; }
    """

    def __init__(self, records):
        super().__init__()
        self.records = {record['name']: record for record in records}

    def compose(self):
        with VerticalScroll(id='copy-web-dialog'):
            yield Label(f'Copy {WEB_SEARCH_LABEL}', classes='title')
            with Vertical(classes='setting'):
                yield Label('Source sandbox')
                yield Select([(name, name) for name in self.records], value=next(iter(self.records)), allow_blank=False, id='copy-web-source')
            yield Static('Copies Web Search settings into this form. The source sandbox is unchanged.', classes='setting-help')
            with Vertical(classes='setting', id='copy-web-key-setting'):
                yield Checkbox('Include Web Search API keys', value=True, id='copy-web-keys')
                yield Static('', id='copy-web-help', markup=False, classes='setting-help')
            yield Feedback('', id='copy-web-error', markup=False, classes='error')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='copy-web-cancel')
                yield Button('Copy configuration', id='copy-web-apply', variant='primary')

    def on_mount(self):
        self.source_changed()

    @on(Select.Changed, '#copy-web-source')
    def source_changed(self):
        record = self.records[self.query_one('#copy-web-source', Select).value]
        checkbox = self.query_one('#copy-web-keys', Checkbox)
        checkbox.display = has_web_search_keys(record['config'])
        self.query_one('#copy-web-key-setting').display = checkbox.display
        checkbox.disabled = record['dev'] != 'running'
        checkbox.value = False
        self.query_one('#copy-web-help', Static).display = checkbox.display and checkbox.disabled
        self.query_one('#copy-web-help', Static).update(
            'Start the source sandbox to copy its Web Search API keys.' if checkbox.disabled else
            'Copies provider settings and selected keys into this form. The source sandbox is unchanged.')

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        if event.button.id == 'copy-web-cancel':
            self.dismiss(None)
            return
        button = self.query_one('#copy-web-apply', Button)
        button.disabled = True
        try:
            record = self.records[self.query_one('#copy-web-source', Select).value]
            value = web_access.settings(options(record['config'].get('options'))['web_access'])
            choice = self.query_one('#copy-web-keys', Checkbox)
            include_keys = choice.display and not choice.disabled and choice.value
            keys = {}
            if include_keys:
                stored = await asyncio.to_thread(self.app.service.search_keys, record['name'])
                keys = web_access.matching_keys(stored, value)
            for name, row in value['providers'].items():
                row['has_key'] = bool(keys.get(name))
                if name != 'searxng':
                    keys.setdefault(name, '')  # Clear matching credentials in an edited target sandbox.
            self.dismiss((value, keys if include_keys else None))
        except (OSError, ValueError) as error:
            self.query_one('#copy-web-error', Static).update(str(error))
            button.disabled = False


class WebAccessForm(Vertical):
    DEFAULT_CSS = """
    WebAccessForm, #web-fields, #web-provider-list { height: auto; }
    .search-provider-row { height: auto; min-height: 4; margin-bottom: 1; max-width: 80; padding: 1 2; background: #343a42; border-left: solid #707780; }
    #web-fields > .buttons { margin: 0 0 1 0; }
    .search-provider-row Static { width: 1fr; height: auto; }
    """

    def __init__(self, value, defaults=False):
        super().__init__()
        self.value, self.defaults_mode = web_access.settings(value), defaults
        self.providers = self.value['providers']
        self.keys = {}

    def compose(self):
        with Collapsible(title=WEB_SEARCH_LABEL, collapsed=True, id='web-section'):
            with Vertical(classes='setting', id='web-enable-setting'):
                yield Checkbox(f'Enable {WEB_SEARCH_LABEL}', value=self.value['enabled'], id='web-enabled')
                yield Static('Search provider endpoints are allowed automatically. Reading pages uses your network rules and website categories.', classes='setting-help checkbox-help')
            with Vertical(id='web-fields'):
                with Vertical(classes='setting'):
                    yield Label('Search mode')
                    yield Select(self.mode_choices(), value=self.value['mode'], allow_blank=False, id='web-mode')
                    yield Feedback('', id='web-config-error', markup=False)
                yield Vertical(id='web-provider-list')
                with Horizontal(classes='buttons'):
                    yield Button('Add provider', id='web-add')
                    yield Button('Copy from sandbox…', id='web-copy')
                with Vertical(classes='setting', id='web-defaults-setting'):
                    yield Button(f'Save as default {WEB_SEARCH_LABEL}', id='web-save-defaults')
                    yield Static('Includes API keys, stored privately on this workstation for new sandboxes.', classes='setting-help')
                yield Static('', id='web-active', markup=False, classes='setting-help')

    def copy_sources(self):
        current = self.screen.draft.get('name') if getattr(self.screen, 'editing', False) else None
        return [record for record in self.app.records if record['name'] != current
                and options(record['config'].get('options'))['web_access']['enabled']]

    async def copied(self, result):
        if result is None:
            return
        value, keys = result
        if keys is None:
            keys = {}
            for name, row in value['providers'].items():
                previous = self.providers.get(name, {})
                if getattr(self.screen, 'editing', False) and previous.get('endpoint') == row['endpoint']:
                    row['has_key'] = previous.get('has_key', False)
                    if name in self.keys:
                        keys[name] = self.keys[name]
                elif name != 'searxng':
                    keys[name] = ''
        self.value, self.keys = value, keys
        self.providers = self.value['providers']
        self.query_one('#web-enabled', Checkbox).value = self.value['enabled']
        mode = self.query_one('#web-mode', Select)
        mode.set_options(self.mode_choices())
        mode.value = self.value['mode']
        await self.refresh_providers()

    def mode_choices(self):
        return [('Automatic fallback', 'auto')] + [('Only ' + web_access.SERVICES[name][0], name) for name in self.providers]

    async def on_mount(self):
        if getattr(self.screen, 'setting', None) not in (None, 'network-settings'):
            return
        if not getattr(self.screen, 'editing', False):
            supplied = getattr(self.screen, 'search_keys', None)
            self.keys = dict(supplied) if supplied is not None else web_access.saved_keys(search_keys_path(), self.value)
            for name, row in self.providers.items():
                row['has_key'] = bool(self.keys.get(name))
        await self.refresh_providers()
        self.sync_fields()
        self.query_one('#web-copy', Button).disabled = not self.copy_sources()
        self.query_one('#web-defaults-setting').display = not self.defaults_mode
        if getattr(self.screen, 'editing', False) and hasattr(self.app.service, 'search_status'):
            self.refresh_key_status(self.screen.draft['name'])

    @work(exclusive=True)
    async def refresh_key_status(self, name):
        original = {provider: dict(row) for provider, row in self.providers.items()}
        try:
            statuses = await asyncio.to_thread(self.app.service.search_status, name)
        except (OSError, ValueError):
            return
        if statuses is None:
            return
        for provider, row in self.providers.items():
            if row == original.get(provider) and provider not in self.keys:
                row['has_key'] = row['has_key'] and statuses.get(provider, False)
        await self.refresh_providers()

    async def refresh_providers(self):
        listing = self.query_one('#web-provider-list', Vertical)
        await listing.remove_children()
        mode = self.query_one('#web-mode', Select).value
        for name, row in self.providers.items():
            status = ('API key configured' if row['has_key'] else 'No API key required') if name == 'exa' else ('Server configured' if row['endpoint'] else 'Server URL missing · inactive') if name == 'searxng' else ('API key configured' if row['has_key'] else 'API key missing · inactive')
            if mode not in ('auto', name):
                status += ' · saved, not used'
            await listing.mount(Horizontal(Static(web_access.SERVICES[name][0] + '\n' + status, markup=False), Button('Edit', id='web-edit-' + name), classes='search-provider-row'))
        self.query_one('#web-add', Button).disabled = len(self.providers) == len(web_access.SERVICES)
        names = [web_access.SERVICES[n][0] for n, row in self.providers.items() if mode in ('auto', n) and web_access.ready(n, row)]
        self.query_one('#web-active', Static).update('Active providers: ' + (', '.join(names) or 'none'))
        self.sync_fields()

    @on(Checkbox.Changed, '#web-enabled')
    def sync_fields(self):
        enabled = self.query_one('#web-enabled', Checkbox).value
        self.query_one('#web-fields').display = enabled
        mode = self.query_one('#web-mode', Select).value
        self.query_one('#web-section', Collapsible).title = WEB_SEARCH_LABEL + ' · ' + (
            ('Automatic fallback' if mode == 'auto' else 'Only ' + web_access.SERVICES[mode][0]) if enabled else 'Off')

    @on(Select.Changed, '#web-mode')
    async def mode_changed(self):
        await self.refresh_providers()

    @on(Button.Pressed)
    async def edit_provider(self, event):
        button = event.button.id or ''
        if button == 'web-copy':
            event.stop()
            self.app.push_screen(CopyWebAccess(self.copy_sources()), self.copied)
            return
        if button == 'web-save-defaults':
            event.stop()
            try:
                value, keys = await self.defaults_selection()
                self.app.defaults = save_web_defaults(value, keys)
                self.notify(f'{WEB_SEARCH_LABEL} defaults saved, including API keys.')
            except (OSError, ValueError) as error:
                self.notify(str(error), severity='error')
            return
        if button != 'web-add' and not button.startswith('web-edit-'):
            return
        event.stop()
        name = button.removeprefix('web-edit-') if button.startswith('web-edit-') else None
        self.app.push_screen(SearchProviderDialog(self.providers, name,
            self.query_one('#web-mode', Select).value), self.provider_saved)

    async def provider_saved(self, result):
        if result is None:
            return
        name, row, key = result
        old = self.providers.get(name)
        if row is None:
            self.providers.pop(name)
            self.keys.pop(name, None)
        else:
            self.providers[name] = row
            if old and old['endpoint'] != row['endpoint']:
                self.keys.pop(name, None)
            if key is not None:
                self.keys[name] = key
        mode = self.query_one('#web-mode', Select)
        selected = mode.value
        mode.set_options(self.mode_choices())
        mode.value = selected
        await self.refresh_providers()

    async def defaults_selection(self):
        value, pending = self.collect()
        keys = {}
        form = self.screen
        if isinstance(form, SettingsForm) and form.editing:
            needed = [name for name, row in value['providers'].items() if row['has_key'] and name not in pending]
            if needed:
                stored = await asyncio.to_thread(self.app.service.search_keys, form.draft['name'])
                keys.update(web_access.matching_keys(stored, value))
        keys.update(pending)
        missing = [web_access.SERVICES[name][0] for name, row in value['providers'].items()
                   if row['has_key'] and not keys.get(name)]
        if missing:
            raise ValueError('Enter API keys before saving defaults for: ' + ', '.join(missing))
        return value, keys

    def selection(self):
        return web_access.settings({'enabled': self.query_one('#web-enabled', Checkbox).value,
            'mode': self.query_one('#web-mode', Select).value, 'providers': self.providers})

    def has_changes(self, original):
        current = self.selection()
        if self.keys or (current['enabled'], current['mode']) != (original['enabled'], original['mode']):
            return True
        # The credential status probe can update badges without editing the form.
        def provider_settings(value):
            return {name: (row['endpoint'], row['model']) for name, row in value['providers'].items()}
        return provider_settings(current) != provider_settings(original)

    def collect(self):
        value = self.selection()
        if value['enabled'] and not web_access.active(value):
            error = 'Configure a search provider with the required key or server URL.'
            self.screen.query_one('#network-search-section', Collapsible).collapsed = False
            self.query_one('#web-section', Collapsible).collapsed = False
            feedback = self.query_one('#web-config-error', Feedback)
            self.query_one('#web-mode', Select).focus()
            feedback.update(error)
            self.screen.call_after_refresh(feedback.scroll_visible, animate=False)
            raise ValueError(error)
        return value, dict(self.keys)


class NetworkRulesFields(Vertical):
    def __init__(self, text, groups, *, profile=None, assigned=None):
        super().__init__(id='network-rules-fields')
        self.rules, self.groups, self.profile = text, groups, profile
        self.assigned = assigned

    def compose(self):
        yield WebsiteGroups(self.groups, assigned=self.assigned)
        with Vertical(classes='setting'):
            yield Label('Additional allowed destinations · hostname:port')
            yield TextArea(self.rules, id='allowlist', soft_wrap=False)
            yield Feedback('', id='network-error', markup=False)
        with Collapsible(title='Import or export rules', collapsed=True):
            with Vertical(classes='setting'):
                yield Label('Allowlist file path')
                yield PathField('network-file', title='Choose allowlist file', mode='save',
                                placeholder='~/network-rules.txt', initial=Path.home() / 'network-rules.txt')
            with Horizontal(classes='buttons'):
                yield Button('Import file', id='network-import')
                yield Button('Export file', id='network-export')
                if self.profile:
                    yield Button('Profile defaults', id='reset-rules')
            yield Feedback('', id='network-file-error')

    @on(TextArea.Changed, '#allowlist')
    def clear_destination_error(self, event):
        event.text_area.remove_class('-invalid')
        self.query_one('#network-error', Feedback).update('')

    @on(Button.Pressed, '#network-import, #network-export, #reset-rules')
    def import_export(self, event):
        event.stop()
        field = self.query_one('#allowlist', TextArea)
        feedback = self.query_one('#network-file-error', Feedback)
        feedback.update('')
        try:
            if event.button.id == 'reset-rules':
                ports = (self.screen.query_one('#model-ports', Input).value
                         if self.screen.query_one('#local-models', Checkbox).value else None)
                field.load_text(preset(self.profile, {**self.app.defaults, 'local_model_ports': ports})['allowlist'])
                return
            value = self.query_one('#network-file', Input).value.strip()
            if not value:
                raise ValueError('Enter an allowlist file path.')
            path = Path(value).expanduser()
            if event.button.id == 'network-import':
                text = path.read_text()
                sandbox.parse_rules(text)
                field.load_text(text)
                self.notify('Rules imported')
            else:
                path.write_text(field.text)
                self.notify('Rules exported')
        except (OSError, ValueError) as error:
            feedback.update(str(error))
            feedback.scroll_visible(animate=False)


class Confirm(ModalScreen[bool]):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def action_cancel(self):
        self.dismiss(False)

    def __init__(self, message, action, *, confirm_by_default, cancel_label='Cancel', title=None, notes=()):
        super().__init__(classes='titled-confirm' if title else None)
        self.message, self.action = message, action
        self.cancel_label = cancel_label
        self.title_text = title
        self.confirm_by_default = confirm_by_default
        self.notes = notes
        self.set_class(bool(notes), 'with-notes')
        self.AUTO_FOCUS = '#yes' if confirm_by_default else '#cancel'

    def compose(self):
        with Vertical(id='confirm-box'):
            if self.notes:
                with VerticalScroll(classes='confirm-notes'):
                    if self.title_text:
                        yield Label(self.title_text, markup=False, classes='title')
                    yield Static(self.message, markup=False, classes='confirm-message')
                    for note in self.notes:
                        yield Bullet(note)
            else:
                if self.title_text:
                    yield Label(self.title_text, markup=False, classes='title')
                yield Static(self.message, markup=False)
            yield from self.action_buttons()

    def action_buttons(self):
        with Horizontal(classes='buttons dialog-actions'):
            yield Button(self.cancel_label, id='cancel')
            yield Button(self.action, id='yes', variant='primary' if self.confirm_by_default else 'error')

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        self.dismiss(event.button.id == 'yes')


class ConfirmWithoutModel(Confirm):
    def __init__(self):
        super().__init__(
            'Pi needs a model connection to reply. You can set one up now or start the sandbox and connect a model later.',
            'Create without a model', confirm_by_default=False, cancel_label='Open Model Providers',
            title='No model providers enabled',
        )

    def action_buttons(self):
        with Horizontal(classes='buttons dialog-actions'):
            yield Button(self.cancel_label, id='cancel', variant='primary')
            yield Button(self.action, id='yes')


class ConfirmCreationConflicts(Confirm):
    DEFAULT_CSS = '''
    ConfirmCreationConflicts #confirm-box { width: 88; height: 21; overflow: hidden; }
    ConfirmCreationConflicts VerticalScroll { height: 1fr; margin: 0 0 1 0; }
    ConfirmCreationConflicts Static { height: auto; }
    '''

    def __init__(self, sandbox_name, kind, resources, warning):
        count = len(resources)
        resource = kind if count != 1 else kind[:-1]
        message = f'The name “{sandbox_name}” is already in use. Review {count} {resource}.\n\n{warning}'
        super().__init__(message + '\n\n' + '\n'.join(name for name, _ in resources),
                         'Delete ' + kind, confirm_by_default=False,
                         title='Delete conflicting ' + kind + '?')

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(self.title_text, markup=False, classes='title')
            with VerticalScroll():
                yield Static(self.message, id='creation-conflict-list', markup=False)
            yield from self.action_buttons()


class InstallSystemPackages(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, name):
        super().__init__()
        self.sandbox_name = name

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Install system packages · ' + self.sandbox_name, markup=False, classes='title')
            with VerticalScroll(id='system-packages-fields'):
                with Vertical(classes='setting'):
                    yield Label('Debian package names')
                    yield Input(id='system-package-names')
                    yield Static('Separate names with spaces or commas, for example jq ripgrep.', classes='setting-help')
                    yield Feedback(id='system-packages-error', markup=False)
                yield Static(settings_effects.bullets([
                    'Package scripts can run as root inside this sandbox.',
                    'Install only packages you trust. The sandbox stays running.',
                ]), id='system-packages-warning')
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='system-packages-cancel')
                yield Button('Install packages', id='system-packages-install', variant='primary')

    @on(Button.Pressed, '#system-packages-install')
    @on(Input.Submitted, '#system-package-names')
    def install(self, event):
        event.stop()
        field = self.query_one(Input)
        try:
            packages = system_packages.package_names(field.value.replace(',', ' ').split())
        except ValueError as error:
            feedback = self.query_one(Feedback)
            feedback.update(str(error))
            feedback.scroll_visible(animate=False)
            field.add_class('-invalid')
            return
        self.dismiss(packages)

    @on(Input.Changed, '#system-package-names')
    def clear_error(self, event):
        event.input.remove_class('-invalid')
        self.query_one(Feedback).update('')

    @on(Button.Pressed, '#system-packages-cancel')
    def cancel(self, event):
        event.stop()
        self.action_cancel()

    def action_cancel(self):
        self.dismiss(None)


def confirm_aws_installation(name, effect, *, restricted=False):
    if effect == 'gateway':
        return Confirm('The sandbox will keep running. Active network connections may need to reconnect.',
                       'Restart gateway and connect', confirm_by_default=False,
                       title=f'Restart the network gateway for {name}?')
    running = effect == 'restart'
    effects = [
        'Running terminals and development servers will stop. In-progress agent work may be interrupted.' if running else
        'The sandbox will stay stopped. AWS access will be available the next time you start it.',
        'Files stored only in the old container, such as files in /tmp and manually installed system packages, will be removed.',
        'Project files, your sandbox home folder and saved chats are kept.',
    ]
    if running and restricted:
        effects.insert(1, 'Active network connections will close when the gateway restarts.')
    return Confirm('AWS tools need to be installed once.\n\n' + settings_effects.bullets(effects),
                   'Restart and connect' if running else 'Install AWS tools', confirm_by_default=False,
                   title=f'Restart {name} to connect AWS?' if running else f'Install AWS tools in {name}?')


class FactoryUpdateDetails(ModalScreen[None]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, title, message, *, close_label='Close'):
        super().__init__()
        self.heading, self.message = title, message
        self.close_label = close_label

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(self.heading, classes='title')
            with VerticalScroll(id='factory-update-details'):
                yield Static(self.message, markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button(self.close_label, id='close-update-details')

    def on_mount(self):
        self.query_one('#close-update-details', Button).focus()

    @on(Button.Pressed, '#close-update-details')
    def action_close(self):
        self.dismiss()


class ApplySettingsReview(Confirm):
    """Scrollable effects review keeps Apply reachable with many changes."""
    DEFAULT_CSS = '''
    ApplySettingsReview { align: center middle; }
    ApplySettingsReview #apply-review { width: 88; max-width: 94%; height: 90%; padding: 1 2; background: #34383d; border: solid #f2b544; }
    ApplySettingsReview #apply-review-fields { height: 1fr; }
    ApplySettingsReview Static { height: auto; margin: 0 0 1 0; }
    '''

    def __init__(self, original, draft, mode='browser'):
        changes = settings_effects.configuration_changes(original, draft)
        summary = 'Changes\n' + settings_effects.bullets(changes) if changes else 'No configuration values changed.'
        super().__init__(summary + '\n\nWhen you apply\n' + settings_effects.bullets(settings_effects.configuration_notes(original, draft, mode)), 'Apply and recreate', confirm_by_default=True)

    def compose(self):
        with Vertical(id='apply-review'):
            yield Label('Review changes', classes='title')
            with VerticalScroll(id='apply-review-fields'):
                yield Static(self.message, markup=False)
            yield from self.action_buttons()


class ApprovalHelp(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]
    DEFAULT_CSS = '''
    #approval-help { height: auto; max-height: 76vh; padding: 0 1 0 0; }
    #approval-help .approval-mode-help { height: auto; margin: 0 1 1 0; }
    #approval-help .approval-mode-help Label { width: 2; }
    #approval-help .approval-mode-description { width: 1fr; }
    '''

    def __init__(self, *, editing):
        super().__init__()
        self.editing = editing

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Approval modes', classes='title')
            with VerticalScroll(id='approval-help'):
                for label, _, explanation in APPROVAL_MODES.values():
                    with Horizontal(classes='approval-mode-help'):
                        yield Label('•')
                        yield Static(Text.assemble((label, 'bold'), ' ', explanation), classes='approval-mode-description')
                yield Static('Pi can ask in its terminal or in FrostPi. If the reviewer fails to respond, '
                             'the action stays blocked in both Auto modes. Explicit sandbox and permission '
                             'blocks and the container’s network rules apply in every mode.',
                             classes='setting-help approval-help-note')
                yield Static('In either Auto mode, use /auto-review-settings in Pi or FrostPi to change the reviewer '
                             'model or thinking effort for the current chat.', classes='setting-help approval-help-note')
                yield Static('Changing the approval mode or sandbox protection recreates the sandbox. Reopen Pi to use a new approval model.' if self.editing else
                             f'Change these settings later in Settings → {CATEGORIES["approval-settings"].label}.',
                             classes='setting-help')
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Close', id='close-approval-help', variant='primary')

    @on(Button.Pressed, '#close-approval-help')
    def action_close(self):
        self.dismiss()


class ProfileServicesHelp(ModalScreen[None]):
    BINDINGS = [('escape', 'close', 'Close')]

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Profile services', classes='title')
            with VerticalScroll(id='profile-details-body'):
                for heading, text in (
                    ('Startup', 'Profile services start with this sandbox.'),
                    ('Stop, start and rebuild', 'Application data is kept.'),
                    ('Create from existing…', 'Each new service initializes new application data. Existing application data is not copied.'),
                    ('Delete sandbox', 'This sandbox’s application data is permanently deleted.'),
                ):
                    with Vertical(classes='setting'):
                        yield Label(heading, classes='section-heading')
                        yield Static(text, markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='close-profile-services-help', variant='primary')

    def on_mount(self):
        self.query_one('#close-profile-services-help', Button).focus()

    @on(Button.Pressed, '#close-profile-services-help')
    def action_close(self):
        self.dismiss()


def saved_profile_only(config):
    return (coding_agent(config) != 'pi' or bool(config.get(profile_edit.EDIT))
            or bool(config.get('profile', {}).get('customized_from')))


class MoreActions(ModalScreen[Optional[str]]):
    BINDINGS = [('escape', 'cancel', 'Back')]

    def __init__(self, record, *, browser_running=False):
        super().__init__()
        running = record['dev'] == 'running'
        self.sandbox_name = record['name']
        if record.get('creation'):
            self.actions = ([('Delete sandbox…', 'delete')] if record['creation'] == 'interrupted'
                            else [('Refresh sandbox list', 'refresh-creation')])
            self.groups = [self.actions]
            self.pages = {}
            self.page = None
            return
        native = coding_agent(record.get('config', {})) != 'pi'
        use_saved_profile = saved_profile_only(record.get('config', {}))
        opening = [('Open', 'open'), ('Open with…', 'launch-with'), ('Settings', 'settings'),
                   (cli_recipes.LABEL, 'cli-commands')]
        lifecycle = [('Stop sandbox' if running else 'Start sandbox', 'stop' if running else 'start'),
                     ('Rebuild sandbox…' if use_saved_profile else 'Update sources and recreate…', 'rebuild')]
        if record.get('config', {}).get('engine'):
            lifecycle.append((INSTALL_SYSTEM_PACKAGES, 'install-system-packages'))
        if browser_running:
            lifecycle.append(('Stop browser editor', 'stop-editor'))
        related = []
        saved_profile = record.get('config', {}).get('profile')
        if isinstance(saved_profile, dict) and all(key in saved_profile for key in ('name', 'digest', 'path')):
            related.append(('Profile and services…', 'profile-information'))
        profile = record.get('config', {}).get('profile') or {}
        if profile.get('path') and (Path(profile['path']) / 'start-here.md').is_file():
            related.append(('Getting started…', 'profile-guide'))
        diagnostics = [('Containers…', 'containers'), ('Container logs', 'logs'), ('Container shell', 'shell')]
        if not native:
            diagnostics.append(('Test MCP connections', 'connections'))
        self.pages = {'diagnostics': ('Troubleshooting', diagnostics)}
        if record.get('config', {}).get('engine'):
            related.append(('Chat history…', 'chats'))
            self.pages['chats'] = ('Chat history', [(f'{EXPORT}…', 'export-chats'), (f'{IMPORT}…', 'import-chats'),
                                                   ('Continue in another sandbox…', 'copy-chat')])
        related.append(('Troubleshooting…', 'diagnostics'))
        if count := len(sandbox_alerts(record)):
            related.append((f'Alerts ({count})', 'alerts'))
        self.groups = [opening, lifecycle, related, [('Delete sandbox…', 'delete')]]
        self.actions = [item for group in self.groups for item in group]
        self.page = None
        self.parent_index = 0

    def root_options(self):
        for index, group in enumerate(self.groups):
            if index:
                yield None
            yield from (label for label, _ in group)

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Actions · ' + self.sandbox_name, classes='title')
            yield OptionList(*self.root_options(), id='more-actions', compact=True)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='more-cancel')

    @on(OptionList.OptionSelected)
    def selected(self, event):
        event.stop()
        choices, index = event.option_list, event.option_index
        if (not choices.is_attached or index >= choices.option_count
                or choices.get_option_at_index(index) is not event.option):
            return
        if self.page:
            self.dismiss(self.pages[self.page][1][index][1])
            return
        action = self.actions[index][1]
        if action not in self.pages:
            self.dismiss(action)
            return
        self.page = action
        self.parent_index = index
        title, items = self.pages[action]
        self.query_one(Label).update(f'{title} · {self.sandbox_name}')
        self.query_one('#more-cancel', Button).label = 'Back'
        choices.set_options(label for label, _ in items)
        choices.highlighted = 0

    @on(Button.Pressed)
    def cancel(self, event):
        event.stop()
        self.action_cancel()

    def action_cancel(self):
        if self.page is None:
            self.dismiss(None)
            return
        self.page = None
        self.query_one(Label).update('Actions · ' + self.sandbox_name)
        self.query_one('#more-cancel', Button).label = 'Close'
        choices = self.query_one(OptionList)
        choices.set_options(self.root_options())
        choices.highlighted = self.parent_index
        choices.focus()


class ContainersScreen(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        yield Header()
        yield Label('Containers · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form', id='container-list'):
            containers = self.record.get('containers')
            if not self.record.get('engine'):
                yield Static('Factory could not read this sandbox’s saved configuration.')
            elif containers is None:
                yield Static('Factory could not query the container engine. Once it’s available, press R on the sandbox list to refresh.')
            elif not containers:
                yield Static('There are no containers for this sandbox.')
            else:
                for container in containers:
                    status = container['status']
                    heading = Text(container['role'], style='bold')
                    heading.append(' · ' + ('Stopped' if status == 'exited' else safe_text(status).capitalize()))
                    with Vertical(classes='setting'):
                        yield Static(heading)
                        yield Static(safe_text(container['name']), markup=False)
                        yield Static(container['purpose'], classes='setting-help', markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='containers-back')
        yield Footer()

    def on_mount(self):
        self.query_one('#containers-back').focus()

    @on(Button.Pressed, '#containers-back')
    def back_pressed(self, event):
        event.stop()
        self.action_back()

    def action_back(self):
        self.app.pop_screen()


class AppliedProfile(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, state):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record, self.state = record, state
        self.return_focus = None
        self.refresh_request = None

    def compose(self):
        yield Header()
        yield Label('Profile and services · ' + self.record['name'], classes='title focused-settings-title')
        yield Feedback('', id='profile-information-error', classes='profile-page-feedback', markup=False)
        with VerticalScroll(classes='form'):
            profile = self.record['config']['profile']
            with Vertical(classes='setting'):
                yield Label(MAIN_AGENT_SANDBOX, id='profile-main-agent', classes='group-title', markup=False)
                yield Details([('Profile', profile.get('description') or profile['name'])])
                yield Details([('Installed profile version', profile['digest'])])
                editable = self.record['config'].get(profile_edit.EDIT)
                origin = profile.get('customized_from')
                origin = origin or (editable and {'name': editable['origin_name'], 'digest': editable['origin_digest']})
                if origin:
                    yield Details([('Customized from', origin['name'] + ' · ' + origin['digest'])])
                if editable:
                    yield Details([('Profile folder', editable['path'])])
                    if self.record.get('shared_profiles'):
                        yield Details([('Shared with', ', '.join(self.record['shared_profiles']))])
                if profile_runtime.applied(self.state):
                    yield Details([('Read-only build files', profile_runtime.PATH)])
                else:
                    yield Static('Rebuild this sandbox to make its applied recipes available inside the main agent sandbox.', markup=False)
                yield Static('The profile and base-image recipes are separate from your project files.', classes='setting-help')
            if not profile.get('services'):
                yield Static('This profile has no additional profile services.')
            else:
                with Horizontal(classes='profile-guide-row review-profile-row'):
                    yield Static('Profile services', classes='section-heading')
                    yield Button('More information…', id='profile-services-information')
                yield Feedback('Checking profile services…', id='profile-service-status', markup=False)
                for name, spec in profile['services'].items():
                    with Vertical(classes='setting'):
                        yield Label(name + ' · Checking…', id='profile-service-' + name)
                        yield Static(spec['description'], markup=False)
                        yield Label('Connect with SSH', classes='command-label')
                        yield Command('ssh -F ' + profile_services.CLIENT + '/config ' + profile_services.hostname(name))
                yield Details([('Connection settings and application credentials', profile_services.CLIENT + '/connections.json')])
            if self.record['config'].get('profile_service_recovery'):
                with Vertical(classes='setting'):
                    yield Button('Saved service data…', id='profile-information-data')
                    yield Static('Export or restore service data kept by Factory.', classes='setting-help')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='profile-information-back')
            yield Button(profile_edit.action_label(self.record['config']), id='profile-information-edit', variant='primary')
        yield Footer()

    def on_mount(self):
        self.query_one('#profile-information-back').focus()
        if self.record['config']['profile'].get('services'):
            self.load_status()

    def on_screen_resume(self):
        if self.return_focus and self.app.screen is self:
            self.refresh_saved_config()

    @work(exit_on_error=False)
    async def refresh_saved_config(self):
        target = self.return_focus
        if not target or self.app.screen is not self:
            return
        request = self.refresh_request = object()
        try:
            config = await asyncio.to_thread(self.app.service.config, self.record['name'])
            error = ''
        except (OSError, ValueError) as failure:
            config = self.record['config']
            error = f'Could not refresh the profile for {self.record["name"]}: {safe_text(failure)}. Check its saved configuration and reopen this page.'
        if not self.is_attached or self.refresh_request is not request:
            return
        if config != self.record['config']:
            self.record = {**self.record, 'config': config}
            await self.recompose()
            if self.record['config']['profile'].get('services'):
                self.load_status()
        self.query_one('#profile-information-error', Feedback).update(error)
        if self.app.screen is self:
            self.return_focus = None
            self.call_after_refresh(self.restore_return_focus, target, request)

    def restore_return_focus(self, target, request):
        if self.is_attached and self.app.screen is self and self.refresh_request is request:
            control = self.query_one_optional('#' + target, Button) or self.query_one('#profile-information-back', Button)
            control.focus()
            control.scroll_visible(animate=False)

    @work(exit_on_error=False)
    async def load_status(self):
        config = self.record['config']
        try:
            rows = await asyncio.to_thread(profile_services.status, config)
            text = ''
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            text = 'Could not read profile service status: ' + safe_text(error) + '. Check the container engine and reopen this page.'
        if self.is_attached and self.record['config'] is config:
            message = self.query_one('#profile-service-status', Static)
            message.update(text)
            message.display = bool(text)
            message.set_class(bool(text), 'error')
            if text:
                for name in self.record['config']['profile']['services']:
                    self.query_one('#profile-service-' + name, Label).update(name + ' · Status unavailable')
            if not text:
                for row in rows:
                    self.query_one('#profile-service-' + row['name'], Label).update(
                        row['name'] + ' · ' + ('Running' if row['running'] else 'Stopped'))

    @on(Button.Pressed, '#profile-information-back')
    def back_pressed(self, event):
        event.stop()
        self.action_back()

    @on(Button.Pressed, '#profile-information-edit')
    def edit_profile(self):
        self.return_focus = 'profile-information-edit'
        self.refresh_request = None
        self.app.perform('edit-profile', record=self.record)
        if self.app.screen is self:
            self.return_focus = None

    @on(Button.Pressed, '#profile-services-information')
    def service_information(self):
        self.app.push_screen(ProfileServicesHelp())

    @on(Button.Pressed, '#profile-information-data')
    def saved_service_data(self):
        self.return_focus = 'profile-information-data'
        self.refresh_request = None
        self.app.perform('saved-service-data', record=self.record)
        if self.app.screen is self:
            self.return_focus = None

    def action_back(self):
        self.app.pop_screen()


class SettingsMenu(ModalScreen[Optional[str]]):
    BINDINGS = [('escape', 'cancel', 'Close')]
    ACTIONS = [(category.label, action) for action, category in CATEGORIES.items()]

    def __init__(self, name, config, editor_mode, system_prompt=None, *, state=None):
        super().__init__()
        self.sandbox_name = name
        agent = coding_agent(config)
        self.actions = ([(category.label, action) for action, category in primary_categories(agent).items()]
                        if agent != 'pi' else self.ACTIONS)
        self.set_class(agent != 'pi', 'native-settings-menu')
        self.summaries = {'editor-settings': editor_settings.modes(agent)[editor_mode], 'connectors': connector_summary(config, state)}
        if agent != 'pi':
            self.summaries['primary-sign-in'] = PRIMARY_AGENTS[agent]
            self.summaries['primary-approvals'] = ('Ask for approval' if options(config.get('options'))['native_approval'] == 'manual'
                else {'codex': 'Approve for me', 'claude': 'Auto', 'agy': 'Allow configured tools'}[agent])
        self.summaries['system-prompt'] = system_prompt['name'] if system_prompt else NO_SYSTEM_PROMPT_ADD_ON
        if isinstance(config, dict) and config.get('engine'):
            local = bool(config.get('local_model_ports'))
            count = (int(local) + sum(row['name'] != 'local' for row in model_providers.selections(config))
                     + len(config.get('hosted_connections', {})))
            self.summaries['models'] = model_sources_summary(local, count)
            self.summaries['skills'] = saved_skills_summary(len(config.get('extra_skills', [])))
            try:
                selected = options(config.get('options'))
                network = sandbox.network_mode(config)
            except ValueError:
                return
            self.summaries.update({
                'network-settings': (network_search_summary(network, selected[WEB_SEARCH_OPTIONS[agent]]) if agent in WEB_SEARCH_OPTIONS else
                                     network_mode_label(network) if agent != 'pi' else
                                     network_search_summary(network, selected['web_access']['enabled'])),
                'approval-settings': approval_mode_summary(selected['approval'], selected['inner_sandbox']),
                'agent-tools': pi_features_summary(config.get('subagents', False), selected),
                'primary-browser': on_off(selected['browser']).capitalize(),
                'document-search': search_summary(selected, embedding_models.sources(config)),
                'communications': communications_summary(selected['communications']['slack']),
            })

    def compose(self):
        prompts = []
        for label, action in self.actions:
            prompt = Text(label)
            if summary := self.summaries.get(action):
                prompt.append(' · ' + summary, style='dim')
            prompts.append(prompt)
        with Vertical(id='confirm-box'):
            yield Label('Settings · ' + self.sandbox_name, classes='title')
            yield OptionList(*prompts, id='settings-actions')
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='settings-back')

    @on(OptionList.OptionSelected)
    def selected(self, event):
        event.stop()
        action = self.actions[event.option_index][1]
        if action == 'connectors':
            try:
                config = self.app.service.config(self.sandbox_name)
            except (OSError, ValueError) as error:
                self.notify(safe_text(error), severity='error')
                return
            def back(_):
                try:
                    summary = connector_summary(self.app.service.config(self.sandbox_name), self.app.state_root / self.sandbox_name)
                except (OSError, ValueError) as error:
                    self.notify(safe_text(error), severity='error')
                    return
                self.summaries[action] = summary
                label = Text(CATEGORIES[action].label)
                label.append(' · ' + summary, style='dim')
                self.query_one(OptionList).replace_option_prompt_at_index(event.option_index, label)

            self.app.push_screen(ConnectorSettings(self.sandbox_name, config), back)
        else:
            self.dismiss(action)

    @on(Button.Pressed)
    def back(self, event):
        event.stop()
        self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


def model_sources_summary(local, count):
    enabled = f'{count} enabled' if count else 'None enabled'
    return f"{LOCAL_MODELS_LABEL} {on_off(local)} · {enabled}"


def on_off(enabled):
    return 'on' if enabled else 'off'


def network_mode_label(mode):
    return 'Restricted' if mode == 'allowlist' else 'Unrestricted'


def network_search_summary(mode, search):
    return f'{network_mode_label(mode)} · {WEB_SEARCH_LABEL} {on_off(search)}'


def approval_mode_summary(mode, inner):
    return f'{APPROVAL_MODES[mode][0]} · Inner sandbox {on_off(inner)}'


def pi_features_summary(subagents, selected):
    return ' · '.join((f'Subagents {on_off(subagents)}',
                       f'Question forms {on_off(selected["question_forms"])}',
                       f'Scheduled prompts {on_off(selected["scheduling"])}',
                       f'Recall {selected["compaction_recall"].capitalize()}',
                       f'Browser {on_off(selected["browser"])}',
                       f'Goals {on_off(selected["goals"])}'))


class FoldersScreen(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        yield Header()
        with VerticalScroll(classes='form'):
            yield Label(CATEGORIES['folders'].label + ' · ' + self.record['name'], classes='title')
            yield FolderFields(self.record['config'], self.app.state_root)
            yield Static(settings_effects.bullets(['Changing host mounts restarts sandbox processes.', settings_effects.FOLDER]))
            yield Feedback('', id='folders-error', classes='error', markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='folders-back')
            yield Button('Apply', id='folders-apply', variant='primary')
        yield Footer()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'folders-back':
            self.action_back()
        elif event.button.id == 'folders-apply':
            try:
                selected = self.query_one(FolderFields).collect()
                app, name = self.app, self.record['name']
                service = app.service
                def apply():
                    config = self.record['config']
                    message = (settings_effects.bullets(settings_effects.reconnect(settings_client(config, app.state_root / name)))
                               if selected['extra_folders'] != config.get('extra_folders', []) else settings_effects.FOLDER)
                    app.pop_screen()
                    app.start_settings_job(name, 'Update folders · ' + name,
                        lambda log, update: service.update_settings(name, selected, log, update), success_message=message)
                if selected['extra_folders'] != self.record['config'].get('extra_folders', []):
                    notes = ['This recreates the sandbox and interrupts running processes.',
                             *settings_effects.editor_migration(self.record['config'])]
                    self.app.push_screen(Confirm('Apply folder mounts?\n' + settings_effects.bullets(notes),
                                                 'Apply and recreate', confirm_by_default=True), lambda yes: apply() if yes else None)
                else:
                    apply()
            except (ValueError, OSError) as error:
                self.query_one('#folders-error', Static).update(str(error))

    def action_back(self):
        self.app.pop_screen()


class SandboxTable(DataTable):
    # The header and entries each use one content line and one inert margin.
    ROW_PITCH = 2

    class ActionsRequested(Message):
        def __init__(self, name):
            super().__init__()
            self.name = name

    def _render_line(self, y, x1, x2, base_style):
        if y % self.ROW_PITCH:
            return Strip.blank(self.size.width, base_style)
        return super()._render_line(y, x1, x2, base_style)

    @staticmethod
    def action_cell(label, *, primary=False):
        style = Style(color='#17191b' if primary else '#f1f3f5',
                      bgcolor='#f2b544' if primary else '#494f57', bold=True,
                      meta={'sandbox_action': True})
        padded = f'{label:^8}' if primary else label
        return Text.assemble(' ', ('  ' + padded + '  ', style), no_wrap=True)

    def on_click(self, event: events.Click):
        # Textual normally activates an already-selected cell on click. Keep all
        # data-cell clicks selection-only unless they hit the painted action.
        meta = event.style.meta
        row, column = meta.get('row', -1), meta.get('column', -1)
        if meta.get('out_of_bounds') or not (0 <= row < self.row_count and 0 <= column < len(self.columns)):
            if event.button != 1:
                event.prevent_default()
                event.stop()
            return
        event.prevent_default()
        event.stop()
        coordinate = Coordinate(row, column)
        if event.button == 3:
            if event.chain == 1:
                self.post_message(self.ActionsRequested(str(self.coordinate_to_cell_key(coordinate).row_key.value)))
            return
        if event.button != 1:
            return
        self.focus()
        self.move_cursor(row=row, column=column, animate=False)
        if event.chain == 1 and meta.get('sandbox_action'):
            self.post_message(DataTable.CellSelected(self, self.get_cell_at(coordinate),
                coordinate=coordinate, cell_key=self.coordinate_to_cell_key(coordinate)))


class SandboxAlerts(ModalScreen[Optional[str]]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, record):
        super().__init__()
        self.name_label = record['name']
        self.alerts = sandbox_alerts(record)

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Alerts · ' + self.name_label, classes='title')
            with VerticalScroll(id='sandbox-alerts-list'):
                for alert in self.alerts:
                    classes = 'sandbox-alert -blocking' if alert.blocking else 'sandbox-alert'
                    with Vertical(classes=classes):
                        with Horizontal(classes='sandbox-alert-heading'):
                            yield Label(alert.title, classes='sandbox-alert-title')
                            yield Button(alert.button, name=alert.action, id='sandbox-alert-' + alert.action,
                                         classes='sandbox-alert-action')
                        yield Static(alert.description, classes='sandbox-alert-description', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='close-alerts')

    def on_mount(self):
        self.query_one('.sandbox-alert-action', Button).focus()

    @on(Button.Pressed, '.sandbox-alert-action')
    def selected(self, event):
        event.stop()
        self.dismiss(event.button.name)

    @on(Button.Pressed, '#close-alerts')
    def action_close(self):
        self.dismiss(None)


class SSHScreen(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, *, key_focus=None):
        super().__init__()
        self.record = record
        self.AUTO_FOCUS = key_focus

    def compose(self):
        hosts = self.record['config'].get('ssh', {}).get('hosts', [])
        from .repository_ui import GitIdentityFields
        identity = options(self.record['config'].get('options'))['git_identity']
        yield Header()
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Label(SSH_GIT_LABEL + ' · ' + self.record['name'], classes='title')
            yield Static('Keys stay in a separate SSH container. The remote account or deploy key determines what the sandbox can do.', classes='section-intro')
            yield Static('Applying changes to destinations or commit identity restarts the sandbox.', classes='section-intro')
            yield SSHDestinationFields(self.record['config'].get('ssh'))
            with Collapsible(title='SSH key · one per sandbox', collapsed=not (hosts or self.AUTO_FOCUS), id='ssh-key-section'):
                with Vertical(classes='setting'):
                    with Horizontal(classes='buttons'):
                        yield Button('Generate repository key', id='ssh-generate-key', disabled=not hosts)
                        yield Button('Show public key', id='ssh-public-key', disabled=not hosts)
                    yield Static('Creates a new key inside the companion. An existing key is never replaced.', classes='setting-help')
                with Vertical(classes='setting'):
                    yield Label('Or import a private key from this computer')
                    yield PathField('ssh-key-file', title='Choose SSH private key', mode='file',
                                    initial=Path.home() / '.ssh', placeholder='~/.ssh/id_ed25519', disabled=not hosts)
                    with Horizontal(classes='buttons'):
                        yield Button('Import and unlock', id='ssh-import-key', disabled=not hosts)
                    yield Static('Import replaces this sandbox’s key and keeps its server permissions. Encrypted keys ask for their passphrase in the terminal.', classes='setting-help')
                with Vertical(classes='setting'):
                    yield Label('Keep SSH key unlocked for')
                    yield Input(sandbox_ssh.DEFAULT_UNLOCK_DURATION, id='ssh-duration', disabled=not hosts)
                    yield Static('Enter 8h for eight hours or 2d for two days. Applies when you import or unlock a key.', classes='setting-help')
                    yield Feedback('', id='ssh-duration-error', markup=False)
                with Vertical(classes='setting'):
                    yield Button('Unlock existing key', id='ssh-unlock', disabled=not hosts)
                    yield Static('Unlock takes effect immediately. Restarting the SSH service can lock the key early.', classes='setting-help')
                    yield Feedback('', id='ssh-key-feedback', markup=False)
            with Collapsible(title='Git commit identity · ' + ('set' if identity else 'not set'), collapsed=True, id='git-identity-section'):
                yield GitIdentityFields(identity)
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='ssh-back')
            yield Button('Apply', id='ssh-apply', variant='primary')
        yield Footer()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        action = event.button.id
        name = self.record['name']
        try:
            if action == 'ssh-back': self.action_back()
            elif action in ('ssh-generate-key', 'ssh-public-key'):
                from .repository_ui import PublicKey
                self.app.push_screen(PublicKey(name, generate=action == 'ssh-generate-key'))
            elif action in ('ssh-import-key', 'ssh-unlock'):
                field = self.query_one('#ssh-duration', Input)
                duration = field.value.strip().lower()
                error = self.query_one('#ssh-duration-error', Feedback)
                feedback = self.query_one('#ssh-key-feedback', Feedback)
                feedback.update('')
                try:
                    sandbox_ssh.unlock_seconds(duration)
                except ValueError as invalid:
                    error.update(str(invalid))
                    field.focus()
                    return
                error.update('')
                args = ['ssh-unlock', name]
                if action == 'ssh-import-key':
                    path = self.query_one('#ssh-key-file', Input).value.strip()
                    if not path: raise ValueError('Choose a private key file')
                    args = ['ssh-import-key', name, '--key-file', path]
                with self.app.suspend():
                    result = subprocess.run(self.app.service.command(*args, '--duration', duration), check=False)
                feedback.set_class(bool(result.returncode), 'error')
                feedback.set_class(not result.returncode, 'unlock-succeeded')
                feedback.update('Could not unlock the SSH key. Check the SSH service and passphrase.'
                                if result.returncode else f'SSH key unlocked for {duration}. Git can use it now.')
                feedback.scroll_visible(animate=False)
            elif action == 'ssh-apply':
                from .repository_ui import GitIdentityFields
                identity = self.query_one(GitIdentityFields).collect()
                old = self.record['config'].get('ssh', {'hosts': []})
                settings = self.query_one(SSHDestinationFields).collect()
                identity_changed = identity != options(self.record['config'].get('options'))['git_identity']
                if settings == old and not identity_changed:
                    self.notify('No SSH or Git settings changed.')
                    return
                app, service = self.app, self.app.service
                def apply(yes):
                    if not yes: return
                    message = settings_effects.bullets(settings_effects.reconnect(settings_client(self.record['config'], app.state_root / name))
                        + (['Unlock the SSH key before connecting.'] if settings['hosts'] else [])
                        + (['Identity applies to future commits; repository-local settings take precedence.'] if identity_changed else []))
                    app.pop_screen()
                    app.start_settings_job(name, 'SSH & Git settings · ' + name,
                        lambda log, update: service.ssh_settings(name, settings, log, identity, update), success_message=message)
                notes = ['Active sandbox processes restart.', 'Workspace and private key are preserved.',
                         *settings_effects.editor_migration(self.record['config'])]
                self.app.push_screen(Confirm('Apply SSH & Git settings?\n' + settings_effects.bullets(notes), 'Apply', confirm_by_default=True), apply)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.notify(safe_text(error), severity='error')

    def action_back(self):
        self.app.pop_screen()
        self.app.refresh_inventory()


class AdvisorsScreen(Screen):
    DEFAULT_CSS = """
    AdvisorsScreen Input, AdvisorsScreen Select { max-width: 54; }
    AdvisorsScreen #advisor-calls { max-width: 14; }
    AdvisorsScreen .model-error { color: $error; height: auto; }
    """
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        settings = self.record['config'].get('advisors', {})
        providers = settings.get('providers', [])
        yield Header()
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Label(CATEGORIES['advisors'].label + ' · ' + self.record['name'], classes='title')
            yield Static('Choose the advisors Pi can consult. Sign-ins stay in a separate container.')
            yield Static(settings_effects.bullets(['Apply restarts sandbox processes.', 'Sign in and Check advisors take effect immediately.']), classes='section-intro')
            yield AdvisorChoices(providers, 'advisor-')
            yield Static('Copying credentials replaces the selected advisor’s saved sign-in when you apply.', id='advisor-copy-help')
            yield Static('If a copied sign-in stops working, use its Sign in button below, then Check advisors.'
                         if providers else f'Apply to add the advisor, then reopen {ADVISORS_PATH} to sign in.',
                         id='advisor-login-help')
            with Horizontal(classes='buttons'):
                yield Button('Sign in to Codex', id='advisor-login-codex', disabled='codex' not in providers)
                yield Button('Sign in to Claude', id='advisor-login-claude', disabled='claude' not in providers)
            with Horizontal(classes='buttons'):
                yield Button('Sign in to Antigravity', id='advisor-login-agy', disabled='agy' not in providers)
                yield Button('Check advisors', id='advisor-check', disabled=not providers)
            with Collapsible(title='Claude model', collapsed=True, id='advisor-claude-options'):
                with Vertical(classes='setting'):
                    yield Label('Preferred model · leave blank to use Claude’s default')
                    yield Input(settings.get('claude_model', ''), id='advisor-claude-model')
                    yield Static('Run /advisor status in Pi to see model IDs and remaining quota.', classes='setting-help')
                    yield Static(id='advisor-claude-error', classes='model-error')
                with Vertical(classes='setting'):
                    yield Label('Thinking effort for the preferred model')
                    yield Select([(x.title(), x) for x in ('low', 'medium', 'high', 'xhigh', 'max')], value=settings.get('claude_effort', 'medium'), allow_blank=False, id='advisor-claude-effort')
            with Collapsible(title='Antigravity model', collapsed=True, id='advisor-agy-options'):
                with Vertical(classes='setting'):
                    yield Label('Limit Antigravity to one model · leave blank to allow all')
                    yield Input(settings.get('agy_model', ''), id='advisor-model')
                    yield Static('Run /advisor status in Pi to see model IDs and their effort levels.', classes='setting-help')
                    yield Static(id='advisor-agy-error', classes='model-error')
                with Vertical(classes='setting'):
                    yield Label('Quota group · optional')
                    yield Input(settings.get('agy_quota_group', ''), id='advisor-quota')
                    yield Static('If /advisor status shows no quota for this model, enter its group name from /usage in Antigravity.', classes='setting-help')
            with Collapsible(title='Usage and network', collapsed=True, id='advisor-usage-options'):
                with Vertical(classes='setting'):
                    yield Label('Total advisor calls per Pi chat · blank for no limit')
                    yield Input(str(settings.get('max_calls') or ''), id='advisor-calls', type='integer')
                with Vertical(classes='setting'):
                    yield Label('Additional allowed destinations · hostname:port')
                    yield TextArea(settings.get('extra_hosts', ''), id='advisor-hosts')
                    yield Static('All enabled advisors can connect to these destinations.', classes='setting-help')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='advisor-back')
            yield Button('Apply', id='advisor-apply', variant='primary')
        yield Footer()

    def on_mount(self):
        self.sync_fields()

    @on(Checkbox.Changed)
    @on(Input.Changed, '#advisor-claude-model')
    @on(Input.Changed, '#advisor-model')
    def sync_fields(self):
        from .sandbox_advisors import MODEL_ID_HELP, valid_model_id
        selected = self.query_one(AdvisorChoices).selected()
        self.query_one('#advisor-copy-help').display = bool(self.query_one(AdvisorChoices).imports())
        self.query_one('#advisor-login-help').display = bool(selected) and (not self.record['config'].get('advisors', {}).get('providers') or bool(self.query_one(AdvisorChoices).imports()))
        self.query_one('#advisor-usage-options').display = bool(selected)
        for provider, fields in {
            'claude': ('claude-model', 'claude-effort'),
            'agy': ('model', 'quota'),
        }.items():
            section = self.query_one('#advisor-' + provider + '-options', Collapsible)
            section.display = provider in selected
            model = self.query_one('#advisor-' + fields[0], Input).value.strip()
            error = self.query_one('#advisor-' + provider + '-error', Static)
            error.display = not valid_model_id(model)
            error.update(MODEL_ID_HELP if error.display else '')
            label = 'Claude model' if provider == 'claude' else 'Antigravity model'
            default = 'Claude’s default' if provider == 'claude' else 'All models'
            section.title = label + ' · ' + ('Check model ID' if error.display else model or default)
            for field in fields:
                self.query_one('#advisor-' + field).disabled = provider not in selected or (field != fields[0] and not model)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        action = event.button.id
        name = self.record['name']
        if action == 'advisor-back':
            self.action_back()
        elif action == 'advisor-check':
            self.app.pop_screen()
            self.app.operation('Advisor login, models and quota · ' + name, ['advisor-check', name])
        elif action and action.startswith('advisor-login-'):
            provider = action.removeprefix('advisor-login-')
            with self.app.suspend():
                subprocess.run(self.app.service.command('advisor-login', name, provider), check=False)
        elif action == 'advisor-apply':
            from . import sandbox_advisors
            try:
                calls = self.query_one('#advisor-calls', Input).value.strip()
                settings = sandbox_advisors.validate({
                    'providers': self.query_one(AdvisorChoices).selected(),
                    'claude_model': self.query_one('#advisor-claude-model', Input).value.strip(),
                    'claude_effort': self.query_one('#advisor-claude-effort', Select).value,
                    'agy_model': self.query_one('#advisor-model', Input).value.strip(),
                    'agy_quota_group': self.query_one('#advisor-quota', Input).value.strip(),
                    'max_calls': int(calls) if calls else None,
                    'extra_hosts': self.query_one('#advisor-hosts', TextArea).text,
                })
            except ValueError as error:
                self.notify(str(error), severity='error')
                return
            imports = self.query_one(AdvisorChoices).imports()
            app, service = self.app, self.app.service
            def apply(yes):
                if not yes:
                    return
                app.pop_screen()
                def setup(log, update):
                    service.advisors(name, settings, log, update)
                    for provider in imports:
                        service.run(['advisor-import', name, provider], log)
                app.start_settings_job(name, ADVISORS_LABEL + ' · ' + name, setup,
                    success_message=settings_effects.bullets(settings_effects.reconnect(editor_settings.load(app.state_root / name)['mode'])))
            notes = ['Active sandbox processes restart.', 'Workspace and private home are preserved.',
                     *settings_effects.editor_migration(self.record['config'])]
            if imports:
                notes.append('Selected host sign-ins replace the saved advisor logins.')
            self.app.push_screen(Confirm('Apply advisor settings?\n' + settings_effects.bullets(notes), 'Apply', confirm_by_default=True), apply)

    def action_back(self):
        self.app.pop_screen()


class ModelsScreen(Screen):
    BINDINGS=[('escape','back','Back')]
    def __init__(self,record,finished=None):
        super().__init__();self.record=record;self.finished=finished
        self.key_status = {}
        rows = record['config'].get('model_providers', [{'name': 'local', 'manifest': {}}])
        self.providers = {row['name']: row['manifest'] for row in rows}

    def credential_binding(self, name):
        return provider_credentials.scope(name, self.providers.get(name, {}).get('inference'))

    def discovery_buttons(self, prefix):
        with Horizontal(classes='buttons'):
            for action in ('Status', 'Preview', 'Refresh'):
                yield Button(action, id=f'{prefix}-{action.lower()}')

    def compose(self):
        ports = self.record['config'].get('local_model_ports')
        local = 'local' in self.providers
        plugins = [(manifest.get('description', name), name)
                   for name, manifest in self.providers.items() if name != 'local']
        yield Header()
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Label(CATEGORIES['models'].label + ' · ' + self.record['name'], classes='title')
            if ports or local:
                with Collapsible(title=local_models_title(bool(ports)), id='live-local-provider'):
                    if ports:
                        with Vertical(classes='setting'):
                            yield Static('Server ports · ' + ports, markup=False)
                        with Vertical(classes='setting'):
                            yield Button(LOCAL_KEYS_LABEL, id='local-server-keys')
                    if local:
                        yield from self.discovery_buttons('local')
            else:
                yield Static(local_models_title(False), classes='group-title')
            yield HostedProviders(self.record['name'])
            if plugins:
                with Collapsible(title=PROVIDER_PLUGINS_LABEL, id='provider-controls'):
                    with Vertical(classes='setting'):
                        yield Label('Model provider')
                        yield Select(plugins, id='model-provider', allow_blank=False, value=plugins[0][1])
                    with Vertical(classes='setting', id='legacy-provider-key'):
                        yield Label('Provider credential · when required')
                        with Horizontal(classes='path-row'):
                            yield Input(password=True, id='provider-key')
                            yield Button('Configure', id='provider-auth')
                    with Vertical(classes='setting', id='managed-provider-key'):
                        yield Static(key_caption(), id='assigned-provider-key', markup=False)
                        with Horizontal(classes='buttons'):
                            yield Button('Choose key…', id='provider-change-key')
                            yield Button('Choose model…', id='provider-model-choice')
                    yield from self.discovery_buttons('provider')
            else:
                with Vertical(classes='setting'):
                    yield Label(PROVIDER_PLUGINS_LABEL, classes='group-title model-group-title')
                    yield Static('No provider plugins are enabled.', id='no-model-providers', classes='setting-help')
            with Vertical(classes='setting'):
                yield Button(EDIT_FORMS['model-sources'].label + '…', id='model-sources')
                yield Static('Changing local access or enabled plugins recreates the sandbox.', classes='setting-help')
            selected = options(self.record['config'].get('options'))
            if self.finished and selected['approval'] in ('automatic', 'assisted'):
                yield ReviewerFields(self.record['name'], selected['approval_model'], save_on_choice=True)
        with Horizontal(classes='wizard-actions'):
            yield Button('Continue' if self.finished else 'Back',id='models-back',variant='primary' if self.finished else 'default')
        yield Footer()

    @on(HostedProviders.Changed)
    def refresh_approval_models(self):
        if fields := next(iter(self.query(ReviewerFields)), None):
            fields.load()

    def on_mount(self):
        local = self.query('#live-local-provider CollapsibleTitle')
        if local:
            local.first().focus()
        else:
            self.query_one('.form', VerticalScroll).focus()
        if not self.query('#model-provider'):
            return
        self.show_key_status()
        if any(self.credential_binding(name) for name in self.providers):
            self.load_key_status()

    @on(Select.Changed, '#model-provider')
    def show_key_status(self):
        provider = self.query_one('#model-provider', Select).value
        managed = bool(self.credential_binding(provider))
        self.query_one('#legacy-provider-key').display = not managed and provider not in ('local', Select.NULL)
        self.query_one('#managed-provider-key').display = managed
        label = assigned_key_label(self.key_status, provider)
        self.query_one('#assigned-provider-key', Static).update(key_caption(label))
        self.query_one('#provider-change-key', Button).label = 'Change key…' if label else 'Choose key…'

    @work(exclusive=True)
    async def load_key_status(self):
        try:
            self.key_status = await asyncio.to_thread(self.app.service.provider_key_status, self.record['name'])
            self.show_key_status()
        except (ValueError, OSError, subprocess.SubprocessError):
            self.query_one('#assigned-provider-key', Static).update('Start the sandbox to view its assigned key.')

    def change_key(self, provider, choice, label=None):
        if choice is None:
            return
        service, name = self.app.service, self.record['name']
        choose_model = (not choice.get('disconnect') and self.credential_binding(provider)
                        and provider not in self.key_status.get('configured', ()))

        def task(log):
            if choice.get('disconnect'):
                service.disconnect_provider_key(name, provider)
                log('API key disconnected from this sandbox.')
            else:
                service.assign_provider_key(name, provider, choice, log)

        app = self.app
        def saved(_):
            if app.screen is self:
                self.load_key_status()
                self.refresh_approval_models()
            if choose_model:
                app.push_screen(ModelDialog(name, provider, plugin_label=label or provider, optional=True))

        app.start_task(name, 'API key · ' + (label or provider), task,
                       on_success=saved, followup_label='Choose a model' if choose_model else 'View model settings')

    def open_plugin_model(self, provider, *, optional=False):
        label = self.providers[provider].get('description') or provider
        self.app.push_screen(ModelDialog(self.record['name'], provider, plugin_label=label, optional=optional))

    @work(exclusive=True, group='local-server-keys')
    async def open_local_keys(self):
        try:
            status = await asyncio.to_thread(self.app.service.provider_key_status, self.record['name'], local=True)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.notify(str(error), severity='error')
            return
        ports = sandbox.local_model_ports(self.record['config']['local_model_ports'])
        def selected(selection):
            if selection:
                port, choice = selection
                self.change_key(inference.local_route_id(port), choice, f'Local server · port {port}')
        self.app.push_screen(LocalKeysDialog(ports, status=status), selected)

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop();action=event.button.id
        if action in ('model-sources','models-back'):
            self.app.pop_screen()
            if action=='model-sources':self.app.perform('model-sources', record=self.record)
            elif self.finished:self.finished()
            return
        if action == 'local-server-keys':
            self.open_local_keys()
            return
        if action in ('local-status', 'local-preview', 'local-refresh'):
            self.run_discovery('local', action.removeprefix('local-'))
            return
        if action not in ('provider-change-key', 'provider-model-choice', 'provider-status',
                          'provider-preview', 'provider-refresh', 'provider-auth'):
            return
        selected = self.query_one('#model-provider', Select).value
        if action == 'provider-model-choice':
            self.open_plugin_model(selected)
            return
        if action == 'provider-change-key':
            provider = self.providers[selected]
            self.app.push_screen(CredentialDialog(provider.get('description', selected),
                self.credential_binding(selected), active=selected in self.key_status.get('configured', ())),
                lambda choice: self.change_key(selected, choice))
            return
        key = self.query_one('#provider-key', Input).value if action == 'provider-auth' else None
        if action == 'provider-auth' and not key.strip():
            self.notify('Enter the provider credential first.', severity='error')
            return
        self.query_one('#provider-key',Input).value=''
        self.run_discovery(selected, action.removeprefix('provider-'), key)

    def run_discovery(self, provider, action, key=None):
        app, name = self.app, self.record['name']
        service = app.service
        self.app.pop_screen()
        label = LOCAL_MODELS_LABEL if provider == 'local' else self.providers[provider].get('description', provider)
        notice = settings_effects.bullets([settings_effects.reconnect(editor_settings.load(app.state_root / name)['mode'])[0],
                                           'Select a model in the chat.']) if action in ('auth', 'refresh') else ''
        app.start_task(name, label, lambda log: service.provider(name, provider, action, key, log),
                       working='Checking model connections', success_message=notice)
    def action_back(self):self.app.pop_screen()


class PackageRow(Vertical):
    def __init__(self, kind, name, description, record=None, summary=''):
        super().__init__(classes='package-row')
        self.kind, self.package_name, self.description, self.record = kind, name, description, record
        self.summary = summary

    def compose(self):
        title = self.description or self.package_name
        if self.record:
            title = installed_profile_title(self.package_name) if self.kind == 'profile' else 'Local name · ' + self.package_name
        yield Static(title, classes='package-title', markup=False)
        summary = self.summary if self.kind == 'profile' else self.description if self.record else ''
        if summary:
            yield Static(summary, markup=False)
        if self.record is None:
            yield Static('Included with AI Sandbox Factory', markup=False)
        else:
            source = self.record['source']
            if self.record.get('subdir', '.') != '.':
                source += ' · ' + self.record['subdir']
            if self.record.get('ref'):
                source += ' · ' + self.record['ref']
            yield Static(source, markup=False)
        if self.kind == 'profile' or self.record is not None:
            with Horizontal(classes='buttons package-actions'):
                if self.kind == 'profile':
                    yield Button('Details…', name='details')
                if self.record is not None:
                    yield Button('Update', name='update')
                    yield Button('Remove', name='remove')


class ProfileGuide(ModalScreen[None]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, name, guide):
        super().__init__()
        self.profile_name = name
        self.guide = guide

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label(f'Getting started · {self.profile_name}', markup=False, classes='title')
            with VerticalScroll(id='profile-details-body'):
                yield Markdown(safe_text(self.guide))
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='close-profile-guide', variant='primary')

    @on(Button.Pressed, '#close-profile-guide')
    def action_close(self):
        self.dismiss()


class ProfileDetails(ModalScreen[None]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, name, description, prompt):
        super().__init__()
        self.profile_name = name
        self.description = description
        self.prompt = prompt

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Profile · ' + self.profile_name, markup=False, classes='title')
            with VerticalScroll(id='profile-details-body'):
                if self.description and self.description != self.profile_name:
                    yield Static(safe_text(self.description), classes='section-intro', markup=False)
                yield Label('System prompt addendum', classes='group-title')
                yield Static(safe_text(self.prompt) if self.prompt else 'This profile has no system prompt addendum.',
                             id='profile-system-prompt', markup=False)
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Close', id='profile-details-close', variant='primary')

    @on(Button.Pressed, '#profile-details-close')
    def action_close(self):
        self.dismiss()


class PackagesScreen(Screen):
    BINDINGS = [('escape', 'back', 'Back')]
    INTRO = {
        'profile': ('A profile’s Dockerfile adds software dependencies to Factory’s base image. '
                    'It can also include skills, tools and instructions for Pi.'),
        'provider': ('Providers connect Pi to models. A provider plugin configures a model service or local server '
                     'and makes its models available in Pi.'),
    }

    def __init__(self, initial='profile'):
        super().__init__()
        self.initial_tab = initial

    def compose(self):
        yield Header()
        with Vertical(classes='form', id='packages-page'):
            yield Label('Library', classes='title')
            with TabbedContent(initial=self.initial_tab, id='package-kind'):
                for kind, label in (('profile', 'Profiles'), ('provider', 'Providers')):
                    with TabPane(label, id=kind):
                        with VerticalScroll(classes='package-pane'):
                            yield Static(self.INTRO[kind], classes='section-intro', markup=False)
                            yield Vertical(id=f'{kind}-packages', classes='package-list')
                            yield Static('Package updates apply when you create a sandbox or update its sources.',
                                         classes='setting-help')
                with TabPane('Skills', id='skills'):
                    with VerticalScroll(classes='package-pane'):
                        yield SkillLibrary()
                with TabPane('AWS access', id='aws-profiles'):
                    with VerticalScroll(classes='package-pane'):
                        yield AWSProfileChoices()
                with TabPane(SYSTEM_PROMPT_LIBRARY_LABEL, id='system-prompts'):
                    with VerticalScroll(classes='package-pane'):
                        yield PromptLibrary()
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='packages-back')
            yield Button('Install profile…', id='package-install', variant='primary')
        yield Footer()

    @property
    def kind(self):
        return self.query_one('#package-kind', TabbedContent).active

    async def on_mount(self):
        await self.show_packages()
        self.query_one(Tabs).focus()

    async def on_screen_resume(self):
        if self.is_mounted and self.kind != 'system-prompts':
            await self.show_packages()

    @on(TabbedContent.TabActivated, '#package-kind')
    async def changed(self):
        await self.show_packages()

    async def show_packages(self):
        kind = self.kind
        if not kind:
            return
        install = self.query_one('#package-install', Button)
        install.display = kind != 'aws-profiles'
        if kind == 'aws-profiles':
            return
        if kind == 'skills':
            install.label = 'Add skills…'
            await self.query_one(SkillLibrary).refresh_library()
            return
        if kind == 'system-prompts':
            install.label = 'New add-on…'
            await self.query_one(PromptLibrary).refresh_library()
            return
        install.label = 'Install ' + kind + '…'
        listing = self.query_one(f'#{kind}-packages', Vertical)
        await listing.remove_children()
        installed = registry.installed(kind)
        summaries = registry.profile_summaries() if kind == 'profile' else {}
        await listing.mount(*(PackageRow(kind, name, description, installed.get(name), summaries.get(name, ''))
                              for name, description in registry.choices(kind).items()))

    def run_package_action(self, kind, action, name):
        service = self.app.service
        self.app.start_task(None, action.capitalize() + ' ' + kind + ' · ' + name,
                            lambda log: service.run([kind, action, name], log), targets=(f'{kind}:{name}',))

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'packages-back':
            self.action_back()
            return
        if event.button.id == 'package-install':
            if self.kind == 'skills':
                self.query_one(SkillLibrary).add_skills()
            elif self.kind == 'system-prompts':
                self.query_one(PromptLibrary).edit()
            else:
                self.app.push_screen(InstallPackage(self.kind))
            return
        row = event.button.parent.parent
        if not isinstance(row, PackageRow):
            return
        kind, name, action = row.kind, row.package_name, event.button.name
        if action == 'details':
            try:
                path = registry.resolve('profile', name)
                metadata = registry.manifest('profile', path)
                prompt = registry.profile_system_prompt(path, metadata['name'])
            except (OSError, ValueError) as error:
                self.notify(safe_text(error), severity='error')
            else:
                title = installed_profile_title(name) if row.record else row.description or name
                self.app.push_screen(ProfileDetails(title, row.summary, prompt))
        elif action == 'remove':
            self.app.push_screen(Confirm('Remove ' + kind + ' “' + name + '”? Existing sandboxes keep their saved copy.', 'Remove', confirm_by_default=False),
                                 lambda yes: self.run_package_action(kind, action, name) if yes else None)
        elif action == 'update':
            self.run_package_action(kind, action, name)

    def action_back(self):
        self.dismiss()


class InstallPackage(Screen):
    BINDINGS=[('escape','back','Cancel')]

    def __init__(self, kind):
        super().__init__()
        self.kind=kind

    def compose(self):
        yield Header()
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Label('Install '+self.kind,classes='title')
            with Vertical(classes='setting'):
                yield Label('Git URL or local folder')
                yield PathField('package-source', title=f'Choose local {self.kind} folder')
            with Vertical(classes='setting'):
                yield Label('Local name')
                yield Input(placeholder='reporting' if self.kind=='profile' else 'company',id='package-name')
            with Collapsible(title='Repository subfolder and revision', collapsed=True):
                with Vertical(classes='setting'):
                    yield Label('Subfolder in the repository · optional')
                    yield PathField('package-subdir', title='Choose repository subfolder', placeholder='profiles/reporting')
                with Vertical(classes='setting'):
                    yield Label('Tag, branch or commit · optional')
                    yield Input(id='package-ref')
            yield Static('Install only profiles and providers you trust; they contain executable code.',classes='setting-help')
        yield Feedback('',id='package-error',markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Cancel',id='package-cancel')
            yield Button('Install',id='package-add',variant='primary')
        yield Footer()

    @on(Input.Changed, '#package-source')
    def local_source_changed(self, event):
        value = event.value.strip()
        field = self.query_one('#package-subdir-field', PathField)
        try:
            path = host_platform.local_path(value) if value and '://' not in value else None
            field.root = path if path and path.is_dir() else None
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            field.root = None
        field.query_one(Button).display = field.root is not None

    def on_mount(self):
        self.query_one('#package-subdir-browse', Button).display = False

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        if event.button.id=='package-cancel':self.action_back();return
        name=self.query_one('#package-name',Input).value.strip()
        source=self.query_one('#package-source',Input).value.strip()
        ref=self.query_one('#package-ref',Input).value.strip()
        subdir=self.query_one('#package-subdir',Input).value.strip()
        invalid_field = 'package-name'
        try:
            if not registry.NAME.fullmatch(name) or name in ('base','local'):
                raise ValueError(registry.NAME_ERROR + ' The names base and local are reserved.')
            if name in registry.choices(self.kind):
                raise ValueError('That name is already installed. Choose another name or update the existing '+self.kind+'.')
            invalid_field = 'package-source'
            source=registry.source_url(source)
            invalid_field = 'package-subdir'
            if Path(subdir).is_absolute() or '..' in Path(subdir).parts:
                raise ValueError('The subfolder must be inside the repository.')
            invalid_field = 'package-ref'
            if ref.startswith('-') or any(c.isspace() for c in ref):
                raise ValueError('Enter a Git tag, branch or commit without spaces.')
        except ValueError as error:
            message = self.query_one('#package-error', Feedback)
            message.update('Error: ' + str(error))
            if invalid_field in ('package-subdir', 'package-ref'):
                self.query_one(Collapsible).collapsed = False
            field = self.query_one('#' + invalid_field, Input)
            field.focus()
            self.call_after_refresh(field.scroll_visible, animate=False)
            return
        args=[self.kind,'add',name,source]
        if ref:args+=['--ref',ref]
        if subdir:args+=['--subdir',subdir]
        app, kind = self.app, self.kind
        service = app.service
        app.pop_screen()
        app.start_task(None, f'Install {kind} · {name}', lambda log: service.run(args, log),
                       targets=(f'{kind}:{name}',))

    def action_back(self):self.app.pop_screen()


class CopySandboxHelp(ModalScreen[None]):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        config = self.record['config']
        with Vertical(id='confirm-box'):
            yield Label('Copying a sandbox', classes='title')
            with VerticalScroll(id='copy-help-body'):
                with Vertical(classes='setting'):
                    yield Label('Settings', classes='section-heading')
                    yield Static('The new sandbox copies the source’s network rules, tools and other settings. '
                                 'Next, choose a new name and workspace, then review.', markup=False)
                with Vertical(classes='setting'):
                    yield Label('Profile', classes='section-heading')
                    yield Static('The new sandbox starts with the profile version installed in the source sandbox, ' + self.record['name'] + '.', markup=False)
                    if editable := config.get(profile_edit.EDIT):
                        yield Details([('Profile folder', editable['path'])])
                if editable:
                    with Vertical(classes='setting'):
                        yield Label('Sharing the profile folder', classes='section-heading')
                        yield Static('Select Share the profile folder so both sandboxes use the same profile files. '
                                     'Leave it unchecked for a separate copy of the installed profile.', markup=False)
                    with Vertical(classes='setting'):
                        yield Label('Installing later profile edits', classes='section-heading')
                        yield Static('Editing files does not update either sandbox. In each sandbox you want to update, choose '
                                     'Actions… → Profile and services… → Editable profile… → Review changes… → Apply profile changes.', markup=False)
                        yield Static('To edit a separate copy later, choose Actions… → Profile and services… → Make profile editable…', markup=False)
                with Vertical(classes='setting'):
                    yield Label('Project files', classes='section-heading')
                    yield Static('Project files are not copied. Choosing the same workspace shares its files.', markup=False)
                if profile_services.selected(config):
                    with Vertical(classes='setting'):
                        yield Label('Application data', classes='section-heading')
                        yield Static('Application data, such as databases, is not copied. Profile services initialize new data.', markup=False)
                with Vertical(classes='setting'):
                    yield Label('Credentials and chats', classes='section-heading')
                    yield Static('Model and advisor credentials, including sign-ins, are not copied. Set them up on Review sandbox.', markup=False)
                    yield Static('SSH keys, browser sessions and saved chats are not copied.', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='close-copy-help', variant='primary')

    def on_mount(self):
        self.query_one('#close-copy-help', Button).focus()
        self.call_after_refresh(self.fit_dialog)

    def on_resize(self):
        self.call_after_refresh(self.fit_dialog)

    def fit_dialog(self):
        if not self.is_attached:
            return
        dialog, body = self.query_one('#confirm-box'), self.query_one('#copy-help-body')
        if dialog.region.height and body.region.height:
            content = body.get_content_height(dialog.content_size, self.app.size, body.content_size.width)
            dialog.styles.height = dialog.region.height - body.content_size.height + content

    @on(Button.Pressed, '#close-copy-help')
    def action_close(self):
        self.dismiss()


class CreateFromSandbox(EntryDialog):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    CreateFromSandbox { align: center middle; background: #000000 70%; }
    #copy-sandbox-dialog { width: 72; max-width: 95%; height: auto; max-height: 95%; padding: 1 2; background: #34383d; border: solid #f2b544; }
    #copy-sandbox-dialog Static { height: auto; }
    #copy-sandbox-fields { height: 1fr; }
    #copy-source-label { width: 1fr; }
    #copy-error { margin: 1 0 0 0; }
    """

    def __init__(self, records, selected=None):
        super().__init__()
        self.records = {r['name']: r for r in records if not r.get('creation')
                        and isinstance(r.get('config', {}).get('profile'), dict)
                        and all(key in r['config']['profile'] for key in ('name', 'path', 'digest'))}
        if not self.records:
            raise ValueError('No completed sandbox is available to copy. Finish creating a sandbox, then try Create from existing again.')
        self.selected = selected if selected in self.records else next(iter(self.records))
        self._copy_request = None

    def compose(self):
        with Vertical(id='copy-sandbox-dialog'):
            yield Label('Create from existing sandbox', classes='title')
            with VerticalScroll(id='copy-sandbox-fields', classes='spaced-form-fields'):
                with Vertical(classes='setting', id='copy-source-setting'):
                    with Horizontal(classes='profile-guide-row'):
                        yield Label('Copy settings from', id='copy-source-label')
                        yield Button('More information…', id='copy-information')
                    yield Select([(name, name) for name in self.records], value=self.selected,
                                 allow_blank=False, id='copy-source')
                with Vertical(classes='setting', id='copy-profile-setting'):
                    yield Checkbox('Share the profile folder', value=False, id='copy-share-profile')
                    yield Static('Both sandboxes use the same editable profile files.', classes='setting-help', id='copy-share-help')
                with Vertical(classes='setting', id='copy-credentials'):
                    yield Checkbox('Include Web Search API keys', value=False, id='copy-keys')
                    yield Static('', id='copy-keys-help', classes='setting-help', markup=False)
                yield Feedback('', id='copy-error', classes='error', markup=False)
            with Horizontal(classes='buttons dialog-actions'):
                yield Button('Cancel', id='copy-cancel')
                yield Button('Continue', id='copy-continue', variant='primary')

    def on_mount(self):
        self._copy_request = None
        self.query_one('#copy-source', Select).disabled = False
        self.query_one('#copy-share-profile', Checkbox).disabled = False
        self.query_one('#copy-continue', Button).disabled = False
        self.query_one('#copy-information', Button).disabled = False
        self.query_one('#copy-continue', Button).label = 'Continue'
        self.source_changed()

    def on_resize(self):
        self.call_after_refresh(self.fit_dialog)

    def fit_dialog(self):
        if not self.is_mounted:
            return
        dialog = self.query_one('#copy-sandbox-dialog')
        fields = self.query_one('#copy-sandbox-fields')
        if dialog.region.height and fields.region.height:
            # Fit the content and fixed actions; CSS caps it at the viewport.
            content = fields.get_content_height(dialog.content_size, self.app.size, fields.content_size.width)
            dialog.styles.height = dialog.region.height - fields.content_size.height + content
            self.call_after_refresh(self.reveal_copy_feedback)

    def reveal_copy_feedback(self):
        if not self.is_mounted:
            return
        error = self.query_one('#copy-error', Feedback)
        if error.display:
            fields = self.query_one('#copy-sandbox-fields')
            focused = self.app.focused
            if (self.app.screen is self and focused is not None and fields in focused.ancestors
                    and focused.region.height + error.outer_size.height > fields.content_size.height):
                fields.focus()
            error.scroll_visible(animate=False, immediate=True)
        elif self.app.focused is self.query_one('#copy-source', Select):
            self.query_one('#copy-sandbox-fields').scroll_home(animate=False, immediate=True)

    @on(Select.Changed, '#copy-source')
    def source_changed(self):
        if self._copy_request is not None:
            return
        self.query_one('#copy-error', Static).update('')
        record = self.records[self.query_one('#copy-source', Select).value]
        self.query_one('#copy-profile-setting').display = bool(record.get('config', {}).get(profile_edit.EDIT))
        self.query_one('#copy-share-profile', Checkbox).value = False
        checkbox = self.query_one('#copy-keys', Checkbox)
        checkbox.value = False
        native = coding_agent(record.get('config', {})) != 'pi'
        checkbox.display = not native and has_web_search_keys(record.get('config', {}))
        self.query_one('#copy-credentials').display = checkbox.display
        checkbox.disabled = native or record['dev'] != 'running'
        help_text = self.query_one('#copy-keys-help', Static)
        help_text.display = checkbox.display and checkbox.disabled
        help_text.update('Start the source sandbox to copy its Web Search API keys, or continue without them.')
        groups = [self.query_one('#' + name) for name in ('copy-source-setting','copy-profile-setting','copy-credentials')]
        last = next(group for group in reversed(groups) if group.display)
        for group in groups:
            group.set_class(group is last, 'last-copy-setting')
        self.query_one('#copy-sandbox-fields').scroll_home(animate=False)
        self.call_after_refresh(self.fit_dialog)

    def action_cancel(self):
        self._copy_request = None
        self.dismiss(None)

    def on_unmount(self):
        self._copy_request = None

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if not self.is_mounted:
            return
        if event.button.id == 'copy-cancel':
            self.action_cancel()
            return
        if event.button.id == 'copy-information':
            if self._copy_request is None:
                self.app.push_screen(CopySandboxHelp(self.records[self.query_one('#copy-source', Select).value]))
            return
        if event.button.id != 'copy-continue':
            return
        if self._copy_request is not None:
            return
        request = self._copy_request = object()
        self.query_one('#copy-error', Static).update('')
        self.call_after_refresh(self.fit_dialog)
        button = self.query_one('#copy-continue', Button)
        record = self.records[self.query_one('#copy-source', Select).value]
        shared = bool(record['config'].get(profile_edit.EDIT)) and self.query_one('#copy-share-profile', Checkbox).value
        keys = self.query_one('#copy-keys', Checkbox)
        include_keys = keys.display and not keys.disabled and keys.value
        disabled = {selector: self.query_one(selector).disabled for selector in
                    ('#copy-source', '#copy-share-profile', '#copy-keys', '#copy-information')}
        button.disabled = True
        button.label = 'Preparing…'
        for selector in disabled:
            self.query_one(selector).disabled = True
        self.prepare_copy(request, record, shared, include_keys, disabled)

    @work(exit_on_error=False)
    async def prepare_copy(self, request, record, shared, include_keys, disabled):
        if self._copy_request is not request or not self.is_mounted:
            return
        completed = False
        phase = 'reading saved settings'
        try:
            draft = await asyncio.to_thread(copy_draft, record, self.app.state_root)
            if self._copy_request is not request or not self.is_mounted:
                return
            draft['share_editable_profile'] = shared
            keys = {}
            if include_keys:
                phase = 'copying Web Search API keys'
                stored = await asyncio.to_thread(self.app.service.search_keys, record['name'])
                if self._copy_request is not request or not self.is_mounted:
                    return
                keys = web_access.matching_keys(stored, options(record['config'].get('options'))['web_access'])
            phase = 'preparing copied settings'
            for name, row in draft['options']['web_access']['providers'].items():
                row['has_key'] = bool(keys.get(name))
            if self._copy_request is request and self.is_mounted:
                completed = True
                self.app.call_after_refresh(self.finish_copy, request, draft, keys)
        except Exception as error:
            if self._copy_request is request and self.is_mounted:
                advice = ('Try copying again, or leave Include Web Search API keys unchecked.'
                          if phase == 'copying Web Search API keys' else
                          'Check the source settings, then try Continue again.')
                cause = safe_text(error).strip() or 'No error details were returned'
                punctuation = '' if cause.endswith(('.', '!', '?')) else '.'
                next_step = '' if cause.endswith(advice) else ' ' + advice
                self.query_one('#copy-error', Static).update(
                    f'Cannot prepare a copy of {record["name"]} while {phase}: '
                    f'{type(error).__name__}: {cause}{punctuation}{next_step}')
                self.query_one('#copy-error').scroll_visible(animate=False)
                if phase == 'copying Web Search API keys':
                    self.query_one('#copy-keys', Checkbox).focus()
                    self.query_one('#copy-keys', Checkbox).scroll_visible(top=True, animate=False)
        finally:
            if self._copy_request is request and not completed:
                self._copy_request = None
                if self.is_mounted:
                    button = self.query_one('#copy-continue', Button)
                    button.disabled = False
                    button.label = 'Continue'
                    for selector, previous in disabled.items():
                        self.query_one(selector).disabled = previous
                    self.call_after_refresh(self.fit_dialog)

    def finish_copy(self, request, draft, keys):
        if self._copy_request is request and self.is_mounted:
            self.dismiss((draft, keys))


class CreationRequest(NamedTuple):
    draft: dict
    keys: dict
    provider_label: str | None
    guide: bool


class NewSandbox(Screen):
    def __init__(self, draft=None, search_keys=None):
        super().__init__()
        self.copied_draft = draft
        self.connector_draft = {}
        self.search_keys = search_keys
        self.AUTO_FOCUS = '#workspace' if draft else '#primary-agent'

    BINDINGS = [Binding('escape', 'back', 'Back')]

    def compose(self):
        settings = self.copied_draft or self.app.defaults
        profiles = profile_titles()
        self.summaries = registry.profile_summaries()
        self.profile_paths = {name: row['path'] for name, row in registry.installed('profile').items()}
        if self.copied_draft:
            source = settings['reused_profile']
            title = source.get('description') or source['name']
            profiles[settings['profile']] = title + (' (saved customized profile)' if source.get('customized_from') else ' (saved profile)')
            self.profile_paths[settings['profile']] = source['path']
        self.profiles = profiles
        yield Header()
        yield Label('New sandbox · 1 of 2' + (' · from ' + settings['reuse_from'] if self.copied_draft else ''),
                    classes='title focused-settings-title', id='new-title')
        with VerticalScroll(classes='form'):
            with Vertical(id='coding-agent-setting', classes='setting'):
                yield Label('Coding agent', id='primary-agent-label')
                yield Select([(label, agent) for agent, label in PRIMARY_AGENTS.items()],
                             value=coding_agent(settings), allow_blank=False, id='primary-agent')
            with Vertical(id='profile-setting', classes='setting'):
                with Horizontal(classes='profile-guide-row'):
                    yield Label('Profile', id='profile-label')
                    yield Static('', classes='action-spacer')
                    yield Button('Getting started…', id='new-profile-guide')
                yield Select([(v, k) for k, v in profiles.items()] + [('Custom profile…', 'custom')],
                             value=settings['profile'] if settings['profile'] in profiles else 'custom',
                             allow_blank=False, id='profile')
                yield Static('', id='profile-summary', classes='setting-help', markup=False)
            with Vertical(id='custom-profile-fields', classes='setting'):
                yield Label('Custom profile folder')
                yield PathField('custom-profile', title='Choose custom profile folder', placeholder='~/profiles/my-profile')
                yield Static('Folder containing profile.json, Dockerfile and profile.md.', classes='setting-help')
            with Vertical(classes='setting'):
                yield Label('Workspace')
                yield PathField('workspace', settings['workspace_parent'], title='Choose workspace folder', browse_id='browse')
                yield Checkbox('Create this workspace folder if it does not exist', id='mkdir')
            with Vertical(id='sandbox-name-setting', classes='setting'):
                yield Label('Sandbox name')
                yield Input(id='name')
            yield Feedback('', id='new-error', classes='error', markup=False)
        with Horizontal(classes='wizard-actions'):
            yield Button('Cancel', id='cancel')
            yield Button('Continue', id='continue', variant='primary')
        yield Footer()

    def on_mount(self):
        self.refresh_profiles()
        self.profile_changed()

    def refresh_profiles(self):
        agent = self.query_one('#primary-agent', Select).value
        selector = self.query_one('#profile', Select)
        choices = []
        for name, label in self.profiles.items():
            try:
                metadata = (self.copied_draft['reused_profile'] if self.copied_draft and name == self.copied_draft['profile']
                            else json.loads((Path(self.profile_paths.get(name, sandbox.ROOT / 'profiles' / name))
                                             / 'profile.json').read_text()))
                if profile_spec.compatible(metadata, agent):
                    choices.append((label, name))
            except (OSError, ValueError):
                continue
        current = selector.value
        selector.set_options([*choices, ('Custom profile…', 'custom')])
        selector.value = current if current == 'custom' or any(value == current for _, value in choices) else 'base'

    @on(Select.Changed, '#primary-agent')
    def coding_agent_changed(self):
        self.refresh_profiles()
        self.profile_changed()
        self.suggest_name()

    @on(Select.Changed, '#profile')
    def profile_changed(self):
        profile = self.query_one('#profile', Select).value
        self.query_one('#profile-setting').display = True
        self.query_one('#custom-profile-fields').display = profile == 'custom'
        summary = '' if self.copied_draft else self.summaries.get(profile, '')
        helper = self.query_one('#profile-summary', Static)
        helper.update(shorten(safe_text(summary), width=130, placeholder='…'))
        helper.display = bool(summary)
        self.guide = ''
        if isinstance(profile, str) and profile != 'custom':
            path = self.profile_paths.get(profile, sandbox.ROOT / 'profiles' / profile)
            try:
                self.guide = registry.profile_guide(path)
            except (OSError, ValueError):
                pass  # Continue reports package validation errors in the form.
        self.query_one('#new-profile-guide').display = bool(self.guide)

    @on(Input.Changed, '#workspace')
    def suggest_name(self):
        current = self.query_one('#name', Input)
        if not current.value or current.value == getattr(self, 'suggested', ''):
            workspace = self.query_one('#workspace', Input).value
            stem = re.sub('[^a-z0-9-]+', '-', Path(workspace).name.lower()).strip('-')
            agent = self.query_one('#primary-agent', Select).value
            suffixes = {'codex': '-codex', 'claude': '-claude', 'agy': '-antigravity'}
            suffix = suffixes.get(agent, '')
            self.suggested = stem[:SANDBOX_NAME_LIMIT - len(suffix)].rstrip('-') + suffix if stem else ''
            current.value = self.suggested

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'cancel':
            self.action_back()
        elif event.button.id == 'new-profile-guide':
            profile = self.query_one('#profile', Select).value
            self.app.push_screen(ProfileGuide(profile_titles().get(profile, profile), self.guide))
        elif event.button.id == 'continue':
            focus = '#profile'
            try:
                agent = self.query_one('#primary-agent', Select).value
                profile = self.query_one('#profile', Select).value
                if profile == 'custom':
                    focus = '#custom-profile'
                    profile = self.query_one('#custom-profile', Input).value
                    path = host_platform.local_path(profile)
                    if not path.is_dir():
                        problem = 'does not exist' if not path.exists() else 'is not a folder'
                        raise ValueError(f'Profile folder {profile!r} {problem}. Choose an existing folder containing profile.json, Dockerfile and profile.md.')
                metadata = (self.copied_draft['reused_profile'] if self.copied_draft and profile == self.copied_draft['reused_profile']['path']
                            else sandbox.load_profile(profile))
                if not profile_spec.compatible(metadata, agent):
                    raise ValueError(f'Profile {metadata["name"]!r} does not declare support for {PRIMARY_AGENTS[agent]}. Choose a compatible profile.')
                focus = '#name'
                name = self.query_one('#name', Input).value.strip()
                if not SANDBOX_NAME.fullmatch(name):
                    raise ValueError(SANDBOX_NAME_ERROR)
                if (self.app.state_root / name).exists():
                    raise ValueError('That name already exists. Choose another name.')
                focus = '#workspace'
                workspace_text = self.query_one('#workspace', Input).value.strip()
                if not workspace_text:
                    raise ValueError('Choose a workspace folder.')
                workspace = host_platform.local_path(workspace_text).resolve()
                if host_platform.comparison_path(workspace) in (Path('/'), host_platform.comparison_path(Path.home())):
                    raise ValueError('Choose a project folder, not your home folder or filesystem root.')
                if not workspace.exists():
                    if not self.query_one('#mkdir', Checkbox).value:
                        raise ValueError('Choose an existing workspace or enable folder creation.')
                elif not workspace.is_dir():
                    raise ValueError('Workspace is not a directory.')
                draft = (copy_for_agent(self.copied_draft, agent) if self.copied_draft
                         else preset(metadata['name'], self.app.defaults, metadata.get('defaults')))
                draft.update(profile=profile, name=name, workspace=str(workspace),
                             mkdir=self.query_one('#mkdir', Checkbox).value,
                             primary_agent=agent, primary_auth=DEFAULT_AUTH)
                if agent != 'pi' and not self.copied_draft:
                    draft.update(model_providers={}, local_model_ports=None, subagents=False,
                                 allowlist='', env_file='', pi_config='',
                                 editor={'mode': editor_settings.default_for_agent(agent, draft['engine'])})
                    draft['options'] = options({'git_identity': draft['options']['git_identity'],
                                                'website_groups': draft['options']['website_groups']})
                draft.update({k: v for k, v in self.connector_draft.items() if k != 'git_identity'})
                if 'git_identity' in self.connector_draft:
                    draft['options']['git_identity'] = self.connector_draft['git_identity']
                self.app.push_screen(SettingsForm(draft, search_keys=self.search_keys if agent == 'pi' else {},
                                                  on_create=self.start_creation),
                                     self.remember_connectors)
            except (OSError, ValueError) as error:
                feedback = self.query_one('#new-error', Feedback)
                feedback.update(str(error))
                self.query_one(focus).focus()
                self.call_after_refresh(feedback.scroll_visible, animate=False)

    def remember_connectors(self, values):
        if values is not None:
            self.connector_draft = values

    def start_creation(self, request):
        app = self.app
        self.dismiss()
        app.call_later(app.start_creation, request)

    def action_back(self):
        self.app.pop_screen()


class PrimarySignIn(ModalScreen):
    AUTO_FOCUS = '#primary-login'
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, record):
        super().__init__()
        self.record = record

    def compose(self):
        agent = coding_agent(self.record['config'])
        with Vertical(id='confirm-box'):
            yield Label(SIGN_IN_LABEL + ' · ' + PRIMARY_AGENTS[agent], classes='title')
            yield Static('The sign-in is stored outside the workspace.',
                         classes='setting-help')
            with Vertical(classes='setting sign-in-actions'):
                yield Button('Sign in', id='primary-login', variant='primary')
                if can_copy_primary_sign_in(agent):
                    yield Button('Copy sign-in from this computer', id='primary-import')
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='sign-in-close')

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        action = event.button.id
        if action == 'sign-in-close':
            self.action_close()
        elif action == 'primary-login':
            self.dismiss()
            self.app.interactive('primary-login', self.record['name'])
        elif action == 'primary-import':
            name = self.record['name']
            def replace(yes):
                if yes:
                    self.dismiss()
                    self.app.operation('Copying sign-in', ['primary-import', name],
                                       success_message='Sign-in copied. Choose Open to use it.')
            confirmation = Confirm('Replace the saved sign-in with the account signed in on this computer?',
                                   'Copy sign-in', confirm_by_default=True)
            confirmation.add_class('sign-in-confirm')
            self.app.push_screen(confirmation, replace)

    def action_close(self):
        self.dismiss()


class SettingsForm(Screen):
    BINDINGS = [Binding('escape', 'back', 'Back'), Binding('escape', 'cancel_edit', 'Cancel')]

    def __init__(self, draft, editing=False, defaults=False, search_keys=None, section=None, setting=None,
                 on_create=None):
        super().__init__(classes='settings-review' if not editing and not defaults else None)
        self.draft, self.editing, self.defaults_mode = draft, editing, defaults
        self.on_create = on_create
        self.provider_key_choices = dict(draft.get('_provider_credential_choices', {}))
        self.local_key_choices = dict(draft.get('_local_model_keys', {}))
        self.setting = setting
        self.set_class(setting == 'resources' or defaults, 'bounded-form')
        self.primary = coding_agent(draft)
        self.native = self.primary != 'pi'
        self.categories = primary_categories(self.primary) if self.native else CATEGORIES
        if self.native:
            self.AUTO_FOCUS = '#' + self.categories[setting].focus if setting else '#primary-auth'
        self.provider_forms = {}
        self.set_class(self.native, 'native-primary-form')
        selected = options(draft.get('options'))
        self.document_search = {key: selected[key] for key in ('knowledge_embedding', 'knowledge_embedding_provider',
                                                               'knowledge_embedding_url', 'knowledge_embedding_model')}
        self.document_search_sources = embedding_models.sources(draft) if isinstance(draft.get('model_providers'), list) else {}
        self.allow_offline_search = editing and self.document_search['knowledge_embedding'] == 'local'
        if self.document_search['knowledge_embedding'] == 'local' and not editing:
            self.document_search['knowledge_embedding'] = 'off'
        self.initial_section = (self.categories[setting].section if self.native else EDIT_FORMS[setting].section) if setting else section
        self.search_keys = search_keys
        self.profile = (draft['reused_profile'] if draft.get('reused_profile') and draft['profile'] == draft['reused_profile']['path']
                        else sandbox.load_profile(draft['profile']))
        self.profile_name = self.profile['name']
        self.guide = registry.profile_guide(self.profile['path']) if not (editing or defaults) else ''

    def compose(self):
        value, selected = self.draft, options(self.draft.get('options'))
        review = self.has_class('settings-review')
        yield Header()
        if self.setting:
            category = self.categories[self.setting] if self.native else EDIT_FORMS[self.setting]
            yield Label(category.label + ' · ' + value['name'],
                        classes='title focused-settings-title')
        elif review:
            origin = ' · from ' + value['reuse_from'] if value.get('reuse_from') else ''
            yield Label('Review sandbox · 2 of 2' + origin, classes='title focused-settings-title')
        with VerticalScroll(classes='form'):
            if not self.setting and not review:
                yield Label('Defaults for new sandboxes' if self.defaults_mode else 'Sandbox settings', classes='title')
            if self.defaults_mode:
                with Vertical(classes='spaced-form-fields default-profile-setting'):
                    with Vertical(classes='setting'):
                        yield Label('Default profile')
                        yield Select([(v, k) for k, v in profile_titles().items()], value=self.profile_name if self.profile_name in profile_titles() else 'base',
                                     allow_blank=False, id='default-profile')
                with Vertical(classes='setting'):
                    yield Label('Workspace parent folder')
                    yield PathField('workspace-parent', value.get('workspace_parent', ''), title='Choose workspace parent folder')
            if not self.defaults_mode and not self.setting:
                with Vertical(classes='setting review-profile-summary'):
                    with Horizontal(classes='profile-guide-row review-profile-row'):
                        yield Static(PRIMARY_AGENTS[self.primary] + ' · ' + value['name'], markup=False)
                        if self.guide:
                            yield Button('Getting started…', id='review-profile-guide')
                    copied_profile = value.get('reuse_from') and value.get('reused_profile', {}).get('path') == value['profile']
                    title = self.profile.get('description') or self.profile_name
                    if copied_profile:
                        title += ' (saved customized profile)' if self.profile.get('customized_from') else ' (saved profile)'
                    project = Details([('Profile', title if copied_profile else profile_titles().get(self.profile_name, self.profile_name)),
                                       ('Workspace', value['workspace'])], id='review-project')
                    project.tooltip = value['workspace']
                    yield project
                    if copied_profile and value.get('source_editable_profile'):
                        if shares_editable_profile(value):
                            yield Details([('Profile folder', 'shared with ' + value['reuse_from'] + '. Editing files does not update either sandbox automatically.')],
                                          id='review-profile-copy')
                        else:
                            yield Details([('Profile folder', 'not shared. To edit the new sandbox’s profile later, choose Actions… → Profile and services… → Make profile editable…')],
                                          id='review-profile-copy')
            if review and self.profile.get('services'):
                with Vertical(classes='setting', id='review-profile-services'):
                    with Horizontal(classes='profile-guide-row review-profile-row'):
                        yield Static('Profile services', classes='section-heading')
                        yield Button('More information…', id='profile-services-information')
                    yield Details([(name, spec['description']) for name, spec in self.profile['services'].items()])
            if value.get('_copy_source_agent') is not None:
                yield Static(f'Settings that apply to every agent are copied. {PRIMARY_AGENTS[self.primary]} model and feature settings use defaults.',
                             classes='setting-help', id='copied-settings')
            for action, category in self.categories.items():
                if self.native and self.editing and action != self.setting:
                    continue
                compose_category = getattr(self, 'compose_' + action.replace('-', '_'))
                yield from compose_category(category, value, selected)
            yield Feedback('', id='form-error', classes='error', markup=False)
        with Vertical(classes='wizard-actions settings-actions'):
            yield Static('', id='curlie-download-notice', classes='setting-help')
            mode = (editor_settings.load(self.app.state_root / value['name'])['mode'] if self.editing and not self.native
                    else value.get('editor', editor_settings.DEFAULTS)['mode'])
            yield Checkbox(self.open_client_label(mode), value=False if self.editing else value.get('open_editor', True),
                           id='open-editor')
            with Horizontal(classes='action-buttons'):
                yield Button('Cancel' if self.editing else 'Back', id='back')
                if not self.defaults_mode:
                    yield Button(cli_recipes.LABEL, id='settings-cli')
                apply_label = 'Save defaults' if self.defaults_mode else 'Create and start sandbox'
                if self.editing:
                    apply_label = ('Save' if self.setting == 'primary-approvals' else
                                   'Apply' if self.setting in ('approval-settings', 'network-settings', 'resources') else 'Apply and recreate')
                yield Button(apply_label, id='apply', variant='primary')
        yield Footer()

    def open_client_label(self, mode):
        client = 'the browser editor' if mode == 'browser' else editor_settings.modes(self.primary)[mode]
        return 'Open ' + client + (' after applying' if self.editing else ' when ready')

    @on(Select.Changed, '#editor-mode')
    def update_open_client_label(self, event):
        self.query_one('#open-editor', Checkbox).label = self.open_client_label(event.value)

    def compose_primary_sign_in(self, category, value, selected):
        with Collapsible(title=category.label, collapsed=False, id=category.section,
                         classes='spaced-form-fields'):
            copy_supported = can_copy_primary_sign_in(self.primary)
            if not copy_supported:
                yield Static('Sign in the first time you open the sandbox. The sign-in is stored outside the workspace.',
                             classes='setting-help')
                return
            with Vertical(classes='setting'):
                yield Label('How to sign in')
                yield Select(SIGN_IN_OPTIONS, value=value.get('primary_auth', DEFAULT_AUTH),
                             allow_blank=False, id='primary-auth')
                yield Static('', classes='setting-help', id='primary-auth-help')

    @on(Select.Changed, '#primary-auth')
    def primary_auth_changed(self):
        method = self.query_one('#primary-auth', Select).value
        self.query_one('#primary-auth-help', Static).update(SIGN_IN_HELP[method] + '\nThe sign-in is stored outside the workspace.')

    def compose_primary_approvals(self, category, value, selected):
        with Collapsible(title=category.label, collapsed=True, id=category.section,
                         classes='spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Label('Tool approvals')
                label = {'codex': 'Approve for me', 'claude': 'Auto',
                         'agy': 'Allow configured tools'}[self.primary]
                yield Select([(label, 'automatic'), ('Ask for approval', 'manual')],
                             value=selected['native_approval'], allow_blank=False, id='native-approval', classes='native-approval-choice')
                yield Static({'codex': 'Codex reviews actions that need permission.',
                              'claude': 'Claude reviews tool requests automatically.',
                              'agy': 'Allows the workspace tools enabled for this sandbox.'}[self.primary]
                             if selected['native_approval'] == 'automatic' else
                             'You decide when the agent requests permission.', classes='setting-help', id='native-approval-help')

    @on(Select.Changed, '#native-approval')
    def native_approval_changed(self):
        mode = self.query_one('#native-approval', Select).value
        label = ('Ask for approval' if mode == 'manual' else
                 {'codex': 'Approve for me', 'claude': 'Auto', 'agy': 'Allow configured tools'}[self.primary])
        self.query_one('#primary-approvals-section', Collapsible).title = 'Approvals · ' + label
        self.query_one('#native-approval-help', Static).update(
            'You decide when the agent requests permission.' if mode == 'manual' else
            {'codex': 'Codex reviews actions that need permission.',
             'claude': 'Claude reviews tool requests automatically.',
             'agy': 'Allows the workspace tools enabled for this sandbox.'}[self.primary])
        if self.editing:
            self.query_one('#native-approval-help', Static).update(
                ('You decide when the agent requests permission.' if mode == 'manual' else
                 {'codex': 'Codex reviews actions that need permission.',
                  'claude': 'Claude reviews tool requests automatically.',
                  'agy': 'Allows the workspace tools enabled for this sandbox.'}[self.primary])
                + '\nRestart the sandbox to use saved changes.')

    def compose_primary_browser(self, category, value, selected):
        with Collapsible(title=category.label, collapsed=True, id=category.section,
                         classes='spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Checkbox('Enable the browser', id='primary-browser', value=selected['browser'])
                yield Static('The agent can open websites through the workspace network. '
                             'Open the viewer link to see and interact with its pages.', classes='setting-help checkbox-help')

    @on(Checkbox.Changed, '#primary-browser')
    def primary_browser_changed(self):
        enabled = self.query_one('#primary-browser', Checkbox).value
        self.query_one('#primary-browser-section', Collapsible).title = 'Browser · ' + on_off(enabled)

    def compose_connectors(self, category, value, selected):
        if self.defaults_mode:
            from .repository_ui import GitIdentityFields
            identity = selected['git_identity']
            with Collapsible(title='Git commit identity', collapsed=True, id='git-identity-section',
                             classes='spaced-form-fields'):
                yield GitIdentityFields(identity, defaults=True)
        elif not self.editing:
            with Collapsible(title=category.label + ' · ' + connector_summary(value), collapsed=True, id=category.section):
                yield ConnectorFields(value)

    @on(ConnectorFields.Configure)
    def configure_connector(self, event):
        event.stop()
        connector = event.connector
        if not connector.external:
            adapter = ADAPTERS[connector.id]

            def selected(value):
                if value is not None:
                    adapter.apply_draft(self.draft, value)
                    self.update_connector_fields()

            self.app.push_screen(adapter.draft_editor(self.draft, connector), selected)
        else:
            enabled = connector.id in self.draft.get('connector_ids', [])
            granted = self.draft.get('connector_operations', {}).get(connector.id)
            self.app.push_screen(ExternalConnectorDialog(connector, enabled=enabled, enabled_operations=granted, draft=True),
                                 lambda value: self.save_connector_selection(connector, value))

    def update_connector_fields(self):
        self.query_one(ConnectorFields).update_config(self.draft)
        self.query_one('#connectors-section', Collapsible).title = 'Connectors · ' + connector_summary(self.draft)

    def save_connector_selection(self, connector, operations):
        if operations is not None:
            selected = [item for item in self.draft.get('connector_ids', []) if item != connector.id]
            routes = dict(self.draft.get('_connector_routes', {}))
            instructions = dict(self.draft.get('_connector_instructions', {}))
            permissions = dict(self.draft.get('connector_operations', {}))
            if operations:
                selected.append(connector.id)
                permissions[connector.id] = list(operations)
                routes[connector.id] = dict(connector.routes)
                instructions[connector.id] = connector.reviewed_instructions
            else:
                permissions.pop(connector.id, None)
                routes.pop(connector.id, None)
                instructions.pop(connector.id, None)
            self.draft.update(connector_ids=selected, connector_operations=permissions, _connector_routes=routes,
                              _connector_instructions=instructions)
            self.update_connector_fields()

    def validate_ssh_key(self, draft):
        # The user may have changed the workspace or shared folders after choosing the key.
        if draft.get('_ssh_key') == 'import':
            sandbox_ssh.key_source(draft, draft['_ssh_key_file'])

    def compose_approval_settings(self, category, value, selected):
        with Collapsible(title=category.label, collapsed=True, id=category.section, classes='spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Checkbox('Use inner command sandbox', value=selected['inner_sandbox'], id='inner-sandbox')
                yield Static('Runs Bash with file and network limits. Bash can still edit the workspace.',
                             id='inner-sandbox-help', classes='setting-help checkbox-help', markup=False)
            with Vertical(classes='setting'):
                yield Checkbox('Protect common credential files', value=selected['protect_credentials'], id='protect-credentials')
                yield Static('Blocks file tools and sandboxed Bash from reading or writing .env files, private keys '
                             'and similar files.',
                             id='protect-credentials-help', classes='setting-help checkbox-help', markup=False)
            with Vertical(classes='setting'):
                with Horizontal(id='approval-heading'):
                    yield Label('Approval mode')
                    yield Button('? Explain', id='explain-approval')
                yield Select([(label, mode) for mode, (label, _, _) in APPROVAL_MODES.items()],
                             value=selected['approval'], allow_blank=False, id='approval')
                yield Static('', id='selected-approval-help', classes='setting-help', markup=False)

            if not self.defaults_mode:
                yield ReviewerFields(value['name'] if self.editing else None, selected['approval_model'])

    @on(Collapsible.Expanded, '#agent-section')
    def load_approval_models(self):
        if self.query(ReviewerFields) and self.query_one(ReviewerFields).display:
            self.query_one(ReviewerFields).load()

    def compose_models(self, category, value, selected):
        configured=value.get('model_providers',{'local':{}})
        if isinstance(configured,list):configured={r['name']:r.get('settings',{}) for r in configured}
        saved_rows={r['name']:r for r in value.get('model_providers',[]) if isinstance(r,dict)}
        available=registry.choices('provider')
        available.update({name:row['manifest'].get('description',name) for name,row in saved_rows.items() if name not in available})
        self.provider_forms={}
        with Collapsible(title=category.label, collapsed=self.editing or self.defaults_mode, id=category.section, classes='spaced-form-fields'):
            ports = value.get('local_model_ports')
            with Collapsible(title=local_models_title(bool(ports)), id='local-provider'):
                with Vertical(classes='setting', id='local-enable-setting'):
                    yield Checkbox('Connect to models on this computer', value=bool(ports), id='local-models')
                    yield Static('Requires rootless Podman.', classes='setting-help checkbox-help')
                with Vertical(id='local-model-details'):
                    with Vertical(classes='setting', id='model-port-setting'):
                        yield Label('Server ports')
                        yield Input(ports or '8000', id='model-ports')
                        yield Feedback('', id='model-ports-error', markup=False)
                        yield Static('Use comma-separated ports or ranges, for example 8000,8080-8082.',
                                     classes='setting-help')
                    if not self.editing and not self.defaults_mode:
                        with Vertical(classes='setting'):
                            yield Button(LOCAL_KEYS_LABEL, id='local-server-keys')
            if not self.editing and not self.defaults_mode:
                yield HostedProviders(changes=value.get('_hosted_changes'))
            elif self.setting != 'model-sources':
                with Vertical(classes='setting'):
                    yield Label(HOSTED_PROVIDERS_LABEL, classes='group-title model-group-title')
                    yield Static(hosted_provider_names(), markup=False)
                    yield Static(f'Configure hosted services in {MODEL_PROVIDERS_PATH} without recreating the sandbox.'
                                 if self.editing else f'Connect hosted services during creation or later in {MODEL_PROVIDERS_PATH}.',
                                 classes='setting-help')
            if any(name != 'local' for name in available):
                yield Label(PROVIDER_PLUGINS_LABEL, classes='group-title model-group-title')
            for name,label in available.items():
                if name=='local':continue
                info=saved_rows[name]['manifest'] if name in saved_rows else registry.manifest('provider',registry.resolve('provider',name))
                self.provider_forms[name]=info
                with Vertical(classes='setting'):
                    yield Checkbox(label,value=name in configured,id='source-'+name)
                    with Vertical(id='source-details-'+name, classes='provider-details') as details:
                        details.display = name in configured
                        if info.get('network'):
                            yield Static('Required network access · '+', '.join(info['network']),
                                         classes='setting-help provider-setting', markup=False)
                        for key,spec in info.get('settings',{}).items():
                            secret = spec['type']=='secret'
                            if secret and (self.editing or self.defaults_mode):continue
                            with Vertical(classes='setting provider-setting'):
                                if secret and (binding := provider_credentials.scope(name, info.get('inference'))):
                                    choice = self.provider_key_choices.get(name)
                                    yield Static(choice_key_caption(binding, choice), id='source-key-label-'+name, markup=False)
                                    yield Button('Change key…' if choice else 'Choose key…',
                                                 id='source-'+name+'-'+key, name='provider-key:'+name,
                                                 disabled=name not in configured)
                                    continue
                                yield Label(spec.get('label',key))
                                yield Input('' if secret else configured.get(name,{}).get(key,spec.get('default','')),
                                            password=secret, disabled=name not in configured, id='source-'+name+'-'+key)
                                if secret:
                                    yield Static(('Stored in the separate inference container. ' if info.get('inference') else '')
                                                 + f'Optional. Configure later in {MODEL_PROVIDERS_PATH}.', classes='setting-help')
                                    yield Feedback('', id='source-'+name+'-'+key+'-error', markup=False)
            yield Static('Open Library… from the home screen to install provider plugins.', classes='setting-help')

    def compose_editor_settings(self, category, value, selected):
        if not self.editing:
            mode = value.get('editor', editor_settings.DEFAULTS)['mode']
            with Collapsible(title=category.label + ' · ' + editor_settings.modes(self.primary)[mode], collapsed=True, id=category.section):
                yield EditorFields(value.get('editor', editor_settings.DEFAULTS), connection_settings=not self.defaults_mode,
                                   agent=self.primary)

    def compose_folders(self, category, value, selected):
        if not self.editing and not self.defaults_mode:
            with Collapsible(title=category.label, collapsed=not bool(value.get('extra_folders')), id=category.section):
                yield FolderFields(value, self.app.state_root)

    def compose_network_settings(self, category, value, selected):
        if self.native:
            with Collapsible(title=category.label, collapsed=True, id=category.section, classes='spaced-form-fields'):
                if key := WEB_SEARCH_OPTIONS.get(self.primary):
                    with Vertical(classes='setting'):
                        yield Checkbox('Enable web search', id=self.primary + '-web-search', value=selected[key])
                        help_text = ('Allows Codex to search OpenAI’s index.' if self.primary == 'codex' else
                                     f'Allows {PRIMARY_AGENTS[self.primary]} to search the web.')
                        if self.editing:
                            help_text += ' Reopen the CLI or reload the VS\u00a0Code window to apply changes.'
                        yield Static(help_text, id=self.primary + '-web-search-help', classes='setting-help checkbox-help')
                yield from self.compose_network_fields(value, selected)
            return
        with Collapsible(title=category.label, collapsed=True, id=category.section, classes='spaced-form-fields'):
            with Collapsible(title=NETWORK_LABEL, collapsed=True, id='network-section'):
                yield from self.compose_network_fields(value, selected)
            yield WebAccessForm(selected['web_access'], self.defaults_mode)

    def compose_network_fields(self, value, selected):
        with Vertical(classes='setting'):
            yield Label(NETWORK_MODE_LABEL)
            yield Select([('Restricted — only allowed destinations', 'allowlist'),
                          ('Unrestricted — ordinary container network', 'unrestricted')],
                         value=value['network'], allow_blank=False, id='network')
            if self.editing:
                yield Static('Changing the network mode recreates the sandbox.', classes='setting-help')
        with Vertical(id='network-rules-setting'):
            profile = self.profile_name if not self.native and (not self.editing or value['network'] == 'unrestricted') else None
            assigned = curlie_catalog.assigned(value.get('website_groups', {})) if self.editing else None
            yield NetworkRulesFields(value.get('allowlist', ''), selected['website_groups'], profile=profile,
                                    assigned=assigned)
            if not self.native and self.editing and value['network'] == 'allowlist':
                search = web_access.destinations(selected['web_access'])
                if search:
                    yield Static(f'Current {WEB_SEARCH_LABEL} destinations · ' + ', '.join(
                        f'{host}:{port}' for host, port in search), markup=False, classes='setting-help')
                channels = communications.destinations(selected['communications'])
                if channels:
                    yield Static('Current Slack destinations · ' + ', '.join(
                        f'{host}:{port}' for host, port in channels), markup=False, classes='setting-help')

    def compose_communications(self, category, value, selected):
        yield CommunicationsForm(selected['communications'], self.defaults_mode, value.get('_communication_keys'))

    def compose_document_search(self, category, value, selected):
        with Collapsible(title=category.label + ' · ' + self.document_search_summary(),
                         collapsed=True, id=category.section):
            if self.setting == 'document-search':
                yield Details([('Search mode', self.document_search_summary())], id='document-search-summary')
            with Horizontal(id='document-search-actions'):
                yield Button('Configure…', id='configure-document-search')
                yield Button('How to search documents…', id='document-search-help')
            yield Static(search_description(self.document_search, self.document_search_sources),
                         id='document-search-effect', classes='setting-help', markup=False)

    def document_search_summary(self):
        mode = search_summary(self.document_search, self.document_search_sources)
        return mode

    @on(Button.Pressed, '#document-search-help')
    def document_search_help(self, event):
        event.stop()
        self.app.push_screen(DocumentSearchHelp())

    @on(Button.Pressed, '#configure-document-search')
    def configure_document_search(self, event):
        event.stop()
        rows = [{'name': name, 'manifest': manifest} for name, manifest in self.provider_forms.items()
                if self.query_one('#source-' + name, Checkbox).value]
        hosted = dict(self.draft.get('hosted_connections', {}))
        credentials = dict(self.provider_key_choices)
        for group in self.query(HostedProviders):
            for name, value in group.changes.items():
                hosted[name] = hosted_models.connection(value)
                credentials[name] = value.get('credential') or {'key': value.get('key', '')}
        config = {'model_providers': rows, 'hosted_connections': hosted}
        dialog = DocumentSearchDialog(self.document_search, self.allow_offline_search, config, credentials,
                                      self.draft if self.editing else None)
        self.document_search_sources = dialog.sources
        self.app.push_screen(dialog, self.document_search_saved)

    def document_search_saved(self, values):
        if values is not None:
            self.document_search = values
            self.query_one('#document-search-effect', Static).update(search_description(values, self.document_search_sources))
            mode = self.document_search_summary()
            self.query_one('#document-search-section', Collapsible).title = CATEGORIES['document-search'].label + ' · ' + mode
            if self.query('#document-search-summary'):
                self.query_one('#document-search-summary', Details).set_rows([('Search mode', mode)])

    def compose_agent_tools(self, category, value, selected):
        with WrappingCollapsible(title=category.label, collapsed=True, id=category.section, classes='spaced-form-fields'):
            yield Checkbox('Enable optional subagents', value=value['subagents'], id='subagents')
            with Vertical(classes='setting'):
                yield Label('Conversation recall')
                yield Select([('Lite — search history on demand', 'lite'),
                              ('Full — automatic history hints', 'full'), ('Off', 'off')],
                             value=selected['compaction_recall'], allow_blank=False, id='compaction-recall')
                yield Static('Finds earlier details after conversation compaction. Full adds automatic hints and may reduce prefix-cache reuse.', classes='setting-help')
            with Vertical(classes='setting'):
                yield Checkbox('Agent browser · Betterwright', value=selected['browser'], id='browser')
                yield Static('Adds a browser with a live viewer for you to see and interact with its pages.', classes='setting-help checkbox-help')
            with Vertical(classes='setting browser-vault-setting', id='browser-vault-setting'):
                yield Checkbox('Protect saved browser vault files', value=selected['protect_browser_credentials'], id='protect-browser-credentials')
                yield Static('Blocks native file tools from the saved vault; browser account selection and login still work.', classes='setting-help checkbox-help')
                yield Static('', id='browser-vault-shell-help', classes='setting-help checkbox-help')
                yield Static('Not isolation from malicious editor or Pi extensions with the same user permissions. Active browser sessions remain accessible to browser automation.', classes='setting-help checkbox-help')
            with Vertical(classes='setting'):
                yield Checkbox('Question forms', value=selected['question_forms'], id='question-forms')
                yield Static('Let Pi and FrostPi ask questions with choices and free-text answers.', classes='setting-help checkbox-help')
            with Vertical(classes='setting'):
                yield Checkbox('Goals', value=selected['goals'], id='goals')
                yield Static('Use /goal to save an objective and keep working toward it across turns.',
                             classes='setting-help checkbox-help')
            with Vertical(classes='setting'):
                yield Checkbox('Scheduled prompts', value=selected['scheduling'], id='scheduling')
                yield Static('Run scheduled prompts while this chat is open.', classes='setting-help checkbox-help')

    def compose_skills(self, category, value, selected):
        skills = SkillSelection(value)
        with Collapsible(title=category.label + ' · ' + skills.summary(), collapsed=True, id=category.section):
            for skill in (() if self.native else PROFILE_SKILLS.get(self.profile_name, [])):
                yield Checkbox(SKILLS[skill][0], value=skill not in selected['disabled_skills'], id='skill-' + skill)
            yield skills

    def compose_advisors(self, category, value, selected):
        if not self.editing and not self.defaults_mode:
            with Collapsible(title=category.label, collapsed=True, id=category.section):
                providers = value.get('advisors', {}).get('providers', [])
                yield AdvisorChoices(providers, 'new-advisor-')
                yield Static(f'Copy a sign-in from this computer, or sign in after creation in {ADVISORS_PATH}. '
                             'Sign-ins stay in a separate container.', id='new-advisor-help')

    def compose_resources(self, category, value, selected):
        if self.setting == 'resources':
            section = Vertical(id=category.section, classes='spaced-form-fields resource-fields')
        else:
            section = Collapsible(title=category.label, collapsed=True, id=category.section, classes='spaced-form-fields')
        with section:
            with Vertical(classes='setting'):
                yield Label('Container engine')
                yield Select([('Automatic — prefer rootless Podman', 'auto'), ('Podman', 'podman'), ('Docker', 'docker')],
                             value=value.get('engine', 'auto'), allow_blank=False, id='engine', disabled=self.editing)
                if self.editing:
                    yield Static('Create a separate sandbox to change the engine.' if self.native else
                                 'Create a separate sandbox to change its engine or seed directory.', classes='setting-help')
            with Vertical(classes='setting'):
                yield Label(image_sources.LABEL)
                initial = image_sources.mode(value) if self.editing else value.get('image_source', image_sources.DEFAULT)
                with Horizontal(classes='path-row'):
                    yield Select(image_sources.CHOICES, value=initial, allow_blank=False, id='image-source')
                    yield Button('More information…', id='image-source-information')
            with Vertical(classes='setting'):
                yield Label('Memory limit · empty means no explicit limit')
                yield Input(value.get('memory') or '', id='memory')
            with Vertical(classes='setting'):
                yield Label('Credentials environment file · optional path')
                yield PathField('env-file', value.get('env_file') or '', title='Choose environment file', mode='file')
            if not self.native:
                with Vertical(classes='setting'):
                    yield Label('Seed Pi agent directory · optional, fresh configuration by default')
                    yield PathField('pi-config', value.get('pi_config') or '', title='Choose Pi agent folder', disabled=self.editing)

    def compose_system_prompt(self, category, value, selected):
        if not self.editing and not self.defaults_mode:
            current = value.get('system_prompt')
            title = category.label + ' · ' + (current['name'] if current else NO_SYSTEM_PROMPT_ADD_ON)
            with Collapsible(title=title, collapsed=True, id=category.section):
                agent = PRIMARY_AGENTS[self.primary]
                yield PromptChoice(current, agent=agent,
                                   help_text=f'The add-on applies when you open {agent}.' if self.native else '')

    @on(Select.Changed, '#system-prompt-choice')
    def system_prompt_changed(self):
        value = self.query_one(PromptChoice).selected()
        self.query_one('#system-prompt-section', Collapsible).title = (
            SYSTEM_PROMPT_LABEL + ' · ' + (value['name'] if value else NO_SYSTEM_PROMPT_ADD_ON))

    def on_mount(self):
        if self.setting == 'resources':
            self.resource_fields_changed()
        if self.native:
            self.update_curlie_download_notice()
            if self.setting == 'primary-approvals':
                self.query_one('#open-editor').display = False
            if self.setting:
                category = self.categories[self.setting]
                section = self.query_one('#' + category.section)
                if isinstance(section, Collapsible):
                    section.collapsed = False
                    section.add_class('focused-setting')
                self.query_one('#' + category.focus).focus()
            elif can_copy_primary_sign_in(self.primary):
                self.primary_auth_changed()
                field = self.query_one('#primary-auth', Select)
                self.set_focus(field, scroll_visible=False)
                self.call_after_refresh(field.scroll_visible, animate=False)
            if self.setting in (None, 'primary-browser'):
                self.primary_browser_changed()
            if self.setting in (None, 'primary-approvals'):
                self.native_approval_changed()
            if self.setting in (None, 'network-settings'):
                self.network_changed()
            return
        self.approval_summary()
        self.network_changed()
        self.local_models_changed()
        self.browser_changed()
        self.tools_summary()
        if self.initial_section:
            section = self.query_one('#' + self.initial_section)
            if isinstance(section, Collapsible):
                section.collapsed = False
            if self.setting:
                for child in self.query_one('.form', VerticalScroll).children:
                    if isinstance(child, (Collapsible, CommunicationsForm)) or child.id == 'document-search-section':
                        child.display = child is section or child in section.ancestors
                section.add_class('focused-setting')
                focus = self.query_one('#' + EDIT_FORMS[self.setting].focus)
                if isinstance(focus, Collapsible):
                    focus = focus.query_one('CollapsibleTitle')
                focus.focus()
                return
            self.call_after_refresh(section.scroll_visible, top=True, animate=False)
        elif self.has_class('settings-review'):
            models = self.query_one('#' + CATEGORIES['models'].section, Collapsible)
            models.query_one('CollapsibleTitle').focus(scroll_visible=False)

    @on(Checkbox.Changed, '#local-models')
    def local_models_changed(self):
        disabled = not self.query_one('#local-models', Checkbox).value
        self.query_one('#model-ports', Input).disabled = disabled
        self.query_one('#local-model-details').display = not disabled
        provider = self.query_one('#local-provider', Collapsible)
        provider.title = local_models_title(not disabled)
        provider.set_class(disabled, '-local-off')
        self.local_ports_changed()
        self.models_summary()

    @on(Input.Changed, '#model-ports')
    def local_ports_changed(self):
        self.query_one('#model-ports', Input).remove_class('-invalid')
        self.query_one('#model-ports-error', Feedback).update('')

    def checked_local_ports(self):
        field = self.query_one('#model-ports', Input)
        try:
            if not field.value.strip():
                raise ValueError('Enter at least one server port, or turn off local models.')
            return sandbox.local_model_ports(field.value.strip())
        except ValueError as error:
            self.query_one('#model-ports-error', Feedback).update(str(error))
            field.add_class('-invalid')
            self.query_one('#hosted-section', Collapsible).collapsed = False
            self.query_one('#local-provider', Collapsible).collapsed = False
            field.focus()
            self.call_after_refresh(field.parent.scroll_visible, animate=False)
            raise

    @on(HostedProviders.Changed)
    def models_summary(self):
        local = self.query_one('#local-models', Checkbox).value
        count = int(local)
        count += sum(self.query_one('#source-' + name, Checkbox).value for name in self.provider_forms)
        count += sum(len({**group.saved, **group.changes}) for group in self.query(HostedProviders))
        self.query_one('#hosted-section', Collapsible).title = (
            CATEGORIES['models'].label + ' · ' + model_sources_summary(local, count))

    @on(SkillSelection.Changed)
    def skills_summary(self):
        self.query_one('#skill-library-section', Collapsible).title = CATEGORIES['skills'].label + ' · ' + self.query_one(SkillSelection).summary()

    @on(Checkbox.Changed, '#subagents')
    @on(Checkbox.Changed, '#question-forms')
    @on(Checkbox.Changed, '#scheduling')
    @on(Checkbox.Changed, '#browser')
    @on(Checkbox.Changed, '#goals')
    @on(Select.Changed, '#compaction-recall')
    def tools_summary(self):
        selected = {key: self.query_one('#' + key.replace('_', '-'), Checkbox).value
                    for key in ('question_forms', 'scheduling', 'browser', 'goals')}
        selected['compaction_recall'] = self.query_one('#compaction-recall', Select).value
        subagents = self.query_one('#subagents', Checkbox).value
        self.query_one('#skills-section', Collapsible).title = (
            CATEGORIES['agent-tools'].label + ' · ' + pi_features_summary(subagents, selected))

    @on(Checkbox.Changed, '#browser')
    def browser_changed(self):
        enabled = self.query_one('#browser', Checkbox).value
        self.query_one('#protect-browser-credentials', Checkbox).disabled = not enabled
        self.query_one('#browser-vault-setting').set_class(not enabled, '-browser-disabled')
        inner = self.query_one('#inner-sandbox', Checkbox).value
        mode = self.query_one('#approval', Select).value
        text = ('Inner Bash hides private home. Reviewed outer-shell commands still follow credential policy.' if inner else
                'Bash runs in the outer container under approval rules; there is no inner filesystem barrier.')
        if not inner and mode == 'off':
            text = 'Inner sandbox and reviews are off: Bash can read vault files. This checkbox does not protect shell access.'
        self.query_one('#browser-vault-shell-help', Static).update(text)

    @on(Checkbox.Changed)
    def provider_selected(self, event):
        for name, info in self.provider_forms.items():
            if event.checkbox.id == 'source-' + name:
                self.query_one('#source-details-' + name).display = event.value
                for key in info.get('settings', {}):
                    for field in self.query('#source-' + name + '-' + key):
                        field.disabled = not event.value
                        if not event.value:
                            field.remove_class('-invalid-credential')
                            for message in field.parent.query(Feedback):
                                message.update('')
                self.models_summary()
                break

    @on(Input.Changed)
    def provider_credential_changed(self, event):
        if event.input.parent.has_class('provider-setting'):
            event.input.remove_class('-invalid-credential')
            for message in event.input.parent.query(Feedback):
                message.update('')

    def approval_policy_changed(self):
        original = options(self.draft.get('options'))
        return (original['approval'] != self.query_one('#approval', Select).value
                or original['inner_sandbox'] != self.query_one('#inner-sandbox', Checkbox).value
                or original['protect_credentials'] != self.query_one('#protect-credentials', Checkbox).value)

    def resource_replacement_needed(self):
        memory = self.query_one('#memory', Input).value.strip()
        env_file = self.query_one('#env-file', Input).value.strip()
        return ((None if memory == 'none' else memory or None) != (self.draft.get('memory') or None)
                or (env_file or None) != (self.draft.get('env_file') or None))

    @on(Input.Changed, '#memory')
    @on(Input.Changed, '#env-file')
    def resource_fields_changed(self):
        if self.setting == 'resources':
            replace = self.resource_replacement_needed()
            apply = self.query_one('#apply', Button)
            apply.label = 'Apply and recreate' if replace else 'Apply'
            apply.refresh(layout=True)
            self.query_one('#open-editor').display = replace
            self.query_one('.settings-actions').set_class(not replace, '-without-open-client')

    def approval_summary(self):
        mode = self.query_one('#approval', Select).value
        if fields := next(iter(self.query(ReviewerFields)), None):
            automatic = mode in ('automatic', 'assisted')
            was_visible = fields.display
            fields.display = automatic
            if automatic and not was_visible and not self.query_one('#agent-section', Collapsible).collapsed:
                fields.load()
        if self.setting == 'approval-settings':
            policy_changed = self.approval_policy_changed()
            self.query_one('#apply', Button).label = 'Apply and recreate' if policy_changed else 'Apply'
            self.query_one('#open-editor').display = policy_changed
        _, inline_help, _ = APPROVAL_MODES[mode]
        inner = self.query_one('#inner-sandbox', Checkbox).value
        category = CATEGORIES['approval-settings'].label
        self.query_one('#agent-section', Collapsible).title = (
            category + ' · ' + approval_mode_summary(mode, inner))
        self.query_one('#selected-approval-help', Static).update(inline_help)

    @on(Select.Changed, '#approval')
    @on(Checkbox.Changed, '#inner-sandbox')
    @on(Checkbox.Changed, '#protect-credentials')
    def approval_changed(self):
        self.approval_summary()
        self.browser_changed()

    @on(Select.Changed, '#network')
    @on(Checkbox.Changed, '#codex-web-search')
    @on(Checkbox.Changed, '#claude-web-search')
    @on(Checkbox.Changed, '#agy-web-search')
    @on(Checkbox.Changed, '#web-enabled')
    def network_changed(self):
        mode = self.query_one('#network', Select).value
        self.update_curlie_download_notice()
        if self.native:
            category = self.categories['network-settings']
            summary = (network_search_summary(mode, self.query_one('#' + self.primary + '-web-search', Checkbox).value)
                       if self.primary in WEB_SEARCH_OPTIONS else network_mode_label(mode))
            self.query_one('#network-section', Collapsible).title = category.label + ' · ' + summary
            self.query_one('#network-rules-setting').display = mode == 'allowlist'
            return
        search = self.query_one('#web-enabled', Checkbox).value
        category = CATEGORIES['network-settings']
        self.query_one('#' + category.section, Collapsible).title = (
            category.label + ' · ' + network_search_summary(mode, search))
        self.query_one('#network-section', Collapsible).title = NETWORK_LABEL + ' · ' + network_mode_label(mode)
        self.query_one('#network-rules-setting').display = mode == 'allowlist'

    def update_curlie_download_notice(self):
        notice = self.query_one('#curlie-download-notice', Static)
        websites = next(iter(self.query(WebsiteGroups)), None)
        notice.display = bool(websites and not self.defaults_mode and self.query_one('#network', Select).value == 'allowlist'
                              and websites.needs_curlie_download())
        notice.parent.set_class(notice.display, '-with-curlie-notice')
        if notice.display:
            action = 'apply these settings' if self.editing else 'create this sandbox'
            notice.update(f'The Curlie list ({curlie_catalog.DOWNLOAD_SIZE}) will be downloaded when you {action}.')

    def network_rules(self, text):
        try:
            return sandbox.parse_rules(text)
        except ValueError as error:
            if self.setting in (None, 'network-settings'):
                if not self.native:
                    self.query_one('#network-search-section', Collapsible).collapsed = False
                self.query_one('#network-section', Collapsible).collapsed = False
                feedback = self.query_one('#network-error', Feedback)
                field = self.query_one('#allowlist', TextArea)
                field.add_class('-invalid')
                field.focus()
                detail = str(error).replace('Allowlist line ', 'Line ', 1)
                feedback.update('Network · ' + detail if self.native and not self.editing else detail)
                self.call_after_refresh(feedback.scroll_visible, animate=False)
            raise

    def apply_network_rules(self):
        name = self.draft['name']
        text = self.query_one('#allowlist', TextArea).text
        self.network_rules(text)
        websites = self.query_one(WebsiteGroups)
        groups, curlie_list = websites.selected(), websites.curlie_revision()
        service = self.app.service
        search_change = self.native_web_search_change()
        def apply(log):
            with tempfile.TemporaryDirectory(prefix='sandbox-network-') as tmp:
                path = Path(tmp) / 'allowlist.txt'
                path.write_text(text)
                args = ['network', name, '--allowlist', str(path), '--website-groups', *groups]
                if curlie_list:
                    args += ['--curlie-list', curlie_list]
                service.run(args, log)
                if search_change:
                    service.update_settings(name, search_change, log)
        mode = None if self.native else editor_settings.load(self.app.state_root / name)['mode']
        reopen = self.query_one('#open-editor', Checkbox).value
        self.app.pop_screen()
        message = settings_effects.NETWORK
        if self.native:
            effects = ['Rules apply to new connections.', 'Retry interrupted connections.']
            if search_change:
                effects.append(f'Reopen {PRIMARY_AGENTS[self.primary]} CLI or reload the VS Code window to apply web-search changes.')
            message = settings_effects.bullets(effects)
        app = self.app
        app.start_task(name, 'Update network rules · ' + name, apply,
                       success_message=message, followup_label='Reopen editor',
                       on_success=(lambda _: app.launch_client(name, mode)) if reopen else None,
                       cli_recipe=cli_recipes.network(name, text, groups, app.state_root, revision=curlie_list,
                                                      search_change=search_change, reopen=reopen))

    def native_web_search_change(self):
        if not (key := WEB_SEARCH_OPTIONS.get(self.primary)):
            return {}
        enabled = self.query_one('#' + self.primary + '-web-search', Checkbox).value
        return ({key: enabled} if enabled != options(self.draft.get('options'))[key] else {})

    def network_rules_changed(self):
        rules = self.network_rules(self.query_one('#allowlist', TextArea).text)
        websites = self.query_one(WebsiteGroups)
        assigned = curlie_catalog.assigned(self.draft.get('website_groups', {}))
        revision = websites.curlie_revision()
        return (set(rules) != set(sandbox.parse_rules(self.draft.get('allowlist', '')))
                or set(websites.selected()) != set(options(self.draft.get('options'))['website_groups'])
                or revision is not None and revision != (assigned or {}).get('revision'))

    def collect_primary(self):
        draft = dict(self.draft)
        selected = options(draft.get('options'))
        draft['open_editor'] = self.query_one('#open-editor', Checkbox).value
        draft['skill_repo'] = draft['skill_path'] = ''
        if self.setting in (None, 'network-settings'):
            if key := WEB_SEARCH_OPTIONS.get(self.primary):
                selected[key] = self.query_one('#' + self.primary + '-web-search', Checkbox).value
            draft['network'] = self.query_one('#network', Select).value
            draft['allowlist'] = self.query_one('#allowlist', TextArea).text if draft['network'] == 'allowlist' else ''
            self.network_rules(draft['allowlist'])
            websites = self.query_one(WebsiteGroups)
            selected['website_groups'] = websites.selected()
            draft['_curlie_list'] = websites.curlie_revision()
        if self.setting in (None, 'resources'):
            draft['memory'] = memory_limit(self.query_one('#memory', Input).value.strip())
            draft['engine'] = self.query_one('#engine', Select).value
            draft['image_source'] = self.query_one('#image-source', Select).value
            draft['env_file'] = self.query_one('#env-file', Input).value.strip() or None
            sandbox.validate_env(draft['env_file'])
        if self.setting in (None, 'skills'):
            draft.update(self.query_one(SkillSelection).collect())
        if self.setting in (None, 'primary-approvals'):
            selected['native_approval'] = self.query_one('#native-approval', Select).value
        if self.setting in (None, 'primary-browser'):
            selected['browser'] = self.query_one('#primary-browser', Checkbox).value
        if not self.editing:
            fields = next(iter(self.query(EditorFields)), None)
            draft['editor'] = fields.collect() if fields else {'mode': 'pi'}
            if draft['editor']['mode'] == 'browser' and editor_settings.default_mode(draft['engine']) != 'browser':
                raise ValueError('Browser editor requires rootless Podman on Linux, WSL2 or macOS. Choose the CLI client.')
            draft['primary_auth'] = (self.query_one('#primary-auth', Select).value
                                     if can_copy_primary_sign_in(self.primary) else 'login')
            draft.update(self.query_one(FolderFields).collect(private_paths=[draft.get('env_file')]))
            draft['system_prompt'] = self.query_one(PromptChoice).selected()
            self.validate_ssh_key(draft)
        draft['options'] = options(selected)
        return draft, {}

    def collect(self):
        if self.native:
            return self.collect_primary()
        draft = dict(self.draft)
        def inp(id): return self.query_one('#' + id, Input).value.strip()
        def check(id): return self.query_one('#' + id, Checkbox).value
        draft['local_model_ports'] = inp('model-ports') if check('local-models') else None
        draft['model_providers'] = {'local':{}} if check('local-models') else {}
        draft.pop('_provider_keys', None)
        draft.pop('_provider_credential_choices', None)
        draft.pop('_local_model_keys', None)
        provider_rules=[]
        if not self.editing and not self.defaults_mode:
            draft['_hosted_changes'] = dict(self.query_one(HostedProviders).changes)
            draft['_provider_keys'] = {}
            draft['_provider_credential_choices'] = {}
        for name,info in self.provider_forms.items():
            if check('source-'+name):
                settings={key:inp('source-'+name+'-'+key) for key,spec in info.get('settings',{}).items() if spec['type']!='secret'}
                draft['model_providers'][name]=settings
                provider_rules+=model_providers.required_rules([{'manifest':info,'settings':settings}])
                if not self.editing and not self.defaults_mode:
                    for key,spec in info.get('settings',{}).items():
                        if spec['type'] != 'secret':
                            continue
                        if provider_credentials.scope(name, info.get('inference')):
                            if choice := self.provider_key_choices.get(name):
                                draft['_provider_credential_choices'][name] = choice
                        elif credential := inp('source-'+name+'-'+key):
                            if any(c in credential for c in '\r\n\0'):
                                field = self.query_one('#source-'+name+'-'+key, Input)
                                field.add_class('-invalid-credential')
                                field.parent.query_one(Feedback).update('Enter a single-line credential.')
                                field.focus()
                                field.parent.scroll_visible(animate=False)
                                raise ValueError(spec.get('label', key) + ': enter a single-line credential.')
                            draft['_provider_keys'][name] = credential
        if check('local-models'):
            ports = self.checked_local_ports()
            if not self.editing and not self.defaults_mode and self.local_key_choices:
                draft['_local_model_keys'] = {port: self.local_key_choices[port] for port in ports
                                              if port in self.local_key_choices}
        if not self.editing and not self.defaults_mode:
            draft.update(self.query_one(FolderFields).collect(private_paths=[inp('pi-config')]))
            draft['system_prompt'] = self.query_one(PromptChoice).selected()
        selected = options(draft.get('options'))
        if self.defaults_mode:
            from .repository_ui import GitIdentityFields
            selected['git_identity'] = self.query_one(GitIdentityFields).collect()
        key = {}
        if self.setting in (None, 'network-settings'):
            web_search = self.query_one(WebAccessForm)
            if self.setting is None or web_search.has_changes(selected['web_access']):
                selected['web_access'], key = web_search.collect()
        if self.setting in (None, 'communications'):
            selected['communications'], draft['_communication_keys'] = self.query_one(CommunicationsForm).collect()
        provider = self.document_search.get('knowledge_embedding_provider')
        if self.setting in (None, 'document-search', 'model-sources') and provider and provider != CUSTOM:
            if (provider not in draft['model_providers'] and provider not in draft.get('hosted_connections', {})
                    and provider not in draft.get('_hosted_changes', {})):
                raise ValueError('The embedding provider is no longer configured. Choose a provider in Document search.')
        websites = self.query_one(WebsiteGroups)
        selected['website_groups'] = websites.selected()
        draft['_curlie_list'] = websites.curlie_revision()
        if fields := next(iter(self.query(ReviewerFields)), None):
            selected['approval_model'] = fields.selected
        selected.update(approval=self.query_one('#approval', Select).value, inner_sandbox=check('inner-sandbox'),
                        protect_credentials=check('protect-credentials'),
                        protect_browser_credentials=check('protect-browser-credentials'),
                        goals=check('goals'), scheduling=check('scheduling'), question_forms=check('question-forms'), browser=check('browser'),
                        compaction_recall=self.query_one('#compaction-recall', Select).value,
                        **self.document_search)
        selected['disabled_skills'] = [s for s in SKILLS if
            (s in PROFILE_SKILLS.get(self.profile_name, []) and not check('skill-' + s))
            or (s not in PROFILE_SKILLS.get(self.profile_name, []) and s in selected['disabled_skills'])]
        if not self.editing:
            draft['editor'] = self.query_one(EditorFields).collect()
            if draft['editor']['mode'] == 'browser' and editor_settings.default_mode(self.query_one('#engine', Select).value) != 'browser':
                raise ValueError('Browser editor requires rootless Podman on Linux, WSL2 or macOS. Choose Pi CLI.')
        draft.update(network=self.query_one('#network', Select).value, memory=memory_limit(inp('memory')),
                     subagents=check('subagents'), options=options(selected), engine=self.query_one('#engine', Select).value,
                     image_source=self.query_one('#image-source', Select).value,
                     env_file=inp('env-file') or None, pi_config=inp('pi-config') or None,
                     open_editor=check('open-editor'),
                     skill_repo='', skill_path='')
        draft.update(self.query_one(SkillSelection).collect())
        if not self.editing and not self.defaults_mode:
            from . import sandbox_advisors
            choices = self.query_one(AdvisorChoices)
            draft['advisors'] = sandbox_advisors.validate({**draft.get('advisors', {}), 'providers': choices.selected()})
            draft['_advisor_imports'] = choices.imports()
        draft['allowlist'] = self.query_one('#allowlist', TextArea).text if draft['network'] == 'allowlist' else ''
        # Local model ports are controlled by their labelled field; other rules stay intact.
        rules = [r for r in self.network_rules(draft['allowlist']) if r[0] != 'local-model.internal']
        if draft['local_model_ports']:
            rules += [('local-model.internal', p) for p in sandbox.local_model_ports(draft['local_model_ports'])]
            if draft['network'] == 'unrestricted':
                if self.setting == 'network-settings':
                    raise ValueError(f'Turn off local models in {LOCAL_MODELS_PATH} before choosing Unrestricted.')
                if self.setting == 'model-sources':
                    raise ValueError(f'Choose Restricted in {NETWORK_PATH} before enabling local models.')
            if self.editing or self.defaults_mode:
                actual = sandbox.choose_engine(draft['engine'], quiet=True)
                sandbox.configure_local_gateway({'engine':actual, 'network':draft['network']}, draft['local_model_ports'], rules)
        if draft['network'] == 'allowlist':
            rules += [r for r in sandbox.parse_rules('\n'.join(provider_rules)) if r not in rules]
        draft['allowlist'] = '\n'.join(f'{host}:{port}' for host,port in rules)
        if not self.editing and not self.defaults_mode:
            self.validate_ssh_key(draft)
        sandbox.validate_env(draft['env_file'])
        return draft, key

    def configuration_unchanged(self, draft):
        original = self.draft
        if draft.get('_curlie_list') or image_sources.mode(draft) != image_sources.mode(original):
            return False
        for field in ('network', 'memory', 'subagents', 'local_model_ports', 'env_file'):
            if (draft.get(field) or None) != (original.get(field) or None):
                return False
        if options(draft.get('options')) != options(original.get('options')):
            return False
        providers = original.get('model_providers', {'local': {}})
        if isinstance(providers, list):
            providers = {row['name']: row.get('settings', {}) for row in providers}
        return (draft.get('model_providers') == providers and not draft.get('skill_repo')
                and not draft.get('remove_skills') and not draft.get('library_skills') and
                set(sandbox.parse_rules(draft['allowlist'])) == set(sandbox.parse_rules(original.get('allowlist', ''))))

    def quick_request(self):
        """Resolve live changes once for both Apply and the optional CLI preview."""
        previous = options(self.draft.get('options'))
        if self.setting == 'resources' and not self.resource_replacement_needed():
            source = self.query_one('#image-source', Select).value
            return ('settings', {'image_source': source}) if source != image_sources.mode(self.draft) else ('none', {})
        if self.setting == 'primary-approvals':
            mode = self.query_one('#native-approval', Select).value
            return ('settings', {'native_approval': mode}) if mode != previous['native_approval'] else ('none', {})
        if self.setting == 'approval-settings':
            fields = self.query_one(ReviewerFields)
            if (previous['approval'] in ('manual', 'off') and fields.display and fields.selected != 'current'
                    and not any(m['provider'] + '/' + m['id'] == fields.selected for m in fields.models)):
                raise ValueError('Choose an available model for approvals or use the current model.')
            if not self.approval_policy_changed():
                return ('reviewer', fields.selected) if fields.selected != previous['approval_model'] else ('none', {})
        if self.setting == 'network-settings':
            mode = self.query_one('#network', Select).value
            if mode == self.draft['network'] and (self.native or not self.query_one(WebAccessForm).has_changes(previous['web_access'])):
                if mode == 'allowlist' and (self.primary not in WEB_SEARCH_OPTIONS or self.network_rules_changed()):
                    text = self.query_one('#allowlist', TextArea).text
                    self.network_rules(text)
                    websites = self.query_one(WebsiteGroups)
                    return 'network', dict(text=text, groups=websites.selected(), revision=websites.curlie_revision(),
                                           search_change=self.native_web_search_change(),
                                           reopen=self.query_one('#open-editor', Checkbox).value)
                change = self.native_web_search_change()
                return ('web-search', change) if change else ('none', {})
        return None

    def quick_recipe(self, kind, value):
        name, state = self.draft['name'], self.app.state_root
        if kind == 'none':
            return cli_recipes.Recipe('No changes to apply', (), str(state))
        if kind == 'network':
            return cli_recipes.network(name, value['text'], value['groups'], state,
                                      **{key: value[key] for key in ('revision', 'search_change', 'reopen')})
        if kind == 'reviewer':
            return cli_recipes.Recipe('Save approval model',
                (cli_recipes.Step(('select-approval-model', name, '--model', value)),), str(state))
        return cli_recipes.update(name, value, state)

    def command_recipe(self):
        if self.editing and (quick := self.quick_request()) is not None:
            return self.quick_recipe(*quick)
        draft, keys = self.collect()
        if not self.editing:
            return cli_recipes.creation(draft, self.app.state_root, web_keys=keys)
        if draft.get('_communication_keys') and not keys and self.configuration_unchanged(draft):
            return cli_recipes.Recipe('Save Slack credentials',
                (cli_recipes.key_changes(draft['name'], draft['_communication_keys'], communication=True),), str(self.app.state_root))
        return cli_recipes.full_update(draft, keys, self.app.state_root)

    def apply_quick_request(self, kind, value):
        if kind == 'none':
            self.notify('No changes to apply.')
            return
        if kind == 'network':
            self.apply_network_rules()
            return
        app, name, agent = self.app, self.draft['name'], self.primary
        recipe = self.quick_recipe(kind, value)
        if kind == 'web-search':
            async def failed(message):
                form = self
                if app.screen is not self:
                    form = SettingsForm(self.draft, editing=True, setting='network-settings')
                    await app.push_screen(form)
                    form.query_one('#' + agent + '-web-search', Checkbox).value = value[WEB_SEARCH_OPTIONS[agent]]
                form.query_one('#form-error', Feedback).update(message)
            app.start_task(name, 'Save web search setting · ' + name, lambda log:
                app.service.update_settings(name, value, log), success_message=(
                f'Web search saved. Reopen {PRIMARY_AGENTS[agent]} CLI or reload the VS Code window to apply it.'),
                on_failure=failed, failure_label='Edit web search settings', cli_recipe=recipe)
            return
        self.dismiss()
        if kind == 'reviewer':
            app.start_task(name, 'Save approval model · ' + name, lambda log:
                app.service.save_approval_model(name, value, log),
                success_message='Approval model saved. Reopen Pi to use it.', cli_recipe=recipe)
        elif 'native_approval' in value:
            app.start_task(name, 'Save approval mode · ' + name, lambda log:
                app.service.update_settings(name, value, log),
                success_message='Approval mode saved. Restart the sandbox to use it.', cli_recipe=recipe)
        else:
            app.start_task(name, 'Save image setup · ' + name, lambda log:
                app.service.update_settings(name, value, log),
                success_message='Image setup saved for future builds.', cli_recipe=recipe)

    @on(Button.Pressed)
    async def pressed(self, event):
        event.stop()
        action = event.button.id
        try:
            if action == 'settings-cli':
                if self.app.screen is self and not self.query_one('#apply', Button).disabled:
                    self.app.push_screen(CLICommands(self.command_recipe()))
                return
            if action == 'image-source-information':
                self.app.push_screen(FactoryUpdateDetails(image_sources.LABEL,
                    'With prebuilt images selected, Factory downloads matching images when needed. '
                    'If no matching image has been published, Factory builds that part locally. '
                    'If a download or verification fails, you can retry or select Build images locally.\n\n'
                    'When you build locally, your container engine can reuse cached layers. '
                    'It may download base images and software packages.'
                    + ('\n\nSaving this choice affects future builds. The sandbox keeps its current images.'
                       if self.editing else '')))
                return
            if action == 'profile-services-information':
                self.app.push_screen(ProfileServicesHelp())
                return
            if action == 'review-profile-guide':
                self.app.push_screen(ProfileGuide(self.profile.get('description') or self.profile_name, self.guide))
                return
            if action == 'local-server-keys':
                self.app.push_screen(LocalKeysDialog(self.checked_local_ports(), draft=self.local_key_choices),
                                     self.choose_local_keys)
                return
            if (event.button.name or '').startswith('provider-key:'):
                name = event.button.name.split(':', 1)[1]
                info = self.provider_forms[name]
                self.app.push_screen(CredentialDialog(info.get('description', name),
                    provider_credentials.scope(name, info.get('inference')), choice=self.provider_key_choices.get(name)),
                    lambda choice: self.choose_provider_key(name, event.button, choice))
                return
            if action == 'back':
                self.action_back()
            elif action == 'explain-approval':
                self.app.push_screen(ApprovalHelp(editing=self.editing))
            elif action == 'apply':
                if event.button.disabled or self.app.screen is not self:
                    return
                for feedback in self.query('#form-error, #network-error, #web-config-error'):
                    feedback.update('')
                quick = self.quick_request()
                if quick is not None:
                    self.apply_quick_request(*quick)
                    return
                draft, key = self.collect()
                draft['_search_keys_changed'] = bool(key)
                if self.defaults_mode:
                    saved = {k: draft[k] for k in DEFAULT_SETTINGS if k in draft}
                    saved.update(env_file=draft['env_file'] or '', pi_config=draft['pi_config'] or '')
                    saved['editor'] = editor_settings.copy_choice(saved['editor'])
                    saved['profile'] = self.query_one('#default-profile', Select).value
                    saved['workspace_parent'] = self.query_one('#workspace-parent', Input).value.strip()
                    search, keys = await self.query_one(WebAccessForm).defaults_selection()
                    self.app.defaults = save_defaults_with_search(saved, search, keys)
                    self.app.pop_screen()
                    self.app.check_dependencies()
                    self.notify('Defaults saved for future sandboxes.')
                elif self.editing and draft.get('_communication_keys') and not key and self.configuration_unchanged(draft):
                    app, service = self.app, self.app.service
                    app.pop_screen()
                    app.start_task(draft['name'], 'Save Slack credentials', lambda log:
                        service.save_communication_keys(draft['name'], draft['_communication_keys'], log),
                        success_message='Slack credentials are ready for the next tool call.',
                        cli_recipe=cli_recipes.Recipe('Save Slack credentials',
                            (cli_recipes.key_changes(draft['name'], draft['_communication_keys'], communication=True),), str(app.state_root)))
                elif self.editing:
                    mode = None if self.native else editor_settings.load(self.app.state_root / draft['name'])['mode']
                    self.app.push_screen(ApplySettingsReview(self.draft, draft, mode),
                        lambda yes: self.submit(draft, key) if yes else None)
                else:
                    self.review_creation(draft, key)
        except (OSError, ValueError) as error:
            if any(message.display for message in self.query('SkillSelection Feedback')):
                self.query_one('#skill-library-section', Collapsible).collapsed = False
            for message in self.query('#skill-selection-error'):
                if message.display:
                    self.query_one('#skill-library-section', Collapsible).collapsed = False
                    message.scroll_visible(animate=False)
            if not any(message.display for message in self.query(
                    '.provider-setting Feedback, #model-ports-error, SkillSelection Feedback, #network-error, '
                    '#web-config-error')):
                feedback = self.query_one('#form-error', Static)
                feedback.update(str(error))
                feedback.scroll_visible(animate=False)

    def choose_provider_key(self, name, button, choice):
        if choice is None:
            return
        self.provider_key_choices[name] = choice
        button.label = 'Change key…'
        binding = provider_credentials.scope(name, self.provider_forms[name].get('inference'))
        self.query_one('#source-key-label-'+name, Static).update(choice_key_caption(binding, choice))

    def choose_local_keys(self, choices):
        if choices is not None:
            self.local_key_choices = choices

    def review_creation(self, draft, key):
        if self.native or any(draft.get(field) for field in ('model_providers', '_hosted_changes', 'pi_config')):
            self.begin_creation_check(draft, key)
            return

        def confirmed(continue_without_model):
            if not self.is_attached or self.app.screen is not self:
                return
            if continue_without_model:
                self.begin_creation_check(draft, key)
                return
            section = self.query_one('#' + CATEGORIES['models'].section, Collapsible)
            section.collapsed = False
            section.query_one('#local-provider > CollapsibleTitle').focus(scroll_visible=False)
            self.call_after_refresh(section.scroll_visible, top=True, animate=False)

        self.app.push_screen(ConfirmWithoutModel(), confirmed)

    def begin_creation_check(self, draft, key, approved=None):
        button = self.query_one('#apply', Button)
        button.disabled = True
        feedback = self.query_one('#form-error', Feedback)
        feedback.add_class('checking-creation')
        feedback.update('Checking the sandbox name…' if approved is None else 'Deleting the listed resources…')
        feedback.scroll_visible(animate=False)
        self.query_one('VerticalScroll.form').disabled = True
        self.check_creation(draft, key, approved)

    @work(exit_on_error=False)
    async def check_creation(self, draft, key, approved):
        app = self.app
        error = None
        try:
            conflicts = await asyncio.to_thread(app.service.check_creation, draft, approved)
        except (OSError, ValueError) as cause:
            error = str(cause)
        finally:
            if self.is_attached:
                self.query_one('#apply', Button).disabled = False
                self.query_one('VerticalScroll.form').disabled = False
        if not self.is_attached or app.screen is not self:
            return
        feedback = self.query_one('#form-error', Feedback)
        feedback.remove_class('checking-creation')
        feedback.update(error or '')
        if error is not None:
            self.call_after_refresh(feedback.scroll_visible, animate=False)
            return
        if not (conflicts.containers or conflicts.volumes or conflicts.networks):
            self.submit(draft, key)
            return

        def remove(yes):
            if yes and app.screen is self:
                self.begin_creation_check(draft, key, conflicts)

        def confirm_networks(yes):
            if not yes or app.screen is not self:
                return
            if conflicts.networks:
                app.push_screen(ConfirmCreationConflicts(
                    draft['name'], 'networks', conflicts.networks,
                    'The listed private networks will be deleted. Other networks stay in place.'), remove)
            else:
                remove(True)

        def confirm_volumes(yes):
            if not yes or app.screen is not self:
                return
            if conflicts.volumes:
                warning = [
                    'Deleting volumes permanently erases their contents.',
                    'They can hold saved chats, sign-in data and editor settings.',
                    'Files in your host project folder stay in place.',
                ]
                if any(name.endswith('-agent-workspace') for name, _ in conflicts.volumes):
                    warning.append('The volume ending in -agent-workspace holds runtime sockets.')
                backup = 'Cancel to back up these volumes or choose another name.'
                if conflicts.engine == 'podman':
                    backup += '\nReplace VOLUME with the listed name.\n\npodman volume export VOLUME --output VOLUME.tar'
                else:
                    backup += '\nSee the Docker guide on backing up volumes.'
                app.push_screen(ConfirmCreationConflicts(
                    draft['name'], 'volumes', conflicts.volumes, settings_effects.bullets(warning) + '\n\n' + backup), confirm_networks)
            else:
                confirm_networks(True)

        if conflicts.containers:
            app.push_screen(ConfirmCreationConflicts(
                draft['name'], 'containers', conflicts.containers, settings_effects.bullets([
                    'Running work in these containers will stop.',
                    'Files stored only inside these containers will be deleted.',
                    'Files in your host project folder stay in place.',
                    'Deleting volumes requires separate confirmation.',
                ]) + '\n\nCancel and go back to choose another name.'), confirm_volumes)
        else:
            confirm_volumes(True)

    def submit(self, draft, key):
        app = self.app
        choices = list(draft.get('_provider_credential_choices', {}))
        provider_label = self.provider_forms.get(choices[0], {}).get('description') if len(choices) == 1 else None
        if not self.editing:
            self.dismiss()
            app.call_later(self.on_create or app.start_creation,
                           CreationRequest(draft, key, provider_label, bool(self.guide)))
            return
        self.dismiss()
        app.call_later(app.update_configuration, draft, key, provider_label)

    def check_action(self, action, parameters):
        if action == 'back':
            return not self.editing
        if action == 'cancel_edit':
            return self.editing
        return True

    def action_cancel_edit(self):
        self.action_back()

    def action_back(self):
        if self.has_class('settings-review'):
            selections = {key: self.draft[key] for key in ('ssh', '_ssh_key', '_ssh_key_file', '_ssh_key_duration',
                                                           'aws_access', 'connector_ids', 'connector_operations',
                                                           '_connector_routes', '_connector_instructions') if key in self.draft}
            selections['git_identity'] = self.draft.get('options', {}).get('git_identity')
            self.dismiss(selections)
        else:
            self.app.pop_screen()


def banner_title():
    from .cli import PRODUCT_NAME
    return pyfiglet.figlet_format(PRODUCT_NAME, font='big', width=120).rstrip()


def banner(version, compact=False):
    from .cli import PRODUCT_NAME
    title = PRODUCT_NAME if compact else banner_title()
    return f'[bold #f2b544]{title}[/]  [dim]v{version}[/]' if compact else f'[bold #f2b544]{title}[/]\n[dim]AI coding sandboxes · v{version}[/]'


class Cockpit(App):
    TITLE = 'AI Sandbox Factory'
    BINDINGS = [Binding('f2', 'new', 'New sandbox', priority=True),
                Binding('n', 'new', 'New', show=False), Binding('r', 'refresh_inventory', 'Refresh'),
                Binding('a', 'sandbox_actions', 'Actions'), Binding('shift+f10', 'sandbox_actions', show=False),
                Binding('j', 'jobs', 'Jobs'),
                Binding('slash', 'search', show=False), Binding('d', 'defaults', 'Defaults'), Binding('q', 'quit', 'Quit')]
    CSS = '''
    Screen { background: #171a1d; }
    Header { background: #f2b544; color: #171a1d; }
    Footer { background: #34383d; }
    .title { color: #f2b544; text-style: bold; margin: 0 0 1 0; }
    Label { margin: 0; height: auto; max-width: 100%; text-style: bold; }
    .form { width: 100%; padding: 1 3 0 3; }
    ReviewProfile > .form { padding: 1 4 0 3; }
    .focused-settings-title { margin: 0; padding: 1 3 0 3; }
    .form > Static { height: auto; margin-bottom: 1; }
    .form > Label { margin-bottom: 0; }
    .form > .title, .form > .group-title { margin-bottom: 1; }
    #container-list { width: 1fr; margin: 1 3 0 3; padding: 0; scrollbar-size-vertical: 1; }
    #container-list > .setting { margin: 0 1 1 0; }
    #container-list:focus { outline: none; background: $surface; scrollbar-color: $primary; }
    .package-pane > .package-list, #library-list { padding: 0 1 0 0; }
    #library-list > .package-row:last-child, #skill-candidates > .setting:last-child { margin-bottom: 0; }
    PackagesScreen Button:focus, ImportSkills Button:focus { text-style: bold underline; }
    .package-list, SkillLibrary, #library-list, #skill-candidates { height: auto; }
    #packages-page, #package-kind, #package-kind ContentSwitcher { height: 1fr; }
    #package-kind TabPane { height: 1fr; padding: 0; }
    #package-kind Tabs { border-bottom: solid #454a51; }
    #package-kind Underline { display: none; }
    #package-kind Tab.-active { color: #f2b544; background: #493b24; text-style: bold; }
    #package-kind Tabs:focus Tab.-active { color: #17191b; background: #f2b544; text-style: bold underline; }
    Screen:nocolor #package-kind Tab.-active { color: black; background: #d6d6d6; text-style: bold; }
    Screen:nocolor #package-kind Tabs:focus Tab.-active { color: black; background: white; text-style: bold underline; }
    #package-install { min-width: 22; }
    Screen:nocolor PackagesScreen .title, Screen:nocolor InstallPackage .title,
    Screen:nocolor ImportSkills .title, Screen:nocolor ProfileDetails #confirm-box > Label {
        color: white; text-style: bold;
    }
    .package-pane { height: 1fr; padding: 1 1 0 0; }
    #system-prompts .package-pane { padding: 1 0 0 0; }
    PromptLibrary, PromptChoice, #system-prompt-preview { height: auto; }
    PromptChoice TextArea, #prompt-text { height: 7; padding: 0 1; }
    #system-prompt-section { margin: 1 0; }
    .settings-review #system-prompt-section { margin: 0 0 1 0; }
    #system-prompt-preview { margin: 1 0 0 0; }
    #system-prompt-preview > TextArea { margin: 0; }
    #edit-prompt-copy { margin: 1 0 0 0; }
    #prompt-fields { height: auto; max-height: 56vh; }
    #prompt-fields > .setting:last-of-type { margin-bottom: 0; }
    EditPrompt #confirm-box, .titled-confirm #confirm-box { width: 80; }
    Confirm #yes.-error, ExternalConnectorDialog #connector-confirm.-error { background: $error; color: white; }
    Confirm Button:focus { text-style: bold underline; }
    Confirm.with-notes .confirm-notes { height: auto; max-height: 55vh; margin: 1 0 0 0; }
    Confirm.with-notes .confirm-message { margin: 0 0 1 0; }
    Confirm.with-notes Button:focus, Confirm.with-notes #cancel:focus { text-style: bold; background: #d4e0e7; color: #171a1d; }
    Confirm #cancel:focus, ExternalConnectorDialog #connector-cancel:focus,
    .settings-review CollapsibleTitle:focus {
        background: #d4e0e7; color: #171a1d; text-style: bold underline;
    }
    Confirm:nocolor #yes.-error, ExternalConnectorDialog:nocolor #connector-confirm.-error { background: #454a51; color: white; }
    ConnectorSetup .connector-agent-heading { margin: 1 0 0 0; text-style: bold; }
    .compact-terminal PromptChoice TextArea, .compact-terminal #prompt-text { height: 5; }
    EditPrompt Feedback, #sandbox-prompt-error { color: #ef9a91; }
    EditPrompt TextArea.prompt-invalid:focus { border: tall $error; }
    #prompt-update-notice { color: $warning; }
    Screen:nocolor EditPrompt Feedback, Screen:nocolor #sandbox-prompt-error { color: white; text-style: bold reverse; }
    ProfileDetails #confirm-box { height: auto; max-height: 90%; }
    #profile-details-body { height: auto; max-height: 60vh; padding: 0 1 0 0; }
    .compact-terminal #profile-details-body { max-height: 52vh; }
    ProfileGuide Markdown { padding: 0; }
    ProfileGuide .dialog-actions { margin: 1 1 0 0; }
    .compact-terminal ProfileGuide #profile-details-body { max-height: 56vh; }
    ProfileGuide #profile-details-body { padding: 0 2 0 0; }
    #skill-discover { margin: 0 0 0 0; }
    #skill-import-error, #skill-selection-error, #library-error { color: #f1f3f5; text-style: bold; }
    SkillSelection Feedback.error { color: #f1f3f5; text-style: bold; }
    #skill-selection-error { margin: 1 0 0 0; }
    .package-row { height: auto; padding: 1 2; margin-bottom: 1; background: #24292f; }
    .package-title { text-style: bold; }
    .package-row > .package-actions { margin: 1 0 0 0; }
    .package-actions Button { margin: 0 2 0 0; }
    .package-actions Button:last-child { margin: 0; }
    #package-error { color: #ef9a91; text-style: bold; height: auto; }
    Screen:nocolor #package-error { color: white; text-style: bold reverse; }
    .setting, .default-profile-setting, .resource-fields { height: auto; }
    .setting { margin-bottom: 1; }
    .bounded-form > .form, .bounded-form > .wizard-actions,
    .native-primary-form > .form, .native-primary-form > .wizard-actions,
    .settings-review > .form, .settings-review > .wizard-actions { max-width: 100; }
    .bounded-form > .profile-page-feedback { width: 100%; max-width: 100; height: auto; max-height: 25vh; padding: 1 3 0 3; overflow-y: auto; }
    .native-primary-form EditorFields > .setting { margin: 0 0 1 0; }
    #approval-heading { height: 1; }
    #approval-heading Label { width: auto; margin: 0 2 0 0; }
    .compact-terminal ApprovalHelp #approval-help { max-height: 52vh; }
    ApprovalHelp .approval-help-note { margin: 0 1 1 0; }
    .browser-vault-setting { margin: 0 0 1 2; }
    .browser-vault-setting.-browser-disabled { opacity: 50%; }
    GitIdentityFields #git-identity-fields > .setting:last-child { margin: 0 0 0 0; }
    .spaced-form-fields GitIdentityFields .setting > Input:last-child { margin: 1 0 0 0; }
    #git-identity-error { color: #f1f3f5; text-style: bold; }
    .provider-setting { margin: 0 0 1 0; }
    .provider-details { height: auto; padding: 1 0 0 2; }
    .provider-details > *:last-child { margin-bottom: 0; }
    .group-title.model-group-title { margin-bottom: 0; }
    .available-providers { height: auto; margin-bottom: 1; }
    #model-ports-error, #network-error, #curlie-error, #new-error, .key-dialog Feedback { color: #ff9184; text-style: bold; }
    #allowlist { border: solid #454a51; }
    #allowlist:focus { border: solid $primary; }
    #allowlist.-invalid, #allowlist.-invalid:focus { border: solid $error; }
    .compact-terminal .native-primary-form #allowlist { height: 6; }
    Screen:nocolor #model-ports-error, Screen:nocolor #network-error,
    Screen:nocolor #curlie-error, Screen:nocolor #new-error,
    Screen:nocolor .key-dialog Feedback { color: white; text-style: bold reverse; }
    ModelsScreen #configuration { margin: 1 0 0 0; }
    ModelsScreen #legacy-provider-key { margin: 0 0 1 2; }
    .setting > Checkbox, .setting > TextArea { margin-bottom: 0; }
    .setting > Button, .setting > .buttons { margin: 0; }
    AdvisorChoices .setting > .copy-login { margin: 1 0 0 0; }
    .setting-help { height: auto; color: #b8bdc3; }
    .checkbox-help { padding-left: 4; }
    .section-intro { height: auto; margin-bottom: 1; }
    Input {
        height: 1; padding: 0 1 0 0; margin: 1 0;
        border: none; border-left: thick #929ba6; background: #414850; color: #f1f3f5;
    }
    Input:focus { border: none; border-left: thick $primary; background: #505965; }
    Input.-invalid, Input.-invalid-credential, Input.prompt-invalid:focus { border-left: thick $error; }
    Screen:nocolor Input:focus { background: white; color: black; border-left: thick black; }
    .setting > Input:last-child, .setting > Select:last-child, .setting > .path-row:last-child { margin: 1 0 0 0; }
    Select { height: 1; margin: 1 0; }
    Select > SelectCurrent, Select:focus > SelectCurrent { height: 1; border: none; padding: 0 1; }
    Select > SelectCurrent > #label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Select:focus > SelectCurrent { background: #fff1d2; }
    Select:focus > SelectCurrent > #label, Select:focus > SelectCurrent > .arrow { color: #17191b; text-style: bold; }
    Screen:nocolor Select:focus > SelectCurrent { background: white; }
    TextArea { height: 9; margin-bottom: 1; }
    CLICommands #cli-text { height: 1fr; margin: 0; }
    .group-title { text-style: bold underline; margin-bottom: 1; }
    .section-heading { text-style: bold underline; }
    .command-text { height: auto; padding: 0 1; background: #24292f; text-wrap: wrap; }
    .command-label { margin: 1 0 0 0; }
    Checkbox { height: 1; border: none; padding: 0; margin: 0 0 1 0; }
    Checkbox > .toggle--button { color: #f1f3f5; background: transparent; text-style: bold; }
    Checkbox.-on > .toggle--button { color: #f2b544; background: transparent; }
    Screen:nocolor Checkbox.-on > .toggle--button { color: white; text-style: bold; }
    Checkbox:focus > .toggle--label { color: #171a1d; background: #f2b544; text-style: bold underline; }
    Checkbox:focus > .toggle--button { color: #171a1d; background: #f2b544; }
    Screen:nocolor Checkbox.-on:focus > .toggle--button { color: black; }
    Button.-primary { background: #f2b544; color: #171a1d; }
    Button:focus { text-style: bold; background: #f2b544; color: #171a1d; }
    #settings-cli:focus, #editor-cli:focus, #editor-back:focus, SettingsForm #back:focus,
    JobDetails Button:focus, CLICommands Button:focus {
        background: #d4e0e7; color: #171a1d; text-style: bold underline;
    }
    .profile-edit-flow Button:focus { background: #d4e0e7; color: #171a1d; text-style: bold; }
    #hosted-section Button:focus, ModelsScreen Button:focus,
    .key-dialog Button:focus, LocalKeysDialog Button:focus { text-style: bold underline; }
    Collapsible { margin-bottom: 1; padding: 0; border: none; }
    CollapsibleTitle { text-style: bold; }
    CollapsibleTitle:focus { text-style: bold underline; }
    Collapsible > Contents { padding: 1 3; }
    #local-provider > Contents { padding: 1 3 0 3; }
    #local-provider > CollapsibleTitle, #live-local-provider > CollapsibleTitle,
    #provider-controls > CollapsibleTitle { padding: 0 1 0 0; }
    #skills-section > CollapsibleTitle { width: 1fr; text-wrap: wrap; }
    #local-provider.-local-off #local-enable-setting { margin: 0; }
    #network-search-section #network-section,
    #network-search-section #web-section { margin: 0; }
    Collapsible.focused-setting > CollapsibleTitle { display: none; }
    Collapsible.focused-setting > Contents { padding: 0; }
    Collapsible > Contents > *:last-child { margin-bottom: 0; }
    .buttons { height: 1; min-height: 1; margin: 1 0; }
    .wizard-actions { dock: bottom; height: 3; padding: 1 3; margin-bottom: 1; width: 100%; }
    .settings-actions { height: 5; padding: 1 3; }
    .settings-actions.-without-open-client { height: 3; }
    .settings-actions.-with-curlie-notice { height: auto; }
    #curlie-download-notice { margin: 0 0 1 0; }
    .action-buttons { height: 1; }
    #settings-cli { margin: 0 0 0 1; }
    .wizard-actions Button { min-width: 12; padding: 0 2; }
    .wizard-actions Button.-primary { dock: right; margin-right: 0; }
    .wizard-actions .data-actions { dock: right; width: auto; height: 1; }
    .wizard-actions .data-actions Button { dock: none; margin: 0 0 0 1; }
    .job-actions #job-cancel { dock: right; margin-right: 0; }
    .dialog-actions { align-horizontal: right; height: 1; min-height: 1; margin: 1 0 0 0; }
    .dialog-actions Button { min-width: 12; padding: 0 2; }
    CreateFromSandbox #copy-continue { min-width: 16; }
    CreateFromSandbox #copy-source { margin: 1 0 0 0; }
    CreateFromSandbox #copy-sandbox-fields > .last-copy-setting { margin: 0; }
    CreateFromSandbox #copy-sandbox-dialog > .dialog-actions { padding: 0 1 0 0; }
    CreateFromSandbox:nocolor .title, CopySandboxHelp:nocolor #confirm-box > .title { color: white; text-style: bold; }
    CopySandboxHelp #copy-help-body { height: 1fr; padding: 0 1 0 0; scrollbar-size-vertical: 1; }
    CopySandboxHelp #copy-help-body > .setting:last-child { margin: 0; }
    .action-spacer { width: 1fr; height: 1; }
    .profile-guide-row { height: auto; }
    NewSandbox > .form, NewSandbox > .wizard-actions { max-width: 80; }
    .compact-terminal NewSandbox > #new-title { padding: 0 3; }
    NewSandbox #primary-agent { max-width: 32; }
    SettingsForm #primary-auth, SettingsForm #native-approval, SettingsForm #engine { max-width: 40; }
    SettingsForm #image-source { width: 26; margin: 0; }
    SettingsForm #memory { max-width: 12; }
    NewSandbox #profile, NewSandbox #name, NewSandbox #profile-setting .profile-guide-row { max-width: 52; }
    NewSandbox #profile { margin: 1 0 0 0; }
    NewSandbox #sandbox-name-setting { margin: 0; }
    NewSandbox #new-error { margin: 1 0; }
    .profile-guide-row Button { padding: 0 2; margin: 0 0 0 1; }
    .review-profile-row { margin: 0 0 1 0; }
    .review-profile-row Static { width: 1fr; height: auto; }
    #review-project { height: auto; text-wrap: wrap; }
    .review-profile-summary .review-profile-row { margin: 0; }
    Button { height: 1; max-height: 1; border: none; margin-right: 1; min-width: 8; padding: 0 1; background: #4a5058; }
    Button:hover { background: #5a616a; }
    .dialog-actions Button:last-child { margin-right: 0; }
    .key-dialog .key-dialog-fields { max-height: 65vh; }
    .compact-terminal .key-dialog { height: 96%; max-height: 96%; }
    .compact-terminal ProviderDialog .model-dialog { height: 100%; max-height: 100%; }
    .compact-terminal ProviderDialog .dialog-actions { margin: 0; }
    .compact-terminal .key-dialog .key-dialog-fields { height: 1fr; max-height: 100%; }
    .compact-terminal .key-dialog.-saved-key-selected,
    .compact-terminal .key-dialog.-saved-key-selected .key-dialog-fields { height: auto; }
    .compact-terminal CredentialDialog #credential-dialog { height: auto; }
    .compact-terminal CredentialDialog #credential-dialog-fields { height: auto; max-height: 55vh; }
    .key-dialog #remember-key { margin: 1 0 0 0; }
    .key-dialog #credential-remember { margin: 1 0; }
    SSHDestinationFields #ssh-hosts { height: 5; }
    #web-enable-setting { margin: 0; }
    #web-fields { margin: 1 0 0 0; }
    #web-fields > .buttons { margin: 0 0 1 0; }
    #slack-controls { width: auto; }
    #slack-controls Checkbox { width: auto; margin-bottom: 0; }
    #editor-show-password { margin-top: 1; }
    #editor-tls-fields { margin-top: 1; }
    #local-model-details { height: auto; margin: 0 0 0 2; }
    #model-ports { max-width: 48; }
    Collapsible > Contents > *:last-child > .setting:last-child,
    Collapsible > Contents > *:last-child > Collapsible:last-child { margin-bottom: 0; }
    #editor-section EditorFields > .setting { margin: 0 0 1 0; }
    #editor-section EditorFields > .last-editor-field { margin: 0; }
    #slack-controls Button { margin-right: 0; }
    .search-provider-row Button { margin-right: 0; }
    .model-fields, #slack-fields, #copy-sandbox-fields, #search-provider-fields { padding-right: 1; }
    ProviderDialog .model-fields { padding: 0; }
    .compact-terminal .plugin-model-dialog #model-results { max-height: 3; border: none; padding: 0 1; }
    OptionList > .option-list--option { padding: 0 1; }
    #custom-profile-fields, #network-rules-fields, #network-rules-setting, #provider-controls { height: auto; }
    #home > .buttons { margin: 0 0 1 0; }
    #home > .buttons Label { width: 1fr; }
    #home > .buttons Button:last-child { margin-right: 0; }
    .path-row { height: 1; margin: 1 0; }
    NewSandbox .form .path-row { padding: 0 1 0 0; }
    .path-row Input { width: 1fr; margin: 0; }
    .path-row Button { height: 1; padding: 0 2; margin: 0 0 0 1; }
    .chat-success { color: #91caa6; }
    ContinueChat #confirm-box { width: 100; max-width: 95%; height: auto; max-height: 90%; }
    ContinueChat #copy-fields { height: auto; max-height: 55vh; padding: 0 1 0 0; }
    ContinueChat Button:disabled { background: #44484e; color: #a1a7af; opacity: 55%; }
    ContinueChat Button:focus { background: #d4e0e7; color: #171a1d; text-style: bold underline; }
    ContinueChat Feedback.error { color: #ffb3a7; background: #452f30; text-style: bold; }
    ContinueChat:nocolor Feedback.error { color: black; background: white; text-style: bold; }
    .tall-terminal ChatTransfer #confirm-box { width: 114; }
    ChatTransfer .path-row { margin: 1 0 0 0; }
    ChatTransfer Input:focus { background: #4a5360; }
    ChatTransfer:nocolor Feedback.error { color: white; text-style: bold reverse; }
    DeleteSandbox #yes { background: #573638; color: #ffd1cd; text-style: bold; }
    ChatTransfer Button:focus, DeleteSandbox Button:focus { background: #d4e0e7; color: #171a1d; text-style: bold underline; }
    DeleteSandbox #yes:focus { background: #ffd1cd; color: #442529; text-style: bold underline; }
    DeleteSandbox:nocolor #delete-export-chats.-primary { background: #4a5058; color: #f1f3f5; }
    DeleteSandbox:nocolor #delete-export-chats.-primary:focus { background: #d4e0e7; color: #171a1d; }
    .error { color: #b75b52; height: auto; }
    Screen:nocolor .error { color: white; text-style: bold; }
    SettingsForm #form-error { color: #ffb3a7; text-style: bold; }
    SettingsForm:nocolor #form-error { color: white; text-style: bold reverse; }
    SettingsForm #form-error.checking-creation { color: #f1f3f5; text-style: none; }
    Screen:nocolor ConnectorFields .error { color: white; text-style: bold reverse; }
    .model-dialog > Feedback, FolderEditor Feedback { margin-top: 1; }
    .compact-terminal FolderEditor #folder-fields { max-height: 52vh; }
    #folder-fields > .setting { margin: 0; }
    #folder-preview { margin: 0 0 1 0; }
    FolderFields #folder-add { margin: 1 0; }
    FolderEditor Feedback.error { color: #ff9184; }
    #folder-fields > Checkbox:last-child { margin-bottom: 0; }
    .hosted-row > .buttons, #managed-provider-key > .buttons { margin: 1 0 0 0; }
    #iam-fields { margin-bottom: 1; }
    #title-banner { height: auto; padding: 1 3 0 3; }
    #startup-notice { height: auto; margin: 0 3 0 3; padding: 0 1; border: solid #f2b544; border-title-color: #f2b544; }
    .compact-terminal #title-banner { display: none; }
    #startup-dependencies { height: auto; margin: 1 3 0 3; padding: 0 1; color: #ef9a91; border-left: solid $error; }
    Screen:nocolor #startup-dependencies { text-style: bold; }
    #application-update {
        display: none; height: auto; margin: 1 3 0 3; padding: 0 1;
        background: #30291d; border-left: solid $warning;
    }
    #document-search-section { height: auto; margin: 0 0 1 0; }
    #skills-section.focused-setting > Contents { padding: 0 2 0 0; }
    #document-search-section.focused-setting { padding: 0; background: transparent; }
    #document-search-summary { width: auto; height: auto; margin: 0 0 1 0; }
    #document-search-actions { height: auto; }
    #configure-document-search { width: auto; padding: 0 2; }
    #document-search-help { width: auto; margin: 0 0 0 2; padding: 0 2; }
    #document-search-fields { height: auto; max-height: 50vh; padding: 0 1 0 0; scrollbar-size-vertical: 1; scrollbar-gutter: auto; overflow: hidden scroll; }
    #document-search-fields .setting { margin: 0; }
    #document-search-fields Feedback.error { margin: 1 0 0 0; }
    #document-search-fields .setting > Select, #document-search-fields .setting > Input { margin: 1 0 0 0; }
    #knowledge-model-help { margin: 1 0 0 0; }
    #document-search-fields #embedding-provider-setting, #document-search-fields #embedding-url-setting { margin: 0 0 1 0; }
    #embedding-catalogue-status { height: auto; margin: 0 0 1 0; }
    DocumentSearchDialog .dialog-actions { margin: 1 2 0 0; }
    DocumentSearchHelp #confirm-box { height: auto; overflow: hidden; }
    #document-search-help-fields { width: 100%; height: auto; padding: 0 1 0 0; scrollbar-size-vertical: 1; scrollbar-gutter: auto; overflow: hidden scroll; }
    DocumentSearchHelp .dialog-actions { margin: 1 2 0 0; }
    DocumentSearchHelp .setting { height: auto; margin: 0 0 1 0; padding: 0 1 0 0; }
    DocumentSearchHelp .setting:last-child { margin: 0; }
    DocumentSearchHelp .setting > Label { text-style: bold; }
    #embedding-catalogue-message { height: auto; width: 1fr; }
    #retry-embedding-catalogue { width: auto; margin: 0 0 0 2; padding: 0 2; }
    #embedding-server-fields { height: auto; padding: 1 0 0 2; }
    #embedding-mode-setting { margin: 0; }
    .spaced-form-fields .setting > Select, .spaced-form-fields .setting > Input { margin: 1 0; }
    .spaced-form-fields .setting > .path-row { margin: 1 0; }
    .form .spaced-form-fields .setting > Select:last-child, .form .spaced-form-fields .setting > Input:last-child,
    .form .spaced-form-fields .setting > .path-row:last-child, .form.spaced-form-fields .setting > .path-row:last-child,
    .form.spaced-form-fields .setting > Select:last-child, .form.spaced-form-fields .setting > Input:last-child { margin: 1 0 0 0; }
    .form .spaced-form-fields Button, .form.spaced-form-fields Button,
    CommunicationsForm Button, SkillSelection Button { padding: 0 2; }
    DocumentSearchDialog #embedding-catalogue SelectOverlay { max-height: 6; offset: 0 -7; }
    .compact-terminal DocumentSearchDialog #confirm-box { height: auto; max-height: 90%; padding: 1 2; }
    DocumentSearchDialog Feedback.error { color: #ff9184; text-style: bold; }
    DocumentSearchDialog:nocolor Feedback.error { color: white; text-style: bold reverse; }
    #application-update-row { height: auto; }
    #application-update-message { width: auto; height: auto; text-style: bold; }
    #application-update-hint { display: none; width: auto; height: auto; margin: 0 0 0 2; }
    #application-update-run { width: auto; margin: 0 0 0 2; }
    #factory-update-details { height: auto; max-height: 55vh; padding: 0 2 0 0; }
    #application-update-help { width: auto; margin: 0 0 0 2; }
    Screen:nocolor #application-update { border-left: solid white; }
    #home { width: 100%; padding: 1 3; }
    .compact-terminal #home { padding: 1 3 0 3; }
    .compact-terminal.missing-dependencies #details { padding: 0 0 1 0; min-height: 3; }
    .toolbar-control { height: 1; margin: 0; }
    Input.toolbar-control, .toolbar-control > SelectCurrent, Button.toolbar-control {
        height: 1; padding: 0 2;
    }
    Input.toolbar-control:focus { background: #34383d; }
    Button.toolbar-control:focus, Select.toolbar-control:focus > SelectCurrent {
        background: #fff1d2; color: #17191b; text-style: bold;
    }
    Select.toolbar-control:focus > SelectCurrent > #label,
    Select.toolbar-control:focus > SelectCurrent > .arrow { color: #17191b; }
    #image-source-information:focus, ConnectorFields .connector-row Button:focus { color: #171a1d; background: #d4e0e7; text-style: bold underline; }
    Screen:nocolor #image-source-information:focus,
    Screen:nocolor ConnectorFields .connector-row Button:focus,
    CreateSSHDialog:nocolor .dialog-actions Button:focus { color: black; background: white; text-style: bold underline; }
    Screen:nocolor Button.toolbar-control:focus,
    Screen:nocolor Select.toolbar-control:focus > SelectCurrent { background: white; color: black; }
    #dashboard-filters { height: 3; margin: 0 0 1 0; }
    #dashboard-filters Label { margin: 0 0 1 0; }
    #dashboard-search Input { margin: 0; }
    #dashboard-search { width: 1fr; }
    #sandbox-sort { width: auto; margin: 0 0 0 2; }
    #sandbox-sort-controls { height: 1; width: auto; }
    #sort-field { width: 18; }
    #sort-direction { min-width: 16; margin: 0 0 0 2; }
    #sandboxes { height: 1fr; min-height: 4; }
    #sandboxes > .datatable--even-row, #sandboxes > .datatable--odd-row {
        background: $surface-darken-1 40%;
    }
    #sandboxes > .datatable--hover { background: $panel; }
    #sandboxes > .datatable--cursor { background: transparent; color: $foreground; text-style: bold; }
    #sandboxes:focus > .datatable--cursor { background: transparent; text-style: bold underline; }
    #details { height: auto; min-height: 3; padding: 1 0; text-wrap: nowrap; text-overflow: ellipsis; }
    #ssh-key-feedback { height: auto; }
    #ssh-duration-error, #ssh-key-feedback.error, #ssh-aliases-error, SSHDestinationFields Feedback, CreateSSHFields Feedback { color: #ef9a91; }
    #ssh-key-feedback.unlock-succeeded { color: #87c99b; text-style: bold; }
    Screen:nocolor #ssh-key-feedback { color: white; text-style: bold; }
    Screen:nocolor #ssh-key-feedback.error, Screen:nocolor #ssh-duration-error, Screen:nocolor #ssh-aliases-error,
    Screen:nocolor SSHDestinationFields Feedback, Screen:nocolor CreateSSHFields Feedback { color: white; text-style: bold reverse; }
    #home-error, #open-error { height: auto; margin: 0 0 1 0; color: #b75b52; }
    #operation-body { width: 100%; padding: 1 3 0 3; }
    #operation-title { height: auto; text-style: bold; margin-bottom: 1; }
    #operation-status { height: auto; color: #f2b544; text-style: bold; margin-bottom: 1; }
    #operation-next { height: auto; margin: 0 0 1 0; }
    #operation-status.complete { color: #87c99b; }
    #operation-status.failed { color: #ef9a91; }
    #operation-log { height: 1fr; min-height: 5; border: round #707780; border-title-color: #b8bdc3; padding: 0 1; overflow-y: auto; scrollbar-gutter: stable; }
    Confirm, AWSAccessDialog, AWSProfilePicker, AWSProfileTargets, DocumentSearchDialog, DocumentSearchHelp, FactoryUpdateDetails, ApprovalHelp, ProfileServicesHelp, CopySandboxHelp, FolderPicker, PickerErrors, MoreActions, SettingsMenu, PrimarySignIn, ProviderDialog, SandboxAlerts, ProfileDetails, ProfileGuide, EditPrompt,
    ChatTransfer, DeleteSandbox, InstallSystemPackages, JobsMenu { align: center middle; background: #000000 70%; }
    ConfirmCreationConflicts #confirm-box { width: 88; height: auto; max-height: 90%; overflow: hidden; }
    ConfirmCreationConflicts VerticalScroll { height: auto; max-height: 55vh; margin: 0 0 1 0; }
    #confirm-box { width: 75; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; background: #34383d; border: solid #f2b544; }
    AWSAccessDialog #confirm-box { height: auto; max-height: 90%; }
    .aws-profile-body { height: auto; max-height: 55vh; padding: 0; scrollbar-size-vertical: 1; }
    #confirm-box .aws-profile-actions { height: auto; margin: 1 0 0 0; }
    .aws-target { margin: 0 0 1 0; }
    #aws-fields { height: auto; max-height: 68vh; padding: 0; scrollbar-size-vertical: 1; }
    .compact-terminal AWSAccessDialog #confirm-box { height: 90%; }
    .compact-terminal #aws-fields { height: 1fr; max-height: 100%; }
    .compact-terminal AWSProfilePicker.has-assignment #confirm-box { height: 90%; }
    .compact-terminal AWSProfilePicker.has-assignment .aws-profile-body { height: 1fr; max-height: 100%; }
    .compact-terminal AWSProfilePicker.has-assignment AWSProfileChoices { margin: 0; }
    #aws-fields .setting > Input { margin: 1 0 0 0; }
    #aws-grants { height: 4; margin: 1 0; padding: 0 1; border: none; background: #414850; }
    #aws-grants:focus { border: none; border-left: thick $primary; }
    ApprovalHelp #confirm-box { width: 84; }
    .tall-terminal ApprovalHelp #confirm-box { max-height: 95%; }
    #confirm-box Static { height: auto; }
    PrimarySignIn .sign-in-actions { margin: 1 0 0 0; }
    PrimarySignIn .sign-in-actions > Button { width: 41; max-width: 100%; margin: 0 0 1 0; padding: 0 2; }
    PrimarySignIn #confirm-box, Confirm.sign-in-confirm #confirm-box { width: 61; }
    #confirm-box > Label { margin-bottom: 1; color: #f2b544; text-style: bold; }
    #confirm-box > OptionList { height: auto; max-height: 14; margin-bottom: 1; }
    InstallSystemPackages #confirm-box > Label { text-wrap: nowrap; text-overflow: ellipsis; }
    #system-packages-fields { height: auto; max-height: 55vh; }
    #system-packages-warning { color: #f2b544; }
    #system-packages-error { color: #ef9a91; }
    InstallSystemPackages .dialog-actions Button:focus { text-style: bold underline; }
    Screen:nocolor #system-packages-warning { color: white; text-style: bold; }
    Screen:nocolor #system-packages-error { color: white; text-style: bold reverse; }
    Screen:nocolor #system-package-names.-invalid { border-left: thick #b8bdc3; }
    MoreActions #confirm-box { max-height: 100%; }
    MoreActions #confirm-box > Label { text-wrap: nowrap; text-overflow: ellipsis; }
    MoreActions #more-actions { max-height: 55vh; }
    MoreActions #confirm-box .dialog-actions { margin: 0; }
    JobsMenu #confirm-box > OptionList { max-height: 55vh; }
    #job-log-error { height: auto; color: #ffb3a7; }
    SandboxAlerts #confirm-box { padding: 1 2; }
    #sandbox-alerts-list { height: auto; max-height: 56vh; margin: 0 0 1 0; }
    .sandbox-alert { height: auto; padding: 1 2; margin: 0 0 1 0; background: #25292e; }
    .sandbox-alert:last-child { margin: 0; }
    .sandbox-alert-heading { height: auto; }
    .sandbox-alert-title { width: 1fr; text-style: bold; }
    .sandbox-alert.-blocking .sandbox-alert-title { color: #ff9184; }
    .sandbox-alert-action { width: auto; margin: 0 0 0 2; padding: 0 2; }
    .sandbox-alert-action:focus { background: #f2b544; color: #17191b; text-style: bold; }
    Screen:nocolor .sandbox-alert-action:focus { background: white; color: black; }
    .sandbox-alert-description { height: auto; margin-top: 1; }
    SettingsMenu #confirm-box { width: 80; max-width: 98%; height: 95%; max-height: 26; }
    .compact-terminal SettingsMenu #confirm-box { height: 100%; max-height: 100%; }
    SettingsMenu #confirm-box > #settings-actions { height: 1fr; max-height: 100%; margin: 0; }
    SettingsMenu.native-settings-menu #confirm-box { height: auto; max-height: 95%; }
    SettingsMenu.native-settings-menu #confirm-box > #settings-actions { height: auto; max-height: 60vh; }
    InstallPackage #package-error { margin: 0 3; }
    #folder-box { width: 98; max-width: 95%; height: 94%; max-height: 30; padding: 1 2; background: #34383d; border: solid #f2b544; }
    #folder-box .path-row { height: 1; align: left middle; }
    .folder-input-label { height: 1; margin: 0 0 1 0; }
    #folder-filename-label { margin: 1 0; }
    #folder-box .path-row Button { height: 1; padding: 0 2; margin: 0 0 0 1; }
    #folder-errors { display: none; height: 1; padding: 0 2; }
    #picker-errors-box { width: 98; max-width: 95%; height: 80%; max-height: 26; padding: 1 2; background: #34383d; border: solid #f2b544; }
    #picker-errors-box VerticalScroll { height: 1fr; }
    #folder-filter-heading { height: 1; margin: 0 0 1 0; }
    #folder-filter-heading Label { width: 1fr; margin: 0; }
    #folder-inputs { height: 3; }
    #folder-location-group { width: 1fr; height: 3; margin: 0 2 0 0; }
    #folder-filter-group { width: 1fr; height: 3; }
    #folder-places Checkbox { margin: 0 0 0 1; }
    #folder-places { height: 1; margin: 1 0; }
    #folder-columns { height: 1; padding: 0 3 0 6; text-style: bold; background: #31353a; }
    #folder-name-column { width: 1fr; }
    #folder-size-column { width: 10; text-align: right; }
    #folder-modified-column { width: 20; text-align: right; }
    #folder-box DirectoryNavigation { height: 1fr; border: none; }
    #folder-browse-error { margin: 1 0 0 0; text-style: bold; }
    ChatTransfer:nocolor Feedback, FolderPicker:nocolor Feedback { color: #f1f3f5; text-style: bold; }
    #folders { height: 1fr; padding-right: 1; }
    '''

    def __init__(self, state_root, service=None, *, startup_notice=''):
        super().__init__()
        self.startup_notice = startup_notice
        self.register_theme(Theme(
            name='cockpit-gold', primary='#f2b544', secondary='#8a5a00', accent='#f2b544',
            foreground='#f1f3f5', background='#171a1d', surface='#25292e', panel='#34383d',
            success='#4f8a62', warning='#f2b544', error='#b75b52', dark=True,
            variables={'footer-key-foreground': '#f2b544', 'border': '#8a5a00',
                       'block-cursor-foreground': '#171a1d', 'block-cursor-background': '#f2b544',
                       'block-cursor-text-style': 'bold', 'input-selection-background': '#8a5a00'}))
        self.theme = 'cockpit-gold'
        self.state_root = Path(state_root)
        self.service = service or Service(self.state_root)
        self.records = []
        self.jobs = []
        self.job_completions = {}
        self._dashboard_jobs = ()
        self.upgrade_job = None
        self.sort_field = "started"
        self.sort_reverse = True
        self.defaults = load_defaults()
        self.version = updates.installed_version()
        self.available_version = None

    def compose(self):
        yield Header()
        yield Static(banner(self.version, compact=True), id='title-banner')
        notice = Static(self.startup_notice, id='startup-notice', markup=False)
        notice.border_title = 'Notice'
        notice.display = bool(self.startup_notice)
        yield notice
        yield Feedback('', id='startup-dependencies', markup=False)
        with Vertical(id='application-update'):
            with Horizontal(id='application-update-row'):
                yield Static('', id='application-update-message', markup=False)
                yield Button('Upgrade now', id='application-update-run', variant='primary', classes='toolbar-control')
                yield Button('Update instructions…', id='application-update-help', classes='toolbar-control')
                yield Static('', id='application-update-hint', markup=False)
        with Vertical(id='home'):
            with Horizontal(classes='buttons'):
                yield Label('Sandboxes', classes='title')
                yield Button('New sandbox', id='new')
                yield Button('Create from existing…', id='create-from', disabled=True)
                yield Button('Library…', id='packages')
            with Horizontal(id='dashboard-filters'):
                with Vertical(id='dashboard-search'):
                    yield Label('Search sandboxes')
                    yield Input(id='search', compact=True, classes='toolbar-control')
                with Vertical(id='sandbox-sort'):
                    yield Label('Sort by')
                    with Horizontal(id='sandbox-sort-controls'):
                        yield Select([(label, key) for key, label in [
                            ('started', 'Last started'), ('edited', 'Last edited'),
                            ('created', 'Created'), ('name', 'Name'), ('dev', 'Status'),
                        ]], value='started', allow_blank=False, id='sort-field', compact=True,
                            classes='toolbar-control')
                        yield Button('Newest first', id='sort-direction', classes='toolbar-control')
            yield SandboxTable(id='sandboxes', cursor_type='cell', cursor_background_priority='renderable',
                               cursor_foreground_priority='renderable', zebra_stripes=True,
                               header_height=SandboxTable.ROW_PITCH)
            yield Static('Choose a sandbox, or create your first one.', id='details', markup=False)
            yield Feedback('', id='home-error', markup=False)
            yield Feedback('', id='open-error', classes='error', markup=False)
            with Horizontal(classes='buttons'):
                yield Button('Settings', id='settings')
                yield Button('Actions…', id='more')
                yield Button('Jobs… (0)', id='jobs')
        yield Footer()

    def on_mount(self):
        self.screen.AUTO_FOCUS = '#new'
        self.query_one('#new', Button).focus()
        for key, label in [('name', 'Name'), ('open', ''), ('dev', 'Status'), ('alerts', 'Alerts'), ('started', 'Last started'), ('edited', 'Last edited'), ('created', 'Created'), ('sources', 'Model sources'), ('network', 'Network')]:
            self.query_one('#sandboxes', DataTable).add_column(label, key=key)
        self.action_refresh_inventory()
        self.set_interval(15, self.poll_ssh_status)
        self.set_interval(0.15, self.poll_jobs)
        self.check_update()
        self.resize_banner()

    def on_resize(self):
        if self.is_mounted:
            self.resize_banner()

    def resize_banner(self):
        self.set_class(self.size.height >= 32, 'tall-terminal')
        self.set_class(self.size.height < 32, 'compact-terminal')
        self.screen_stack[0].sub_title = f'v{self.version}' if self.size.height < 32 else ''
        title = self.query_one('#title-banner', Static)
        available = self.size.width - title.styles.padding.width
        compact = max(map(len, banner_title().splitlines())) > available
        title.update(banner(self.version, compact))
        self.render_records()

    @work(thread=True, exclusive=True, group='application-update', exit_on_error=False)
    def check_update(self):
        latest = updates.available_update(self.version)
        if latest:
            self.call_from_thread(self.show_update, latest)

    def show_update(self, latest):
        row = self.query_one_optional('#application-update')
        if row is None or not row.is_attached:
            return
        self.available_version = latest
        self.query_one('#application-update-message', Static).update(
            f'Update available · v{latest}')
        button = self.query_one('#application-update-run', Button)
        pipx_install = updates.pipx_install()
        button.display = pipx_install and bool(shutil.which('pipx'))
        button.disabled = not button.display
        hint = self.query_one('#application-update-hint', Static)
        hint.display = pipx_install and not button.display
        hint.update('Install pipx to upgrade.')
        self.query_one('#application-update-help', Button).display = not pipx_install
        row.display = True

    def confirm_update(self):
        if self.upgrade_pending():
            self.show_job(self.upgrade_job)
            return
        if self.jobs_active():
            self.notify('Wait for running jobs to finish before upgrading Factory.')
            return
        self.push_screen(Confirm(
            f'Upgrade AI Sandbox Factory to {self.available_version}?\n'
            'Runs pipx upgrade ai-sandbox-factory, then restarts this manager.\n'
            'Running sandboxes keep their installed software.', 'Upgrade', confirm_by_default=True), self.upgrade)

    def upgrade(self, confirmed):
        if confirmed and not self.jobs_active():
            self.upgrade_job = self.start_task(None, 'Upgrade Factory', updates.upgrade,
                working='Installing Factory update', on_success=lambda _: self.exit(result=updates.RELAUNCH),
                followup_label='Restart Factory', success_message='Restart Factory to use the new version.')

    def upgrade_pending(self):
        return self.upgrade_job is not None and self.upgrade_job.result != 'Failed'

    def poll_ssh_status(self):
        if (self.screen is self.screen_stack[0] and any('ssh_key_state' in record for record in self.records)
                and not any(worker.group == 'inventory' and worker.is_running for worker in self.workers)):
            self.refresh_inventory(only_if_changed=True)

    @work(thread=True, exclusive=True, group='inventory', exit_on_error=False)
    def refresh_inventory(self, only_if_changed=False):
        worker = get_current_worker()
        try:
            records = self.service.inventory()
        except Exception as error:
            self.call_from_thread(self.finish_inventory, worker, only_if_changed, error=safe_text(error))
        else:
            self.call_from_thread(self.finish_inventory, worker, only_if_changed, records=records)

    def finish_inventory(self, worker, only_if_changed, *, records=None, error=None):
        if worker.is_cancelled:
            return
        feedback = self.query_one_optional('#home-error', Feedback)
        if feedback is None or not feedback.is_attached:
            return
        if error is not None:
            feedback.update(error)
            return
        self.receive_inventory(records, only_if_changed)

    def action_refresh_inventory(self):
        self.check_dependencies()
        self.refresh_inventory()

    def check_dependencies(self):
        missing = []
        try:
            host_platform.find_engine(self.defaults['engine'])
        except ValueError as error:
            missing.append(str(error))
        if not shutil.which('git'):
            missing.append('Git was not found. Install Git to use Git repositories.')
        self.screen_stack[0].query_one('#startup-dependencies', Feedback).update(
            '\n'.join('• ' + message for message in missing))
        self.set_class(bool(missing), 'missing-dependencies')

    def receive_inventory(self, records, only_if_changed=False):
        self.query_one('#home-error', Static).update('')
        if only_if_changed and records == self.records:
            return
        self.records = records
        self.query_one('#create-from', Button).disabled = not records
        self.render_records()

    def render_records(self):
        table = self.query_one('#sandboxes', DataTable)
        selected = self.selected_name()
        table.clear()
        text = self.query_one('#search', Input).value.casefold()
        creating = {job.name: job.preview for job in self.jobs
                    if isinstance(job, CreationJob) and job.result is None and job.preview is not None}
        rows = [*creating.values(), *(record for record in self.sorted_records() if record['name'] not in creating)]
        for record in rows:
            agent = coding_agent(record['config'])
            if text not in (record['name'] + ' ' + record['profile'] + ' ' + PRIMARY_AGENTS[agent]
                            + ' ' + (record.get('workspace') or '')).casefold():
                continue
            config = record['config']
            providers = config.get('model_providers', [])
            source_names = list(providers) if isinstance(providers, dict) else [row['name'] for row in providers]
            sources = (['Local ' + config['local_model_ports']] if config.get('local_model_ports') else [])
            sources += [name for name in source_names if name != 'local']
            name = safe_text(record['name'])
            if self.size.width < 100 and len(name) > 32:
                name = name[:31] + '…'
            active = self.active_job(record['name'])
            action = active.view_label if active else '' if record.get('creation') else (
                CHOOSE_MODEL if self.pending_model_choice(record['name']) else 'Open')
            alert_count = len(sandbox_alerts(record))
            cells = (
                name,
                SandboxTable.action_cell(action, primary=True) if action else '',
                safe_text(record['dev'].capitalize()),
                SandboxTable.action_cell(f'Alerts ({alert_count})') if alert_count else '',
                *(activity.display(record.get('activity', {}).get(key)) for key in ('started', 'edited', 'created')),
                (safe_text(', '.join(sources)) or 'Not configured') if agent == 'pi' else PRIMARY_AGENTS[agent],
                'Restricted' if config.get('network', 'allowlist') == 'allowlist' else 'Unrestricted',
            )
            table.add_row(*(Text(cell, no_wrap=True) if isinstance(cell, str) and cell else cell for cell in cells),
                          height=SandboxTable.ROW_PITCH, key=record['name'])
        if selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected))
        self.update_details()

    def sorted_records(self):
        dated = self.sort_field in activity.FIELDS
        def value(record):
            return ((record.get('activity', {}).get(self.sort_field) or '') if dated
                    else str(record.get(self.sort_field, '')).casefold())
        known = [r for r in self.records if not dated or value(r)]
        unknown = [r for r in self.records if dated and not value(r)]
        # Stable name ordering for ties; missing dates always remain at the end.
        known.sort(key=lambda r: r['name'].casefold())
        return sorted(known, key=value, reverse=self.sort_reverse) + sorted(unknown, key=lambda r: r['name'].casefold())

    def update_sort(self):
        dated = self.sort_field in activity.FIELDS
        self.query_one('#sort-direction', Button).label = (
            ('Newest first' if self.sort_reverse else 'Oldest first') if dated
            else ('Z–A' if self.sort_reverse else 'A–Z'))
        self.render_records()

    @on(Select.Changed, '#sort-field')
    def sort_changed(self, event):
        self.sort_field = str(event.value)
        self.sort_reverse = self.sort_field in activity.FIELDS
        if self.is_mounted:
            self.update_sort()

    @on(Button.Pressed, '#sort-direction')
    def reverse_sort(self, event):
        event.stop()
        self.sort_reverse = not self.sort_reverse
        self.update_sort()

    @on(DataTable.HeaderSelected, '#sandboxes')
    def sort_header(self, event):
        key = str(event.column_key.value)
        if key not in (*activity.FIELDS, 'name', 'dev'):
            return
        if key == self.sort_field:
            self.sort_reverse = not self.sort_reverse
            self.update_sort()
        else:
            self.query_one('#sort-field', Select).value = key

    def selected_name(self):
        table = self.query_one('#sandboxes', DataTable)
        if table.row_count and table.cursor_row < table.row_count:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return None

    def selected(self):
        name = self.selected_name()
        job = self.active_job(name)
        if isinstance(job, CreationJob) and job.preview is not None:
            return job.preview
        return next((r for r in self.records if r['name'] == name), None)

    @on(Input.Changed, '#search')
    def filter_records(self):
        self.render_records()

    @on(DataTable.CellHighlighted, '#sandboxes')
    def highlighted(self, event):
        if event.data_table.is_attached:
            self.update_details()

    @on(DataTable.CellSelected, '#sandboxes')
    def select_sandbox_cell(self, event):
        action = event.cell_key.column_key.value
        if action in ('open', 'alerts') and event.value:
            name = event.cell_key.row_key.value
            if action == 'open' and (job := self.active_job(name)):
                self.show_job(job)
                return
            if action == 'open' and (job := self.pending_model_choice(name)):
                self.resume_job(job)
                return
            record = next((item for item in self.records if item['name'] == name), None)
            if record:
                self.perform(action, record=record)

    @on(SandboxTable.ActionsRequested)
    def right_click_actions(self, event):
        event.stop()
        if self.screen is self.screen_stack[0]:
            self.show_sandbox_actions(event.name)

    def action_sandbox_actions(self):
        self.perform('more')

    def show_sandbox_actions(self, name):
        from .browser_editor import runtime
        if isinstance(job := self.active_job(name), CreationJob):
            self.show_job(job)
            return
        record = next((row for row in self.records if row['name'] == name), None)
        if record is not None:
            self.push_screen(MoreActions(record, browser_running=bool(runtime(self.state_root / name))),
                             lambda choice: self.choose_sandbox_action(name, choice))

    def choose_sandbox_action(self, name, choice):
        if choice is None:
            return
        record = next((row for row in self.records if row['name'] == name), None)
        if record is None:
            self.notify(f'{name} is no longer in the sandbox list.', severity='warning')
            return
        if choice in ('start', 'stop') and (choice == 'stop') != (record['dev'] == 'running'):
            self.notify(f'{name} changed state. Choose an action again.')
            self.show_sandbox_actions(name)
            return
        self.perform(choice, record=record)

    def action_jobs(self):
        self.push_screen(JobsMenu(self.jobs), self.show_job)

    def show_job(self, job):
        if job is not None and not (isinstance(self.screen, JobDetails) and self.screen.job is job):
            self.push_screen(JobDetails(job))

    def jobs_active(self):
        return any(job.result is None for job in self.jobs)

    def update_jobs_badge(self):
        if not self.screen_stack:
            return
        button = self.screen_stack[0].query_one_optional('#jobs', Button)
        if button is None or not button.is_attached:
            return
        active = sum(job.result is None for job in self.jobs)
        label = f'Jobs… ({active})' + (' · To review' if any(job.followup for job in self.jobs) else '')
        if str(button.label) != label:
            button.label = label
            button.refresh(layout=True)

    def poll_jobs(self):
        if not self.screen_stack:
            return
        table = self.screen_stack[0].query_one_optional(SandboxTable)
        if table is None or not table.is_attached:
            return
        finished = [job for job in self.job_completions if job.result is not None]
        if finished:
            self.refresh_inventory()
        for job in finished:
            original_view, completed_callback = self.job_completions.pop(job)
            visible = self.screen is original_view
            if completed_callback:
                completed_callback(job)
            for view in self.screen_stack:
                if isinstance(view, JobDetails) and view.job is job and view.timer is not None and view.is_attached:
                    view.refresh_job()
            if visible and job.followup:
                self.resume_job(job)
            elif not visible:
                self.notify(f'{job.title} · {job.followup[0] if job.followup else job.status}',
                            severity='error' if job.result == 'Failed' else 'information')
        rows = tuple(job for job in self.jobs if job.result is None and job.targets)
        if rows != self._dashboard_jobs:
            self._dashboard_jobs = rows
            self.render_records()
        elif rows:
            for job in rows:
                label = job.view_label
                for name in job.targets:
                    if name in table.rows and str(table.get_cell(name, 'open')).strip() != label:
                        table.update_cell(name, 'open', SandboxTable.action_cell(label, primary=True),
                                          update_width=True)
        self.update_jobs_badge()

    def resume_job(self, job, *args):
        if followup := job.followup:
            label, resume = followup

            def set_followup(value):
                job.followup = value
                if isinstance(job, CreationJob) and label == CHOOSE_MODEL:
                    self.render_records()
                self.update_jobs_badge()

            if isinstance(self.screen, JobDetails) and self.screen.job is job:
                self.pop_screen()
            context = self.screen
            async def dispatch():
                if self.screen is not context or job.followup is not followup:
                    return
                set_followup(None)
                try:
                    result = resume(*args)
                    if isawaitable(result):
                        await result
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    job.notice = f'Could not continue with {label.lower()}: {error}'
                    job.write(job.notice)
                    set_followup(followup)
                    self.show_job(job)
            self.call_later(dispatch)

    def start_job(self, job, task, completed=None):
        if self.upgrade_pending():
            job.close()
            self.notify('Finish upgrading and restart Factory before starting another job.')
            self.show_job(self.upgrade_job)
            return None
        conflict = next((active for name in job.targets if (active := self.active_job(name))), None)
        if conflict:
            job.close()
            self.notify(f'{conflict.title} is already running. Open Jobs to see its progress.')
            self.show_job(conflict)
            return None
        self.jobs.append(job)
        view = JobDetails(job)
        self.job_completions[job] = (view, completed)
        self.push_screen(view)
        def run():
            job.run(task)
            if self.is_running:
                self.call_from_thread(self.poll_jobs)
        self.run_worker(run, thread=True, exit_on_error=False)
        self.poll_jobs()
        return job

    def start_task(self, name, title, task, *, working='Working', on_success=None,
                   followup_label='Continue', on_failure=None, failure_label='Try again', targets=None,
                   success_message='', cli_recipe=None):
        job = Job(name, title, working)
        job.cli_recipe = cli_recipe
        if targets is not None:
            job.targets = targets

        def completed(finished):
            if finished.result == 'Complete':
                finished.notice = success_message
            if finished.result == 'Complete' and on_success:
                async def resume():
                    result = on_success(finished.value)
                    if isawaitable(result):
                        await result
                    finished.value = None
                finished.followup = (followup_label, resume)
            elif finished.result == 'Failed' and on_failure:
                finished.followup = (failure_label, lambda: on_failure(finished.error))

        return self.start_job(job, task, completed)

    def start_rebuild(self, name, args):
        job = RebuildJob(name)
        job.cli_recipe = cli_recipes.Recipe('Rebuild sandbox', (cli_recipes.Step(tuple(args)),), str(self.state_root))
        return self.start_job(job, lambda log, update: self.service.run_update(args, log, update))

    def start_settings_job(self, name, title, task, *, success_message='', cooperative=False, cli_recipe=None):
        def completed(job):
            if job.result == 'Complete':
                job.notice = success_message
        job = RebuildJob(name, title)
        job.cli_recipe = cli_recipe
        job.update.cooperative = cooperative
        return self.start_job(job, task, completed)

    def start_creation(self, request):
        draft = request.draft
        name = draft['name']
        agent = coding_agent(draft)
        job = CreationJob(name, preview={
            'name': name, 'dev': 'creating', 'proxy': 'pending',
            'engine': draft.get('engine') or 'auto', 'workspace': draft.get('workspace'),
            'profile': str(draft.get('profile') or PRIMARY_AGENTS[agent]),
            'config': {'primary_agent': agent, 'network': draft.get('network', 'allowlist')},
        })
        job.cli_recipe = cli_recipes.creation(draft, self.state_root, web_keys=request.keys)

        def create_sandbox(log):
            if draft.get('mkdir'):
                Path(draft['workspace']).mkdir(parents=True, exist_ok=True)
            self.service.create(draft, request.keys, log, cancellation=job.creation)

        def completed(finished):
            if finished.result == 'Complete':
                self.prepare_job_setup(finished, draft, request.provider_label, creating=True, guide=request.guide)
            elif finished.result == 'Failed' and not job.creation.retry_blocked:
                finished.edit = lambda: self.push_screen(SettingsForm(draft, search_keys=request.keys))
                if not os.path.lexists(self.state_root / name):
                    finished.retry = lambda: self.start_creation(request)
            elif finished.result == 'Failed' and finished.error.startswith('Creation stopped, but cleanup failed'):
                finished.notice = 'From the sandbox list, choose Actions… → Delete sandbox… to finish cleanup.'

        return self.start_job(job, create_sandbox, completed)

    def update_configuration(self, draft, key, provider_label):
        name = draft['name']
        job = RebuildJob(name, 'Update settings · ' + name)
        job.cli_recipe = cli_recipes.full_update(draft, key, self.state_root)
        return self.start_job(job,
                              lambda log, update: self.service.update(name, draft, key, log, update),
                              lambda job: self.prepare_job_setup(job, draft, provider_label)
                              if job.result == 'Complete' else None)

    def prepare_job_setup(self, job, draft, provider_label, *, creating=False, guide=False):
        name = job.name
        agent = coding_agent(draft)
        mode = (draft.get('editor', editor_settings.DEFAULTS) if creating else
                editor_settings.load(self.state_root / name))['mode']
        open_requested = draft.get('open_editor')
        login = creating and draft.get('primary_auth') == 'login' and agent != 'pi' and mode != 'pi'
        providers = tuple(draft.get('_provider_credential_choices', {}))
        hosted = bool(draft.get('_hosted_changes'))
        approval = options(draft.get('options'))
        approval_model = (approval['approval_model'] if creating and approval['approval'] in
                          ('automatic', 'assisted') else None)
        if creating:
            if not open_requested:
                job.notice = ('Choose Open to start ' + PRIMARY_AGENTS[agent] + '.' if agent != 'pi' else
                              settings_effects.open_client(mode))
            if guide:
                client = 'Pi' if mode == 'pi' else 'FrostPi'
                job.notice = '\n'.join(filter(None, (job.notice, f'Type /profile-guide in {client} for first steps.')))
        else:
            job.notice = settings_effects.bullets(settings_effects.reconnect(mode if agent == 'pi' else None))

        def open_client():
            if login:
                self.interactive('primary-login', name)
            if agent == 'pi' and mode == 'pi':
                self.interactive('pi', name)
            elif agent == 'pi':
                def opened(editor_job):
                    if editor_job.result == 'Complete' and not creating:
                        job.notice = settings_effects.bullets(settings_effects.reconnect(mode, opened=True))
                self.start_job(Job(name, 'Open editor · ' + name, 'Opening editor'),
                               lambda log: self.service.run(['open', name], log), opened)
            else:
                self.launch_client(name, mode)

        def after_models(choice=None, *, launch=None):
            if choice is ModelDialog.LATER:
                job.followup = (CHOOSE_MODEL, choose_model)
                if creating:
                    self.render_records()
                self.update_jobs_badge()
            elif launch:
                launch()
            elif open_requested:
                open_client()

        def choose_model(launch=None):
            def finished(choice=None):
                after_models(choice, launch=launch)

            if len(providers) == 1 and not hosted:
                provider = providers[0]
                self.push_screen(ModelDialog(name, provider, plugin_label=provider_label or provider,
                                             optional=True, approval_model=approval_model, offer_later=True),
                                 finished)
            else:
                self.push_screen(ModelsScreen({'name': name, 'config': self.service.config(name)},
                                              finished=finished))

        def after_ssh(ready=True):
            if not ready:
                job.notice = 'Finish setting up the SSH key in Settings → Connectors → SSH & Git.'
                self.show_job(job)
            elif providers or hosted:
                choose_model()
            elif open_requested:
                open_client()

        method = draft.get('_ssh_key') if creating else None
        if method == 'generate':
            from .repository_ui import PublicKey
            job.followup = ('Set up SSH key', lambda: self.push_screen(PublicKey(name, generate=True), after_ssh))
        elif method == 'import':
            args = ['ssh-import-key', name, '--key-file', draft['_ssh_key_file'], '--duration', draft['_ssh_key_duration']]

            def import_key():
                with self.suspend():
                    result = subprocess.run(self.service.command(*args), check=False)
                after_ssh(result.returncode == 0)

            job.followup = ('Import SSH key', import_key)
        elif providers or hosted:
            job.followup = (CHOOSE_MODEL, choose_model)
        elif open_requested:
            job.followup = ('Sign in' if login else 'Open editor' if mode != 'pi' else 'Open terminal', open_client)

    def active_job(self, name):
        return next((job for job in self.jobs if name in job.targets and
                     (job.result is None or isinstance(job, CreationJob) and job in self.job_completions)), None)

    def pending_model_choice(self, name):
        return next((job for job in reversed(self.jobs) if isinstance(job, CreationJob) and job.name == name
                     and job.followup and job.followup[0] == CHOOSE_MODEL), None)

    def on_unmount(self):
        for job in self.jobs:
            if job.result:
                job.close()

    def update_details(self):
        record = self.selected()
        text = ('No matching sandboxes. Clear the search or use New sandbox (F2).'
                if self.records else 'No sandboxes yet. Use New sandbox (F2) to create one.')
        if record:
            text = Text.assemble(('Workspace: ', 'bold'), safe_text(record.get('workspace') or 'Unavailable'),
                                 '\n', ('Engine: ', 'bold'), safe_text(record.get('engine') or 'Unknown'),
                                 ' · ', ('Gateway: ', 'bold'), safe_text(record.get('proxy', 'Unknown')))
            agent = coding_agent(record['config'])
            label, value = ('Profile', record['profile']) if agent == 'pi' else ('Coding agent', PRIMARY_AGENTS[agent])
            text.append(' · ').append(label + ': ', style='bold').append(safe_text(value))
        details = self.query_one('#details', Static)
        details.update(text)
        details.tooltip = safe_text(record['workspace']) if record and record.get('workspace') else None
        for action in ('settings', 'more'):
            self.query_one('#' + action, Button).disabled = record is None or (action == 'settings' and bool(record.get('creation')))

    @on(Button.Pressed)
    def pressed(self, event):
        if self.screen_stack[0] not in event.button.ancestors:
            return
        event.stop()
        self.perform(event.button.id)

    def action_quit(self):
        if self.jobs_active():
            if isinstance(self.screen, FactoryUpdateDetails) and self.screen.heading == 'Jobs still running':
                return
            self.push_screen(FactoryUpdateDetails('Jobs still running',
                'Wait for jobs to finish before quitting. Open Jobs… from the sandbox list to check progress '
                'or cancel a job that offers it.\n\n'
                'If you close the terminal, reopening Factory won’t restore these jobs or their output.',
                close_label='Keep Factory open'))
            return
        self.exit()

    def check_action(self, action, parameters):
        if action in ('new', 'search', 'refresh_inventory', 'defaults', 'sandbox_actions', 'jobs'):
            return self.screen is self.screen_stack[0]
        return True

    def action_defaults(self):
        self.perform('defaults')

    def action_search(self):
        if self.screen is self.screen_stack[0]:
            self.query_one('#search', Input).focus()

    def action_new(self):
        if self.screen is self.screen_stack[0]:
            self.push_screen(NewSandbox())

    def perform(self, action, *, record=None):
        if action == 'application-update-run':
            self.confirm_update()
            return
        if action == 'application-update-help':
            self.push_screen(FactoryUpdateDetails('Update Factory manually',
                'If you run Factory from a Git checkout, open that checkout and run:\n'
                'git pull --ff-only\n\n'
                'If you installed Factory with pip, activate its Python environment and run:\n'
                'python -m pip install --upgrade \\\n'
                '  git+https://github.com/kyuz0/ai-sandbox-factory.git\n\n'
                'Restart Factory after updating.'))
            return
        try:
            self.perform_action(action, record=record)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.notify(safe_text(error), severity='error')

    def copied_sandbox(self, result):
        if result is not None:
            draft, keys = result
            self.push_screen(NewSandbox(draft, keys))

    def configuration_draft(self, record):
        name, config = record['name'], record['config']
        if not config.get('engine'):
            raise ValueError('Repair the saved configuration before editing it.')
        profile = config.get('profile', {})
        value = {**DEFAULT_SETTINGS, **config, 'profile': profile['path'] if saved_profile_only(config) else profile.get('registry_name', profile.get('name', 'base')),
                 'options': options(config.get('options')), 'local_model_ports': config.get('local_model_ports'),
                 'image_source': image_sources.mode(config)}
        if 'model_providers' not in config and value['local_model_ports']:
            value['model_providers'] = {'local': {}}
        if value['profile'] not in profile_titles():
            value['profile'] = profile['path']
        value['allowlist'] = (self.state_root / name / 'allowlist.txt').read_text()
        return value

    def perform_action(self, action, *, record=None):
        if action == 'jobs':
            self.action_jobs()
            return
        if action == 'create-from':
            if self.records:
                try:
                    screen = CreateFromSandbox(self.records, self.selected_name())
                except ValueError as error:
                    self.notify(safe_text(error), severity='warning')
                else:
                    self.push_screen(screen, self.copied_sandbox)
            return
        if action == 'packages':
            self.push_screen(PackagesScreen())
            return
        if action == 'new':
            self.action_new()
            return
        if action == 'defaults':
            self.push_screen(SettingsForm(preset(self.defaults['profile'], self.defaults), defaults=True))
            return
        record = record or self.selected()
        if not record:
            self.notify('Choose a sandbox first.')
            return
        name, config = record['name'], record['config']
        running_job = self.active_job(name)
        if running_job and action not in ('more', 'logs', 'containers', 'profile-guide', 'profile-information', 'alerts'):
            self.show_job(running_job)
            return
        if action == 'refresh-creation':
            self.action_refresh_inventory()
            return
        if record.get('creation') and action not in ('more', 'alerts', 'delete'):
            self.push_screen(SandboxAlerts(record),
                             lambda choice: self.perform(choice, record=record) if choice else None)
            return
        native = coding_agent(config) != 'pi'
        if action == 'cli-commands':
            names = [row['name'] for row in self.records]
            self.push_screen(CLICommands(loader=lambda: self.service.cli_recipe(name, existing_names=names)))
        elif action == 'primary-sign-in' and native:
            self.push_screen(PrimarySignIn(record))
        elif action == 'launch-with':
            self.push_screen(OpenWith(agent=coding_agent(config)), lambda mode: self.launch_client(name, mode) if mode else None)
        elif action == 'editor-settings':
            self.push_screen(EditorScreen(name))
        elif action == 'stop-editor':
            self.operation('Stopping browser editor', ['browser-editor', name, '--stop'])
        elif action == 'more':
            self.show_sandbox_actions(name)
        elif action == 'containers':
            self.push_screen(ContainersScreen(record))
        elif action == 'profile-information':
            self.push_screen(AppliedProfile(record, self.state_root / name))
        elif action == 'saved-service-data':
            try:
                saved = self.service.config(name)
            except (OSError, ValueError) as error:
                self.notify(f'Cannot read saved service data for {name}: {error}. Check the sandbox settings and retry.', severity='error')
            else:
                self.push_screen(SavedServiceData({**record, 'config': saved}))
        elif action == 'profile-guide':
            profile = sandbox.selected_profile(config)
            self.push_screen(ProfileGuide(profile.get('description') or profile['name'], registry.profile_guide(profile['path'])))
        elif action == 'alerts' and sandbox_alerts(record):
            self.push_screen(SandboxAlerts(record),
                             lambda choice: self.perform(choice, record=record) if choice else None)
        elif action == 'settings':
            state = self.state_root / name
            try:
                saved = self.service.config(name) if config.get('engine') else config
            except (OSError, ValueError):
                saved = {}
            self.push_screen(SettingsMenu(name, saved, editor_settings.load(state)['mode'], prompt_library.selection(state), state=state),
                             lambda choice: self.perform(choice, record=record) if choice else None)
        elif action == 'aws-access':
            saved_access = self.service.config(name).get('aws_access') or {}

            def change(value):
                if value is None or value == saved_access:
                    return
                try:
                    current = self.service.config(name)
                    effect = sandbox.aws_installation(current, self.state_root / name, value)[0] if value else 'ready'
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.notify(safe_text(error), severity='error')
                    return

                def apply(confirmed=True):
                    if not confirmed:
                        return
                    changes = {'aws_access': value, 'expected_aws_access': saved_access,
                               'expected_aws_installation': effect}
                    success = ('AWS disconnected.' if not value else
                               'AWS access is ready. Choose Open to resume work.' if effect == 'restart' else
                               'AWS access saved for the next start.' if effect in ('replace', 'prepare') else
                               'AWS access updated.')
                    self.start_settings_job(name, 'Update AWS access · ' + name, lambda log, update:
                        self.service.update_settings(name, changes, log, update), success_message=success)

                if effect in ('restart', 'replace', 'gateway'):
                    self.push_screen(confirm_aws_installation(name, effect,
                                                               restricted=sandbox.network_mode(current) == 'allowlist'), apply)
                else:
                    apply()

            self.push_screen(AWSProfilePicker(saved_access, sandbox_name=name), change)
        elif action == 'folders':
            self.push_screen(FoldersScreen(record))
        elif action == 'ssh-settings':
            self.push_screen(SSHScreen(record))
        elif action == 'ssh-key-alert':
            focus = '#ssh-unlock' if record.get('ssh_key_state') == 'locked' else '#ssh-key-file'
            self.push_screen(SSHScreen(record, key_focus=focus))
        elif action == 'advisors':
            self.push_screen(AdvisorsScreen(record))
        elif action == 'system-prompt':
            if not config.get('engine'):
                raise ValueError('Repair the saved configuration before editing it.')
            common = Path(config['common']['path'])
            self.push_screen(SandboxPromptScreen(name, self.state_root / name, native or (common / 'system-prompt.mjs').is_file(),
                                                 agent=PRIMARY_AGENTS[coding_agent(config)]))
        elif action == 'models':
            self.push_screen(ModelsScreen(record))
        elif action in EDIT_FORMS or (native and action in ('primary-browser', 'primary-approvals')):
            self.push_screen(SettingsForm(self.configuration_draft(record), editing=True, setting=action))
        elif action in ('preview-models', 'refresh-models'):
            mode = 'preview' if action == 'preview-models' else 'refresh'
            self.start_task(name, 'Local models · ' + name, lambda log: self.service.models(name, mode, log),
                            working='Checking local models')
        elif action in ('export-chats', 'import-chats'):
            self.push_screen(ChatTransfer(name, importing=(action == 'import-chats'), agent=coding_agent(config)))
        elif action == 'copy-chat':
            from .session_transfer_ui import ContinueChat
            self.push_screen(ContinueChat(name))
        elif action == 'delete':
            self.push_screen(DeleteSandbox(name, has_config=bool(config.get('engine')), agent=coding_agent(config),
                                           services=list(profile_services.selected(config)),
                                           retained_volumes=config.get('profile_service_retained_volumes', [])),
                             lambda yes: self.operation('Deleting ' + name, ['delete', name]) if yes else None)
        elif action == 'pi':
            self.launch_client(name, 'pi')
        elif action == 'shell':
            self.interactive('shell', name)
        elif action == 'open':
            self.launch_client(name, editor_settings.load(self.state_root / name)['mode'])
        elif action == 'connections':
            args = ['exec', name, '--', 'pi', 'mcp', 'list']
            self.operation('Checking configured MCP connections', args)
        elif action == 'logs':
            def logs(log):
                result = subprocess.run([config['engine'], 'logs', '--tail', '100', sandbox.names(config)[0]],
                                        capture_output=True, text=True)
                log(result.stdout + result.stderr)
                if result.returncode:
                    raise ValueError(f'{config["engine"]} logs exited with status {result.returncode}.')
            self.start_task(name, 'Container logs · ' + name, logs, working='Reading container logs')
        elif action == 'install-system-packages':
            if record.get('dev') != 'running':
                self.notify('Start the sandbox, then choose Install system packages again.')
                return

            def install_packages(packages):
                if packages:
                    job = Job(name, 'Install system packages · ' + name, 'Preparing package installation')
                    update = Update(progress=job.set_progress)
                    self.start_job(job, lambda log: self.service.run_update(
                        ['install-system-packages', name, *packages], log, update))

            self.push_screen(InstallSystemPackages(name), install_packages)
        elif action == 'edit-profile':
            self.push_screen(EditProfile(record))
        elif action == 'rebuild':
            profile = config.get('profile', {})
            source = profile.get('registry_name', profile.get('name', 'base'))
            args = ['recreate', name]
            migration = [] if native else settings_effects.editor_migration(config)
            use_saved_profile = saved_profile_only(config)
            action_label = 'Rebuild sandbox' if use_saved_profile else 'Update sources and recreate'
            if use_saved_profile:
                question = f'Rebuild {name} using its saved profile and current Factory software?'
            else:
                args += ['--profile', source if source in profile_titles() else profile['path']]
                profile_source = 'the installed profile' if source in profile_titles() else 'its saved profile'
                question = f'Update {name} to FrostPi?' if migration else f'Rebuild {name} using {profile_source} and current Factory software?'
            notes = ['Workspace files, private home and saved chats are kept.']
            notes[0:0] = ['If the sandbox is running, work in its main agent sandbox stops after the build.',
                          'This includes active tasks, terminals and browser processes.',
                          'If the sandbox is stopped, it stays stopped.']
            if profile.get('services'):
                notes.append('Profile services and their application data are kept.')
            self.push_screen(Confirm(question + '\n' + settings_effects.bullets(notes), action_label, confirm_by_default=True),
                lambda yes: self.start_rebuild(name, args) if yes else None)
        elif action == 'start':
            self.start_task(name, 'Start sandbox · ' + name, lambda log: self.service.start(name, log), working='Starting sandbox')
        elif action == 'stop':
            self.operation('Stopping ' + name, ['down', name])

    def launch_client(self, name, mode):
        if job := self.pending_model_choice(name):
            self.resume_job(job, lambda: self.launch_client(name, mode))
        elif mode is None:
            self.interactive('open', name)
        elif mode == 'pi':
            self.interactive('pi', name)
        else:
            self.start_task(name, 'Open editor · ' + name, lambda log: self.open_ready(name, log, mode), working='Opening editor')

    def open_ready(self, name, log, mode=None):
        with operation_locks.sandbox(name, 'open the editor for'):
            self.service.start(name, log)
            args = ['open', name]
            if mode:
                args += ['--editor', mode]
            self.service.run(args, log)

    def operation(self, title, args, *, success_message=''):
        command, name, *_ = args
        if command == 'delete':
            def completed(job):
                if job.result == 'Complete':
                    for previous in self.jobs:
                        if name in previous.targets and previous.result is not None:
                            previous.discard_followup()

            return self.start_job(Job(name, title, 'Deleting sandbox'),
                                  lambda log: self.service.run(args, log), completed)
        return self.start_task(name, title, lambda log: self.service.run(args, log), success_message=success_message)

    def interactive(self, command, name):
        if self.upgrade_pending():
            self.notify('Finish upgrading and restart Factory before opening a sandbox session.')
            self.show_job(self.upgrade_job)
            return
        with self.suspend():
            if command in ('pi', 'open', 'primary-login'):
                result = subprocess.run(self.service.command(command, name), check=False)
            else:
                result = subprocess.run(self.service.command('up', name), check=False)
                if result.returncode == 0:
                    subprocess.run(self.service.command(command, name), check=False)
        if command in ('open', 'primary-login'):
            action = 'open' if command == 'open' else 'sign in to'
            feedback = self.screen_stack[0].query_one('#open-error', Feedback)
            feedback.update(f'Could not {action} {name}. Open Settings → Sign-in to check the account.'
                            if result.returncode else '')
        self.refresh_inventory()


def launch(state_root, *, startup_notice=''):
    if Cockpit(state_root, startup_notice=startup_notice).run() == updates.RELAUNCH:
        os.execv(sys.executable, host_platform.python_command('ai_sandbox_factory',
                                 '--state-dir', str(state_root), 'tui'))
