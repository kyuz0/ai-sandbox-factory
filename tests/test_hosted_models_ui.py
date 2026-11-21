import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from textual.widgets import Button, Collapsible, Input, OptionList, Select, Static
from pi_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, ModelsScreen, SettingsForm, preset
from pi_sandbox_factory.models_ui import HostedProviders, ModelDialog, ProviderDialog
from pi_sandbox_factory.credentials_ui import CredentialDialog, LocalKeysDialog
from pi_sandbox_factory.tui_widgets import Checkbox
from pi_sandbox_factory import editor_settings, hosted_models, inference, provider_credentials
from test_tui import FakeService


class ModelService(FakeService):
    def hosted_request(self, name, action, **values):
        if action == 'status': return {'providers':[{'provider':'openrouter','configured':True,'has_key':True}]}
        return {'models':[{'id':'model-1','name':'Example model','api':'openai-completions','contextWindow':128000,
                          'maxTokens':8192,'input':['text','image'],'reasoning':True}], 'warning':''}
    def hosted_select(self,*args,**kwargs): self.calls.append((args,kwargs))
    def hosted_test(self,*args,**kwargs): self.calls.append(('test',args,kwargs));return {'ok':True}
    def hosted_save(self,name,value):
        return {'provider':value['provider'],'configured':True,'has_key':True}


class HostedUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.runner=asyncio.Runner()
    @classmethod
    def tearDownClass(cls): cls.runner.close()
    def _callTestMethod(self,method): self.runner.run(method())

    async def test_post_creation_reviewer_choice_survives_skipping_chat_default(self):
        from pi_sandbox_factory.reviewer_ui import ReviewerFields
        service = ModelService([])
        service.plugin_model = Mock(side_effect=service.hosted_request)
        service.approval_models = lambda name: [
            {'provider': 'chaucer', 'id': 'main', 'name': 'Main'},
            {'provider': 'openrouter', 'id': 'other/model', 'name': 'Other'},
        ]
        with tempfile.TemporaryDirectory() as temp, patch('pi_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(temp), service)
            async with app.run_test(size=(80, 24)) as pilot:
                dialog = ModelDialog('test', 'chaucer', plugin_label='Chaucer', optional=True, approval_model='current')
                await app.push_screen(dialog)
                await app.workers.wait_for_complete()
                dialog.query_one('#review-current').focus()
                await pilot.press('space')
                app.screen.query_one(Select).value = 'openrouter'
                await pilot.pause()
                app.screen.query_one(Input).focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertEqual(dialog.query_one(ReviewerFields).selected, 'openrouter/other/model')
                await pilot.click('#model-cancel')
                self.assertEqual(service.calls, [['approval-model', 'test', 'openrouter/other/model']])
                self.assertTrue(all(call.args[1] == 'catalogue' for call in service.plugin_model.call_args_list))

    async def test_managed_plugin_key_opens_default_model_picker(self):
        source = {'name': 'company', 'manifest': {
            'description': 'Company models',
            'inference': {'origin': 'https://example.test', 'auth': {'type': 'bearer'},
                          'operations': [{'method': 'GET', 'path': '/models'}]},
        }}
        record = {'name': 'test', 'config': {'model_providers': [source]}}
        with tempfile.TemporaryDirectory() as temp, patch('pi_sandbox_factory.updates.available_update', return_value=None):
            service = ModelService([])
            configured = []
            service.provider_key_status = lambda name: {'configured': list(configured)}
            service.assign_provider_key = lambda name, provider, choice, log: configured.append(provider)
            request = service.plugin_model = Mock(side_effect=service.hosted_request)
            app = Cockpit(Path(temp), service)
            async with app.run_test(size=(80, 24)) as pilot:
                screen = ModelsScreen(record)
                await app.push_screen(screen)
                await app.workers.wait_for_complete()
                await pilot.pause()
                screen.change_key('company', {'ref': 'a' * 32})
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelDialog)
                self.assertEqual(str(app.screen.query_one('#model-cancel', Button).label), 'Skip')
                await pilot.press('e', 'x')
                self.assertEqual(app.screen.query_one('#model-search', Input).value, 'ex')
                request.assert_called_once_with('test', 'catalogue', provider='company', refresh=False)
                await pilot.click('#model-cancel')
                self.assertIs(app.screen, screen)
                screen.query_one('#provider-controls', Collapsible).collapsed = False
                screen.query_one('#provider-model-choice', Button).focus()
                await pilot.press('enter')
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelDialog)
                app.screen.query_one('#model-results', OptionList).highlighted = 0
                await pilot.pause()
                await pilot.click('#model-save')
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen, screen)
                request.assert_called_with('test', 'select', provider='company', model='model-1')

    async def test_saved_key_picker_returns_a_reference_and_filters_by_connection(self):
        binding = hosted_models.credential_scope({'provider': 'openrouter'})
        work, test = 'a' * 32, 'b' * 32
        records = {work: {'label':'Work', 'scope':binding}, test: {'label':'Test', 'scope':binding}}
        with tempfile.TemporaryDirectory() as temp, \
             patch('pi_sandbox_factory.updates.available_update', return_value=None), \
             patch.object(provider_credentials, 'entries', side_effect=lambda scope: records if scope == binding else {}):
            app = Cockpit(Path(temp), ModelService([]))
            chosen = []
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause()
                await app.push_screen(ProviderDialog(), chosen.append)
                await pilot.pause()
                picker = app.screen.query_one('#saved-key', Select)
                remember = app.screen.query_one('#remember-key')
                key = app.screen.query_one('#hosted-key', Input)
                self.assertEqual(remember.region.x, key.region.x)
                self.assertEqual(remember.region.y, key.region.bottom + 1)
                self.assertLessEqual(remember.region.bottom,
                                     app.screen.query_one('.model-fields').content_region.bottom)
                picker.value = work
                await pilot.pause()
                self.assertFalse(app.screen.query_one('#credential-new').display)
                self.assertEqual(app.screen.query_one('#hosted-key', Input).value, '')
                app.screen.query_one('#hosted-provider', Select).value = 'together'
                await pilot.pause()
                self.assertFalse(app.screen.query_one('#credential-saved').display)
                self.assertEqual(picker.value, 'new')
                app.screen.query_one('#hosted-provider', Select).value = 'openrouter'
                await pilot.pause()
                picker.value = test
                app.screen.query_one('#hosted-save', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertEqual(chosen[0]['credential'], {'ref': test})
                self.assertFalse(chosen[0]['key'])

                await app.push_screen(CredentialDialog('Company models', binding), chosen.append)
                await pilot.pause()
                app.screen.query_one('#credential-apply', Button).focus()
                await pilot.press('enter')
                await pilot.pause(0.3)
                self.assertIs(app.focused, app.screen.query_one('#managed-key', Input))
                feedback = app.screen.query_one('#credential-input-error', Static)
                self.assertIn('Enter an API key.', str(feedback.render()))
                self.assertEqual(feedback.region.y - app.focused.region.bottom, 1)
                app.screen.query_one('#remember-key', Checkbox).value = True
                app.screen.query_one('#replace-key', Select).value = work
                await pilot.pause()
                apply = app.screen.query_one('#credential-apply', Button)
                self.assertIs(app.screen.get_widget_at(apply.region.x, apply.region.y)[0], apply)
                app.screen.query_one('#saved-key', Select).value = work
                app.screen.query_one('#credential-apply', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertEqual(chosen[-1], {'ref': work})

    async def test_key_only_save_does_not_show_unrelated_model_or_reload_steps(self):
        with tempfile.TemporaryDirectory() as temp, patch('pi_sandbox_factory.updates.available_update',return_value=None), patch('pi_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
            root=Path(temp);(root/'test').mkdir()
            editor_settings.save(root/'test',{'mode':'pi'})
            app=Cockpit(root,ModelService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause();await app.workers.wait_for_complete()
                await app.push_screen(ModelsScreen({'name':'test','config':{'model_providers':[]}}))
                await pilot.pause();await app.workers.wait_for_complete()
                form=app.screen.query_one(HostedProviders)
                form.save({'provider':'openrouter','key':'fixture-only'})
                await app.workers.wait_for_complete();await pilot.pause()
                self.assertIsInstance(app.screen,ModelsScreen)
                message=str(form.query_one('#hosted-message',Static).render())
                self.assertIn('subsequent requests',message)
                for irrelevant in ('reload','Reopen','VS Code','Choose a model','fixture-only'):
                    self.assertNotIn(irrelevant,message)
                form.save({'provider':'together','key':'fixture-only'})
                await app.workers.wait_for_complete();await pilot.pause()
                self.assertIsInstance(app.screen,ModelDialog)
                message=str(form.query_one('#hosted-message',Static).render())
                self.assertIn('Restart Pi CLI',message)
                self.assertNotIn('VS Code',message)
                self.assertTrue(all(line.startswith('• ') for line in message.splitlines()))

    async def test_create_can_assign_separate_local_keys_and_removing_ports_filters_draft(self):
        work = 'a' * 32
        binding = provider_credentials.scope('local', inference.local_route(8000))
        records = {work: {'label': 'Work', 'scope': binding}}
        with tempfile.TemporaryDirectory() as temp, \
             patch('pi_sandbox_factory.updates.available_update', return_value=None), \
             patch('pi_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), \
             patch.object(provider_credentials, 'entries', side_effect=lambda scope: records if scope == binding else {}):
            app = Cockpit(Path(temp), ModelService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='test', workspace=temp, local_model_ports='8000-8001')
                await app.push_screen(SettingsForm(draft, section='hosted-section'))
                await pilot.pause()
                form = app.screen
                local = form.query_one('#local-provider', Collapsible)
                self.assertTrue(local.collapsed)
                local.query_one('CollapsibleTitle').focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertFalse(local.collapsed)
                form.query_one('#local-server-keys', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertIsInstance(app.screen, LocalKeysDialog)
                dialog = app.screen
                port = app.screen.query_one('#local-key-port', Select)
                port.focus()
                await pilot.press('enter')
                self.assertTrue(port.expanded)
                await pilot.press('escape')
                self.assertIs(app.screen, dialog)
                self.assertFalse(port.expanded)
                app.screen.query_one('#local-key-choose', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                app.screen.query_one('#remember-key', Checkbox).value = True
                app.screen.query_one('#replace-key', Select).value = work
                await pilot.pause()
                apply = app.screen.query_one('#credential-apply', Button)
                self.assertIs(app.screen.get_widget_at(apply.region.x, apply.region.y)[0], apply)
                app.screen.query_one('#saved-key', Select).value = work
                app.screen.query_one('#credential-apply', Button).focus()
                await pilot.press('enter')
                await pilot.pause(0.3)
                self.assertEqual(str(app.screen.query_one('#local-key-choose', Button).label), 'Change key…')
                port.value = 8001
                await pilot.pause()
                self.assertEqual(str(app.screen.query_one('#local-key-choose', Button).label), 'Choose key…')
                app.screen.query_one('#local-key-choose', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                self.assertFalse(app.screen.query_one('#credential-saved').display)
                app.screen.query_one('#managed-key', Input).value = 'synthetic-8001-key'
                app.screen.query_one('#credential-apply', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                app.screen.query_one('#local-key-done', Button).focus()
                await pilot.press('enter')
                await pilot.pause()
                selected, _ = form.collect()
                self.assertEqual(selected['_local_model_keys'], {8000: {'ref': work}, 8001: {'key': 'synthetic-8001-key'}})
                form.query_one('#model-ports', Input).value = '8000'
                selected, _ = form.collect()
                self.assertEqual(selected['_local_model_keys'], {8000: {'ref': work}})

    async def test_create_edit_model_selection_and_small_terminal(self):
        for size in [(80,24),(120,40),(220,50)]:
            with tempfile.TemporaryDirectory() as temp, patch('pi_sandbox_factory.updates.available_update',return_value=None), patch('pi_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
                service=ModelService([]);app=Cockpit(Path(temp),service)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause();await app.workers.wait_for_complete()
                    draft=preset('base',copy.deepcopy(DEFAULT_SETTINGS));draft.update(name='test',workspace=temp)
                    await app.push_screen(SettingsForm(draft,section='hosted-section'));await pilot.pause()
                    form=app.screen.query_one(HostedProviders)
                    names = form.query_one('#hosted-available', Static)
                    self.assertEqual(str(names.render()), ', '.join(hosted_models.PROVIDERS.values()))
                    form.query_one('#hosted-add',Button).focus();await pilot.press('enter');await pilot.pause()
                    self.assertIsInstance(app.screen,ProviderDialog)
                    remember = app.screen.query_one('#remember-key', Checkbox)
                    key = app.screen.query_one('#hosted-key', Input)
                    self.assertEqual(remember.region.x, key.region.x)
                    self.assertEqual(remember.region.y, key.region.bottom + 1)
                    label = str(remember.label)
                    remember.value = True
                    await pilot.pause()
                    self.assertEqual(str(remember.label), label)
                    key_name = app.screen.query_one('#key-label', Input)
                    self.assertEqual(key_name.region.x, remember.region.x)
                    remember.value = False
                    app.screen.query_one('#hosted-provider',Select).value='amazon-bedrock';await pilot.pause()
                    app.screen.query_one('#hosted-method',Select).value='iam';await pilot.pause()
                    self.assertTrue(app.screen.query_one('#hosted-secret_key',Input).password)
                    save=app.screen.query_one('#hosted-save',Button)
                    self.assertLess(save.region.bottom,size[1])
                    save.focus();await pilot.press('enter');await pilot.pause()
                    self.assertIn('region',str(app.screen.query_one('#hosted-error',Static).render()))
                    await pilot.press('escape');await pilot.pause();self.assertFalse(form.changes)
                    form.query_one('#hosted-add',Button).focus();await pilot.press('enter');await pilot.pause()
                    app.screen.query_one('#hosted-key',Input).value='fixture-key'
                    app.screen.query_one('#hosted-save',Button).focus();await pilot.press('enter');await pilot.pause()
                    self.assertEqual(form.changes['openrouter']['key'],'fixture-key')
                    self.assertEqual(str(names.render()), 'Available to connect · Together, Azure / Foundry, AWS Bedrock')
                    form.query_one('#hosted-add', Button).focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one('#hosted-provider', Select).value, 'together')
                    await pilot.press('escape')
                    await pilot.press('escape');await pilot.pause()
                    await app.push_screen(ModelsScreen({'name':'test','config':{'model_providers':[]}}));await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    await app.push_screen(ModelDialog('test','azure'));await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertTrue(app.screen.query_one('#model-save',Button).disabled)
                    self.assertTrue(app.screen.query_one('#model-test',Button).disabled)
                    app.screen.query_one('#model-results',OptionList).highlighted=0;await pilot.pause()
                    app.screen.query_one('#model-deployment',Input).value='my-deployment'
                    app.screen.query_one('#model-test',Button).focus();await pilot.press('enter');await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertEqual(service.calls[-1][0],'test')
                    app.screen.query_one('#model-save',Button).focus();await pilot.press('enter');await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertEqual(service.calls[-1][1]['deployment'],'my-deployment')
