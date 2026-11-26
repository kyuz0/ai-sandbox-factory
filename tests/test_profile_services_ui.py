"""Profile setup and status rendering, including dismissed asynchronous queries."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static, Checkbox
from textual.screen import Screen
from ai_sandbox_factory import profile_edit, sandbox, profile_services
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, CopyWebAccess, DEFAULT_SETTINGS, NewSandbox, ProfileServicesHelp, SettingsForm, CreateFromSandbox, CopySandboxHelp, preset, copy_draft
from test_tui import FakeService


class ProfileServicesUI(unittest.IsolatedAsyncioTestCase):
    async def test_profile_page_identifies_the_main_agent_separately_from_services(self):
        for size in ((80,24), (120,40), (220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp, \
                    patch('ai_sandbox_factory.updates.available_update', return_value=None), \
                    patch('ai_sandbox_factory.profile_services.status', return_value=[{'name':'database','running':True}]):
                app = Cockpit(Path(tmp), FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    for profile in ('base', 'developer-postgres'):
                        config = {'profile': sandbox.load_profile(profile)}
                        page = AppliedProfile({'name':'example', 'config':config}, Path(tmp))
                        await app.push_screen(page)
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        heading = page.query_one('#profile-main-agent', Static)
                        self.assertEqual(str(heading.render()), 'Main agent sandbox')
                        self.assertTrue(heading.styles.text_style.bold)
                        self.assertTrue(page.query_one('.form').content_region.contains_region(heading.region))
                        text = ' '.join(str(widget.render()) for widget in page.query(Static))
                        self.assertIn(config['profile']['description'], text)
                        self.assertEqual(bool(page.query('#profile-service-database')), profile == 'developer-postgres')
                        back = page.query_one('#profile-information-back', Button)
                        self.assertEqual(back.region.height, 1)
                        self.assertLess(back.region.bottom, size[1])
                        await pilot.press('escape')

    async def test_copy_hides_search_keys_when_absent_and_explains_a_stopped_source_with_keys(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            root=Path(tmp)
            records=[]
            for name,keys,running,agent in (('empty',False,True,'pi'),('with-keys',True,True,'pi'),
                                          ('stopped',True,False,'pi'),('native',True,True,'codex')):
                state=root/name
                state.mkdir()
                (state/'allowlist.txt').write_text('')
                records.append({'name':name,'dev':'running' if running else 'stopped','config':{
                    'primary_agent':agent,'engine':'podman','workspace':'/home/example/project',
                    'profile':sandbox.load_profile('base'),
                    'options':{'web_access':{'providers':{'exa':{'has_key':keys}}}}}})
            service=FakeService([])
            service.search_keys=lambda name:self.fail('Opening or copying without selected keys must not export credentials')
            app=Cockpit(root,service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                result=[]
                screen=CreateFromSandbox(records)
                await app.push_screen(screen,result.append)
                await pilot.pause()
                keys=screen.query_one('#copy-keys',Checkbox)
                help_text=screen.query_one('#copy-keys-help',Static)
                for name,visible,disabled in (('empty',False,False),('with-keys',True,False),
                                               ('stopped',True,True),('native',False,True)):
                    screen.query_one('#copy-source',Select).value=name
                    await pilot.pause()
                    self.assertEqual(keys.display,visible,name)
                    self.assertEqual(keys.disabled,disabled,name)
                    self.assertFalse(keys.value,name)
                    self.assertEqual(help_text.display,visible and disabled,name)
                screen.query_one('#copy-source',Select).value='empty'
                await pilot.pause()
                screen.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(result[0][1],{})
                web=CopyWebAccess(records[:3])
                await app.push_screen(web)
                await pilot.pause()
                self.assertFalse(web.query_one('#copy-web-keys',Checkbox).display)
                self.assertFalse(web.query_one('#copy-web-help',Static).display)
                web.query_one('#copy-web-source',Select).value='with-keys'
                await pilot.pause()
                self.assertTrue(web.query_one('#copy-web-keys',Checkbox).display)
                self.assertFalse(web.query_one('#copy-web-keys',Checkbox).value)
                self.assertEqual(str(web.query_one('#copy-web-keys',Checkbox).label),'Include Web Search API keys')

    async def test_copy_freezes_accepted_choices_discards_late_keys_and_preserves_failure_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            root=Path(tmp)
            (root/'source').mkdir()
            (root/'source/allowlist.txt').write_text('')
            config={'primary_agent':'pi','engine':'podman','workspace':'/home/example/project',
                    'options':{'web_access':{'providers':{'exa':{'has_key':True}}}},
                    'profile':sandbox.load_profile('base'),profile_edit.EDIT:{'path':'/home/example/profiles/shared',
                    'origin_name':'base','origin_digest':'1234567890abcdef'}}
            source={'name':'source','dev':'running','config':config}
            entered,release,returned=threading.Event(),threading.Event(),threading.Event()
            def keys(name):
                entered.set()
                try:release.wait();return {}
                finally:returned.set()
            service=FakeService([])
            service.search_keys=keys
            app=Cockpit(root,service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                results=[]
                screen=CreateFromSandbox([source])
                await app.push_screen(screen,results.append)
                await pilot.pause()
                screen.query_one('#copy-share-profile',Checkbox).value=True
                screen.query_one('#copy-keys',Checkbox).value=True
                screen.query_one('#copy-continue',Button).press()
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,10))
                    for selector in ('#copy-source','#copy-share-profile','#copy-keys','#copy-information','#copy-continue'):
                        self.assertTrue(screen.query_one(selector).disabled)
                    screen.query_one('#copy-cancel',Button).press()
                    await pilot.pause()
                    replacement=Screen()
                    await app.push_screen(replacement)
                finally:
                    release.set()
                self.assertTrue(await asyncio.to_thread(returned.wait,10))
                await pilot.pause()
                self.assertIs(app.screen,replacement)
                self.assertEqual(results,[None])
                await app.pop_screen()
                retry=CreateFromSandbox([source])
                await app.push_screen(retry,results.append)
                await pilot.pause()
                retry.query_one('#copy-share-profile',Checkbox).value=True
                retry.query_one('#copy-keys',Checkbox).value=True
                def failed(name):raise ValueError('Saved key store could not be read: fixture I/O failure. Retry.')
                service.search_keys=failed
                retry.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn('fixture I/O failure',str(retry.query_one('#copy-error',Static).render()))
                self.assertTrue(retry.query_one('#copy-share-profile',Checkbox).value)
                self.assertIs(app.focused,retry.query_one('#copy-keys',Checkbox))
                keys=retry.query_one('#copy-keys',Checkbox)
                fields=retry.query_one('#copy-sandbox-fields')
                self.assertGreaterEqual(keys.region.y,fields.content_region.y)
                self.assertLessEqual(keys.region.bottom,fields.content_region.bottom)
                feedback=retry.query_one('#copy-error',Static)
                self.assertTrue(fields.content_region.contains_region(feedback.region))
                self.assertFalse(retry.query_one('#copy-share-profile',Checkbox).disabled)
                self.assertFalse(retry.query_one('#copy-source',Select).disabled)
                self.assertFalse(retry.query_one('#copy-continue',Button).disabled)
                def unexpected(name):raise TypeError('fixture malformed key status')
                service.search_keys=unexpected
                retry.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                message=str(retry.query_one('#copy-error',Static).render())
                self.assertIn('Cannot prepare a copy of source',message)
                self.assertIn('TypeError: fixture malformed key status',message)
                self.assertFalse(retry.query_one('#copy-continue',Button).disabled)
                retry.query_one('#copy-source',Select).focus()
                with patch('ai_sandbox_factory.sandbox_tui.copy_draft',side_effect=KeyError('providers')):
                    retry.query_one('#copy-continue',Button).press()
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                message=str(retry.query_one('#copy-error',Static).render())
                self.assertIn('copy of source while reading saved settings',message)
                self.assertIn("KeyError: 'providers'",message)
                self.assertIn('Check the source settings',message)
                self.assertIsNot(app.focused,retry.query_one('#copy-keys',Checkbox))
                details='\n'.join('Diagnostic line '+str(i) for i in range(60))
                def long_failure(name):raise ValueError(details)
                service.search_keys=long_failure
                retry.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn('Diagnostic line 59',str(retry.query_one('#copy-error',Static).render()))
                feedback=retry.query_one('#copy-error',Static)
                self.assertIs(app.focused,retry.query_one('#copy-sandbox-fields'))
                selected=retry.query_one('#copy-keys',Checkbox).value
                await pilot.press('space')
                self.assertEqual(retry.query_one('#copy-keys',Checkbox).value,selected)
                self.assertGreaterEqual(feedback.region.y,fields.content_region.y)
                self.assertLess(feedback.region.y,fields.content_region.bottom)
                for action in ('copy-cancel','copy-continue'):
                    self.assertTrue(app.screen.region.contains_region(retry.query_one('#'+action,Button).region))
                service.search_keys=lambda name:{}
                retry.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(results[-1][0]['share_editable_profile'])

    def test_copy_picker_uses_completed_sources_only(self):
        config={'profile':sandbox.load_profile('base'),'workspace':'/home/example/project'}
        ready={'name':'ready','dev':'stopped','config':config}
        creating={'name':'creating','creation':'running','config':config}
        screen=CreateFromSandbox([ready,creating],selected='creating')
        self.assertEqual(list(screen.records),['ready'])
        self.assertEqual(screen.selected,'ready')
        with self.assertRaisesRegex(ValueError,'No completed sandbox.*Finish creating'):
            CreateFromSandbox([creating])

    async def test_copy_action_reports_no_completed_source_without_leaving_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app=Cockpit(Path(tmp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                dashboard=app.screen
                app.records=[{'name':'creating','creation':'running','config':{'profile':sandbox.load_profile('base')}}]
                with patch.object(app,'notify') as notice:
                    app.perform_action('create-from')
                self.assertIs(app.screen,dashboard)
                self.assertIn('No completed sandbox',notice.call_args.args[0])
                self.assertIn('Finish creating',notice.call_args.args[0])

    async def test_copy_sharing_is_explicit_resets_with_source_and_is_clear_on_review(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            root=Path(tmp)
            (root/'source').mkdir()
            (root/'source/allowlist.txt').write_text('')
            config={'primary_agent':'pi','engine':'podman','workspace':'/home/example/project',
                    'options':{'web_access':{'providers':{'exa':{'has_key':True}}}},
                    'profile':sandbox.load_profile('base'),profile_edit.EDIT:{'path':'/home/example/profiles/shared',
                    'origin_name':'base','origin_digest':'1234567890abcdef'}}
            source={'name':'source','dev':'running','config':config}
            plain={'name':'plain','dev':'running','config':{key:value for key,value in config.items() if key!=profile_edit.EDIT}}
            app=Cockpit(root,FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                result=[]
                screen=CreateFromSandbox([source,plain])
                await app.push_screen(screen,result.append)
                await pilot.pause()
                share=screen.query_one('#copy-share-profile',Checkbox)
                self.assertEqual(str(screen.query_one('#copy-keys',Checkbox).label),'Include Web Search API keys')
                self.assertFalse(screen.query('#copy-sign-in-help'))
                screen.query_one('#copy-information',Button).press()
                await pilot.pause()
                self.assertIsInstance(app.screen,CopySandboxHelp)
                credentials=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertIn('Model and advisor credentials, including sign-ins, are not copied',credentials)
                self.assertIn('Review sandbox',credentials)
                await pilot.press('escape')
                self.assertFalse(share.value)
                self.assertTrue(screen.query_one('#copy-profile-setting').display)
                def sharing_help():
                    return ' '.join(str(widget.render()) for widget in screen.query('#copy-profile-setting Static')
                                    if not isinstance(widget,Checkbox))
                before=(str(share.label),sharing_help())
                self.assertIn('Both sandboxes use the same editable profile files',before[1])
                self.assertNotIn('own Apply',before[1])
                self.assertFalse(screen.query('#copy-profile-version'))
                share.value=True
                await pilot.pause()
                self.assertEqual(before,(str(share.label),sharing_help()))
                screen.query_one('#copy-source',Select).value='plain'
                await pilot.pause()
                self.assertFalse(share.value)
                self.assertFalse(screen.query_one('#copy-profile-setting').display)
                screen.query_one('#copy-source',Select).value='source'
                await pilot.pause()
                share.value=True
                screen.query_one('#copy-continue',Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(result[0][0]['share_editable_profile'])
                for shared in (False,True):
                    draft=copy_draft(source,root)
                    draft.update(name='new-project',share_editable_profile=shared)
                    form=SettingsForm(draft)
                    await app.push_screen(form)
                    await pilot.pause()
                    text=str(form.query_one('#review-profile-copy',Static).render())
                    self.assertIn('shared with source' if shared else 'not shared',text)
                    if shared:self.assertIn('Editing files does not update either sandbox automatically',text)
                    await pilot.press('escape')

    async def test_profile_selection_for_every_agent_and_service_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            for agent in ('pi', 'codex', 'claude', 'agy'):
                with self.subTest(agent=agent), patch('ai_sandbox_factory.updates.available_update', return_value=None):
                    app = Cockpit(Path(tmp), FakeService([]))
                    async with app.run_test(size=(80, 24)) as pilot:
                        await app.workers.wait_for_complete()
                        new = NewSandbox()
                        await app.push_screen(new)
                        new.query_one('#primary-agent', Select).value = agent
                        await pilot.pause()
                        self.assertTrue(new.query_one('#profile-setting').display)
                        new.query_one('#profile', Select).value = 'developer-postgres'
                        await pilot.pause()
                        draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='example', primary_agent=agent, workspace='/home/example/project')
                        form = SettingsForm(draft)
                        await app.push_screen(form)
                        await pilot.pause()
                        self.assertIn('Developer + PostgreSQL', str(form.query_one('#review-project', Static).render()))
                        self.assertIn('database', ' '.join(str(widget.render()) for widget in form.query('#review-profile-services Static')))
                        self.assertEqual(form.query_one('#apply', Button).region.height, 1)
                        review = '\n'.join(str(widget.render()) for widget in form.query('#review-profile-services Static'))
                        self.assertNotIn('application data is not copied', review)
                        self.assertNotIn('permanently deletes', review)
                        self.assertNotIn('workspace rebuild', review)
                        self.assertFalse(form.query('#review-profile-services Bullet'))
                        form.query_one('#profile-services-information',Button).press()
                        await pilot.pause()
                        self.assertIsInstance(app.screen,ProfileServicesHelp)
                        await pilot.press('escape')
                        self.assertIs(app.screen,form)

    async def test_service_reference_is_only_in_help_and_dismissal_preserves_each_page(self):
        for size in ((80,24),(120,40),(220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                    patch('ai_sandbox_factory.profile_services.status',return_value=[{'name':'database','running':True}]):
                app=Cockpit(Path(tmp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    draft=preset('developer-postgres',copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='example',workspace='/home/example/project')
                    pages=[SettingsForm(draft),AppliedProfile({'name':'example','config':{
                        'profile':sandbox.load_profile('developer-postgres')}},Path(tmp))]
                    for page in pages:
                        await app.push_screen(page)
                        await app.workers.wait_for_complete()
                        if isinstance(page,SettingsForm):
                            page.query_one('#memory',Input).value='12g'
                            page.query_one('#open-editor',Checkbox).value=False
                        before=copy.deepcopy(draft)
                        text=' '.join(str(widget.render()) for widget in page.query(Static))
                        for reference in ('Profile services start with','Existing application data is not copied',
                                          'application data is permanently deleted','Stop, start and rebuild'):
                            self.assertNotIn(reference,text)
                        control=page.query_one('#profile-services-information',Button)
                        for close in ('escape','button'):
                            control.focus()
                            control.scroll_visible(animate=False)
                            await pilot.pause()
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertIsInstance(app.screen,ProfileServicesHelp)
                            self.assertEqual(app.focused.id,'close-profile-services-help')
                            text=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                            for reference in ('Profile services start with this sandbox','Application data is kept',
                                              'Existing application data is not copied','application data is permanently deleted'):
                                self.assertIn(reference,text)
                            self.assertFalse(app.screen.query('Bullet'))
                            button=app.screen.query_one('#close-profile-services-help',Button)
                            self.assertEqual(button.region.height,1)
                            self.assertTrue(app.screen.query_one('#confirm-box').content_region.contains_region(button.region))
                            if close=='escape':
                                await pilot.press('escape')
                            else:
                                await pilot.click('#close-profile-services-help')
                            await pilot.pause()
                            self.assertIs(app.screen,page)
                            self.assertIs(app.focused,control)
                            self.assertEqual(draft,before)
                            if isinstance(page,SettingsForm):
                                self.assertEqual(page.query_one('#memory',Input).value,'12g')
                                self.assertFalse(page.query_one('#open-editor',Checkbox).value)
                        await app.pop_screen()
                    plain_draft=preset('base',copy.deepcopy(DEFAULT_SETTINGS))
                    plain_draft.update(name='example',workspace='/home/example/project')
                    plain=SettingsForm(plain_draft)
                    await app.push_screen(plain)
                    self.assertFalse(plain.query('#profile-services-information'))

    async def test_copy_and_rebuild_explain_data_and_process_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                records = [{'name':name, 'dev':'running', 'config':{'primary_agent':'pi', 'engine':'podman',
                            'profile':sandbox.load_profile(profile)}}
                           for name, profile in (('database-project','developer-postgres'), ('plain-project','base'))]
                screen = CreateFromSandbox(records)
                await app.push_screen(screen)
                await pilot.pause()
                self.assertGreaterEqual(screen.query_one('#copy-source-label').region.y,
                                        screen.query_one('#copy-sandbox-fields').content_region.y)
                self.assertFalse(screen.query_one('#copy-credentials').display)
                self.assertFalse(screen.query('#copy-data-help'))
                screen.query_one('#copy-information',Button).press()
                await pilot.pause()
                text=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertIn('Application data, such as databases, is not copied',text)
                await pilot.press('escape')
                screen.query_one('#copy-source', Select).value = 'plain-project'
                await pilot.pause()
                screen.query_one('#copy-information',Button).press()
                await pilot.pause()
                text=' '.join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertNotIn('Application data',text)
                await pilot.press('escape')
                await pilot.press('escape')
                for agent in ('pi','codex','claude','agy'):
                    record = copy.deepcopy(records[0])
                    record['config']['primary_agent'] = agent
                    app.perform('rebuild', record=record)
                    await pilot.pause()
                    message = app.screen.message
                    self.assertIn('If the sandbox is running, work in its main agent sandbox stops after the build', message)
                    self.assertIn('If the sandbox is stopped, it stays stopped.', message)
                    self.assertIn('Profile services and their application data are kept', message)
                    self.assertNotIn('Fetch current profile sources', message)
                    await pilot.press('escape')

    async def test_rebuild_names_the_actual_profile_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                profile = sandbox.load_profile('developer-postgres')
                record = {'name':'example', 'dev':'stopped', 'config':{'profile':profile, 'primary_agent':'pi'}}
                for installed in (True, False):
                    with patch('ai_sandbox_factory.sandbox_tui.profile_titles',
                               return_value={'developer-postgres':'Developer + PostgreSQL'} if installed else {}):
                        app.perform('rebuild', record=record)
                        await pilot.pause()
                        self.assertIn('the installed profile' if installed else 'its saved profile', app.screen.message)
                        self.assertIn('If the sandbox is stopped, it stays stopped.', app.screen.message)
                        self.assertEqual(app.focused.id, 'yes')
                        with patch.object(app, 'start_rebuild') as rebuild:
                            await pilot.press('enter')
                            await pilot.pause()
                            rebuild.assert_called_once_with('example', ['recreate', 'example', '--profile',
                                'developer-postgres' if installed else profile['path']])

    async def test_wrapped_features_summary_preserves_keyboard_and_live_values(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            for size in ((80,24),(120,40),(220,50)):
                with self.subTest(size=size):
                    app = Cockpit(Path(tmp), FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await app.workers.wait_for_complete()
                        draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
                        draft.update(name='example', workspace='/home/example/project')
                        screen = SettingsForm(draft)
                        await app.push_screen(screen)
                        section = screen.query_one('#skills-section')
                        title = section.query_one('CollapsibleTitle')
                        title.focus()
                        title.scroll_visible(top=True, animate=False)
                        await pilot.pause()
                        self.assertIn('Browser on', title.label.plain)
                        self.assertGreater(title.region.height, 1)
                        self.assertTrue(section.collapsed)
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertFalse(section.collapsed)
                        screen.query_one('#browser', Checkbox).value = False
                        await pilot.pause()
                        self.assertIn('Browser off', title.label.plain)
                        title.focus()
                        await pilot.press('enter')
                        await pilot.pause()
                        self.assertTrue(section.collapsed)
                        self.assertIn('Browser off', title.label.plain)

    async def test_dismissed_status_result_cannot_update_another_page(self):
        entered, release = threading.Event(), threading.Event()
        def status(config):
            entered.set(); release.wait()
            return [{'name': 'database', 'running': True}]
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None), \
                patch('ai_sandbox_factory.profile_services.status', side_effect=status):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await app.workers.wait_for_complete()
                screen = AppliedProfile({'name': 'example', 'config': {'profile': sandbox.load_profile('developer-postgres')}}, Path(tmp))
                try:
                    await app.push_screen(screen)
                    await asyncio.to_thread(entered.wait)
                    await pilot.press('escape')
                    self.assertNotIn(screen, app.screen_stack)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen, app.screen_stack[0])

    async def test_old_service_status_cannot_update_a_refreshed_profile_page(self):
        for fail in (False,True):
            entered, release=threading.Event(),threading.Event()
            next_entered,next_release=threading.Event(),threading.Event()
            calls=[]
            def status(config):
                calls.append(config)
                if len(calls)==1:
                    entered.set();release.wait()
                    if fail: raise ValueError('obsolete service failed')
                    return [{'name':'database','running':True}]
                next_entered.set();next_release.wait()
                return [{'name':'database','running':False}]
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                    patch('ai_sandbox_factory.profile_services.status',side_effect=status):
                service=FakeService([])
                app=Cockpit(Path(tmp),service)
                async with app.run_test(size=(80,24)) as pilot:
                    await app.workers.wait_for_complete()
                    row={'name':'example','config':{'profile':sandbox.load_profile('developer-postgres')}}
                    page=AppliedProfile(row,Path(tmp))
                    try:
                        await app.push_screen(page)
                        self.assertTrue(await asyncio.to_thread(entered.wait,10))
                        old_worker=next(worker for worker in app.workers if worker.name=='load_status')
                        await app.push_screen(ProfileServicesHelp())
                        updated=copy.deepcopy(row)
                        updated['config']['profile']['services']['database']['description']='Updated database description'
                        service.config=lambda name:copy.deepcopy(updated['config'])
                        page.return_focus='profile-information-back'
                        await pilot.press('escape')
                        self.assertTrue(await asyncio.to_thread(next_entered.wait,10))
                        status_widget=page.query_one('#profile-service-status')
                        label=page.query_one('#profile-service-database')
                        before=(str(status_widget.render()),status_widget.display,str(label.render()))
                        release.set()
                        await old_worker.wait()
                        await pilot.pause()
                        self.assertIsNone(old_worker.error)
                        self.assertEqual((str(status_widget.render()),status_widget.display,str(label.render())),before)
                        next_release.set()
                        await app.workers.wait_for_complete()
                        self.assertIn('Stopped',str(label.render()))
                        self.assertTrue(all(worker.error is None for worker in app.workers))
                    finally:
                        release.set();next_release.set()
