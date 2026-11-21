import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Collapsible, Input, Label, Select
from test_tui import FakeService

from pi_sandbox_factory import aws_profiles, sandbox
from pi_sandbox_factory.aws_ui import AWSProfilePicker, AWSProfileTargets
from pi_sandbox_factory.sandbox_options import write_json
from pi_sandbox_factory.sandbox_tui import Cockpit, Confirm, DEFAULT_SETTINGS, SettingsForm, UpdateOperation, preset
from pi_sandbox_factory.tui_widgets import Checkbox


class AWSProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        directory = patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(self.root / 'library'))
        directory.start()
        self.addCleanup(directory.stop)
        self.grant = dict(profile='work', role='arn:aws:iam::123456789012:role/Test', region='eu-west-1',
                          grants=[{'Action': 'ec2:DescribeRegions', 'Resource': '*'}])
        self.original = aws_profiles.save('Research', self.grant)
        for name in ['alpha', 'beta']:
            write_json(self.root / name / 'config.json', dict(name=name, aws_access=self.original))

    def test_independent_copy_and_concurrent_edits(self):
        copy = aws_profiles.save('Research copy', self.original)
        edited = aws_profiles.save('Regions', dict(self.grant, region='eu-west-2'), original=self.original)
        self.assertEqual(edited['profile_id'], self.original['profile_id'])
        self.assertNotEqual(edited['profile_id'], copy['profile_id'])
        self.assertEqual(aws_profiles.entries()[copy['profile_id']], copy)
        self.assertEqual(aws_profiles.pending(self.root, copy), {})
        with self.assertRaisesRegex(ValueError, 'Another edit'):
            aws_profiles.save('Earlier edit', self.grant, original=self.original)
        with self.assertRaisesRegex(ValueError, 'already uses'):
            aws_profiles.save('REGIONS', self.grant)

    def test_partial_failure_retry_and_reassignment(self):
        edit = aws_profiles.save('Regions', self.grant, original=self.original)
        targets = aws_profiles.pending(self.root, edit)
        service = Mock()

        def update(name, changes, log):
            if name == 'alpha':
                raise ValueError('Sign in again')
            write_json(self.root / name / 'config.json', dict(name=name, aws_access=changes['aws_access']))

        service.update_settings.side_effect = update
        with self.assertRaisesRegex(ValueError, 'alpha'):
            aws_profiles.apply_selected(service, edit, targets, lambda line: None)
        self.assertEqual(aws_profiles.pending(self.root, edit), {'alpha': self.original})
        with self.assertRaisesRegex(ValueError, 'changed'):
            sandbox.apply_settings({'aws_access': {}}, self.root / 'alpha',
                                   {'aws_access': edit, 'expected_aws_access': self.original})

    async def test_review_assigns_and_clears_aws_access_for_pi_and_native_agents(self):
        app = Cockpit(self.root, FakeService([]))
        with patch('pi_sandbox_factory.updates.available_update', return_value=None):
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                for agent in ('pi', 'codex'):
                    with self.subTest(agent=agent):
                        draft = preset('base', DEFAULT_SETTINGS)
                        draft.update(name='review', workspace=str(self.root), primary_agent=agent,
                                     local_model_ports=None, model_providers={})
                        review = SettingsForm(draft)
                        await app.push_screen(review)
                        section = review.query_one('#aws-access-section', Collapsible)
                        self.assertTrue(section.collapsed)
                        self.assertEqual(section.title, 'AWS access · Off')
                        section.collapsed = False
                        configure = section.query_one('#configure-aws', Button)
                        configure.focus()
                        await pilot.pause()
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertIsInstance(app.screen, AWSProfilePicker)
                        app.screen.query_one('#aws-choice', Select).value = self.original['profile_id']
                        await pilot.pause()
                        app.screen.query_one('#aws-assign', Button).press()
                        await pilot.pause()
                        self.assertEqual(section.title, 'AWS access · Research')
                        self.assertEqual(review.collect()[0]['aws_access'], self.original)
                        configure.press()
                        await pilot.pause()
                        app.screen.query_one('#aws-disconnect', Button).press()
                        await pilot.pause()
                        self.assertEqual(section.title, 'AWS access · Off')
                        self.assertEqual(review.collect()[0]['aws_access'], {})
                        await app.pop_screen()
                self.assertFalse(app.service.calls)

    async def test_edit_then_choose_only_one_sandbox(self):
        service = FakeService([])
        app = Cockpit(self.root, service)
        service.update_settings = Mock()
        with patch('pi_sandbox_factory.updates.available_update', return_value=None):
            async with app.run_test(size=(80, 24)) as pilot:
                await app.push_screen(AWSProfilePicker(self.original))
                await pilot.pause()
                self.assertEqual(app.screen.query_one(Select).value, self.original['profile_id'])
                self.assertTrue(app.screen.query_one('#aws-assign', Button).disabled)
                box = app.screen.query_one('#confirm-box').content_region
                for selector in ('#aws-assigned', '#aws-disconnect', '#aws-cancel', '#aws-assign'):
                    self.assertTrue(box.contains_region(app.screen.query_one(selector).region), selector)
                fields = app.screen.query_one('.aws-profile-body')
                self.assertTrue(fields.content_region.contains_region(fields.query_one(Label).region))
                app.screen.query_one('#aws-edit', Button).press()
                await pilot.pause()
                app.screen.query_one('#aws-region', Input).value = 'eu-west-2'
                app.screen.query_one('#aws-save', Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen, AWSProfileTargets)
                self.assertTrue(app.screen.query_one('#aws-apply-selected').disabled)
                first, second = app.screen.query(Checkbox)
                self.assertEqual((first.name, second.name), ('alpha', 'beta'))
                self.assertFalse(first.value or second.value)
                second.value = True
                await pilot.pause()
                app.screen.query_one('#aws-apply-selected', Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                service.update_settings.assert_called_once()
                name, changes, _ = service.update_settings.call_args.args
                self.assertEqual(name, 'beta')
                self.assertEqual(changes['aws_access']['region'], 'eu-west-2')
                self.assertEqual(changes['expected_aws_access'], self.original)

    async def test_assignment_confirms_only_the_disruptive_path(self):
        config = dict(name='alpha', engine='podman', profile=sandbox.load_profile('base'), workspace=str(self.root))
        record = dict(name='alpha', config=config, profile='base', engine='podman', workspace=str(self.root),
                      dev='running', proxy='running')
        service = FakeService([record])
        service.update_settings = Mock()
        app = Cockpit(self.root, service)
        with patch('pi_sandbox_factory.updates.available_update', return_value=None), \
                patch.object(sandbox, 'aws_installation', side_effect=[('restart', {}), ('ready', {})]):
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                for expected in (Confirm, UpdateOperation):
                    app.perform_action('aws-access', record=record)
                    await pilot.pause()
                    app.screen.query_one('#aws-choice', Select).value = self.original['profile_id']
                    await pilot.pause()
                    app.screen.query_one('#aws-assign', Button).press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, expected)
                    if expected is Confirm:
                        self.assertIn('saved chats are kept', app.screen.message)
                        self.assertIn('files in /tmp', app.screen.message)
                        self.assertEqual(app.screen.action, 'Restart and connect')
                        self.assertEqual(app.focused.id, 'cancel')
                        await pilot.press('enter')
                        await pilot.pause()
                        service.update_settings.assert_not_called()
                    else:
                        await app.workers.wait_for_complete()
                        service.update_settings.assert_called_once()
                        self.assertEqual(service.update_settings.call_args.args[1]['expected_aws_installation'], 'ready')
