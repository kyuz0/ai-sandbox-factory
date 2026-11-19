"""Exercise the shipped FrostPi webview and shared Pi tools in a disposable Factory container."""
import argparse
import base64
import hashlib
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

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]
DEV = REPO / 'pi_sandbox_factory/containers/dev'
EDITOR = DEV.parent / 'editor'
APPROVAL_LABELS = {
    'automatic': 'Auto (non-interactive)', 'assisted': 'Auto (interactive)',
    'manual': 'Manual approval (ask me)', 'off': 'YOLO (no approvals)',
}
sys.path.insert(0, str(REPO))
from pi_sandbox_factory import folders, sandbox


def check_external_link(page, frame, send):
    url = 'http://127.0.0.2:19382/fixture'
    image_url = 'http://127.0.0.2:19382/image.png'
    visits = []

    def serve_fixture(route):
        visits.append(route.request.url)
        route.fulfill(body='<!doctype html><title>Link fixture</title><h1>Link fixture</h1>',
                      content_type='text/html')

    context = page.context
    context.route(url, serve_fixture)
    context.route(image_url, serve_fixture)
    try:
        send('Factory link fixture')
        link = frame.get_by_role('link', name='Open link fixture', exact=True)
        dialog = page.get_by_role('dialog').filter(
            has_text='Open this link in your browser?')
        trusted_domains = page.get_by_role('dialog').filter(
            has_text='Do you want code-server to open the external website?')
        original_pages = context.pages
        expect(frame.get_by_text('Remote image blocked · 127.0.0.2', exact=True)).to_be_visible()
        link.click(timeout=25000)
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text('opening the link may bypass its network restrictions')
        expect(dialog).to_contain_text(url)
        assert context.pages == original_pages and not visits, 'The link opened before the user answered'
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).not_to_be_visible()
        assert context.pages == original_pages and not visits, 'Cancel opened a tab'

        link.click()
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Open in browser', exact=True).click()
        expect(trusted_domains).to_be_visible()
        assert context.pages == original_pages and not visits, 'The link opened before the editor confirmed it'
        trusted_domains.get_by_role('button', name='Cancel', exact=True).click()
        expect(trusted_domains).not_to_be_visible()
        assert context.pages == original_pages and not visits, 'Cancelling the editor confirmation opened a tab'

        link.click()
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Open in browser', exact=True).click()
        expect(trusted_domains).to_be_visible()
        with context.expect_page() as opened:
            trusted_domains.get_by_role('button', name='Open', exact=True).click()
        tab = opened.value
        expect(tab.get_by_role('heading', name='Link fixture')).to_be_visible()
        expect(dialog).not_to_be_visible()
        expect(trusted_domains).not_to_be_visible()
        assert tab.url == url and context.pages == [*original_pages, tab] and visits == [url]
        tab.close()
    finally:
        context.unroute(url, serve_fixture)
        context.unroute(image_url, serve_fixture)
    print('PASS FrostPi blocks remote Markdown images and opens external links once after confirmation.')


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
    frame.get_by_role('button', name=re.compile(r'^Stop (?:Pi|turn)$')).wait_for()
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


