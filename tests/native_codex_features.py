#!/usr/bin/env python3
"""Exercise native Codex features in an existing disposable Factory sandbox.

Run with --workspace-container NAME --credential-container NAME. The supplied
containers must be disposable: this creates a fixture file and isolated Codex home.
Browser checks require the sandbox's managed browser viewer to be running.
"""
import argparse
import hashlib
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path


def sanitized(value, private_values=()):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for secret in private_values:
        if secret:
            text = text.replace(secret, '[redacted credential]')
    text = re.sub(r'(?i)(Bearer\s+)[^\s"\x27,}]+', r'\1[redacted credential]', text)
    return re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted JWT]', text)


class AppServer:
    def __init__(self, container, home, private_values=()):
        self.container = container
        self.private_values = private_values
        self.stderr = []
        self.operation = 'app-server startup'
        self.process = subprocess.Popen(
            ['podman', 'exec', '-i', '-e', f'CODEX_HOME={home}', container,
             '/usr/local/bin/codex', 'app-server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.messages = queue.Queue()
        self.events = []
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(sanitized(line, self.private_values))

    def failure(self, reason):
        detail = (f'Codex {self.operation} in container {self.container}: '
                  f'{sanitized(reason, self.private_values)}; process exit status {self.process.poll()}')
        if self.stderr:
            detail += '\nApp-server stderr:\n' + ''.join(self.stderr)
        return detail

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except Exception as error:
            self.messages.put(error)
        finally:
            self.messages.put(None)

    def receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(self.failure('operation exceeded the configured timeout'))
        try:
            value = self.messages.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError(self.failure('no completion before the configured timeout')) from error
        if value is None:
            self.stderr_reader.join(timeout=5)
            raise RuntimeError(self.failure('app-server closed its output before completion'))
        if isinstance(value, Exception):
            raise RuntimeError(self.failure(f'could not parse app-server output: {type(value).__name__}: {value}')) from None
        self.events.append(value)
        if 'method' in value and 'id' in value:
            raise RuntimeError(self.failure({'unexpected_interactive_server_request': value}))
        return value

    def call(self, method, params, deadline):
        self.sequence += 1
        request_id = self.sequence
        self.operation = f'RPC {method}, request {request_id}'
        try:
            self.process.stdin.write(json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise RuntimeError(self.failure(f'could not send request: {type(error).__name__}: {error}')) from None
        while True:
            value = self.receive(deadline)
            if value.get('id') == request_id:
                if 'error' in value:
                    raise RuntimeError(self.failure({'rpc_error': value['error']}))
                return value['result']

    def wait_turn(self, thread_id, turn_id, deadline):
        self.operation = f'turn {turn_id} in thread {thread_id}'
        while True:
            value = self.receive(deadline)
            params = value.get('params', {})
            if (value.get('method') == 'turn/completed'
                    and params.get('threadId') == thread_id
                    and params['turn']['id'] == turn_id):
                if params['turn']['status'] != 'completed':
                    raise RuntimeError(self.failure({'turn': params['turn']}))
                return

    def turn(self, thread_id, prompt, timeout):
        deadline = time.monotonic() + timeout
        start = len(self.events)
        result = self.call('turn/start', {
            'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}],
            'effort': 'low',
        }, deadline)
        self.wait_turn(thread_id, result['turn']['id'], deadline)
        return [event['params']['item'] for event in self.events[start:]
                if event.get('method') == 'item/completed'
                and event['params'].get('threadId') == thread_id]

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=5)
        self.stderr_reader.join(timeout=5)


def podman(container, *command):
    return subprocess.check_output(['podman', 'exec', container, *command])


def assert_isolated(args):
    private = podman(args.credential_container, 'cat', '/home/node/.codex/auth.json')
    auth = json.loads(private)
    secrets = [value.encode() for key, value in auth.get('tokens', {}).items()
               if key in ('access_token', 'id_token', 'refresh_token')
               and isinstance(value, str) and value]
    assert secrets, 'Credential companion has no reference tokens for isolation comparison'
    for home in ('/home/node/.codex', args.codex_home):
        visible = podman(args.workspace_container, 'cat', home + '/auth.json')
        assert not any(secret in visible for secret in secrets), 'Real credential exposed in workspace auth file'
    script = (
        'import os,pathlib,json; '
        'assert not pathlib.Path("/run/primary-input/settings.json").exists(); '
        'assert not pathlib.Path("/home/node/.codex-proxy/mitmproxy-ca.pem").exists(); '
        'print(json.dumps(dict(os.environ))); '
        'print(pathlib.Path("/proc/self/environ").read_bytes())'
    )
    visible = podman(args.workspace_container, 'python3', '-c', script)
    assert not any(secret in visible for secret in secrets), 'Real credential exposed in workspace process environment'
    after = podman(args.credential_container, 'cat', '/home/node/.codex/auth.json')
    assert hashlib.sha256(private).digest() == hashlib.sha256(after).digest(), 'Isolation check changed private credentials'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace-container', required=True)
    parser.add_argument('--credential-container', required=True)
    parser.add_argument('--codex-home', default='/home/node/.codex-native-features-test')
    parser.add_argument('--timeout', type=float, default=600, help='Seconds allowed per native operation')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--output', type=Path, help='Write token-free check results as JSON')
    args = parser.parse_args()
    assert args.codex_home.startswith('/home/node/.codex-'), 'Use an isolated /home/node/.codex-* fixture home'
    results = {}
    # Reference credentials stay in this host test process solely for redaction
    # and isolation comparisons. They are never passed to the workspace.
    auth = json.loads(podman(args.credential_container, 'cat', '/home/node/.codex/auth.json'))
    private_values = [value for key, value in auth.get('tokens', {}).items()
                      if key in ('access_token', 'id_token', 'refresh_token') and isinstance(value, str)]
    server = AppServer(args.workspace_container, args.codex_home, private_values)
    try:
        deadline = time.monotonic() + args.timeout
        server.call('initialize', {'clientInfo': {'name': 'factory-native-feature-test', 'version': '1'},
                                  'capabilities': {'experimentalApi': True}}, deadline)
        thread = server.call('thread/start', {'cwd': '/workspace', 'config': {'web_search': 'live'}}, deadline)
        thread_id = thread['thread']['id']
        items = server.turn(thread_id,
            'Remember FEATURE-COMPACT-742. Use native shell to write NATIVE-FILE-742 to '
            '/workspace/codex-native-feature.txt and read it back. Reply briefly.', args.timeout)
        commands = [item for item in items if item['type'] == 'commandExecution']
        assert commands and all(item.get('exitCode') == 0 for item in commands), 'Native file command failed or was not invoked'
        assert podman(args.workspace_container, 'cat', '/workspace/codex-native-feature.txt').strip() == b'NATIVE-FILE-742'
        results['native_file_tool'] = 'passed'
        items = server.turn(thread_id,
            'Use native web search now to find the official OpenAI Codex CLI documentation. '
            'Do not use shell or browser. Return one official URL.', args.timeout)
        assert any(item['type'] == 'webSearch' for item in items), 'No native web search item completed'
        results['native_web_search'] = 'passed'
        if args.browser:
            items = server.turn(thread_id,
                'Use factory_workspace browser MCP to load a data URL containing '
                '<title>Factory Native Browser</title><body>BROWSER-CONTENT-742</body>. '
                'Read its title and body through the browser tool. Reply briefly. Do not use shell.', args.timeout)
            calls = [item for item in items if item['type'] == 'mcpToolCall'
                     and item.get('server') == 'factory_workspace' and item.get('tool') == 'browser']
            assert calls, 'No native browser MCP call completed'
            for item in calls:
                assert item.get('status') == 'completed' and not item.get('error'), 'Native browser MCP call failed'
                content = item.get('result', {}).get('content', [])
                texts = [part.get('text', '') for part in content if part.get('type') == 'text']
                for text in texts:
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        assert payload.get('ok') is not False, 'Managed browser reported an operation error'
            observed = json.dumps([item.get('result') for item in calls])
            assert 'BROWSER-CONTENT-742' in observed and 'Factory Native Browser' in observed, 'Browser result did not contain fixture content'
            results['native_browser'] = 'passed'
        start = len(server.events)
        deadline = time.monotonic() + args.timeout
        server.call('thread/compact/start', {'threadId': thread_id}, deadline)
        while not any(event.get('method') == 'item/completed'
                      and event['params'].get('threadId') == thread_id
                      and event['params']['item']['type'] == 'contextCompaction'
                      for event in server.events[start:]):
            server.receive(deadline)
        compact_event = next(event for event in server.events[start:]
                             if event.get('method') == 'item/completed'
                             and event['params'].get('threadId') == thread_id
                             and event['params']['item']['type'] == 'contextCompaction')
        server.wait_turn(thread_id, compact_event['params']['turnId'], deadline)
        items = server.turn(thread_id,
            'What exact FEATURE-COMPACT marker did I ask you to remember? '
            'Also use native shell to read /workspace/codex-native-feature.txt. Return both markers.', args.timeout)
        reply = '\n'.join(item.get('text', '') for item in items if item['type'] == 'agentMessage')
        assert 'FEATURE-COMPACT-742' in reply and 'NATIVE-FILE-742' in reply
        assert any(item['type'] == 'commandExecution' and item.get('exitCode') == 0 for item in items)
        results['native_compaction_continuation'] = 'passed'
    finally:
        server.close()
    cli = subprocess.run(['podman', 'exec', '-e', f'CODEX_HOME={args.codex_home}',
                          args.workspace_container, '/usr/local/bin/codex', 'exec', '--json',
                          '-C', '/workspace', '--skip-git-repo-check',
                          'Use native shell to read /workspace/codex-native-feature.txt. Reply with its contents only.'],
                         capture_output=True, text=True, timeout=args.timeout)
    assert cli.returncode == 0, (f'Native CLI in container {args.workspace_container} exited with status {cli.returncode}:\n'
                                + sanitized(cli.stderr + '\n' + cli.stdout, private_values))
    events = [json.loads(line) for line in cli.stdout.splitlines() if line.strip()]
    assert any(event.get('type') == 'item.completed'
               and event['item'].get('type') == 'command_execution'
               and event['item'].get('exit_code') == 0
               and 'NATIVE-FILE-742' in event['item'].get('aggregated_output', '') for event in events)
    results['native_cli'] = 'passed'
    assert_isolated(args)
    results['credential_isolation'] = 'passed'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
