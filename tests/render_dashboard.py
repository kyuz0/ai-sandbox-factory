"""Render dashboard focus, hover, sorting and error states for visual review."""
import argparse
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from ai_sandbox_factory.sandbox_jobs import Job
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, SandboxTable
from textual.widgets import Input, Select
from test_tui import FakeService


async def render(out, size):
    records = []
    for index, name in enumerate(('project-alpha', 'a-longer-project-name', 'legacy-project')):
        date = f'2026-11-{12 + index}T12:30:00+00:00' if index < 2 else None
        records.append(dict(name=name, profile='base', engine='podman',
                            dev='running' if index == 0 else 'exited', proxy='running',
                            workspace='/home/example/projects/' + name,
                            config={'network': 'allowlist', 'local_model_ports': '8000'},
                            activity=dict.fromkeys(('created', 'edited', 'started'), date)))
    records[-1]['ssh_key_state'] = 'locked'
    with tempfile.TemporaryDirectory() as temp, \
            patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), \
            patch('ai_sandbox_factory.updates.available_update', return_value=None), \
            patch('ai_sandbox_factory.updates.installed_version', return_value='0.0.0+source'):
        app = Cockpit(Path(temp), FakeService(records))
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()

            async def shot(name):
                await pilot.pause()
                (out / f'dashboard-{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())

            await shot('dates')
            table = app.query_one(SandboxTable)
            table.focus()
            await shot('name-focus')
            await pilot.press('right')
            await shot('open-focus')
            table.move_cursor(row=table.get_row_index('legacy-project'), column=table.get_cell_coordinate(
                'legacy-project', 'alerts').column)
            await shot('alerts-focus')
            table.move_cursor(row=0, column=0)
            region = table._get_cell_region(table.get_cell_coordinate('project-alpha', 'name'))
            await pilot.hover(table, offset=(region.x + 3, region.y))
            await shot('name-hover')
            await pilot.hover('#new')

            with_job = Job('project-alpha', 'Update · project-alpha', 'Updating sandbox')
            app.jobs.append(with_job)
            app.poll_jobs()
            job_cell = table.get_cell_coordinate('project-alpha', 'open')
            table.move_cursor(row=job_cell.row, column=job_cell.column)
            await shot('job-focus')
            app.jobs.remove(with_job)
            with_job.close()
            app.poll_jobs()

            app.query_one('#sort-field', Select).focus()
            await pilot.press('enter')
            await shot('sort')
            await pilot.press('escape')
            app.query_one('#search', Input).value = 'missing'
            await shot('empty')
            app.query_one('#search', Input).value = ''
            app.query_one('#home-error').update('Container engine unavailable; showing saved sandboxes.')
            await shot('error')


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
    for dimensions in ((80, 24), (120, 40), (220, 50)):
        asyncio.run(render(args.output, dimensions))
