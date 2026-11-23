#!/usr/bin/env python3
"""Exercise official Claude subscription login against a local OAuth fixture.

No account, external network or real credential is used. The fixture issues
synthetic tokens and the production proxy must mask them before Claude writes
its native credential file. Requires the companion's pinned mitmproxy runtime.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from mitmproxy import http, options
from mitmproxy.tools.dump import DumpMaster
from claude_auth import AuthStore
from claude_auth_proxy import OAUTH_ENDPOINTS, SubscriptionProxy
from claude_client import configure, instructions

ACCESS = 'fixture-private-new-subscription-access'
REFRESH = 'fixture-private-new-subscription-refresh'
ACCOUNT = {'uuid': 'ba82eb0d-40ae-4998-8b8b-054d10c80f3e', 'email_address': 'fixture@example.invalid',
           'display_name': 'Fixture', 'full_name': 'Fixture User', 'has_claude_max': True}
ORGANIZATION = {'uuid': '309fc5a6-c498-43b2-be02-1ac2c8e263a8', 'name': 'Fixture',
                'organization_type': 'claude_max', 'rate_limit_tier': 'default_claude_max_5x'}


class FixtureProxy(SubscriptionProxy):
    def __init__(self, store):
        super().__init__(store)
        self.paths = []
        self.search_requested = False
        self.search_denied = False
        self.search_results = []

    def route(self, flow):
        pass

    async def request(self, flow):
        await super().request(flow)
        if flow.response:
            return
        endpoint = (flow.request.host, flow.request.path.split('?', 1)[0])
        self.paths.append(endpoint)
        if endpoint in OAUTH_ENDPOINTS:
            payload = {'access_token': ACCESS, 'refresh_token': REFRESH, 'token_type': 'Bearer',
                       'expires_in': 3600, 'scope': 'user:inference user:profile user:sessions:claude_code',
                       'account': ACCOUNT, 'organization': ORGANIZATION}
        elif endpoint[1] == '/api/oauth/profile':
            assert flow.request.headers.get('authorization') == 'Bearer ' + ACCESS
            payload = {'account': ACCOUNT, 'organization': ORGANIZATION}
        elif endpoint[1] == '/api/oauth/claude_cli/roles':
            payload = {'organization_role': 'admin', 'workspace_role': 'admin',
                       'organization_name': 'Fixture'}
        elif endpoint[1] == '/v1/messages':
            request = json.loads(flow.request.content)
            for message in request.get('messages', []):
                for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
                    if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') == 'tool_fixture_search':
                        self.search_results.append(block)
                        self.search_denied = block.get('is_error') is True
            force_search = 'fixture-force-search' in json.dumps(request) and not self.search_requested
            if force_search:
                self.search_requested = True
            content = [
                ('message_start', {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message',
                  'role': 'assistant', 'model': 'claude-sonnet-4-6', 'content': [], 'stop_reason': None,
                  'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                  'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                  'delta': {'type': 'text_delta', 'text': 'fixture-native-success'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn',
                  'stop_sequence': None}, 'usage': {'output_tokens': 1}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            if force_search:
                content[1] = ('content_block_start', {'type': 'content_block_start', 'index': 0,
                    'content_block': {'type': 'tool_use', 'id': 'tool_fixture_search', 'name': 'WebSearch',
                                      'input': {'query': 'Claude Code official documentation'}}})
                content.pop(2)
                content[-2][1]['delta']['stop_reason'] = 'tool_use'
            body = ''.join(f'event: {kind}\ndata: {json.dumps(data)}\n\n' for kind, data in content)
            flow.response = http.Response.make(200, body, {'content-type': 'text/event-stream'})
            return
        else:
            payload = {}
        flow.response = http.Response.make(200, json.dumps(payload), {'content-type': 'application/json'})


async def exercise(binary, root, timeout):
    companion = root / 'companion'
    workspace = root / 'workspace'
    companion.mkdir()
    workspace.mkdir()
    store = AuthStore(companion)
    proxy = FixtureProxy(store)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    opts = options.Options(listen_host='127.0.0.1', listen_port=port, confdir=str(root / 'ca'))
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.options.update(connection_strategy='lazy', upstream_cert=False)
    master.addons.add(proxy)
    running = asyncio.create_task(master.run())
    ca = root / 'ca/mitmproxy-ca-cert.pem'
    try:
        while not ca.exists():
            await asyncio.sleep(.05)
        env = {**os.environ, 'HOME': str(workspace), 'CLAUDE_CONFIG_DIR': str(workspace / '.claude'),
               'HTTPS_PROXY': f'http://127.0.0.1:{port}', 'HTTP_PROXY': f'http://127.0.0.1:{port}',
               'NODE_EXTRA_CA_CERTS': str(ca), 'SSL_CERT_FILE': str(ca), 'BROWSER': '/bin/true',
               'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1', 'DISABLE_AUTOUPDATER': '1'}
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                    'ANTHROPIC_BASE_URL', 'NO_PROXY', 'no_proxy'):
            env.pop(key, None)
        process = await asyncio.create_subprocess_exec(binary, 'auth', 'login', '--claudeai',
            env=env, cwd=workspace, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        transcript = b''
        sent = False
        async with asyncio.timeout(timeout):
            while chunk := await process.stdout.read(4096):
                transcript += chunk
                (root / 'native-login-output.txt').write_bytes(transcript)
                match = re.search(rb'https://[^\s\x1b]+[?&]state=([^&\s\x1b]+)', transcript)
                short = re.search(rb'https://claude\.ai/oauth/authorize\?([^\s\x1b]+)', transcript)
                if match and not sent:
                    from urllib.parse import unquote
                    state = unquote(match.group(1).decode())
                    process.stdin.write(('fixture-code#' + state + '\n').encode())
                    await process.stdin.drain()
                    sent = True
                elif short and not sent:
                    process.stdin.write(b'fixture-code#fixture-state\n')
                    await process.stdin.drain()
                    sent = True
            await process.wait()
        (root / 'native-login-output.txt').write_bytes(transcript)
        assert sent, 'Native login did not show an OAuth authorization URL'
        assert process.returncode == 0, f'Native login exited {process.returncode}; inspect {root}/native-login-output.txt'
        credentials = (workspace / '.claude/.credentials.json').read_text()
        assert ACCESS not in credentials and REFRESH not in credentials, 'Native credential file contains a real fixture token'
        assert json.loads(store.path.read_text())['claudeAiOauth']['accessToken'] == ACCESS
        assert 'factory-claude-' in credentials, 'Native login did not save masked credentials'
        for mode in ('auto', 'default', 'plan'):
            native = await asyncio.create_subprocess_exec(binary, '--print', '--verbose',
                '--output-format', 'stream-json', '--settings', json.dumps({'permissions': {'defaultMode': mode}}),
                'Reply with fixture-native-success.', env=env, cwd=workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(native.communicate(), timeout)
            assert native.returncode == 0, f'Native defaultMode {mode} failed: {stderr.decode(errors="replace")}'
            events = [json.loads(line) for line in stdout.splitlines() if line.startswith(b'{')]
            init = next(event for event in events if event.get('type') == 'system' and event.get('subtype') == 'init')
            assert init['permissionMode'] == mode, f'Native defaultMode {mode} produced {init["permissionMode"]}'
        print('Official Claude native settings accepted auto, default and plan defaults.', flush=True)
        config = {'working_directory': str(workspace), 'native_approval': 'automatic'}
        configure(workspace / '.claude', config)
        native = await asyncio.create_subprocess_exec(binary, '--print', '--verbose',
            '--output-format', 'stream-json', 'fixture-force-search', env=env, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(native.communicate(), timeout)
        assert native.returncode == 0, f'Native default-off search turn failed: {stderr.decode(errors="replace")}'
        assert proxy.search_requested and proxy.search_denied, f'Native permission engine did not deny the requested WebSearch tool: {proxy.search_results}'
        print('Official Claude native permission engine denied WebSearch with Factory default off.', flush=True)
        for path in workspace.rglob('*'):
            if path.is_file():
                contents = path.read_bytes()
                assert ACCESS.encode() not in contents and REFRESH.encode() not in contents, f'Credential escaped to {path}'
        print('Official Claude login completed; native workspace files contain only placeholders, companion owns issued tokens.', flush=True)
        print('Fixture operations: ' + ', '.join(host + path for host, path in dict.fromkeys(proxy.paths)), flush=True)
    finally:
        if 'process' in locals() and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        master.shutdown()
        await running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        asyncio.run(exercise(args.binary, args.output, args.timeout))
    else:
        with tempfile.TemporaryDirectory(prefix='native-claude-login-') as temporary:
            asyncio.run(exercise(args.binary, Path(temporary), args.timeout))


if __name__ == '__main__':
    main()
