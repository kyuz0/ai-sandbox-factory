"""Exercise the shipped FrostPi webview and shared Pi tools in a disposable Factory container."""
import argparse
import json
from pathlib import Path
import re
from queue import Queue
import selectors
import socket
import subprocess
import sys
import tempfile
import time
from threading import Thread
import uuid

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
DEV = REPO / 'pi_sandbox_factory/containers/dev'
EDITOR = DEV.parent / 'editor'
APPROVAL_LABELS = {
    'automatic': 'Auto (non-interactive)', 'assisted': 'Auto (interactive)',
    'manual': 'Manual approval (ask me)', 'off': 'YOLO (no approvals)',
}
sys.path.insert(0, str(REPO))
from pi_sandbox_factory import sandbox


def check_cli_goal(name, session, usage, output):
    with subprocess.Popen(['podman', 'exec', '-i', name, 'pi', '--mode', 'rpc', '--session', session],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as pi:
        events = Queue()

        def read_events():
            for line in pi.stdout:
                events.put(json.loads(line))

        Thread(target=read_events, daemon=True).start()

        def send(message):
            pi.stdin.write(json.dumps(message) + '\n')
            pi.stdin.flush()

        send({'type': 'prompt', 'id': 'goal-status', 'message': '/goal status'})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0, deadline - time.monotonic()))
            assert event.get('type') != 'agent_start', 'Opening a paused goal started a model turn'
            body = event.get('message', '')
            if event.get('method') == 'notify' and isinstance(body, str) and 'Revised Factory objective' in body and 'paused' in body.lower():
                assert usage in body, body
                (output / 'cli-goal.txt').write_text(body + '\n')
                break
        else:
            raise AssertionError('Pi CLI did not report the paused FrostPi goal')

        send({'type': 'prompt', 'id': 'goal-resume', 'message': '/goal resume'})
        deadline = time.monotonic() + 45
        finished = False
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0, deadline - time.monotonic()))
            if event.get('type') == 'tool_execution_end' and event.get('toolName') == 'goal_update':
                assert not event.get('isError') and 'Goal complete.' in json.dumps(event), event
                finished = True
            if event.get('type') == 'agent_settled' and finished:
                break
        assert finished, 'Resuming the revised goal did not complete it'
        send({'type': 'prompt', 'id': 'goal-status-after-resume', 'message': '/goal status'})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            event = events.get(timeout=max(0, deadline - time.monotonic()))
            body = event.get('message', '')
            if event.get('method') == 'notify' and isinstance(body, str) and 'Revised Factory objective' in body:
                assert re.search(r'Goal:\s+complete', body), body
                (output / 'cli-goal-complete.txt').write_text(body + '\n')
                break
        else:
            raise AssertionError('Pi CLI did not retain the completed goal')
        pi.stdin.close()
        assert pi.wait(timeout=15) == 0


def check_chat_workflows(page, frame, composer, send, inside, output):
    def reply(text):
        frame.get_by_text('CHAT REPLY ' + text, exact=True).wait_for(timeout=25000)
        frame.get_by_role('button', name='Send to Pi', exact=True).wait_for(timeout=15000)

    def sessions():
        paths = inside('find', '/home/node/.pi/agent/sessions', '-type', 'f', '-name', '*.jsonl').splitlines()
        return {path: inside('cat', path) for path in paths}

    send('Factory chat original')
    reply('original')
    send('Factory chat original leaf')
    reply('original leaf')
    original = next(iter(sessions()))
    message = frame.locator('article').filter(has_text='Factory chat original leaf').first
    message.hover()
    message.get_by_role('button', name='Branch from this prompt in the same session').click()
    page.locator('.quick-input-list .monaco-list-row').filter(has_text='No summary').first.click()
    frame.get_by_text('CHAT REPLY original leaf', exact=True).wait_for(state='hidden', timeout=15000)
    send('Factory chat branch')
    reply('branch')
    assert list(sessions()) == [original]
    frame.get_by_role('button', name=re.compile('Switch conversation branch')).last.click()
    page.locator('.quick-input-list .monaco-list-row').filter(has_text='Factory chat original leaf').last.click()
    page.locator('.quick-input-list .monaco-list-row').filter(has_text='No summary').first.click()
    reply('original leaf')
    assert not frame.get_by_text('CHAT REPLY branch', exact=True).count()

    message.hover()
    message.get_by_role('button', name='Fork session from this message').click()
    frame.get_by_text('CHAT REPLY original leaf', exact=True).wait_for(state='hidden', timeout=15000)
    send('Factory chat fork')
    reply('fork')
    histories = sessions()
    assert len(histories) == 2, list(histories)
    assert 'CHAT REPLY fork' not in histories[original]
    assert 'CHAT REPLY fork' in next(history for path, history in histories.items() if path != original)
    page.screenshot(path=str(output / 'fork.png'))

    send('Factory chat steering initial')
    frame.get_by_role('button', name='Stop Pi', exact=True).wait_for()
    composer.fill('Factory chat steer')
    frame.get_by_role('button', name='Choose streaming delivery').click()
    frame.get_by_role('option', name=re.compile('Steer')).click()
    frame.get_by_role('button', name='Send as Steer').click()
    reply('steer')

    frame.locator('button.session-heading').click()
    session_list = frame.get_by_role('listbox', name='Open sessions', exact=True)
    session_list.get_by_role('option', selected=False).click(timeout=15000)
    frame.get_by_text('CHAT REPLY fork', exact=True).wait_for(state='hidden', timeout=15000)
    reply('original')
    send('Factory chat original continued')
    reply('original continued')
    assert 'CHAT REPLY original continued' in sessions()[original]
    print('PASS Same-session branch and restore, separate fork, steering and independent continuation of the original chat.')


