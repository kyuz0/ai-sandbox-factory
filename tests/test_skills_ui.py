import asyncio
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Collapsible
from ai_sandbox_factory import skill_library
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, PackagesScreen, SettingsForm, preset
from ai_sandbox_factory.skills_ui import ImportSkills, SkillSelection
from ai_sandbox_factory.tui_widgets import Checkbox
from test_tui import FakeService


class SkillsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.runner = asyncio.Runner()
    @classmethod
    def tearDownClass(cls): cls.runner.close()
    def _callTestMethod(self, method): self.runner.run(method())

    async def test_import_multiple_select_and_remove_without_affecting_saved_copy(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': temp+'/library'}), patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
            source = Path(temp)/'source'
            for name in ('one','two'):
                folder=source/name;folder.mkdir(parents=True)
                (folder/'SKILL.md').write_text(f'---\nname: {name}\ndescription: Use {name}\n---\n')
            app=Cockpit(Path(temp)/'state',FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause(); await app.workers.wait_for_complete()
                await app.push_screen(PackagesScreen(initial='skills'));await pilot.pause()
                app.screen.query_one('#package-install',Button).focus();await pilot.press('enter');await pilot.pause()
                self.assertIsInstance(app.screen,ImportSkills)
                app.screen.query_one('#skill-source',Input).value=str(source);await pilot.pause()
                app.screen.query_one('#skill-discover',Button).focus();await pilot.press('enter');await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                self.assertEqual(len(app.screen.rows),2)
                for index in range(2):app.screen.query_one(f'#candidate-{index}',Checkbox).value=True
                await pilot.pause()
                save=app.screen.query_one('#skill-import-save',Button)
                self.assertLess(save.region.bottom,24)
                save.focus();await pilot.press('enter');await pilot.pause()
                self.assertIsInstance(app.screen,PackagesScreen)
                rows=skill_library.entries();self.assertEqual(len(rows),2)
                selected=skill_library.snapshot(list(rows),Path(temp)/'sandbox')
                for key in rows:skill_library.remove(key)
                await app.screen.show_packages()
                self.assertTrue(all((Path(s['path'])/'SKILL.md').exists() for s in selected))

    async def test_changed_source_requires_validation_and_errors_keep_actions_visible(self):
        for size in ((80,24),(120,40),(220,50)):
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': temp+'/library'}), patch('ai_sandbox_factory.updates.available_update',return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
                app=Cockpit(Path(temp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await pilot.pause();await app.workers.wait_for_complete()
                    await app.push_screen(ImportSkills());await pilot.pause()
                    app.screen.query_one('#skill-source',Input).value='invalid source'
                    await pilot.pause()
                    app.screen.query_one('#skill-discover',Button).focus();await pilot.press('enter');await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                    self.assertTrue(app.screen.query_one('#skill-import-error').display)
                    self.assertTrue(app.screen.query_one('#skill-import-save',Button).disabled)
                    self.assertFalse(app.screen.query_one('#skill-import-cancel',Button).disabled)
                    self.assertLess(app.screen.query_one('#skill-import-save',Button).region.bottom,size[1])
                    await pilot.press('escape');await pilot.pause()
                    self.assertNotIsInstance(app.screen,ImportSkills)

    async def test_default_selection_summary_and_update_conflict_are_visible(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': temp+'/library'}), patch('ai_sandbox_factory.updates.available_update',return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
            source=Path(temp)/'source';source.mkdir()
            (source/'SKILL.md').write_text('---\nname: example\ndescription: Example skill\n---\n')
            rows=skill_library.discover(str(source),Path(temp)/'fetched');skill_library.install(rows)
            key=rows[0]['id']
            draft=preset('base',copy.deepcopy(DEFAULT_SETTINGS));draft.update(name='demo',workspace=temp,library_skills=[key])
            app=Cockpit(Path(temp)/'state',FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause();await app.workers.wait_for_complete()
                await app.push_screen(SettingsForm(draft));await pilot.pause()
                section=app.screen.query_one('#skill-library-section',Collapsible)
                self.assertIn('1 from library',section.title)
                selection=app.screen.query_one(SkillSelection)
                manage = selection.query_one('#manage-skills', Button)
                manage.focus()
                await pilot.press('enter')
                self.assertIsInstance(app.screen, PackagesScreen)
                self.assertEqual(app.screen.kind, 'skills')
                self.assertEqual(str(app.screen.query_one('#package-install', Button).label), 'Add skills…')
                await pilot.press('escape')
                self.assertIsInstance(app.screen, SettingsForm)
                selection.query_one('#library-skill-'+key,Checkbox).value=False
                await pilot.pause();self.assertIn('0 from library',section.title)
                await app.pop_screen()
                saved=skill_library.snapshot([key],Path(temp)/'sandbox');saved[0]['digest']='older'
                draft['extra_skills']=saved
                await app.push_screen(SettingsForm(draft,editing=True));await pilot.pause()
                section=app.screen.query_one('#skill-library-section',Collapsible);section.collapsed=False
                selection=app.screen.query_one(SkillSelection)
                self.assertIn('1 update available',section.title)
                with self.assertRaises(ValueError):selection.collect()
                await pilot.pause()
                self.assertTrue(selection.query_one('#saved-skill-error-0').display)
                selection.query_one('#saved-skill-0',Checkbox).value=False
                await pilot.pause()
                self.assertEqual(selection.collect()['library_skills'],[key])

    async def test_update_prefill_survives_mount_and_browse_is_available(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause();await app.workers.wait_for_complete()
                await app.push_screen(ImportSkills(temp,'main'));await pilot.pause()
                self.assertEqual(app.screen.query_one('#skill-source',Input).value,temp)
                self.assertEqual(app.screen.query_one('#skill-ref',Input).value,'main')
                source = app.screen.query_one('#skill-source', Input)
                browse = app.screen.query_one('#skill-browse', Button)
                self.assertEqual((source.region.height, browse.region.height), (1, 1))
                self.assertEqual(source.region.y, browse.region.y)

    async def test_search_preserves_hidden_selections_and_imports_them(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)):
            app = Cockpit(Path(temp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(); await app.workers.wait_for_complete()
                screen = ImportSkills()
                await app.push_screen(screen); await pilot.pause()
                self.assertFalse(screen.query_one('#skill-search-setting').display)
                rows = [dict(id=str(i), name=f'skill-{i}', description='Python auditing' if i == 42 else 'Code review', folder=f'plugins/group-{i}/skill') for i in range(1000)]
                rows.append(dict(id='invalid', name='invalid-skill', description='', folder='broken', error='Missing description'))
                await screen.show_candidates(rows); await pilot.pause()
                search = screen.query_one('#skill-search', Input)
                search.value = 'PYTHON auditing'; await pilot.pause()
                self.assertEqual([i for i, (_, _, box, _) in enumerate(screen.candidates) if box.display], [42])
                screen.query_one('#candidate-42', Checkbox).value = True
                search.value = 'group-7/'; await pilot.pause()
                self.assertTrue(screen.query_one('#candidate-7').parent.display)
                self.assertFalse(screen.query_one('#candidate-42').parent.display)
                screen.query_one('#candidate-7', Checkbox).value = True
                search.value = 'no-such-skill'; await pilot.pause()
                self.assertIn('No matching skills · 2 selected', str(screen.query_one('#skill-search-status').render()))
                self.assertEqual(len(screen.rows), 1001)
                scrolled = asyncio.Event()
                show_results = screen.show_search_results

                def show_and_wait_for_layout():
                    show_results()
                    screen.call_after_refresh(scrolled.set)

                with patch.object(screen, 'show_search_results', new=show_and_wait_for_layout):
                    search.value = 'invalid-skill'
                    await asyncio.wait_for(scrolled.wait(), timeout=10)
                await pilot.pause()
                invalid = screen.query_one('#candidate-1000', Checkbox)
                self.assertTrue(invalid.disabled)
                self.assertLessEqual(invalid.region.bottom, screen.query_one('VerticalScroll').content_region.bottom)
                self.assertGreaterEqual(invalid.region.y, screen.query_one('VerticalScroll').content_region.y)
                search.value = ''; await pilot.pause()
                self.assertTrue(screen.query_one('#candidate-42', Checkbox).value)
                self.assertTrue(screen.query_one('#candidate-7', Checkbox).value)
                search.value = 'no-such-skill'; await pilot.pause()
                with patch.object(screen, 'install_selected') as install:
                    screen.query_one('#skill-import-save', Button).focus()
                    await pilot.press('enter'); await pilot.pause()
                    self.assertEqual([row['id'] for row in install.call_args.args[0]], ['7', '42'])
                screen.set_busy(False)
                screen.query_one('#skill-source', Input).value = 'changed-source'; await pilot.pause()
                self.assertEqual(search.value, '')
                self.assertEqual(screen.rows, [])
                self.assertFalse(screen.query_one('#skill-search-setting').display)
                self.assertTrue(screen.query_one('#skill-import-save', Button).disabled)
                await app.pop_screen(); await pilot.pause()
