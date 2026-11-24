"""Run inside a disposable Pi workspace: real CLI service tool paths."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = request['messages']
        content = json.dumps(messages)
        reviewing = any(row['role'] in ('system', 'developer') and
                        any(prefix in str(row.get('content')) for prefix in
                            ('Review one proposed coding-agent action.', 'You are a fail-closed permission reviewer.'))
                        for row in messages)
        if not reviewing and request.get('tools'):
            assert '/opt/sandbox/base-profile' in content, 'Applied recipe instructions missing'
            assert 'database.profile.internal' in content and 'bash_escalate' in content
        delta = {'role': 'assistant', 'content': 'PROFILE_CLIENT_FINISHED'}
        finish = 'stop'
        if reviewing:
            delta['content'] = json.dumps({'outcome': 'allow', 'risk_level': 'medium',
                'user_authorization': 'high', 'rationale': 'Disposable profile SSH regression.'})
        else:
            count = sum(row['role'] == 'tool' for row in messages)
            if count < 3:
                command = 'ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -Atc \\\"SELECT \'PROFILE_SQL_OK\'\\\""'
                tool, arguments = [('read', {'path': '/opt/sandbox/base-profile/profile/profile.json'}),
                    ('bash', {'command': command}),
                    ('bash_escalate', {'command': command, 'justification': 'Test direct SQL over profile SSH in the disposable sandbox.'})][count]
                delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'profile-' + str(count),
                    'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}
                finish = 'tool_calls'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        for part, reason in ((delta, None), ({}, finish)):
            row = {'id': 'profile-fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'profile-fixture',
                   'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
            self.wfile.write(('data: ' + json.dumps(row) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')

server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
models = Path('/home/node/.pi/agent/models.json')
prior = models.read_bytes() if models.exists() else None
models.write_text(json.dumps({'providers': {'fixture': {'baseUrl': f'http://127.0.0.1:{server.server_port}/v1',
    'api': 'openai-completions', 'apiKey': 'synthetic', 'models': [{'id': 'fixture-model', 'contextWindow': 32000, 'maxTokens': 4096}]}}}))
process = None
try:
    process = subprocess.Popen(['pi', '--mode', 'rpc', '--provider', 'fixture', '--model', 'fixture-model', '--no-session'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd='/workspace')
    lines, errors, events = queue.Queue(), [], []
    def reader():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=lambda: errors.extend(process.stderr.readlines()), daemon=True).start()
    process.stdin.write(json.dumps({'type': 'prompt', 'message': 'RUN_PROFILE_SERVICES'}) + '\n'); process.stdin.flush()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        line = lines.get(timeout=max(1, deadline - time.monotonic()))
        assert line is not None, errors
        try: event = json.loads(line)
        except json.JSONDecodeError: continue
        events.append(event)
        assert not (event.get('type') == 'extension_ui_request' and event.get('method') in ('select','confirm','input')), event
        if event.get('type') == 'agent_settled': break
    results = [event for event in events if event.get('type') == 'tool_execution_end']
    assert [event['toolName'] for event in results] == ['read', 'bash', 'bash_escalate'], (events, errors)
    assert 'developer-postgres' in json.dumps(results[0]), results
    assert 'PROFILE_SQL_OK' not in json.dumps(results[1]), results
    assert 'PROFILE_SQL_OK' in json.dumps(results[2]) and not results[2].get('isError'), results
    print('PASS Pi CLI: applied instructions/read, blocked inner SSH and existing reviewed direct SSH', flush=True)
    process.terminate(); process.wait(timeout=15); process = None
finally:
    if process is not None:
        process.terminate()
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
    if prior is None: models.unlink(missing_ok=True)
    else: models.write_bytes(prior)
    server.shutdown(); server.server_close(); thread.join()
