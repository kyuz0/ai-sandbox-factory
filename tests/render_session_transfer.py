"""Render and exercise cross-agent chat-copy controls with synthetic history."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, Select, Static

from ai_sandbox_factory.sandbox_tui import Cockpit
from ai_sandbox_factory.session_transfer_ui import ContinueChat
from test_tui import FakeService


class Fixture:
    def __init__(self, config, state):
        self.config = config
        self.agent = 'claude'
        self.rows = [{'title': 'Fix a workspace request handler', 'path': 'sessions/chat.jsonl'}]
        self.plan = None

    def preview(self, path, target, state):
        assert path == 'sessions/chat.jsonl'
        assert target['primary_agent'] == 'pi'
        self.plan = True
        return {'source': {'events': {'message': 8, 'tool_call': 2, 'tool_result': 2}},
                'dropped_events': {'thinking:unsupported': 2},
                'warnings': [{'message': 'The target agent will use its own model and tools.'}]}

    def apply(self):
        assert self.plan
        self.plan = None
        return {}

    def close(self):
        pass


async def check(output, size, monochrome):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        configs = [dict(name=name, engine='podman', primary_agent=agent, workspace=str(project))
                   for name, agent in [('source', 'claude'), ('destination', 'pi')]]
        records = [dict(name=c['name'], dev='exited', config=c, workspace=str(project), profile='base',
                        engine='podman', proxy='exited') for c in configs]
        service = FakeService(records)
        service.state = root
        service.chat_config = lambda name: next(c for c in configs if c['name'] == name)
        with patch('ai_sandbox_factory.session_transfer.Transfer', Fixture), \
             patch('ai_sandbox_factory.updates.available_update', return_value=None):
            app = Cockpit(root, service)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                await app.push_screen(ContinueChat('source'))
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.screen.query_one('#copy-next', Button).disabled
                app.screen.query_one('#copy-chat', Select).focus()

                async def shot(name):
                    await pilot.pause()
                    for button in app.screen.query(Button):
                        if button.display:
                            assert app.screen.region.contains_region(button.region), (name, size, button.id)
                    if output:
                        suffix = '-mono' if monochrome else ''
                        path = output / f'copy-chat-{name}-{size[0]}x{size[1]}{suffix}.svg'
                        path.write_text(app.export_screenshot())

                await shot('choose')
                app.screen.query_one('#copy-chat', Select).value = 'sessions/chat.jsonl'
                app.screen.query_one('#copy-target', Select).value = 'destination'
                await pilot.pause()
                assert not app.screen.query_one('#copy-next', Button).disabled
                await shot('selected')
                app.screen.query_one('#copy-next', Button).press()
                await app.workers.wait_for_complete()
                await shot('preview')
                assert 'Claude Code → Pi' in str(app.screen.query_one('#copy-preview', Static).render())
                app.screen.failed('The destination sandbox is running. Stop it before copying the chat.')
                await shot('error')
                app.screen.query_one('#copy-error').update('')
                app.screen.query_one('#copy-next', Button).press()
                await app.workers.wait_for_complete()
                await shot('complete')
                assert 'Created a new chat in destination' in str(app.screen.query_one('#copy-preview', Static).render())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--monochrome', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.monochrome:
        os.environ['NO_COLOR'] = '1'
    else:
        os.environ.pop('NO_COLOR', None)
    os.environ['TEXTUAL_COLOR_SYSTEM'] = 'truecolor'
    for size in ([(80, 24)] if args.monochrome else [(80, 24), (120, 40), (220, 50)]):
        asyncio.run(check(args.output, size, args.monochrome))
