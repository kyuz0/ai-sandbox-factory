"""Verify native Claude API-error filtering offline with a synthetic credential."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

FIXTURE_CREDENTIAL = 'fixture-private-auth-not-a-real-key'
RECOVERY_REPLY = 'fixture-error-recovery-complete'


def browser_fixture():
    """Serve errors until the browser test explicitly enables recovery."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
            if not Path('/tmp/fixture-api-recover').exists():
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'type': 'error', 'error': {
                    'type': 'invalid_request_error', 'message': 'Rejected token ' + FIXTURE_CREDENTIAL}}).encode())
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = [
                {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message',
                    'role': 'assistant', 'content': [], 'model': data.get('model'), 'stop_reason': None,
                    'usage': {'input_tokens': 30, 'output_tokens': 1}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'text_delta', 'text': RECOVERY_REPLY}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                    'usage': {'output_tokens': 9}},
                {'type': 'message_stop'},
            ]
            for event in events:
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
    ThreadingHTTPServer(('127.0.0.1', 18782), Handler).serve_forever()


def inside():
    sys.path.insert(0, '/opt/check-source')
    from editor_protocol import ClaudeProtocol
    from editor_bridge import history_message

    credential = FIXTURE_CREDENTIAL
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('content-length', 0)))
            requests.append(self.path)
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'type': 'error', 'error': {
                'type': 'invalid_request_error', 'message': 'Rejected token ' + credential}}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, 'ANTHROPIC_API_KEY': credential,
           'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
    args = ['/usr/local/bin/factory-agent', '--restricted', '--print', '--model', 'sonnet',
            '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--permission-prompt-tool', 'stdio', '--include-partial-messages']
    prompt = {'type': 'user', 'message': {'role': 'user', 'content': 'Reply hello.'},
              'parent_tool_use_id': None, 'session_id': ''}
    try:
        # Leave room for the native client's exponential retry schedule if it
        # retries this failure; a normal 400 response completes immediately.
        result = subprocess.run(args, input=json.dumps(prompt) + '\n', env=env,
                                capture_output=True, text=True, timeout=1200)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert requests, 'The client did not reach the local API fixture'
    assert result.returncode != 0, 'The rejected request was reported as successful'
    native = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    # The control must reproduce the leak before the boundary can prove it
    # removes it. No real sign-in or credential is present in this container.
    assert credential in result.stdout, 'The native error format changed; review this test'
    bridge = ClaudeProtocol()
    safe = [value for item in native if (value := bridge.outgoing(item)) is not None]
    assert credential not in json.dumps(safe), 'A private credential crossed the editor boundary'
    assert all(item.get('tools') == [] for item in native if item.get('subtype') == 'init')
    assert any(item.get('type') == 'assistant' and item.get('is_api_error_message') for item in safe)
    assert any(item.get('type') == 'result' and item.get('is_error') for item in safe)
    session = next(item['session_id'] for item in native if item.get('type') == 'result')
    history = history_message(session)
    assert history and history['records'], 'The native conversation was not saved'
    assert credential not in json.dumps(history), 'A private credential crossed through saved history'
    assert any(row['type'] == 'assistant' for row in history['records']), 'The error record was removed'
    print('PASS: native errors, partial events and saved history filtered; HTTP status retained; no credential exposed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Locally built Claude controller image')
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--serve-browser-fixture', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_browser_fixture:
        browser_fixture()
    elif args.inside:
        inside()
    elif args.image:
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['podman', 'run', '--rm', '--network=none', '--security-opt=label=disable',
                        '--entrypoint', 'python3',
                        '-v', f'{Path(__file__).resolve()}:/tmp/check.py:ro',
                        '-v', f'{root / "ai_sandbox_factory/containers/primary"}:/opt/check-source:ro',
                        args.image, '/tmp/check.py', '--inside'], check=True)
    else:
        parser.error('--image is required')