def check_session_naming(page, frame, send, inside, output):
    title = frame.locator('button.session-heading .session-title')

    def expect_title(name):
        title.filter(has_text=re.compile('^' + re.escape(name) + '$')).wait_for(timeout=18000)

    def new_session():
        frame.get_by_role('button', name='New or resume session').click()
        frame.locator('button.launcher-create').click()
        expect_title('New session')

    def expect_pi_name(name):
        files = inside('find', '/home/node/.pi/agent/sessions', '-name', '*.jsonl').splitlines()
        for path in files:
            entries = [json.loads(line) for line in inside('cat', path).splitlines()]
            names = [entry['name'] for entry in entries if entry['type'] == 'session_info']
            if names and names[-1] == name:
                messages = inside('pi', '--mode', 'rpc', '--session', path,
                                  input='{"id":"name-check","type":"get_state"}\n').splitlines()
                reply = next(json.loads(line) for line in messages if '"name-check"' in line)
                assert reply['success'] and reply['data']['sessionName'] == name, reply
                return
        raise AssertionError('No saved Pi session with name ' + name)

    send('Fix the sign-in errors in the authentication flow.')
    frame.get_by_text('FACTORY FIXTURE READY', exact=True).wait_for()
    expect_title('Fix sign-in errors')
    expect_pi_name('Fix sign-in errors')
    page.screenshot(path=str(output / 'generated-name.png'))
    send('Check the same session again.')
    frame.get_by_text('FACTORY FIXTURE READY', exact=True).last.wait_for()
    expect_title('Fix sign-in errors')
    new_session()
    send('Investigate the failing checkout request when the payment gateway responds slowly.')
    fallback = 'Investigate the failing checkout request when'
    expect_title(fallback)
    expect_pi_name(fallback)
    page.screenshot(path=str(output / 'fallback-name.png'))

    new_session()
    frame.get_by_role('button', name='Session actions', exact=True).click()
    frame.locator('button').filter(has_text=re.compile(r'^\s*Rename\s*$')).click()
    name_input = frame.get_by_role('textbox', name='Session name')
    name_input.fill('My checkout notes')
    name_input.press('Enter')
    expect_title('My checkout notes')
    send('Check the checkout notes.')
    frame.get_by_text('FACTORY FIXTURE READY', exact=True).wait_for()
    expect_pi_name('My checkout notes')
    assert inside('cat', '/tmp/fixture.log').count('TITLE REQUEST') == 2
    print('PASS Generated, fallback and manual names appear in FrostPi and persist in Pi sessions.')


def check_tool_images(page, frame, send, inside, output):
    thumbnails = frame.locator('button.image-thumb')

    def image_digest(locator):
        image = locator.get_attribute('src')
        assert image and image.startswith('data:image/'), image
        assert locator.evaluate('async image => { await image.decode(); return image.naturalWidth > 0; }')
        return hashlib.sha256(base64.b64decode(image.split(',', 1)[1])).hexdigest()

    for index, prompt in enumerate(('Read the Factory image fixture.', 'Capture the Factory browser fixture.')):
        send(prompt)
        frame.get_by_text('FACTORY TOOL IMAGE REACHED MODEL', exact=True).nth(index).wait_for(timeout=60000)
        frame.get_by_role('button', name='Send to Pi', exact=True).wait_for(timeout=15000)
    logs = inside('cat', '/tmp/fixture.log').splitlines()
    expected = [next(line.split()[3] for line in logs if line.startswith('TOOL IMAGE factory-' + name + '-'))
                for name in ('read', 'browser')]
    for index, digest in enumerate(expected):
        thumbnail = thumbnails.nth(index)
        thumbnail.wait_for(timeout=15000)
        assert image_digest(thumbnail.locator('img')) == digest
        thumbnail.click()
        lightbox = frame.locator('.image-lightbox img')
        lightbox.wait_for()
        assert image_digest(lightbox) == digest
        assert lightbox.evaluate('''image => {
            const pane = image.closest('.image-lightbox').getBoundingClientRect();
            const picture = image.getBoundingClientRect();
            const left = picture.left - pane.left;
            const right = pane.right - picture.right;
            return left >= 7 && right >= 7 && Math.abs(left - right) < 2;
        }'''), 'The lightbox should center the entire image inside the sidebar padding'
        page.screenshot(path=str(output / f'tool-image-{index + 1}.png'))
        page.keyboard.press('Escape')
        lightbox.wait_for(state='hidden')
    assert thumbnails.count() == len(expected)

    frame.get_by_role('button', name='Session actions', exact=True).click()
    frame.locator('button').filter(has_text=re.compile(r'^\s*Restart session\s*$')).click()
    frame.get_by_text('FACTORY TOOL IMAGE REACHED MODEL', exact=True).nth(1).wait_for(timeout=30000)
    for index, digest in enumerate(expected):
        thumbnail = thumbnails.nth(index)
        thumbnail.wait_for(timeout=15000)
        assert image_digest(thumbnail.locator('img')) == digest
    page.screenshot(path=str(output / 'restored-tool-images.png'))
    print('PASS Pi read and Betterwright screenshots match what the model received, open in FrostPi, and survive session restart.')


