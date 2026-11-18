"""Check approval announcements and reviewer settings in the installed Pi client."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


mode = os.environ['SANDBOX_APPROVAL_MODE']
label = {
    'automatic': 'Automatic (non-interactive)',
    'assisted': 'Automatic (interactive)',
    'manual': 'Manual (ask me)',
    'off': 'No approval prompts (YOLO)',
}[mode]
automatic = mode in ('automatic', 'assisted')
reload_fixture = Path('/tmp/reload-reviewer-fixture.mjs')
reload_fixture.write_text('export default pi => pi.registerCommand("fixture-reload", '
                          '{handler: async (_args, ctx) => ctx.reload()});\n')
with subprocess.Popen(
    ['pi', '--mode', 'rpc', '--no-session', '--provider', 'fixture', '--model', 'fixture-model',
     '--extension', str(reload_fixture)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
) as process:
    lines = queue.Queue()
    def read():
        for line in process.stdout:
            lines.put(json.loads(line))
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    notices = []
    prompts = []

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def request(kind, choices=(), **payload):
        send({'type': kind, 'id': 'check', **payload})
        selections = iter(choices)
        while True:
            event = lines.get(timeout=45)
            assert event is not None, 'Pi exited before responding'
            if event.get('type') == 'extension_ui_request':
                if event['method'] == 'notify':
                    notices.append(event['message'])
                if event['method'] == 'select':
                    prompts.append(event)
                    choice = next(selections)
                    assert choice in event['options'], event
                    send({'type': 'extension_ui_response', 'id': event['id'], 'value': choice})
            if event.get('type') == 'response' and event.get('id') == 'check':
                assert event.get('success'), event
                return event.get('data')

    def approval_notices():
        return [notice for notice in notices if notice.startswith('Approval mode is ')]

    commands = request('get_commands')['commands']
    assert any(command['name'] == 'auto-review-settings' for command in commands) == automatic
    assert len(approval_notices()) == 1, approval_notices()
    assert approval_notices()[0].startswith(f'Approval mode is {label}.')
    assert ('/auto-review-settings' in approval_notices()[0]) == automatic
    if automatic:
        request('prompt', message='/auto-review-settings', choices=('Thinking effort', 'Off'))
        request('get_state')
        assert prompts[-1]['options'] == ['Off', 'High', 'Max'], prompts[-1]
        assert any('Reviewer updated for this chat.' in notice and notice.endswith('· Off') for notice in notices)
        request('prompt', message='/auto-review-settings', choices=('Reviewer model', 'fixture/second-model'))
        request('get_state')
        assert any(notice.endswith('fixture/second-model · Off') for notice in notices)
    request('prompt', message='/fixture-reload')
    request('get_state')
    assert len(approval_notices()) == 1, approval_notices()
    assert request('get_messages')['messages'] == []
    process.stdin.close()
    assert process.wait(timeout=30) == 0
print(f'PASS Pi approval notice and reviewer settings for {mode}')
