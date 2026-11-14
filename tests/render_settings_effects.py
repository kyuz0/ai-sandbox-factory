"""Offline settings-effect screens: python tests/render_settings_effects.py OUTPUT [--mono]."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Input, OptionList
from pi_sandbox_factory import sandbox, settings_effects
from pi_sandbox_factory.sandbox_tui import (
    Cockpit, DEFAULT_SETTINGS, ApplySettingsReview, Operation, NetworkForm,
    SSHScreen, AdvisorsScreen, ModelsScreen, SettingsForm, preset,
)
from pi_sandbox_factory.editor_ui import EditorScreen
from pi_sandbox_factory.models_ui import ModelDialog
from test_hosted_models_ui import ModelService

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
if '--mono' in sys.argv: os.environ['NO_COLOR'] = '1'
else: os.environ.pop('NO_COLOR', None)
os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'

async def render(size):
    with tempfile.TemporaryDirectory() as temp, patch('pi_sandbox_factory.updates.available_update', return_value=None), patch('pi_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': temp + '/library'}):
        root = Path(temp); (root/'demo').mkdir()
        draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS)); draft.update(name='demo', workspace=str(root), engine='podman')
        config = copy.deepcopy(draft); config['profile'] = sandbox.load_profile('base')
        config['model_providers'] = []
        record = dict(name='demo', config=config, dev='running', proxy='running', profile='base', engine='podman', workspace=str(root))
        app = Cockpit(root, ModelService([]))
        async with app.run_test(size=size) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            async def shot(name, screen=None, focus=None):
                if screen: await app.push_screen(screen)
                await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
                if focus:
                    widget = app.screen.query_one(focus); widget.focus(); widget.scroll_visible(animate=False); await pilot.pause()
                (out/f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                print(name, size, flush=True)
                if screen: await app.pop_screen()
            changed = copy.deepcopy(draft)
            changed.update(library_skills=['fixture'], local_model_ports='8000,8080', memory='16g')
            changed['options'].update(browser=True, goals=False, git_identity={'name':'Example User','email':'example@example.com'})
            await app.push_screen(ApplySettingsReview({}, changed)); await pilot.pause()
            await shot('review-top', focus='#cancel')
            app.screen.query_one('#apply-review-fields', VerticalScroll).scroll_end(animate=False)
            await pilot.pause(); await shot('review-bottom'); await app.pop_screen()
            await shot('review-no-changes', ApplySettingsReview(draft, draft))
            memory=copy.deepcopy(draft);memory['memory']='16g'
            await shot('review-memory-only',ApplySettingsReview(draft,memory,'pi'))
            for name, message in [('configuration',settings_effects.REOPEN),('network',settings_effects.NETWORK),('folders',settings_effects.FOLDER),('models',settings_effects.MODELS),('ssh',settings_effects.bullets(settings_effects.reconnect()+['Unlock the SSH key before connecting.']))]:
                await shot('complete-'+name, Operation('Updated demo', lambda log: log('Settings saved.'), success_message=message))
            def fail(log): raise ValueError('Example validation failed; saved settings were not applied.')
            await shot('failed', Operation('Updating demo', fail, success_message=settings_effects.REOPEN))
            await shot('configuration-intro', SettingsForm(draft, editing=True))
            await shot('network-intro', NetworkForm('demo','example.com:443',config))
            await shot('ssh-intro', SSHScreen(record))
            await shot('advisors-intro', AdvisorsScreen(record))
            await shot('models-intro', ModelsScreen(record))
            await app.push_screen(ModelDialog('demo','openrouter')); await pilot.pause(); await app.workers.wait_for_complete()
            app.screen.query_one('#model-results',OptionList).highlighted=0
            await shot('model-default', focus='#model-save');
            app.screen.query_one('.model-fields',VerticalScroll).scroll_end(animate=False);await pilot.pause();await shot('model-default-bottom'); await app.pop_screen()
            await shot('editor-intro', EditorScreen('demo'))
            await app.push_screen(EditorScreen('demo')); await pilot.pause()
            app.screen.query_one('#editor-connection',Collapsible).collapsed=False
            app.screen.query_one('#editor-address',Input).value='192.0.2.10'
            await shot('editor-connection', focus='#editor-private-key'); await app.pop_screen()
for size in ((80,24),(120,40),(220,50)): asyncio.run(render(size))