def check_folder_access(frame, send, inside, container):
    prompt = 'RUN_FACTORY_FOLDER_ACCESS'
    cases = json.loads(Path(__file__).with_name('folder_access_cases.json').read_text())
    send(prompt)
    complete = frame.get_by_text('FACTORY FOLDER CHECK FINISHED', exact=True)
    approval = frame.get_by_role('region', name='Prompt details')
    complete.or_(approval).wait_for(timeout=120000)
    assert complete.is_visible(), 'FrostPi asked for approval during the folder check'
    frame.get_by_role('button', name='Send to Pi', exact=True).wait_for(timeout=15000)
    session_file = inside('find', '/home/node/.pi/agent/sessions', '-type', 'f', '-name', '*.jsonl')
    entries = [json.loads(line) for line in inside('cat', session_file).splitlines()]
    frostpi = [entry['message'] for entry in entries if entry.get('message', {}).get('role') == 'toolResult']

    pi_results = []
    command = ['podman', 'exec', '-i', '--workdir', '/workspace/app', container, 'pi', '--mode', 'rpc',
               '--no-session', '--approve', '--provider', 'fixture', '--model', 'fixture']
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as pi:
        lines = Queue()

        def read():
            for line in pi.stdout:
                lines.put(line)
            lines.put(None)

        Thread(target=read, daemon=True).start()
        try:
            pi.stdin.write(json.dumps({'id': 'folder-test', 'type': 'prompt', 'message': prompt}) + '\n')
            pi.stdin.flush()
            deadline = time.monotonic() + 120
            while True:
                line = lines.get(timeout=max(0, deadline - time.monotonic()))
                assert line is not None, 'Pi stopped before completing the folder check'
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                assert not (event.get('type') == 'extension_ui_request'
                            and event.get('method') in ('select', 'confirm', 'input')), event
                if event.get('type') == 'tool_execution_end':
                    pi_results.append(event)
                if event.get('type') == 'agent_settled':
                    break
        finally:
            pi.stdin.close()
            try:
                pi.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pi.terminate()
                pi.wait(timeout=15)
    failures = []
    for client, results in (('FrostPi', frostpi), ('Pi CLI', pi_results)):
        assert len(results) == len(cases), (client, results)
        for step, result in zip(cases, results):
            assert step['tool'] == result['toolName'], (client, step, result)
            content = result.get('result', result).get('content', [])
            text = '\n'.join(item.get('text', '') for item in content)
            assert 'FOLDER_TEST_TOKEN' not in text, (client, step['name'])
            if bool(result.get('isError')) != step.get('denied', False) or step.get('text', '') not in text:
                failures.append(f'{client} · {step["name"]}\n{text}')
    assert not failures, '\n\n'.join(failures)
    assert inside('cat', '/workspace/parent-native.txt') == 'NATIVE_WORKSPACE_OK'
    assert inside('cat', '/mnt/output/nested/.env') == 'FOLDER_TEST_TOKEN=synthetic'
    assert inside('find', '/mnt/reference/nested', '-type', 'f') == '/mnt/reference/nested/readme.txt'
    assert inside('cat', '/mnt/reference/nested/readme.txt') == 'REFERENCE_OK'
    assert inside('cat', '/mnt/output/nested/copied.txt') == 'REFERENCE_OK'
    inside('test', '!', '-e', '/mnt/output/nested/denied-secret.txt')
    print(f'PASS {len(cases)} folder-access checks in each client. No approvals requested.')


