"""Opt-in Linux integration check. Run with PYTHONPATH=. from the repository root."""
import argparse
import base64
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import struct
import sys
import tempfile
import time
import zlib
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
          'agy': ('google.google-antigravity', 'workbench.view.extension.antigravity-sidebar'),
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


def stop_controls(frame):
    # Claude's pinned webview renders an unlabelled square in its submit button.
    return frame.get_by_role('button', name=re.compile(r'^(stop|cancel)(\b|\s)', re.I)).or_(
        frame.locator('button:has(svg[class*="stopIcon"])'))


def verify_reply(page, prompt, expected, timeout, output=None):
    deadline = time.monotonic() + timeout
    submitted = False
    submitted_frame = None
    observations = set()
    previous_matches = 0
    last_snapshot = 0
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
                    if frame != submitted_frame:
                        continue
                    one_time = frame.get_by_text('Yes, allow this time', exact=True)
                    if one_time.count() == 1 and one_time.is_visible() and not getattr(page, 'factory_automatic', False):
                        # Native MCP approval must display the actual arguments,
                        # not just the tool name and a generic permission prompt.
                        if not frame.get_by_text('command', exact=True).count() and '"command"' not in visible:
                            raise AssertionError('Workspace command is missing from the approval display')
                        if output:
                            page.screenshot(path=str(output / 'permission-request.png'))
                        one_time.click()
                        frame.get_by_role('button', name=re.compile(r'^Submit')).click()
                        print('Approved one workspace tool request.', flush=True)
                    approval = frame.get_by_role('button', name=re.compile(r'^(1 Yes|Allow once)$'))
                    if approval.count() == 1 and approval.is_visible():
                        approval.click()
                        print('Approved one workspace tool request.', flush=True)
                    stopping = stop_controls(frame)
                    busy = frame.get_by_role('button', name=re.compile(r'Recent chats\. [1-9]\d* in progress'))
                    # Do not mistake the submitted prompt's own receipt for
                    # the model's reply while the next command is still running.
                    if (visible.count(expected) > previous_matches + prompt.count(expected)
                            and 'Claude is working' not in visible
                            and not any(stopping.nth(i).is_visible() for i in range(stopping.count()))
                            and not any(busy.nth(i).is_visible() for i in range(busy.count()))):
                        return
                    continue
                inputs = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible, '
                                       'input[type=text]:visible, [role=textbox]:visible')
                if inputs.count():
                    previous_matches = visible.count(expected)
                    inputs.last.fill(prompt, timeout=1500)
                    inputs.last.press('Enter')
                    submitted = True
                    submitted_frame = frame
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
        if output and time.monotonic() - last_snapshot > 10:
            page.screenshot(path=str(output / 'latest.png'))
            snapshots = []
            for frame in page.frames:
                if frame.parent_frame:
                    try:
                        snapshots.append(frame.url.split('?')[0] + '\n' + frame.locator('body').inner_text(timeout=900))
                    except BrowserError:
                        continue
            (output / 'latest.txt').write_text('\n'.join(snapshots))
            last_snapshot = time.monotonic()
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