def check(image, code_server, output, chromium, startup_only, chat_workflows):
    output.mkdir(parents=True, exist_ok=True)
    name = 'factory-frostpi-check-' + uuid.uuid4().hex[:10]
    mode = startup_only or 'assisted'

    def podman(*args, **kwargs):
        return subprocess.check_output(['podman', *args], text=True, **kwargs).strip()

    def inside(*args, **kwargs):
        return podman('exec', '-i', name, *args, **kwargs)

    with tempfile.TemporaryDirectory(prefix='factory-frostpi-check-') as tmp:
        root = Path(tmp)
        for directory in ('workspace', 'editor'):
            (root / directory).mkdir()
        (root / 'allowlist.txt').write_text('')
        sandbox.prepare_options({'options': {'approval': mode}, 'common': {'path': str(DEV)}}, root)
        mounts = [
            (root / 'workspace', '/workspace', 'rw'),
            (root / 'editor', '/home/node/.local/share/pi-browser-editor', 'rw'),
            (root / 'options.json', '/opt/sandbox/options.json', 'ro'),
            (root / 'runtime-config', '/opt/pi-security/config', 'ro'),
            (code_server, '/opt/test-code-server', 'ro'),
            (Path(__file__).with_name('frostpi_model.mjs').resolve(), '/tmp/frostpi_model.mjs', 'ro'),
        ]
        args = ['run', '-d', '--name', name, '--network', 'none', '--userns', 'keep-id:uid=1000,gid=1000',
                '--user', '1000:1000', '--security-opt', 'label=disable', '--security-opt', 'seccomp=unconfined',
                '-e', 'SANDBOX_APPROVAL_MODE=' + mode, '-e', 'SANDBOX_NETWORK_MODE=unrestricted', '--entrypoint', 'sleep']
        for source, target, access in mounts:
            args += ['-v', f'{source}:{target}:{access}']
        podman(*args, image, 'infinity')
        supervisor = relay = None
        try:
            inside('python3', '/usr/local/lib/sandbox-bootstrap.py')
            inside('test', '!', '-d', '/opt/pendant')
            if not startup_only:
                model = {'id': 'fixture', 'name': 'Factory fixture', 'reasoning': False, 'input': ['text'],
                         'contextWindow': 131072, 'maxTokens': 8192, 'cost': dict.fromkeys(('input', 'output', 'cacheRead', 'cacheWrite'), 0)}
                catalogue = {'providers': {'fixture': {'api': 'openai-completions',
                             'baseUrl': 'http://127.0.0.1:19381/v1', 'apiKey': 'fixture-only', 'models': [model]}}}
                inside('tee', '/home/node/.pi/agent/models.json', input=json.dumps(catalogue))
                inside('python3', '-c', """import json
from pathlib import Path
p = Path('/home/node/.pi/agent/settings.json')
v = json.loads(p.read_text())
v.update(defaultProvider='fixture', defaultModel='fixture')
p.write_text(json.dumps(v))
""")
                podman('exec', '-d', name, 'sh', '-c', 'node /tmp/frostpi_model.mjs > /tmp/fixture.log 2>&1')
            with socket.socket() as port:
                port.bind(('127.0.0.1', 0))
                address = f'127.0.0.1:{port.getsockname()[1]}'
            settings = {'binary': '/opt/test-code-server/bin/code-server', 'cwd': '/workspace',
                        'authority': address, 'password_hash': '', 'watch_fallback': (EDITOR / 'watch-fallback.cjs').read_text()}
            with (output / 'editor.log').open('w') as log, sync_playwright() as playwright:
                supervisor = subprocess.Popen(['podman', 'exec', '-i', name, 'python3', '-u', '-c',
                                               (EDITOR / 'launch.py').read_text()],
                                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
                supervisor.stdin.write(json.dumps(settings) + '\n')
                supervisor.stdin.flush()
                with selectors.DefaultSelector() as ready:
                    ready.register(supervisor.stdout, selectors.EVENT_READ)
                    assert ready.select(90) and supervisor.stdout.readline() == 'ready\n'
                relay = subprocess.Popen(['socat', f'TCP-LISTEN:{address.split(":")[-1]},bind=127.0.0.1,reuseaddr,fork',
                                          'UNIX-CONNECT:' + str(root / 'editor/editor.sock')])
                browser = playwright.chromium.launch(headless=True, executable_path=chromium)
                page = browser.new_page(viewport={'width': 1440, 'height': 960})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://{address}/?folder=/workspace')
                page.locator('.monaco-workbench').wait_for(timeout=60000)
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    frame = next((f for f in page.frames if f.locator('[aria-label="Message Pi"]').count()), None)
                    if frame:
                        break
                    page.wait_for_timeout(150)
                else:
                    raise AssertionError('Factory did not open a fresh FrostPi session')
                composer = frame.get_by_label('Message Pi', exact=True)

                def send(prompt):
                    composer.fill(prompt)
                    frame.get_by_role('button', name='Send to Pi', exact=True).click(timeout=15000)

                notice = frame.get_by_text(re.compile(re.escape('Approval mode is ' + APPROVAL_LABELS[mode]) + r'\.'))
                notice.wait_for(timeout=15000)
                assert notice.count() == 1
                page.screenshot(path=str(output / 'startup.png'))
                assert ('/auto-review-settings' in notice.inner_text()) == (mode in ('automatic', 'assisted'))
                if startup_only:
                    assert not errors, errors
                    browser.close()
                    print(f'PASS {mode} mode notice before the first prompt without a configured model.')
                    return
                if chat_workflows:
                    check_chat_workflows(page, frame, composer, send, inside, output)
                    assert not errors, errors
                    browser.close()
                    return
                send('/auto-review-settings')
                frame.get_by_role('button', name='Reviewer model', exact=True).wait_for(timeout=15000)
                frame.get_by_role('button', name='Thinking effort', exact=True).wait_for(timeout=15000)
                page.screenshot(path=str(output / 'reviewer-settings.png'))
                frame.get_by_role('button', name='Cancel', exact=True).click()
                send('Factory tools fixture')
                frame.get_by_text('FACTORY TOOLS READY', exact=True).wait_for(timeout=25000)
                send('Factory question fixture')
                pear = frame.get_by_role('button', name='2. Pear — Second choice', exact=True)
                pear.wait_for(timeout=25000)
                page.screenshot(path=str(output / 'question.png'))
                pear.click()
                frame.get_by_text('[Custom answer] Pick another fruit', exact=True).wait_for(timeout=15000)
                frame.get_by_role('button', name='3. Type something.', exact=True).click()
                frame.get_by_role('textbox', name=re.compile(r'Pick another fruit')).fill('Dragon fruit')
                frame.get_by_role('button', name='Submit', exact=True).click()
                frame.get_by_role('textbox', name=re.compile(r'Pick several fruits')).fill('1,2')
                frame.get_by_role('button', name='Submit', exact=True).click()
                frame.get_by_text('FACTORY QUESTION ANSWERS RECEIVED', exact=True).wait_for(timeout=25000)
                send('Factory question fixture again')
                pear.wait_for(timeout=25000)
                frame.get_by_role('button', name='Cancel', exact=True).click()
                frame.get_by_text('FACTORY QUESTION DISMISSED', exact=True).wait_for(timeout=25000)
                assert not pear.is_visible()
                send('Factory approval fixture')
                card = frame.get_by_role('region', name='Prompt details')
                card.wait_for(timeout=25000)
                allow = frame.get_by_role('button', name='Allow this exact command once')
                deny = frame.get_by_role('button', name='Deny', exact=True)
                assert allow.is_visible() and deny.is_visible()
                assert 'Working directory: /workspace' in card.inner_text()
                assert 'FINAL_APPROVAL_ARGUMENT' in card.inner_text()
                assert 'REVIEWER_REASON_AT_THE_END' in card.inner_text()
                assert card.evaluate('(e) => e.scrollHeight > e.clientHeight && e.scrollWidth <= e.clientWidth')
                page.screenshot(path=str(output / 'approval-start.png'))
                page.set_viewport_size({'width': 1200, 'height': 700})
                card.focus()
                card.press('End')
                deadline = time.monotonic() + 3
                at_end = '(e) => Math.abs(e.scrollHeight - e.clientHeight - e.scrollTop) <= 2'
                while time.monotonic() < deadline and not card.evaluate(at_end):
                    page.wait_for_timeout(50)
                assert card.evaluate(at_end)
                reason = card.get_by_text(re.compile('REVIEWER_REASON_AT_THE_END'))
                assert reason.count() == 1
                assert card.evaluate('''(e) => {
                    const text = e.querySelector('p').firstChild;
                    const marker = 'REVIEWER_REASON_AT_THE_END';
                    const offset = text.textContent.lastIndexOf(marker);
                    const range = document.createRange();
                    range.setStart(text, offset);
                    range.setEnd(text, offset + marker.length);
                    const detail = range.getBoundingClientRect();
                    const bounds = e.getBoundingClientRect();
                    return detail.top >= bounds.top && detail.bottom <= bounds.bottom;
                }''')
                assert allow.is_visible() and deny.is_visible()
                page.screenshot(path=str(output / 'approval.png'))
                deny.click()
                frame.get_by_text('FACTORY FIXTURE READY', exact=True).wait_for(timeout=25000)
                marker = root / 'workspace/approval-marker.txt'
                assert not marker.exists()
                send('Factory approval fixture again')
                allow.wait_for(timeout=25000)
                allow.click()
                frame.get_by_text('FACTORY FIXTURE READY', exact=True).nth(1).wait_for(timeout=25000)
                assert marker.read_text().endswith('FINAL_APPROVAL_ARGUMENT')
                send('/goal Factory goal fixture')
                frame.get_by_text('FACTORY GOAL TURN', exact=True).first.wait_for(timeout=25000)
                composer.fill('/goal pause')
                composer.press('Control+Enter')
                frame.get_by_text(re.compile(r'goal paused', re.I)).first.wait_for(timeout=15000)
                send('/goal revise Revised Factory objective')
                send('/goal status')
                status = frame.get_by_text(re.compile(r'Objective:\s+Revised Factory objective'))
                status.wait_for(timeout=15000)
                usage = re.search(r'\d+ turns · \$[\d.]+ spent', status.inner_text()).group()
                page.screenshot(path=str(output / 'goal-paused.png'))
                assert not errors, errors
                browser.close()
            supervisor.stdin.close()
            assert supervisor.wait(timeout=20) == 0
            supervisor = None
            sessions = inside('find', '/home/node/.pi/agent/sessions', '-type', 'f', '-name', '*.jsonl').splitlines()
            assert len(sessions) == 1, sessions
            check_cli_goal(name, sessions[0], usage, output)
            print('PASS Startup notice, question answers, full approval, deny and allow once in FrostPi, with revised goal restored and completed in the Pi CLI.')
        except Exception:
            log = subprocess.run(['podman', 'exec', name, 'cat', '/tmp/fixture.log'], text=True, capture_output=True)
            (output / 'fixture.log').write_text(log.stdout + log.stderr)
            raise
        finally:
            if relay:
                relay.terminate()
                relay.wait(timeout=10)
            if supervisor:
                supervisor.stdin.close()
                try:
                    supervisor.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    supervisor.terminate()
            podman('rm', '-f', '-t', '2', name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--code-server', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', help='Path to Chromium when Playwright uses an existing installation.')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--startup-only', choices=APPROVAL_LABELS, help='Check only the mode notice on an unconfigured sandbox.')
    scope.add_argument('--chat-workflows', action='store_true', help='Check steering, branching, forking and continuing the original chat.')
    args = parser.parse_args()
    check(args.image, args.code_server.resolve(), args.output.resolve(), args.chromium, args.startup_only, args.chat_workflows)