def check_streaming_metrics(frame, send, output):
    metrics = frame.locator('.response-metrics')
    active = frame.get_by_role('button', name=re.compile(r'^Stop (?:Pi|turn)$'))
    answer = frame.get_by_text(re.compile(r'^FACTORY STREAMING ANSWER\.')).first
    live = re.compile(r'≈([\d,]+) output tokens · ≈([\d.]+) tok/s avg')

    def read_live_count():
        expect(active).to_be_visible()
        expect(metrics).to_contain_text(live)
        count, rate = live.search(metrics.inner_text()).groups()
        assert float(rate) > 0
        return int(count.replace(',', ''))

    send('Factory live token meter fixture')
    reasoning_count = read_live_count()
    assert reasoning_count > 0
    assert not answer.is_visible(), 'The token meter appeared after the answer started'
    metrics.screenshot(path=str(output / 'reasoning-meter.png'))

    expect(answer).to_be_visible(timeout=15000)
    answer_count = read_live_count()
    assert answer_count > reasoning_count
    metrics.screenshot(path=str(output / 'answer-meter.png'))
    expect(active).to_be_hidden(timeout=15000)
    print(f'PASS live output count and rate during reasoning ({reasoning_count} tokens) '
          f'and answer generation ({answer_count} tokens).')

    send('Factory burst token meter fixture')
    burst = re.compile(r'≈420 output tokens · ≈([\d.]+) tok/s avg')
    expect(metrics).to_contain_text(burst, timeout=15000)
    live_rate = float(burst.search(metrics.inner_text())[1])
    assert 0 < live_rate <= 420 / 2.4, live_rate
    metrics.screenshot(path=str(output / 'burst-meter.png'))

    expect(active).to_be_hidden(timeout=15000)
    final = re.compile(r'^360 output tokens · ([\d.]+) tok/s avg$')
    expect(metrics).to_contain_text(final)
    final_rate = float(final.search(metrics.inner_text().strip())[1])
    assert 0 < final_rate <= 360 / 3.25, final_rate
    metrics.screenshot(path=str(output / 'final-meter.png'))
    print(f'PASS buffered output averaged {live_rate} tok/s live and {final_rate} tok/s with reported usage.')



def check_pi_nonblocking_questions(inside, output):
    def tmux(*args):
        return inside('tmux', '-S', '/tmp/factory-question-test.sock', *args)

    def wait_for_text(text):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = tmux('capture-pane', '-p', '-t', 'pi')
            if text in snapshot:
                return snapshot
            time.sleep(.2)
        raise AssertionError(f'Expected {text!r} in the Pi terminal.\n{snapshot}')

    def enter(prompt):
        tmux('send-keys', '-t', 'pi', '-l', prompt)
        tmux('send-keys', '-t', 'pi', 'Enter')

    command = 'pi --provider fixture --model fixture --approve'
    tmux('new-session', '-d', '-s', 'pi', '-c', '/workspace', '-x', '110', '-y', '36', command)
    try:
        wait_for_text('rpiv-ask-user-question')
        enter('Factory deferred question fixture terminal')
        wait_for_text('FACTORY WORK CONTINUED')
        wait_for_text('Questions (1) · /questions')
        enter('/questions')
        wait_for_text('Which fruit should I use later?')
        tmux('send-keys', '-t', 'pi', 'Down', 'Enter')
        transcript = wait_for_text('FACTORY DEFERRED ANSWER RECEIVED')
        (output / 'pi-nonblocking-questions.txt').write_text(transcript)
        print('PASS Pi terminal continues work and accepts a deferred answer through /questions.')
    finally:
        tmux('kill-server')


