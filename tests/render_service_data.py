"""Render synthetic saved-data inventory, export and restore states."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, Input, OptionList
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, MoreActions
from ai_sandbox_factory.service_data_ui import ExportServiceData, RestoreServiceData, SavedServiceData
from test_service_data_ui import recovery_record
from test_tui import FakeService
from tui_geometry import check_dropdown_layout, check_input_layout


async def capture(output, size):
    row,saved = recovery_record()
    saved['volumes']['logs'] = {'target':'/var/log/application', 'volume':'pi-sandbox-factory-example-profile-data-fedcba9876543210'}
    row['config']['profile']['services']['database']['volumes']['logs'] = '/var/log/application'
    with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), \
            patch('ai_sandbox_factory.service_data_ui.Path.home',return_value=Path('/home/example')):
        pointer=Path(temp)/'example/profile-runtime/applied.json'
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({'services':{'database':'sha256:0123456789abcdef','reporting':'sha256:fedcba9876543210'}}))
        dashboard={**row,'engine':'podman','profile':'Default Developer Sandbox',
                   'workspace':'/home/example/project','proxy':'running'}
        app = Cockpit(Path(temp),FakeService([dashboard]))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            async def shot(name):
                await pilot.pause()
                await pilot.wait_for_scheduled_animations()
                check_dropdown_layout(app.screen)
                check_input_layout(app.screen)
                for button in app.screen.query(Button):
                    if button.display and button.region.height:
                        assert button.region.height == 1,(name,button.region)
                        if name == 'profile-refresh-error' and button.id in ('profile-services-information','profile-information-data'):
                            continue  # Contextual actions scroll with the fields; page actions stay visible.
                        assert 0 <= button.region.y < button.region.bottom <= size[1],(name,button.region)
                        assert button.parent.content_region.contains_region(button.region),(name,button.parent.content_region,button.region)
                (output/f'service-data-{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            await app.push_screen(MoreActions(row))
            await shot('actions')
            await pilot.press('escape')
            guide=Path(temp)/'profile'
            guide.mkdir()
            (guide/'start-here.md').write_text('Profile guide')
            full=copy.deepcopy(row)
            full['config']['profile']['path']=str(guide)
            with patch('ai_sandbox_factory.sandbox_tui.sandbox_alerts',return_value=['fixture']):
                await app.push_screen(MoreActions(full,browser_running=True))
            await shot('actions-all')
            choices=app.screen.query_one(OptionList)
            if size[1] >= 40:
                assert choices.virtual_size.height<=choices.content_size.height,(choices.virtual_size,choices.content_size)
            await pilot.press('end')
            assert str(choices.get_option_at_index(choices.highlighted).prompt)=='Delete sandbox…'
            await shot('actions-all-delete')
            await pilot.press('escape')
            with patch('ai_sandbox_factory.profile_runtime.applied',return_value={'path':'/synthetic/applied'}), \
                    patch('ai_sandbox_factory.profile_services.status',return_value=[{'name':'database','running':True}]):
                await app.push_screen(AppliedProfile(row,Path(temp)))
                await app.workers.wait_for_complete()
                app.screen.query_one('#profile-information-data').scroll_visible(animate=False)
                await shot('profile-entry')
                page=app.screen
                app.service.config=lambda name:copy.deepcopy(row['config'])
                page.query_one('#profile-information-data',Button).focus()
                await pilot.press('enter')
                updated=copy.deepcopy(row['config'])
                updated['profile_service_recovery'].append({**saved,'id':'latest-copy'})
                app.service.config=lambda name:copy.deepcopy(updated)
                await pilot.press('escape')
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.screen is page and app.focused.id=='profile-information-data'
                await shot('profile-return')
                page.query_one('#profile-information-data',Button).focus()
                await pilot.press('enter')
                with patch.object(app.service,'config',side_effect=ValueError('config.json: expected a comma at line 4')):
                    await pilot.press('escape')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                await shot('profile-data-refresh-error')
                page.query_one('#profile-information-edit',Button).press()
                await pilot.pause()
                with patch.object(app.service,'config',side_effect=ValueError('config.json: expected a comma at line 4')):
                    await pilot.press('escape')
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                page.query_one('.form').scroll_home(animate=False)
                await shot('profile-refresh-error')
                await app.pop_screen()
            await app.push_screen(SavedServiceData(row))
            await shot('inventory')
            await app.pop_screen()
            await app.push_screen(ExportServiceData(row,saved))
            await shot('export')
            app.screen.query_one('#service-export-path',Input).value = ''
            app.screen.query_one('#service-export-go',Button).press()
            await shot('export-error')
            await app.pop_screen()
            await app.push_screen(RestoreServiceData(row,saved))
            await shot('restore')
            app.screen.query_one('#service-restore-go',Button).press()
            await shot('restore-confirm')
            await pilot.press('escape')
            await app.pop_screen()
            other=copy.deepcopy(row)
            other['config']['profile']['services']['reporting'] = other['config']['profile']['services'].pop('database')
            await app.push_screen(RestoreServiceData(other,saved))
            await shot('other-service')
            await app.pop_screen()
            removed=copy.deepcopy(row)
            removed['config']['profile'].pop('services')
            await app.push_screen(SavedServiceData(removed))
            await shot('removed-inventory')
            await app.pop_screen()
            await app.push_screen(RestoreServiceData(removed,saved))
            await shot('removed-service')
            await app.pop_screen()
            row['config'].pop('profile_service_recovery')
            await app.push_screen(SavedServiceData(row))
            await shot('empty')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--mono',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.mono: os.environ['NO_COLOR']='1'
    else: os.environ.pop('NO_COLOR',None)
    os.environ['TEXTUAL_COLOR_SYSTEM']='truecolor'
    for size in ((80,24),(120,40),(220,50)):
        asyncio.run(capture(args.output,size))


if __name__=='__main__': main()
