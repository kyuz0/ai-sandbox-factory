"""Offline skill library visual fixtures: python tests/render_skills.py OUTPUT [--mono]."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from pi_sandbox_factory import skill_library, sandbox
from pi_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, PackagesScreen, SettingsForm, preset
from pi_sandbox_factory.skills_ui import ImportSkills
from pi_sandbox_factory.tui_widgets import Checkbox
from textual.widgets import Button, Collapsible, Input
from test_tui import FakeService

out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
if '--mono' in sys.argv:os.environ['NO_COLOR']='1'
else:os.environ.pop('NO_COLOR',None)
os.environ['TEXTUAL_COLOR_SYSTEM']='truecolor'

async def render(size):
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'SANDBOX_REGISTRY_DIR':temp+'/library'}), patch('pi_sandbox_factory.updates.available_update',return_value=None), patch('pi_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
        root=Path(temp);source=root/'source';source.mkdir()
        for i in range(10):
            folder=source/f'document-task-{i}';folder.mkdir()
            (folder/'SKILL.md').write_text(f'---\nname: document-task-{i}\ndescription: Read and prepare documents using the project instructions, with clear checks before sharing the completed result.\n---\nUse the project files.\n')
        rows=skill_library.discover(str(source),root/'fetched')
        app=Cockpit(root/'state',FakeService([]))
        async with app.run_test(size=size) as pilot:
            await pilot.pause();await app.workers.wait_for_complete()
            async def shot(name,screen=None,focus=None):
                if screen:await app.push_screen(screen)
                await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                if focus:
                    widget=app.screen.query_one(focus);widget.focus();widget.scroll_visible(animate=False);await pilot.pause()
                (out/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                print(name,size,flush=True)
                if screen:await app.pop_screen()
            await shot('home-library')
            await shot('package-library',PackagesScreen())
            await shot('library-empty',PackagesScreen(initial='skills'))
            await shot('import-empty',ImportSkills(), '#skill-source')
            screen=ImportSkills(str(source), 'main');await app.push_screen(screen);await pilot.pause()
            await screen.show_failure('No SKILL.md files were found in this source')
            await shot('import-error',focus='#skill-source');await app.pop_screen()
            screen=ImportSkills(str(source), 'main');await app.push_screen(screen);await pilot.pause()
            await screen.show_candidates(rows+[{'name':'invalid/[draft]','folder':'invalid/[draft]','id':'invalid','error':'SKILL.md needs YAML frontmatter at the start'}])
            screen.query_one('#candidate-0',Checkbox).value=True
            screen.query_one('#candidate-1',Checkbox).value=True
            await shot('import-candidates',focus='#candidate-0')
            await shot('import-invalid-candidate',focus='#candidate-9')
            screen.query_one('#skill-candidates').scroll_end(animate=False);await pilot.pause()
            await shot('import-candidates-bottom');await app.pop_screen()
            await shot('import-long-source',ImportSkills('https://example.com/organisation/very-long-repository-name-for-document-processing-and-validation.git','release-document-processing'), '#skill-source')
            screen=ImportSkills(str(source),'main');await app.push_screen(screen);await pilot.pause()
            screen.set_busy(True);screen.query_one('#skill-import-status').update('Fetching source and validating skills…')
            await shot('import-busy');await app.pop_screen()
            empty=preset('base',copy.deepcopy(DEFAULT_SETTINGS));empty.update(name='demo',workspace=str(root))
            screen=SettingsForm(empty);await app.push_screen(screen);await pilot.pause()
            screen.query_one('#skill-library-section',Collapsible).collapsed=False
            await shot('selection-empty',focus='#manage-skills');await app.pop_screen()
            skill_library.install(rows)
            await shot('library-populated',PackagesScreen(initial='skills'), '#update-'+rows[0]['id'])
            await shot('library-back-focus',PackagesScreen(initial='skills'), '#packages-back')
            await app.push_screen(PackagesScreen(initial='skills'));await pilot.pause()
            await shot('library-bottom',focus='#remove-'+rows[-1]['id']);await app.pop_screen()
            draft=preset('base',copy.deepcopy(DEFAULT_SETTINGS));draft.update(name='demo',workspace=str(root),library_skills=[rows[0]['id'],rows[1]['id']])
            for context in ('create','defaults','existing','missing'):
                sample=copy.deepcopy(draft)
                if context=='missing':sample['library_skills']=['missing']
                if context=='existing':
                    sample['extra_skills']=skill_library.snapshot([rows[0]['id']],root/'sandbox')
                    sample['library_skills']=[]
                screen=SettingsForm(sample,editing=context=='existing',defaults=context=='defaults')
                await app.push_screen(screen);await pilot.pause()
                await shot('selection-'+context+'-collapsed',focus='#skill-library-section CollapsibleTitle')
                screen.query_one('#skill-library-section',Collapsible).collapsed=False
                await shot('selection-'+context+'-expanded',focus='#library-skill-'+('missing' if context=='missing' else rows[1]['id']))
                await shot('selection-'+context+'-bottom',focus='#manage-skills')
                await shot('selection-'+context+'-apply',focus='#apply')
                await app.pop_screen()
            saved=skill_library.snapshot([rows[0]['id']],root/'sandbox')
            saved[0]['digest']='old-snapshot'
            draft['extra_skills']=saved
            draft['library_skills']=[rows[0]['id']]
            screen=SettingsForm(draft,editing=True)
            await app.push_screen(screen);await pilot.pause()
            screen.query_one('#skill-library-section',Collapsible).collapsed=False
            try:screen.collect()
            except ValueError:pass
            await shot('selection-conflict',focus='#saved-skill-0');await app.pop_screen()
for size in ((80,24),(120,40),(220,50)):asyncio.run(render(size))