def check_cancellation(page, agent, editor, output, timeout):
    """Cancel a confirmed running workspace process through the native panel."""
    source = ('import os, signal\nfrom pathlib import Path\n'
              'Path("/workspace/cancel.pid").write_text(str(os.getpid()))\n'
              'signal.pause()\n'
              'Path("/workspace/cancel-finished").touch()\n')
    podman('exec', '-i', editor, 'tee', '/workspace/cancel-check.py', payload=source)
    prompt = 'Run python3 /workspace/cancel-check.py in the foreground and wait for it. Do not run it in the background or change the file. I will stop it using the Stop button.'
    deadline = time.monotonic() + timeout
    submitted = False
    clicked = False
    while time.monotonic() < deadline and not clicked:
        for frame in page.frames:
            if not frame.parent_frame:
                continue
            inputs = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible, [role=textbox]:visible')
            if not submitted and inputs.count():
                inputs.last.fill(prompt)
                inputs.last.press('Enter')
                submitted = True
            approval = frame.get_by_role('button', name=re.compile(r'^(1 Yes|Allow once)$'))
            if approval.count() == 1 and approval.is_visible():
                approval.click()
            one_time = frame.get_by_text('Yes, allow this time', exact=True)
            if one_time.count() == 1 and one_time.is_visible():
                one_time.click()
                frame.get_by_role('button', name=re.compile(r'^Submit')).click()
            started = subprocess.run(['podman', 'exec', editor, 'test', '-f', '/workspace/cancel.pid']).returncode == 0
            if started:
                buttons = frame.get_by_role('button')
                if buttons.count():
                    (output / 'cancel-buttons.json').write_text(json.dumps(buttons.evaluate_all(
                        '(buttons) => buttons.map(b => ({text:b.innerText, aria:b.getAttribute("aria-label"),title:b.title}))'), indent=2))
                stop = stop_controls(frame)
                for index in range(stop.count()):
                    if stop.nth(index).is_visible():
                        page.screenshot(path=str(output / 'before-cancel.png'))
                        stop.nth(index).click()
                        clicked = True
                        break
        page.wait_for_timeout(250)
    if not clicked:
        page.screenshot(path=str(output / 'cancel-failed.png'))
        raise RuntimeError('Could not cancel the running workspace command from the native panel')
    pid = subprocess.check_output(['podman', 'exec', editor, 'cat', '/workspace/cancel.pid'], text=True).strip()
    if agent == 'codex':
        # Codex keeps resumable exec sessions across turn cancellation. Stop
        # ends the reply; it does not promise to terminate background commands.
        for frame in page.frames:
            if frame.parent_frame:
                stop = stop_controls(frame)
                for index in range(stop.count()):
                    expect(stop.nth(index)).not_to_be_visible(timeout=timeout * 1000)
        if subprocess.run(['podman', 'exec', editor, 'test', '-e', '/proc/' + pid]).returncode == 0:
            receipt = 'stopped-' + secrets.token_hex(8)
            verify_reply(page, f'Stop the test command still running as process {pid} in the workspace. '
                         f'Run kill -TERM {pid}, then verify it has exited. Do not edit any files. '
                         f'Reply {receipt} after it has stopped.', receipt, timeout, output)
            print('Codex stopped the reply; the next turn stopped its resumable workspace command.', flush=True)
        deadline = time.monotonic() + timeout
    # Use the remaining test budget: a successful click alone does not prove
    # that cancellation reached the command in the other container.
    while time.monotonic() < deadline:
        alive = subprocess.run(['podman', 'exec', editor, 'test', '-e', '/proc/' + pid]).returncode == 0
        if not alive:
            break
        page.wait_for_timeout(250)
    else:
        raise RuntimeError('The native panel stopped, but its workspace command is still running')
    podman('exec', editor, 'test', '!', '-e', '/workspace/cancel-finished')
    receipt = 'recovered-' + secrets.token_hex(8)
    verify_reply(page, f'Run printf {receipt} and reply with its output.', receipt, timeout, output)
    page.screenshot(path=str(output / 'after-cancel.png'))
    print('Cancellation recovery passed: the test process is gone and the next tool request succeeded.', flush=True)


def choose_manual_approvals(page, agent, output):
    for frame in page.frames:
        if not frame.parent_frame:
            continue
        if agent == 'codex':
            control = frame.get_by_role('button', name='Change permissions', exact=True)
            if control.count() == 1 and control.is_visible():
                control.click()
                frame.get_by_text('Ask for approval', exact=True).click()
                page.screenshot(path=str(output / 'manual-approvals.png'))
                return
        elif agent == 'claude':
            control = frame.get_by_title(
                'Claude will approve actions that pass a safety check and pause for anything risky. '
                'Click to change, or press Shift+Tab to cycle.', exact=True)
            if control.count() == 1 and control.is_visible():
                control.click()
                frame.get_by_text('Manual', exact=True).click()
                page.screenshot(path=str(output / 'manual-approvals.png'))
                return
    raise RuntimeError('Could not select manual approvals in the native panel')


