"""Render offline TUI fixtures for visual review: python tests/render_tui.py OUTPUT_DIR."""
import argparse
import os
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=str)
parser.add_argument('--size', action='append', help='Terminal size, e.g. 80x24; defaults to all three review sizes')
parser.add_argument('--only', nargs='+', help='Capture only these named states')
parser.add_argument('--monochrome', action='store_true')
args = parser.parse_args()
if args.monochrome:
    os.environ['NO_COLOR'] = '1'
else:
    os.environ.pop('NO_COLOR', None)
os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'
import asyncio, copy, json, subprocess, sys, tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
from test_tui import FakeService
from tui_geometry import check_checkbox_alignment, check_dropdown_layout, check_input_layout, check_table_layout
from test_hosted_models_ui import ModelService
from ai_sandbox_factory import sandbox, registry, host_platform
from ai_sandbox_factory import aws_profiles
from ai_sandbox_factory.aws_ui import AWSAccessDialog, AWSProfilePicker, AWSProfileTargets
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.settings_catalog import EDIT_FORMS
from ai_sandbox_factory.sandbox_tui import ContainersScreen, SandboxAlerts, confirm_aws_installation
from ai_sandbox_factory.sandbox_tui import Cockpit, options, preset, DEFAULT_SETTINGS, NewSandbox, SettingsForm, FoldersScreen, SettingsMenu, MoreActions, ModelsScreen, PackagesScreen, AdvisorsScreen, SSHScreen, CreateFromSandbox, SearchProviderDialog, Confirm, FolderPicker, InstallPackage, CopyWebAccess, Operation, CreationOperation, UpdateOperation, settings_effects, web_access
from textual.widgets import Button, Collapsible, Input, Static, OptionList, Select, RichLog, TextArea
from textual.containers import VerticalScroll
from ai_sandbox_factory.tui_widgets import Checkbox
from ai_sandbox_factory.folders_ui import FolderEditor
from ai_sandbox_factory.editor_ui import EditorScreen, OpenWith
from ai_sandbox_factory.models_ui import ProviderDialog, ModelDialog
from ai_sandbox_factory.credentials_ui import CredentialDialog, LocalKeysDialog
from ai_sandbox_factory import hosted_models, inference, provider_credentials
from ai_sandbox_factory.communications_ui import SlackCredentials
from ai_sandbox_factory.repository_ui import RepositorySetup, PublicKey, GitIdentityFields, CreateSSHDialog
from ai_sandbox_factory import prompt_library
from ai_sandbox_factory.prompt_ui import EditPrompt, SandboxPromptScreen
from ai_sandbox_factory.chat_history_ui import ChatTransfer, DeleteSandbox, backup_message
from textual_fspicker.parts import DirectoryNavigation

class ReviewService(FakeService):

    def ssh_key(self, *args):
        return 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE3uK0K5OO6n4efS1W21F9JbLM5Ko34YTK95svh9jMfN pi-sandbox-factory'
    populated = False

    def hosted_request(self, *args, **kw):
        return ModelService.hosted_request(self, *args, **kw) if self.populated else {'providers': [], 'models': []}

    def provider_key_status(self, name):
        return {'configured': ['company'], 'assignments': {'company': {'ref': 'a'*32, 'label': 'Work'}}}
out = Path(args.output)
out.mkdir(exist_ok=True, parents=True)
coverage = []

