"""Opt-in Linux integration check. Run with PYTHONPATH=. from the repository root."""
import argparse
from contextlib import ExitStack
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.request import urlopen
from urllib.error import HTTPError

from playwright.sync_api import Error as BrowserError, sync_playwright, expect
from pi_sandbox_factory import advisor_credentials, primary_agents, primary_workspace, sandbox
FILE_TASK = 'Read /workspace/input.txt, copy its contents to /workspace/output.txt and reply with the contents.'

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'research/primary-agents/native-editor'
SOURCE = ROOT / 'pi_sandbox_factory/containers/primary'
PANELS = {'codex': ('openai.chatgpt', 'chatgpt.openSidebar'),
          'claude': ('anthropic.claude-code', 'claude-vscode.sidebar.open'),
}


def podman(*args, payload=None):
    subprocess.run(['podman', *args], check=True, input=payload, text=True, stdout=subprocess.DEVNULL)


def listen(stack, socket_path, ticket, hub=None):
    args = ['node', str(HERE / 'relay.mjs'), '0', str(socket_path)]
    if hub:
        args.append(str(hub))
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    stack.callback(process.wait)
    stack.callback(process.terminate)
    process.stdin.write(ticket)
    process.stdin.close()
    port = int(process.stdout.readline())
    try:
        urlopen(f'http://127.0.0.1:{port}/').close()
    except HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise RuntimeError('The test relay accepted an unauthenticated request')
    return port


def verify_reply(page, prompt, expected, timeout):
    deadline = time.monotonic() + timeout
    submitted = False
    observations = set()
    previous_matches = 0
    while time.monotonic() < deadline:
        for frame in page.frames:
            if not frame.parent_frame:
                continue
            try:
                visible = frame.locator('body').inner_text(timeout=900)
                for text in ('Sign in', 'rate limit', 'API Error', 'server failed', 'Session expired'):
                    if text.lower() in visible.lower():
                        observations.add(text)
                if submitted:
                    approval = frame.get_by_role('button', name='1 Yes', exact=True)
                    if approval.count() == 1 and approval.is_visible():
                        approval.click()
                        print('Approved one workspace tool request.', flush=True)
                    state = visible[-5000:]
                    stopping = frame.get_by_role('button', name=re.compile(r'^(stop|cancel)( generation| response| turn)?$', re.I))
                    if visible.count(expected) > previous_matches and not any(stopping.nth(i).is_visible() for i in range(stopping.count())):
                        return
                    continue
                inputs = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible, '
                                       'input[type=text]:visible, [role=textbox]:visible')
                if inputs.count():
                    previous_matches = visible.count(expected)
                    inputs.last.fill(prompt, timeout=1500)
                    inputs.last.press('Enter')
                    submitted = True
                    print('Sent the test prompt in the native panel.', flush=True)
                else:
                    for name in ('Next', 'Continue', 'Skip', 'Get started', 'Start coding'):
                        button = frame.get_by_role('button', name=name, exact=True)
                        if button.count() == 1 and button.is_visible():
                            button.click(timeout=900)
                            break
            except BrowserError:
                # Extension startup replaces frames while the panel is mounting.
                continue
        page.wait_for_timeout(350)
    page.screenshot(path='/tmp/factory-native-failure.png')
    for frame in page.frames:
        print('FRAME', frame.locator('body').inner_text()[-8000:], flush=True)
    raise RuntimeError(f'Expected reply missing. Prompt submitted: {submitted}. Panel notices: {sorted(observations)}')


def resume_chat(page, output, timeout, expected):
    deadline = time.monotonic() + timeout
    opened = set()
    while time.monotonic() < deadline:
        for frame in page.frames:
            if not frame.parent_frame:
                continue
            try:
                buttons = frame.get_by_role('button')
                history = frame.get_by_role('button', name=re.compile('history|past conversations|recent chats|resume', re.I))
                if frame not in opened and history.count() and history.first.is_visible():
                    history.first.click()
                    opened.add(frame)
                if frame in opened:
                    (output / 'history-buttons.json').write_text(json.dumps(buttons.evaluate_all(
                        '(buttons) => buttons.map(b => ({text:b.innerText, aria:b.getAttribute("aria-label"),title:b.title}))'), indent=2))
                    (output / 'history.txt').write_text(frame.locator('body').inner_text())
                    page.screenshot(path=str(output / 'history.png'))
                    # The history popover duplicates the background chat row.
                    # Its later row is clickable; the background row is covered.
                    for index in reversed(range(buttons.count())):
                        previous = buttons.nth(index)
                        if previous.is_visible() and re.fullmatch(
                            r'(?!Untitled\b).+\n(?:just now|now|\d+\s*(?:s|m|h|minutes?|seconds?)\s*(?:ago)?)',
                            previous.inner_text().strip(), re.I,
                        ):
                            previous.click()
                            expect(frame.locator('body')).to_contain_text(expected, timeout=timeout * 1000)
                            print('Opened the saved conversation and verified its previous reply.', flush=True)
                            return
                if frame.locator('textarea:visible, [contenteditable]:visible').count():
                    (output / 'reload-buttons.json').write_text(json.dumps(buttons.evaluate_all(
                        '(buttons) => buttons.map(b => ({text:b.innerText, aria:b.getAttribute("aria-label"),title:b.title}))'), indent=2))
            except BrowserError:
                continue
        page.wait_for_timeout(250)
    page.screenshot(path=str(output / 'reload.png'))
    raise RuntimeError('Could not find the native conversation history control')


