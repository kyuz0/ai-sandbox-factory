#!/usr/bin/env python3
"""Authenticated native Claude CLI checks in disposable companion/workspace containers.

Uses the existing host Claude subscription sign-in. Never changes a saved
sandbox or workstation sign-in. Requires the current production images.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import tempfile
import time
import zlib

from ai_sandbox_factory import advisor_credentials, primary_agents, sandbox


def run(*args, payload=None, capture=False):
    return subprocess.run(['podman', *args], input=payload, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.PIPE, check=True)


def png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    pixels = b''.join(b'\0' + b'\x00\xff\x00' * 64 for _ in range(64))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 64, 64, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def turn(container, prompt, timeout, resume=None, *, effort=None):
    args = ['exec', '-i', '-w', '/workspace', container, '/usr/local/bin/claude',
            '--print', '--verbose', '--output-format', 'stream-json']
    if resume:
        args += ['--resume', resume]
    if effort:
        args += ['--effort', effort]
    result = subprocess.run(['podman', *args, prompt], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]
    terminal = next((event for event in reversed(events) if event.get('type') == 'result'), {})
    if result.returncode or terminal.get('is_error'):
        # Native output contains placeholders; never print the private store.
        raise RuntimeError(f'Claude native turn failed with exit {result.returncode}: '
                           f'{terminal.get("result", result.stderr.decode(errors="replace"))}')
    assert terminal, 'Native CLI returned no terminal result'
    return events, terminal


def exercise(args, root):
    identifier = 'factory-claude-cli-' + secrets.token_hex(5)
    controller, workspace, proxy = identifier, identifier + '-workspace', identifier + '-proxy'
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    private = root / 'private'
    private.mkdir()
    settings = root / 'settings'
    settings.mkdir()
    config = {'agent': 'claude', 'browser': False, 'native_approval': 'automatic',
              'working_directory': '/workspace', 'write_roots': ['/workspace'], 'network_mode': 'allowlist',
              'claude_web_search': args.web}
    (settings / 'settings.json').write_text(json.dumps(config))
    (settings / 'context.md').write_text('Work in /workspace using your native shell, file, image and web tools.')
    rules = root / 'squid.conf'
    rules.write_text(sandbox.squid_config(primary_agents.network_rules('claude')))
    egress = identifier + '-egress'
    with ExitStack() as stack:
        run('volume', 'create', egress)
        stack.callback(run, 'volume', 'rm', egress)
        def start(name, *command):
            run('run', '-d', '--pull=never', '--name', name, *command)
            stack.callback(run, 'rm', '-f', '-v', name)
        start(proxy, '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
              '--cap-add=DAC_OVERRIDE', '-v', f'{rules}:/etc/squid/sandbox.conf:ro,z',
              '-v', f'{egress}:/run/egress:z', 'localhost/pi-sandbox-factory-proxy:1')
        isolated = ['--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                    '--security-opt=label=disable', f'--userns=keep-id:uid={os.getuid()},gid={os.getgid()}',
                    '--tmpfs', '/home/node:rw,mode=1777', '--tmpfs', '/workspace:rw,mode=1777']
        env = [arg for key, value in sandbox.RUNTIME_ENV.items() for arg in ('-e', f'{key}={value}')]
        start(controller, *isolated, *env, '-e', 'FACTORY_PRIMARY_AGENT=claude',
              '-v', f'{egress}:/run/primary-egress:ro', '-v', f'{egress}:/run/workspace-egress:ro',
              '-v', f'{private}:/run/primary-editor', '-v', f'{settings}:/run/primary-input:ro', args.controller_image)
        deadline = time.monotonic() + args.timeout
        while not (private / 'claude-ca.pem').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Claude credential companion did not publish its public CA')
            time.sleep(.1)
        credential = advisor_credentials.read_host('claude').encode()
        values = json.loads(credential)['claudeAiOauth']
        private_values = [values[key].encode() for key in ('accessToken', 'refreshToken') if values.get(key)]
        run('exec', '-i', controller, 'python3', '-c', advisor_credentials._WRITE_FILE,
            '/home/node/.claude/.credentials.json', payload=credential)
        start(workspace, *isolated, '-v', f'{private}:/run/primary-editor:ro',
              '--tmpfs', '/run/primary-runtime:rw,mode=1777', '--entrypoint', '/usr/bin/tini',
              args.workspace_image, '--', 'sleep', 'infinity')
        for name, content in [('settings.json', json.dumps(config).encode()),
                              ('context.md', (settings / 'context.md').read_bytes())]:
            run('exec', '-i', workspace, 'tee', '/run/primary-runtime/' + name, payload=content)
        run('exec', '-d', workspace, 'python3', '/opt/primary/entrypoint.py')
        while True:
            ready = subprocess.run(['podman', 'exec', workspace, 'bash', '-c', 'true </dev/tcp/127.0.0.1/3130'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('Claude workspace authentication relay did not start')
            time.sleep(.1)
        run('exec', workspace, 'python3', '/opt/primary/claude_client.py', '--factory-prepare')
        trust = json.loads(run('exec', '-w', '/workspace', workspace, '/usr/local/bin/claude',
                               '--setting-sources', 'user', 'workspace-trust', '--json', capture=True).stdout)
        assert trust.get('trusted') is True, f'Native workspace trust is missing: {trust}'
        run('exec', workspace, 'git', 'init', '-q', '/workspace')
        starting = '/workspace/src café'
        run('exec', workspace, 'mkdir', '-p', starting)
        podman_settings = {**config, 'working_directory': starting}
        run('exec', '-i', workspace, 'tee', '/run/primary-runtime/settings.json',
            payload=json.dumps(podman_settings).encode())
        run('exec', workspace, 'python3', '/opt/primary/claude_client.py', '--factory-prepare')
        trust = json.loads(run('exec', '-w', starting, workspace, '/usr/local/bin/claude',
                               '--setting-sources', 'user', 'workspace-trust', '--json', capture=True).stdout)
        assert trust.get('trusted') is True, f'Native Git project subfolder trust is missing: {trust}'
        run('exec', '-i', workspace, 'tee', '/run/primary-runtime/settings.json', payload=json.dumps(config).encode())
        run('exec', workspace, 'python3', '/opt/primary/claude_client.py', '--factory-prepare')
        for path in ('/run/primary-egress/proxy.sock', '/run/primary-bus/bus', '/run/podman/podman.sock'):
            run('exec', workspace, 'test', '!', '-e', path)
        masked = run('exec', workspace, 'cat', '/home/node/.claude/.credentials.json', capture=True).stdout
        assert all(secret not in masked for secret in private_values), 'Real account token reached workspace credentials'
        run('exec', '-i', workspace, 'tee', '/workspace/feature-image.png', payload=png())
        word = secrets.token_hex(8)
        run('exec', '-i', workspace, 'tee', '/workspace/feature.txt', payload=(word + '\nbefore\n').encode())
        prompt = ('Use your native Read tool to read /workspace/feature.txt. Use your native Edit tool to change '
                  'the second line from before to after. Use your native Bash tool to verify the file. '
                  'Use Read to inspect /workspace/feature-image.png and identify its solid color. '
                  'Remember the first line for the next turn. Reply with the first line and the color.')
        events, terminal = turn(workspace, prompt, args.timeout, effort='medium')
        text = json.dumps(events)
        assert word in terminal['result'], 'Native file contents were missing from the response'
        assert 'green' in terminal['result'].lower(), 'Native image was not identified as green'
        actual = run('exec', workspace, 'cat', '/workspace/feature.txt', capture=True).stdout
        assert actual == (word + '\nafter\n').encode(), 'Native edit did not update workspace file'
        invoked = {block.get('name') for event in events for block in event.get('message', {}).get('content', [])
                   if isinstance(block, dict) and block.get('type') == 'tool_use'}
        assert {'Read', 'Edit', 'Bash'}.issubset(invoked), f'Expected native tools, got {invoked}'
        assert all(secret not in text.encode() for secret in private_values), 'Native events exposed a real token'
        (output / 'file-image.json').write_text(text)
        print('Native CLI: Read, Edit, Bash and image understanding passed.', flush=True)
        if args.compact:
            compacted, compact_result = turn(workspace, '/compact Preserve the first line and the image color.',
                                             args.timeout, resume=terminal['session_id'])
            boundaries = [event for event in compacted if event.get('type') == 'system' and event.get('subtype') == 'compact_boundary']
            assert boundaries, 'Native /compact returned no compaction boundary'
            (output / 'compact.json').write_text(json.dumps(compacted))
            print('Native CLI: manual compaction passed.', flush=True)
        resumed, result = turn(workspace, 'Without using tools, recall the first line and the image color from our previous turn.',
                               args.timeout, resume=terminal['session_id'])
        assert word in result['result'] and 'green' in result['result'].lower(), 'Native saved-session reload lost the previous turn'
        (output / 'resume.json').write_text(json.dumps(resumed))
        print('Native CLI: saved-session resume passed.', flush=True)
        if args.web:
            web, result = turn(workspace, 'Use your native WebSearch tool to find the official Claude Code documentation. '
                               'Reply with its documentation hostname.', args.timeout)
            invoked = {block.get('name') for event in web for block in event.get('message', {}).get('content', [])
                       if isinstance(block, dict) and block.get('type') == 'tool_use'}
            assert 'WebSearch' in invoked, f'Native WebSearch was not invoked: {invoked}'
            assert 'claude' in result['result'].lower(), 'Native web search returned no Claude documentation'
            (output / 'web.json').write_text(json.dumps(web))
            print('Native CLI: WebSearch passed.', flush=True)
        # Read only the workspace process/files surface, never the companion's
        # private store. Real-token comparisons happen solely in this host test.
        scan = run('exec', workspace, 'python3', '-c',
            'from pathlib import Path;import sys;'
            'paths=list(Path("/home/node").rglob("*"))+list(Path("/proc").glob("[0-9]*/environ"))+list(Path("/proc").glob("[0-9]*/cmdline"));'
            '\nfor p in paths:\n try:\n  if p.is_file(): sys.stdout.buffer.write(p.read_bytes())\n except (OSError,PermissionError): pass', capture=True).stdout
        assert all(secret not in scan for secret in private_values), 'Real subscription token reached workspace files or process environment'
        print('Native CLI: workspace files and process environments contain no real subscription token.', flush=True)
        run('exec', workspace, '/usr/local/bin/claude', 'auth', 'logout')
        run('exec', controller, 'test', '!', '-e', '/home/node/.claude/.credentials.json')
        status_process = subprocess.run(['podman', 'exec', workspace, '/usr/local/bin/claude',
            'auth', 'status', '--json'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        status = json.loads(status_process.stdout)
        assert status.get('loggedIn') is False, 'Native auth status silently signed back in after logout'
        print('Native CLI: logout cleared only the sandbox credential copy and stayed signed out.', flush=True)
        (output / 'result.json').write_text(json.dumps({'native_tools': ['Read', 'Edit', 'Bash'],
            'image': True, 'resume': True, 'web': args.web, 'compact': args.compact,
            'credential_isolation': True, 'logout': True}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-image', required=True)
    parser.add_argument('--workspace-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--web', action='store_true')
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='native-claude-features-') as temporary:
        exercise(args, Path(temporary))


if __name__ == '__main__':
    main()
