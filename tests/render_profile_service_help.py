"""Render compact service summaries and their opt-in reference popup."""
import argparse
import asyncio
import copy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, Static
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import AppliedProfile, Cockpit, DEFAULT_SETTINGS, SettingsForm, preset
from test_tui import FakeService


async def capture(output,size):
    with tempfile.TemporaryDirectory() as temporary, \
            patch.dict(os.environ,SANDBOX_REGISTRY_DIR=str(Path(temporary)/'registry')), \
            patch('ai_sandbox_factory.updates.available_update',return_value=None), \
            patch('ai_sandbox_factory.profile_services.status',return_value=[{'name':'database','running':True}]), \
            patch('ai_sandbox_factory.sandbox_tui.load_defaults',return_value=copy.deepcopy(DEFAULT_SETTINGS)):
        root=Path(temporary)
        (root/'profile-runtime').mkdir()
        (root/'profile-runtime/applied.json').write_text('{"name":"developer-postgres"}')
        app=Cockpit(root,FakeService([]))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            async def shot(name):
                await pilot.pause()
                await pilot.wait_for_scheduled_animations()
                for button in app.screen.query(Button):
                    if button.display and button.region.height:
                        assert button.region.height==1,(name,button.id,button.region)
                close=app.screen.query('#close-profile-services-help')
                if close:
                    assert app.screen.query_one('#confirm-box').content_region.contains_region(close.first().region)
                    app.screen.query_one('#profile-details-body').scroll_home(animate=False)
                (output/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            for agent in ('pi','codex'):
                draft=preset('developer-postgres',copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='example',primary_agent=agent,workspace='/home/example/projects/application')
                page=SettingsForm(draft)
                await app.push_screen(page)
                page.query_one('.form').scroll_home(animate=False)
                await shot('services-review-'+agent)
                button=page.query_one('#profile-services-information',Button)
                button.focus()
                button.scroll_visible(animate=False)
                await pilot.press('enter')
                await shot('services-help-'+agent)
                app.screen.query_one('#profile-details-body').scroll_end(animate=False)
                await pilot.pause()
                (output/f'services-help-end-{agent}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                await pilot.press('escape')
                await shot('services-help-dismissed-'+agent)
                await app.pop_screen()
            record={'name':'example','config':{'profile':sandbox.load_profile('developer-postgres')}}
            await app.push_screen(AppliedProfile(record,root))
            await app.workers.wait_for_complete()
            await shot('services-information')
            button=app.screen.query_one('#profile-services-information',Button)
            button.focus()
            button.scroll_visible(animate=False)
            await pilot.press('enter')
            await shot('services-information-help')
            await pilot.press('escape')
            await app.pop_screen()
            plain=preset('base',copy.deepcopy(DEFAULT_SETTINGS))
            plain.update(name='example',workspace='/home/example/project')
            await app.push_screen(SettingsForm(plain))
            app.screen.query_one('.form').scroll_home(animate=False)
            assert not app.screen.query('#profile-services-information')
            await shot('services-none')


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


if __name__=='__main__':main()