def check_codex_file_isolation(page, controller, editor, output, timeout):
    """Exercise native file tools against different files in the two containers."""
    def png(red, green, blue):
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress((b'\x00' + bytes((red, green, blue)) * 32) * 32)) + chunk(b'IEND', b''))
    for container, color in ((controller, (255, 0, 0)), (editor, (0, 0, 255))):
        encoded = base64.b64encode(png(*color)).decode()
        podman('exec', '-i', container, 'python3', '-c',
               'import sys,base64;from pathlib import Path;Path("/workspace/isolation.png").write_bytes(base64.b64decode(sys.stdin.read()))',
               payload=encoded)
        podman('exec', '-i', container, 'tee', '/workspace/isolation-patch.txt', payload='before\n')
    podman('exec', controller, 'touch', '/home/node/.factory-private-isolation')
    verify_reply(page, 'Use your native view_image tool to inspect /workspace/isolation.png. '
                 'Do not use shell commands to inspect it. Reply with the color in lowercase.',
                 'blue', timeout, output)
    receipt = 'patch-' + secrets.token_hex(12)
    verify_reply(page, 'Use apply_patch to replace the text in /workspace/isolation-patch.txt with '
                 f'{receipt}. Then use the workspace shell to test whether '
                 '/home/node/.factory-private-isolation exists. Do not create that file. '
                 'Write exactly present or absent into /workspace/isolation-access.txt. '
                 f'Reply {receipt} after both operations finish.', receipt, timeout, output)
    for container, expected in ((editor, receipt), (controller, 'before')):
        actual = subprocess.check_output(['podman', 'exec', container, 'cat', '/workspace/isolation-patch.txt'], text=True)
        assert actual.strip() == expected, (container, actual)
    actual = subprocess.check_output(['podman', 'exec', editor, 'cat', '/workspace/isolation-access.txt'], text=True)
    assert actual.strip() == 'absent', actual
    # Confirm that the requested native tools actually ran, rather than only
    # accepting a model's statement about which tool it used.
    inspection = '''import json
from pathlib import Path
calls=[]
for path in (Path.home()/'.codex/sessions').rglob('*.jsonl'):
 for line in path.read_text().splitlines():
  try: item=json.loads(line).get('payload',{})
  except ValueError: continue
  if item.get('type') in ('function_call','custom_tool_call'):
   calls.append({'name':item.get('name'),'arguments':item.get('arguments',item.get('input',''))})
print(json.dumps({'view_image':any('view_image' in str(c) for c in calls),
                  'apply_patch':any('apply_patch' in str(c) for c in calls),
                  'tools':sorted(set(c['name'] for c in calls if c['name']))}))'''
    invoked = json.loads(subprocess.check_output(['podman', 'exec', controller, 'python3', '-c', inspection], text=True))
    (output / 'isolation-tools.json').write_text(json.dumps(invoked, indent=2))
    assert invoked['view_image'] and invoked['apply_patch'], invoked
    print('Native image and patch tools used workspace files; the controller file stayed unchanged and its private marker was absent.', flush=True)


def check_denial(page, editor, output, timeout, agent='pi'):
    deadline = time.monotonic() + timeout
    submitted = denied = False
    while time.monotonic() < deadline:
        for frame in page.frames:
            if not frame.parent_frame:
                continue
            inputs = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible, [role=textbox]:visible')
            if not submitted and inputs.count():
                inputs.last.fill('Run touch /workspace/denied-check. I will deny the permission request. If denied, do not retry or use another tool; just say the command was denied.')
                inputs.last.press('Enter')
                submitted = True
            no = frame.get_by_role('button', name=re.compile(r'^(\d+\s+)?(No|Deny|Reject)(?:,? (?:deny|reject|this time|once))?$', re.I))
            if agent == 'agy' and frame.get_by_text('Yes, allow this time', exact=True).count():
                choice = frame.get_by_test_id('ask-question-writein')
                if choice.count() == 1 and choice.is_visible():
                    page.screenshot(path=str(output / 'permission-request.png'))
                    choice.fill('Do not run this command. Report that permission was denied.')
                    frame.get_by_role('button', name=re.compile(r'^Submit')).click()
                    denied = True
            for index in range(no.count() if agent != 'agy' else 0):
                if no.nth(index).is_visible() and no.nth(index).is_enabled():
                    page.screenshot(path=str(output / 'permission-request.png'))
                    no.nth(index).click()
                    denied = True
                    break
            stop = stop_controls(frame)
            if denied and inputs.count() and not any(stop.nth(i).is_visible() for i in range(stop.count())):
                podman('exec', editor, 'test', '!', '-e', '/workspace/denied-check')
                page.screenshot(path=str(output / 'permission-denied.png'))
                print('Denied the native permission request; its workspace command did not run.', flush=True)
                return
        page.wait_for_timeout(250)
    page.screenshot(path=str(output / 'denial-failed.png'))
    raise RuntimeError('The native permission denial did not complete')


