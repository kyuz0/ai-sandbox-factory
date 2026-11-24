import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Button, Collapsible

from ai_sandbox_factory import creation, registry, sandbox
from ai_sandbox_factory.sandbox_tui import Checkbox, Cockpit, ConfirmWithoutModel, DEFAULT_SETTINGS, SettingsForm, preset
from test_tui import FakeService


class ModelSetupWarningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch('ai_sandbox_factory.updates.available_update', return_value=None))
        self.enterContext(patch.dict('os.environ', {'SANDBOX_REGISTRY_DIR': str(self.root / 'packages')}))
        self.service = FakeService([])
        self.service.check_creation = Mock(return_value=creation.ResourceConflicts('podman', None, (), ()))
        self.app = Cockpit(self.root / 'state', self.service)
        self.draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
        self.draft.update(name='model-setup', workspace=str(self.root), engine='podman')
        self.submitted = []

    async def ready(self, pilot):
        await pilot.pause()
        await self.app.workers.wait_for_complete()
        await pilot.pause()

    async def open_review(self, pilot):
        form = SettingsForm(self.draft, on_create=self.submitted.append)
        await self.app.push_screen(form)
        await pilot.pause()
        return form

    async def test_return_to_model_providers_and_continue_after_configuring(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot)
            form = await self.open_review(pilot)
            models = form.query_one('#hosted-section', Collapsible)
            form.query_one('#open-editor', Checkbox).value = False
            for key in ('escape', 'enter'):
                models.collapsed = True
                await pilot.click('#apply')
                await pilot.pause()
                self.assertIsInstance(self.app.screen, ConfirmWithoutModel)
                self.assertEqual(self.app.focused.id, 'cancel')
                await pilot.press(key)
                await pilot.pause()
                self.assertIs(self.app.screen, form)
                self.assertFalse(models.collapsed)
                self.assertIs(self.app.focused, models.query_one('#local-provider > CollapsibleTitle'))
                self.assertFalse(form.query_one('#open-editor', Checkbox).value)
                self.service.check_creation.assert_not_called()
                self.assertFalse(self.submitted)

            await pilot.press('enter')
            await pilot.pause()
            self.assertFalse(models.collapsed)
            self.assertFalse(models.query_one('#local-provider', Collapsible).collapsed)
            form.query_one('#local-models', Checkbox).value = True
            await pilot.click('#apply')
            await self.ready(pilot)
            self.service.check_creation.assert_called_once()
            self.assertEqual([request.draft['local_model_ports'] for request in self.submitted], ['8000'])

    async def test_explicitly_continue_without_a_model(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot)
            form = await self.open_review(pilot)
            create = form.query_one('#apply', Button)
            create.press()
            create.press()
            await pilot.pause()
            self.assertIsInstance(self.app.screen, ConfirmWithoutModel)
            self.assertEqual(len(self.app.screen_stack), 3)
            self.service.check_creation.assert_not_called()
            self.assertEqual(str(self.app.screen.query_one('#yes', Button).label), 'Create without a model')
            await pilot.click('#yes')
            await self.ready(pilot)
            self.service.check_creation.assert_called_once()
            self.assertEqual([request.draft['model_providers'] for request in self.submitted], [{}])

    async def test_hosted_plugin_and_shared_pi_config_skip_warning(self):
        original_choices, original_resolve = registry.choices, registry.resolve
        provider = sandbox.ROOT.parent / 'examples' / 'provider'
        (self.root / 'project').mkdir()
        (self.root / 'shared-pi').mkdir()
        with patch.object(registry, 'choices', side_effect=lambda kind: {'company': 'Company models'}
                          if kind == 'provider' else original_choices(kind)), \
                patch.object(registry, 'resolve', side_effect=lambda kind, name: provider
                             if (kind, name) == ('provider', 'company') else original_resolve(kind, name)):
            async with self.app.run_test(size=(120, 40)) as pilot:
                await self.ready(pilot)
                for configured in ({'_hosted_changes': {'openrouter': {'provider': 'openrouter'}}},
                                   {'model_providers': {'company': {'model': 'fixture'}}},
                                   {'pi_config': str(self.root / 'shared-pi')}):
                    self.draft = {**preset('base', copy.deepcopy(DEFAULT_SETTINGS)),
                                  'name': 'model-setup', 'workspace': str(self.root / 'project'), **configured}
                    form = await self.open_review(pilot)
                    await pilot.click('#apply')
                    await self.ready(pilot)
                    self.assertIsNot(self.app.screen, form, configured)
                    self.assertNotIsInstance(self.app.screen, ConfirmWithoutModel)
                self.assertEqual(len(self.submitted), 3)
