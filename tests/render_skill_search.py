"""Capture skill-import search states: python tests/render_skill_search.py OUTPUT [--mono]."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS
from ai_sandbox_factory.skills_ui import ImportSkills
from ai_sandbox_factory.tui_widgets import Checkbox
from textual.widgets import Input
from test_tui import FakeService

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
if '--mono' in sys.argv:
    os.environ['NO_COLOR'] = '1'
else:
    os.environ.pop('NO_COLOR', None)
os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'

async def render(size):
    with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)):
        app = Cockpit(Path(temp), FakeService([]))
        async with app.run_test(size=size) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            screen = ImportSkills('https://example.com/security/skills.git', 'main')
            await app.push_screen(screen); await pilot.pause()
            async def shot(name, focus=None):
                if focus:
                    widget = screen.query_one(focus); widget.focus()
                    await pilot.pause()
                    widget.scroll_visible(animate=False)
                await pilot.pause()
                (out / f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            await shot('empty', '#skill-source')
            rows = [dict(id=str(i), name=f'audit-{i}', description='Review Python code and document findings.' if i == 42 else 'Review code for security issues and verify findings.', folder=f'plugins/security-audit-{i}/skills/audit') for i in range(99)]
            rows.append(dict(id='invalid', name='invalid-skill', description='', folder='plugins/invalid', error='SKILL.md needs YAML frontmatter at the start'))
            await screen.show_candidates(rows); await pilot.pause()
            screen.query_one('#candidate-0', Checkbox).value = True
            await shot('populated', '#skill-search')
            search = screen.query_one('#skill-search', Input)
            search.value = 'python'; await pilot.pause(0.1)
            await shot('filtered')
            search.value = 'no-match'; await pilot.pause(0.1)
            await shot('no-matches')
            search.value = 'invalid'; await pilot.pause(0.1)
            await shot('invalid')
            search.value = ''; await pilot.pause()
            screen.query_one('#candidate-98').focus()
            screen.query_one('VerticalScroll').scroll_end(animate=False)
            await pilot.pause()
            (out / f'bottom-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            await screen.show_failure('Could not fetch the skills. Check the repository URL, revision and your Git access.')
            await shot('error', '#skill-source')
            await app.pop_screen(); await pilot.pause()
    print(size, flush=True)

for size in ((80, 24), (120, 40), (220, 50)):
    asyncio.run(render(size))