def check_capacity(page, agent, editor, output, timeout):
    """Exercise one workspace process, then ten, through the shipped client."""
    fixture = '''import json,os,subprocess,sys,time
from pathlib import Path
if sys.argv[2]=='all':
    children=[subprocess.Popen([sys.executable,__file__,sys.argv[1],str(i)]) for i in range(10)]
    sys.exit(any(child.wait() for child in children))
root=Path('/workspace/capacity')/sys.argv[1]
root.mkdir(parents=True,exist_ok=True)
name=sys.argv[2]
started=time.monotonic()
(root/(name+'.started')).write_text(json.dumps({'pid':os.getpid(),'started':started}))
while not (root/'release').exists():time.sleep(.05)
(root/(name+'.done')).write_text(json.dumps({'pid':os.getpid(),'started':started,'finished':time.monotonic()}))
print('capacity-job-'+name+'-complete')
'''
    podman('exec', '-i', editor, 'tee', '/workspace/capacity-job.py', payload=fixture)
    measurements = []
    for count in (1, 10):
        batch = secrets.token_hex(6)
        receipt = 'capacity-' + batch + '-complete'

        def release():
            deadline = time.monotonic() + timeout
            observed = 0
            while time.monotonic() < deadline:
                command = ('from pathlib import Path; '
                           f'p=Path("/workspace/capacity/{batch}"); '
                           'print(len(list(p.glob("*.started"))))')
                observed = int(subprocess.check_output(
                    ['podman', 'exec', editor, 'python3', '-c', command], text=True))
                if observed == count:
                    podman('exec', editor, 'touch', '/workspace/capacity/' + batch + '/release')
                    return
                time.sleep(.2)
            # Release any started fixtures before reporting an incomplete run.
            podman('exec', editor, 'mkdir', '-p', '/workspace/capacity/' + batch)
            podman('exec', editor, 'touch', '/workspace/capacity/' + batch + '/release')
            raise RuntimeError(f'Expected {count} concurrent native tools; observed {observed}')

        prompt = (f'Run {count} separate shell tool calls concurrently, one for each index from 0 to {count - 1}. '
                  f'Each command is: python3 /workspace/capacity-job.py {batch} INDEX . '
                  'Use independent native shell tool calls (or parallel Factory workspace shell calls). '
                  'Do not combine them in a shell loop or launch background jobs yourself. '
                  'The test releases the commands after they have all started. Wait for all to finish, '
                  f'then reply {receipt}.')
        if agent in ('claude', 'agy') and count > 1:
            # Native Claude serializes mutating MCP tools. Do not label that
            # scheduling policy a bridge defect or mark a shell read-only.
            # Exercise ten concurrent workspace processes in its normal tool.
            prompt = (f'Run python3 /workspace/capacity-job.py {batch} all using the Factory workspace shell. '
                      'This test command runs ten child jobs and waits for them. '
                      f'Wait for completion, then reply {receipt}.')
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(release)
            verify_reply(page, prompt, receipt, timeout, output)
            pending.result()
        records = json.loads(subprocess.check_output(['podman', 'exec', editor, 'python3', '-c',
            'import json;from pathlib import Path;'
            f'print(json.dumps([json.loads(p.read_text()) for p in Path("/workspace/capacity/{batch}").glob("*.done")]))'], text=True))
        assert len(records) == count, records
        assert len({item['pid'] for item in records}) == count, records
        assert max(item['started'] for item in records) < min(item['finished'] for item in records), records
        measurements.append({'concurrent_workspace_processes': count,
                             'native_tool_calls': 1 if agent in ('claude', 'agy') else count,
                             'completed_processes': len(records), 'records': records})
        print(f'Native browser completed {count} concurrent workspace processes.', flush=True)
    (output / 'capacity.json').write_text(json.dumps(measurements, indent=2))


