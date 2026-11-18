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
import asyncio, copy, json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
from test_tui import FakeService
from test_hosted_models_ui import ModelService
from pi_sandbox_factory import sandbox, registry, host_platform
from pi_sandbox_factory.creation import Creation
from pi_sandbox_factory.settings_catalog import EDIT_FORMS
from pi_sandbox_factory.sandbox_tui import Cockpit, options, preset, DEFAULT_SETTINGS, NewSandbox, SettingsForm, NetworkForm, FoldersScreen, SettingsMenu, MoreActions, ModelsScreen, PackagesScreen, AdvisorsScreen, SSHScreen, CreateFromSandbox, SearchProviderDialog, Confirm, FolderPicker, InstallPackage, CopyWebAccess, Operation, CreationOperation, settings_effects, web_access
from textual.widgets import Collapsible, Input, Static, OptionList, Select, RichLog
from pi_sandbox_factory.tui_widgets import Checkbox
from pi_sandbox_factory.folders_ui import FolderEditor
from pi_sandbox_factory.editor_ui import EditorScreen, OpenWith
from pi_sandbox_factory.models_ui import ProviderDialog, ModelDialog
from pi_sandbox_factory.communications_ui import SlackCredentials
from pi_sandbox_factory.repository_ui import RepositorySetup, PublicKey, GitIdentityFields