def check(image, code_server, output, chromium, startup_only, chat_workflows, session_naming, tool_images,
          folder_access=False, streaming_metrics=False, profile_prompt=None, subagents=None,
          subagent_case=None, subagent_client=None, subagent_compaction=None, nonblocking_questions=False):
    output.mkdir(parents=True, exist_ok=True)
    name = 'factory-frostpi-check-' + uuid.uuid4().hex[:10]
    if subagent_compaction:
        subagents = 'automatic'
    mode = 'manual' if folder_access else startup_only or (subagents if subagents != 'disabled' else None) or 'assisted'
    web_workers = subagent_case == 'web-tools'

    def podman(*args, **kwargs):
        return subprocess.check_output(['podman', *args], text=True, **kwargs).strip()

    def inside(*args, **kwargs):
        return podman('exec', '-i', name, *args, **kwargs)

    with tempfile.TemporaryDirectory(prefix='factory-frostpi-check-') as tmp:
        root = Path(tmp)
        for directory in ('workspace', 'editor'):
            (root / directory).mkdir()
        if tool_images:
            fixture = 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NQQkAAAgEsCtpODsZyhQ+hMH+S/WcikAgEAgEAoFAIPgSLCQx6Fu1gaoXAAAAAElFTkSuQmCC'
            (root / 'workspace/image-fixture.png').write_bytes(base64.b64decode(fixture))
        (root / 'allowlist.txt').write_text('')
        config = {'options': {'approval': mode, 'browser': tool_images}, 'common': {'path': str(DEV)},
                  'engine': 'podman'}
        if subagents:
            config['subagents'] = subagents != 'disabled'
        if subagent_compaction:
            config['options']['compaction_recall'] = subagent_compaction
        if web_workers:
            # Both services run on loopback inside the network-disabled test container.
            config['network'] = 'unrestricted'
            config['options'].update(browser=True, web_access={
                'enabled': True, 'mode': 'searxng',
                'providers': {'searxng': {'endpoint': 'http://localhost:19382'}},
            })
        cwd = '/workspace'
        if profile_prompt:
            config['profile'] = sandbox.load_profile(str(profile_prompt.resolve()))
            project_config = root / 'workspace/.pi'
            project_config.mkdir()
            (project_config / 'APPEND_SYSTEM.md').write_text('FACTORY PROJECT APPEND MARKER\n')
            (root / 'workspace/AGENTS.md').write_text('FACTORY PROJECT CONTEXT MARKER\n')
        if folder_access:
            cwd = '/workspace/app'
            (root / 'workspace/app').mkdir()
            for directory in ('reference', 'output'):
                (root / directory / 'nested').mkdir(parents=True)
            (root / 'reference/nested/readme.txt').write_text('REFERENCE_OK')
            (root / 'reference/nested/private-link').symlink_to('/mnt/output/nested/.env')
            (root / 'output/nested/.env').write_text('FOLDER_TEST_TOKEN=synthetic')
            config.update(workspace=str(root / 'workspace'), working_directory='app', extra_folders=[
                {'name': name, 'source': str(root / name), 'read_only': name == 'reference'}
                for name in ('reference', 'output')
            ])
        sandbox.prepare_options(config, root)
        if subagents:
            policy = root / 'runtime-config/permissions.json'
            permissions = json.loads(policy.read_text())
            permissions['permission']['fixture_echo'] = 'allow'
            policy.write_text(json.dumps(permissions))
        model_script = 'subagent_compaction_model.mjs' if subagent_compaction else 'frostpi_model.mjs'
        mounts = [
            (root / 'workspace', '/workspace', 'rw'),
            (root / 'editor', '/home/node/.local/share/pi-browser-editor', 'rw'),
            (root / 'options.json', '/opt/sandbox/options.json', 'ro'),
            (root / 'runtime-config', '/opt/pi-security/config', 'ro'),
            (code_server, '/opt/test-code-server', 'ro'),
            (Path(__file__).with_name(model_script).resolve(), '/tmp/frostpi_model.mjs', 'ro'),
            (Path(__file__).with_name('frostpi_subagent_model.mjs').resolve(), '/tmp/frostpi_subagent_model.mjs', 'ro'),
        ]
        if folder_access:
            mounts.append((Path(__file__).with_name('folder_access_cases.json').resolve(),
                           '/tmp/folder-access-cases.json', 'ro'))
        args = ['run', '-d', '--name', name, '--network', 'none', '--userns', 'keep-id:uid=1000,gid=1000',
                '--user', '1000:1000', '--security-opt', 'label=disable',
                '-e', 'SANDBOX_APPROVAL_MODE=' + mode, '-e', 'SANDBOX_NETWORK_MODE=unrestricted', '--entrypoint', 'sleep']
        if subagents:
            args += ['-e', 'SANDBOX_SUBAGENTS=' + ('1' if config['subagents'] else '0'),
                     '-e', 'PI_SUBAGENTS_TEMP_ROOT=/tmp/factory-subagent-runs']
        if subagent_compaction:
            args += ['-e', 'COMPACTION_RECALL_MODE=' + ('full' if subagent_compaction == 'full' else 'lite')]
        if web_workers:
            args += ['-e', 'FACTORY_SUBAGENT_WEB_TOOLS=1']
            mounts.append((Path(__file__).with_name('subagent_web_tools.mjs').resolve(), '/tmp/subagent_web_tools.mjs', 'ro'))
        if folder_access:
            args += ['--cap-drop=ALL', '--security-opt=no-new-privileges'] + folders.mounts(config)
        else:
            args += ['--security-opt', 'seccomp=unconfined']
        for source, target, access in mounts:
            args += ['-v', f'{source}:{target}:{access}']
        podman(*args, image, 'infinity')
        supervisor = relay = None
        try:
            inside('python3', '/usr/local/lib/sandbox-bootstrap.py')
            if profile_prompt:
                inside('python3', '-c', '''
from pathlib import Path
file = Path('/home/node/.pi/agent/AGENTS.md')
file.write_text('FACTORY USER CONTEXT MARKER\\n' + file.read_text())
file.with_name('trust.json').write_text('{"/workspace":true}')
''')
                inside('python3', '/usr/local/lib/sandbox-bootstrap.py')
            inside('test', '!', '-d', '/opt/pendant')
            if subagents:
                from frostpi_subagents import check_native_subagents, prepare_subagent_fixtures
                prepare_subagent_fixtures(root / 'workspace', inside)
            if subagent_compaction:
                import subagent_compaction as compact_fixture
                compact_fixture.prepare(root / 'workspace', inside)

            if not startup_only:
                model = {'id': 'fixture', 'name': 'Factory fixture', 'reasoning': streaming_metrics,
                         'input': ['text', 'image'] if tool_images else ['text'],
                         'contextWindow': 131072, 'maxTokens': 8192, 'cost': dict.fromkeys(('input', 'output', 'cacheRead', 'cacheWrite'), 0)}
                catalogue = {'providers': {'fixture': {'api': 'openai-completions',
                             'baseUrl': 'http://127.0.0.1:19381/v1', 'apiKey': 'fixture-only', 'models': [model]}}}
                if subagent_compaction:
                    model['contextWindow'] = 1_048_576
                inside('tee', '/home/node/.pi/agent/models.json', input=json.dumps(catalogue))
                inside('python3', '-c', """import json, sys
from pathlib import Path
p = Path('/home/node/.pi/agent/settings.json')
v = json.loads(p.read_text())
v.update(defaultProvider='fixture', defaultModel='fixture')
if len(sys.argv) > 1:
    v['defaultThinkingLevel'] = sys.argv[1]
p.write_text(json.dumps(v))
""", *(['high'] if streaming_metrics else []))
                podman('exec', '-d', name, 'sh', '-c', 'node /tmp/frostpi_model.mjs > /tmp/fixture.log 2>&1')
                if subagent_compaction and subagent_client == 'pi':
                    compact_fixture.check_pi(inside, output, subagent_compaction)
                    return
                if subagents and subagent_client == 'pi':
                    check_native_subagents(name, root / 'workspace', inside, output, subagents, subagent_case)
                    return
                if profile_prompt:
                    cli = podman('exec', '-i', '--workdir', cwd, name, 'pi', '--print', '--no-session',
                                 '--approve', '--provider', 'fixture', '--model', 'fixture',
                                 'Factory profile prompt fixture')
                    assert 'FACTORY PROFILE PROMPT VERIFIED' in cli, cli
            with socket.socket() as port:
                port.bind(('127.0.0.1', 0))
                address = f'127.0.0.1:{port.getsockname()[1]}'
            settings = {'binary': '/opt/test-code-server/bin/code-server', 'cwd': cwd,
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
                page.goto(f'http://{address}/?folder={cwd}')
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
                if subagent_compaction:
                    compact_fixture.check_frostpi(page, frame, send, inside, output, subagent_compaction)
                    assert not errors, errors
                    browser.close()
                    if subagent_client != 'frostpi':
                        compact_fixture.check_pi(inside, output, subagent_compaction)
                    return
                if subagents:
                    from frostpi_subagents import check_frostpi_subagents
                    check_frostpi_subagents(page, frame, send, root / 'workspace', inside,
                                            output, subagents, subagent_case)
                    assert not errors, errors
                    browser.close()
                    if subagent_client != 'frostpi':
                        check_native_subagents(name, root / 'workspace', inside, output, subagents, subagent_case)
                    return
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
                if session_naming:
                    check_session_naming(page, frame, send, inside, output)
                    assert not errors, errors
                    browser.close()
                    return
                if tool_images:
                    check_tool_images(page, frame, send, inside, output)
                    assert not errors, errors
                    browser.close()
                    return
                if folder_access:
                    check_folder_access(frame, send, inside, name)
                    assert not errors, errors
                    browser.close()
                    return
                if profile_prompt:
                    send('Factory profile prompt fixture')
                    verified = frame.get_by_text('FACTORY PROFILE PROMPT VERIFIED', exact=True)
                    counts = frame.get_by_text(re.compile(r'^FACTORY PROFILE PROMPT COUNTS'))
                    response = verified.or_(counts)
                    response.wait_for(timeout=20000)
                    page.screenshot(path=str(output / 'profile-prompt.png'))
                    assert verified.is_visible(), response.inner_text()
                    assert not errors, errors
                    browser.close()
                    print('PASS The real Pi CLI and FrostPi model requests each include the profile, user and project context once.')
                    return
                if streaming_metrics:
                    check_streaming_metrics(frame, send, output)
                    assert not errors, errors
                    browser.close()
                    return
                if nonblocking_questions:
                    send('Factory deferred question fixture')
                    question = frame.get_by_role('region', name='Which fruit should I use later?')
                    question.get_by_text('Answer when ready.', exact=True).wait_for()
                    continued = frame.get_by_text('FACTORY WORK CONTINUED', exact=True)
                    continued.first.wait_for()
                    stop_turn = frame.get_by_role('button', name=re.compile(r'^Stop (?:Pi|turn)$'))
                    assert stop_turn.is_visible()
                    assert question.get_by_text('Recommended', exact=True).is_visible()
                    question.screenshot(path=str(output / 'nonblocking-question.png'))
                    question.get_by_role('button', name='Pear', exact=True).click()
                    frame.get_by_text('FACTORY DEFERRED ANSWER RECEIVED', exact=True).wait_for()
                    assert not question.count()

                    send('Factory deferred question fixture again')
                    question.wait_for()
                    continued.nth(1).wait_for()
                    stop_turn.click()
                    stop_turn.wait_for(state='hidden')
                    assert question.is_visible(), 'Stopping the turn discarded the pending question'
                    request_label = question.get_attribute('aria-labelledby')
                    frame.get_by_role('button', name='Session actions', exact=True).click()
                    frame.locator('button').filter(has_text=re.compile(r'^\s*Restart session\s*$')).click()
                    page.get_by_role('button', name='Restart session', exact=True).click()
                    expect(question).not_to_have_attribute('aria-labelledby', request_label, timeout=15000)
                    question.get_by_role('button', name='Pear', exact=True).wait_for()
                    assert question.count() == 1
                    question.get_by_role('button', name='Cancel', exact=True).click()
                    frame.get_by_text('FACTORY DEFERRED QUESTION DISMISSED', exact=True).wait_for()
                    assert not question.count()
                    assert not errors, errors
                    browser.close()
                    print('PASS FrostPi deferred questions deliver answers and survive Stop and a Pi process restart.')
                    check_pi_nonblocking_questions(inside, output)
                    return
                send('/auto-review-settings')
                frame.get_by_role('button', name='Reviewer model', exact=True).wait_for(timeout=15000)
                frame.get_by_role('button', name='Thinking effort', exact=True).wait_for(timeout=15000)
                page.screenshot(path=str(output / 'reviewer-settings.png'))
                frame.get_by_role('button', name='Cancel', exact=True).click()
                send('Factory tools fixture')
                frame.get_by_text('FACTORY TOOLS READY', exact=True).wait_for(timeout=25000)
                send('Factory question fixture')
                pear = frame.get_by_role('button', name='Pear', exact=True)
                pear.wait_for(timeout=25000)
                frame.get_by_text('Pick a fruit', exact=True).wait_for(timeout=15000)
                apple = frame.get_by_role('button', name='Apple Recommended', exact=True)
                assert apple.get_by_text('Recommended', exact=True).is_visible()
                assert pear.get_by_text('Second choice', exact=True).is_visible()
                page.screenshot(path=str(output / 'question.png'))
                pear.press('Enter')
                frame.get_by_text('Pick another fruit', exact=True).wait_for(timeout=15000)
                frame.get_by_role('button', name='Type something.', exact=True).click()
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
                detail = card.inner_text()
                assert 'Run this exact command outside the inner sandbox?' in detail
                assert 'Working directory: /workspace' in detail
                assert ('The reviewer rejected this command.\n'
                        'The fixture declined this command. REVIEWER_REASON_AT_THE_END') in detail
                assert '\n\nCommand\n# Write a marker to the test workspace.\nprintf ' in detail
                assert 'FINAL_APPROVAL_ARGUMENT' in detail
                assert '\n\nJustification\nCheck the full approval request' in detail
                assert '\n\nExecution scope\nThis uses the outer container' in detail
                assert card.evaluate('(e) => e.scrollHeight > e.clientHeight && e.scrollWidth <= e.clientWidth')
                page.screenshot(path=str(output / 'approval-start.png'))
                frame_height = frame.evaluate('window.innerHeight')
                page.set_viewport_size({'width': 1200, 'height': 700})
                frame.wait_for_function('(height) => window.innerHeight < height', arg=frame_height)
                card.focus()
                assert card.evaluate('(e) => e.ownerDocument.activeElement === e')
                card.press('End')
                deadline = time.monotonic() + 3
                at_end = '(e) => Math.abs(e.scrollHeight - e.clientHeight - e.scrollTop) <= 2'
                while time.monotonic() < deadline and not card.evaluate(at_end):
                    page.wait_for_timeout(50)
                assert card.evaluate(at_end)
                assert card.evaluate('''(e) => {
                    const text = e.querySelector('p').firstChild;
                    const marker = 'FINAL_APPROVAL_ARGUMENT';
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
                check_external_link(page, frame, send)
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
            if not subagents:
                log = subprocess.run(['podman', 'exec', name, 'cat', '/tmp/fixture.log'],
                                     text=True, capture_output=True)
                (output / 'fixture.log').write_text(log.stdout + log.stderr)
            raise
        finally:
            if subagents:
                log = subprocess.run(['podman', 'exec', name, 'cat', '/tmp/fixture.log'],
                                     text=True, capture_output=True)
                (output / 'fixture.log').write_text(log.stdout + log.stderr)
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
    scope.add_argument('--session-naming', action='store_true', help='Check generated and fallback names shared with Pi.')
    scope.add_argument('--tool-images', action='store_true', help='Check model-visible image results from Pi read and Betterwright.')
    scope.add_argument('--folder-access', action='store_true', help='Check Bash and native tools in mounted folders and /tmp.')
    scope.add_argument('--streaming-metrics', action='store_true', help='Check the live token meter during reasoning and answer output.')
    scope.add_argument('--nonblocking-questions', action='store_true', help='Check deferred questions while the model works, stops and reloads.')
    scope.add_argument('--profile-prompt', type=Path, help='Check a profile addendum in the model request from Pi CLI and FrostPi.')
    scope.add_argument('--subagents', choices=(*APPROVAL_LABELS, 'disabled'),
                       help='Check delegated tools and user approvals in installed Pi and FrostPi.')
    scope.add_argument('--subagent-compaction', choices=('lite', 'full', 'off'),
                       help='Check automatic child compaction and conversation recall in Pi and FrostPi.')
    parser.add_argument('--subagent-case', choices=('inline', 'context', 'background', 'mixed', 'parallel', 'named', 'specialist', 'nested', 'cancel',
                                                    'nested-approval', 'critical', 'native', 'manual', 'off', 'disabled', 'web-tools', 'interrupt-goal', 'stop-all'))
    parser.add_argument('--subagent-client', choices=('pi', 'frostpi'), help='Run only one client for diagnosis.')
    args = parser.parse_args()
    if args.subagent_case == 'web-tools' and args.subagents != 'automatic':
        parser.error('--subagent-case web-tools requires --subagents automatic')
    if args.subagent_case and not args.subagents:
        parser.error('--subagent-case requires --subagents')
    if args.subagent_client and not (args.subagents or args.subagent_compaction):
        parser.error('--subagent-client requires --subagents or --subagent-compaction')
    check(args.image, args.code_server.resolve(), args.output.resolve(), args.chromium,
          args.startup_only, args.chat_workflows, args.session_naming, args.tool_images, args.folder_access,
          args.streaming_metrics, args.profile_prompt, args.subagents, args.subagent_case, args.subagent_client,
          args.subagent_compaction, args.nonblocking_questions)