def run(args, root, stack):
    agent = args.agent
    api_error = getattr(args, 'api_error', False)
    fixture = 'factory-editor-test-' + secrets.token_hex(6)
    options = {'options': {'native_approval': getattr(args, 'approval', 'automatic')}, 'primary_agent': agent, 'uid': os.getuid(), 'gid': os.getgid()}
    primary_agents.build({**options, 'engine': 'podman'})
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
    (trusted/'settings.json').write_text(json.dumps({'agent':agent,'browser':False,'working_directory':'/workspace', 'native_approval':getattr(args, 'approval', 'automatic')}))
    (trusted/'context.md').write_text('Perform file and shell operations only in the workspace with the configured remote tools.')
    from pi_sandbox_factory import browser_editor
    editor_path = browser_editor.INSTALL if agent == 'agy' else '/opt/editor'
    editor_mounts = ['-v', f'{args.code_server}:{editor_path}:ro', '-v', f'{client}:/qa:rw']
    controller_env = [arg for key, value in sandbox.RUNTIME_ENV.items() for arg in ('-e', f'{key}={value}')]
    if api_error:
        from primary_claude_errors import FIXTURE_CREDENTIAL, RECOVERY_REPLY
        controller_env += ['-e', 'ANTHROPIC_API_KEY=' + FIXTURE_CREDENTIAL,
                           '-e', 'ANTHROPIC_BASE_URL=http://127.0.0.1:18782',
                           '-e', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1']
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
    if agent == 'agy':
        store = ['secret-tool', 'store', '--label=Antigravity CLI', 'service', 'gemini', 'username', 'antigravity']
    else:
        target = {'codex': '.codex/auth.json', 'claude': '.claude/.credentials.json'}[agent]
        store = ['python3', '-c', advisor_credentials._WRITE_FILE, '/home/node/' + target]
    if api_error:
        podman('exec', '-i', fixture, 'tee', '/tmp/fixture-api.py',
               payload=(ROOT / 'tests/primary_claude_errors.py').read_text())
        podman('exec', '-d', fixture, 'python3', '/tmp/fixture-api.py', '--serve-browser-fixture')
    else:
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
    if args.background_controls:
        from native_codex_background import check
        check(editor, args.output, args.timeout)
        return
    native_editor.install({'engine':'podman','primary_agent':agent}, editor, 'x86_64')
    if agent == 'agy':
        state = root / 'state'
        state.mkdir()
        (state / 'config.json').write_text(json.dumps({'primary_agent': agent}))
        container_id = subprocess.check_output(['podman', 'inspect', '--format', '{{.Id}}', editor], text=True).strip()
        host_source = ("import asyncio,sys;from pathlib import Path;"
                       "from pi_sandbox_factory import browser_editor,editor_settings;"
                       "asyncio.run(browser_editor.serve(sys.argv[1],'/workspace',editor_settings.validate(),"
                       "Path(sys.argv[2]),lambda url:print(url,flush=True)))")
        supervisor = subprocess.Popen([sys.executable, '-u', '-c', host_source, container_id, str(state)],
            stdout=subprocess.PIPE, stderr=(args.output / 'editor.log').open('w'), text=True)
        stack.callback(supervisor.wait)
        stack.callback(supervisor.terminate)
        url = supervisor.stdout.readline().strip()
        if not url.startswith('http://'):
            raise RuntimeError('Production browser editor did not start: ' + (args.output / 'editor.log').read_text()[-4000:])
    else:
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
        url = f'http://127.0.0.1:{port}'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={'width': 1420, 'height': 920})
        page.factory_automatic = agent == 'agy' and args.approval == 'automatic'
        page.set_default_timeout(args.timeout * 1000)
        page.context.add_cookies([{'name': 'editor_test', 'value': ticket, 'domain': '127.0.0.1',
                                   'path': '/', 'httpOnly': True, 'sameSite': 'Strict'}])
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        trust_warnings = []
        page.expose_function('recordTrustWarning', lambda text: trust_warnings.append(text))
        page.add_init_script(r"""(() => {
            let recorded = false;
            const check = () => {
                const text = document.body?.innerText || '';
                if (!recorded && /couldn.t check whether this folder is trusted|Do you trust the files in this folder|Trust folder\?/i.test(text)) {
                    recorded = true;
                    window.recordTrustWarning(text);
                }
            };
            new MutationObserver(check).observe(document, {subtree:true, childList:true, characterData:true});
        })();""")
        if agent == 'agy':
            page.on('console', lambda message: (args.output / 'console.log').open('a').write(message.type + ': ' + message.text + '\n') if message.type in ('error', 'warning') else None)
        page.goto(url + '/?folder=/workspace')
        expect(page).to_have_title(re.compile('workspace', re.I), timeout=args.timeout * 1000)
        if api_error:
            verify_reply(page, 'Reply hello.', 'Claude could not complete the request (HTTP 400).', args.timeout, args.output)
            page.screenshot(path=str(args.output / 'filtered-error.png'))
            for frame in page.frames:
                assert FIXTURE_CREDENTIAL not in frame.locator('body').inner_text()
                assert FIXTURE_CREDENTIAL not in frame.content()
            podman('exec', fixture, 'touch', '/tmp/fixture-api-recover')
            verify_reply(page, 'Reply hello again.', RECOVERY_REPLY, args.timeout, args.output)
            page.screenshot(path=str(args.output / 'recovered.png'))
            (args.output / 'browser-errors.json').write_text(json.dumps(errors, indent=2))
            assert not errors, errors
            assert not trust_warnings, trust_warnings
            browser.close()
            print('Native error displayed without credentials; next turn recovered.', flush=True)
            return
        verify_reply(page, prompt, expected, args.timeout, args.output)
        if args.attachments:
            from native_agy_attachments import check as check_attachments
            check_attachments(page, args.output, args.timeout)
        if args.separate:
            second = 'second-' + secrets.token_hex(12)
            podman('exec','-i',editor,'tee','/workspace/second.txt',payload=second)
            verify_reply(page, 'Read second.txt and write its contents to final.txt, then reply with the contents.', second, args.timeout, args.output)
            final = subprocess.check_output(['podman','exec',editor,'cat','/workspace/final.txt'],text=True)
            assert final.strip() == second
            print('Two different workspace edits passed in one native chat.',flush=True)
        if args.deny:
            if agent != 'agy' and args.approval != 'manual':
                choose_manual_approvals(page, agent, args.output)
            check_denial(page, editor, args.output, args.timeout, agent)
        if args.isolation:
            check_codex_file_isolation(page, fixture, editor, args.output, args.timeout)
        if args.capacity:
            check_capacity(page, agent, editor, args.output, args.timeout)
        if args.reload:
            page.reload()
            if agent != 'agy':
                resume_chat(page, args.output, args.timeout, second)
            verify_reply(page, 'What were the contents of second.txt that you read earlier? Reply from this chat without using tools.', second, args.timeout, args.output)
            if args.attachments:
                assert any(f.locator('img[src^="data:image/"], img[src^="blob:"]').count()
                           for f in page.frames if f.parent_frame), 'Uploaded image missing after chat reload'
            print('Continued the saved chat after a page reload.', flush=True)
        if args.deny and args.reload:
            check_denial(page, editor, args.output, args.timeout, agent)
        if args.cancel:
            check_cancellation(page, agent, editor, args.output, args.timeout)
        page.screenshot(path=str(args.output / 'working.png'))
        assert 'Build with Agent' not in page.locator('body').inner_text()
        (args.output / 'browser-errors.json').write_text(json.dumps(errors, indent=2))
        assert not errors, errors
        (args.output / 'trust-warnings.json').write_text(json.dumps(trust_warnings, indent=2))
        assert not trust_warnings, trust_warnings
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
    parser.add_argument('--approval', choices=('automatic', 'manual'), default='automatic')
    parser.add_argument('--attachments', action='store_true')
    parser.add_argument('--output', type=Path, required=True, help='Directory for screenshots and browser diagnostics')
    parser.add_argument('--reload', action='store_true', help='Reload the editor and continue the same chat')
    parser.add_argument('--cancel', action='store_true', help='Stop a running workspace command and verify the next turn works')
    parser.add_argument('--deny', action='store_true', help='Deny a native tool request and verify it did not run')
    parser.add_argument('--api-error', action='store_true', help='Claude only: use a local fake API to test error filtering and recovery without host credentials')
    parser.add_argument('--isolation', action='store_true', help='Codex only: verify native image and patch tools use the separate workspace')
    parser.add_argument('--capacity', action='store_true', help='Exercise one then ten concurrent workspace processes through the native panel')
    parser.add_argument('--background-controls', action='store_true', help='Codex only: verify native background-terminal list, terminate and clean with real workspace jobs')
    parser.add_argument('--timeout', type=int, default=180, help='Seconds to wait for the live reply')
    args = parser.parse_args()
    if args.api_error and args.agent != 'claude':
        parser.error('--api-error requires claude')
    if args.isolation and args.agent != 'codex':
        parser.error('--isolation requires codex')
    if args.background_controls and args.agent != 'codex':
        parser.error('--background-controls requires codex')
    args.code_server = args.code_server.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='factory-ide-') as directory, ExitStack() as cleanup:
        run(args, Path(directory), cleanup)
    print(f'{args.agent}: {"synthetic error and recovery" if args.api_error else "authenticated reply"} verified; disposable resources removed.')
