"""Document search uses the current model connections without applying the draft."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, Label, Select

from pi_sandbox_factory import embedding_models
from pi_sandbox_factory.models_ui import HostedProviders
from pi_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, SettingsForm, preset
from test_tui import FakeService


def embedding_provider_fixture():
    return {'name': 'example', 'settings': {}, 'manifest': {
        'description': 'Example Models', 'settings': {'key': {'type': 'secret'}},
        'inference': {'origin': 'https://models.example.test', 'auth': {'type': 'bearer'},
                      'basePath': '/v1', 'operations': [{'method': 'GET', 'path': '/v1/models'}],
                      'embeddingCatalogue': {'label': 'Example Models', 'basePath': '/v1', 'path': '/v1/models'}},
    }}


class DocumentSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Cockpit(Path(self.temp.name), FakeService([]))
        self.draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
        self.draft.update(name='research', workspace=self.temp.name, model_providers=[embedding_provider_fixture()])
        self.addCleanup(patch.stopall)
        patch('pi_sandbox_factory.updates.available_update', return_value=None).start()

    async def open_form(self, pilot, editing=False):
        form = SettingsForm(self.draft, editing=editing, setting='document-search' if editing else None)
        await self.app.push_screen(form)
        if not editing:
            form.query_one('#skills-section').collapsed = False
        await pilot.pause()
        return form

    async def configure(self, form, pilot):
        form.query_one('#configure-document-search').focus()
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        return self.app.screen

    async def test_new_provider_choices_and_unsaved_keys_remain_in_the_draft(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            form = await self.open_form(pilot)
            form.provider_key_choices['example'] = {'key': 'fixture-plugin-key'}
            form.query_one(HostedProviders).changes['openrouter'] = {
                'provider': 'openrouter', 'credential': {'ref': 'fixture-hosted-ref'}}
            with patch.object(embedding_models, 'catalogue', return_value=['embedding-a', 'embedding-b']) as load:
                dialog = await self.configure(form, pilot)
                load.assert_not_called()
                dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(str(dialog.query_one('#knowledge-model-help').render()),
                                 'An embedding model can be used to find passages even when they use different words from your search.')
                provider = dialog.query_one('#embedding-provider', Select)
                for key, credential in [('example', {'key': 'fixture-plugin-key'}), ('openrouter', {'ref': 'fixture-hosted-ref'})]:
                    provider.value = key
                    await pilot.pause()
                    await self.app.workers.wait_for_complete()
                    request = next(call for call in load.call_args_list if call.args[1] == key)
                    self.assertEqual(request.kwargs, {'choice': credential, 'saved_config': None})
                    self.assertNotIn('fixture-plugin-key', repr(request.args[0]))
                    self.assertNotIn('fixture-hosted-ref', repr(request.args[0]))
                dialog.query_one('#embedding-catalogue', Select).value = 'embedding-b'
                await pilot.pause()
                self.assertFalse(dialog.query_one('#save-document-search', Button).disabled)
                await pilot.click('#save-document-search')
                await pilot.pause()
                self.assertEqual(form.document_search['knowledge_embedding_provider'], 'openrouter')
                self.assertEqual(form.document_search['knowledge_embedding_model'], 'embedding-b')
                self.assertNotIn('fixture-hosted-ref', repr(form.document_search))
                self.assertEqual(self.draft['options']['knowledge_embedding'], 'off')
                dialog = await self.configure(form, pilot)
                self.assertEqual(dialog.query_one('#embedding-catalogue', Select).value, 'embedding-b')
                dialog.query_one('#embedding-provider', Select).value = '@custom'
                await pilot.pause()
                self.assertTrue(dialog.query_one('#embedding-url-setting').display)
                self.assertTrue(dialog.query_one('#save-document-search').disabled)
                await pilot.press('escape')
                self.assertEqual(form.document_search['knowledge_embedding_provider'], 'openrouter')

    async def test_plugin_without_catalogue_explains_update_for_each_workflow(self):
        del self.draft['model_providers'][0]['manifest']['inference']['embeddingCatalogue']
        for editing in (False, True):
            self.app = Cockpit(Path(self.temp.name), FakeService([]))
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.open_form(pilot, editing=editing)
                dialog = await self.configure(form, pilot)
                help_field = dialog.query_one('#knowledge-model-help')
                self.assertNotIn('Library', str(help_field.render()))
                dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
                await pilot.pause()
                help_text = str(help_field.render())
                self.assertIn('This version of Example Models does not list embedding models', help_text)
                self.assertIn('Library → Providers', help_text)
                self.assertEqual('Update sources and recreate' in help_text, editing)
                self.assertNotIn('example', dialog.sources)

    async def test_visible_fields_have_blank_rows_above_and_below_at_all_sizes(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(Path(self.temp.name), FakeService([]))
            async with app.run_test(size=size) as pilot:
                form = SettingsForm(self.draft)
                await app.push_screen(form)
                form.query_one('#skills-section').collapsed = False
                await pilot.pause()
                form.query_one('#configure-document-search').focus()
                with patch.object(embedding_models, 'catalogue', return_value=['example-embedding-v2']):
                    await pilot.press('enter')
                    await pilot.pause()
                    dialog = app.screen
                    actions = dialog.query_one('#save-document-search', Button)
                    self.assertEqual(actions.region.y - dialog.query_one('#knowledge-model-help').region.bottom, 1)
                    dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    for provider, fields in (('example', ['knowledge-embedding', 'embedding-provider', 'embedding-catalogue']),
                                             ('@custom', ['knowledge-embedding', 'embedding-provider', 'embedding-url', 'embedding-model'])):
                        dialog.query_one('#embedding-provider', Select).value = provider
                        await pilot.pause()
                        controls = [control for control in dialog.query('Input, Select')
                                    if control.display and all(parent.display for parent in control.ancestors)]
                        self.assertEqual({control.id for control in controls}, set(fields))
                        viewport = dialog.query_one('#document-search-fields')
                        self.assertEqual(viewport.max_scroll_y, 0, (size, provider, 'unnecessary scrolling'))
                        self.assertEqual(viewport.scroll_y, 0)
                        heading = dialog.query_one('#confirm-box > Label', Label)
                        if heading.display:
                            self.assertGreaterEqual(heading.region.y, 0)
                            self.assertLessEqual(heading.region.bottom, viewport.region.y)
                        else:
                            box = dialog.query_one('#confirm-box')
                            self.assertEqual(box.border_title, 'Document search')
                            self.assertEqual(viewport.region.y - box.region.y, 2)
                        for button in dialog.query('.dialog-actions Button'):
                            self.assertIs(dialog.get_widget_at(button.region.x, button.region.y)[0], button)
                            self.assertLessEqual(button.region.bottom, size[1])
                        previous_bottom = None
                        for control in controls:
                            identifier = control.id
                            group = control.parent
                            label = group.query_one(Label)
                            if previous_bottom is not None:
                                self.assertEqual(label.region.y - previous_bottom, 1,
                                                 (size, provider, identifier, 'between groups'))
                            previous_bottom = max(child.region.bottom for child in group.children if child.display)
                            self.assertEqual(control.region.y - label.region.bottom, 1,
                                             (size, provider, identifier, 'above'))
                            self.assertGreaterEqual(group.region.bottom - control.region.bottom, 1,
                                                    (size, provider, identifier, 'below'))
                            adjacent = [widget for widget in group.children if widget.display and widget.region.y >= control.region.bottom]
                            if adjacent:
                                self.assertEqual(adjacent[0].region.y - control.region.bottom, 1,
                                                 (size, provider, identifier, 'help'))
                        footer = dialog.query_one('#save-document-search', Button)
                        self.assertEqual(footer.region.y - previous_bottom, 1,
                                         (size, provider, 'last field to actions'))
                        self.assertGreaterEqual(footer.region.y, viewport.region.bottom)
                        self.assertLessEqual(footer.region.bottom, dialog.query_one('#confirm-box').content_region.bottom)
                        self.assertLessEqual(dialog.query_one('#confirm-box').region.bottom, size[1] - 1)
                    field = dialog.query_one('#embedding-url', Input)
                    field.focus()
                    field.value = 'invalid-url'
                    await pilot.pause()
                    feedback = dialog.query_one('#embedding-url-error')
                    next_label = dialog.query_one('#embedding-model-setting Label')
                    self.assertTrue(feedback.display)
                    self.assertEqual(next_label.region.y - feedback.region.bottom, 1)
                    self.assertTrue(actions.disabled)
                    if size[1] < 32:
                        self.assertEqual(actions.region.y - viewport.region.bottom, 1)
                    self.assertLessEqual(actions.region.bottom, dialog.query_one('#confirm-box').content_region.bottom)
                    self.assertLessEqual(dialog.query_one('#confirm-box').region.bottom, size[1] - 1)

    async def test_saved_provider_empty_offline_retry_and_typed_fallback(self):
        self.draft['options'].update(knowledge_embedding='endpoint', knowledge_embedding_provider='example',
                                    knowledge_embedding_url='https://models.example.test/v1', knowledge_embedding_model='saved-id')
        async with self.app.run_test(size=(80, 24)) as pilot:
            form = await self.open_form(pilot, editing=True)
            with patch.object(embedding_models, 'catalogue', side_effect=[ValueError('Offline'), [], ['new-id']]) as load:
                dialog = await self.configure(form, pilot)
                await self.app.workers.wait_for_complete()
                self.assertIs(load.call_args.kwargs['saved_config'], self.draft)
                self.assertIsNone(load.call_args.kwargs['choice'])
                field = dialog.query_one('#embedding-model', Input)
                self.assertEqual(field.value, 'saved-id')
                self.assertTrue(dialog.query_one('#retry-embedding-catalogue').display)
                for button_id in ('cancel-document-search', 'save-document-search'):
                    button = dialog.query_one('#' + button_id, Button)
                    self.assertLessEqual(button.region.bottom, dialog.query_one('#confirm-box').content_region.bottom)
                    self.assertGreater(button.region.y, dialog.query_one('#document-search-fields').region.y)
                field.value = 'typed-id'
                dialog.query_one('#retry-embedding-catalogue').focus()
                await pilot.pause()
                await pilot.press('enter')
                await self.app.workers.wait_for_complete()
                self.assertIn('No embedding models', str(dialog.query_one('#embedding-catalogue-message').render()))
                self.assertEqual(field.value, 'typed-id')
                await pilot.pause(dialog.query_one('#retry-embedding-catalogue', Button).active_effect_duration)
                dialog.query_one('#retry-embedding-catalogue').focus()
                await pilot.press('enter')
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(load.call_count, 3)
                self.assertEqual(dialog.query_one('#embedding-catalogue', Select).value, 'typed-id')
                await pilot.click('#save-document-search')
                await pilot.pause()
                self.assertEqual(form.document_search['knowledge_embedding_model'], 'typed-id')
                self.assertEqual(self.draft['options']['knowledge_embedding_model'], 'saved-id')
