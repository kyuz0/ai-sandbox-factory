"""Saved service data is discoverable and recovery choices name their effects."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static

from ai_sandbox_factory.sandbox_tui import Cockpit, MoreActions
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

    async def test_menu_only_shows_saved_data_when_it_exists(self):
        plain = record()
        with_data, _ = recovery_record()
        self.assertNotIn(('Saved service data…','saved-service-data'),MoreActions(plain).actions)
        self.assertIn(('Saved service data…','saved-service-data'),MoreActions(with_data).actions)
        with_data['config']['profile'].pop('services')
        self.assertIn(('Saved service data…','saved-service-data'),MoreActions(with_data).actions)

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
