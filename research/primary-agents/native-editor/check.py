"""Opt-in Linux integration check. Run with PYTHONPATH=. from the repository root."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.request import urlopen
from urllib.error import HTTPError

from playwright.sync_api import Error as BrowserError, sync_playwright
from pi_sandbox_factory import advisor_credentials, primary_agents, primary_workspace, sandbox
from fixed_turn import FILE_TASK

HERE = Path(__file__).resolve().parent
PANELS = {'codex': ('openai.chatgpt', 'chatgpt.openSidebar'),
          'claude': ('anthropic.claude-code', 'claude-vscode.editor.open'),
          'agy': ('google.google-antigravity', 'workbench.view.extension.antigravity-sidebar')}


def podman(*args, payload=None):
    subprocess.run(['podman', *args], check=True, input=payload, text=True, stdout=subprocess.DEVNULL)


def prepare_extensions(directory, agent, extension):
    manifest = json.loads((extension / 'package.json').read_text())
    identifier = manifest['publisher'] + '.' + manifest['name']
    expected, command = PANELS[agent]
    if identifier.lower() != expected:
        raise ValueError(f'Expected {expected}, got {identifier}')
    registered = directory / 'extensions'
    registered.mkdir()
    installed = identifier + '-' + manifest['version']
    (registered / installed).symlink_to('/opt/vendor')
    opener = registered / 'factory.opener-1.0.0'
    opener.mkdir()
    opener_manifest = {'name': 'opener', 'publisher': 'factory', 'version': '1.0.0',
                       'engines': {'vscode': '^1.90.0'}, 'activationEvents': ['onStartupFinished'],
                       'main': './index.cjs'}
    (opener / 'package.json').write_text(json.dumps(opener_manifest))
    (opener / 'index.cjs').write_text(
        "const vscode = require('vscode'); exports.activate = async () => {"
        f"await vscode.extensions.getExtension({json.dumps(identifier)}).activate();"
        f"await vscode.commands.executeCommand({json.dumps(command)}); }};")
    index = []
    for name, item in [(installed, manifest), (opener.name, opener_manifest)]:
        path = '/qa/extensions/' + name
        index.append({'identifier': {'id': item['publisher'] + '.' + item['name']},
                      'version': item['version'], 'relativeLocation': name,
                      'location': {'$mid': 1, 'scheme': 'file', 'path': path, 'fsPath': path}})
    (registered / 'extensions.json').write_text(json.dumps(index))


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
                    if expected in visible:
                        return
                    continue
                inputs = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible, '
                                       'input[type=text]:visible, [role=textbox]:visible')
                if inputs.count():
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
    raise RuntimeError(f'Expected reply missing. Prompt submitted: {submitted}. Panel notices: {sorted(observations)}')


def run(args, root, stack):
    agent = args.agent
    fixture = 'factory-editor-test-' + secrets.token_hex(6)
    options = {'primary_agent': agent, 'uid': os.getuid(), 'gid': os.getgid()}
    editor = fixture + '-editor' if args.separate else fixture
    client = root / 'client'
    client.mkdir()
    private = root / 'session'
    private.mkdir()
    prepare_extensions(client, agent, args.extension)
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
    editor_mounts = ['-v', f'{args.code_server}:/opt/editor:ro', '-v', f'{args.extension}:/opt/vendor:ro',
                     '-v', f'{client}:/qa:rw']
    controller_env = [arg for key, value in sandbox.RUNTIME_ENV.items() for arg in ('-e', f'{key}={value}')]
    start(fixture, *isolated, *controller_env, '-e', 'FACTORY_PRIMARY_AGENT=' + agent,
          '-v', egress + ':/run/primary-egress:ro', '-v', wire + ':/run/primary-workspace:ro,z',
          '-v', f'{private}:/session', '-v', f'{HERE}/fixed_turn.py:/opt/test-turn.py:ro',
          *(editor_mounts if not args.separate else []), primary_agents.image(options))
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
    podman('exec', '-i', fixture, *store, payload=advisor_credentials.read_host(agent))

    settings = {'claudeCode.disableLoginPrompt': True} if agent == 'claude' else {}
    ticket = secrets.token_urlsafe(32)
    hub = None
    first = secrets.choice(('cedar', 'maple', 'aspen'))
    last = secrets.choice(('violet', 'amber', 'coral'))
    prompt = (f'Your first word is {first}. Your second word is {last}. '
              'Reply with these two words joined by a hyphen. Do not use tools.')
    expected = first + '-' + last
    if args.separate:
        start(editor, *isolated, *editor_mounts, '-v', f'{private}:/session:ro', '-v', wire + ':/run/primary-workspace:z',
              '--tmpfs', '/run/primary-runtime:rw,mode=1777', '--entrypoint', '/usr/bin/tini',
              primary_workspace.image(options), '--', 'sleep', 'infinity')
        for path in ('/home/node/.codex/auth.json', '/home/node/.claude/.credentials.json',
                     '/run/primary-egress/proxy.sock', '/run/primary-bus/bus', '/run/podman/podman.sock'):
            podman('exec', editor, 'test', '!', '-e', path)
        podman('exec', '-i', editor, 'tee', '/run/primary-runtime/cwd.json', payload=json.dumps('/workspace'))
        prompt, expected = FILE_TASK, 'proof-' + secrets.token_hex(16)
        podman('exec', '-i', editor, 'tee', '/workspace/input.txt', payload=expected)
        podman('exec', '-d', editor, 'bash', '/opt/primary/worker.sh', agent)
        podman('exec', '-d', fixture, 'socat', 'UNIX-LISTEN:/session/rpc.sock,fork,mode=0600',
               'EXEC:python3 /opt/test-turn.py ' + agent)
        wrapper = client / 'native-cli'
        wrapper.write_text("#!/bin/sh\ncase \" $* \" in\n"
                           "  *' --version '*) echo 1.0.0; exit 0 ;;\n"
                           "  *' auth status '*) exit 1 ;;\n"
                           "esac\nexec socat STDIO UNIX-CONNECT:/session/rpc.sock\n")
        wrapper.chmod(0o755)
        key = 'chatgpt.cliExecutable' if agent == 'codex' else 'claudeCode.claudeProcessWrapper'
        settings[key] = '/qa/native-cli'
    if agent == 'agy':
        podman('exec', fixture, 'mkdir', '-p', '/home/node/.gemini/bin')
        podman('exec', fixture, 'ln', '-s', '/usr/local/bin/factory-agent', '/home/node/.gemini/bin/agy')
        hub = listen(stack, client / 'hub.sock', ticket)
        settings['antigravity.serverPort'] = hub
        podman('exec', '-d', editor, 'socat', 'UNIX-LISTEN:/qa/hub.sock,mode=0600,fork',
               'TCP4:127.0.0.1:' + str(hub))
    podman('exec', editor, 'mkdir', '-p', '/tmp/editor-data/User', '/tmp/editor-data/Machine')
    for location in ('User', 'Machine'):
        podman('exec', '-i', editor, 'tee', f'/tmp/editor-data/{location}/settings.json', payload=json.dumps(settings))
    podman('exec', '-d', editor, '/opt/editor/bin/code-server', '--socket', '/qa/editor.sock', '--auth', 'none',
           '--disable-file-uploads', '--disable-update-check', '--disable-telemetry', '--disable-workspace-trust',
           '--user-data-dir', '/tmp/editor-data', '--extensions-dir', '/qa/extensions', '/workspace')
    for _ in range(150):
        if (client / 'editor.sock').is_socket():
            break
        time.sleep(.2)
    port = listen(stack, client / 'editor.sock', ticket, hub)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={'width': 1420, 'height': 920})
        page.context.add_cookies([{'name': 'editor_test', 'value': ticket, 'domain': '127.0.0.1',
                                   'path': '/', 'httpOnly': True, 'sameSite': 'Strict'}])
        page.goto(f'http://127.0.0.1:{port}/?folder=/workspace')
        verify_reply(page, prompt, expected, args.timeout)
        browser.close()
    if args.separate:
        output = subprocess.check_output(['podman', 'exec', editor, 'cat', '/workspace/output.txt'], text=True)
        if output.strip() != expected:
            raise RuntimeError('The output file did not contain the workspace-only value')
        podman('exec', fixture, 'test', '!', '-e', '/workspace/output.txt')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('agent', choices=PANELS)
    parser.add_argument('--extension', required=True, type=Path, help='Extracted official Linux extension directory')
    parser.add_argument('--code-server', required=True, type=Path, help='Extracted code-server directory')
    parser.add_argument('--chromium', required=True, help='Local Chromium executable')
    parser.add_argument('--separate', action='store_true', help='Test an offline workspace with the fixed file task')
    parser.add_argument('--timeout', type=int, default=180, help='Seconds to wait for the live reply')
    args = parser.parse_args()
    if args.separate and args.agent == 'agy':
        parser.error('The Antigravity file test requires a way to connect the extension to a separate hub.')
    args.extension = args.extension.resolve(strict=True)
    args.code_server = args.code_server.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='factory-ide-') as directory, ExitStack() as cleanup:
        run(args, Path(directory), cleanup)
    print(f'{args.agent}: authenticated reply verified; disposable resources removed.')