async def capture(size):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / 'project'
        workspace.mkdir()
        (workspace / 'app').mkdir()
        reference = root / 'reference'
        reference.mkdir()
        c = dict(name='demo', engine='podman', network='allowlist', workspace=str(workspace), profile=sandbox.load_profile('base'), local_model_ports='8000', memory='8g', subagents=False, working_directory='app', extra_folders=[dict(source=str(reference), name='reference', read_only=True)], model_providers=[dict(name='local', manifest={'description': 'Local models'}, settings={})], options=options())
        state = root / 'demo'
        state.mkdir()
        (state / 'allowlist.txt').write_text('local-model.internal:8000\n')
        record = dict(name='demo', profile='base', engine='podman', dev='running', proxy='running', workspace=str(workspace), config=c)
        research_only = args.only and all(name.startswith('research-') for name in args.only)
        prompts_only = args.only and all(name.startswith('prompt-') for name in args.only)
        aws_only = args.only and all(name.startswith('aws-access-') for name in args.only)
        variables = {'SANDBOX_REGISTRY_DIR': str(root / 'library')} if research_only or prompts_only or aws_only else {}
        with patch.dict(os.environ, variables), patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), patch('ai_sandbox_factory.sandbox_tui.search_keys_path', return_value=root / 'keys.json'), patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.chat_history.backup_folder', return_value=Path('/home/sam/.local/share/ai-sandbox-factory/chat-backups')):
            app = Cockpit(root, ReviewService([record]), startup_notice=('Pi Sandbox Factory is now AI Sandbox Factory. Use ai-sandbox-factory next time; pi-sandbox-factory still works.' if args.only and 'home-renamed' in args.only else ''))
            async with app.run_test(size=size, notifications=app.startup_notice) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                async def shot(name, screen=None, section=None, focus=None):
                    if args.only and name not in args.only:
                        return
                    if screen:
                        await app.push_screen(screen)
                    if section:
                        s = app.screen.query_one(section)
                        for parent in reversed(s.ancestors):
                            if isinstance(parent, Collapsible):
                                parent.collapsed = False
                        s.collapsed = False
                        await pilot.pause()
                        s.scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    if focus:
                        widget = app.screen.query_one(focus)
                        for parent in reversed(widget.ancestors):
                            if isinstance(parent, Collapsible):
                                parent.collapsed = False
                        await pilot.pause()
                        widget.focus()
                        await pilot.pause()
                    p = out / f'{name}-{size[0]}x{size[1]}.svg'
                    p.write_text(app.export_screenshot())
                    if isinstance(app.screen, CreateSSHDialog):
                        panel = app.screen.query_one('Vertical').content_region
                        assert all(panel.contains_region(button.region)
                                   for button in app.screen.query('.dialog-actions Button')), name
                        destinations = app.screen.query_one('#ssh-hosts')
                        assert destinations.region.x == destinations.parent.query_one('Label').region.x, name
                        if name == 'connector-review-identity-populated':
                            name_field = app.screen.query_one('#git-author-name')
                            email_label = app.screen.query_one('#git-author-email').parent.query_one('Label')
                            help_text = app.screen.query_one('GitIdentityFields .checkbox-help')
                            assert name_field.region.x == help_text.content_region.x, name
                            assert email_label.region.y == name_field.region.bottom + 1, name
                    if isinstance(app.screen, SettingsForm) and name.startswith('connector-review-') and app.screen.focused.id == 'connector-ssh':
                        assert app.screen.focused.styles.background != app.screen.query_one('#apply').styles.background, name
                    if isinstance(app.screen, AWSAccessDialog):
                        box = app.screen.query_one('#confirm-box').content_region
                        assert all(box.contains_region(button.region) for button in app.screen.query(Button)), name
                    check_checkbox_alignment(app.screen)
                    check_dropdown_layout(app.screen)
                    check_table_layout(app.screen)
                    coverage.append(dict(file=p.name, screen=type(app.screen).__name__, size=size,
                                         inputs=check_input_layout(app.screen)))
                    print(p.name, flush=True)
                    if screen:
                        await app.pop_screen()
                if args.only and all(name.startswith('native-web-search-') for name in args.only):
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='demo', workspace=str(workspace), primary_agent='codex',
                                 model_providers={}, editor={'mode': 'browser'})
                    draft['options']['codex_web_search'] = True
                    await app.push_screen(SettingsForm(draft))
                    await pilot.pause()
                    form = app.screen
                    form.query_one('#network-section', Collapsible).collapsed = False
                    checkbox = form.query_one('#codex-web-search', Checkbox)
                    checkbox.scroll_visible(animate=False)
                    await shot('native-web-search-on', focus='#codex-web-search')
                    checkbox.value = False
                    await shot('native-web-search-off', focus='#codex-web-search')
                    await app.pop_screen()
                    await app.push_screen(SettingsForm(draft, editing=True, setting='network-settings'))
                    await shot('native-web-search-edit', focus='#codex-web-search')
                    app.screen.query_one('#codex-web-search', Checkbox).value = False
                    with patch.object(app.service, 'update_settings', create=True,
                                      side_effect=ValueError('Could not save web search: fixture storage is read-only.')):
                        app.screen.query_one('#apply', Button).press()
                        await pilot.pause()
                        await shot('native-web-search-error')
                    return
                if args.only and set(args.only) <= {'home', 'home-renamed'}:
                    await shot('home-renamed' if app.startup_notice else 'home')
                    return
                if args.only and set(args.only) == {'ssh-alias-invalid'}:
                    draft = preset('base', DEFAULT_SETTINGS)
                    await app.push_screen(CreateSSHDialog(draft, 'SSH & Git'))
                    await pilot.pause()
                    aliases = app.screen.query_one('#ssh-aliases', Input)
                    for ancestor in aliases.ancestors:
                        if isinstance(ancestor, Collapsible):
                            ancestor.collapsed = False
                    aliases.value = 'build*'
                    aliases.focus()
                    aliases.scroll_visible(animate=False)
                    await pilot.pause()
                    app.screen.query_one('#ssh-import-aliases', Button).press()
                    await shot('ssh-alias-invalid')
                    return
                if args.only and all(name.startswith('native-docs-') for name in args.only):
                    record['config']['primary_agent'] = 'agy'
                    app.service.records = [record]
                    await app.push_screen(EditorScreen('demo'))
                    await pilot.pause()
                    app.screen.query_one('#editor-mode', Select).expanded = True
                    await shot('native-docs-editor', focus='#editor-mode')
                    await app.pop_screen()
                    draft = preset('base', DEFAULT_SETTINGS)
                    draft.update(name='demo', workspace=str(workspace), primary_agent='codex')
                    await app.push_screen(SettingsForm(draft))
                    await pilot.pause()
                    app.screen.query_one('#primary-approvals-section', Collapsible).collapsed = False
                    await pilot.pause()
                    choice = app.screen.query_one('#native-approval', Select)
                    choice.focus()
                    choice.scroll_visible(animate=False)
                    await pilot.pause()
                    choice.expanded = True
                    await shot('native-docs-approvals')
                    return
                if args.only and all(name.startswith('picker-') for name in args.only):
                    for relative in ('reports', 'archive', '.hidden'):
                        (workspace / relative).mkdir()
                    for relative in ('report.zip', 'report.txt', 'archive/report.zip', '.hidden/report.zip'):
                        (workspace / relative).touch()
                    await shot('picker-folder', FolderPicker(workspace))
                    await app.push_screen(FolderPicker(workspace, title='Choose a file', mode='file', file_extensions={'.zip'}))
                    app.screen.query_one('#folder-filter', Input).value = 'report'
                    await shot('picker-filter', focus='#folder-filter')
                    app.screen.query_one('#folder-recursive', Checkbox).value = True
                    await shot('picker-subfolders', focus='#folder-recursive')
                    app.screen.query_one('#folder-hidden', Checkbox).value = True
                    await shot('picker-hidden', focus='#folder-hidden')
                    app.screen.query_one('#folder-filter', Input).value = 'missing'
                    await shot('picker-empty', focus='#folder-filter')
                    await app.pop_screen()
                    await app.push_screen(FolderPicker(workspace, title='Choose where to save the backup', mode='save', filename='backup.zip', file_extensions={'.zip'}))
                    await shot('picker-save', focus='#folder-filename')
                    app.screen.query_one('#folder-filename', Input).value = '../backup.zip'
                    app.screen.save_path()
                    await shot('picker-error', focus='#folder-filename')
                    await app.pop_screen()
                    await app.push_screen(FolderPicker(workspace, mode='file'))
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    nav = app.screen.navigation
                    nav.receive_entries(nav.generation, [], False, False,
                                        ['reference: Permission denied', 'archive: Permission denied'])
                    await shot('picker-read-error', focus='#folder-errors')
                    await pilot.pause()
                    await pilot.click('#folder-errors')
                    await shot('picker-read-details', focus='#picker-errors-close')
                    await pilot.press('escape')
                    await app.pop_screen()
                    return
                if not args.only or any(name.startswith('connector-settings-') for name in args.only):
                    from ai_sandbox_factory import connectors
                    from ai_sandbox_factory.connector_dialogs import ConnectorSettings, ConnectorSetup, ExternalConnectorDialog
                    with patch.dict(os.environ, XDG_CONFIG_HOME=str(root / 'connector-home')), \
                            patch.object(connectors, 'has_token', return_value=True) as saved_token:
                        await shot('connector-settings-menu', SettingsMenu('demo', c, 'browser'))
                        await shot('connector-settings-empty', ConnectorSettings('demo', c))
                        path = connectors.directory() / 'release-status' / 'connector.json'
                        path.parent.mkdir(parents=True)
                        path.write_text(json.dumps({
                            'version': 1, 'id': 'release-status', 'title': 'Release status',
                            'description': 'Read the current release status.',
                            'documentation': 'Use the status request to check the latest release.',
                            'operations': {'status': 'https://releases.example.org/api/status'},
                        }))
                        plugin = next(item for item in connectors.catalog() if item.external)
                        await shot('connector-settings-installed', ConnectorSettings('demo', c))
                        await shot('connector-settings-connect', ExternalConnectorDialog(plugin, enabled=False))
                        await shot('connector-settings-disconnect', ExternalConnectorDialog(plugin, enabled=True))
                        saved_token.return_value = False
                        await shot('connector-settings-token', ExternalConnectorDialog(plugin, enabled=False))
                        saved_token.return_value = True
                        manifest = json.loads(path.read_text())
                        manifest['description'] = 'Read current and past release information.'
                        manifest['agent_instructions'] = 'Include the release date in your answer.'
                        manifest['operations']['history'] = {'url': 'https://releases.example.org/api/history',
                                                             'instructions': 'Use history for past releases.'}
                        path.write_text(json.dumps(manifest))
                        plugin = next(item for item in connectors.catalog() if item.external)
                        await shot('connector-settings-select', ExternalConnectorDialog(plugin, enabled=False))
                        await shot('connector-settings-edit', ExternalConnectorDialog(
                            plugin, enabled=True, enabled_operations=['status']))
                        for example in ('draft', 'guide'):
                            detail = ExternalConnectorDialog(plugin, enabled=True, draft=example == 'draft',
                                                             enabled_operations=['status'])
                            await app.push_screen(detail)
                            for box in detail.query(Checkbox):
                                box.value = box.name == 'history'
                            await pilot.pause()
                            assert not detail.query_one('#connector-confirm', Button).disabled
                            await shot('connector-settings-' + example,
                                       section='#connector-agent-guide' if example == 'guide' else None)
                            await app.pop_screen()
                        await shot('connector-settings-help', ConnectorSetup(plugin))
                        config = {**c, 'connector_ids': [plugin.id], 'connector_operations': {plugin.id: ['status']},
                                  'aws_access': {'profile_name': 'Research'},
                                  'ssh': {'hosts': [dict(alias='release')]}}
                        await shot('connector-settings-menu-assigned', SettingsMenu('demo', config, 'browser'))
                        with patch.object(app.service, 'config', return_value=config):
                            await app.push_screen(AWSProfilePicker(config['aws_access'], sandbox_name='demo'))
                            await pilot.pause()
                            app.screen.query_one('#aws-disconnect', Button).press()
                            await pilot.pause()
                            await shot('connector-settings-aws-disconnect')
                            await pilot.press('escape')
                            await app.pop_screen()
                        connectors.snapshot(config['connector_ids'], state)
                        with patch.object(app.service, 'config', return_value=config):
                            await shot('connector-settings-assigned', ConnectorSettings('demo', config))
                        path.write_text('invalid json')
                        await shot('connector-settings-error', ConnectorSettings('demo', c))
                        saved_manifest = state / 'connector-manifests' / (plugin.id + '.json')
                        saved_manifest.write_text('{')
                        recovery = next(item for item in connectors.for_sandbox(config, state) if item.external)
                        with patch.object(app.service, 'config', return_value=config):
                            await shot('connector-settings-damaged', ConnectorSettings('demo', config))
                            if args.only and 'connector-settings-saved-error' in args.only:
                                await app.push_screen(ConnectorSettings('demo', config))
                                await pilot.pause()
                                app.screen.query_one('.connector-settings-body', VerticalScroll).scroll_end(animate=False)
                                await shot('connector-settings-saved-error')
                                await app.pop_screen()
                        await shot('connector-settings-revoke-damaged', ExternalConnectorDialog(recovery, enabled=True))
                        await app.push_screen(ExternalConnectorDialog(plugin, enabled=True, enabled_operations=['status']))
                        app.screen.query_one('#connector-disconnect', Button).press()
                        await pilot.pause()
                        await shot('connector-settings-confirm-disconnect')
                        await pilot.press('escape')
                        await app.pop_screen()
                    if args.only and all(name.startswith('connector-settings-') for name in args.only):
                        return
                if not args.only or any(name.startswith('connector-review-') for name in args.only):
                    package = root / 'packages' / 'build-status'
                    package.mkdir(parents=True)
                    example = Path(__file__).resolve().parents[1] / 'examples/connectors/build-status/connector.json'
                    (package / 'connector.json').write_bytes(example.read_bytes())
                    with patch('ai_sandbox_factory.connectors.directory', return_value=package.parent):
                        draft = preset('base', DEFAULT_SETTINGS)
                        draft.update(name='demo', workspace='/home/sam/projects/research')
                        await app.push_screen(SettingsForm(draft))
                        section = app.screen.query_one('#connectors-section', Collapsible)
                        app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                        section.query_one('CollapsibleTitle').focus()
                        await pilot.pause()
                        await shot('connector-review-collapsed')
                        await shot('connector-review-empty', section='#connectors-section', focus='#connector-ssh')
                        section.collapsed = False
                        await pilot.pause()
                        section.query_one('#planned-connectors').scroll_visible(animate=False)
                        await shot('connector-review-planned')
                        await app.pop_screen()
                        hosts = {'hosts': [dict(alias='repository', user='git', hostname='git.example.test', port=22,
                                                host_keys=['ssh-ed25519 AAAA'])]}
                        draft.update(ssh=hosts, aws_access={'profile_name': 'Research'}, connector_ids=['build-status'],
                                     connector_operations={'build-status': ['latest']})
                        await shot('connector-review-assigned', SettingsForm(draft), '#connectors-section', '#connector-ssh')
                        await shot('connector-review-native', SettingsForm({**draft, 'primary_agent': 'codex'}),
                                   '#connectors-section', '#connector-ssh')
                        await shot('connector-review-native-sign-in', SettingsForm({**draft, 'primary_agent': 'codex'}),
                                   '#primary-sign-in-section', '#primary-auth')
                        for mode in ('pi', 'desktop'):
                            await shot('connector-review-open-' + mode, SettingsForm({**draft, 'editor': {'mode': mode}}),
                                       '#connectors-section', '#connector-ssh')
                        await shot('connector-review-ssh', CreateSSHDialog(draft, 'SSH & Git'))
                        await app.push_screen(CreateSSHDialog({**draft, 'ssh': {}, '_ssh_key': 'later'}, 'SSH & Git'))
                        app.screen.query_one('#create-ssh-key', Select).value = 'generate'
                        await pilot.pause()
                        save = app.screen.query_one('#create-ssh-save', Button)
                        save.press()
                        await pilot.pause(save.active_effect_duration)
                        await shot('connector-review-ssh-error')
                        await app.pop_screen()
                        identity = {'name': 'Sam Developer', 'email': 'sam@example.test'}
                        for name, selected_hosts in (('populated', hosts), ('no-destinations', {'hosts': []})):
                            config = {**draft, 'ssh': selected_hosts,
                                      'options': {**draft['options'], 'git_identity': identity}}
                            if selected_hosts['hosts']:
                                await shot('connector-review-identity-' + name, CreateSSHDialog(config, 'SSH & Git'),
                                           section='#git-identity-section', focus='#git-author-email')
                            else:
                                await shot('connector-review-identity-' + name, CreateSSHDialog(config, 'SSH & Git'))
                            if not selected_hosts['hosts']:
                                await shot('connector-review-identity-only', SettingsForm(config),
                                           '#connectors-section', '#connector-ssh')
                    if args.only and all(name.startswith('connector-review-') for name in args.only):
                        return
                if not args.only or any(name.startswith('aws-access-') for name in args.only):
                    grant = dict(profile='research', role='arn:aws:iam::123456789012:role/Research',
                                 region='eu-west-1', grants=[{'Action': 'ec2:DescribeRegions', 'Resource': '*'}])
                    await shot('aws-access-setup', AWSAccessDialog({}))
                    with patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(root / 'aws-library')):
                        await shot('aws-access-empty', AWSProfilePicker())
                        await shot('aws-access-restart', confirm_aws_installation('research', 'restart', restricted=True))
                        await shot('aws-access-stopped', confirm_aws_installation('research', 'replace'))
                        await shot('aws-access-gateway', confirm_aws_installation('research', 'gateway'))
                        profile = aws_profiles.save('Research', grant)
                        await shot('aws-access-library', PackagesScreen('aws-profiles'))
                        await shot('aws-access-picker', AWSProfilePicker(profile))
                        await shot('aws-access-connected', AWSAccessDialog(profile))
                        await shot('aws-access-duplicate', AWSAccessDialog(profile, duplicate=True))
                        await shot('aws-access-targets', AWSProfileTargets(profile, {'workbench': grant, 'research': grant}))
                    await app.push_screen(AWSAccessDialog())
                    app.screen.query_one('#aws-save', Button).press()
                    await shot('aws-access-error')
                    await app.pop_screen()
                    draft = preset('base', DEFAULT_SETTINGS)
                    draft.update(name='research', workspace=str(workspace))
                    await app.push_screen(SettingsForm(draft))
                    await pilot.pause()
                    app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                    aws_section = app.screen.query_one('#connectors-section', Collapsible)
                    aws_section.query_one('CollapsibleTitle').focus(scroll_visible=False)
                    await pilot.pause()
                    app.screen.query_one('.form', VerticalScroll).scroll_home(animate=False)
                    await shot('aws-access-review-off')
                    await shot('aws-access-review-configure', section='#connectors-section', focus='#connector-aws')
                    aws_section.collapsed = True
                    advisor_section = app.screen.query_one('#new-advisors-section', Collapsible)
                    advisor_section.query_one('CollapsibleTitle').focus()
                    await shot('aws-access-review-advisors')
                    await shot('aws-access-review-advisors-expanded', section='#new-advisors-section')
                    await app.pop_screen()
                    await shot('aws-access-review-assigned', SettingsForm({**draft, 'aws_access': grant}),
                               '#connectors-section', '#connector-aws')
                    await shot('aws-access-review-native', SettingsForm({**draft, 'primary_agent': 'codex', 'aws_access': grant}),
                               '#connectors-section', '#connector-aws')
                    await shot('aws-access-settings', SettingsMenu('demo', {**c, 'aws_access': grant}, 'browser'), focus='#settings-actions')
                    await shot('aws-access-advisor-settings', AdvisorsScreen(record))
                    if aws_only:
                        return
                if args.only and all(name.startswith('folder-layout-') for name in args.only):
                    folder_config = dict(c, workspace='/home/sam/projects/research', working_directory='notes',
                                         extra_folders=[dict(source='/home/sam/documents/reference',
                                                             name='reference', read_only=True)])
                    folder_record = {**record, 'name': 'research', 'config': folder_config}
                    is_dir = Path.is_dir
                    with patch.object(Path, 'is_dir', lambda path: str(path) == folder_config['workspace'] or is_dir(path)):
                        await shot('folder-layout-settings', FoldersScreen(folder_record))
                        await shot('folder-layout-empty', FoldersScreen({**folder_record, 'config': {
                            **folder_config, 'extra_folders': []}}))
                        draft = preset('base', DEFAULT_SETTINGS)
                        draft.update(name='research', workspace=folder_config['workspace'],
                                     working_directory='notes', extra_folders=folder_config['extra_folders'])
                        await shot('folder-layout-setup', SettingsForm(draft), '#folders-section')
                    await app.push_screen(FolderEditor(None, [], str(workspace), root))
                    await shot('folder-layout-add', focus='#folder-source')
                    app.screen.query_one('#folder-save', Button).press()
                    await shot('folder-layout-error')
                    await app.pop_screen()
                    await shot('folder-layout-edit', FolderEditor(folder_config['extra_folders'][0], [],
                                                                  str(workspace), root), focus='#folder-name')
                    return
                if not args.only or any(name.startswith('chat-history-') for name in args.only):
                    sample = '/home/sam/.local/share/ai-sandbox-factory/chat-backups/demo-chats-20261120-164101.zip'
                    (workspace / 'demo-chats.zip').touch()
                    (workspace / 'old-chat.jsonl').touch()
                    await shot('chat-history-actions', MoreActions(record))
                    await shot('chat-history-delete', DeleteSandbox('demo', has_config=True))
                    await shot('chat-history-delete-codex', DeleteSandbox('demo', has_config=True, agent='codex'))
                    await app.push_screen(DeleteSandbox('demo', has_config=True))
                    await pilot.pause()
                    app.screen.show_backup(backup_message(sample, 3))
                    await shot('chat-history-delete-backed-up')
                    await app.pop_screen()
                    for importing in (False, True):
                        action = 'import' if importing else 'export'
                        await app.push_screen(ChatTransfer('demo', importing=importing))
                        await pilot.pause()
                        if not importing:
                            field = app.screen.query_one('#chat-transfer-path', Input)
                            field.value = sample
                            field.cursor_position = len(sample)
                        await shot('chat-history-' + action)
                        if importing:
                            app.screen.query_one('#chat-transfer-path', Input).value = str(workspace / 'demo-chats.zip')
                        app.screen.failed('The chat backup is incomplete or damaged.' if importing
                                          else 'A file already exists there. Choose another name.')
                        await shot('chat-history-' + action + '-error')
                        app.screen.query_one('#chat-transfer-error').update('')
                        app.screen.query_one('#chat-transfer-result').display = True
                        app.screen.completed(2, 1) if importing else app.screen.completed(sample, 3)
                        await shot('chat-history-' + action + '-complete')
                        if importing:
                            app.screen.completed(0, 3)
                            await shot('chat-history-import-duplicates')
                        await app.pop_screen()
                    await app.push_screen(FolderPicker(str(workspace), title='Choose chat backup or Pi chat file', mode='file',
                                                       file_extensions={'.zip', '.jsonl'}, places={'Chat backups': workspace}))
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await shot('chat-history-import-picker')
                    listing = app.screen.query_one(DirectoryNavigation)
                    listing.highlighted = next(index for index in range(listing.option_count)
                                               if listing.get_option_at_index(index).location.name == 'demo-chats.zip')
                    await shot('chat-history-import-file-selected')
                    await app.pop_screen()
                    await shot('chat-history-export-picker', FolderPicker(str(workspace), title='Choose where to save the backup',
                                                                         mode='save', file_extensions={'.zip'}, filename='demo-chats.zip'))
                    for agent in ('codex', 'claude', 'agy'):
                        prefix = 'chat-history-native-' + agent
                        if args.only and not any(name.startswith(prefix) for name in args.only):
                            continue
                        await shot(prefix + '-actions', MoreActions({
                            **record, 'dev': 'exited', 'config': {**c, 'primary_agent': agent}}))
                        await app.push_screen(DeleteSandbox('demo', has_config=True, agent=agent))
                        await shot(prefix + '-delete')
                        app.screen.show_backup(backup_message(sample, None))
                        await shot(prefix + '-delete-backed-up')
                        await app.pop_screen()
                        for importing in (False, True):
                            action = prefix + ('-import' if importing else '-export')
                            await app.push_screen(ChatTransfer('demo', importing=importing, agent=agent))
                            await pilot.pause()
                            await shot(action)
                            app.screen.query_one('#chat-transfer-path', Input).value = sample
                            await shot(action + '-selected')
                            app.screen.failed('This sandbox already has chat storage. Restore into a new sandbox before opening its coding agent.'
                                              if importing else 'Choose Actions → Stop sandbox, then try again.')
                            await shot(action + '-error')
                            app.screen.query_one('#chat-transfer-error').update('')
                            app.screen.query_one('#chat-transfer-result').display = True
                            app.screen.completed(4, 0) if importing else app.screen.completed(sample, None)
                            await shot(action + '-complete')
                            await app.pop_screen()
                    if args.only and all(name.startswith('chat-history-') for name in args.only):
                        return
                if prompts_only:
                    await shot('prompt-sandbox-no-library', SandboxPromptScreen('demo', state, True))
                    await shot('prompt-settings-menu', SettingsMenu('demo', c, 'pi'))
                    await app.push_screen(PackagesScreen(initial='system-prompts'))
                    await shot('prompt-library-empty')
                    await app.push_screen(EditPrompt('New add-on', None, lambda value: None))
                    await shot('prompt-new')
                    save = app.screen.query_one('#save-prompt', Button)
                    save.active_effect_duration = 0
                    save.press()
                    await shot('prompt-validation')
                    await app.pop_screen()
                    prompt_library.save('Research briefing', 'Prefer primary sources. Record the date for each source.\n\n'
                                        'Flag uncertainty and cite evidence next to each claim.')
                    key, value = next(iter(prompt_library.entries().items()))
                    await app.pop_screen()
                    await app.push_screen(PackagesScreen(initial='system-prompts'))
                    await shot('prompt-library-populated')
                    await shot('prompt-edit', EditPrompt('Edit add-on', value, lambda value: None))
                    await shot('prompt-remove', Confirm(
                        'Remove “Research briefing” from the library? Existing sandboxes keep their saved copies.',
                        'Remove', confirm_by_default=False, title='Remove add-on'))
                    await app.pop_screen()
                    creation = preset('base', DEFAULT_SETTINGS)
                    creation.update(name='demo', workspace=str(workspace))
                    await app.push_screen(SettingsForm(creation))
                    await pilot.pause()
                    group = app.screen.query_one('#system-prompt-section', Collapsible)
                    group.scroll_visible(animate=False)
                    group.query_one('CollapsibleTitle').focus(scroll_visible=False)
                    await shot('prompt-create-collapsed')
                    group.collapsed = False
                    await pilot.pause()
                    group.scroll_visible(top=True, animate=False)
                    await shot('prompt-create-empty')
                    app.screen.query_one('#system-prompt-choice', Select).value = key
                    await pilot.pause()
                    await shot('prompt-create-selected', focus='#system-prompt-choice')
                    await shot('prompt-create-preview', focus='#system-prompt-preview TextArea')
                    app.screen.query_one('.form', VerticalScroll).scroll_end(animate=False)
                    await shot('prompt-create-bottom')
                    await app.pop_screen()
                    await shot('prompt-sandbox-empty', SandboxPromptScreen('demo', state, True))
                    prompt_library.assign(state, value)
                    await shot('prompt-sandbox-saved', SandboxPromptScreen('demo', state, True))
                    await app.push_screen(SandboxPromptScreen('demo', state, False))
                    await shot('prompt-sandbox-update')
                    await shot('prompt-sandbox-update-edit', focus='#edit-prompt-copy')
                    await app.pop_screen()
                    return
                if args.only and all(name.startswith('embedding-picker-') for name in args.only):
                    from ai_sandbox_factory import embedding_models
                    from test_document_search_ui import embedding_provider_fixture
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='research', workspace='/home/user/projects/research',
                                 model_providers=[embedding_provider_fixture()], hosted_connections={'openrouter': {}})
                    form = SettingsForm(draft, editing=True, setting='document-search')
                    await app.push_screen(form)
                    await pilot.pause()
                    models = {'example': ['example-embedding-v2'],
                              'openrouter': ['openai/text-embedding-3-small', 'openai/text-embedding-3-large']}
                    def catalogue(config, provider, **kwargs):
                        result = models[provider]
                        if isinstance(result, Exception):
                            raise result
                        return result
                    with patch.object(embedding_models, 'catalogue', side_effect=catalogue):
                        await pilot.press('enter')
                        await pilot.pause()
                        dialog = app.screen
                        await shot('embedding-picker-keyword', focus='#knowledge-embedding')
                        dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        provider = dialog.query_one('#embedding-provider', Select)
                        provider.expanded = True
                        await shot('embedding-picker-providers')
                        provider.expanded = False
                        provider.value = 'example'
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        dialog.query_one('#embedding-catalogue', Select).value = 'example-embedding-v2'
                        await shot('embedding-picker-example', focus='#embedding-catalogue')
                        provider.value = 'openrouter'
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        picker = dialog.query_one('#embedding-catalogue', Select)
                        picker.focus()
                        await pilot.press('enter', 'down')
                        await shot('embedding-picker-openrouter-list')
                        picker.expanded = False
                        picker.value = 'openai/text-embedding-3-small'
                        await shot('embedding-picker-openrouter', focus='#embedding-catalogue')
                        await pilot.click('#save-document-search')
                        await pilot.pause()
                        await shot('embedding-picker-saved-draft', focus='#configure-document-search')
                        await pilot.press('enter')
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        dialog = app.screen
                        provider = dialog.query_one('#embedding-provider', Select)
                        provider.value = '@custom'
                        await pilot.pause()
                        url = dialog.query_one('#embedding-url', Input)
                        url.focus()
                        await pilot.pause()
                        url.value = 'models.example.org'
                        await pilot.pause()
                        await shot('embedding-picker-custom-error')
                        dialog.query_one('#embedding-url', Input).value = 'https://models.example.org/v1'
                        dialog.query_one('#embedding-model', Input).value = 'text-embedding'
                        await shot('embedding-picker-custom', focus='#embedding-model')
                        provider.value = 'example'
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        dialog.query_one('#embedding-catalogue', Select).value = 'example-embedding-v2'
                        await pilot.pause()
                        await pilot.click('#save-document-search')
                        await pilot.pause()
                        models['example'] = ValueError('Offline')
                        await pilot.press('enter')
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        dialog = app.screen
                        await shot('embedding-picker-offline', focus='#retry-embedding-catalogue')
                        models['example'] = []
                        dialog.query_one('#retry-embedding-catalogue').focus()
                        await pilot.pause()
                        await pilot.press('enter')
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause(dialog.query_one('#retry-embedding-catalogue', Button).active_effect_duration)
                        await shot('embedding-picker-empty', focus='#retry-embedding-catalogue')
                        models['example'] = ['example-embedding-v2']
                        dialog.query_one('#retry-embedding-catalogue').focus()
                        await pilot.pause()
                        await pilot.press('enter')
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await shot('embedding-picker-restored', focus='#embedding-catalogue')
                        await pilot.press('escape')
                        await app.pop_screen()
                    return
                if research_only:
                    package = root / 'sample-profile'
                    package.mkdir()
                    (package / 'profile.json').write_text('{"name":"example","description":"Run the reference document workflow."}')
                    (package / 'profile.md').write_text('Reference document example.')
                    (package / 'Dockerfile').write_text('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n')
                    registry.add('profile', 'reference-document-review', str(package))
                    await shot('research-profiles', PackagesScreen())
                    await app.push_screen(NewSandbox())
                    await pilot.pause()
                    profile = app.screen.query_one('#profile', Select)
                    profile.focus()
                    profile.expanded = True
                    await shot('research-profile-picker')
                    profile.expanded = False
                    profile.value = 'deep-research'
                    await shot('research-web-selected', focus='#profile')
                    await pilot.click('#new-profile-guide')
                    await shot('research-web-guide', focus='#close-profile-guide')
                    await app.pop_screen()
                    profile.value = 'local-knowledge'
                    await shot('research-local-selected', focus='#profile')
                    await pilot.click('#continue')
                    await shot('research-profile-error', focus='#name')
                    await app.pop_screen()
                    web_profile = sandbox.load_profile('deep-research')
                    web_draft = preset('deep-research', DEFAULT_SETTINGS, web_profile['defaults'])
                    web_draft.update(name='research', workspace=str(workspace))
                    await app.push_screen(SettingsForm(web_draft))
                    await shot('research-web-review')
                    app.screen.query_one('#network-search-section CollapsibleTitle').focus()
                    await shot('research-web-network')
                    app.screen.query_one('#network-search-section', Collapsible).collapsed = False
                    await shot('research-web-provider', focus='#web-mode')
                    await app.pop_screen()
                    metadata = sandbox.load_profile('local-knowledge')
                    draft = preset('local-knowledge', DEFAULT_SETTINGS, metadata['defaults'])
                    draft.update(name='research', workspace='/home/user/projects/research')
                    await app.push_screen(SettingsForm(draft))
                    await shot('research-local-review')
                    app.screen.query_one('#document-search-section CollapsibleTitle').focus()
                    await shot('research-document-search-collapsed')
                    app.screen.query_one('#document-search-section', Collapsible).collapsed = False
                    await pilot.pause()
                    configure = app.screen.query_one('#configure-document-search', Button)
                    configure.focus()
                    await pilot.pause()
                    await shot('research-document-search-entry')
                    app.screen.query_one('#document-search-help', Button).focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    await shot('research-document-search-help')
                    app.screen.query_one(VerticalScroll).scroll_end(animate=False)
                    await pilot.pause()
                    await shot('research-document-search-help-bottom')
                    await pilot.press('escape')
                    configure.focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    await shot('research-pi-knowledge-off', focus='#knowledge-embedding')
                    search_mode = app.screen.query_one('#knowledge-embedding', Select)
                    search_mode.expanded = True
                    await shot('research-pi-knowledge-modes')
                    search_mode.expanded = False
                    search_mode.value = 'endpoint'
                    await pilot.pause()
                    app.screen.query_one('#embedding-url', Input).value = 'models.example.org'
                    await pilot.pause()
                    await shot('research-pi-knowledge-error', focus='#embedding-url')
                    app.screen.query_one('#embedding-url', Input).value = 'https://models.example.org/v1'
                    await pilot.pause()
                    app.screen.query_one('#embedding-model', Input).focus()
                    app.screen.query_one('#embedding-model', Input).value = ''
                    await pilot.press('tab')
                    await pilot.pause()
                    await shot('research-pi-knowledge-model-error', focus='#embedding-model')
                    app.screen.query_one('#embedding-model', Input).value = 'text-embedding'
                    await shot('research-pi-knowledge-on', focus='#knowledge-embedding')
                    await pilot.pause()
                    await pilot.click('#save-document-search')
                    await pilot.pause()
                    saved = app.screen.document_search
                    await app.pop_screen()
                    draft['options'].update(saved)
                    config = {**c, 'options': draft['options']}
                    await app.push_screen(SettingsMenu('research', config, 'browser'))
                    menu = app.screen.query_one('#settings-actions', OptionList)
                    menu.highlighted = next(i for i, (_, key) in enumerate(SettingsMenu.ACTIONS) if key == 'document-search')
                    await shot('research-document-search-menu', focus='#settings-actions')
                    menu.highlighted = menu.option_count - 1
                    await shot('research-document-search-menu-last', focus='#settings-actions')
                    await app.pop_screen()
                    await shot('research-document-search-settings',
                               SettingsForm(draft, editing=True, setting='document-search'), focus='#configure-document-search')
                    await app.push_screen(SettingsForm(draft, editing=True, setting='agent-tools'))
                    await shot('research-document-search-agent-tools', focus='#subagents')
                    await shot('research-document-search-agent-tools-bottom', focus='#scheduling')
                    await app.pop_screen()
                    draft['options'].update(knowledge_embedding='local', knowledge_embedding_url='', knowledge_embedding_model='')
                    await app.push_screen(SettingsForm(draft, editing=True, setting='document-search'))
                    await pilot.pause()
                    await pilot.press('enter')
                    await pilot.pause()
                    await shot('research-pi-knowledge-offline', focus='#knowledge-embedding')
                    await pilot.press('escape')
                    await app.pop_screen()
                    local_config = {**c, 'profile': metadata, 'workspace': '/home/example/projects/research',
                                    'working_directory': '.', 'extra_folders': [
                                        dict(name='reference', source='/home/example/documents', read_only=True)]}
                    await shot('research-local-folders', FoldersScreen({**record, 'config': local_config}))
                    return
                if args.only and all(name.startswith('sandbox-alerts-') for name in args.only):
                    app.receive_inventory([{**record, 'workspace': '/home/user/projects/demo'}])
                    legacy = {**record, 'chat_editor': 'pendant'}
                    await shot('sandbox-alerts-editor', SandboxAlerts(legacy))
                    await shot('sandbox-alerts-two', SandboxAlerts({**legacy, 'ssh_key_state': 'locked'}))
                    await shot('sandbox-alerts-missing-key', SandboxAlerts({**record, 'ssh_key_state': 'missing'}))
                    await shot('sandbox-alerts-unavailable', SandboxAlerts({**record, 'ssh_key_state': 'unavailable'}))
                    return
                if args.only and all(name.startswith('editor-migration-') for name in args.only):
                    common = root / 'legacy-common'
                    common.mkdir()
                    (common / 'Dockerfile').write_text(
                        'ARG PI_VERSION=1.4.0\nARG PENDANT_VERSION=0.94.2\nRUN curl cdervis.vscode-pi-${PENDANT_VERSION}.vsix\n')
                    legacy = {**record, 'chat_editor': 'pendant', 'dev': 'exited',
                              'config': {**c, 'common': {'path': str(common)}}}
                    app.receive_inventory([legacy])
                    await shot('editor-migration-pendant')
                    await shot('editor-migration-actions', MoreActions(legacy))
                    if 'editor-migration-confirm' in args.only:
                        app.perform('rebuild', record={**legacy, 'dev': 'running'})
                        await shot('editor-migration-confirm')
                        await app.pop_screen()
                    app.receive_inventory([record])
                    await shot('editor-migration-frostpi')
                    return
                if args.only and all(name.startswith('containers-') for name in args.only):
                    name = 'fixture-with-a-long-readable-sandbox-id1'
                    config = {**c, 'name': name}
                    prefix = sandbox.names(config)[0]
                    states = {
                        prefix: 'running', prefix + '-proxy': 'running', prefix + '-inference': 'running',
                        prefix + '-ssh': 'exited', prefix + '-advisor': 'running', prefix + '-advisor-proxy': 'running',
                    }
                    owned = {**record, 'name': name, 'config': config, 'containers': sandbox.describe_containers(config, states)}
                    await shot('containers-actions', MoreActions(record))
                    await app.push_screen(ContainersScreen(owned))
                    await shot('containers-populated')
                    listing = app.screen.query_one('#container-list')
                    listing.focus()
                    listing.scroll_end(animate=False)
                    await shot('containers-scrolled')
                    await app.pop_screen()
                    await shot('containers-empty', ContainersScreen({**record, 'containers': []}))
                    await shot('containers-offline', ContainersScreen({**record, 'containers': None}))
                    await shot('containers-invalid', ContainersScreen({**record, 'engine': None}))
                    return
                if args.only and all(name.startswith('providers-') for name in args.only):
                    plugin = {'name': 'example', 'settings': {}, 'manifest': {
                        'description': 'Example Models',
                        'network': ['models.example.test:443'],
                        'settings': {'key': {'type': 'secret', 'label': 'Example Models API key'}},
                        'inference': {'origin': 'https://models.example.test', 'auth': {'type': 'bearer'},
                                      'operations': [{'method': 'GET', 'path': '/v1/models'}]},
                    }}
                    resolve = registry.resolve
                    def choices(kind):
                        return {'local': 'Local models', 'example': 'Example Models'} if kind == 'provider' else {'base': 'Default Developer Sandbox'}
                    with patch('ai_sandbox_factory.skill_library.entries', return_value={}), \
                         patch.object(registry, 'choices', side_effect=choices), \
                         patch.object(registry, 'resolve', side_effect=lambda kind, name:
                                      root if kind == 'provider' else resolve(kind, name)), \
                         patch.object(registry, 'manifest', return_value=plugin['manifest']):
                        draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='demo', workspace=str(workspace), local_model_ports='8000')
                        form = SettingsForm(draft)
                        await app.push_screen(form)
                        await shot('providers-create')
                        await shot('providers-create-local-on', focus='#local-models')
                        form.query_one('#local-models', Checkbox).value = False
                        await pilot.pause()
                        await shot('providers-create-local-off', focus='#local-models')
                        form.query_one('#local-models', Checkbox).value = True
                        form.query_one('#model-ports', Input).value = '8000,not-a-port'
                        await pilot.pause()
                        try:
                            form.checked_local_ports()
                        except ValueError:
                            pass
                        await shot('providers-create-local-error', focus='#model-ports')
                        form.query_one('#model-ports', Input).value = '8000'
                        form.query_one('#local-provider', Collapsible).collapsed = True
                        form.query_one('#source-example', Checkbox).value = True
                        await pilot.pause()
                        await shot('providers-create-plugin', focus='#source-example')
                        await app.pop_screen()
                        defaults = SettingsForm(draft, defaults=True)
                        await app.push_screen(defaults)
                        section = defaults.query_one('#hosted-section', Collapsible)
                        section.query_one('CollapsibleTitle').focus()
                        section.collapsed = False
                        await shot('providers-defaults')
                        await app.pop_screen()
                        existing = {**draft, 'model_providers': [*c['model_providers'], plugin]}
                        await shot('providers-edit', SettingsForm(existing, editing=True, setting='model-sources'))
                        app.service.populated = True
                        live = ModelsScreen({**record, 'config': {**c, 'model_providers': existing['model_providers']}})
                        await app.push_screen(live)
                        await shot('providers-settings')
                        await shot('providers-settings-local', focus='#local-server-keys')
                        live.query_one('#live-local-provider', Collapsible).collapsed = True
                        await shot('providers-settings-plugin', focus='#model-provider')
                        dialog = ProviderDialog(providers={
                            'together': 'Together', 'azure': 'Azure / Foundry', 'amazon-bedrock': 'AWS Bedrock'})
                        await app.push_screen(dialog)
                        await shot('providers-connect')
                        dialog.query_one('#hosted-save', Button).focus()
                        await pilot.press('enter')
                        await pilot.pause(0.3)
                        await shot('providers-connect-error')
                        await app.pop_screen()
                        await app.pop_screen()
                    return
                if args.only and all(name.startswith('local-key-') for name in args.only):
                    ports = [8000, 8080, 8081]
                    binding = provider_credentials.scope('local', inference.local_route(8000))
                    work = 'a' * 32
                    status = {'configured': ['local-8000', 'local-8080'],
                              'assignments': {'local-8000': {'ref': work, 'label': 'Work'}}}
                    with patch.object(provider_credentials, 'entries', side_effect=lambda scope:
                                      {work: {'label': 'Work', 'scope': binding}} if scope == binding else {}):
                        draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='demo', workspace=str(workspace), local_model_ports='8000,8080-8081')
                        await shot('local-key-create', SettingsForm(draft), focus='#local-server-keys')
                        await shot('local-key-settings', ModelsScreen({**record, 'config':{**c, 'local_model_ports':'8000,8080-8081'}}),
                                   focus='#local-server-keys')
                        await shot('local-key-ports', LocalKeysDialog(ports, draft={}), focus='#local-key-port')
                        await shot('local-key-assigned', LocalKeysDialog(ports, status=status), focus='#local-key-choose')
                        if 'local-key-menu' in args.only:
                            await app.push_screen(LocalKeysDialog(ports, status=status))
                            await pilot.pause()
                            app.screen.query_one('#local-key-port', Select).focus()
                            await pilot.press('enter')
                            await shot('local-key-menu')
                            await app.pop_screen()
                        await shot('local-key-selected', LocalKeysDialog(ports, draft={8000: {'ref':work}}), focus='#local-key-choose')
                        await shot('local-key-change', CredentialDialog('Local server · port 8000', binding,
                            choice={'ref': work}), focus='#saved-key')
                        if 'local-key-invalid' in args.only:
                            draft['local_model_ports'] = '8080-8000'
                            await app.push_screen(SettingsForm(draft, section='hosted-section'))
                            await pilot.pause()
                            app.screen.query_one('#local-provider', Collapsible).collapsed = False
                            await pilot.pause()
                            app.screen.query_one('#local-server-keys', Button).focus()
                            await pilot.press('enter')
                            await shot('local-key-invalid')
                            await app.pop_screen()
                    return
                if args.only and all(name.startswith('provider-key-') for name in args.only):
                    hosted = hosted_models.credential_scope({'provider': 'openrouter'})
                    route = {'origin':'https://models.example.test', 'basePath':'/api',
                             'auth':{'type':'bearer'}, 'operations':[{'method':'GET', 'path':'/api/models'}]}
                    company = provider_credentials.scope('company', route)
                    work, personal = 'a'*32, 'b'*32
                    def records(binding):
                        if binding in (hosted, company):
                            return {work:{'label':'Work', 'scope':binding}, personal:{'label':'Personal', 'scope':binding}}
                        return {}
                    manifest = {'description':'Company models', 'inference':route,
                                'settings':{'key':{'type':'secret','label':'Company API key'}}}
                    row = {'name':'company', 'manifest':manifest, 'settings':{}}
                    with patch.object(provider_credentials, 'entries', side_effect=records):
                        await shot('provider-key-hosted', ProviderDialog({
                            'credential':{'key':'synthetic-key', 'label':'Work'}}), focus='#remember-key')
                        await shot('provider-key-hosted-new', ProviderDialog(), focus='#saved-key')
                        await shot('provider-key-plugin', CredentialDialog('Company models', company, active=True),
                                   focus='#saved-key')
                        await shot('provider-key-plugin-saved', CredentialDialog('Company models', company,
                            choice={'ref': work}, active=True), focus='#saved-key')
                        await shot('provider-key-replace', CredentialDialog('Company models', company,
                            choice={'key':'synthetic-key','label':'Work','replace':work}, active=True), focus='#replace-key')
                        if 'provider-key-error' in args.only:
                            await app.push_screen(CredentialDialog('Company models', company))
                            await pilot.pause()
                            app.screen.query_one('#credential-apply').focus()
                            await pilot.press('enter')
                            await pilot.pause(0.3)
                            await shot('provider-key-error')
                            await app.pop_screen()
                        await shot('provider-key-settings',
                            ModelsScreen({**record, 'config':{**c, 'model_providers':[row]}}), focus='#provider-change-key')
                        if 'provider-key-create' in args.only:
                            resolve = registry.resolve
                            with patch.object(registry, 'choices', side_effect=lambda kind:
                                {'company':'Company models'} if kind == 'provider' else {'base':'Generic tools'}), \
                                 patch.object(registry, 'manifest', return_value=manifest), \
                                 patch.object(registry, 'resolve', side_effect=lambda kind,name:
                                    root if kind == 'provider' else resolve(kind,name)):
                                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                                draft.update(name='demo', workspace=str(workspace), model_providers={'company': {}})
                                await shot('provider-key-create', SettingsForm(draft), focus='#source-company-key')
                                draft['_provider_credential_choices'] = {'company': {'ref': work}}
                                await shot('provider-key-create-selected', SettingsForm(draft), focus='#source-company-key')
                    return
                await shot('home')
                if args.only and all(name.startswith('home-filters-') for name in args.only):
                    app.receive_inventory([{**record, 'workspace': '/home/user/projects/demo'}])
                    await shot('home-filters-default')
                    await shot('home-filters-sort-focus', focus='#sort-field')
                    await pilot.click('#sort-field')
                    await shot('home-filters-sort-expanded')
                    await pilot.press('escape')
                    app.query_one('#search', Input).value = 'demo'
                    await shot('home-filters-search', focus='#search')
                    await shot('home-filters-direction', focus='#sort-direction')
                    return
                if args.only and all(name.startswith('home-update-') for name in args.only):
                    app.receive_inventory([{**record, 'workspace': '/home/user/projects/demo'}])
                    with patch('ai_sandbox_factory.updates.pipx_install', return_value=True), \
                            patch.object(host_platform.shutil, 'which', return_value='/usr/bin/pipx'):
                        app.show_update('2099.11.20.153000')
                        await shot('home-update-available', focus='#search')
                        await shot('home-update-focused', focus='#application-update-run')
                        app.confirm_update()
                        await shot('home-update-confirm')
                        await pilot.pause()
                        await pilot.press('escape')
                        with patch.object(app, 'suspend', return_value=nullcontext()), \
                                patch('ai_sandbox_factory.sandbox_tui.subprocess.run',
                                      side_effect=subprocess.CalledProcessError(1, ['pipx', 'upgrade', 'ai-sandbox-factory'],
                                                                              stderr='Could not connect to the repository.')):
                            app.upgrade(True)
                        await shot('home-update-failed', focus='#close-update-details')
                        await pilot.press('escape')
                        await pilot.pause()
                    with patch('ai_sandbox_factory.updates.pipx_install', return_value=False):
                        app.show_update('2099.11.20.153000')
                        await pilot.pause()
                        await shot('home-update-source', focus='#application-update-help')
                        await pilot.click('#application-update-help')
                        await shot('home-update-instructions', focus='#close-update-details')
                    return
                with patch.object(host_platform, 'is_mac', return_value=False), \
                        patch.object(host_platform.shutil, 'which', side_effect=lambda name: '/usr/bin/pipx' if name == 'pipx' else None), \
                        patch('ai_sandbox_factory.updates.pipx_install', return_value=True):
                    app.check_dependencies()
                    app.receive_inventory([{**record, 'dev': 'unknown', 'proxy': 'unknown',
                                            'workspace': '/home/user/projects/demo'}])
                    await shot('home-missing-dependencies')
                    app.show_update('2099.1.1.1')
                    await shot('home-missing-dependencies-update')
                    app.query_one('#application-update').display = False
                if args.only and 'home-missing-dependencies-long-workspace' in args.only:
                    with patch.object(host_platform, 'is_mac', return_value=False), \
                            patch.object(host_platform.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in ('docker', 'pipx') else None), \
                            patch('ai_sandbox_factory.updates.pipx_install', return_value=True):
                        app.check_dependencies()
                        app.show_update('2099.11.18.150000')
                        app.receive_inventory([{**record, 'dev': 'unknown', 'proxy': 'unknown',
                                                'workspace': '/work/' + 'nested-project-directory/' * 9}])
                        await shot('home-missing-dependencies-long-workspace')
                        app.query_one('#application-update').display = False
                app.check_dependencies()
                if args.only and all(name.startswith('home') for name in args.only):
                    return
                app.receive_inventory([])
                await shot('empty')
                app.receive_inventory([record])
                await shot('new', NewSandbox())
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='demo', workspace=str(workspace), working_directory='.', extra_folders=[])
                if args.only and all(name.startswith('settings-summary-') for name in args.only):
                    selected = options({'browser': True, 'goals': False, 'compaction_recall': 'full',
                                        'approval': 'manual', 'inner_sandbox': False,
                                        'web_access': {'enabled': True}, 'communications': {'slack': True}})
                    skills = [{'name': 'sample-skill', 'digest': 'fixture'}]
                    saved = {**c, 'options': selected, 'network': 'unrestricted',
                             'extra_skills': skills,
                             'hosted_connections': {'openrouter': {}}}
                    await shot('settings-summary-default', SettingsMenu('demo', c, 'browser'))
                    await shot('settings-summary-populated', SettingsMenu('demo', saved, 'pi'), focus='#settings-actions')
                    await shot('settings-summary-invalid-config', SettingsMenu('demo', {}, 'browser'))
                    form = {**draft, 'options': selected, 'network': 'unrestricted',
                            'editor': {**draft['editor'], 'mode': 'pi'}, 'extra_skills': skills,
                            '_hosted_changes': {'openrouter': {'provider': 'openrouter'}}}
                    await app.push_screen(SettingsForm(form))
                    app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                    await shot('settings-summary-create')
                    await shot('settings-summary-create-network', section='#network-search-section',
                               focus='#network-search-section > CollapsibleTitle')
                    await app.pop_screen()
                    for context in ('create', 'defaults', 'existing'):
                        screen = SettingsForm({**draft, 'subagents': True,
                                               'options': {**selected, 'question_forms': False, 'scheduling': False}},
                                              editing=context == 'existing', defaults=context == 'defaults',
                                              setting='agent-tools' if context == 'existing' else None)
                        await app.push_screen(screen)
                        prefix = 'settings-summary-features-' + context
                        if context != 'existing':
                            screen.query_one('#hosted-section', Collapsible).collapsed = True
                            await pilot.pause()
                            title = screen.query_one('#skills-section > CollapsibleTitle')
                            title.focus(scroll_visible=False)
                            title.scroll_visible(top=True, animate=False, immediate=True)
                            await shot(prefix + '-collapsed')
                        await shot(prefix + '-expanded', section='#skills-section', focus='#subagents')
                        await shot(prefix + '-scheduling', focus='#scheduling')
                        await app.pop_screen()
                    return

                def network_form(text=None, config=None):
                    value = copy.deepcopy(draft)
                    if config is not None:
                        value['options'] = options(config.get('options'))
                        value['website_groups'] = config.get('website_groups', {})
                        value['network'] = config.get('network', value['network'])
                        value['local_model_ports'] = config.get('local_model_ports', value['local_model_ports'])
                    value['allowlist'] = (state / 'allowlist.txt').read_text() if text is None else text
                    return SettingsForm(value, editing=True, setting='network-settings')

                if args.only and all(name.startswith('curlie-') for name in args.only):
                    from ai_sandbox_factory.sandbox_tui import WebsiteGroups
                    before = {'revision': 'a' * 32, 'downloaded_at': 1795077720}
                    newer = {'revision': 'b' * 32, 'downloaded_at': 1795209420}
                    research = copy.deepcopy(c)
                    research['options']['website_groups'] = ['ut1-press', 'curlie-health', 'curlie-science',
                                                              'curlie-government']

                    async def topics():
                        for selector in ('#network-search-section', '#network-section', 'WebsiteGroups Collapsible'):
                            app.screen.query_one(selector, Collapsible).collapsed = False
                        await pilot.pause()
                        widget = app.screen.query_one('#curlie-status').parent
                        widget.scroll_visible(top=True, animate=False)
                        await pilot.pause()

                    for name, info in [('curlie-first-use', None), ('curlie-saved', newer)]:
                        with patch('ai_sandbox_factory.curlie_catalog.metadata', return_value=info):
                            await app.push_screen(SettingsForm({**draft, 'options': research['options']}))
                            await topics()
                            await shot(name)
                            if info:
                                for part, anchor in [('website', '#website-category-sources'),
                                                     ('topics', '#website-topic-sources')]:
                                    app.screen.query_one(anchor).scroll_visible(top=True, animate=False)
                                    await shot('curlie-sources-' + part)
                                for part, anchor in [('news', '#category-ut1-press'),
                                                     ('end', '#category-curlie-consumer')]:
                                    app.screen.query_one(anchor).focus()
                                    await shot('curlie-create-' + part)
                            await app.pop_screen()
                    research['website_groups'] = {'ut1-press': {'curlie_download': before}}
                    with patch('ai_sandbox_factory.curlie_catalog.metadata', return_value=newer):
                        await app.push_screen(network_form(config=research))
                        await topics()
                        await shot('curlie-existing')
                        update = app.screen.query_one('#use-newer-curlie', Checkbox)
                        update.value = True
                        await shot('curlie-selected', focus='#use-newer-curlie')
                        websites = app.screen.query_one(WebsiteGroups)
                        websites.available_list = before
                        update.value = False
                        websites.show_curlie_status()
                        websites.confirm_curlie_download('about 180 MB')
                        await shot('curlie-confirm')
                        await app.pop_screen()
                        with patch('ai_sandbox_factory.curlie_catalog.refresh', side_effect=ValueError(
                                'The Curlie download failed. The connection closed.')):
                            websites.download_curlie()
                            await app.workers.wait_for_complete()
                        await topics()
                        await shot('curlie-download-error')
                        app.screen.query_one('#allowlist', TextArea).load_text('invalid.example/path')
                        app.screen.query_one('#apply', Button).press()
                        await shot('curlie-network-error')
                        await app.pop_screen()
                    return

                if args.only and all(name.startswith('network-group-') for name in args.only):
                    await shot('network-group-menu', SettingsMenu('demo', c, 'browser'))
                    await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                    app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                    await shot('network-group-create-order')
                    await shot('network-group-create', section='#network-search-section')
                    await shot('network-group-create-network', section='#network-section', focus='#network')
                    app.screen.query_one('#network-section', Collapsible).collapsed = True
                    await shot('network-group-create-search-off', section='#web-section', focus='#web-enabled')
                    app.screen.query_one('#web-enabled', Checkbox).value = True
                    await shot('network-group-create-search', section='#web-section', focus='#web-enabled')
                    await app.pop_screen()
                    await shot('network-group-edit', network_form())
                    await shot('network-group-edit-network', network_form(), '#network-section', '#network')
                    unrestricted = {**c, 'network': 'unrestricted', 'local_model_ports': None}
                    await shot('network-group-edit-unrestricted', network_form(config=unrestricted), '#network-section')
                    searchable = copy.deepcopy(c)
                    searchable['options']['web_access'] = {'enabled': True, 'mode': 'auto', 'providers': {'exa': {}}}
                    await shot('network-group-edit-search', network_form(config=searchable), '#web-section', '#web-mode')
                    await app.push_screen(network_form('https://invalid.example.test/path'))
                    app.screen.query_one('#apply', Button).press()
                    await shot('network-group-rules-error')
                    await app.pop_screen()
                    await app.push_screen(network_form())
                    web = app.screen.query_one('WebAccessForm')
                    await web.provider_saved(('brave', {'endpoint': '', 'model': '', 'has_key': False}, None))
                    app.screen.query_one('#web-mode', Select).value = 'brave'
                    app.screen.query_one('#web-enabled', Checkbox).value = True
                    app.screen.query_one('#apply', Button).press()
                    await shot('network-group-search-error')
                    await app.pop_screen()
                    await app.push_screen(network_form())
                    app.screen.query_one('#allowlist', TextArea).load_text('local-model.internal:8000\nexample.test:443')
                    app.screen.query_one('#web-enabled', Checkbox).value = True
                    with patch('ai_sandbox_factory.sandbox.choose_engine', return_value='podman'):
                        app.screen.query_one('#apply', Button).press()
                        await pilot.pause()
                    await shot('network-group-edit-review')
                    await app.pop_screen()
                    await app.pop_screen()
                    return
                if args.only and all(name.startswith('settings-ux-') for name in args.only):
                    await shot('settings-ux-menu', SettingsMenu('demo', c, 'browser'))
                    await shot('settings-ux-create', SettingsForm(copy.deepcopy(draft)))
                    if 'settings-ux-create-categories' in args.only:
                        await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                        app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                        await shot('settings-ux-create-categories')
                        await app.pop_screen()
                    await shot('settings-ux-create-approvals', SettingsForm(
                        {**draft, 'options': options({'approval': 'manual'})}), '#agent-section', '#inner-sandbox')
                    if args.only and any(name in args.only for name in (
                            'settings-ux-create-approval-help', 'settings-ux-create-approval-help-bottom')):
                        await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                        section = app.screen.query_one('#agent-section', Collapsible)
                        section.collapsed = False
                        await pilot.pause()
                        section.scroll_visible(top=True, animate=False)
                        await pilot.pause()
                        app.screen.query_one('#explain-approval').focus()
                        await pilot.pause()
                        await pilot.press('enter')
                        await shot('settings-ux-create-approval-help')
                        help_box = app.screen.query_one('#approval-help')
                        help_box.focus()
                        help_box.scroll_end(animate=False)
                        await shot('settings-ux-create-approval-help-bottom')
                        await app.pop_screen()
                        await app.pop_screen()
                    await shot('settings-ux-create-network', SettingsForm(copy.deepcopy(draft)), '#network-section')
                    await shot('settings-ux-models', ModelsScreen(record))
                    await shot('settings-ux-editor', EditorScreen('demo'))
                    await shot('settings-ux-network', network_form('local-model.internal:8000', c))
                    await shot('settings-ux-network-unrestricted', network_form('', {**c, 'network': 'unrestricted', 'local_model_ports': None}))
                    await shot('settings-ux-folders', FoldersScreen(record))
                    await shot('settings-ux-ssh', SSHScreen(record))
                    await shot('settings-ux-advisors', AdvisorsScreen(record))
                    settings = {**draft, 'allowlist': 'local-model.internal:8000'}
                    if any(name.startswith('settings-ux-reviewer-') for name in args.only):
                        from ai_sandbox_factory.reviewer_ui import ReviewerPicker
                        catalogue = [
                            dict(provider='chaucer', id='global-deepseek-v4-1-flash', name='DeepSeek'),
                            dict(provider='openrouter', id='openai/gpt-4o-mini', name='GPT-4o mini'),
                        ]
                        app.service.approval_models = lambda name: catalogue
                        for state, ref in [('current', 'current'), ('selected', 'openrouter/openai/gpt-4o-mini'),
                                           ('unavailable', 'openrouter/old-model')]:
                            form = SettingsForm({**settings, 'options': options({'approval_model': ref})},
                                                editing=True, setting='approval-settings')
                            await shot('settings-ux-reviewer-' + state, form, focus='#review-current')
                        await app.push_screen(ReviewerPicker(catalogue, 'openrouter/openai/gpt-4o-mini'))
                        await shot('settings-ux-reviewer-picker')
                        app.screen.query_one(Input).value = 'no matches'
                        await shot('settings-ux-reviewer-search-empty')
                        await app.pop_screen()
                        app.service.plugin_model = lambda name, action, **values: {'models': [
                            dict(id='global-deepseek-v4-1-flash', name='DeepSeek', contextWindow=1000000)]}
                        await shot('settings-ux-reviewer-first-plugin', ModelDialog(
                            'demo', 'chaucer', plugin_label='Chaucer', optional=True, approval_model='current'),
                            focus='#review-current')
                        await shot('settings-ux-reviewer-first-providers', ModelsScreen(record, finished=lambda: None),
                                   focus='#review-current')
                        await shot('settings-ux-reviewer-before-start', SettingsForm(copy.deepcopy(draft)),
                                   '#agent-section', focus='#approval')
                        app.service.approval_models = lambda name: catalogue[:1]
                        await shot('settings-ux-reviewer-one-model', SettingsForm(
                            copy.deepcopy(settings), editing=True, setting='approval-settings'), focus='#approval')
                        if all(name.startswith('settings-ux-reviewer-') for name in args.only):
                            return
                    for action in EDIT_FORMS:
                        await shot('settings-ux-' + action, SettingsForm(copy.deepcopy(settings), editing=True, setting=action))
                    await shot('settings-ux-approval-choice', SettingsForm(
                        {**settings, 'options': options({'approval': 'assisted'})}, editing=True, setting='approval-settings'), focus='#approval')
                    await shot('settings-ux-approval-protection-off', SettingsForm(
                        {**settings, 'options': options({'approval': 'off', 'inner_sandbox': False,
                                                         'protect_credentials': False})},
                        editing=True, setting='approval-settings'), focus='#protect-credentials')
                    if args.only and any(name in args.only for name in (
                            'settings-ux-edit-approval-help', 'settings-ux-edit-approval-help-bottom')):
                        await app.push_screen(SettingsForm(
                            {**settings, 'options': options({'approval': 'assisted'})}, editing=True, setting='approval-settings'))
                        await pilot.click('#explain-approval')
                        await shot('settings-ux-edit-approval-help')
                        help_box = app.screen.query_one('#approval-help')
                        help_box.focus()
                        help_box.scroll_end(animate=False)
                        await shot('settings-ux-edit-approval-help-bottom')
                        await app.pop_screen()
                        await app.pop_screen()
                    if 'settings-ux-model-error' in args.only:
                        form = SettingsForm(copy.deepcopy(settings), editing=True, setting='model-sources')
                        await app.push_screen(form)
                        form.query_one('#model-ports', Input).value = '8000,bad-port'
                        await pilot.pause()
                        await pilot.click('#apply')
                        await shot('settings-ux-model-error')
                        await app.pop_screen()
                    await shot('settings-ux-created', CreationOperation(
                        root / 'new-demo', lambda log: log('Sandbox ready.'), Creation(), retry=lambda: None,
                        on_success=None, success_message=settings_effects.open_client('browser')))
                    await shot('settings-ux-updated', Operation(
                        'Updating demo', lambda log: log('Sandbox ready.'), done_on_success=True,
                        completed_title='Updated demo', success_message=settings_effects.REOPEN))
                    return
                if args.only and any(name.startswith('model-settings-') for name in args.only):
                    for context in ('create', 'defaults', 'existing'):
                        sample = copy.deepcopy(draft)
                        if context == 'create':
                            sample['_hosted_changes'] = {'openrouter': {'provider': 'openrouter'}}
                        screen = SettingsForm(sample, editing=context == 'existing', defaults=context == 'defaults')
                        await app.push_screen(screen)
                        await shot('model-settings-' + context + '-collapsed')
                        section = screen.query_one('#hosted-section', Collapsible)
                        section.collapsed = False
                        await pilot.pause()
                        section.scroll_visible(top=True, animate=False)
                        screen.query_one('#model-ports', Input).value = '8000,8080-8082,9000-9010,10000-10010'
                        await shot('model-settings-' + context + '-on', focus='#model-ports')
                        screen.query_one('#local-models', Checkbox).value = False
                        await shot('model-settings-' + context + '-off', focus='#local-models')
                        screen.query_one('#local-models', Checkbox).value = True
                        screen.query_one('#model-ports', Input).value = '8000,not-a-port'
                        await pilot.pause()
                        try:
                            screen.collect()
                        except ValueError:
                            pass
                        await shot('model-settings-' + context + '-error', focus='#model-ports')
                        if context != 'existing':
                            section.collapsed = True
                            screen.query_one('#editor-section', Collapsible).collapsed = False
                            screen.query_one('#editor-section').scroll_visible(top=True, animate=False)
                            await shot('model-settings-' + context + '-open-with', focus='#editor-mode')
                        for part in ('browser', 'goals', 'compaction-recall'):
                            screen.query_one('#skills-section', Collapsible).collapsed = False
                            await shot('model-settings-' + context + '-tools-' + part, focus='#' + part)
                        goals_off = 'model-settings-' + context + '-tools-goals-off'
                        if args.only and goals_off in args.only:
                            screen.query_one('#goals', Checkbox).value = False
                            await shot(goals_off, focus='#goals')
                        await app.pop_screen()
                    await shot('model-settings-open-with', EditorScreen('demo'))
                    await shot('model-settings-menu', SettingsMenu('demo', c, 'browser'))
                    await shot('model-settings-saved-models', ModelsScreen(record))
                    app.service.populated = True
                    await shot('model-settings-connected-models', ModelsScreen(record))
                    app.service.populated = False
                    return
                if not args.only or any(name.startswith('git-') for name in args.only):
                    for context in ('create', 'defaults', 'ssh'):
                        if context == 'ssh':
                            screen = SSHScreen(record)
                        elif context == 'create':
                            screen = CreateSSHDialog(copy.deepcopy(draft), 'SSH & Git')
                        else:
                            screen = SettingsForm(copy.deepcopy(draft), defaults=True)
                        await app.push_screen(screen)
                        screen.query_one('#git-identity-section', Collapsible).scroll_visible(top=True, animate=False)
                        await shot('git-' + context + '-collapsed')
                        section = screen.query_one('#git-identity-section', Collapsible)
                        section.collapsed = False
                        await pilot.pause()
                        section.scroll_visible(top=True, animate=False)
                        await shot('git-' + context + '-disabled', focus='#git-identity-enabled')
                        screen.query_one('#git-identity-enabled', Checkbox).value = True
                        screen.query_one('#git-author-name', Input).value = 'Example Developer with a longer display name'
                        screen.query_one('#git-author-email', Input).value = 'developer.with.a.long.address@example.test'
                        await pilot.pause()
                        await shot('git-' + context + '-populated', focus='#git-author-email')
                        screen.query_one('#git-author-email', Input).value = 'developer.' + 'very-long-address.' * 12 + '@example.test'
                        screen.query_one('#git-author-email', Input).cursor_position = len(screen.query_one('#git-author-email', Input).value)
                        await shot('git-' + context + '-overflow', focus='#git-author-email')
                        screen.query_one('#git-author-email', Input).value = 'invalid address'
                        await pilot.pause()
                        try:
                            screen.query_one(GitIdentityFields).collect()
                        except ValueError:
                            pass
                        screen.query_one('#git-identity-error').scroll_visible(animate=False)
                        await shot('git-' + context + '-error')
                        await app.pop_screen()
                if not args.only or any(name.startswith('provider-credential') for name in args.only):
                    info = {'description': 'Company models', 'settings': {
                        'endpoint': {'type': 'text', 'label': 'API endpoint', 'default': 'https://models.example.com/api'},
                        'key': {'type': 'secret', 'label': 'Company API key'}},
                        'inference': {'origin': 'https://models.example.com', 'auth': {'type': 'bearer'}}}
                    resolve = registry.resolve
                    with patch.object(registry, 'choices', side_effect=lambda kind: {'base': 'Generic tools'} if kind == 'profile' else {'company': 'Company models'}), \
                         patch.object(registry, 'manifest', return_value=info), \
                         patch.object(registry, 'resolve', side_effect=lambda kind, name: root if kind == 'provider' else resolve(kind, name)):
                        await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                        form = app.screen
                        section = form.query_one('#hosted-section', Collapsible)
                        section.collapsed = False
                        await pilot.pause()
                        section.scroll_visible(top=True, animate=False)
                        form.query_one('#source-company', Checkbox).focus()
                        await shot('provider-credential-disabled')
                        form.query_one('#source-company', Checkbox).value = True
                        await pilot.pause()
                        field = form.query_one('#source-company-key', Input)
                        field.focus(); field.scroll_visible(animate=False)
                        await shot('provider-credential-empty')
                        field.value = 'synthetic-credential-for-visual-review'
                        await pilot.pause()
                        field.cursor_position = len(field.value)
                        await shot('provider-credential-populated')
                        field.value = 'synthetic\0credential'
                        await pilot.pause()
                        try:
                            form.collect()
                        except ValueError:
                            pass
                        await shot('provider-credential-error')
                        await app.pop_screen()
                await shot('review', SettingsForm(copy.deepcopy(draft)))
                enabled = copy.deepcopy(draft)
                enabled['options']['web_access']['enabled'] = True
                await shot('web-enabled', SettingsForm(enabled), 'WebAccessForm Collapsible')
                for name, sel in [('permissions', '#agent-section'), ('web', 'WebAccessForm Collapsible'), ('skills', '#skills-section'), ('network', '#network-section'), ('advanced', 'Collapsible')]:
                    if name == 'advanced':
                        continue
                    await shot(name, SettingsForm(copy.deepcopy(draft)), sel)
                if not args.only or any(name.startswith('browser-vault-') for name in args.only):
                    for browser, protected, inner, name in (
                        (False, True, True, 'disabled'), (True, True, True, 'protected'),
                        (True, False, True, 'unprotected'), (True, True, False, 'outer-shell'),
                        (True, True, False, 'unreviewed-shell')):
                        vault_draft = copy.deepcopy(draft)
                        vault_draft['options'].update(browser=browser, protect_browser_credentials=protected, inner_sandbox=inner)
                        if name == 'unreviewed-shell':
                            vault_draft['options']['approval'] = 'off'
                        await app.push_screen(SettingsForm(vault_draft))
                        app.screen.query_one('#skills-section', Collapsible).collapsed = False
                        await shot('browser-vault-' + name, section='#skills-section',
                                   focus='#protect-browser-credentials' if browser else '#browser')
                        if size[0] == 80:
                            app.screen.query_one('#browser-vault-shell-help').scroll_visible(top=True, animate=False)
                            await shot('browser-vault-' + name + '-help')
                        await app.pop_screen()
                questions_off = copy.deepcopy(draft)
                questions_off['options']['question_forms'] = False
                await shot('questions-off', SettingsForm(questions_off), '#skills-section', '#question-forms')
                credentials_off = copy.deepcopy(draft)
                credentials_off['options']['protect_credentials'] = False
                await shot('credentials-unprotected', SettingsForm(credentials_off), '#agent-section', '#protect-credentials')
                await shot('categories', network_form('local-model.internal:8000', c), 'WebsiteGroups Collapsible')
                if not args.only or any(name.startswith('auth-') for name in args.only):
                    auth = copy.deepcopy(c)
                    auth['options']['website_groups'] = ['auth-microsoft']
                    await shot('auth-selected', network_form('app.example.com:443', auth), 'WebsiteGroups Collapsible', '#category-auth-microsoft')
                    await app.push_screen(network_form('https://invalid.example.com/path', auth))
                    app.screen.query_one('#apply', Button).press()
                    await shot('auth-error')
                    await app.pop_screen()
                    auth_draft = copy.deepcopy(draft)
                    auth_draft['options']['website_groups'] = ['auth-microsoft']
                    await app.push_screen(SettingsForm(auth_draft))
                    app.screen.query_one('#network-search-section', Collapsible).collapsed = False
                    app.screen.query_one('#network-section', Collapsible).collapsed = False
                    app.screen.query_one('WebsiteGroups Collapsible').collapsed = False
                    await pilot.pause()
                    app.screen.query_one('#category-auth-microsoft').scroll_visible(top=True, animate=False)
                    await shot('auth-create', focus='#category-auth-microsoft')
                    await app.pop_screen()
                await shot('network-edit', network_form('local-model.internal:8000', c))
                await shot('folders', FoldersScreen(record))
                await shot('folder-add', FolderEditor(None, [], str(workspace), root))
                await shot('settings', SettingsMenu('demo', c, 'browser'))
                await shot('actions', MoreActions(record))
                await shot('models', ModelsScreen(record))
                await shot('packages', PackagesScreen())
                await shot('advisors', AdvisorsScreen(record))
                await shot('ssh', SSHScreen(record))
                await shot('copy', CreateFromSandbox([record]))
                await shot('search-provider', SearchProviderDialog(web_access.settings(None)['providers'], 'exa'))
                await shot('confirm', Confirm('Apply settings? This recreates the sandbox and interrupts running processes.', 'Apply and recreate', confirm_by_default=True))
                await shot('folder-picker', FolderPicker(str(workspace)))
                await shot('install-profile', InstallPackage('profile'))
                await shot('install-provider', InstallPackage('provider'))
                await shot('editor', EditorScreen('demo'))
                await shot('editor-connection', EditorScreen('demo'), '#editor-connection')
                await shot('open-with', OpenWith())
                await shot('provider-openrouter', ProviderDialog())
                await shot('provider-together', ProviderDialog({'provider': 'together'}))
                await shot('provider-azure', ProviderDialog({'provider': 'azure'}))
                await shot('provider-bedrock', ProviderDialog({'provider': 'amazon-bedrock', 'method': 'iam'}))
                await shot('model-select', ModelDialog('demo', 'azure'))
                await shot('slack', SlackCredentials({}, {}))
                await shot('copy-web', CopyWebAccess([record]))
                await shot('repository', RepositorySetup())
                await shot('public-key', PublicKey('demo'))
                await shot('operation', Operation('Creating sandbox', lambda log: log('Sandbox ready.')))
                if not args.only or any(name.startswith('update-operation-') for name in args.only):
                    for phase in ('preparing', 'confirm', 'applying', 'cancelling', 'cancelled', 'complete', 'failed'):
                        screen = UpdateOperation('Updating example-project', lambda log, update: None,
                                                 done_on_success=True, completed_title='Updated example-project')
                        with patch.object(Operation, 'execute'):
                            await app.push_screen(screen)
                        for index in range(24):
                            screen.write_log(f'STEP {index + 1}/24: building the updated image and checking cached dependencies')
                        if phase == 'confirm':
                            screen.action_cancel()
                        elif phase == 'applying':
                            with patch.object(type(screen.update), 'applying', True):
                                screen.check_phase()
                        elif phase in ('cancelling', 'cancelled'):
                            screen.confirm_cancel(True)
                        if phase in ('cancelled', 'complete', 'failed'):
                            screen.cancelled = phase == 'cancelled'
                            screen.failed = phase == 'failed'
                            if screen.failed:
                                screen.write_log('Error: The container engine could not build the replacement image.')
                            if phase == 'complete':
                                screen.write_log('Finished.')
                            screen.complete()
                        if app.screen is screen and not screen.query_one('#operation-back', Button).disabled:
                            screen.query_one('#operation-back', Button).focus()
                        await shot('update-operation-' + phase)
                        if phase == 'confirm':
                            await app.pop_screen()
                        await app.pop_screen()
                if not args.only or any(name.startswith('creation-') for name in args.only):
                    for state_name in ('running', 'cancelling', 'cancelled', 'cleanup-error', 'complete', 'failed'):
                        controller = Creation()
                        name = 'example-project-with-a-long-name'
                        screen = CreationOperation(root / name, lambda log: None, controller,
                                                   retry=lambda: None, on_success=None)
                        with patch.object(Operation, 'execute'):
                            await app.push_screen(screen)
                        output = screen.query_one(RichLog)
                        for index in range(35):
                            output.write(f'STEP {index + 1}/39: preparing sandbox dependencies and cached image layers')
                        if state_name in ('cancelling', 'cancelled', 'cleanup-error'):
                            screen.action_cancel()
                            output.write('Stopping creation…')
                        if state_name in ('cancelled', 'cleanup-error', 'complete', 'failed'):
                            screen.failed = state_name in ('cleanup-error', 'failed')
                            screen.cancelled = state_name == 'cancelled'
                            if state_name == 'failed':
                                screen.result = 'Podman was not found. Install rootless Podman.'
                            elif state_name == 'cleanup-error':
                                screen.result = ('Creation stopped, but cleanup failed. Saved state was retained. '
                                                 'Choose Actions… → Delete sandbox… from the sandbox list. Container engine unavailable.')
                            elif state_name == 'cancelled':
                                screen.result = ('Creation cancelled. Runtime containers and private state removed; '
                                                 'project files and build cache preserved.')
                            else:
                                screen.result = 'Sandbox ready.'
                            screen.write_log(('Error: ' if screen.failed else '') + screen.result)
                            screen.complete()
                        focus = '#operation-retry' if state_name == 'failed' else '#operation-back'
                        await shot('creation-' + state_name, focus=focus if state_name != 'cancelling' else None)
                        await app.pop_screen()
                    if args.only and all(name.startswith(('creation-', 'home')) for name in args.only):
                        return

                if not args.only or any(name.startswith('advisor-choice-') for name in args.only):
                    from ai_sandbox_factory.advisors_ui import AdvisorModelHelp
                    for entry, form in [('setup', SettingsForm(copy.deepcopy(draft))), ('settings', AdvisorsScreen(record))]:
                        await app.push_screen(form)
                        if entry == 'setup':
                            form.query_one('#new-advisors-section', Collapsible).collapsed = False
                        button = form.query_one('.advisor-model-help', Button)
                        button.focus()
                        await pilot.pause()
                        await shot('advisor-choice-' + entry)
                        await pilot.press('enter')
                        await pilot.pause()
                        assert isinstance(app.screen, AdvisorModelHelp)
                        await shot('advisor-choice-help', focus='#advisor-help-close')
                        close = app.screen.query_one('#advisor-help-close').region
                        body = app.screen.query_one('VerticalScroll')
                        assert app.screen.region.contains_region(close)
                        assert app.screen.query_one('#confirm-box').content_region.contains_region(close)
                        assert body.max_scroll_y == 0, (size, body.region, body.virtual_size)
                        await pilot.press('escape' if entry == 'setup' else 'enter')
                        await pilot.pause()
                        assert app.screen is form
                        await app.pop_screen()
                    if args.only and all(name.startswith('advisor-choice-') for name in args.only):
                        return
                for label, sel in [('editor', '#editor-section'), ('providers', '#hosted-section'), ('folders', '#folders-section'), ('advisors', '#new-advisors-section'), ('agent', '#agent-section'), ('communications', '#communications-section')]:
                    await shot('review-' + label, SettingsForm(copy.deepcopy(draft)), sel)
                if args.only and 'review-error' in args.only:
                    await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                    error = app.screen.query_one('#form-error', Static)
                    error.update('Allowlist line 1: use an exact hostname:port')
                    error.scroll_visible(animate=False)
                    await shot('review-error', focus='#apply')
                    await app.pop_screen()
                await app.push_screen(SSHScreen(record))
                for i, section in enumerate(app.screen.query(Collapsible)):
                    section.collapsed = False
                    await pilot.pause()
                    section.scroll_visible(top=True, animate=False)
                    await shot('ssh-section-' + str(i))
                await app.pop_screen()
                await shot('defaults', SettingsForm(copy.deepcopy(draft), defaults=True))
                await shot('configuration-edit', SettingsForm(copy.deepcopy(draft), editing=True))
                await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                section = list(app.screen.query(Collapsible))[-1]
                section.collapsed = False
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('advanced')
                await app.pop_screen()
                advisor_record = copy.deepcopy(record)
                advisor_record['config']['advisors'] = dict(providers=['codex','claude','agy'], claude_model='sonnet', agy_model='example-high', agy_quota_group='Example', agy_effort='high')
                await app.push_screen(AdvisorsScreen(advisor_record))
                for i, section in enumerate(app.screen.query(Collapsible)):
                    section.collapsed = False
                    await pilot.pause()
                    section.scroll_visible(top=True, animate=False)
                    await shot('advisor-section-' + str(i))
                await app.pop_screen()
                if args.only and any(name.startswith('advisor-model-') for name in args.only):
                    only_claude = copy.deepcopy(record)
                    only_claude['config']['advisors'] = {'providers': ['claude']}
                    await app.push_screen(AdvisorsScreen(only_claude))
                    await shot('advisor-model-default', section='#advisor-claude-options', focus='#advisor-claude-model')
                    app.screen.query_one('#advisor-claude-model', Input).value = 'bad model id'
                    app.screen.query_one('#advisor-apply', Button).press()
                    await pilot.pause()
                    await shot('advisor-model-error', focus='#advisor-claude-model')
                    await app.pop_screen()
                    only_agy = copy.deepcopy(record)
                    only_agy['config']['advisors'] = {'providers': ['agy']}
                    await shot('advisor-model-unrestricted', AdvisorsScreen(only_agy), section='#advisor-agy-options', focus='#advisor-model')
                await app.push_screen(EditorScreen('demo'))
                app.screen.query_one('#editor-connection', Collapsible).collapsed = False
                app.screen.query_one('#editor-auth', Checkbox).value = True
                app.screen.query_one('#editor-address', Input).value = '192.0.2.10'
                app.screen.query_one('#editor-password', Input).focus()
                await shot('editor-password')
                app.screen.query_one('#editor-private-key', Input).focus()
                await shot('editor-tls')
                await app.pop_screen()
                await app.push_screen(network_form('example.com:443', c))
                app.screen.query_one('#network-section', Collapsible).collapsed = False
                section = app.screen.query_one('#network-rules-fields').query(Collapsible)[-1]
                section.collapsed = False
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('network-import')
                await app.pop_screen()
                for name, screen, selector, text in [('folder-error', FolderEditor(None, [], str(workspace), root), '#folder-error', 'This folder overlaps private sandbox state. Choose another directory, such as a project or reference folder outside the Factory state directory.'), ('repository-error', RepositorySetup(), '#repository-error', 'Enter an HTTPS GitHub or GitLab project URL.'), ('provider-error', ProviderDialog(), '#hosted-error', 'Enter an API key.'), ('slack-error', SlackCredentials({}, {}), '#slack-error', 'Enter a Slack bot token beginning with xoxb-.'), ('search-error', SearchProviderDialog({'exa': {'has_key': True}}, 'exa'), '#search-provider-error', 'Choose another search mode before removing this provider.'), ('install-error', InstallPackage('profile'), '#package-error', registry.NAME_ERROR + ' The names base and local are reserved.')]:
                    await app.push_screen(screen)
                    screen.query_one(selector, Static).update(text)
                    await pilot.pause()
                    screen.query_one(selector).scroll_visible(animate=False)
                    await shot(name)
                    await app.pop_screen()
                await app.push_screen(RepositorySetup())
                app.screen.query_one('#repository-url', Input).value = 'https://github.com/example/project'
                await shot('repository-filled')
                await app.pop_screen()
                await app.push_screen(FolderEditor(dict(source='/home/user/Documents/customer-projects/reference-documents/technical-manuals', name='reference', read_only=True), [], str(workspace), root))
                await shot('folder-filled')
                await app.pop_screen()

                def fail(log):
                    raise ValueError('Unable to start the sandbox. Check the container engine and retry.')
                await shot('operation-error', Operation('Starting sandbox', fail))
                app.service.populated = True
                await shot('models-configured', ModelsScreen(record))
                await app.push_screen(ModelDialog('demo', 'azure'))
                await pilot.pause()
                await app.workers.wait_for_complete()
                app.screen.query_one('#model-results', OptionList).highlighted = 0
                await shot('model-selected')
                app.screen.query_one('#model-deployment', Input).focus()
                await pilot.pause()
                app.screen.query_one('#model-test').scroll_visible(animate=False)
                await shot('model-actions')
                await app.pop_screen()
                await shot('search-openai', SearchProviderDialog({'openai': {'endpoint': 'https://api.openai.com/v1/responses', 'has_key': True}}, 'openai'))
                await shot('search-searxng', SearchProviderDialog({'searxng': {'endpoint': 'https://search.example.com'}}, 'searxng'))
                await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                section = app.screen.query_one('#new-advisors-section', Collapsible)
                section.collapsed = False
                for provider in ('codex', 'claude', 'agy'):
                    app.screen.query_one('#new-advisor-' + provider, Checkbox).value = True
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('advisors-selected')
                await app.pop_screen()
                await app.push_screen(SettingsForm(copy.deepcopy(enabled)))
                app.screen.query_one('#network-search-section', Collapsible).collapsed = False
                section = app.screen.query_one('#web-section', Collapsible)
                section.collapsed = False
                await pilot.pause()
                web = app.screen.query_one('WebAccessForm')
                web.providers['openai'] = {'endpoint': 'https://api.openai.com/v1/responses', 'has_key': True, 'model': ''}
                await web.refresh_providers()
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('web-multiple')
                app.screen.query_one('#web-save-defaults').focus()
                app.screen.query_one('#web-save-defaults').scroll_visible(animate=False)
                await shot('web-actions')
                await app.pop_screen()
                await app.push_screen(InstallPackage('profile'))
                section = app.screen.query_one(Collapsible)
                section.collapsed = False
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('install-subfolder')
                await app.pop_screen()
                await app.push_screen(NewSandbox())
                app.screen.query_one('#profile', Select).value = 'custom'
                app.screen.query_one('#custom-profile', Input).focus()
                await shot('new-custom-profile')
                await app.pop_screen()

async def main():
    for size in [tuple(map(int, value.split('x'))) for value in args.size] if args.size else [(80, 24), (120, 40), (220, 50)]:
        await capture(size)
asyncio.run(main())
(out / 'coverage.json').write_text(json.dumps(coverage, indent=2) + '\n')
