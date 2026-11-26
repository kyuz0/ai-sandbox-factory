"""Image setup in the creation review, workstation defaults and saved sandbox settings."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Input, Label, OptionList, Select, Static

from ai_sandbox_factory import operations, sandbox
from ai_sandbox_factory.sandbox_tui import ApplySettingsReview, Cockpit, FactoryUpdateDetails, SettingsForm
from test_tui import FakeService


class ImageSetupUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.state = self.root / 'state'
        (self.state / 'demo').mkdir(parents=True)
        (self.state / 'demo' / 'allowlist.txt').write_text('')
        self.enterContext(patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'library'),
                                                  'XDG_CONFIG_HOME': str(self.root / 'preferences')}))
        self.enterContext(patch('ai_sandbox_factory.updates.available_update', return_value=None))

    async def test_save_defaults_and_review_for_both_agent_forms(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            defaults = SettingsForm(operations.preset('base', app.defaults), defaults=True)
            await app.push_screen(defaults)
            field = defaults.query_one('#image-source', Select)
            self.assertEqual(field.value, 'prebuilt')
            field.value = 'local'
            defaults.query_one('#apply', Button).press()
            await pilot.pause()
            self.assertEqual(operations.load_defaults()['image_source'], 'local')
            for agent in ('pi', 'codex'):
                draft = operations.preset('base', app.defaults)
                draft.update(name='demo', workspace=str(self.workspace), primary_agent=agent)
                review = SettingsForm(draft)
                await app.push_screen(review)
                source = review.query_one('#image-source', Select)
                self.assertEqual(source.value, 'local')
                source.value = 'prebuilt'
                self.assertEqual(review.collect()[0]['image_source'], 'prebuilt')
                await app.pop_screen()

    async def test_existing_sandbox_help_save_and_mixed_resource_changes(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            with self.subTest(size=size):
                service = FakeService([])
                service.update_settings = Mock()
                app = Cockpit(self.state, service)
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    for agent in ('pi', 'codex'):
                        config = {'name': 'demo', 'workspace': str(self.workspace), 'primary_agent': agent,
                                  'engine': 'podman', 'profile': sandbox.load_profile('base'), 'memory': '8g'}
                        service.records = [dict(name='demo', profile='base', engine='podman', dev='running',
                                                proxy='running', workspace=str(self.workspace), config=config)]
                        app.receive_inventory(service.records)
                        await pilot.pause()
                        self.assertTrue(await pilot.click('#settings'))
                        menu = app.screen
                        actions = menu.query_one('#settings-actions', OptionList)
                        actions.focus()
                        actions.highlighted = next(i for i, action in enumerate(menu.actions) if action[1] == 'resources')
                        await pilot.press('enter')
                        await pilot.pause()
                        page = app.screen
                        self.assertIsInstance(page, SettingsForm)
                        self.assertEqual(page.setting, 'resources')
                        source = page.query_one('#image-source', Select)
                        help_button = page.query_one('#image-source-information', Button)
                        apply = page.query_one('#apply', Button)
                        self.assertIs(app.focused, source)
                        self.assertEqual(source.value, 'local')
                        self.assertTrue(page.query_one('.form').content_region.contains_region(source.region))
                        self.assertEqual(source.region.height, 1)
                        self.assertEqual(source.region.y, help_button.region.y)
                        self.assertEqual(source.region.right + 1, help_button.region.x)
                        form = page.query_one('.form')
                        section = page.query_one('#resources-section')
                        self.assertEqual(section.background_colors, form.background_colors)
                        labels = list(section.query(Label))
                        fields = [page.query_one(key) for key in ('#engine', '#image-source', '#memory', '#env-file-field')]
                        if agent == 'pi':
                            fields.append(page.query_one('#pi-config-field'))
                        labels = [label for label in labels if label.parent.has_class('setting')]
                        self.assertEqual(len(labels), len(fields))
                        title = page.query_one('.focused-settings-title').content_region
                        self.assertEqual(labels[0].region.y, title.bottom + 1)
                        self.assertEqual(title.x, 3)
                        self.assertEqual(title.x, form.content_region.x)
                        self.assertEqual(page.query_one('#back').region.x, title.x)
                        self.assertEqual(apply.region.right, form.content_region.right)
                        for label, field in zip(labels, fields):
                            self.assertEqual(label.region.x, title.x)
                            self.assertEqual(field.region.x, label.region.x)
                            self.assertEqual(field.region.y, label.region.bottom + 1)
                            self.assertEqual(field.region.height, 1)
                        engine_help = section.query_one('.setting-help')
                        self.assertEqual(engine_help.region.y, fields[0].region.bottom + 1)
                        self.assertEqual(labels[1].region.y, engine_help.region.bottom + 1)
                        for field, label in zip(fields[1:], labels[2:]):
                            self.assertEqual(label.region.y, field.region.bottom + 1)
                        self.assertLess(fields[2].region.width, source.region.width)
                        self.assertLess(source.region.width, fields[0].region.width)
                        self.assertEqual(fields[3].region.right, section.content_region.right)
                        self.assertFalse(any('cached layers' in str(w.render()) for w in page.query(Static)))
                        source.value = 'prebuilt'
                        memory = page.query_one('#memory', Input)
                        memory.value = '4g'
                        help_button.focus()
                        await pilot.press('enter')
                        self.assertIsInstance(app.screen, FactoryUpdateDetails)
                        self.assertIn('cached layers', app.screen.message)
                        self.assertIn('verification fails', app.screen.message)
                        close = app.screen.query_one('#close-update-details', Button)
                        self.assertTrue(app.screen.query_one('#confirm-box').content_region.contains_region(close.region))
                        await pilot.press('escape' if agent == 'pi' else 'enter')
                        self.assertIs(app.screen, page)
                        self.assertIs(app.focused, help_button)
                        self.assertNotEqual(help_button.styles.background, apply.styles.background)
                        self.assertTrue(help_button.styles.text_style.underline)
                        self.assertEqual((source.value, memory.value), ('prebuilt', '4g'))
                        self.assertEqual(str(apply.label), 'Apply and recreate')
                        self.assertIn('Apply and recreate', apply.render_line(0).text)
                        self.assertTrue(page.query_one('#open-editor').display)
                        apply.press()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, ApplySettingsReview)
                        self.assertIn('Image setup', app.screen.message)
                        self.assertIn('Memory limit', app.screen.message)
                        await pilot.press('escape')
                        env_file = page.query_one('#env-file', Input)
                        env_file.value = str(self.workspace / ('long-project-' * 10) / 'environment')
                        memory.value = '8Gb'
                        self.assertTrue(await pilot.click('#apply'))
                        await pilot.pause()
                        feedback = page.query_one('#form-error')
                        self.assertIn('Memory must be a positive size', str(feedback.render()))
                        self.assertTrue(form.content_region.contains_region(feedback.region))
                        self.assertEqual(apply.region.bottom + 2, size[1])
                        memory.value = '8g'
                        await pilot.pause()
                        self.assertEqual(str(apply.label), 'Apply and recreate')
                        env_file.value = ''
                        await pilot.pause()
                        self.assertEqual(str(apply.label), 'Apply')
                        self.assertEqual(apply.render_line(0).text.strip(), 'Apply')
                        self.assertFalse(page.query_one('#open-editor').display)
                        apply.press()
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        service.update_settings.assert_called_once()
                        self.assertEqual(service.update_settings.call_args.args[:2],
                                         ('demo', {'image_source': 'prebuilt'}))
                        self.assertEqual(service.calls, [])
                        service.update_settings.reset_mock()
                        while len(app.screen_stack) > 1:
                            await app.pop_screen()
