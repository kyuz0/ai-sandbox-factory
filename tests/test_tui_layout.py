"""Layout and navigation contracts for the terminal forms."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Collapsible, Input, Label, OptionList, Select, Static, RichLog
from pi_sandbox_factory import sandbox
from pi_sandbox_factory.sandbox_options import APPROVAL_MODES
from pi_sandbox_factory.sandbox_tui import (
    Cockpit, Confirm, DEFAULT_SETTINGS, FolderPicker, NewSandbox, SettingsForm,
    SettingsMenu, WebAccessForm, preset,
)
from pi_sandbox_factory.folders_ui import FolderEditor
from pi_sandbox_factory.tui_widgets import Checkbox
from test_tui import FakeService
from tui_geometry import check_checkbox_alignment


class TUILayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.runner = asyncio.Runner()
    @classmethod
    def tearDownClass(cls): cls.runner.close()
    def _callTestMethod(self, method): self.runner.run(method())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        state = self.root / 'demo'
        state.mkdir()
        (state / 'allowlist.txt').write_text('local-model.internal:8000\n')
        config = dict(name='demo', engine='podman', network='allowlist',
                      workspace=str(self.workspace), profile=sandbox.load_profile('base'),
                      local_model_ports='8000', memory='8g', subagents=False)
        self.record = dict(name='demo', profile='base', engine='podman', dev='running',
                           proxy='running', workspace=str(self.workspace), config=config)
        for target, kwargs in [
            ('pi_sandbox_factory.sandbox_tui.load_defaults', dict(side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS))),
            ('pi_sandbox_factory.sandbox_tui.search_keys_path', dict(return_value=self.root/'keys.json')),
            ('pi_sandbox_factory.updates.available_update', dict(return_value=None)),
        ]:
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def app(self): return Cockpit(self.root, FakeService([self.record]))
    def draft(self):
        value = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
        value.update(name='demo', workspace=str(self.workspace))
        return value

    async def ready(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_workspace_controls_align_and_remain_reachable(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await pilot.press('f2')
                await pilot.pause()
                field = app.screen.query_one('#workspace', Input)
                browse = app.screen.query_one('#browse', Button)
                checkbox = app.screen.query_one('#mkdir', Checkbox)
                check_checkbox_alignment(app.screen)
                self.assertEqual(browse.region.y, field.region.y)
                self.assertEqual(browse.region.height, field.region.height)
                self.assertEqual(field.region.height, 1)
                self.assertEqual(checkbox.region.y, field.region.bottom + 1)
                self.assertEqual(checkbox.region.x, field.region.x)
                self.assertEqual(checkbox.region.x, app.screen.query_one('#name').region.x)
                primary = app.screen.query_one('#continue', Button)
                actions = app.screen.query_one('.wizard-actions')
                self.assertEqual(primary.region.right, actions.content_region.right)
                self.assertEqual(primary.region.height, 3 if size[1] >= 32 else 1)
                self.assertLess(primary.region.bottom, app.screen.query_one('Footer').region.y)
                app.screen.query_one('#name', Input).focus()
                await pilot.pause()
                self.assertLessEqual(app.screen.query_one('#name').region.bottom,
                                     app.screen.query_one('.wizard-actions').region.y)
                await pilot.press('escape')
                await pilot.press('q')

    async def test_review_uses_terminal_width_with_padded_actions(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(SettingsForm(self.draft()))
                app.screen.query_one('#folders-section', Collapsible).collapsed = False
                await pilot.pause()
                form = app.screen.query_one('.form')
                actions = app.screen.query_one('.wizard-actions')
                primary = actions.query_one('Button.-primary', Button)
                self.assertEqual(form.region.width, size[0])
                self.assertEqual(actions.region.width, size[0])
                self.assertEqual(actions.content_region.x, 3)
                self.assertEqual(primary.region.right, size[0] - 3)
                self.assertLess(primary.region.bottom,
                                app.screen.query_one('Footer').region.y)
                await pilot.press('escape')
                await pilot.press('q')

    async def test_advisor_labels_order_and_explicit_copy_controls(self):
        from pi_sandbox_factory.advisors_ui import AdvisorChoices
        from pi_sandbox_factory.advisor_credentials import LABELS
        from pi_sandbox_factory.sandbox_tui import AdvisorsScreen
        for creation in (True, False):
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(SettingsForm(self.draft()) if creation else AdvisorsScreen(self.record))
                if creation:
                    app.screen.query_one('#new-advisors-section', Collapsible).collapsed = False
                await pilot.pause()
                choices = app.screen.query_one(AdvisorChoices)
                prefix = 'new-advisor-' if creation else 'advisor-'
                fields = [choices.query_one('#' + prefix + p, Checkbox) for p in LABELS]
                self.assertEqual([str(field.label) for field in fields], list(LABELS.values()))
                self.assertEqual([field.region.y for field in fields], sorted(field.region.y for field in fields))
                copy = choices.query_one('#' + prefix + 'copy-claude', Checkbox)
                self.assertFalse(copy.value)
                self.assertFalse(copy.display)
                choices.query_one('#' + prefix + 'claude', Checkbox).value = True
                await pilot.pause()
                self.assertTrue(copy.display)
                self.assertFalse(copy.disabled)
                check_checkbox_alignment(app.screen)
                parent = choices.query_one('#' + prefix + 'claude', Checkbox)
                self.assertEqual(copy.region.x, parent.region.x)
                self.assertEqual(copy.region.y, parent.region.bottom + 1)
                copy.value = True
                self.assertEqual(choices.imports(), ['claude'])
                if not creation:
                    self.assertFalse(app.screen.query_one('#advisor-claude-model').disabled)
                    self.assertTrue(app.screen.query_one('#advisor-model').disabled)
                    calls = []
                    app.service.advisors = lambda name, settings, log, update=None: calls.append(('save', settings['providers']))
                    app.service.run = lambda args, log: calls.append(args)
                    await pilot.click('#advisor-apply')
                    await pilot.pause()
                    await pilot.click('#yes')
                    await self.ready(app, pilot)
                    self.assertEqual(calls, [('save', ['claude']), ['advisor-import', 'demo', 'claude']])
                else:
                    choices.query_one('#' + prefix + 'claude', Checkbox).value = False
                    await pilot.pause()
                    self.assertFalse(copy.value)
                    self.assertEqual(choices.imports(), [])
                await pilot.press('q')

    async def test_folder_and_confirmation_dialogs_cancel_with_escape(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            result = []
            await app.push_screen(Confirm('Recreate this sandbox?', 'Recreate'), result.append)
            await pilot.press('escape')
            self.assertEqual(result, [False])
            await app.push_screen(FolderPicker(str(self.workspace)), result.append)
            await pilot.press('escape')
            self.assertIsNone(result[-1])
            await app.push_screen(FolderEditor(None, [], str(self.workspace), self.root))
            await pilot.pause()
            self.assertIsInstance(app.screen.query_one('#folder-read-only'), Checkbox)
            self.assertLess(app.screen.query_one('#folder-cancel').region.x,
                            app.screen.query_one('#folder-save').region.x)
            app.screen.query_one('#folder-error', Static).update('Cannot use this folder. ' * 12)
            await pilot.pause()
            save = app.screen.query_one('#folder-save', Button)
            self.assertLess(save.region.bottom, 24)
            self.assertIs(app.screen.get_widget_at(save.region.x, save.region.y)[0], save)
            await pilot.click('#folder-cancel')
            self.assertIs(app.screen, app.screen_stack[0])
            await pilot.press('q')

    async def test_web_access_is_discoverable_and_opens_its_controls(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await pilot.click('#settings')
            menu = app.screen.query_one('#settings-actions', OptionList)
            menu.highlighted = next(i for i, (_, action) in enumerate(SettingsMenu.ACTIONS) if action == 'network-settings')
            menu.focus()
            await pilot.press('enter')
            await pilot.pause()
            form = app.screen
            self.assertIsInstance(form, SettingsForm)
            self.assertTrue(form.editing)
            group = form.query_one('#network-search-section', Collapsible)
            self.assertFalse(group.collapsed)
            network = form.query_one('#network-section', Collapsible)
            self.assertIs(app.focused, network.query_one('CollapsibleTitle'))
            section = form.query_one('#web-section', Collapsible)
            self.assertLess(network.region.y, section.region.y)
            self.assertEqual(section.title, 'Web Search · Off')
            await pilot.press('tab', 'enter')
            await pilot.pause()
            self.assertFalse(section.collapsed)
            control = form.query_one('#web-enabled', Checkbox)
            self.assertGreaterEqual(control.region.y, 1)
            self.assertLess(control.region.bottom, form.query_one('.wizard-actions').region.y)
            control.value = True
            await pilot.pause()
            self.assertIn('Automatic fallback', section.title)
            enable_group = form.query_one('#web-enable-setting')
            mode_group = form.query_one('#web-mode', Select).parent
            self.assertEqual(mode_group.region.y, enable_group.region.bottom + 1)
            web = form.query_one(WebAccessForm)
            self.assertTrue(web.collect()[0]['enabled'])
            await pilot.press('escape')
            await pilot.press('q')

    async def test_create_and_edit_share_category_names_and_order(self):
        from pi_sandbox_factory.settings_catalog import CATEGORIES
        expected_order = ['Model Providers', 'Editor', 'Folders', 'Document search', 'Network and Web Search', 'Approvals & sandbox',
                          'Agent tools', 'System prompt', 'SSH & Git', 'Advisors', 'Communications',
                          'Skills', 'Resources & environment']
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            draft = self.draft()
            draft['local_model_ports'] = self.record['config']['local_model_ports']
            await app.push_screen(SettingsForm(draft))
            await pilot.pause()
            sections = {category.section for category in CATEGORIES.values()}
            category_widgets = [section for section in app.screen.query('*') if section.id in sections]
            titles = {section.id: (section.title if isinstance(section, Collapsible) else
                                   str(section.query_one('#document-search-summary').render())) for section in category_widgets}
            creation = [title.split(' · ', 1)[0] for title in titles.values()]
            models = app.screen.query_one('#hosted-section', Collapsible)
            self.assertTrue(models.collapsed)
            self.assertIs(app.focused, models.query_one('CollapsibleTitle'))
            form = app.screen.query_one('.form')
            primary = app.screen.query_one('#apply', Button)
            primary_region = primary.region
            category_widgets[-1].scroll_visible(animate=False)
            await pilot.pause()
            self.assertLessEqual(category_widgets[-1].region.bottom, form.region.bottom)
            self.assertEqual(primary.region, primary_region)
            self.assertLess(primary.region.bottom, app.screen.query_one('Footer').region.y)
            await app.pop_screen()
            await pilot.click('#settings')
            menu = app.screen.query_one('#settings-actions', OptionList)
            prompts = [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]
            editing = [prompt.split(' · ', 1)[0] for prompt in prompts]
            self.assertEqual(creation, expected_order)
            self.assertEqual(editing, expected_order)
            for action in ('models', 'editor-settings', 'network-settings',
                           'approval-settings', 'agent-tools', 'document-search', 'ssh-settings', 'communications'):
                index = list(CATEGORIES).index(action)
                self.assertEqual(prompts[index], titles[CATEGORIES[action].section])
            self.assertIn('Model Providers · Local models on · 1 enabled', prompts)
            self.assertIn('Agent tools · Recall Lite · Browser off · Goals on', prompts)
            self.assertIn('Skills · 0 saved', prompts)

    async def test_settings_menu_reads_updated_saved_options(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            app.service.records[0]['config'].update(
                local_model_ports=None, network='unrestricted',
                hosted_connections={'openrouter': {}},
                extra_skills=[{'name': 'fixture-skill'}],
                options={'browser': True, 'goals': False, 'compaction_recall': 'full',
                         'approval': 'manual', 'inner_sandbox': False,
                         'web_access': {'enabled': True}, 'communications': {'slack': True}},
            )
            await pilot.click('#settings')
            menu = app.screen.query_one('#settings-actions', OptionList)
            prompts = [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]
            self.assertIn('Model Providers · Local models off · 1 enabled', prompts)
            self.assertIn('Agent tools · Recall Full · Browser on · Goals off', prompts)
            self.assertIn('Approvals & sandbox · Manual approval (ask me) · Inner sandbox off', prompts)
            self.assertIn('Network and Web Search · Unrestricted · Web Search on', prompts)
            self.assertIn('Communications · Slack', prompts)
            self.assertIn('Skills · 1 saved', prompts)
            self.assertEqual(menu.virtual_size.height, menu.option_count)
            self.assertLess(menu.region.bottom, app.screen.query_one('#settings-back').region.y)
            await pilot.press('escape')
            self.assertIs(app.screen, app.screen_stack[0])

    async def test_model_settings_are_grouped_and_bad_ports_are_visible(self):
        for size in ((80,24),(120,40),(220,50)):
            for context in ('create','defaults','existing'):
                app=self.app()
                async with app.run_test(size=size) as pilot:
                    await self.ready(app,pilot)
                    form=SettingsForm(self.draft(),editing=context=='existing',defaults=context=='defaults')
                    await app.push_screen(form)
                    await pilot.pause()
                    section=form.query_one('#hosted-section',Collapsible)
                    local=form.query_one('#local-provider',Collapsible)
                    self.assertTrue(section.collapsed)
                    self.assertTrue(local.collapsed)
                    self.assertEqual(local.title, 'Local models · Off')
                    control=form.query_one('#local-models',Checkbox)
                    control.value = True
                    await pilot.pause()
                    help_text = control.parent.query_one('.checkbox-help', Static)
                    copy=(str(control.label), str(help_text.render()))
                    field=form.query_one('#model-ports',Input)
                    self.assertIn(field,section.query(Input))
                    for invalid in ('bad',''):
                        field.value=invalid
                        await pilot.pause()
                        with self.assertRaises(ValueError):form.collect()
                        await pilot.pause()
                        self.assertFalse(section.collapsed)
                        self.assertFalse(local.collapsed)
                        error=form.query_one('#model-ports-error')
                        self.assertTrue(error.display)
                        self.assertGreaterEqual(error.region.y,1)
                        self.assertLessEqual(error.region.bottom,form.query_one('.wizard-actions').region.y)
                    form.query_one('#local-models',Checkbox).value=False
                    await pilot.pause()
                    self.assertTrue(field.disabled)
                    self.assertFalse(form.query_one('#local-model-details').display)
                    self.assertFalse(form.query_one('#model-ports-error').display)
                    self.assertEqual(local.title, 'Local models · Off')
                    self.assertEqual((str(control.label), str(help_text.render())), copy)
                    field.value='8000'
                    form.query_one('#local-models',Checkbox).value=True
                    await pilot.pause()
                    self.assertFalse(field.disabled)
                    self.assertTrue(form.query_one('#local-model-details').display)

    async def test_focused_settings_align_and_count_field_gaps_once(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                for setting, field_id in (('agent-tools', 'compaction-recall'),
                                          ('approval-settings', 'approval'), ('resources', 'memory')):
                    form = SettingsForm(self.draft(), editing=True, setting=setting)
                    await app.push_screen(form)
                    await pilot.pause()
                    field = form.query_one('#' + field_id)
                    group = field.parent
                    self.assertEqual(field.region.x, form.query_one('.wizard-actions').content_region.x)
                    self.assertEqual(field.region.y - group.children[0].region.bottom, 1)
                    if field_id == 'memory':
                        next_label = form.query_one('#env-file').parent.parent.query_one(Label)
                        self.assertEqual(field.region.height, 1)
                        self.assertEqual(next_label.region.y - field.region.bottom, 1)
                        env_file = form.query_one('#env-file-field')
                        browse = env_file.query_one(Button)
                        seed_label = form.query_one('#pi-config-field').parent.query_one(Label)
                        self.assertEqual(browse.region.height, 1)
                        self.assertEqual(browse.region.y, env_file.query_one(Input).region.y)
                        self.assertEqual(seed_label.region.y - env_file.region.bottom, 1)
                    else:
                        self.assertEqual(group.query_one('.setting-help').region.y - field.region.bottom, 1)
                        self.assertEqual(field.region.height, 1 if size[1] < 32 else 3)
                    await app.pop_screen()

    async def test_agent_tools_summary_tracks_visible_controls(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            form = SettingsForm(self.draft())
            await app.push_screen(form)
            await pilot.pause()
            section = form.query_one('#skills-section', Collapsible)
            self.assertIn('Agent tools', section.title)
            form.query_one('#browser', Checkbox).value = True
            form.query_one('#goals', Checkbox).value = False
            form.query_one('#compaction-recall', Select).value = 'full'
            await pilot.pause()
            self.assertIn('Browser on', section.title)
            self.assertIn('Recall Full', section.title)
            self.assertIn('Goals off', section.title)
            self.assertFalse(form.query('#knowledge-embedding, #embedding-url, #embedding-model'))

    async def test_document_search_dialog_cancel_validation_and_save_draft(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            draft = self.draft()
            form = SettingsForm(draft)
            await app.push_screen(form)
            await pilot.pause()
            configure = form.query_one('#configure-document-search', Button)
            self.assertEqual(form.query_one('#document-search-section').region.height, 1)
            configure.focus()
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            dialog = app.screen
            self.assertFalse(dialog.query_one('#embedding-server-fields').display)
            dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
            await pilot.pause()
            self.assertTrue(dialog.query_one('#embedding-server-fields').display)
            self.assertTrue(dialog.query_one('#save-document-search', Button).disabled)
            dialog.query_one('#embedding-url', Input).value = 'bad'
            await pilot.pause()
            self.assertTrue(dialog.query_one('#embedding-url-error').display)
            dialog.query_one('#embedding-url', Input).value = 'https://vectors.example/v1'
            await pilot.pause()
            dialog.query_one('#embedding-model', Input).value = 'invalid model'
            await pilot.pause()
            self.assertTrue(dialog.query_one('#embedding-model-error').display)
            dialog.query_one('#embedding-model', Input).value = 'test-embedding'
            await pilot.press('escape')
            self.assertEqual(form.document_search['knowledge_embedding'], 'off')
            await pilot.click('#configure-document-search')
            await pilot.pause()
            dialog = app.screen
            self.assertEqual(dialog.query_one('#knowledge-embedding', Select).value, 'off')
            dialog.query_one('#knowledge-embedding', Select).value = 'endpoint'
            await pilot.pause()
            dialog.query_one('#embedding-url', Input).value = 'https://vectors.example/v1'
            dialog.query_one('#embedding-model', Input).value = 'test-embedding'
            await pilot.pause()
            await pilot.click('#save-document-search')
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertIn('Semantic search', str(form.query_one('#document-search-summary').render()))
            self.assertEqual(form.collect()[0]['options']['knowledge_embedding_model'], 'test-embedding')
            self.assertEqual(draft['options'].get('knowledge_embedding', 'off'), 'off')

    async def test_existing_offline_search_can_be_kept_only_on_its_saved_sandbox(self):
        app = self.app()
        draft = self.draft()
        draft['options']['knowledge_embedding'] = 'local'
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            for editing in (True, False):
                form = SettingsForm(draft, editing=editing, setting='document-search' if editing else None)
                await app.push_screen(form)
                await pilot.pause()
                form.query_one('#configure-document-search').focus()
                await pilot.press('enter')
                await pilot.pause()
                dialog = app.screen
                choice = dialog.query_one('#knowledge-embedding', Select)
                self.assertEqual(choice.value, 'local' if editing else 'off')
                if not editing:
                    from textual.widgets._select import InvalidSelectValueError
                    with self.assertRaises(InvalidSelectValueError):
                        choice.value = 'local'
                await pilot.click('#save-document-search')
                await pilot.pause()
                self.assertEqual(form.document_search['knowledge_embedding'], 'local' if editing else 'off')
                await app.pop_screen()

    async def test_agent_tools_excludes_configured_document_search_and_menu_opens_it_separately(self):
        self.record['config']['options'] = {'knowledge_embedding': 'endpoint',
            'knowledge_embedding_url': 'https://vectors.example/v1', 'knowledge_embedding_model': 'test-embedding'}
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            for action in ('agent-tools', 'document-search'):
                await pilot.click('#settings')
                menu = app.screen.query_one('#settings-actions', OptionList)
                index = next(i for i, (_, key) in enumerate(SettingsMenu.ACTIONS) if key == action)
                prompt = str(menu.get_option_at_index(index).prompt)
                menu.highlighted = index
                menu.focus()
                await pilot.press('enter')
                await pilot.pause()
                form = app.screen
                self.assertFalse(form.query('#embedding-url, #embedding-model, #knowledge-embedding'))
                if action == 'agent-tools':
                    self.assertFalse(form.query_one('#document-search-section').display)
                    self.assertNotIn('search', prompt.lower())
                    self.assertNotIn('search', form.query_one('#skills-section', Collapsible).title.lower())
                else:
                    self.assertEqual(prompt, 'Document search · Semantic search')
                    self.assertEqual(app.focused.id, 'configure-document-search')
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertEqual(app.screen.query_one('#embedding-model', Input).value, 'test-embedding')
                    await pilot.press('escape')
                await pilot.press('escape')
                await pilot.pause()

    async def test_research_profile_picker_explains_the_sources_and_keeps_actions_visible(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            screen = NewSandbox()
            await app.push_screen(screen)
            helper = screen.query_one('#profile-summary', Static)
            picker = screen.query_one('#profile', Select)
            guide = screen.query_one('#new-profile-guide', Button)
            self.assertFalse(guide.display)
            picker.value = 'deep-research'
            await pilot.pause()
            self.assertIn('Web Search and subagents start on', str(helper.render()))
            self.assertTrue(guide.display)
            await pilot.click('#new-profile-guide')
            await pilot.pause()
            self.assertIn('Compare Python', app.screen.guide)
            self.assertLess(app.screen.query_one('#close-profile-guide', Button).region.bottom, 24)
            await pilot.press('escape')
            picker.value = 'local-knowledge'
            await pilot.pause()
            self.assertIn('Web Search starts off', str(helper.render()))
            self.assertLessEqual(helper.region.height, 2)
            self.assertLess(screen.query_one('#continue', Button).region.bottom, 24)
            await pilot.click('#continue')
            await pilot.pause()
            self.assertIn('for the sandbox name', str(screen.query_one('#new-error', Static).render()))
            self.assertLessEqual(screen.query_one('#new-title').region.bottom, screen.query_one('.form').region.y)
            profile = sandbox.load_profile('deep-research')
            draft = preset('deep-research', DEFAULT_SETTINGS, profile['defaults'])
            draft.update(name='research', workspace=str(self.workspace))
            form = SettingsForm(draft)
            await app.push_screen(form)
            await pilot.pause()
            title = form.query_one('.title', Label)
            self.assertEqual(str(title.render()), 'Review sandbox · 2 of 2')
            self.assertGreaterEqual(title.region.y, form.query_one('Header').region.bottom)
            viewport = form.query_one('.form')
            self.assertLessEqual(title.region.bottom, viewport.region.y)
            self.assertGreaterEqual(form.query_one('#review-project').region.y, viewport.region.y)

    async def test_form_groups_and_disabled_fields_follow_selections(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.ready(app, pilot)
            draft = self.draft()
            draft['local_model_ports'] = '8000'
            await app.push_screen(SettingsForm(draft))
            form = app.screen
            ports = form.query_one('#model-port-setting')
            section = form.query_one('#hosted-section', Collapsible)
            self.assertIn(ports, section.query('#model-port-setting'))
            self.assertIn(form.query_one('#local-models'), section.query('#local-models'))
            section.collapsed = False
            await pilot.pause()
            self.assertIn('1 enabled', section.title)
            form.query_one('#network-search-section', Collapsible).collapsed = False
            form.query_one('#network-section', Collapsible).collapsed = False
            await pilot.pause()
            categories = form.query_one('WebsiteGroups')
            destinations = next(label for label in form.query('Label') if str(label.render()).startswith('Additional allowed destinations'))
            self.assertGreaterEqual(destinations.region.y, categories.region.bottom)
            form.query_one('#local-models', Checkbox).value = False
            await pilot.pause()
            self.assertTrue(form.query_one('#model-ports', Input).disabled)
            form.query_one('#network', Select).value = 'unrestricted'
            await pilot.pause()
            self.assertFalse(form.query_one('#network-rules-setting').display)
            form.query_one('#network', Select).value = 'allowlist'
            await pilot.pause()
            self.assertTrue(form.query_one('#network-rules-setting').display)
            form.query_one('#skills-section', Collapsible).collapsed = False
            await pilot.pause()
            recall = form.query_one('#compaction-recall', Select)
            help_text = recall.parent.query_one('.setting-help')
            self.assertEqual(help_text.region.y, recall.region.bottom + 1)
            await pilot.press('escape')
            draft = self.draft()
            draft['options']['web_access']['enabled'] = True
            await app.push_screen(SettingsForm(draft, defaults=True))
            await pilot.pause()
            self.assertFalse(app.screen.query_one('#web-defaults-setting').display)
            await pilot.press('escape')
            await pilot.press('q')

    async def test_create_launches_editor_only_when_selected(self):
        for open_editor in (True, False):
            app = self.app()
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(NewSandbox())
                draft = self.draft()
                draft['open_editor'] = open_editor
                await app.push_screen(SettingsForm(draft))
                app.screen.submit(draft, {})
                await self.ready(app, pilot)
                self.assertEqual(app.service.calls[0][0], 'create')
                self.assertEqual(['open', 'demo'] in app.service.calls, open_editor)
                self.assertTrue(app.screen.finished)
                self.assertFalse(app.screen.failed)
                await pilot.press('escape')
                await self.ready(app, pilot)
                await pilot.press('q')

    async def test_update_completion_reports_whether_editor_reopened(self):
        for fails in (False, True):
            app = self.app()
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                draft = self.draft()
                draft['open_editor'] = True
                await app.push_screen(SettingsForm(draft, editing=True))
                with patch.object(app.service, 'run', side_effect=ValueError('Cannot open') if fails else None):
                    app.screen.submit(draft, {})
                    await self.ready(app, pilot)
                notice = str(app.screen.query_one('#operation-next', Static).render())
                self.assertIn('Open in browser' if fails else 'reopened client', notice)
                self.assertNotIn('Slack', notice)
                self.assertNotIn('SSH', notice)
                self.assertFalse(app.screen.failed)

    async def test_create_with_pi_opens_the_terminal_after_success(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(NewSandbox())
            draft = self.draft()
            draft['editor']['mode'] = 'pi'
            draft['open_editor'] = True
            await app.push_screen(SettingsForm(draft))
            with patch.object(app, 'interactive') as launch:
                app.screen.submit(draft, {})
                await self.ready(app, pilot)
                launch.assert_called_once_with('pi', 'demo')
            self.assertEqual([call[0] for call in app.service.calls], ['create'])
            self.assertTrue(app.screen.finished)
            self.assertFalse(app.screen.failed)

    async def test_editor_choice_stays_with_final_action(self):
        for size in ((80, 24), (120, 40)):
            for editing in (False, True):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    await app.push_screen(SettingsForm(self.draft(), editing=editing))
                    await pilot.pause()
                    form = app.screen
                    choice = form.query_one('#open-editor', Checkbox)
                    actions = form.query_one('.wizard-actions')
                    apply = form.query_one('#apply', Button)
                    self.assertIs(choice.parent, actions)
                    self.assertEqual(apply.region.right, actions.content_region.right)
                    self.assertEqual(apply.region.height, 3 if size[1] >= 32 else 1)
                    self.assertEqual(choice.region.x, form.query_one('.form').content_region.x)
                    self.assertEqual(choice.region.x, form.query_one('#back').region.x)
                    self.assertEqual(choice.value, not editing)
                    self.assertGreaterEqual(choice.region.y, actions.region.y)
                    self.assertLess(choice.region.bottom, apply.region.y)
                    self.assertLess(apply.region.bottom, form.query_one('Footer').region.y)
                    self.assertGreater(choice.region.y, actions.region.y)
                    form.query_one('#network-search-section', Collapsible).collapsed = False
                    form.query_one('#web-section', Collapsible).collapsed = False
                    form.query_one('#web-section').scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    self.assertGreaterEqual(choice.region.y, actions.region.y)
                    await pilot.press('escape')
                    await pilot.press('q')

    async def test_editor_failure_preserves_successful_creation(self):
        app = self.app()
        def fail_open(args, log):
            raise OSError('VS Code is not installed')
        app.service.run = fail_open
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(NewSandbox())
            draft = self.draft()
            draft['open_editor'] = True
            await app.push_screen(SettingsForm(draft))
            app.screen.submit(draft, {})
            await self.ready(app, pilot)
            self.assertEqual(app.service.calls[0][0], 'create')
            self.assertTrue(app.screen.finished)
            self.assertTrue(app.screen.failed)
            self.assertTrue(app.screen.title.startswith('Opening '))
            self.assertIsNone(app.screen.cancellation)
            output = '\n'.join(line.text for line in app.screen.query_one(RichLog).lines)
            self.assertIn('VS Code is not installed', output)
            # Opening is separate: a late Cancel cannot delete a completed sandbox.
            await pilot.press('escape')
            await self.ready(app, pilot)
            self.assertTrue(app.screen.finished)
            self.assertFalse(app.screen.failed)
            self.assertTrue(app.screen.title.startswith('Created '))
            await pilot.press('escape')
            await pilot.press('q')


    async def test_context_actions_and_modal_actions_have_padding(self):
        from pi_sandbox_factory.repository_ui import RepositorySetup
        from pi_sandbox_factory.sandbox_tui import SSHScreen
        for size in ((80, 24), (120, 40), (220, 50)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(SSHScreen(self.record))
                await pilot.pause()
                button = app.screen.query_one('#ssh-repository', Button)
                self.assertGreaterEqual(button.styles.padding.left + button.styles.line_pad, 2)
                self.assertGreaterEqual(button.styles.padding.right + button.styles.line_pad, 2)
                self.assertGreater(button.region.y, app.screen.query_one('#ssh-hosts').region.bottom)
                self.assertEqual(app.screen.query_one('#ssh-hosts').region.height, 5)
                await app.push_screen(RepositorySetup())
                await pilot.pause()
                actions = app.screen.query_one('.dialog-actions')
                save = app.screen.query_one('#repository-add', Button)
                cancel = app.screen.query_one('#repository-cancel', Button)
                self.assertEqual(save.region.right, actions.content_region.right)
                self.assertEqual(save.region.height, 3 if size[1] >= 32 else 1)
                self.assertEqual(cancel.region.height, save.region.height)
                self.assertGreater(cancel.region.x, actions.content_region.x)
                self.assertGreater(actions.region.y, app.screen.query_one('VerticalScroll').region.bottom)
                self.assertLess(save.region.bottom, size[1] - 1)
                self.assertNotEqual(cancel.styles.background, cancel.parent.parent.styles.background)

    async def test_permission_controls_and_subagents_are_in_their_sections(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(SettingsForm(self.draft()))
            app.screen.query_one('#agent-section', Collapsible).collapsed = False
            await pilot.pause()
            approval = app.screen.query_one('#approval', Select)
            explanation = app.screen.query_one('#explain-approval', Button)
            subagents = app.screen.query_one('#subagents', Checkbox)
            inner = app.screen.query_one('#inner-sandbox', Checkbox)
            credentials = app.screen.query_one('#protect-credentials', Checkbox)
            self.assertLess(inner.region.y, credentials.region.y)
            self.assertLess(credentials.region.y, approval.region.y)
            checkbox_copy = {}
            for checkbox in (inner, credentials):
                help_text = app.screen.query_one('#' + checkbox.id + '-help', Static)
                self.assertIs(help_text.parent, checkbox.parent)
                self.assertEqual(help_text.region.y, checkbox.region.bottom)
                self.assertTrue(str(help_text.render()))
                checkbox_copy[checkbox.id] = (str(checkbox.label), str(help_text.render()))
            self.assertIs(approval.parent, explanation.parent.parent)
            self.assertEqual(approval.region.y, explanation.region.bottom + 1)
            explanation.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click('#explain-approval')
            await pilot.pause()
            help_box = app.screen.query_one('#approval-help')
            rows = list(help_box.query('.approval-mode-help'))
            self.assertEqual(len(rows), len(APPROVAL_MODES))
            for row, (label, _, description) in zip(rows, APPROVAL_MODES.values()):
                self.assertEqual(str(row.query_one(Label).render()), '•')
                self.assertEqual(str(row.query_one('.approval-mode-description', Static).render()),
                                 f'{label} {description}')
            self.assertIn('Settings → Approvals & sandbox.', str(list(help_box.query(Static))[-1].render()))
            close = app.screen.query_one('#close-approval-help', Button)
            self.assertEqual(close.region.height, 3)
            for y in range(close.region.y, close.region.bottom):
                self.assertIs(app.screen.get_widget_at(close.region.x + 1, y)[0], close)
            await pilot.click('#close-approval-help')
            await pilot.pause()
            self.assertIn(app.screen.query_one('#skills-section', Collapsible), subagents.ancestors)
            questions = app.screen.query_one('#question-forms', Checkbox)
            self.assertTrue(questions.value)
            self.assertIn(app.screen.query_one('#skills-section', Collapsible), questions.ancestors)
            questions.value = False
            self.assertFalse(app.screen.collect()[0]['options']['question_forms'])
            credentials = app.screen.query_one('#protect-credentials', Checkbox)
            self.assertTrue(credentials.value)
            self.assertIn(app.screen.query_one('#agent-section', Collapsible), credentials.ancestors)
            inner.value = credentials.value = False
            await pilot.pause()
            for checkbox in (inner, credentials):
                help_text = app.screen.query_one('#' + checkbox.id + '-help', Static)
                self.assertEqual((str(checkbox.label), str(help_text.render())), checkbox_copy[checkbox.id])
            self.assertFalse(app.screen.collect()[0]['options']['protect_credentials'])
            self.assertFalse(app.screen.collect()[0]['options']['inner_sandbox'])
            self.assertTrue(str(app.screen.query_one('#agent-section', Collapsible).title).startswith('Approvals & sandbox'))

    async def test_labels_controls_and_feedback_do_not_create_empty_rows(self):
        from pi_sandbox_factory.sandbox_tui import CreateFromSandbox, Operation
        from pi_sandbox_factory.models_ui import ProviderDialog
        for size in ((80, 24), (120, 40)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await self.ready(app, pilot)
                await app.push_screen(NewSandbox())
                await pilot.pause()
                labels = list(app.screen.query(Label))
                profile = app.screen.query_one('#profile')
                self.assertEqual(profile.region.y, labels[1].region.bottom)
                workspace = app.screen.query_one('#workspace')
                label = next(x for x in labels if str(x.render()) == 'Workspace')
                self.assertEqual(workspace.region.y, label.region.bottom + 1)
                self.assertLessEqual(app.screen.query_one('#name').region.bottom,
                                     app.screen.query_one('.wizard-actions').region.y)
                await app.pop_screen()
                await app.push_screen(CreateFromSandbox([self.record]))
                await pilot.pause()
                self.assertEqual(app.screen.query_one('#copy-source').region.height, 3)
                await app.pop_screen()
                await app.push_screen(ProviderDialog())
                feedback = app.screen.query_one('#hosted-error', Static)
                self.assertFalse(feedback.display)
                feedback.update('Enter an API key.')
                await pilot.pause()
                self.assertTrue(feedback.display)
                self.assertTrue(feedback.has_class('error'))
                self.assertLess(app.screen.query_one('#hosted-save').region.bottom, size[1])
                feedback.update('')
                self.assertFalse(feedback.display)
                await app.pop_screen()
                await app.push_screen(Operation('Starting sandbox', lambda log: log('Ready.')))
                await self.ready(app, pilot)
                button = app.screen.query_one('#operation-back')
                bar = app.screen.query_one('.wizard-actions')
                self.assertIs(button.parent, bar)
                self.assertIs(app.focused, button)
                self.assertGreaterEqual(button.content_region.width, len(str(button.label)))
                self.assertEqual(button.region.x, 3)
                self.assertGreater(button.region.y, bar.region.y)
                self.assertLess(button.region.bottom, app.screen.query_one('Footer').region.y)
