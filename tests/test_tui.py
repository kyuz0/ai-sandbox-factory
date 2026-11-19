import asyncio
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pi_sandbox_factory import sandbox
from pi_sandbox_factory.sandbox_tui import Cockpit, Confirm, NewSandbox, SettingsForm, Operation, UpdateOperation, DEFAULT_SETTINGS, Service, SettingsMenu
from textual.widgets import Input, Button, Collapsible, DataTable, Checkbox, TextArea, OptionList

class FakeService:
    def __init__(self,records): self.records=records;self.calls=[]
    def inventory(self):return copy.deepcopy(self.records)
    def config(self, name):
        return copy.deepcopy(next(record['config'] for record in self.records if record['name'] == name))
    def start(self,name,log):self.calls.append(['start',name]);log('ready')
    def run(self,args,log):
        self.calls.append(args);log('done')
        if args[0]=='delete':self.records=[r for r in self.records if r['name']!=args[1]]
    def create(self,draft,key,log,cancellation=None):self.calls.append(['create',draft]);log('created')
    def update(self,name,draft,key,log,update=None):self.calls.append(['update',name,draft]);log('updated')
    def provider(self,name,provider,mode,key,log):self.calls.append(['provider',name,provider,mode,key]);log('Provider ready')
    def models(self,name,mode,log):self.calls.append(['models',name,mode]);log('Model catalogue')

class TUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.runner=asyncio.Runner()
    @classmethod
    def tearDownClass(cls):cls.runner.close()
    def _callTestMethod(self,method):self.runner.run(method())
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.search_keys=patch('pi_sandbox_factory.sandbox_tui.search_keys_path', return_value=self.root/'web-access-keys.json');self.search_keys.start()
        self.defaults=patch('pi_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS));self.defaults.start()
        self.update_check=patch('pi_sandbox_factory.updates.available_update',return_value=None);self.update_check.start()
        self.records=[]
        for name in ['alpha','beta']:
            state=self.root/name;state.mkdir();(state/'allowlist.txt').write_text('local-model.internal:8000\n')
            c=dict(name=name,engine='podman',network='allowlist',workspace=str(self.root/'files'/name),
                   profile=sandbox.load_profile('base'),local_model_ports='8000',memory='8g',subagents=False)
            self.records.append(dict(name=name,profile='base',engine='podman',dev='running',proxy='running',workspace=c['workspace'],config=c))
    def tearDown(self):self.update_check.stop();self.defaults.stop();self.search_keys.stop();self.tmp.cleanup()
    async def ready(self,app,pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def settings_page(self, app, pilot, action="approval-settings"):
        await pilot.click('#settings');await pilot.pause()
        menu=app.screen.query_one('#settings-actions',OptionList)
        menu.highlighted=next(i for i, (_, key) in enumerate(SettingsMenu.ACTIONS) if key == action);menu.focus()
        await pilot.press('enter');await pilot.pause()

    async def delete_menu(self, app, pilot):
        await pilot.click('#more');await pilot.pause()
        menu=app.screen.query_one('#more-actions',OptionList)
        menu.highlighted=next(i for i, (_, key) in enumerate(app.screen.actions) if key == 'delete');menu.focus()
        await pilot.press('enter');await pilot.pause()

    async def test_banner_and_update_notice_fit_small_and_large_terminals(self):
        for size in ((80,24),(120,40)):
            app=Cockpit(self.root,FakeService(self.records))
            async with app.run_test(size=size) as pilot:
                await self.ready(app,pilot)
                with patch('pi_sandbox_factory.updates.pipx_install',return_value=True), patch('pi_sandbox_factory.sandbox_tui.shutil.which',return_value='/bin/pipx'):
                    app.show_update('2099.1.1.1')
                await pilot.pause()
                title=app.query_one('#title-banner')
                if size[1] < 32:
                    self.assertFalse(title.display)
                    self.assertEqual(app.screen.sub_title, f'v{app.version}')
                else:
                    self.assertEqual(title.content_region.height, 9)
                    self.assertEqual(title.styles.padding.top, 1)
                self.assertFalse(app.query_one('#application-update-run',Button).disabled)
                self.assertLess(app.query_one('#new').region.bottom,size[1])
                self.assertLess(app.query_one('#more').region.bottom,size[1])
                self.assertGreaterEqual(app.query_one('#sandboxes').region.height,5)
                self.assertIn('pipx upgrade pi-sandbox-factory',str(app.query_one('#application-update-message').render()))
                self.assertTrue(await pilot.click('#settings'))
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__,'SettingsMenu')

    async def test_update_requires_confirmation_and_reports_failure(self):
        from contextlib import nullcontext
        import subprocess
        app=Cockpit(self.root,FakeService([]))
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app,pilot)
            with patch('pi_sandbox_factory.updates.pipx_install',return_value=True), patch('pi_sandbox_factory.sandbox_tui.shutil.which',return_value='/bin/pipx'):
                app.show_update('2099.1.1.1')
            await pilot.pause()
            with patch.object(app,'suspend',return_value=nullcontext()), patch('pi_sandbox_factory.sandbox_tui.subprocess.run',side_effect=subprocess.CalledProcessError(1,['pipx'])) as run:
                await pilot.click('#application-update-run');await pilot.pause()
                self.assertIsInstance(app.screen,Confirm)
                await pilot.click('#cancel');await pilot.pause()
                run.assert_not_called()
                await pilot.click('#application-update-run');await pilot.pause()
                await pilot.click('#yes');await pilot.pause()
                run.assert_called_once_with(('pipx','upgrade','pi-sandbox-factory'),check=True)
                self.assertIn('Upgrade failed',str(app.query_one('#application-update-message').render()))
                self.assertFalse(app.query_one('#application-update-run',Button).disabled)

    async def test_startup_reports_missing_dependencies_and_refresh_clears_notice(self):
        from pi_sandbox_factory import host_platform
        records = copy.deepcopy(self.records)
        records[0]['workspace'] = '/work/' + 'nested-project-directory/' * 9
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(self.root, FakeService(records))
            with patch.object(host_platform, 'is_mac', return_value=False), \
                    patch.object(host_platform.shutil, 'which', side_effect=lambda name: '/usr/bin/docker' if name == 'docker' else None) as which:
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    warning = app.query_one('#startup-dependencies')
                    self.assertTrue(warning.display)
                    self.assertIn('Install rootless Podman', str(warning.render()))
                    self.assertIn('Install Git', str(warning.render()))
                    self.assertLessEqual(app.query_one('#more').region.bottom, size[1] - 1)
                    self.assertTrue(await pilot.click('#settings'))
                    await pilot.press('escape')
                    app.show_update('2099.1.1.1')
                    await pilot.pause()
                    self.assertTrue(await pilot.click('#settings'))
                    await pilot.press('escape')
                    self.assertTrue(await pilot.click('#new'))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, NewSandbox)
                    await pilot.press('escape')
                    which.side_effect = None
                    which.return_value = '/usr/bin/dependency'
                    app.action_refresh_inventory()
                    await self.ready(app, pilot)
                    self.assertFalse(warning.display)

    async def test_successful_update_relaunches_and_source_install_disables_button(self):
        from contextlib import nullcontext
        from pi_sandbox_factory import updates
        app=Cockpit(self.root,FakeService([]))
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app,pilot)
            with patch('pi_sandbox_factory.updates.pipx_install',return_value=False):
                app.show_update('2099.1.1.1')
            self.assertTrue(app.query_one('#application-update-run',Button).disabled)
            with patch.object(app,'suspend',return_value=nullcontext()), patch('pi_sandbox_factory.sandbox_tui.subprocess.run'), patch.object(app,'exit') as finish:
                app.upgrade(True)
                finish.assert_called_once_with(result=updates.RELAUNCH)

    async def test_new_shortcut_works_while_search_has_focus(self):
        for records in ([], self.records):
            with self.subTest(empty=not records):
                # Search includes workspace paths; random temporary names can contain 'n'.
                records = copy.deepcopy(records)
                for record in records:
                    record['workspace'] = '/work/' + record['name']
                app = Cockpit(self.root, FakeService(records))
                async with app.run_test(size=(80,24)) as pilot:
                    await self.ready(app, pilot)
                    search = app.query_one('#search', Input)
                    search.focus()
                    await pilot.press('n')
                    self.assertEqual(search.value, 'n')
                    details = str(app.query_one('#details').render())
                    self.assertIn('F2', details)
                    self.assertIn('No matching' if records else 'No sandboxes yet', details)
                    await pilot.press('f2')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, NewSandbox)
                    screen = app.screen
                    await pilot.press('f2')
                    self.assertIs(app.screen, screen)
                    await pilot.press('escape')
                    await pilot.pause()
                    self.assertEqual(app.query_one('#search', Input).value, 'n')
                    await pilot.click('#new')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, NewSandbox)

    async def test_advisors_live_in_settings_and_allow_initial_setup(self):
        from pi_sandbox_factory.sandbox_tui import AdvisorsScreen
        service=FakeService(self.records)
        service.advisors=lambda name,settings,log,update=None:service.calls.append(['advisors',name,settings])
        app=Cockpit(self.root,service)
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app,pilot)
            await self.settings_page(app,pilot,action="advisors")
            self.assertIsInstance(app.screen,AdvisorsScreen)
            self.assertTrue(app.screen.query_one('#advisor-login-codex',Button).disabled)
            self.assertLess(app.screen.query_one('#advisor-login-codex',Button).region.bottom, 34)
            self.assertIn('only after the advisor has been added', str(app.screen.query_one('#advisor-login-help').render()))
            app.screen.query_one('#advisor-codex',Checkbox).value=True
            await pilot.pause()
            self.assertTrue(app.screen.query_one('#advisor-login-codex',Button).disabled)
            app.screen.query_one('#advisor-codex',Checkbox).value=False
            app.screen.query_one('#advisor-agy',Checkbox).value=True
            await pilot.click('#advisor-apply');await pilot.pause()
            self.assertIsInstance(app.screen,Confirm)
            await pilot.click('#yes');await self.ready(app,pilot)
            self.assertEqual(service.calls[0][0:2],['advisors','alpha'])
            self.assertEqual(service.calls[0][2]['providers'],['agy'])
            self.assertEqual(service.calls[0][2]['agy_model'],'')
            await pilot.press('q')

    async def test_advisor_worker_can_start_after_settings_screen_closes(self):
        from pi_sandbox_factory.sandbox_tui import AdvisorsScreen
        service = FakeService(self.records)
        service.advisors = lambda name, settings, log, update=None: service.calls.append(['advisors', name])
        app = Cockpit(self.root, service)
        # Hold the worker until Textual has removed the settings screen.
        with patch.object(Operation, 'on_mount', return_value=None):
            async with app.run_test(size=(100, 36)) as pilot:
                await self.ready(app, pilot)
                screen = AdvisorsScreen(self.records[0])
                await app.push_screen(screen)
                await pilot.pause()
                screen.query_one('#advisor-codex', Checkbox).value = True
                await pilot.click('#advisor-apply')
                await pilot.pause()
                await pilot.click('#yes')
                await self.ready(app, pilot)
                self.assertIsNone(screen.parent)
                operation = app.screen
                self.assertIsInstance(operation, Operation)
                operation.execute()
                await self.ready(app, pilot)
                self.assertFalse(operation.failed)
                self.assertEqual(service.calls, [['advisors', 'alpha']])

    async def test_models_without_provider_plugins(self):
        from pi_sandbox_factory.sandbox_tui import ModelsScreen
        self.records[0]['config']['model_providers'] = []
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(ModelsScreen(self.records[0]))
            await pilot.pause()
            self.assertFalse(app.screen.query('#model-provider'))
            self.assertTrue(app.screen.query_one('#no-model-providers').display)
            self.assertEqual(str(app.screen.query_one('#model-sources', Button).label), 'Local models & provider plugins…')
            self.assertEqual(service.calls, [])
            await pilot.press('escape')
            self.assertNotIsInstance(app.screen, ModelsScreen)

    async def test_models_refresh_selected_provider(self):
        from pi_sandbox_factory.sandbox_tui import ModelsScreen
        from textual.widgets import Select
        self.records[0]['config']['model_providers'] = [
            {'name': 'local', 'manifest': {'description': 'Local models'}},
            {'name': 'company', 'manifest': {'description': 'Company models'}}]
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(ModelsScreen(self.records[0]))
            await pilot.pause()
            self.assertEqual(app.screen.query_one('#model-provider', Select).value, 'company')
            local = app.screen.query_one('#live-local-provider', Collapsible)
            local.query_one('CollapsibleTitle').focus()
            await pilot.press('enter')
            await pilot.pause()
            await pilot.click('#local-refresh')
            await self.ready(app, pilot)
            self.assertEqual(service.calls, [['provider', 'alpha', 'local', 'refresh', None]])

    async def test_saved_codex_advisor_enables_login(self):
        self.records[0]['config']['advisors'] = {'providers': ['codex']}
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app, pilot)
            await self.settings_page(app, pilot, action="advisors")
            self.assertFalse(app.screen.query_one('#advisor-login-codex', Button).disabled)
            self.assertTrue(app.screen.query_one('#advisor-login-agy', Button).disabled)
            self.assertTrue(app.screen.query_one('#advisor-login-claude', Button).disabled)
            await pilot.press('q')

    async def test_claude_login_and_settings_at_small_terminal(self):
        from textual.widgets import Select
        self.records[0]['config']['advisors'] = {'providers':['claude']}
        service=FakeService(self.records)
        service.advisors=lambda name,settings,log,update=None:service.calls.append(['advisors',name,settings])
        app=Cockpit(self.root,service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            await self.settings_page(app,pilot,action="advisors")
            self.assertFalse(app.screen.query_one('#advisor-login-claude',Button).disabled)
            self.assertTrue(app.screen.query_one('#advisor-login-codex',Button).disabled)
            app.screen.query_one('#advisor-claude-model',Input).value='claude-sonnet-test'
            app.screen.query_one('#advisor-claude-effort',Select).value='high'
            await pilot.click('#advisor-apply');await pilot.pause()
            await pilot.click('#yes');await self.ready(app,pilot)
            self.assertEqual(service.calls[0][2]['providers'],['claude'])
            self.assertEqual(service.calls[0][2]['claude_model'],'claude-sonnet-test')
            self.assertEqual(service.calls[0][2]['claude_effort'],'high')
            await pilot.press('q')

    async def test_repository_setup_and_key_display(self):
        from pi_sandbox_factory.sandbox_tui import SSHScreen
        from pi_sandbox_factory.repository_ui import RepositorySetup, PublicKey
        from textual.widgets import Collapsible
        for size in ((80, 24), (120, 40), (220, 50)):
            service = FakeService(self.records)
            app = Cockpit(self.root, service)
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(SSHScreen(self.records[0]))
                self.assertTrue(app.screen.query_one('#ssh-generate-key', Button).disabled)
                await app.push_screen(RepositorySetup(), app.screen.add_repository)
                await pilot.pause()
                app.screen.query_one('#repository-url', Input).value = 'https://github.com/team/project'
                await pilot.pause()
                button = app.screen.query_one('#repository-add', Button)
                self.assertGreaterEqual(button.region.x, 0)
                self.assertLess(button.region.bottom, size[1])
                await pilot.click('#repository-add');await pilot.pause()
                self.assertEqual(app.screen.query_one('#ssh-hosts', TextArea).text, 'repository git github.com 22')
                # Existing aliases must not silently switch hosts.
                app.screen.add_repository({'line': 'repository git gitlab.com 22'})
                self.assertEqual(app.screen.query_one('#ssh-hosts', TextArea).text, 'repository git github.com 22')
                with patch.object(service, 'ssh_key', create=True, return_value='ssh-ed25519 AAAA fixture'):
                    await app.push_screen(PublicKey('alpha', generate=True))
                    await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertEqual(app.screen.query_one('#key-public', TextArea).text, 'ssh-ed25519 AAAA fixture')
                    self.assertFalse(app.screen.query_one('#key-copy', Button).disabled)
                    self.assertLess(app.screen.query_one('#key-copy').region.bottom, size[1])
                    with patch.object(app, 'copy_to_clipboard') as copy_key:
                        await pilot.click('#key-copy')
                        copy_key.assert_called_once_with('ssh-ed25519 AAAA fixture')
                    await pilot.press('escape')
                with patch.object(service, 'ssh_key', create=True, side_effect=ValueError('Unlock first')):
                    await app.push_screen(PublicKey('alpha'))
                    await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertTrue(app.screen.query_one('#key-copy', Button).disabled)
                    self.assertIn('Unlock first', str(app.screen.query_one('#key-status').render()))
                    await pilot.press('escape')
                await pilot.press('escape')

    async def test_ssh_settings_keep_key_import_separate(self):
        from pi_sandbox_factory.sandbox_tui import SSHScreen
        service=FakeService(self.records);app=Cockpit(self.root,service)
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app,pilot);await self.settings_page(app,pilot,action="ssh-settings")
            self.assertIsInstance(app.screen,SSHScreen)
            self.assertTrue(app.screen.query_one('#ssh-import-key',Button).disabled)
            self.assertEqual(app.screen.query_one('#ssh-key-file',Input).value,'')
            await pilot.press('escape');await pilot.pause()
            self.assertFalse(service.calls)
            await pilot.press('q')

    async def test_ssh_unlock_and_unchanged_apply_leave_the_sandbox_running(self):
        from pi_sandbox_factory.sandbox_tui import SSHScreen
        self.records[0]['config']['ssh'] = {'hosts': [{
            'alias': 'repository', 'hostname': 'git.example.test', 'user': 'git', 'port': 22,
            'host_keys': ['ssh-ed25519 AAAA'],
        }]}
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(SSHScreen(self.records[0]))
            unlock = app.screen.query_one('#ssh-unlock', Button)
            self.assertFalse(unlock.disabled)
            with (patch.object(app, 'suspend', return_value=nullcontext()),
                  patch.object(service, 'command', create=True, side_effect=lambda *args: list(args)),
                  patch('pi_sandbox_factory.sandbox_tui.subprocess.run',
                        side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]) as run,
                  patch.object(app.screen, 'notify') as notify):
                field = app.screen.query_one('#ssh-duration', Input)
                self.assertEqual(field.value, '8h')
                field.value = '0h'
                unlock.press()
                await pilot.pause()
                run.assert_not_called()
                self.assertIn('positive unlock duration', str(app.screen.query_one('#ssh-duration-error').render()))
                field.value = '2d'
                unlock.press()
                await pilot.pause()
                run.assert_called_once_with(['ssh-unlock', 'alpha', '--duration', '2d'], check=False)
                feedback = app.screen.query_one('#ssh-key-feedback')
                self.assertEqual(str(feedback.render()), 'SSH key unlocked for 2d. Git can use it now.')
                self.assertTrue(feedback.display)
                unlock.press()
                await pilot.pause()
                self.assertTrue(feedback.has_class('error'))
                self.assertIn('Could not unlock the SSH key', str(feedback.render()))
                app.screen.query_one('#ssh-apply', Button).press()
                await pilot.pause()
                notify.assert_called_with('No SSH or Git settings changed.')
                self.assertIsInstance(app.screen, SSHScreen)
                self.assertEqual(service.calls, [])

    async def test_git_identity_is_optional_validated_and_saved_with_ssh_settings(self):
        from pi_sandbox_factory.sandbox_tui import SSHScreen, preset
        from pi_sandbox_factory.repository_ui import GitIdentityFields
        from textual.widgets import Collapsible
        for size in ((80, 24), (120, 40), (220, 50)):
            service = FakeService(self.records)
            service.ssh_settings = lambda name, settings, log, identity, update=None: service.calls.append(['ssh-git', identity])
            app = Cockpit(self.root, service)
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(SSHScreen(self.records[0]))
                section = app.screen.query_one('#git-identity-section', Collapsible)
                section.collapsed = False
                await pilot.pause()
                self.assertTrue(app.screen.query_one('#git-author-name', Input).disabled)
                app.screen.query_one('#git-identity-enabled', Checkbox).value = True
                await pilot.pause()
                fields = app.screen.query_one(GitIdentityFields)
                self.assertFalse(app.screen.query_one('#git-author-name', Input).disabled)
                section.collapsed = True
                with self.assertRaises(ValueError):
                    fields.collect()
                self.assertFalse(section.collapsed)
                self.assertTrue(app.screen.query_one('#git-identity-error').display)
                app.screen.query_one('#git-author-name', Input).value = 'Fixture Author'
                app.screen.query_one('#git-author-email', Input).value = 'fixture@example.test'
                self.assertEqual(fields.collect(), {'name': 'Fixture Author', 'email': 'fixture@example.test'})
                self.assertLess(app.screen.query_one('#ssh-apply', Button).region.bottom, size[1])
                await pilot.click('#ssh-apply')
                self.assertIsInstance(app.screen, Confirm)
                await pilot.click('#yes'); await self.ready(app, pilot)
                self.assertEqual(service.calls, [['ssh-git', {'name': 'Fixture Author', 'email': 'fixture@example.test'}]])
                await app.pop_screen()
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='demo', workspace=str(self.root), options={**draft['options'], 'git_identity': {'name': 'Fixture Author', 'email': 'fixture@example.test'}})
                await app.push_screen(SettingsForm(draft))
                self.assertEqual(app.screen.query_one(GitIdentityFields).collect()['name'], 'Fixture Author')
                self.assertEqual(app.screen.collect()[0]['options']['git_identity']['email'], 'fixture@example.test')
                app.screen.query_one('#git-identity-enabled', Checkbox).value = False
                await pilot.pause()
                self.assertIsNone(app.screen.collect()[0]['options']['git_identity'])

    async def test_browser_vault_option_follows_browser_selection_and_is_saved(self):
        from pi_sandbox_factory.sandbox_tui import preset
        from textual.widgets import Collapsible, Select, Static
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(self.root, FakeService(self.records))
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='demo', workspace=str(self.root))
                await app.push_screen(SettingsForm(draft))
                app.screen.query_one('#skills-section', Collapsible).collapsed = False
                await pilot.pause()
                protection = app.screen.query_one('#protect-browser-credentials', Checkbox)
                self.assertTrue(protection.value)
                self.assertTrue(protection.disabled)
                app.screen.query_one('#browser', Checkbox).value = True
                await pilot.pause()
                self.assertFalse(protection.disabled)
                self.assertTrue(app.screen.collect()[0]['options']['protect_browser_credentials'])
                protection.value = False
                await pilot.pause()
                self.assertFalse(app.screen.collect()[0]['options']['protect_browser_credentials'])
                app.screen.query_one('#inner-sandbox', Checkbox).value = False
                app.screen.query_one('#approval', Select).value = 'off'
                await pilot.pause()
                self.assertIn('Bash can read vault files', str(app.screen.query_one('#browser-vault-shell-help', Static).render()))
                app.screen.query_one('#browser', Checkbox).value = False
                await pilot.pause()
                self.assertTrue(protection.disabled)
                self.assertFalse(protection.value)

    async def test_selection_delete_cancel_and_preserved_other_entry(self):
        service=FakeService(self.records);app=Cockpit(self.root,service)
        async with app.run_test(size=(100,32)) as pilot:
            await self.ready(app,pilot)
            table=app.query_one(DataTable);self.assertEqual(table.row_count,2)
            table.move_cursor(row=1);await pilot.pause()
            await self.delete_menu(app,pilot)
            self.assertIsInstance(app.screen,Confirm);self.assertIn('beta',app.screen.message)
            self.assertIn('workspace',app.screen.message)
            await pilot.click('#cancel');await pilot.pause();self.assertFalse(service.calls)
            await self.delete_menu(app,pilot);await pilot.click('#yes');await self.ready(app,pilot)
            self.assertEqual(service.calls,[['delete','beta']])
            await pilot.click('#operation-back');await self.ready(app,pilot)
            self.assertEqual([r['name'] for r in app.records],['alpha'])
            await pilot.press('q')

    async def test_create_from_is_visible_and_copies_settings_without_credentials(self):
        from pi_sandbox_factory.sandbox_tui import CreateFromSandbox, WebAccessForm
        from textual.widgets import Select
        record = self.records[0]
        record['config'].update(
            memory='16g', subagents=True, env_file='/private/keys.env', pi_config='/private/pi',
            options={'browser': True, 'goals': False, 'website_groups': ['dev-python'],
                     'web_access': {'enabled': True, 'providers': {'exa': {'has_key': True}}}},
            advisors={'providers': ['claude'], 'claude_model': 'opus', 'claude_effort': 'high'})
        original = copy.deepcopy(record)
        service = FakeService(self.records)
        service.search_keys = lambda name: self.fail('Keys must require explicit selection')
        workspace = self.root / 'new-project'
        workspace.mkdir()
        app = Cockpit(self.root, service)
        with patch('pi_sandbox_factory.web_access.saved_keys', return_value={'exa': 'workstation-key'}) as defaults:
            async with app.run_test(size=(80,24)) as pilot:
                await self.ready(app,pilot)
                button = app.query_one('#create-from', Button)
                self.assertFalse(button.disabled)
                self.assertLessEqual(button.region.right,80)
                self.assertTrue(await pilot.click('#create-from'))
                await pilot.pause()
                self.assertIsInstance(app.screen,CreateFromSandbox)
                self.assertEqual(app.screen.query_one('#copy-source',Select).value,'alpha')
                self.assertFalse(app.screen.query_one('#copy-keys',Checkbox).value)
                self.assertTrue(await pilot.click('#copy-continue'))
                await pilot.pause()
                self.assertIsInstance(app.screen,NewSandbox)
                self.assertEqual(app.screen.query_one('#workspace',Input).value,'')
                self.assertEqual(app.screen.query_one('#name',Input).value,'')
                app.screen.query_one('#workspace',Input).value=str(workspace)
                await pilot.pause()
                app.screen.query_one('#name',Input).value='copied-agent'
                await pilot.click('#continue'); await pilot.pause()
                self.assertIsInstance(app.screen,SettingsForm, str(app.screen.query_one('#new-error').render()) if isinstance(app.screen,NewSandbox) else '')
                self.assertEqual(app.screen.query_one(WebAccessForm).keys,{})
                defaults.assert_not_called()
                self.assertTrue(app.screen.query_one('#browser',Checkbox).value)
                self.assertEqual(app.screen.query_one('#env-file',Input).value,'')
                self.assertEqual(app.screen.query_one('#pi-config',Input).value,'')
                with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                    await pilot.click('#apply'); await self.ready(app,pilot)
                draft = service.calls[0][1]
                self.assertEqual(draft['reuse_from'],'alpha')
                self.assertEqual(draft['name'],'copied-agent')
                self.assertEqual(draft['memory'],'16g')
                self.assertEqual(draft['advisors']['claude_model'],'opus')
                self.assertEqual(draft['options']['website_groups'],['dev-python'])
                self.assertEqual(record,original)

    async def test_create_from_copies_keys_only_when_selected_and_handles_stopped_source(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm
        from textual.widgets import Select
        self.records[0]['config']['options']={'web_access':{'enabled':True,'providers':{'exa':{'has_key':True}}}}
        self.records[1]['dev']='exited'
        service=FakeService(self.records)
        from unittest.mock import Mock
        service.search_keys=Mock(return_value={'exa':{'endpoint':'','key':'synthetic'}})
        app=Cockpit(self.root,service)
        async with app.run_test(size=(100,36)) as pilot:
            await self.ready(app,pilot)
            await pilot.click('#create-from');await pilot.pause()
            app.screen.query_one('#copy-source',Select).value='beta';await pilot.pause()
            self.assertTrue(app.screen.query_one('#copy-keys',Checkbox).disabled)
            app.screen.query_one('#copy-source',Select).value='alpha';await pilot.pause()
            app.screen.query_one('#copy-keys',Checkbox).value=True
            await pilot.click('#copy-continue');await pilot.pause()
            service.search_keys.assert_called_once_with('alpha')
            self.assertEqual(app.screen.search_keys,{'exa':'synthetic'})
            await pilot.press('escape');await pilot.pause()
            self.assertFalse(service.calls)

    async def test_create_from_disabled_for_empty_inventory(self):
        app=Cockpit(self.root,FakeService([]))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            self.assertTrue(app.query_one('#create-from',Button).disabled)

    async def test_create_uses_user_name_workspace_and_ports(self):
        service=FakeService([]);app=Cockpit(self.root,service)
        workspace=self.root/'my-files';workspace.mkdir()
        async with app.run_test(size=(100,42)) as pilot:
            await self.ready(app,pilot);await pilot.click('#new');await pilot.pause()
            self.assertIsInstance(app.screen,NewSandbox)
            app.screen.query_one('#workspace',Input).value=str(workspace);await pilot.pause()
            app.screen.query_one('#name',Input).value='my-agent'
            await pilot.click('#continue');await pilot.pause()
            self.assertIsInstance(app.screen,SettingsForm)
            self.assertFalse(app.screen.query('#pip-preset, #npm-preset, #github-preset'))
            for category in ('dev-python', 'dev-npmjs', 'dev-github'):
                app.screen.query_one('#category-'+category,Checkbox).value=True
            app.screen.query_one('#model-ports',Input).value='8002-8003'
            app.screen.query_one('#new-advisor-codex',Checkbox).value=True
            app.screen.query_one('#new-advisor-agy',Checkbox).value=True
            app.screen.query_one('#new-advisor-claude',Checkbox).value=True
            with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                await pilot.click('#apply');await self.ready(app,pilot)
            self.assertIsInstance(app.screen,Operation)
            draft=service.calls[0][1]
            self.assertEqual(draft['name'],'my-agent');self.assertEqual(draft['workspace'],str(workspace))
            self.assertEqual(draft['allowlist'],'local-model.internal:8002\nlocal-model.internal:8003')
            self.assertEqual(draft['local_model_ports'],'8002-8003')
            self.assertEqual(draft['options']['website_groups'],['dev-python','dev-npmjs','dev-github'])
            self.assertEqual(draft['advisors']['providers'], ['codex', 'claude', 'agy'])
            await pilot.press('q')

    async def test_creation_failure_preserves_configuration_and_can_retry(self):
        from textual.widgets import Footer, RichLog
        service = FakeService([])
        attempts = []
        def create(draft, key, log, cancellation):
            attempts.append((copy.deepcopy(draft), key, cancellation))
            if len(attempts) < 3:
                raise ValueError('Podman was not found. Install rootless Podman.')
        service.create = create
        app = Cockpit(self.root, service)
        workspace = self.root / 'project'
        workspace.mkdir()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.click('#new')
            await pilot.pause()
            new = app.screen
            new.query_one('#workspace', Input).value = str(workspace)
            await pilot.pause()
            new.query_one('#name', Input).value = 'retry-fixture'
            await pilot.click('#continue')
            await pilot.pause()
            form = app.screen
            self.assertIsInstance(form, SettingsForm)
            form.query_one('#model-ports', Input).value = '8002-8003'
            form.query_one('#open-editor', Checkbox).value = False

            await pilot.click('#apply')
            await self.ready(app, pilot)
            operation = app.screen
            self.assertTrue(operation.failed)
            self.assertIn('Install rootless Podman',
                          '\n'.join(line.text for line in operation.query_one(RichLog).lines))
            back = operation.query_one('#operation-back', Button)
            retry = operation.query_one('#operation-retry', Button)
            self.assertEqual(str(back.label), 'Back to configuration')
            self.assertEqual(str(retry.label), 'Retry creation')
            self.assertLessEqual(retry.region.bottom, operation.query_one(Footer).region.y)
            self.assertEqual(retry.region.right, operation.query_one('.wizard-actions').content_region.right)
            await pilot.press('escape')
            self.assertIs(app.screen, form)
            self.assertEqual(form.query_one('#model-ports', Input).value, '8002-8003')
            self.assertEqual(new.query_one('#workspace', Input).value, str(workspace))
            form.query_one('#model-ports', Input).value = '8004'

            await pilot.click('#apply')
            await self.ready(app, pilot)
            self.assertTrue(await pilot.click('#operation-retry'))
            await self.ready(app, pilot)
            self.assertEqual(attempts[1][:2], attempts[2][:2])
            self.assertEqual(attempts[2][0]['local_model_ports'], '8004')
            self.assertIsNot(attempts[1][2], attempts[2][2])
            self.assertFalse(app.screen.failed)
            self.assertEqual(str(app.screen.query_one('#operation-back', Button).label), 'Done')
            self.assertEqual(app.screen.active_bindings['escape'].binding.description, 'Done')
            self.assertFalse(app.screen.query_one('#operation-retry', Button).display)
            await pilot.click('#operation-back')
            await self.ready(app, pilot)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_settings_and_models_follow_selection(self):
        service=FakeService(self.records);app=Cockpit(self.root,service)
        async with app.run_test(size=(100,42)) as pilot:
            await self.ready(app,pilot);app.query_one(DataTable).move_cursor(row=1);await pilot.pause()
            await self.settings_page(app,pilot,action='models')
            await pilot.click('#model-sources');await pilot.pause()
            form=app.screen;self.assertIsInstance(form,SettingsForm)
            form.query_one('#model-ports',Input).value='8080'
            with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                await pilot.click('#apply');await pilot.pause()
            self.assertIsInstance(app.screen,Confirm);await pilot.click('#yes');await self.ready(app,pilot)
            self.assertEqual(service.calls[0][0:2],['update','beta'])
            self.assertEqual(service.calls[0][2]['local_model_ports'],'8080')
            self.assertEqual(app.screen.title, 'Updated beta')
            self.assertEqual(str(app.screen.query_one('#operation-back', Button).label), 'Done')
            await pilot.click('#operation-back');await self.ready(app,pilot)
            app.query_one(DataTable).move_cursor(row=1);await pilot.pause()
            await self.settings_page(app,pilot,action="models")
            local = app.screen.query_one('#live-local-provider', Collapsible)
            local.query_one('CollapsibleTitle').focus()
            await pilot.press('enter')
            await pilot.pause()
            await pilot.click('#local-preview');await self.ready(app,pilot)
            self.assertEqual(service.calls[-1],['provider','beta','local','preview',None])
            await pilot.press('q')

    async def test_home_controls_fit_and_theme_is_gold(self):
        app=Cockpit(self.root,FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot);self.assertEqual(app.theme,'cockpit-gold')
            for name in ['launch-with','settings','new','create-from','more','packages']:
                b=app.query_one('#'+name,Button)
                self.assertLessEqual(b.region.right,80,name);self.assertLessEqual(b.region.bottom,23,name)
            self.assertEqual(sum(button.display for button in app.query('#home Button')), 7)
            app.save_screenshot(filename='sandbox-home-declutter.svg',path='/tmp')
            await self.settings_page(app,pilot,action="network-settings")
            self.assertLessEqual(app.screen.query_one('#apply',Button).region.bottom,23)
            app.save_screenshot(filename='sandbox-network-declutter.svg',path='/tmp')
            await pilot.press('escape');await pilot.pause()
            await pilot.press('q')

    async def test_network_mode_recreates_from_grouped_settings(self):
        from textual.widgets import Collapsible, Select
        self.records[0]['config'].update(network='unrestricted', local_model_ports=None, options={
            'web_access': {'enabled': True, 'mode': 'brave', 'providers': {'brave': {'has_key': True}}},
        })
        service = FakeService(self.records)
        service.search_status = lambda name: {'brave': False}
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await self.settings_page(app, pilot, action='network-settings')
            form = app.screen
            self.assertIsInstance(form, SettingsForm)
            await self.ready(app, pilot)
            self.assertEqual(str(form.query_one('.focused-settings-title').render()), 'Network and Web Search · alpha')
            form.query_one('#network-section', Collapsible).collapsed = False
            mode = form.query_one('#network', Select)
            self.assertEqual(mode.value, 'unrestricted')
            self.assertFalse(form.query_one('#network-rules-setting').display)
            mode.value = 'allowlist'
            await pilot.pause()
            self.assertTrue(form.query_one('#network-rules-setting').display)
            form.query_one('#allowlist', TextArea).load_text('example.test:443')
            form.query_one('#apply', Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, Confirm)
            self.assertIn('Recreates the sandbox', app.screen.message)
            app.screen.query_one('#yes', Button).press()
            await self.ready(app, pilot)
            self.assertEqual(service.calls[0][0:2], ['update', 'alpha'])
            updated = service.calls[0][2]
            self.assertEqual(updated['network'], 'allowlist')
            self.assertEqual(updated['options']['web_access']['mode'], 'brave')
            self.assertTrue(updated['options']['web_access']['providers']['brave']['has_key'])

    async def test_search_changes_use_recreation_and_keep_network_edits(self):
        from textual.widgets import Collapsible
        for edit_rules in (False, True):
            service = FakeService(self.records)
            app = Cockpit(self.root, service)
            async with app.run_test(size=(120, 40)) as pilot:
                await self.ready(app, pilot)
                await self.settings_page(app, pilot, action='network-settings')
                form = app.screen
                form.query_one('#web-section', Collapsible).collapsed = False
                form.query_one('#web-enabled', Checkbox).value = True
                if edit_rules:
                    form.query_one('#allowlist', TextArea).load_text('example.test:443')
                await pilot.pause()
                with patch('pi_sandbox_factory.sandbox.choose_engine', return_value='podman'):
                    form.query_one('#apply', Button).press()
                    await pilot.pause()
                self.assertIsInstance(app.screen, Confirm)
                self.assertIn('• Web Search', app.screen.message)
                self.assertEqual('• Network\n' in app.screen.message, edit_rules)
                app.screen.query_one('#yes', Button).press()
                await self.ready(app, pilot)
                self.assertEqual(service.calls[0][0:2], ['update', 'alpha'])
                updated = service.calls[0][2]
                self.assertTrue(updated['options']['web_access']['enabled'])
                self.assertEqual('example.test:443' in updated['allowlist'], edit_rules)

    async def test_open_with_pi_uses_terminal_and_returns_to_manager(self):
        from contextlib import nullcontext
        import subprocess
        service=FakeService(self.records)
        service.command=Service(self.root).command
        app=Cockpit(self.root,service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            app.query_one(DataTable).move_cursor(row=1)
            with patch.object(app,'suspend',return_value=nullcontext()) as suspend, \
                 patch('pi_sandbox_factory.sandbox_tui.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run:
                await pilot.click('#launch-with');await pilot.pause()
                choices=app.screen.query_one('#editor-choices',OptionList)
                choices.highlighted=1;choices.focus()
                await pilot.press('enter');await self.ready(app,pilot)
                suspend.assert_called_once()
                run.assert_called_once_with(service.command('pi','beta'),check=False)
            self.assertEqual(len(app.screen_stack),1)
            self.assertEqual(app.selected_name(),'beta')
            table = app.query_one(DataTable)
            table.focus()
            open_cell = table.get_cell_coordinate('beta', 'open')
            table.move_cursor(row=open_cell.row, column=open_cell.column)
            await pilot.press('enter');await self.ready(app,pilot)
            self.assertEqual(service.calls,[['start','beta'],['open','beta','--editor','browser']])

    async def test_creation_cancel_button_escape_cleanup_failure_and_no_success_callback(self):
        import threading
        from pi_sandbox_factory.creation import Creation, Cancelled
        from textual.widgets import Static, Footer
        for size in ((80,24),(120,40),(220,50)):
            for failure in (False, True):
                app=Cockpit(self.root,FakeService(self.records))
                control=Creation()
                release=threading.Event()
                success=[]
                def task(log):
                    for step in range(35):
                        log(f'Step {step}: building fixture image with a long message to exercise wrapping in narrow terminals…')
                    release.wait(10)
                    if failure: raise ValueError('Creation stopped, but cleanup failed. Saved state was retained; use Delete on this sandbox to retry. Container engine unavailable.')
                    raise Cancelled('Creation cancelled. Containers removed; workspace preserved.')
                async with app.run_test(size=size) as pilot:
                    await self.ready(app,pilot)
                    operation=Operation('Creating fixture',task,cancellation=control,on_success=lambda:success.append(True))
                    await app.push_screen(operation)
                    await pilot.pause()
                    button=operation.query_one('#operation-back',Button)
                    self.assertFalse(button.disabled)
                    self.assertEqual(str(button.label),'Cancel creation')
                    self.assertLessEqual(button.region.bottom,operation.query_one(Footer).region.y)
                    await pilot.press('escape')
                    self.assertFalse(control.requested.is_set())
                    await pilot.click('#operation-back')
                    await pilot.pause()
                    self.assertTrue(control.requested.is_set())
                    self.assertTrue(button.disabled)
                    self.assertIn('Cancelling',str(operation.query_one('#operation-status',Static).render()))
                    await pilot.press('escape','q')
                    self.assertIs(app.screen,operation)
                    release.set()
                    await self.ready(app,pilot)
                    self.assertEqual(operation.failed,failure)
                    self.assertEqual(operation.cancelled,not failure)
                    self.assertEqual(success,[])
                    self.assertFalse(button.disabled)
                    self.assertEqual(str(button.label),'Back to sandboxes')
                    summary=str(operation.query_one('#operation-status',Static).render())
                    self.assertIn('Delete sandbox…' if failure else 'project files and build cache preserved',summary)
                    self.assertLessEqual(operation.query_one('#operation-log').region.bottom,
                                         operation.query_one('.wizard-actions').region.y)
                    output=operation.query_one('#operation-log')
                    self.assertLessEqual(output.virtual_size.width,output.scrollable_content_region.width)
                    self.assertEqual(operation.title,'Cleanup failed: fixture' if failure else 'Creation cancelled: fixture')
                    await pilot.press('escape')
                    await self.ready(app,pilot)
                    self.assertEqual(len(app.screen_stack),1)

    async def test_update_cancel_confirmation_and_preserved_sandbox(self):
        import threading
        from textual.widgets import Footer, Static
        from pi_sandbox_factory.updating import UpdateCancelled

        app = Cockpit(self.root, FakeService(self.records))
        success = []
        starts = []
        release = threading.Event()

        def task(log, update):
            starts.append(True)
            release.wait(10)
            raise UpdateCancelled()

        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            screen = UpdateOperation('Updating fixture', task, on_success=lambda: success.append(True))
            await app.push_screen(screen)
            try:
                await pilot.pause()
                button = screen.query_one('#operation-back', Button)
                self.assertFalse(button.disabled)
                self.assertEqual(str(button.label), 'Cancel update')
                self.assertLessEqual(button.region.bottom, screen.query_one(Footer).region.y)
                await pilot.press('escape')
                await pilot.pause()
                self.assertIsInstance(app.screen, Confirm)
                self.assertFalse(app.check_action('quit', ()))
                self.assertEqual(str(app.screen.query_one('#cancel', Button).label), 'Keep updating')
                await pilot.click('#cancel')
                await pilot.pause()
                self.assertIs(app.screen, screen)
                self.assertTrue(screen.update.can_cancel)
                await pilot.click('#operation-back')
                await pilot.pause()
                await pilot.click('#yes')
                await pilot.pause()
                self.assertFalse(screen.update.can_cancel)
            finally:
                release.set()
                await self.ready(app, pilot)
            self.assertTrue(screen.cancelled)
            self.assertFalse(screen.failed)
            self.assertFalse(button.disabled)
            self.assertEqual(str(button.label), 'Back to sandboxes')
            self.assertIn('current sandbox preserved', str(screen.query_one('#operation-status', Static).render()))
            self.assertEqual(starts, [True])
            self.assertEqual(success, [])
            await pilot.press('escape')
            await self.ready(app, pilot)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_missing_model_is_completed_with_warning(self):
        app=Cockpit(self.root,FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            app.push_screen(Operation('Opening sandbox',lambda log:log('Model status: Not connected. The sandbox is ready.')))
            await pilot.pause();await self.ready(app,pilot)
            self.assertTrue(app.screen.finished)
            self.assertFalse(app.screen.failed)
            self.assertTrue(app.screen.model_disconnected)
            self.assertTrue(app.screen.query_one('#operation-status').has_class('disconnected'))
            await pilot.press('escape');await self.ready(app,pilot)
            self.assertEqual(len(app.screen_stack),1)
            await pilot.press('q')


    async def test_network_action_applies_to_selected_sandbox(self):
        service=FakeService(self.records);app=Cockpit(self.root,service)
        self.records[1]['config']['options'] = {
            'web_access': {'enabled': True, 'mode': 'brave', 'providers': {'brave': {'has_key': True}}},
        }
        service.search_status = lambda name: {'brave': False}
        async with app.run_test(size=(100,42)) as pilot:
            await self.ready(app,pilot)
            app.query_one(DataTable).move_cursor(row=1)
            await self.settings_page(app,pilot,action="network-settings")
            await self.ready(app, pilot)
            field=app.screen.query_one('#allowlist',TextArea)
            legacy='local-model.internal:8000\npypi.org:443\nregistry.npmjs.org:443\ngithub.com:443'
            field.load_text(legacy)
            for category in ('dev-python', 'dev-npmjs', 'dev-github'):
                app.screen.query_one('#category-'+category,Checkbox).value=True
            await pilot.pause()
            self.assertEqual(field.text,legacy)
            self.assertFalse(app.screen.query('#pip-preset, #npm-preset, #github-preset'))
            await pilot.click('#apply');await self.ready(app,pilot)
            self.assertEqual(service.calls[0][:2],['network','beta'])
            self.assertEqual(service.calls[0][-4:],['--website-groups','dev-python','dev-npmjs','dev-github'])
            self.assertIsInstance(app.screen,Operation)

    async def test_contextual_actions_and_empty_selection(self):
        self.records[1]['dev']='exited'
        app=Cockpit(self.root,FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            await pilot.click('#more');await pilot.pause()
            self.assertEqual(app.screen.actions[0],('Stop sandbox','stop'))
            await pilot.press('escape');await pilot.pause()
            app.query_one(DataTable).move_cursor(row=1)
            await pilot.click('#more');await pilot.pause()
            self.assertEqual(app.screen.actions[0],('Start sandbox','start'))
            await pilot.press('escape');await pilot.pause()
            app.query_one('#search',Input).value='no-match';await pilot.pause()
            for name in ('launch-with','settings','more'):
                self.assertTrue(app.query_one('#'+name,Button).disabled)
            self.assertFalse(app.query_one('#new',Button).disabled)

    async def test_containers_action_opens_selected_sandbox_and_scrolls(self):
        from textual.widgets import Static
        record = self.records[1]
        prefix = 'pi-sandbox-factory-' + record['name']
        names = [prefix, *(prefix + '-' + kind for kind in ('proxy', 'inference', 'ssh', 'advisor', 'advisor-proxy'))]
        record['containers'] = sandbox.describe_containers(record['config'], dict.fromkeys(names, 'exited'))
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            app.query_one(DataTable).move_cursor(row=1)
            await pilot.click('#more')
            menu = app.screen.query_one('#more-actions', OptionList)
            menu.highlighted = next(i for i, (_, key) in enumerate(app.screen.actions) if key == 'containers')
            menu.focus()
            await pilot.press('enter')
            listing = app.screen.query_one('#container-list')
            back = app.screen.query_one('#containers-back')
            self.assertIs(app.focused, back)
            await pilot.press('tab')
            self.assertIs(app.focused, listing)
            strings = [str(widget.render()) for widget in listing.query(Static)]
            heading = app.screen.query_one('.title')
            self.assertEqual(str(heading.render()), 'Containers · beta')
            self.assertIn('Development · Stopped', strings)
            self.assertIn(prefix + '-advisor-proxy', strings)
            self.assertIn('Runs Pi, the editor backend and development tools.', strings)
            await pilot.press('end')
            await pilot.pause()
            self.assertGreater(listing.scroll_y, 0)
            self.assertLess(heading.region.bottom, listing.region.bottom)
            self.assertLess(back.region.bottom, app.size.height)
            await pilot.click('#containers-back')
            self.assertIs(app.screen, app.screen_stack[0])
            self.assertEqual(service.calls, [])

    async def test_dashboard_row_clicks_and_enter_select_without_opening(self):
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one('#sandboxes', DataTable)
            with patch.object(app, 'launch_client') as launch:
                name_cell = table.get_cell_coordinate('beta', 'name')
                name_region = table._get_cell_region(name_cell)
                await pilot.click('#sandboxes', offset=(3, name_region.y + 1))
                self.assertEqual(app.selected_name(), 'beta')
                await pilot.click('#sandboxes', offset=(3, name_region.y + 1), times=2)
                table.focus()
                await pilot.press('enter')
                launch.assert_not_called()
                self.assertEqual(app.selected_name(), 'beta')
                self.assertEqual(table.get_cell('beta', 'alerts'), '')
                table.move_cursor(row=0, column=0)
                cell = table.get_cell_coordinate('beta', 'open')
                region = table._get_cell_region(cell)
                first = table._get_cell_region(table.get_cell_coordinate('alpha', 'open'))
                self.assertEqual(region.y - first.y, 3)
                self.assertIn('alpha', table.render_line(first.y + 1).text)
                self.assertIn('Running', table.render_line(first.y + 1).text)
                action_line = table.render_line(first.y + 1).crop(first.x, first.right)
                self.assertTrue(any(segment.style and segment.style.meta.get('sandbox_action')
                                    for segment in action_line))
                for y in (first.y, first.y + 2):
                    gap_line = table.render_line(y).crop(first.x, first.right)
                    self.assertFalse(gap_line.text.strip())
                    self.assertFalse(any(segment.text and segment.style
                                         and segment.style.meta.get('sandbox_action') for segment in gap_line))
                    await pilot.click('#sandboxes', offset=(first.x + 3, y), times=2)
                    self.assertEqual(app.selected_name(), 'alpha')
                    launch.assert_not_called()
                await pilot.click('#sandboxes', offset=(region.x + 3, region.y + 1), button=3)
                launch.assert_not_called()
                await pilot.click('#sandboxes', offset=(region.x + 3, region.y + 1), times=2)
                launch.assert_called_once_with('beta', 'browser')
                launch.reset_mock()
                table.move_cursor(row=cell.row, column=cell.column)
                await pilot.press('enter')
                launch.assert_called_once_with('beta', 'browser')

    async def test_padded_dashboard_rows_remain_reachable_by_keyboard(self):
        records = [dict(self.records[0], name=f'sandbox-{i}') for i in range(6)]
        app = Cockpit(self.root, FakeService(records))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one(DataTable)
            table.focus()
            with patch.object(app, 'launch_client') as launch:
                for row in range(1, len(records)):
                    await pilot.press('down')
                    self.assertEqual(app.selected_name(), records[row]['name'])
                self.assertGreater(table.scroll_y, 0)
                await pilot.press('right', 'enter')
                launch.assert_called_once_with(records[-1]['name'], 'browser')

    async def test_sandbox_alerts_follow_selection_and_open_existing_actions(self):
        from pi_sandbox_factory.sandbox_tui import SSHScreen, SandboxAlerts
        old = self.records[0]
        common = self.root / 'legacy-common'
        common.mkdir()
        (common / 'Dockerfile').write_text(
            'ARG PI_VERSION=1.4.0\nARG PENDANT_VERSION=0.94.2\nRUN curl cdervis.vscode-pi-${PENDANT_VERSION}.vsix\n')
        old.update(chat_editor='pendant', ssh_key_state='locked')
        old['config']['common'] = {'path': str(common)}
        old['config']['ssh'] = {'hosts': [{
            'alias': 'repository', 'hostname': 'git.example.test', 'user': 'git', 'port': 22,
            'host_keys': ['ssh-ed25519 AAAA'],
        }]}
        service = FakeService(self.records)
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            table = app.query_one(DataTable)
            self.assertEqual(str(table.get_cell('alpha', 'alerts')).strip(), 'Show Alerts (2)')
            self.assertEqual(table.get_cell('beta', 'alerts'), '')
            cell = table.get_cell_coordinate('alpha', 'alerts')
            region = table._get_cell_region(cell)
            self.assertLess(table.region.x + region.right, app.size.width)
            await pilot.click('#sandboxes', offset=(region.x + 3, region.y + 1))
            self.assertIsInstance(app.screen, SandboxAlerts)
            menu = app.screen.query_one('#sandbox-alerts-list', OptionList)
            self.assertEqual(menu.option_count, 2)
            self.assertIn('SSH key locked', str(menu.get_option_at_index(0).prompt))
            self.assertIn('Update to FrostPi', str(menu.get_option_at_index(1).prompt))
            menu.highlighted = 0
            await pilot.press('enter')
            self.assertIsInstance(app.screen, SSHScreen)
            self.assertEqual(app.focused.id, 'ssh-unlock')
            await pilot.press('escape')
            await self.ready(app, pilot)
            table.move_cursor(row=cell.row, column=cell.column)
            table.focus()
            await pilot.press('enter')
            app.screen.query_one('#sandbox-alerts-list', OptionList).highlighted = 1
            await pilot.press('enter')
            self.assertIn('Update alpha to FrostPi?', app.screen.message)
            await pilot.press('escape')
            table.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(table.get_cell('beta', 'alerts'), '')
            service.records[0]['ssh_key_state'] = 'loaded'
            app.poll_ssh_status()
            await self.ready(app, pilot)
            self.assertEqual(str(table.get_cell('alpha', 'alerts')).strip(), 'Show Alerts (1)')
            self.assertEqual(app.selected_name(), 'beta')
            self.assertEqual(service.calls, [])


    async def test_automatic_approval_default_and_human_fallback_selection(self):
        from textual.widgets import Select, Static, Collapsible
        from pi_sandbox_factory import settings_effects
        from unittest.mock import Mock
        service=FakeService(self.records);app=Cockpit(self.root,service)
        self.records[0]['config']['options'] = {
            'web_access': {'enabled': True, 'mode': 'brave', 'providers': {'brave': {'has_key': True}}},
        }
        service.search_status = Mock(return_value={'brave': False})
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            await pilot.click('#settings')
            menu = app.screen.query_one('#settings-actions', OptionList)
            labels = [str(menu.get_option_at_index(i).prompt).split(' · ', 1)[0]
                      for i in range(menu.option_count)]
            self.assertIn('Approvals & sandbox', labels)
            self.assertEqual(labels.count('Network and Web Search'), 1)
            self.assertNotIn('Web Search', labels)
            self.assertNotIn('Network mode', labels)
            self.assertNotIn('Configuration', labels)
            close = app.screen.query_one('#settings-back', Button)
            self.assertEqual(str(close.label), 'Close')
            self.assertIs(app.screen.get_widget_at(close.region.x, close.region.y)[0], close)
            menu.highlighted = labels.index('Approvals & sandbox')
            await pilot.press('enter')
            await pilot.pause()
            form = app.screen
            choice=form.query_one('#approval',Select)
            self.assertIs(app.focused, form.query_one('#inner-sandbox', Checkbox))
            self.assertFalse(form.query_one('#agent-section', Collapsible).collapsed)
            self.assertFalse(form.query_one('#hosted-section', Collapsible).display)
            self.assertFalse(form.query_one('#network-search-section', Collapsible).display)
            self.assertEqual(choice.value,'automatic')
            self.assertEqual(len(form.query('#approval-help')), 0)
            self.assertIn('without asking you', str(form.query_one('#selected-approval-help', Static).render()))
            choice.value='assisted';await pilot.pause()
            self.assertIn('You can approve an action the reviewer rejects or leaves open',
                          str(form.query_one('#selected-approval-help', Static).render()))
            form.query_one('#explain-approval').scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click('#explain-approval')
            await pilot.pause()
            help_box = app.screen.query_one('#approval-help')
            rows = list(help_box.query('.approval-mode-help'))
            self.assertEqual(len(rows), 4)
            self.assertIn('If the reviewer rejects an action',
                          str(rows[1].query_one('.approval-mode-description', Static).render()))
            help_box.focus()
            await pilot.press('end')
            await pilot.pause(0.35)
            self.assertLessEqual(list(help_box.query(Static))[-1].region.bottom, help_box.content_region.bottom)
            self.assertIn('Apply and recreate to use these settings in Pi and FrostPi.',
                          str(list(help_box.query(Static))[-1].render()))
            close = app.screen.query_one('#close-approval-help', Button)
            self.assertLess(close.region.bottom, app.size.height)
            self.assertIs(app.screen.get_widget_at(close.region.x, close.region.y)[0], close)
            await pilot.press('escape')
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertEqual(choice.value, 'assisted')
            with patch('pi_sandbox_factory.sandbox.choose_engine', return_value='podman'):
                await pilot.click('#apply');await pilot.pause()
            self.assertIsInstance(app.screen, Confirm)
            await pilot.click('#yes')
            await self.ready(app, pilot)
            self.assertEqual(settings_effects.configuration_changes(form.draft, service.calls[0][2]),
                             ['Approvals & sandbox'])
            service.search_status.assert_not_called()
            self.assertEqual(app.screen.active_bindings['escape'].binding.description, 'Done')
            await pilot.press('escape')
            self.assertEqual(len(app.screen_stack), 1)


    async def test_recall_selection_is_saved_at_small_terminal(self):
        from textual.widgets import Select, Collapsible
        service=FakeService(self.records);app=Cockpit(self.root,service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot);await self.settings_page(app,pilot,action='agent-tools')
            form=app.screen
            form.query_one('#skills-section',Collapsible).collapsed=False
            choice=form.query_one('#compaction-recall',Select)
            self.assertEqual(choice.value,'lite')
            await pilot.pause();choice.focus();choice.scroll_visible(animate=False);await pilot.pause()
            await pilot.press('enter','down','enter');await pilot.pause()
            self.assertEqual(choice.value,'full')
            with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                draft,_=form.collect()
            self.assertEqual(draft['options']['compaction_recall'],'full')
            choice.value='off'
            with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                draft,_=form.collect()
            self.assertEqual(draft['options']['compaction_recall'],'off')

    async def test_package_install_at_small_terminal(self):
        service=FakeService(self.records);app=Cockpit(self.root,service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot);await pilot.click('#packages');await pilot.pause()
            await pilot.click('#package-install');await pilot.pause()
            app.screen.query_one('#package-name',Input).value='reporting'
            app.screen.query_one('#package-source',Input).value=str(self.root)
            app.screen.query_one('#package-subdir',Input).value='profiles/reporting'
            button=app.screen.query_one('#package-add',Button)
            button.scroll_visible(animate=False);await pilot.pause()
            self.assertTrue(await pilot.click('#package-add'))
            await self.ready(app,pilot)
            self.assertEqual(service.calls[-1],['profile','add','reporting',str(self.root),'--subdir','profiles/reporting'])
            self.assertIsInstance(app.screen,Operation)

    async def test_saved_unregistered_provider_remains_in_configuration(self):
        from pi_sandbox_factory import registry
        provider={'name':'company','settings':{'endpoint':'https://models.example.com/v1'},
                  'manifest':{'description':'Company models','settings':{'endpoint':{'type':'text','network':True},'key':{'type':'secret'}}}}
        self.records[0]['config']['model_providers']=[provider]
        service=FakeService(self.records);app=Cockpit(self.root,service)
        with patch.object(registry,'choices',side_effect=lambda kind:{'base':'Generic tools'} if kind=='profile' else {'local':'Local models'}):
            async with app.run_test(size=(80,24)) as pilot:
                await self.ready(app,pilot);await self.settings_page(app,pilot,action='models')
                await pilot.click('#model-sources');await pilot.pause()
                form=app.screen
                self.assertTrue(form.query_one('#source-company',Checkbox).value)
                self.assertEqual(form.query_one('#source-company-endpoint',Input).value,'https://models.example.com/v1')
                self.assertEqual(len(form.query('#source-company-key')),0)
                with patch('pi_sandbox_factory.sandbox.choose_engine',return_value='podman'):
                    draft,_=form.collect()
                self.assertEqual(draft['model_providers']['company'],provider['settings'])
                self.assertIn('models.example.com:443',draft['allowlist'])


    async def test_creation_provider_credentials_are_masked_and_separate_from_settings(self):
        from pi_sandbox_factory import registry
        from pi_sandbox_factory.sandbox_tui import preset
        info = {'description': 'Company models', 'network': ['models.example.com:443'],
                'settings': {'key': {'type': 'secret', 'label': 'Company API key'}},
                'inference': {'origin': 'https://models.example.com', 'auth': {'type': 'bearer'}}}
        resolve = registry.resolve
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(self.root, FakeService([]))
            with patch.object(registry, 'choices', side_effect=lambda kind: {'base': 'Generic tools'} if kind == 'profile' else {'company': 'Company models'}), \
                 patch.object(registry, 'manifest', return_value=info), \
                 patch.object(registry, 'resolve', side_effect=lambda kind, name: self.root if kind == 'provider' else resolve(kind, name)):
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='new-project', workspace=str(self.root), model_providers={})
                    await app.push_screen(SettingsForm(draft))
                    await pilot.pause()
                    form = app.screen
                    field = form.query_one('#source-company-key', Input)
                    details = form.query_one('#source-details-company')
                    checkbox = form.query_one('#source-company', Checkbox)
                    label = str(checkbox.label)
                    self.assertTrue(field.password)
                    self.assertTrue(field.disabled)
                    self.assertFalse(details.display)
                    checkbox.value = True
                    await pilot.pause()
                    self.assertFalse(field.disabled)
                    self.assertTrue(details.display)
                    self.assertEqual(str(checkbox.label), label)
                    field.value = 'synthetic-secret'
                    field.focus(); field.scroll_visible(animate=False)
                    await pilot.pause()
                    with patch('pi_sandbox_factory.sandbox.choose_engine', return_value='podman'):
                        selected, _ = form.collect()
                    self.assertEqual(selected['_provider_keys'], {'company': 'synthetic-secret'})
                    self.assertNotIn('synthetic-secret', json.dumps(selected['model_providers']))
                    self.assertNotIn('synthetic-secret', json.dumps(selected['options']))
                    field.value = 'synthetic\0secret'
                    await pilot.pause()
                    with self.assertRaisesRegex(ValueError, 'single-line'):
                        form.collect()
                    await pilot.pause()
                    self.assertIs(app.focused, field)
                    self.assertTrue(form.query_one('#source-company-key-error').display)
                    self.assertLessEqual(form.query_one('#source-company-key-error').region.bottom,
                                         form.query_one('.wizard-actions').region.y)
                    checkbox.value = False
                    await pilot.pause()
                    self.assertTrue(field.disabled)
                    self.assertFalse(details.display)
                    self.assertEqual(str(checkbox.label), label)
                    self.assertFalse(form.query_one('#source-company-key-error').display)
                    with patch('pi_sandbox_factory.sandbox.choose_engine', return_value='podman'):
                        selected, _ = form.collect()
                    self.assertEqual(selected['_provider_keys'], {})
                    await app.pop_screen()
                    await app.push_screen(SettingsForm(draft, defaults=True))
                    await pilot.pause()
                    self.assertFalse(app.screen.query('#source-company-key'))


class WebAccessUITests(unittest.TestCase):
    setUpClass = classmethod(TUITests.setUpClass.__func__)
    tearDownClass = classmethod(TUITests.tearDownClass.__func__)
    _callTestMethod = TUITests._callTestMethod
    setUp = TUITests.setUp
    tearDown = TUITests.tearDown
    ready = TUITests.ready
    async def test_web_settings_keep_key_out_of_saved_options(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm, SearchProviderDialog, preset
        from textual.widgets import Select
        app = Cockpit(self.root, FakeService([]))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            draft = preset('base', DEFAULT_SETTINGS)
            draft.update(name='web', workspace=str(self.root), local_model_ports=None)
            app.push_screen(SettingsForm(draft))
            await pilot.pause()
            form = app.screen
            web = form.query_one(WebAccessForm)
            web.query_one('#web-enabled', Checkbox).value = True
            web.query_one('#web-add', Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, SearchProviderDialog)
            app.screen.query_one('#search-provider-name', Select).value = 'brave'
            await pilot.pause()
            app.screen.query_one('#search-key', Input).value = 'synthetic-test-key'
            app.screen.query_one('#search-save', Button).press()
            await pilot.pause()
            value, keys = form.collect()
            self.assertEqual(keys, {'brave': 'synthetic-test-key'})
            self.assertNotIn('synthetic-test-key', json.dumps(value))
            self.assertEqual(value['options']['web_access']['mode'], 'auto')
            self.assertTrue(value['options']['web_access']['providers']['brave']['has_key'])
            web.query_one('#web-mode', Select).value = 'brave'
            await pilot.pause()
            web.query_one('#web-edit-brave', Button).press()
            await pilot.pause()
            app.screen.query_one('#search-remove', Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, SearchProviderDialog)
            self.assertIn('Choose another search mode', str(app.screen.query_one('#search-provider-error').render()))
            # Remove a key explicitly; an inactive selected provider cannot be applied.
            app.screen.query_one('#search-remove-key', Checkbox).value = True
            app.screen.query_one('#search-save', Button).press()
            await pilot.pause()
            with self.assertRaisesRegex(ValueError, 'Configure a search provider'):
                form.collect()
            web.query_one('#web-mode', Select).value = 'auto'
            await pilot.pause()
            value, keys = form.collect()
            self.assertEqual(keys, {'brave': ''})
            self.assertFalse(value['options']['web_access']['providers']['brave']['has_key'])
            web.query_one('#web-add', Button).press()
            await pilot.pause()
            app.screen.query_one('#search-provider-name', Select).value = 'searxng'
            await pilot.pause()
            self.assertFalse(app.screen.query_one('#search-key-field').display)
            app.screen.query_one('#search-endpoint', Input).value = 'https://search.example.net'
            app.screen.query_one('#search-save', Button).press()
            await pilot.pause()
            value, _ = form.collect()
            self.assertEqual(value['options']['web_access']['providers']['searxng']['endpoint'], 'https://search.example.net')

    async def test_save_web_defaults_preserves_other_defaults_and_keys_stay_private(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm, preset
        from textual.widgets import Select
        app = Cockpit(self.root, FakeService([]))
        with patch('pi_sandbox_factory.sandbox_tui.defaults_path', return_value=self.root / 'cockpit.json'):
            async with app.run_test(size=(80,24)) as pilot:
                await self.ready(app, pilot)
                draft = preset('base', DEFAULT_SETTINGS)
                draft.update(name='web', workspace=str(self.root), local_model_ports=None)
                app.push_screen(SettingsForm(draft))
                await pilot.pause()
                web = app.screen.query_one(WebAccessForm)
                web.query_one('#web-enabled', Checkbox).value = True
                await web.provider_saved(('brave', {'endpoint': '', 'model': '', 'has_key': True}, 'test-default-brave'))
                web.query_one('#web-save-defaults', Button).press()
                await pilot.pause()
                saved = json.loads((self.root / 'cockpit.json').read_text())
                self.assertEqual(saved['memory'], DEFAULT_SETTINGS['memory'])
                self.assertNotIn('test-default-brave', json.dumps(saved))
                keyfile = self.root / 'web-access-keys.json'
                self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(keyfile.read_text())['brave']['key'], 'test-default-brave')
                self.assertTrue(app.defaults['options']['web_access']['providers']['brave']['has_key'])

    async def test_copy_search_settings_without_keys_does_not_use_default_keys(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm, CopyWebAccess, preset
        from pi_sandbox_factory import web_access
        source = self.records[0]
        source['config']['options'] = {'web_access': {'enabled': True, 'mode': 'auto',
            'providers': {'exa': {}, 'brave': {'has_key': True}}}}
        defaults = web_access.settings(source['config']['options']['web_access'])
        web_access.save_keys(self.root/'web-access-keys.json', defaults, {'brave': 'default-key'})
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            draft = preset('base', DEFAULT_SETTINGS)
            draft['options']['web_access'] = defaults
            draft.update(name='new', workspace=str(self.root), local_model_ports=None)
            app.push_screen(SettingsForm(draft))
            await pilot.pause()
            form = app.screen
            web = form.query_one(WebAccessForm)
            self.assertEqual(web.keys['brave'], 'default-key')
            web.query_one('#web-copy', Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, CopyWebAccess)
            app.screen.query_one('#copy-web-keys', Checkbox).value = False
            app.screen.query_one('#copy-web-apply', Button).press()
            await pilot.pause()
            value, keys = form.collect()
            self.assertEqual(keys['brave'], '')
            self.assertFalse(value['options']['web_access']['providers']['brave']['has_key'])
            self.assertNotIn('default-key', str(keys))
            self.assertTrue(source['config']['options']['web_access']['providers']['brave']['has_key'])
            source['dev'] = 'exited'
            web.query_one('#web-copy', Button).press()
            await pilot.pause()
            # Inventory is a snapshot; use its displayed state when selecting the source.
            app.screen.records['alpha']['dev'] = 'exited'
            app.screen.source_changed()
            self.assertTrue(app.screen.query_one('#copy-web-keys', Checkbox).disabled)
            self.assertFalse(app.screen.query_one('#copy-web-keys', Checkbox).value)

    async def test_edit_copy_without_keys_keeps_matching_target_key(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm, preset
        from pi_sandbox_factory import web_access
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            draft = preset('base', DEFAULT_SETTINGS)
            draft.update(name='alpha', workspace=str(self.root), local_model_ports=None)
            draft['options']['web_access'] = {'enabled': True, 'mode': 'auto', 'providers': {'exa': {}, 'brave': {'has_key': True}}}
            app.push_screen(SettingsForm(draft, editing=True))
            await pilot.pause()
            web = app.screen.query_one(WebAccessForm)
            copied = web_access.settings({'enabled': True, 'providers': {'exa': {}, 'brave': {}}})
            await web.copied((copied, None))
            value, keys = app.screen.collect()
            self.assertTrue(value['options']['web_access']['providers']['brave']['has_key'])
            self.assertNotIn('brave', keys)  # No key replacement or deletion queued.

    async def test_missing_runtime_key_is_not_displayed_as_configured(self):
        from pi_sandbox_factory.sandbox_tui import WebAccessForm, preset
        service = FakeService(self.records)
        service.search_status = lambda name: {'exa': False, 'brave': False}
        app = Cockpit(self.root, service)
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            draft = preset('base', DEFAULT_SETTINGS)
            draft.update(name='alpha', workspace=str(self.root), local_model_ports=None)
            draft['options']['web_access'] = {'enabled': True, 'providers': {'exa': {}, 'brave': {'has_key': True}}}
            app.push_screen(SettingsForm(draft, editing=True))
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            web = app.screen.query_one(WebAccessForm)
            self.assertFalse(web.providers['brave']['has_key'])

    async def test_network_category_selection_reaches_cli(self):
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app, pilot)
            app.perform_action('network-settings')
            await pilot.pause()
            app.screen.query_one('#category-ut1-press', Checkbox).value = True
            app.screen.query_one('#apply', Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            call = app.service.calls[-1]
            self.assertEqual(call[-2:], ['--website-groups', 'ut1-press'])

    async def test_allow_all_is_scoped_and_saves_explicit_categories(self):
        from pi_sandbox_factory.sandbox_tui import WebsiteGroups, network_groups
        from textual.widgets import Label
        app = Cockpit(self.root, FakeService(self.records))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            record = copy.deepcopy(self.records[0])
            record['config']['options'] = {'website_groups': ['dev-python']}
            app.perform_action('network-settings', record=record)
            await pilot.pause()
            app.screen.query_one('#network-section', Collapsible).collapsed = False
            groups = app.screen.query_one(WebsiteGroups)
            groups.query_one(Collapsible).collapsed = False
            await pilot.pause()

            async def toggle(control):
                control.focus()
                await pilot.press('space')
                await pilot.pause()

            developers = groups.query_one('#category-all-dev', Checkbox)
            marketplace = groups.query_one('#category-dev-vscode-extensions', Checkbox)
            label = developers.label
            self.assertEqual(label.plain, 'Allow all')
            self.assertEqual([heading.query_one(Label).render().plain
                              for heading in groups.query('.website-category-heading')],
                             ['Developer services', 'Authentication', 'Topics'])
            self.assertEqual([item.label.plain for item in
                              groups.query('#website-category-auth .website-category-member')],
                             ['Microsoft Entra / Microsoft 365', 'Google sign-in', 'Gmail',
                              'GitHub sign-in', 'Facebook and Meta sign-in'])
            for heading in groups.query('.website-category-heading'):
                title = heading.query_one(Label)
                control = heading.query_one(Checkbox)
                self.assertEqual(title.region.y, control.region.y)
                self.assertGreaterEqual(control.region.x - title.region.x - len(title.render().plain), 2)
                self.assertLessEqual(control.region.right, heading.region.right)
            developer_names = [name for name in network_groups.catalog() if name.startswith('dev-')]
            self.assertFalse(developers.value)
            self.assertEqual(groups.selected(), ['dev-python'])
            await toggle(developers)
            self.assertTrue(developers.value)
            self.assertEqual(developers.label, label)
            self.assertEqual(groups.selected(), developer_names)
            await toggle(marketplace)
            self.assertFalse(developers.value)
            self.assertEqual(groups.selected(), [name for name in developer_names if name != 'dev-vscode-extensions'])
            await toggle(marketplace)
            self.assertTrue(developers.value)

            for prefix in ('auth', 'ut1'):
                control = groups.query_one(f'#category-all-{prefix}', Checkbox)
                names = [name for name in network_groups.catalog() if name.startswith(prefix + '-')]
                await toggle(control)
                self.assertEqual(set(groups.selected()), set(developer_names + names))
                await toggle(control)
                self.assertEqual(groups.selected(), developer_names)

            await toggle(developers)
            self.assertEqual(groups.selected(), [])
            await toggle(marketplace)
            await toggle(groups.query_one('#category-auth-google', Checkbox))
            self.assertFalse(groups.query_one('#category-auth-gmail', Checkbox).value)
            self.assertFalse(groups.query_one('#category-all-auth', Checkbox).value)
            rules = app.screen.query_one('#allowlist', TextArea)
            rules.load_text('bad-host')
            await pilot.pause()
            app.screen.query_one('#apply', Button).press()
            await pilot.pause()
            self.assertTrue(rules.has_class('-invalid'))
            self.assertIn('Line 1:', app.screen.query_one('#network-error').render().plain)
            rules.load_text('example.com:443')
            await pilot.pause()
            self.assertFalse(rules.has_class('-invalid'))
            self.assertFalse(app.screen.query_one('#network-error').display)
            app.screen.query_one('#apply', Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertEqual(app.service.calls[-1][-3:], ['--website-groups', 'auth-google', 'dev-vscode-extensions'])

    async def test_microsoft_auth_category_can_be_selected_and_removed(self):
        for initial in (False, True):
            app = Cockpit(self.root, FakeService(self.records))
            async with app.run_test(size=(80,24)) as pilot:
                await self.ready(app, pilot)
                record = copy.deepcopy(self.records[0])
                record['config']['options'] = {'website_groups': ['auth-microsoft'] if initial else []}
                app.perform_action('network-settings', record=record)
                await pilot.pause()
                checkbox = app.screen.query_one('#category-auth-microsoft', Checkbox)
                self.assertEqual(checkbox.value, initial)
                checkbox.value = not initial
                app.screen.query_one('#apply', Button).press()
                await pilot.pause();await app.workers.wait_for_complete()
                expected = ['--website-groups'] + ([] if initial else ['auth-microsoft'])
                self.assertEqual(app.service.calls[-1][-len(expected):], expected)

if __name__ == '__main__':
    unittest.main()

class DashboardActivityTests(unittest.TestCase):
    setUpClass = classmethod(TUITests.setUpClass.__func__)
    tearDownClass = classmethod(TUITests.tearDownClass.__func__)
    _callTestMethod = TUITests._callTestMethod
    setUp = TUITests.setUp
    tearDown = TUITests.tearDown
    ready = TUITests.ready
    async def test_timestamp_sorting_search_and_selection(self):
        self.records[0]['activity']={'created':'2026-11-13T12:00:00+00:00','started':'2026-11-14T10:00:00+00:00','edited':None}
        self.records[1]['activity']={'created':'2026-11-14T12:00:00+00:00','started':'2026-11-14T11:00:00+00:00','edited':'2026-11-14T12:30:00+00:00'}
        app=Cockpit(self.root,FakeService(self.records))
        from textual.widgets import Select
        async with app.run_test(size=(80,24)) as pilot:
            await self.ready(app,pilot)
            table=app.query_one('#sandboxes',DataTable)
            self.assertEqual(str(table.get_row_at(0)[0]).strip(),'beta')
            selected=app.selected_name()
            await pilot.click('#sort-direction');await pilot.pause()
            self.assertEqual(str(table.get_row_at(0)[0]).strip(),'alpha')
            self.assertEqual(app.selected_name(),selected)
            app.query_one('#sort-field',Select).value='edited';await pilot.pause()
            self.assertEqual(str(table.get_row_at(0)[0]).strip(),'beta')
            await pilot.click('#sort-direction');await pilot.pause()
            self.assertEqual(str(table.get_row_at(0)[0]).strip(),'beta') # unknown stays last
            app.query_one('#search',Input).value='alpha';await pilot.pause()
            self.assertEqual(table.row_count,1);self.assertEqual(str(table.get_row_at(0)[0]).strip(),'alpha')
            self.assertLess(app.query_one('#more').region.bottom,24)
