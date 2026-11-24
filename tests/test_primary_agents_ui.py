import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from ai_sandbox_factory import sandbox, settings_effects
from ai_sandbox_factory.primary_workspace import WEB_SEARCH_OPTIONS
from ai_sandbox_factory.sandbox_tui import (
    Cockpit, DEFAULT_SETTINGS, PrimarySignIn, SettingsForm, SettingsMenu, copy_draft, preset,
)
from ai_sandbox_factory.tui_widgets import Checkbox
from test_tui import FakeService


class PrimaryAgentsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = asyncio.Runner()
        cls.addClassCleanup(cls.runner.close)

    def _callTestMethod(self, method):
        self.runner.run(method())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='factory-primary-ui-')
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        defaults = patch('ai_sandbox_factory.sandbox_tui.load_defaults',
                         side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS))
        defaults.start()
        self.addCleanup(defaults.stop)
        updates = patch('ai_sandbox_factory.updates.available_update', return_value=None)
        updates.start()
        self.addCleanup(updates.stop)

    async def test_create_and_review_native_agent_at_each_size(self):
        for size, agent in zip(((80, 24), (120, 40), (220, 50)), ('codex', 'claude', 'agy')):
            with self.subTest(agent=agent, terminal=size):
                app = Cockpit(self.state, FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    app.action_new()
                    await pilot.pause()
                    screen = app.screen
                    selector = screen.query_one('#primary-agent', Select)
                    self.assertEqual(selector.value, 'pi')
                    self.assertIs(screen.focused, selector)
                    selector.value = agent
                    screen.query_one('#workspace', Input).value = str(self.state)
                    await pilot.pause()
                    self.assertFalse(screen.query_one('#profile-setting').display)
                    self.assertIs(screen.focused, selector)
                    name = screen.query_one('#name', Input)
                    name.value = 'quick-start'
                    await pilot.click('#continue')
                    await pilot.pause()
                    review = app.screen
                    self.assertIsInstance(review, SettingsForm)
                    auth = review.query_one('#primary-auth', Select)
                    self.assertIs(review.focused, auth)
                    self.assertEqual(auth.value, 'copy')
                    self.assertLessEqual(auth.region.width, 40)
                    self.assertFalse(review.query_one('#primary-sign-in-section', Collapsible).collapsed)
                    self.assertFalse(review.query('#hosted-section, #agent-section, #web-enabled'))
                    self.assertTrue(review.query('#editor-section'))
                    self.assertEqual(review.query_one('#editor-mode', Select).value, 'browser')
                    auth.value = 'copy'
                    await pilot.pause()
                    approval = review.query_one('#native-approval', Select)
                    self.assertEqual(approval.value, 'automatic')
                    approval.value = 'manual'
                    await pilot.pause()
                    draft, keys = review.collect()
                    self.assertEqual(draft['options']['native_approval'], 'manual')
                    self.assertEqual((draft['primary_agent'], draft['primary_auth'], keys), (agent, 'copy', {}))
                    self.assertEqual(draft['editor']['mode'], 'browser')
                    self.assertEqual(review.query_one('#apply').region.height, 1)
                    self.assertLess(review.query_one('#apply').region.bottom, size[1])
                    await app.pop_screen()
                    await app.pop_screen()

    async def test_saved_settings_and_copy_keep_credential_operation_explicit(self):
        record = {'name': 'project', 'dev': 'running', 'proxy': 'running', 'engine': 'podman',
                  'profile': 'base', 'workspace': str(self.state),
                  'config': {'name': 'project', 'engine': 'podman', 'profile': sandbox.load_profile('base'),
                             'workspace': str(self.state), 'network': 'allowlist',
                             'primary_agent': 'agy', 'primary_auth': 'copy'}}
        directory = self.state / record['name']
        directory.mkdir()
        (directory / 'allowlist.txt').write_text('')
        self.assertEqual(copy_draft(record, self.state)['primary_auth'], 'copy')

        app = Cockpit(self.state, FakeService([record]))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await app.push_screen(SettingsMenu(record['name'], record['config'], 'pi'))
            await pilot.pause()
            actions = dict((action, label) for label, action in app.screen.actions)
            self.assertEqual(set(actions), {'primary-sign-in', 'folders', 'network-settings',
                                            'connectors', 'skills', 'primary-browser', 'primary-approvals',
                                            'system-prompt', 'resources', 'editor-settings'})
            await app.pop_screen()
            await app.push_screen(PrimarySignIn(record))
            await pilot.pause()
            action = app.screen.query_one('#primary-import', Button)
            self.assertGreaterEqual(action.content_region.width, len(str(action.label)))
            action.press()
            await pilot.pause()
            self.assertEqual(app.screen.focused.id, 'yes')
            await pilot.press('escape')
            await pilot.pause()
            self.assertFalse(app.service.calls)
            await app.pop_screen()


    async def test_native_browser_and_network_setting_edits_do_not_offer_pi_controls(self):
        draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
        draft.update(name='project', workspace=str(self.state), primary_agent='claude', model_providers={})
        draft['options']['browser'] = False
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await app.push_screen(SettingsForm(draft, editing=True, setting='primary-browser'))
            await pilot.pause()
            screen = app.screen
            choice = screen.query_one('#primary-browser')
            choice.value = True
            await pilot.pause()
            updated = screen.collect()[0]
            self.assertTrue(updated['options']['browser'])
            self.assertEqual(settings_effects.configuration_changes(draft, updated), ['Browser'])
            self.assertFalse(screen.query('#protect-browser-credentials, #compaction-recall, #primary-auth'))
            self.assertIn('on', screen.query_one('#primary-browser-section', Collapsible).title)
            await app.pop_screen()
            await app.push_screen(SettingsForm(draft, editing=True, setting='network-settings'))
            await pilot.pause()
            screen = app.screen
            screen.query_one('#allowlist', TextArea).load_text('example.com:443')
            self.assertEqual(screen.collect()[0]['allowlist'], 'example.com:443')
            self.assertFalse(screen.query('#web-enabled, #network-search-section, #primary-auth'))
            await app.pop_screen()

    async def test_native_web_search_support_and_checkbox_copy_stay_consistent(self):
        for agent in ('pi', 'codex', 'claude', 'agy'):
            app = Cockpit(self.state, FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='project', workspace=str(self.state), primary_agent=agent, model_providers={})
                await app.push_screen(SettingsForm(draft))
                await pilot.pause()
                self.assertEqual(bool(app.screen.query('#' + agent + '-web-search')), agent in WEB_SEARCH_OPTIONS)
                if agent in WEB_SEARCH_OPTIONS:
                    checkbox = app.screen.query_one('#' + agent + '-web-search', Checkbox)
                    help_text = app.screen.query_one('#' + agent + '-web-search-help', Static)
                    self.assertFalse(checkbox.value)
                    label, explanation = str(checkbox.label), str(help_text.render())
                    self.assertEqual(label, 'Enable web search')
                    if agent == 'codex':
                        self.assertIn('OpenAI’s index', explanation)
                    for enabled in (True, False):
                        checkbox.value = enabled
                        await pilot.pause()
                        self.assertEqual(str(checkbox.label), label)
                        self.assertEqual(str(help_text.render()), explanation)
                        self.assertEqual(app.screen.collect()[0]['options'][WEB_SEARCH_OPTIONS[agent]], enabled)
                await app.pop_screen()
                await app.push_screen(SettingsMenu('project', draft, 'browser'))
                await pilot.pause()
                actions = {action for _, action in app.screen.actions}
                self.assertNotIn('primary-web-search', actions)
                if agent in WEB_SEARCH_OPTIONS:
                    labels = {action: label for label, action in app.screen.actions}
                    self.assertIn('Network and Web Search', labels['network-settings'])

    async def test_native_web_search_saves_without_recreating_and_errors_keep_save_visible(self):
        for agent, key in WEB_SEARCH_OPTIONS.items():
            for size in ((80, 24), (120, 40), (220, 50)):
                with self.subTest(size=size):
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='project', workspace=str(self.state), primary_agent=agent, model_providers={})
                    draft['options'][key] = True
                    app = Cockpit(self.state, FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await app.push_screen(SettingsForm(draft, editing=True, setting='network-settings'))
                        await pilot.pause()
                        form = app.screen
                        self.assertEqual(form.focused.id, agent + '-web-search')
                        choice = form.query_one('#' + agent + '-web-search', Checkbox)
                        self.assertTrue(choice.value)  # Preserve an explicit saved choice.
                        explanation = str(form.query_one('#' + agent + '-web-search-help', Static).render())
                        self.assertIn('CLI', explanation)
                        allowlist = form.query_one('#allowlist', TextArea)
                        self.assertFalse(allowlist.soft_wrap)
                        self.assertEqual(allowlist.region.x, form.query_one('#network', Select).region.x)
                        choice.value = False
                        await pilot.pause()
                        self.assertEqual(str(form.query_one('#' + agent + '-web-search-help', Static).render()), explanation)
                        self.assertFalse(form.collect()[0]['options'][key])
                        save = form.query_one('#apply', Button)
                        self.assertEqual(str(save.label), 'Apply')
                        self.assertEqual(save.region.height, 1)
                        self.assertLess(save.region.bottom, size[1])
                        with patch.object(app.service, 'update_settings', create=True,
                                          side_effect=ValueError('Could not save web search: fixture storage is read-only.')):
                            save.press()
                            await pilot.pause()
                            self.assertIs(app.screen, form)
                            self.assertIn('fixture storage is read-only', str(form.query_one('#form-error').render()))
                            self.assertLess(save.region.bottom, size[1])
                        with patch.object(app.service, 'update_settings', create=True) as update:
                            save.press()
                            await pilot.pause()
                            self.assertEqual(update.call_args.args[:2], ('project', {key: False}))
                            self.assertNotIsInstance(app.screen, SettingsForm)
                        self.assertFalse(app.service.calls)

    async def test_saved_native_approval_choice_preserves_other_options(self):
        draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
        draft.update(name='project', workspace=str(self.state), primary_agent='claude', model_providers={})
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await app.push_screen(SettingsForm(draft, editing=True, setting='primary-approvals'))
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.focused.id, 'native-approval')
            self.assertLess(screen.query_one('#apply', Button).region.bottom, 24)
            self.assertGreater(screen.query_one('#apply', Button).region.height, 0)
            screen.query_one('#native-approval', Select).value = 'manual'
            await pilot.pause()
            updated = screen.collect()[0]
            self.assertEqual(updated['options'], {**draft['options'], 'native_approval': 'manual'})
            self.assertEqual(settings_effects.configuration_changes(draft, updated), ['Approvals'])
            self.assertIn('Ask for approval', screen.query_one('#primary-approvals-section', Collapsible).title)
            self.assertFalse(screen.query('#approval-mode, #inner-sandbox, #primary-auth'))
            self.assertEqual(str(screen.query_one('#apply', Button).label), 'Save')
            self.assertFalse(screen.query_one('#open-editor').display)
            await pilot.press('escape')
            self.assertEqual(draft['options']['native_approval'], 'automatic')
            with patch.object(app.service, 'update_settings', create=True) as save:
                await app.push_screen(SettingsForm(draft, editing=True, setting='primary-approvals'))
                await pilot.pause()
                app.screen.query_one('#native-approval', Select).value = 'manual'
                await pilot.click('#apply')
                await pilot.pause()
                self.assertEqual(save.call_args.args[:2], ('project', {'native_approval': 'manual'}))
                self.assertNotIsInstance(app.screen, SettingsForm)


    async def test_ssh_destinations_and_import_are_collected_inside_creation(self):
        project = self.state / 'project'
        project.mkdir()
        known_hosts = self.state / 'known_hosts'
        known_hosts.write_text('git.example.test ssh-ed25519 AAAA\n')
        private_key = self.state / 'key'
        private_key.write_text('fixture')
        for agent, size in (('pi', (80, 24)), ('codex', (120, 40))):
            with self.subTest(agent=agent):
                app = Cockpit(self.state, FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='ssh-fixture', workspace=str(project), primary_agent=agent, primary_auth='login')
                    review = SettingsForm(draft)
                    await app.push_screen(review)
                    await pilot.pause()
                    group = review.query_one('#connectors-section', Collapsible)
                    self.assertEqual(str(review.query_one('#apply', Button).label), 'Create and start sandbox')
                    launch = review.query_one('#open-editor', Checkbox)
                    if agent == 'pi':
                        self.assertEqual(str(launch.label), 'Open the browser editor when ready')
                        review.query_one('#editor-mode', Select).value = 'pi'
                        await pilot.pause()
                        self.assertEqual(str(launch.label), 'Open Pi CLI when ready')
                        review.query_one('#editor-mode', Select).value = 'browser'
                        await pilot.pause()
                        self.assertEqual(str(launch.label), 'Open the browser editor when ready')
                    else:
                        self.assertEqual(str(launch.label), 'Open the browser editor when ready')
                    self.assertTrue(group.collapsed)
                    group.collapsed = False
                    choose_ssh = group.query_one('#connector-ssh', Button)
                    choose_ssh.press()
                    await pilot.pause()
                    ssh = app.screen
                    self.assertIs(ssh.focused, ssh.query_one('#ssh-hosts', TextArea))
                    self.assertIsNotNone(ssh.query_one('#git-author-name', Input))
                    ssh.query_one('#create-ssh-key', Select).value = 'import'
                    await pilot.pause()
                    save = ssh.query_one('#create-ssh-save', Button)
                    save.press()
                    await pilot.pause()
                    self.assertIn('Add an SSH destination', str(ssh.query_one('#ssh-destinations-error').render()))
                    self.assertIs(ssh.focused, ssh.query_one('#ssh-hosts', TextArea))
                    ssh.query_one('#ssh-hosts', TextArea).load_text('project git git.example.test 22')
                    ssh.query_one('#ssh-known-hosts', Input).value = str(known_hosts)
                    key_file = ssh.query_one('#create-ssh-key-file', Input)
                    save.press()
                    await pilot.pause()
                    feedback = ssh.query_one('#create-ssh-key-file-error')
                    self.assertIn('Choose a private key file.', str(feedback.render()))
                    self.assertIs(ssh.focused, key_file)
                    viewport = ssh.query_one(VerticalScroll).content_region
                    self.assertLessEqual(viewport.y, key_file.region.y)
                    self.assertLessEqual(feedback.region.bottom, viewport.bottom)
                    key_file.value = str(private_key)
                    ssh.query_one('#create-ssh-duration', Input).value = '2d'
                    save.press()
                    await pilot.pause()
                    self.assertIs(app.screen, review)
                    self.assertIn('1 destination', str(group.query_one('#connector-status-ssh').render()))
                    saved, _ = review.collect()
                    self.assertEqual(saved['ssh']['hosts'][0]['host_keys'], ['ssh-ed25519 AAAA'])
                    self.assertEqual(saved['_ssh_key_file'], str(private_key))
                    self.assertEqual(saved['_ssh_key_duration'], '2d')
                    choose_ssh.press()
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one('#create-ssh-key-file', Input).value, str(private_key))
                    app.screen.query_one('#create-ssh-key', Select).value = 'later'
                    await pilot.press('escape')
                    await pilot.pause()
                    self.assertEqual(review.collect()[0]['_ssh_key_file'], str(private_key))
                    self.assertFalse(app.service.calls)

    async def test_back_keeps_connector_choices_in_this_wizard_only(self):
        package = self.state / 'packages' / 'build-status'
        package.mkdir(parents=True)
        routes = {'latest': 'https://ci.example.test/status', 'history': 'https://ci.example.test/history'}
        (package / 'connector.json').write_text(json.dumps(dict(
            version=1, id='build-status', title='Build status', description='Read the latest build.',
            documentation='Connect a read-only service account.', operations=routes,
            agent_instructions='Use the latest request to check the current build.')))
        self.enterContext(patch('ai_sandbox_factory.connectors.has_token', return_value=True))
        app = Cockpit(self.state, FakeService([]))
        with patch('ai_sandbox_factory.connectors.directory', return_value=package.parent):
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                app.action_new()
                await pilot.pause()
                first = app.screen
                first.query_one('#workspace', Input).value = str(self.state)
                first.query_one('#name', Input).value = 'example'
                first.query_one('#continue', Button).press()
                await pilot.pause()
                review = app.screen
                review.query_one('#connector-build-status', Button).press()
                await pilot.pause()
                await pilot.press('escape')
                await pilot.pause()
                self.assertEqual(review.collect()[0].get('connector_ids', []), [])
                review.query_one('#connector-build-status', Button).press()
                await pilot.pause()
                permission = next(box for box in app.screen.query(Checkbox) if box.name == 'latest')
                permission.value = True
                await pilot.pause()
                app.screen.query_one('#connector-confirm', Button).press()
                await pilot.pause()
                self.assertEqual(review.collect()[0]['_connector_routes'], {'build-status': routes})
                self.assertEqual(review.collect()[0]['connector_operations'], {'build-status': ['latest']})
                self.assertIn('1 of 2 requests', str(review.query_one('#connector-status-build-status').render()))
                review.query_one('#back', Button).press()
                await pilot.pause()
                self.assertIs(app.screen, first)
                first.query_one('#continue', Button).press()
                await pilot.pause()
                resumed = app.screen
                self.assertEqual(resumed.collect()[0]['_connector_routes'], {'build-status': routes})
                self.assertEqual(resumed.collect()[0]['_connector_instructions'],
                                 {'build-status': {'service': 'Use the latest request to check the current build.', 'operations': {}}})
                self.assertEqual(resumed.collect()[0]['connector_ids'], ['build-status'])
                self.assertEqual(resumed.collect()[0]['connector_operations'], {'build-status': ['latest']})
                await pilot.press('escape', 'escape')
                await pilot.pause()
                self.assertFalse(app.service.calls)
                app.action_new()
                await pilot.pause()
                self.assertFalse(app.screen.connector_draft)