class ReviewService(FakeService):

    def ssh_key(self, *args):
        return 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE3uK0K5OO6n4efS1W21F9JbLM5Ko34YTK95svh9jMfN pi-sandbox-factory'
    populated = False

    def hosted_request(self, *args, **kw):
        return ModelService.hosted_request(self, *args, **kw) if self.populated else {'providers': [], 'models': []}
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
        with patch('pi_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), patch('pi_sandbox_factory.sandbox_tui.search_keys_path', return_value=root / 'keys.json'), patch('pi_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(root, ReviewService([record]))
            async with app.run_test(size=size) as pilot:
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
                        s.collapsed = False
                        await pilot.pause()
                        s.scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    if focus:
                        app.screen.query_one(focus).focus()
                        await pilot.pause()
                    p = out / f'{name}-{size[0]}x{size[1]}.svg'
                    p.write_text(app.export_screenshot())
                    coverage.append(dict(file=p.name, screen=type(app.screen).__name__, size=size))
                    print(p.name, flush=True)
                    if screen:
                        await app.pop_screen()
                await shot('home')
                with patch.object(host_platform, 'is_mac', return_value=False), \
                        patch.object(host_platform.shutil, 'which', side_effect=lambda name: '/usr/bin/pipx' if name == 'pipx' else None), \
                        patch('pi_sandbox_factory.updates.pipx_install', return_value=True):
                    app.check_dependencies()
                    app.receive_inventory([{**record, 'dev': 'unknown', 'proxy': 'unknown'}])
                    await shot('home-missing-dependencies')
                    app.show_update('2099.1.1.1')
                    await shot('home-missing-dependencies-update')
                    app.query_one('#application-update').display = False
                if args.only and 'home-missing-dependencies-long-workspace' in args.only:
                    with patch.object(host_platform, 'is_mac', return_value=False), \
                            patch.object(host_platform.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in ('docker', 'pipx') else None), \
                            patch('pi_sandbox_factory.updates.pipx_install', return_value=True):
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
                if args.only and all(name.startswith('settings-ux-') for name in args.only):
                    await shot('settings-ux-menu', SettingsMenu('demo'))
                    await shot('settings-ux-create', SettingsForm(copy.deepcopy(draft)))
                    if 'settings-ux-create-categories' in args.only:
                        await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                        app.screen.query_one('#hosted-section', Collapsible).collapsed = True
                        await shot('settings-ux-create-categories')
                        await app.pop_screen()
                    await shot('settings-ux-create-approvals', SettingsForm(copy.deepcopy(draft)), '#agent-section')
                    await shot('settings-ux-create-network', SettingsForm(copy.deepcopy(draft)), '#network-section')
                    await shot('settings-ux-models', ModelsScreen(record))
                    await shot('settings-ux-editor', EditorScreen('demo'))
                    await shot('settings-ux-network', NetworkForm('demo', 'local-model.internal:8000', c))
                    await shot('settings-ux-network-unrestricted', NetworkForm('demo', '', {**c, 'network': 'unrestricted'}))
                    await shot('settings-ux-folders', FoldersScreen(record))
                    await shot('settings-ux-ssh', SSHScreen(record))
                    await shot('settings-ux-advisors', AdvisorsScreen(record))
                    settings = {**draft, 'allowlist': 'local-model.internal:8000'}
                    for action in EDIT_FORMS:
                        await shot('settings-ux-' + action, SettingsForm(copy.deepcopy(settings), editing=True, setting=action))
                    await shot('settings-ux-approval-choice', SettingsForm(
                        {**settings, 'options': options({'approval': 'assisted'})}, editing=True, setting='approval-settings'))
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
                        await app.pop_screen()
                    await shot('model-settings-open-with', EditorScreen('demo'))
                    await shot('model-settings-menu', SettingsMenu('demo'))
                    await shot('model-settings-saved-models', ModelsScreen(record))
                    app.service.populated = True
                    await shot('model-settings-connected-models', ModelsScreen(record))
                    app.service.populated = False
                    return
                if not args.only or any(name.startswith('git-') for name in args.only):
                    for context in ('create', 'defaults', 'ssh'):
                        screen = SSHScreen(record) if context == 'ssh' else SettingsForm(copy.deepcopy(draft), defaults=context == 'defaults')
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
                await shot('categories', NetworkForm('demo', 'local-model.internal:8000', c), 'WebsiteGroups Collapsible')
                if not args.only or any(name.startswith('auth-') for name in args.only):
                    auth = copy.deepcopy(c)
                    auth['options']['website_groups'] = ['auth-microsoft']
                    await shot('auth-selected', NetworkForm('demo', 'app.example.com:443', auth), 'WebsiteGroups Collapsible', '#category-auth-microsoft')
                    await app.push_screen(NetworkForm('demo', 'https://invalid.example.com/path', auth))
                    app.screen.query_one('#network-error', Static).update('Use hostname:port, for example app.example.com:443.')
                    await pilot.pause()
                    app.screen.query_one('#network-error').scroll_visible(animate=False)
                    await shot('auth-error', focus='#apply-network')
                    await app.pop_screen()
                    auth_draft = copy.deepcopy(draft)
                    auth_draft['options']['website_groups'] = ['auth-microsoft']
                    await app.push_screen(SettingsForm(auth_draft))
                    app.screen.query_one('#network-section', Collapsible).collapsed = False
                    app.screen.query_one('WebsiteGroups Collapsible').collapsed = False
                    await pilot.pause()
                    app.screen.query_one('#category-auth-microsoft').scroll_visible(top=True, animate=False)
                    await shot('auth-create', focus='#category-auth-microsoft')
                    await app.pop_screen()
                await shot('network-edit', NetworkForm('demo', 'local-model.internal:8000', c))
                await shot('folders', FoldersScreen(record))
                await shot('folder-add', FolderEditor(None, [], str(workspace), root))
                await shot('settings', SettingsMenu('demo'))
                await shot('actions', MoreActions(record))
                await shot('models', ModelsScreen(record))
                await shot('packages', PackagesScreen())
                await shot('advisors', AdvisorsScreen(record))
                await shot('ssh', SSHScreen(record))
                await shot('copy', CreateFromSandbox([record]))
                await shot('search-provider', SearchProviderDialog(web_access.settings(None)['providers'], 'exa'))
                await shot('confirm', Confirm('Apply settings? This recreates the sandbox and interrupts running processes.', 'Apply and recreate'))
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
                            screen.action_cancel_creation()
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

                for label, sel in [('editor', '#editor-section'), ('providers', '#hosted-section'), ('folders', '#folders-section'), ('advisors', '#new-advisors-section'), ('agent', '#agent-section'), ('communications', '#communications-section')]:
                    await shot('review-' + label, SettingsForm(copy.deepcopy(draft)), sel)
                if args.only and 'review-error' in args.only:
                    await app.push_screen(SettingsForm(copy.deepcopy(draft)))
                    error = app.screen.query_one('#form-error', Static)
                    error.update('Add an allowed destination or enable host-local models.')
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
                await app.push_screen(AdvisorsScreen(record))
                for i, section in enumerate(app.screen.query(Collapsible)):
                    section.collapsed = False
                    await pilot.pause()
                    section.scroll_visible(top=True, animate=False)
                    await shot('advisor-section-' + str(i))
                await app.pop_screen()
                await app.push_screen(EditorScreen('demo'))
                app.screen.query_one('#editor-connection', Collapsible).collapsed = False
                app.screen.query_one('#editor-auth', Checkbox).value = True
                app.screen.query_one('#editor-address', Input).value = '192.0.2.10'
                app.screen.query_one('#editor-password', Input).focus()
                await shot('editor-password')
                app.screen.query_one('#editor-private-key', Input).focus()
                await shot('editor-tls')
                await app.pop_screen()
                await app.push_screen(NetworkForm('demo', 'example.com:443', c))
                section = list(app.screen.query(Collapsible))[-1]
                section.collapsed = False
                await pilot.pause()
                section.scroll_visible(top=True, animate=False)
                await shot('network-import')
                await app.pop_screen()
                for name, screen, selector, text in [('folder-error', FolderEditor(None, [], str(workspace), root), '#folder-error', 'This folder overlaps private sandbox state. Choose another directory, such as a project or reference folder outside the Factory state directory.'), ('repository-error', RepositorySetup(), '#repository-error', 'Enter an HTTPS GitHub or GitLab project URL.'), ('provider-error', ProviderDialog(), '#hosted-error', 'Enter an API key.'), ('slack-error', SlackCredentials({}, {}), '#slack-error', 'Enter a Slack bot token beginning with xoxb-.'), ('search-error', SearchProviderDialog({'exa': {'has_key': True}}, 'exa'), '#search-provider-error', 'Choose another search mode before removing this provider.'), ('install-error', InstallPackage('profile'), '#package-error', 'Use a local name of 1–40 lowercase letters, digits or hyphens, starting with a letter.')]:
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
