"""Render synthetic profile setup, services and destructive confirmations."""
import argparse
import asyncio
import copy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from textual.widgets import Button, Input, Select, Static
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import (AppliedProfile, Cockpit, ConfirmCreationConflicts, DEFAULT_SETTINGS,
                                          NewSandbox, SettingsForm, CreateFromSandbox, copy_draft, preset)
from ai_sandbox_factory.chat_history_ui import DeleteSandbox
from test_tui import FakeService
from tui_geometry import check_checkbox_alignment, check_dropdown_layout, check_input_layout


async def capture(output, size):
    with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.updates.available_update', return_value=None), \
            patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), \
            patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(Path(tmp) / 'registry')):
        app = Cockpit(Path(tmp), FakeService([]))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            async def shot(name):
                await pilot.pause()
                check_checkbox_alignment(app.screen)
                check_dropdown_layout(app.screen)
                check_input_layout(app.screen)
                for button in app.screen.query(Button):
                    if button.display and button.region.height:
                        assert button.region.height == 1, (name, button.id, button.region)
                (output / f'{name}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            new = NewSandbox()
            await app.push_screen(new)
            new.query_one('#profile', Select).value = 'developer-postgres'
            await shot('profile-new-pi')
            new.query_one('#profile', Select).expanded = True
            await shot('profile-picker-expanded')
            await pilot.press('escape')
            new.query_one('#primary-agent', Select).value = 'codex'
            await shot('profile-new-native')
            new.query_one('#profile', Select).value = 'custom'
            new.query_one('#custom-profile', Input).value = '/home/example/profiles/database'
            new.query_one('#continue', Button).press()
            await pilot.pause()
            await shot('profile-new-error')
            await app.pop_screen()
            for agent in ('pi', 'codex'):
                draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name='example', primary_agent=agent, workspace='/home/example/projects/application-with-a-long-name')
                form = SettingsForm(draft)
                await app.push_screen(form)
                await shot('profile-review-' + agent)
                form.query_one('.form').scroll_home(animate=False)
                await shot('profile-review-summary-' + agent)
                form.query_one('#review-profile-services').scroll_visible(top=True, animate=False)
                await shot('profile-review-services-' + agent)
                if agent == 'pi':
                    title = form.query_one('#skills-section CollapsibleTitle')
                    title.focus()
                    title.scroll_visible(top=True, animate=False)
                    await shot('profile-features-collapsed')
                    await pilot.press('enter')
                    await shot('profile-features-expanded')
                await app.pop_screen()
            record = {'name': 'example', 'config': {'profile': sandbox.load_profile('developer-postgres')}}
            (Path(tmp) / 'profile-runtime').mkdir(exist_ok=True)
            (Path(tmp) / 'profile-runtime/applied.json').write_text('{"name":"developer-postgres"}')
            for failing in (False, True):
                with patch('ai_sandbox_factory.profile_services.status',
                           side_effect=ValueError('podman connection refused (exit 125)') if failing else None,
                           return_value=[{'name':'database','running':True}]):
                    await app.push_screen(AppliedProfile(record, Path(tmp)))
                    await app.workers.wait_for_complete()
                    await shot('profile-information-error' if failing else 'profile-information')
                    await app.pop_screen()
            for agent in ('pi', 'codex'):
                config = {'profile':sandbox.load_profile('developer-postgres'), 'primary_agent':agent,
                          'workspace':'/home/example/projects/application', 'engine':'podman'}
                record = {'name':'example', 'dev':'running', 'config':config}
                await app.push_screen(CreateFromSandbox([record]))
                await shot('profile-copy-' + agent)
                await app.pop_screen()
                source = Path(tmp) / 'example'
                source.mkdir(exist_ok=True)
                (source / 'allowlist.txt').write_text('')
                await app.push_screen(NewSandbox(copy_draft(record, Path(tmp))))
                await shot('profile-copy-new-' + agent)
                await app.pop_screen()
                app.perform('rebuild', record=record)
                await shot('profile-rebuild-' + agent)
                await app.pop_screen()
            await app.push_screen(ConfirmCreationConflicts('example', 'networks', [('example-profile', 'synthetic-id')],
                'The listed private networks will be deleted. Other networks stay in place.'))
            await shot('profile-network-conflict')
            await app.pop_screen()
            await app.push_screen(DeleteSandbox('example', has_config=True, services=['database']))
            await shot('profile-delete-data')
            await app.pop_screen()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ.pop('NO_COLOR', None)
    os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'
    async def run():
        for size in ((80,24),(120,40),(220,50)):
            await capture(args.output, size)
    asyncio.run(run())

if __name__ == '__main__':
    main()
