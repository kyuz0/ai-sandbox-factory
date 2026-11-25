"""Profile editing labels, exact review actions, asynchronous ownership and layout."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Static
from ai_sandbox_factory import profile_edit, sandbox
from ai_sandbox_factory.profile_edit_ui import EditProfile, ReviewProfile, consequences
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, MoreActions, NewSandbox, copy_draft
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
                        for screen in (EditProfile(record()),EditProfile(record(True)),
                                       EditProfile(record(True),error='profile.json: line 4, column 8: expected a comma. Fix the JSON and validate again.'),
                                       ReviewProfile(record(True),review())):
                            await app.push_screen(screen)
                            await pilot.pause()
                            check_input_layout(screen)
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
