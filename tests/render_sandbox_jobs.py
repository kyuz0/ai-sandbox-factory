"""Render the dashboard job workflow with synthetic sandboxes and output."""
import argparse
import asyncio
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import OptionList

from ai_sandbox_factory.sandbox_jobs import RebuildJob
from ai_sandbox_factory.sandbox_tui import Cockpit, MoreActions, SandboxTable
from test_sandbox_jobs import JobsService, wait_for


async def render(destination, size):
    service = JobsService()
    jobs = [RebuildJob(name) for name in (
        'api-development', 'a-longer-workspace-name-for-testing-the-jobs-list', 'site-preview')]
    first, second, third = jobs
    try:
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(Path(tmp), service)
            async with app.run_test(size=size) as pilot:
                await wait_for(pilot, lambda: len(app.records) == 2)

                async def shot(name):
                    await pilot.pause()
                    path = destination / f'{name}-{size[0]}x{size[1]}.svg'
                    path.write_text(app.export_screenshot(), encoding='utf-8')

                await shot('dashboard-idle')
                app.query_one(SandboxTable).focus()
                await pilot.press('a')
                await shot('sandbox-actions')
                for page in ('diagnostics', 'chats'):
                    menu = app.screen
                    menu.query_one(OptionList).highlighted = next(i for i, (_, key) in enumerate(menu.actions) if key == page)
                    await pilot.press('enter')
                    await shot('sandbox-actions-' + page)
                    await pilot.press('escape')
                await pilot.press('escape')
                profile = Path(tmp) / 'profile'
                profile.mkdir()
                (profile / 'start-here.md').touch()
                fixture = dict(service.records[0], name='a-very-long-project-name-' * 7 + 'demo', ssh_key_state='locked',
                               config={**service.records[0]['config'], 'profile': {'path': str(profile)}})
                app.push_screen(MoreActions(fixture, browser_running=True))
                await shot('sandbox-actions-all')
                close = app.screen.query_one('#more-cancel')
                assert close.region in app.screen.query_one('#confirm-box').content_region
                assert not app.screen.query_one(OptionList).show_vertical_scrollbar
                await pilot.press('escape', 'j')
                await shot('jobs-empty')
                await pilot.press('escape')

                app.rebuild_jobs.extend(jobs)
                first.write('Waiting for another build to finish using the same image layers…')
                second.write('STEP 4/6: COPY image configuration')
                third.run(lambda log, update: log('Created the new sandbox container.'))
                app.poll_rebuilds()
                await shot('dashboard-jobs')
                await pilot.press('j')
                await shot('jobs-active')
                await pilot.press('down', 'down', 'enter')
                await shot('job-preparing')
                await pilot.click('#job-cancel')
                await shot('job-confirm-cancel')
                await pilot.press('escape', 'escape')

                with patch.object(type(first.update), 'applying', True), \
                     patch.object(type(first.update), 'can_cancel', False):
                    app.show_job(first)
                    await shot('job-applying')
                    await pilot.press('escape')
                first.result = 'Cancelled'
                first.write('Update cancelled. The previous sandbox was preserved.')
                second.write('Error: Podman build exited with status 125: no space left on device.')
                second.result = 'Failed'
                app.poll_rebuilds()
                await shot('dashboard-complete')
                await pilot.press('j')
                await shot('jobs-complete')
                await pilot.press('enter')
                await shot('job-success')
                await pilot.press('escape')
                app.show_job(second)
                await shot('job-error')
                await pilot.press('escape')
                app.show_job(first)
                await shot('job-cancelled')
    finally:
        for job in jobs:
            job.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--mono', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mono:
        os.environ['NO_COLOR'] = '1'
    else:
        os.environ.pop('NO_COLOR', None)
    os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'
    for dimensions in [(80, 24), (120, 40), (220, 50)]:
        asyncio.run(render(args.output, dimensions))
