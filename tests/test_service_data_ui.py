"""Saved service data is discoverable and recovery choices name their effects."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static

from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, MoreActions
from ai_sandbox_factory.service_data_ui import ExportServiceData, RestoreServiceData, SavedServiceData
from test_profile_edit_ui import record
from test_tui import FakeService
from tui_geometry import check_input_layout, check_dropdown_layout


def recovery_record():
    row = record(True)
    row['config']['profile']['services'] = {'database': {'description':'Database for the project', 'image': 'example/db:2', 'volumes': {'data': '/var/lib/db'}}}
    saved = {'id':'0123456789abcdef0123456789abcdef','time':'2026-11-25T12:30:00+00:00','reason':'replaced',
             'service':'database','image':'example/db:1','image_id':'sha256:old-image','profile_digest':'previous-version',
             'volumes':{'data':{'target':'/var/lib/db','volume':'pi-sandbox-factory-example-profile-data-0123456789abcdef'}}}
    row['config']['profile_service_recovery'] = [saved]
    return row, saved


class ServiceDataUI(unittest.IsolatedAsyncioTestCase):
    async def test_password_notice_only_when_restoring_into_another_service(self):
        row,saved = recovery_record()
        row['config']['profile']['services']['other'] = copy.deepcopy(row['config']['profile']['services']['database'])
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app = Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                screen = RestoreServiceData(row,saved)
                await app.push_screen(screen)
                await pilot.pause()
                self.assertFalse(screen.query_one('#service-restore-credentials').display)
                screen.query_one('#service-restore-target',Select).value = 'other'
                await pilot.pause()
                self.assertTrue(screen.query_one('#service-restore-credentials').display)
                self.assertIn('come from database',str(screen.query_one('#service-restore-credentials .bullet-text').render()))
                screen.query_one('#service-restore-target',Select).value = 'database'
                await pilot.pause()
                self.assertFalse(screen.query_one('#service-restore-credentials').display)

    async def test_reopening_saved_data_reads_the_latest_catalog(self):
        old,saved = recovery_record()
        updated = copy.deepcopy(old)
        updated['config']['profile_service_recovery'].append({**saved,'id':'newest','reason':'restored'})
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app = Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                with patch.object(app.service,'config',return_value=updated['config']):
                    app.perform('saved-service-data',record=old)
                await pilot.pause()
                self.assertEqual(app.screen.selected()['id'],'newest')

    async def test_profile_page_only_shows_saved_data_when_it_exists(self):
        plain = record()
        with_data, _ = recovery_record()
        removed=copy.deepcopy(with_data)
        removed['config']['profile'].pop('services')
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                patch('ai_sandbox_factory.profile_services.status',return_value=[]):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                for row,present in ((plain,False),(with_data,True),(removed,True)):
                    actions=MoreActions(row).actions
                    self.assertIn(('Profile and services…','profile-information'),actions)
                    self.assertNotIn(('Edit profile…','edit-profile'),actions)
                    self.assertNotIn(('Saved service data…','saved-service-data'),actions)
                    page=AppliedProfile(row,Path(temp))
                    await app.push_screen(page)
                    await pilot.pause()
                    self.assertEqual(bool(page.query('#profile-information-data')),present)
                    await pilot.press('escape')

    async def test_profile_data_entry_is_keyboard_accessible_and_respects_running_job(self):
        row,_=recovery_record()
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                patch('ai_sandbox_factory.profile_services.status',return_value=[]):
            service=FakeService([])
            service.config=lambda name:copy.deepcopy(row['config'])
            app=Cockpit(Path(temp),service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(row,Path(temp))
                await app.push_screen(page)
                page.query_one('#profile-information-data',Button).focus()
                await pilot.pause()
                self.assertTrue(page.query_one('.form').content_region.contains_region(app.focused.region))
                with patch.object(app.service,'config',return_value=row['config']):
                    await pilot.press('enter')
                    await pilot.pause()
                self.assertIsInstance(app.screen,SavedServiceData)
                updated=copy.deepcopy(row['config'])
                updated['profile_service_recovery'].append({**updated['profile_service_recovery'][0],'id':'latest-copy'})
                service.config=lambda name:copy.deepcopy(updated)
                await pilot.press('escape')
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.focused.id,'profile-information-data')
                self.assertTrue(page.query_one('.form').content_region.contains_region(app.focused.region))
                with patch.object(app,'active_job',return_value=object()),patch.object(app,'show_job') as show:
                    page.query_one('#profile-information-data',Button).focus()
                    await pilot.press('enter')
                    await pilot.pause()
                    show.assert_called_once()
                    self.assertIs(app.screen,page)

    async def test_profile_refresh_error_stays_visible_when_returning_to_saved_data(self):
        row,_=recovery_record()
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                patch('ai_sandbox_factory.profile_services.status',return_value=[]):
            service=FakeService([])
            service.config=lambda name:copy.deepcopy(row['config'])
            app=Cockpit(Path(temp),service)
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(row,Path(temp))
                await app.push_screen(page)
                page.query_one('#profile-information-data',Button).focus()
                await pilot.press('enter')
                with patch.object(service,'config',side_effect=ValueError('config.json: unreadable saved profile')):
                    await pilot.press('escape')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertEqual(app.focused.id,'profile-information-data')
                error=page.query_one('#profile-information-error')
                self.assertIn('unreadable saved profile',str(error.render()))
                self.assertTrue(page.region.contains_region(error.region))
                self.assertLessEqual(error.region.bottom,page.query_one('.form').region.y)
                self.assertTrue(page.query_one('.form').content_region.contains_region(app.focused.region))

    async def test_failed_saved_data_open_does_not_leave_a_return_action(self):
        row,_=recovery_record()
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
                patch('ai_sandbox_factory.profile_services.status',return_value=[]):
            app=Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                page=AppliedProfile(row,Path(temp))
                await app.push_screen(page)
                with patch.object(app.service,'config',side_effect=ValueError('saved config unreadable')) as read:
                    page.query_one('#profile-information-data',Button).press()
                    await pilot.pause()
                    self.assertIsNone(page.return_focus)
                    page.query_one('#profile-services-information',Button).focus()
                    await pilot.press('enter','escape')
                    await app.workers.wait_for_complete()
                    self.assertEqual(read.call_count,1)

    async def test_three_sizes_find_export_restore_and_cancel(self):
        for size in ((80,24),(120,40),(220,50)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp, \
                    patch('ai_sandbox_factory.updates.available_update',return_value=None):
                row,saved = recovery_record()
                pointer = Path(temp)/'example/profile-runtime/applied.json'
                pointer.parent.mkdir(parents=True)
                pointer.write_text(json.dumps({'services':{'database':'sha256:0123456789abcdef'}}))
                app = Cockpit(Path(temp),FakeService([]))
                async with app.run_test(size=size) as pilot:
                    await app.workers.wait_for_complete()
                    page = SavedServiceData(row)
                    await app.push_screen(page)
                    await pilot.pause()
                    self.assertEqual(app.focused.id,'saved-data-set')
                    text = ' '.join(str(widget.render()) for widget in page.query(Static))
                    for expected in ('Factory keeps','on this computer','database','previous-version','example/db:1'):
                        self.assertIn(expected,text)
                    check_dropdown_layout(page)
                    page.query_one('#saved-data-export',Button).press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen,ExportServiceData)
                    self.assertEqual(app.focused.id,'service-export-path')
                    check_input_layout(app.screen)
                    app.screen.query_one('#service-export-path',Input).value = ''
                    app.screen.query_one('#service-export-go',Button).press()
                    await pilot.pause()
                    self.assertIn('Enter a new archive file',str(app.screen.query_one('#service-export-error').render()))
                    await pilot.press('escape')
                    page.query_one('#saved-data-restore',Button).press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen,RestoreServiceData)
                    self.assertEqual(app.focused.id,'service-restore-target')
                    text = ' '.join(str(widget.render()) for widget in app.screen.query(Static))
                    self.assertIn('example/db:2',text)
                    self.assertIn('0123456789ab',text)
                    self.assertIn('example/db:1',text)
                    self.assertIn('current data is kept',text)
                    with patch.object(app,'start_settings_job') as start:
                        app.screen.query_one('#service-restore-go',Button).press()
                        await pilot.pause()
                        self.assertEqual(app.focused.id,'yes')
                        self.assertEqual(app.screen.query_one('#yes',Button).variant,'primary')
                        for button in app.screen.query(Button):
                            self.assertEqual(button.region.height,1)
                            self.assertLessEqual(button.region.bottom,size[1])
                        await pilot.press('escape')
                        start.assert_not_called()
                    await pilot.press('escape')
                    await pilot.press('escape')

    async def test_export_and_confirmed_restore_start_jobs_with_exact_selection(self):
        row,saved = recovery_record()
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app = Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(120,40)) as pilot:
                await app.workers.wait_for_complete()
                for exporting in (True,False):
                    await app.push_screen(SavedServiceData(row))
                    screen = ExportServiceData(row,saved) if exporting else RestoreServiceData(row,saved)
                    await app.push_screen(screen)
                    await pilot.pause()
                    with patch.object(app,'start_settings_job') as start, patch.object(app.service,'run_update',create=True) as run:
                        if exporting:
                            screen.query_one('#service-export-path',Input).value = str(Path(temp)/'db.tar')
                            screen.query_one('#service-export-go',Button).press()
                        else:
                            screen.query_one('#service-restore-go',Button).press()
                            await pilot.pause()
                            app.screen.query_one('#yes',Button).press()
                        await pilot.pause()
                        self.assertTrue(start.call_args.kwargs['cooperative'])
                        task = start.call_args.args[2]
                        task(lambda line:None, None)
                        args = run.call_args.args[0]
                        self.assertEqual(args[:2],['service-data-export' if exporting else 'service-data-restore','example'])
                        self.assertIn(saved['id'],args)
                        self.assertIn('data' if exporting else 'database',args)
                        self.assertNotIsInstance(app.screen,(ExportServiceData,RestoreServiceData,SavedServiceData))

    async def test_removed_last_service_explains_readd_and_still_exports(self):
        row,saved = recovery_record()
        row['config']['profile'].pop('services')
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
            app = Cockpit(Path(temp),FakeService([]))
            async with app.run_test(size=(80,24)) as pilot:
                await app.workers.wait_for_complete()
                await app.push_screen(SavedServiceData(row))
                self.assertFalse(app.screen.query_one('#saved-data-export',Button).disabled)
                self.assertFalse(app.screen.query_one('#saved-data-restore',Button).display)
                self.assertEqual(app.screen.query_one('#saved-data-export',Button).variant,'primary')
                await pilot.pause()
                button = app.screen.query_one('#saved-data-export',Button)
                self.assertGreater(button.parent.content_region.width,0)
                self.assertTrue(button.parent.content_region.contains_region(button.region))
                await app.push_screen(RestoreServiceData(row,saved))
                await pilot.pause()
                self.assertEqual(app.focused.id,'service-restore-back')
                self.assertFalse(app.screen.query('#service-restore-go'))
                text = ' '.join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertIn('Add the service back',text)
                self.assertIn('Choose Back, then Export data…',text)
