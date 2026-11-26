"""Copying rules are optional help; opening them preserves accepted choices."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Label, Select, Static
from ai_sandbox_factory import profile_edit, sandbox
from ai_sandbox_factory.sandbox_tui import Cockpit, CopySandboxHelp, CreateFromSandbox
from test_tui import FakeService


def source():
    return {'name':'example','dev':'running','config':{'primary_agent':'pi','engine':'podman',
        'workspace':'/home/example/project',
        'profile':sandbox.load_profile('developer-postgres'),
        profile_edit.EDIT:{'path':'/home/example/sandbox-profiles/project'},
        'options':{'web_access':{'providers':{'exa':{'has_key':True}}}}}}


class CopyHelpUI(unittest.IsolatedAsyncioTestCase):
    async def test_monochrome_titles_render_bold_white_in_both_dialogs(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                patch.dict(os.environ,NO_COLOR='1',TEXTUAL_COLOR_SYSTEM='truecolor'):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                for page in (CreateFromSandbox([source()]),CopySandboxHelp(source())):
                    await app.push_screen(page)
                    await pilot.pause()
                    segments=[segment for segment in page.query_one('.title',Label).render_line(0) if segment.text.strip()]
                    self.assertTrue(segments)
                    for segment in segments:
                        self.assertTrue(segment.style.bold)
                        self.assertEqual(segment.style.color.get_truecolor(),(255,255,255))
                    await pilot.press('escape')

    async def test_help_preserves_choices_and_keeps_details_out_of_the_main_form(self):
        for size in ((80,24),(120,40),(220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                    patch('ai_sandbox_factory.sandbox_tui.copy_draft') as copy_settings:
                app=Cockpit(Path(temp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    page=CreateFromSandbox([source()])
                    await app.push_screen(page)
                    await pilot.pause()
                    self.assertEqual(app.focused.id,'copy-source')
                    for field in ('copy-share-profile','copy-keys'):
                        page.query_one('#'+field,Checkbox).value=True
                    text=' '.join(str(widget.render()) for widget in page.query(Static))
                    for hidden in ('Application data','Model and advisor credentials','Review changes…',
                                   '/home/example/sandbox-profiles','profile version installed'):
                        self.assertNotIn(hidden,text)
                    self.assertLessEqual(page.query_one('#copy-sandbox-fields').virtual_size.height,
                                         page.query_one('#copy-sandbox-fields').content_size.height)
                    self.assertEqual(page.query_one('#copy-share-profile').region.y-page.query_one('#copy-source').region.bottom,1)
                    self.assertEqual(page.query_one('#copy-keys').region.y-page.query_one('#copy-share-help').region.bottom,1)
                    self.assertEqual(page.query_one('#copy-continue').region.y-page.query_one('#copy-keys').region.bottom,1)
                    self.assertEqual(page.query_one('#copy-continue').region.right,page.query_one('#copy-source').region.right)
                    self.assertEqual(page.query_one('#copy-information').region.right,page.query_one('#copy-source').region.right)
                    for close in ('keyboard','button'):
                        page.query_one('#copy-information',Button).focus()
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertIsInstance(app.screen,CopySandboxHelp)
                        self.assertEqual(app.focused.id,'close-copy-help')
                        details=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                        for expected in ('network rules','profile version installed in the source sandbox, example','Profile folder',
                                         'Leave it unchecked','Editing files does not update either sandbox',
                                         'Editable profile… → Review changes… → Apply profile changes',
                                         'To edit a separate copy later, choose Actions… → Profile and services… → Make profile editable…',
                                         'Project files are not copied','Application data',
                                         'Model and advisor credentials, including sign-ins, are not copied','SSH keys, browser sessions and saved chats'):
                            self.assertIn(expected,details)
                        body=app.screen.query_one('#copy-help-body')
                        button=app.screen.query_one('#close-copy-help')
                        self.assertEqual(button.region.bottom,app.screen.query_one('#confirm-box').content_region.bottom)
                        self.assertEqual(button.region.y-body.region.bottom,1)
                        if body.max_scroll_y:
                            await pilot.press('shift+tab','end')
                            await pilot.wait_for_scheduled_animations()
                            self.assertIs(app.focused,body)
                            self.assertEqual(body.scroll_y,body.max_scroll_y)
                        if close=='keyboard': await pilot.press('escape')
                        else: app.screen.query_one('#close-copy-help',Button).press()
                        await pilot.pause()
                        self.assertIs(app.screen,page)
                        self.assertEqual(page.query_one('#copy-source',Select).value,'example')
                        self.assertTrue(page.query_one('#copy-share-profile',Checkbox).value)
                        self.assertTrue(page.query_one('#copy-keys',Checkbox).value)
                        self.assertEqual(app.focused.id,'copy-information')
                        self.assertIsNone(page._copy_request)
                        copy_settings.assert_not_called()

    async def test_help_tracks_the_selected_source_and_omits_irrelevant_sections(self):
        rich=source()
        plain=copy.deepcopy(rich)
        plain.update(name='plain')
        plain['config'].pop(profile_edit.EDIT)
        plain['config'].update(primary_agent='codex',profile=sandbox.load_profile('base'),options={})
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=CreateFromSandbox([rich,plain])
                await app.push_screen(page)
                page.query_one('#copy-source',Select).value='plain'
                await pilot.pause()
                self.assertFalse(page.query_one('#copy-profile-setting').display)
                self.assertFalse(page.query_one('#copy-credentials').display)
                page.query_one('#copy-information',Button).press()
                await pilot.pause()
                text=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertIn('source sandbox, plain',text)
                self.assertIn('including sign-ins, are not copied',text)
                for absent in ('Sharing the profile folder','Application data','Profile folder'):
                    self.assertNotIn(absent,text)
