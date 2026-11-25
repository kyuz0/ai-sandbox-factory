"""Render synthetic profile editing, validation and reviewed application states."""
import argparse
import asyncio
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, Input
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.profile_edit_ui import EditProfile, ReviewProfile
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, MoreActions
from test_profile_edit_ui import record, review
from test_tui import FakeService
from tui_geometry import check_input_layout


async def capture(output,size):
    with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update',return_value=None):
        dashboard=record()
        dashboard.update(profile='Default Developer Sandbox',workspace='/home/example/project',engine='podman',proxy='running')
        app=Cockpit(Path(tmp),FakeService([dashboard]))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            async def shot(name):
                await pilot.pause()
                check_input_layout(app.screen)
                for button in app.screen.query(Button):
                    if button.display and button.region.height:
                        assert button.region.height == 1,(name,button.id,button.region)
                (output/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            for editable in (False,True):
                row=record(editable)
                await app.push_screen(MoreActions(row))
                await shot('profile-edit-actions-'+('attached' if editable else 'initial'))
                await app.pop_screen()
            page=EditProfile(record(),destination='/home/example/sandbox-profiles/example')
            await app.push_screen(page)
            await shot('profile-edit-folder')
            page.query_one('#profile-edit-folder',Input).value=''
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-folder-error')
            page.query_one('#profile-edit-folder',Input).value='/home/example/sandbox-profiles/example'
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-confirm-running')
            await pilot.press('escape')
            await app.pop_screen()
            page=EditProfile(record(running=False),destination='/home/example/sandbox-profiles/example')
            await app.push_screen(page)
            page.query_one('#profile-edit-go',Button).press()
            await shot('profile-edit-confirm-stopped')
            await pilot.press('escape')
            await app.pop_screen()
            for error in ('',"Cannot review the profile for example (exit 1): profile.json: line 4, column 8: expected a comma. Fix the JSON, then choose Review changes again."):
                await app.push_screen(EditProfile(record(True),error=error))
                await shot('profile-edit-attached-error' if error else 'profile-edit-attached')
                await app.pop_screen()
            for changed in (False,True):
                await app.push_screen(ReviewProfile(record(True),review(changed)))
                await shot('profile-edit-review' if changed else 'profile-edit-no-changes')
                if changed:
                    app.screen.query_one('#profile-edit-diff').scroll_visible(top=True,animate=False)
                    await shot('profile-edit-review-diff')
                await app.pop_screen()
            information=record(True)
            information['config']['profile']=sandbox.load_profile('developer-postgres')
            with patch('ai_sandbox_factory.profile_runtime.applied',return_value={'path':'/synthetic/applied'}), \
                    patch('ai_sandbox_factory.profile_services.status',return_value=[{'name':'database','running':True}]):
                await app.push_screen(AppliedProfile(information,Path(tmp)))
                await app.workers.wait_for_complete()
                await shot('profile-edit-information')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ.pop('NO_COLOR',None)
    os.environ['TEXTUAL_COLOR_SYSTEM']='truecolor'
    for size in ((80,24),(120,40),(220,50)):
        asyncio.run(capture(args.output,size))


if __name__=='__main__':
    main()
