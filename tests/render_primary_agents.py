"""Capture the primary-agent creation and settings walkthrough for visual review."""
import argparse
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_sandbox_factory import sandbox
from pi_sandbox_factory.sandbox_tui import (
    Cockpit, DEFAULT_SETTINGS, NewSandbox, PrimarySignIn, SettingsForm, SettingsMenu,
    options, preset,
)
from test_tui import FakeService
from tui_geometry import check_checkbox_alignment, check_dropdown_layout, check_input_layout
from textual.widgets import Button, Collapsible, Input, Select, TextArea


async def capture(root, size):
    with tempfile.TemporaryDirectory(prefix='factory-ui-primary-') as tmp:
        state = Path(tmp)
        project = state / 'projects' / ('transit-service' * 5)
        project.mkdir(parents=True)
        (state / 'transit').mkdir()
        (state / 'transit' / 'allowlist.txt').write_text('')
        config = dict(name='transit', engine='podman', network='allowlist', workspace=str(project),
                      profile=sandbox.load_profile('base'), memory='8g', subagents=False,
                      model_providers=[], options=options(), primary_agent='codex')
        record = dict(name='transit', profile='base', engine='podman', dev='running',
                      proxy='running', workspace=str(project), config=config)
        with (patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(state / 'library')),
              patch('pi_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)),
              patch('pi_sandbox_factory.updates.available_update', return_value=None)):
            app = Cockpit(state, FakeService([record]))
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()

                async def shot(name):
                    await pilot.pause()
                    check_checkbox_alignment(app.screen)
                    check_dropdown_layout(app.screen)
                    check_input_layout(app.screen)
                    for button in app.screen.query(Button):
                        if button.display and button.region.height:
                            assert button.region.height == 1, (name, button)
                    path = root / f'{name}-{size[0]}x{size[1]}.svg'
                    path.write_text(app.export_screenshot())
                    print(path.name, flush=True)

                await shot('dashboard')
                await app.push_screen(NewSandbox())
                await shot('create-pi')
                app.screen.query_one('#primary-agent', Select).value = 'codex'
                await shot('create-codex')
                app.screen.query_one('#name', Input).value = 'transit-test'
                app.screen.query_one('#continue', Button).press()
                await shot('create-error')
                await app.pop_screen()

                for agent in ('pi', 'codex', 'claude', 'agy'):
                    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                    draft.update(name='transit', workspace=str(project), primary_agent=agent, primary_auth='login',
                                 editor={'mode': 'pi'}, model_providers={})
                    await app.push_screen(SettingsForm(draft))
                    await shot('review-' + agent)
                    if agent == 'codex':
                        app.screen.query_one('#primary-auth', Select).value = 'copy'
                        await shot('review-copy')
                        network = app.screen.query_one('#network-section', Collapsible)
                        network.collapsed = False
                        await pilot.pause()
                        app.screen.query_one('#allowlist', TextArea).focus()
                        app.screen.query_one('#allowlist', TextArea).load_text('invalid.host:not-a-port')
                        app.screen.query_one('#apply', Button).press()
                        await shot('review-network-error')
                        app.screen.query_one('#allowlist', TextArea).load_text('example.com:443')
                        await shot('review-network')
                        network.collapsed = True
                        browser = app.screen.query_one('#primary-browser-section', Collapsible)
                        browser.collapsed = False
                        await pilot.pause()
                        browser.scroll_visible(top=True, animate=False)
                        app.screen.query_one('#primary-browser').focus(scroll_visible=False)
                        await shot('review-browser')
                    await app.pop_screen()
                await app.push_screen(SettingsMenu('transit', config, 'pi'))
                await shot('settings')
                await app.pop_screen()
                await app.push_screen(PrimarySignIn(record))
                await shot('sign-in')
                app.screen.query_one('#primary-import', Button).press()
                await shot('sign-in-replace')
                app.screen.action_cancel()
                await pilot.pause()
                app.screen.action_close()
                await pilot.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--size', action='append')
    parser.add_argument('--monochrome', action='store_true')
    args = parser.parse_args()
    if args.monochrome:
        os.environ['NO_COLOR'] = '1'
    else:
        os.environ.pop('NO_COLOR', None)
    os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'
    args.output.mkdir(parents=True, exist_ok=True)
    async def all_sizes():
        for size in args.size or ['80x24', '120x40', '220x50']:
            await capture(args.output, tuple(map(int, size.split('x'))))
    asyncio.run(all_sizes())


if __name__ == '__main__':
    main()
