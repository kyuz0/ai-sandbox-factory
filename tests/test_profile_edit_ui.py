"""Profile editing labels, exact review actions, asynchronous ownership and layout."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, OptionList, Static
from ai_sandbox_factory import profile_edit, sandbox
from ai_sandbox_factory.profile_edit_ui import EditProfile, ReviewProfile, consequences
from ai_sandbox_factory.chat_history_ui import DeleteSandbox
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, MoreActions, NewSandbox, SettingsForm, copy_draft
from test_tui import FakeService
from tui_geometry import check_input_layout


def record(editable=False, running=True):
    config={'primary_agent':'pi','engine':'podman','workspace':'/home/example/project',
            'profile':sandbox.load_profile('base')}
    if editable:
        config[profile_edit.EDIT]={'path':'/home/example/sandbox-profiles/my-profile',
                                   'origin_name':config['profile']['name'],'origin_digest':config['profile']['digest']}
    return {'name':'example','dev':'running' if running else 'stopped','config':config}


def review(changed=True):
    return {'revision':'1234567890abcdef','profile':{'digest':'abcdef1234567890'},
            'folder':'/home/example/sandbox-profiles/my-profile',
            'changes':['Changed: Dockerfile','Added: services/reporting/Dockerfile'] if changed else [],
            'added_services':['reporting'] if changed else [],
            'diff':'--- applied/Dockerfile\n+++ edited/Dockerfile\n@@ -1 +1 @@\n-RUN true\n+RUN apt-get install -y figlet\n'}


class ProfileEditUI(unittest.IsolatedAsyncioTestCase):
    async def test_copy_review_points_only_independent_copies_to_make_editable(self):
        for size in ((80,24),(120,40),(220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None):
                (Path(tmp)/'example').mkdir()
                (Path(tmp)/'example/allowlist.txt').write_text('')
                app=Cockpit(Path(tmp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    for shared in (False,True):
                        draft=copy_draft(record(True),Path(tmp))
                        draft.update(name='new-project',share_editable_profile=shared)
                        page=SettingsForm(draft)
                        await app.push_screen(page)
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        text=str(page.query_one('#review-profile-copy',Static).render())
                        self.assertEqual('Make profile editable…' in text,not shared)
                        self.assertNotIn('→ Edit profile…',text)
                        await pilot.press('escape')

    def test_actions_group_profile_details_and_guide_without_duplicate_controls(self):
        row=record()
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'start-here.md').write_text('Profile guide')
            row['config']['profile']['path']=temp
            with patch('ai_sandbox_factory.sandbox_tui.sandbox_alerts',return_value=['fixture']):
                menu=MoreActions(row,browser_running=True)
            self.assertEqual([key for _,key in menu.groups[2]],
                             ['profile-information','profile-guide','chats','diagnostics','alerts'])
            self.assertNotIn('edit-profile',[key for _,key in menu.actions])
            self.assertNotIn('saved-service-data',[key for _,key in menu.actions])

    async def test_keyboard_opens_profile_then_edit_and_respects_running_job(self):
        for size in ((80,24),(120,40),(220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None):
                row=record()
                service=FakeService([])
                service.config=lambda name:copy.deepcopy(row['config'])
                app=Cockpit(Path(temp),service)
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    menu=MoreActions(row)
                    await app.push_screen(menu,lambda action:app.perform(action,record=row) if action else None)
                    menu.query_one(OptionList).highlighted=next(i for i,(_,key) in enumerate(menu.actions) if key=='profile-information')
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertIsInstance(app.screen,AppliedProfile)
                    page=app.screen
                    control = page.query_one('#profile-information-edit', Button)
                    self.assertEqual(str(control.label), 'Make profile editable…')
                    self.assertIn('Make profile editable…', control.render_line(0).text)
                    self.assertEqual(app.focused.id,'profile-information-back')
                    await pilot.press('tab','enter')
                    await pilot.pause()
                    self.assertIsInstance(app.screen,EditProfile)
                    self.assertEqual(str(app.screen.query_one('.focused-settings-title').render()), 'Make profile editable · example')
                    await pilot.press('escape')
                    await app.workers.wait_for_complete()
                    self.assertIs(app.screen,page)
                    with patch.object(app,'active_job',return_value=object()) as active, \
                            patch.object(app,'show_job') as show:
                        page.query_one('#profile-information-edit',Button).focus()
                        await pilot.press('enter')
                        await pilot.pause()
                        active.assert_called_with('example')
                        show.assert_called_once()
                        self.assertIs(app.screen,page)

    async def test_returning_from_edit_refreshes_the_profile_page(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            service=FakeService([])
            updated=record(True)
            service.config=lambda name:copy.deepcopy(updated['config'])
            app=Cockpit(Path(temp),service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(record(),Path(temp))
                await app.push_screen(page)
                self.assertEqual(str(page.query_one('#profile-information-edit', Button).label), 'Make profile editable…')
                page.query_one('#profile-information-edit',Button).press()
                await pilot.pause()
                await pilot.press('escape')
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(page.record['config'],updated['config'])
                self.assertEqual(str(page.query_one('#profile-information-edit', Button).label), 'Editable profile…')
                self.assertEqual(app.focused.id,'profile-information-edit')
                self.assertIn(updated['config'][profile_edit.EDIT]['path'],
                              ' '.join(str(widget.render()) for widget in page.query(Static)))

    async def test_failed_profile_refresh_keeps_the_page_and_reports_the_cause(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(record(),Path(temp))
                await app.push_screen(page)
                page.query_one('#profile-information-edit',Button).press()
                await pilot.pause()
                with patch.object(app.service,'config',side_effect=ValueError('config.json: expected a comma at line 4')):
                    await pilot.press('escape')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertIs(app.screen,page)
                self.assertEqual(app.focused.id,'profile-information-edit')
                self.assertIn('expected a comma at line 4',str(page.query_one('#profile-information-error').render()))

    async def test_dismissed_profile_refresh_cannot_update_another_screen(self):
        entered,release=threading.Event(),threading.Event()
        def config(name):
            entered.set();release.wait()
            return record(True)['config']
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            service=FakeService([])
            service.config=config
            app=Cockpit(Path(temp),service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(record(),Path(temp))
                await app.push_screen(page)
                page.query_one('#profile-information-edit',Button).press()
                await pilot.pause()
                await app.pop_screen()
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,10))
                    await app.pop_screen()
                    replacement=EditProfile(record())
                    await app.push_screen(replacement)
                finally:
                    release.set()
                await pilot.pause()
                self.assertIs(app.screen,replacement)
                self.assertEqual(app.focused.id,'profile-edit-folder')

    async def test_replace_only_and_package_only_confirmations_show_only_relevant_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            for size in ((80,24),(120,40),(220,50)):
                app = Cockpit(Path(tmp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    for replace in (False,True):
                        changed = {**review(),'added_services':[], 'removed_services':[],
                                   'replaced_services':['database'] if replace else []}
                        screen = ReviewProfile(record(True),changed)
                        await app.push_screen(screen)
                        await pilot.pause()
                        with patch.object(app,'start_settings_job') as start:
                            await pilot.press('enter')
                            await pilot.pause()
                            notes = ' '.join(app.screen.notes)
                            self.assertEqual(app.focused.id,'yes')
                            self.assertEqual(app.screen.query_one('#yes',Button).variant,'primary')
                            self.assertEqual('These services stop' in notes,replace)
                            self.assertEqual('Existing profile services and their application data are kept.' in notes,not replace)
                            self.assertNotIn('Removed service containers',notes)
                            self.assertNotIn(';',notes)
                            await pilot.press('escape')
                            start.assert_not_called()
                        await pilot.press('escape')

    async def test_changed_and_removed_services_have_visible_review_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            for size in ((80,24),(120,40),(220,50)):
                with self.subTest(size=size):
                    app = Cockpit(Path(tmp), FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await app.workers.wait_for_complete()
                        change = {**review(), 'replaced_services':['database'], 'removed_services':['reporting'],
                                  'retained_volumes':['example-database-data','example-reporting-data']}
                        screen = ReviewProfile(record(True), change)
                        await app.push_screen(screen)
                        await pilot.pause()
                        text = ' '.join(str(item.render()) for item in screen.query(Static))
                        for expected in ('Services to replace','Services to remove','database','reporting',
                                         'Data volumes kept for recovery','example-reporting-data'):
                            self.assertIn(expected,text)
                        with patch.object(app,'start_settings_job') as start:
                            await pilot.press('enter')
                            await pilot.pause()
                            notes = ' '.join(app.screen.notes)
                            self.assertIn('database, reporting', notes)
                            self.assertIn('Removed service containers are deleted',notes)
                            self.assertIn('copies of their data volumes',notes)
                            self.assertEqual(app.focused.id,'cancel')
                            for button in app.screen.query(Button):
                                self.assertEqual(button.region.height,1)
                                self.assertTrue(app.screen.query_one('#confirm-box').content_region.contains_region(button.region))
                            await pilot.press('escape')
                            start.assert_not_called()
                            self.assertIs(app.screen, screen)
                        await pilot.press('escape')
                        volume = 'pi-sandbox-factory-example-profile-data-0123456789abcdef'
                        deletion = DeleteSandbox('example',has_config=True,services=['database'],retained_volumes=[volume])
                        await app.push_screen(deletion)
                        await pilot.pause()
                        text = ' '.join(str(item.render()) for item in deletion.query(Static))
                        self.assertIn('Factory kept old service data when you applied profile changes or restored saved data.',text)
                        self.assertIn('It is stored in Podman volumes on this computer.',text)
                        self.assertIn('Deleting this sandbox permanently deletes that data.',text)
                        self.assertNotIn('kept from previous profile',text)
                        self.assertIn('permanently deleted',text)
                        self.assertIn(f'podman volume export {volume} --output {volume}.tar',text)
                        self.assertNotIn('will also be permanently deleted',text)
                        self.assertEqual(app.focused.id,'cancel')
                        for button in deletion.query(Button):
                            self.assertEqual(button.region.height,1)
                            self.assertLess(button.region.bottom,size[1])
                        await pilot.press('escape')

    async def test_folder_focus_blank_error_confirmation_and_cooperative_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(tmp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                screen=EditProfile(record())
                await app.push_screen(screen)
                await pilot.press('x')
                self.assertTrue(screen.query_one('#profile-edit-folder',Input).value.endswith('x'))
                screen.query_one('#profile-edit-folder',Input).value=''
                screen.query_one('#profile-edit-go',Button).press()
                await pilot.pause()
                self.assertIn('Enter a new or empty',str(screen.query_one('#profile-edit-error',Static).render()))
                screen.query_one('#profile-edit-folder',Input).value='/home/example/profiles/test'
                field=screen.query_one('#profile-edit-folder',Input)
                field.cursor_position=8
                screen.query_one('#profile-edit-go',Button).press()
                await pilot.pause()
                self.assertIn('its agent work',' '.join(app.screen.notes))
                self.assertIn('example',app.screen.message)
                await pilot.press('escape')
                self.assertIs(app.screen,screen)
                self.assertEqual(field.cursor_position,8)
                self.assertEqual(str(screen.query_one('#profile-edit-error',Static).render()),'')
                screen.query_one('#profile-edit-go',Button).press()
                await pilot.pause()
                with patch.object(app,'start_settings_job') as start:
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertTrue(start.call_args.kwargs['cooperative'])

    async def test_review_uses_exact_frozen_revision_and_no_change_has_no_apply(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(tmp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                for changed in (False,True):
                    screen=ReviewProfile(record(True),review(changed))
                    await app.push_screen(screen)
                    await pilot.pause()
                    text=' '.join(str(item.render()) for item in screen.query(Static))
                    self.assertIn('Review ID',text)
                    self.assertIn(review()['revision'],text)
                    self.assertNotIn(review()['profile']['digest'],text)
                    self.assertEqual(screen.query_one('#profile-review-apply',Button).disabled,not changed)
                    if not changed:
                        await pilot.press('escape')
                    else:
                        self.assertEqual(app.focused.id,'profile-review-apply')
                        with patch.object(app,'start_settings_job') as start:
                            await pilot.press('enter')
                            await pilot.pause()
                            start.assert_not_called()
                            self.assertIn('Apply the reviewed profile changes to example?',app.screen.message)
                            self.assertIn('saved chats',' '.join(app.screen.notes))
                            await pilot.press('escape')
                            self.assertIs(app.screen,screen)
                            start.assert_not_called()
                            await pilot.press('enter')
                            await pilot.pause()
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertTrue(start.call_args.kwargs['cooperative'])
                            with patch.object(app.service,'run_update',create=True) as run:
                                start.call_args.args[2](lambda line:None,object())
                                self.assertEqual(run.call_args.args[0],['profile-apply','example','--revision','1234567890abcdef'])

    async def test_layout_at_supported_sizes_and_source_is_clear_in_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            for size in ((80,24),(120,40),(220,50)):
                with self.subTest(size=size):
                    app=Cockpit(Path(tmp),FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await app.workers.wait_for_complete()
                        for screen in (AppliedProfile(record(),Path(tmp)),AppliedProfile(record(True),Path(tmp)),
                                       EditProfile(record()),EditProfile(record(True)),
                                       EditProfile(record(True),error='profile.json: line 4, column 8: expected a comma. Fix the JSON and validate again.'),
                                       ReviewProfile(record(True),review())):
                            await app.push_screen(screen)
                            await pilot.pause()
                            check_input_layout(screen)
                            if isinstance(screen,AppliedProfile):
                                expected = 'Editable profile…' if screen.record['config'].get(profile_edit.EDIT) else 'Make profile editable…'
                                control = screen.query_one('#profile-information-edit', Button)
                                self.assertEqual(str(control.label), expected)
                                self.assertIn(expected, control.render_line(0).text)
                            elif isinstance(screen,EditProfile):
                                expected = 'Editable profile' if screen.record['config'].get(profile_edit.EDIT) else 'Make profile editable'
                                self.assertEqual(str(screen.query_one('.focused-settings-title').render()), expected + ' · example')
                            if isinstance(screen,(EditProfile,ReviewProfile)):
                                self.assertLessEqual(screen.query_one('.form').region.width,100)
                                self.assertEqual(screen.query_one('.form').region.width,screen.query_one('.wizard-actions').region.width)
                            for button in screen.query(Button):
                                if button.display and button.region.height:
                                    self.assertEqual(button.region.height,1,button.id)
                                    self.assertLessEqual(button.region.bottom,size[1]-1,button.id)
                            await pilot.press('escape')
                        app.perform('rebuild',record=record(True))
                        await pilot.pause()
                        self.assertIn('saved profile',app.screen.message)
                        with patch.object(app,'start_rebuild') as rebuild:
                            await pilot.press('enter')
                            await pilot.pause()
                            rebuild.assert_called_once_with('example',['recreate','example'])

    async def test_dismissed_review_finishes_as_job_without_opening_a_replacement_screen(self):
        entered,release=threading.Event(),threading.Event()
        def prepare(name):
            entered.set();release.wait();return review()
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            service=FakeService([])
            service.review_profile=prepare
            app=Cockpit(Path(tmp),service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                await app.push_screen(EditProfile(record(True)))
                app.screen.query_one('#profile-edit-go',Button).press()
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,10))
                    await pilot.press('escape')
                    replacement=EditProfile(record())
                    await app.push_screen(replacement)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen,replacement)
                self.assertIsNotNone(app.jobs[-1].followup)

    async def test_customized_copy_rebuild_and_settings_use_saved_recipe_without_editable_mount(self):
        row=record()
        row['config']['profile']['customized_from']={'name':'base','digest':'original'}
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(tmp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                self.assertIn(('Rebuild sandbox…','rebuild'),MoreActions(row).actions)
                (Path(tmp)/'example').mkdir()
                (Path(tmp)/'example/allowlist.txt').write_text('')
                self.assertEqual(app.configuration_draft(row)['profile'],row['config']['profile']['path'])
                app.perform('rebuild',record=row)
                await pilot.pause()
                self.assertIn('saved profile',app.screen.message)
                with patch.object(app,'start_rebuild') as rebuild:
                    await pilot.press('enter')
                    await pilot.pause()
                    rebuild.assert_called_once_with('example',['recreate','example'])
                await app.push_screen(AppliedProfile(row,Path(tmp)/'example'))
                await pilot.pause()
                text=' '.join(str(item.render()) for item in app.screen.query(Static))
                self.assertIn('Customized from: base · original',text)
                self.assertLessEqual(app.screen.query_one('.form').region.width,100)
                self.assertEqual(app.screen.query_one('.form').region.width,app.screen.query_one('.wizard-actions').region.width)
                await pilot.press('escape')
                draft=copy_draft(row,Path(tmp))
                copied=NewSandbox(draft,search_keys={})
                await app.push_screen(copied)
                await pilot.pause()
                self.assertEqual(copied.profiles[draft['profile']],
                                 row['config']['profile']['description']+' (saved customized profile)')

    def test_replacement_notice_covers_state_changes_while_review_is_open(self):
        self.assertEqual(consequences(record(running=True)),consequences(record(running=False)))
        self.assertIn('If example is stopped, it stays stopped.',consequences(record())[1])


if __name__=='__main__':
    unittest.main()