def run(args, root, stack):
    agent = args.agent
    fixture = 'factory-editor-test-' + secrets.token_hex(6)
    options = {'primary_agent': agent, 'uid': os.getuid(), 'gid': os.getgid()}
    editor = fixture + '-editor'
    client = root / 'client'
    client.mkdir()
    private = root / 'session'
    private.mkdir()
    # The production installer supplies the native extension and opener.
    wire = fixture + '-tools'
    egress = fixture + '-egress'
    for volume in (wire, egress):
        podman('volume', 'create', volume)
        stack.callback(podman, 'volume', 'rm', volume)

    def start(name, *command):
        podman('run', '-d', '--pull=never', '--name', name, *command)
        stack.callback(podman, 'rm', '-f', '-v', name)

    rules = root / 'squid.conf'
    rules.write_text(sandbox.squid_config(sandbox.parse_rules('\n'.join(primary_agents.PRIMARY_HOSTS[agent]))))
    start(fixture + '-proxy', '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
          '--cap-add=DAC_OVERRIDE', '-v', f'{rules}:/etc/squid/sandbox.conf:ro,z',
          '-v', egress + ':/run/egress:z', 'localhost/pi-sandbox-factory-proxy:1')
    isolated = ['--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--security-opt=label=disable', f'--userns=keep-id:uid={os.getuid()},gid={os.getgid()}',
                '--tmpfs', '/home/node:rw,mode=1777', '--tmpfs', '/workspace:rw,mode=1777']
    trusted = root / 'trusted'
    trusted.mkdir()
    (trusted/'settings.json').write_text(json.dumps({'agent':agent,'browser':False,'working_directory':'/workspace'}))
    (trusted/'context.md').write_text('Perform file and shell operations only in the workspace with the configured remote tools.')
    editor_mounts = ['-v', f'{args.code_server}:/opt/editor:ro', '-v', f'{client}:/qa:rw']
    controller_env = [arg for key, value in sandbox.RUNTIME_ENV.items() for arg in ('-e', f'{key}={value}')]
    start(fixture, *isolated, *controller_env, '-e', 'FACTORY_PRIMARY_AGENT=' + agent,
          '-v', egress + ':/run/primary-egress:ro', '-v', wire + ':/run/primary-workspace:ro,z',
          '-v', f'{private}:/run/primary-editor', '-v', f'{trusted}:/run/primary-input:ro',
          primary_agents.image(options))
    for _ in range(120):
        ready = subprocess.run(['podman', 'exec', fixture, 'test', '-f', '/tmp/factory-primary-ready'])
        if ready.returncode == 0:
            break
        time.sleep(.25)
    else:
        raise RuntimeError('Controller failed to start')
    target = {'codex': '.codex/auth.json', 'claude': '.claude/.credentials.json'}[agent]
    store = ['python3', '-c', advisor_credentials._WRITE_FILE, '/home/node/' + target]
    podman('exec', '-i', fixture, *store, payload=advisor_credentials.read_host(agent))

    ticket = secrets.token_urlsafe(32)
    hub = None
    first = secrets.choice(('cedar', 'maple', 'aspen'))
    last = secrets.choice(('violet', 'amber', 'coral'))
    prompt = (f'Your first word is {first}. Your second word is {last}. '
              'Reply with these two words joined by a hyphen. Do not use tools.')
    expected = first + '-' + last
    if args.separate:
        start(editor, *isolated, *editor_mounts, '-v', f'{private}:/run/primary-editor:ro', '-v', wire + ':/run/primary-workspace:z',
              '--tmpfs', '/run/primary-runtime:rw,mode=1777', '--entrypoint', '/usr/bin/tini',
              primary_workspace.image(options), '--', 'sleep', 'infinity')
        for path in ('/home/node/.codex/auth.json', '/home/node/.claude/.credentials.json',
                     '/run/primary-egress/proxy.sock', '/run/primary-bus/bus', '/run/podman/podman.sock'):
            podman('exec', editor, 'test', '!', '-e', path)
        podman('exec', '-i', editor, 'tee', '/run/primary-runtime/cwd.json', payload=json.dumps('/workspace'))
        prompt, expected = FILE_TASK, 'proof-' + secrets.token_hex(16)
        podman('exec', '-i', editor, 'tee', '/workspace/input.txt', payload=expected)
        podman('exec', '-d', editor, 'bash', '/opt/primary/worker.sh', agent)
    from pi_sandbox_factory import native_editor
    native_editor.install({'engine':'podman','primary_agent':agent}, editor, 'x86_64')
    port = listen(stack, client / 'editor.sock', ticket, hub)
    launch = SOURCE.parent/'editor'
    launch_settings = {'agent':agent, 'native_settings':native_editor.settings(agent),
        'binary':'/opt/editor/bin/code-server','authority':'127.0.0.1:' + str(port),'cwd':'/workspace',
        'password_hash':'','allow_uploads':False,'watch_fallback':(launch/'watch-fallback.cjs').read_text()}
    supervisor = subprocess.Popen(['podman','exec','-i',editor,'python3','-u','-c',(launch/'launch.py').read_text()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=(root/'editor.log').open('w'),text=True)
    stack.callback(supervisor.wait)
    stack.callback(supervisor.stdin.close)
    supervisor.stdin.write(json.dumps(launch_settings)+'\n'); supervisor.stdin.flush()
    assert supervisor.stdout.readline().strip() == 'ready', (root/'editor.log').read_text()[-2000:]
    podman('exec','-d',editor,'socat','UNIX-LISTEN:/qa/editor.sock,mode=0600,fork',
        'UNIX-CONNECT:/home/node/.local/share/pi-browser-editor/editor.sock')
    for _ in range(150):
        if (client/'editor.sock').is_socket(): break
        time.sleep(.2)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={'width': 1420, 'height': 920})
        page.context.add_cookies([{'name': 'editor_test', 'value': ticket, 'domain': '127.0.0.1',
                                   'path': '/', 'httpOnly': True, 'sameSite': 'Strict'}])
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{port}/?folder=/workspace')
        expect(page).to_have_title(re.compile('workspace', re.I), timeout=args.timeout * 1000)
        verify_reply(page, prompt, expected, args.timeout)
        if args.separate:
            second = 'second-' + secrets.token_hex(12)
            podman('exec','-i',editor,'tee','/workspace/second.txt',payload=second)
            verify_reply(page, 'Read second.txt and write its contents to final.txt, then reply with the contents.', second, args.timeout)
            final = subprocess.check_output(['podman','exec',editor,'cat','/workspace/final.txt'],text=True)
            assert final.strip() == second
            print('Two different workspace edits passed in one native chat.',flush=True)
        if args.reload:
            page.reload()
            resume_chat(page, args.output, args.timeout, second)
            verify_reply(page, 'What were the contents of second.txt that you read earlier? Reply from this chat without using tools.', second, args.timeout)
            print('Continued the saved chat after a page reload.', flush=True)
        page.screenshot(path=str(args.output / 'working.png'))
        assert 'Build with Agent' not in page.locator('body').inner_text()
        (args.output / 'browser-errors.json').write_text(json.dumps(errors, indent=2))
        assert not errors, errors
        browser.close()
    if args.separate:
        output = subprocess.check_output(['podman', 'exec', editor, 'cat', '/workspace/output.txt'], text=True)
        if output.strip() != expected:
            raise RuntimeError('The output file did not contain the workspace-only value')
        podman('exec', fixture, 'test', '!', '-e', '/workspace/output.txt')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('agent', choices=PANELS)
    parser.add_argument('--code-server', required=True, type=Path, help='Extracted code-server directory')
    parser.add_argument('--chromium', required=True, help='Local Chromium executable')
    parser.set_defaults(separate=True)
    parser.add_argument('--output', type=Path, required=True, help='Directory for screenshots and browser diagnostics')
    parser.add_argument('--reload', action='store_true', help='Reload the editor and continue the same chat')
    parser.add_argument('--timeout', type=int, default=180, help='Seconds to wait for the live reply')
    args = parser.parse_args()
    args.code_server = args.code_server.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='factory-ide-') as directory, ExitStack() as cleanup:
        run(args, Path(directory), cleanup)
    print(f'{args.agent}: authenticated reply verified; disposable resources removed.')
