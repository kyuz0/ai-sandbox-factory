import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Collapsible, Input, Select, TextArea

from pi_sandbox_factory import sandbox, settings_effects
from pi_sandbox_factory.sandbox_tui import (
    Cockpit, DEFAULT_SETTINGS, PrimarySignIn, SettingsForm, SettingsMenu, copy_draft, preset,
)
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
        defaults = patch('pi_sandbox_factory.sandbox_tui.load_defaults',
                         side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS))
        defaults.start()
        self.addCleanup(defaults.stop)
        updates = patch('pi_sandbox_factory.updates.available_update', return_value=None)
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
                    self.assertEqual(auth.value, 'login')
                    self.assertFalse(review.query_one('#primary-sign-in-section', Collapsible).collapsed)
                    self.assertFalse(review.query('#hosted-section, #editor-section, #agent-section, #web-enabled'))
                    auth.value = 'copy'
                    await pilot.pause()
                    draft, keys = review.collect()
                    self.assertEqual((draft['primary_agent'], draft['primary_auth'], keys), (agent, 'copy', {}))
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
        self.assertEqual(copy_draft(record, self.state)['primary_auth'], 'login')

        app = Cockpit(self.state, FakeService([record]))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await app.push_screen(SettingsMenu(record['name'], record['config'], 'pi'))
            await pilot.pause()
            actions = dict((action, label) for label, action in app.screen.actions)
            self.assertEqual(set(actions), {'primary-sign-in', 'folders', 'network-settings',
                                            'ssh-settings', 'aws-access', 'skills', 'primary-browser',
                                            'system-prompt', 'resources'})
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
                    ssh = review.query_one('#git-identity-section', Collapsible)
                    self.assertEqual(ssh.title, 'SSH & Git')
                    self.assertIsNotNone(ssh.query_one('#git-author-name', Input))
                    ssh.query_one('#create-ssh-key', Select).value = 'import'
                    await pilot.pause()
                    with self.assertRaisesRegex(ValueError, 'Add an SSH destination'):
                        review.collect()
                    self.assertFalse(ssh.collapsed)
                    ssh.query_one('#ssh-hosts', TextArea).load_text('project git git.example.test 22')
                    ssh.query_one('#ssh-known-hosts', Input).value = str(known_hosts)
                    key_file = ssh.query_one('#create-ssh-key-file', Input)
                    review.query_one('#apply', Button).press()
                    await pilot.pause()
                    feedback = ssh.query_one('#create-ssh-key-file-error')
                    self.assertIn('Choose a private key file.', str(feedback.render()))
                    self.assertIs(review.focused, key_file)
                    viewport = review.query_one('.form').content_region
                    self.assertLessEqual(viewport.y, key_file.region.y)
                    self.assertLessEqual(feedback.region.bottom, viewport.bottom)
                    key_file.value = str(private_key)
                    ssh.query_one('#create-ssh-duration', Input).value = '2d'
                    saved, _ = review.collect()
                    self.assertEqual(saved['ssh']['hosts'][0]['host_keys'], ['ssh-ed25519 AAAA'])
                    self.assertEqual(saved['_ssh_key_file'], str(private_key))
                    self.assertEqual(saved['_ssh_key_duration'], '2d')
