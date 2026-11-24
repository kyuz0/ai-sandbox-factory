#!/usr/bin/env python3
"""Credential-free native session conversion checks in disposable homes.

Run with --source /path/to/session-migrate/src (or its installed package),
--codex-bin /path/to/codex --claude-bin /path/to/claude and --pi-image IMAGE.
The Pi image must already be cached. No existing sandbox or sign-in is touched.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

USER = 'FACTORY-USER-7319'
ASSISTANT = 'FACTORY-ASSISTANT-4821'
FOLLOWUP = 'FACTORY-FOLLOWUP-2048'
REPLY = 'FACTORY-RELOAD-OK'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(value)
        path = self.path.split('?', 1)[0]
        if path.endswith('/messages'):
            message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                       'model': value['model'], 'content': [], 'stop_reason': None,
                       'usage': {'input_tokens': 10, 'output_tokens': 0}}
            events = [('message_start', {'type': 'message_start', 'message': message}),
                      ('content_block_start', {'type': 'content_block_start', 'index': 0,
                       'content_block': {'type': 'text', 'text': ''}}),
                      ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                       'delta': {'type': 'text_delta', 'text': REPLY}}),
                      ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                      ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
                       'usage': {'output_tokens': 3}}), ('message_stop', {'type': 'message_stop'})]
        elif path.endswith('/responses'):
            item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': REPLY, 'annotations': []}]}
            response = {'id': 'resp_fixture', 'status': 'completed', 'output': [item],
                        'usage': {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13}}
            events = [(None, {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}),
                      (None, {'type': 'response.output_item.added', 'output_index': 0,
                              'item': {**item, 'status': 'in_progress', 'content': []}}),
                      (None, {'type': 'response.output_text.delta', 'item_id': item['id'],
                              'output_index': 0, 'content_index': 0, 'delta': REPLY}),
                      (None, {'type': 'response.output_item.done', 'output_index': 0, 'item': item}),
                      (None, {'type': 'response.completed', 'response': response})]
        elif path.endswith('/chat/completions'):
            events = [(None, {'id': 'fixture', 'object': 'chat.completion.chunk', 'model': value['model'],
                              'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': REPLY}, 'finish_reason': None}]}),
                      (None, {'id': 'fixture', 'object': 'chat.completion.chunk', 'model': value['model'],
                              'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                              'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}})]
        else:
            self.send_error(404)
            return
        payload = ''.join((f'event: {name}\n' if name else '') + f'data: {json.dumps(event)}\n\n'
                          for name, event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def provider():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def verify(command, environment, work, server, timeout):
    start = len(server.requests)
    result = subprocess.run(command, env=environment, cwd=work, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f'Native client exited {result.returncode}: {result.stdout}\n{result.stderr}'
    assert REPLY in result.stdout, result.stdout
    replay = json.dumps(server.requests[start:])
    for marker in (USER, ASSISTANT, FOLLOWUP):
        assert marker in replay, f'Native resume omitted {marker} from the provider request'


def pi_child(root, timeout):
    with provider() as server:
        agent = root / 'pi'; agent.mkdir()
        home = root / 'pi-home'; home.mkdir()
        (agent / 'models.json').write_text(json.dumps({'providers': {'fixture': {
            'baseUrl': f'http://127.0.0.1:{server.server_address[1]}/v1', 'api': 'openai-completions',
            'apiKey': 'synthetic', 'models': [{'id': 'fixture-model', 'name': 'Fixture',
            'input': ['text'], 'contextWindow': 32768, 'maxTokens': 4096}]}}}))
        env = {**os.environ, 'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent), 'PI_OFFLINE': '1'}
        version = subprocess.check_output(['pi', '--version'], text=True).strip()
        assert version == '1.4.0', f'Expected Pi 1.4.0, found {version}'
        verify(['pi', '--print', '--session', str(root / 'pi-import.jsonl'), '--model', 'fixture/fixture-model',
                '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-context-files', '--offline', FOLLOWUP],
               env, root / 'work', server, timeout)
        print('Pi 1.4.0: converted history replay and native follow-up passed')


def codex_database(root, binary):
    helper = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/session-transfer/transfer.py'
    spec = importlib.util.spec_from_file_location('factory_transfer', helper)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    home = root / 'paginated'; home.mkdir()
    identifier, turn = str(uuid.uuid4()), str(uuid.uuid4())
    timestamp = '2026-11-23T12:00:00Z'
    path = home / 'sessions/2026/11/23' / f'rollout-2026-11-23T12-00-00-{identifier}.jsonl'
    path.parent.mkdir(parents=True)
    meta = {'id': identifier, 'session_id': identifier, 'timestamp': timestamp, 'cwd': '/workspace',
            'originator': 'cli', 'cli_version': '0.171.0', 'source': 'cli',
            'model_provider': 'openai', 'history_mode': 'paginated'}
    path.write_text(json.dumps({'type': 'session_meta', 'ordinal': 0, 'timestamp': timestamp, 'payload': meta}) + '\n')
    native = module.NativeCodex(home, binary)
    try:
        native.call('thread/read', {'threadId': identifier, 'includeTurns': False})
        native.call('thread/turns/list', {'threadId': identifier})
        with sqlite3.connect(Path(native.temporary.name) / 'home/thread_history_1.sqlite') as origin, sqlite3.connect(home / 'thread_history_1.sqlite') as target:
            origin.backup(target)
        index = Path(native.temporary.name) / 'home/state_5.sqlite'
        if index.exists():
            with sqlite3.connect(index) as origin, sqlite3.connect(home / 'state_5.sqlite') as target:
                origin.backup(target)
    finally:
        native.close()
    items = [{'type': 'userMessage', 'id': 'user', 'content': [{'type': 'text', 'text': USER, 'text_elements': []}]},
             {'type': 'commandExecution', 'id': 'command', 'command': 'printf SYNTHETIC', 'cwd': '/workspace',
              'status': 'completed', 'commandActions': [], 'aggregatedOutput': 'SYNTHETIC', 'exitCode': 0},
             {'type': 'agentMessage', 'id': 'answer', 'text': ASSISTANT, 'phase': 'final_answer'}]
    with sqlite3.connect(home / 'thread_history_1.sqlite') as database:
        database.execute('INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,final_agent_item_id) VALUES (?,?,?,?,?)',
                         (identifier, turn, 1, 'completed', 'answer'))
        for ordinal, item in enumerate(items, 2):
            database.execute('INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type) VALUES (?,?,?,?,?,?,?)',
                             (identifier, turn, item['id'], ordinal, 1795435200000, json.dumps(item), item['type']))
    index = home / 'state_5.sqlite'
    if index.exists():
        with sqlite3.connect(index) as database:
            database.execute('UPDATE threads SET rollout_path=? WHERE id=?',
                             ('/home/node/.codex/' + str(path.relative_to(home)), identifier))
    before = (home / 'thread_history_1.sqlite').read_bytes()
    native = module.NativeCodex(home, binary)
    try:
        pages = native.pages
        native.pages = lambda method, params: pages(method, {**params, 'limit': 1})
        source = module.load_source(path, module.AgentFormat.CODEX, native)
        assert [event.kind.value for event in source.events] == ['message', 'tool_call', 'tool_result', 'message']
        assert source.events[0].text == USER and source.events[-1].text == ASSISTANT
    finally:
        native.close()
    assert before == (home / 'thread_history_1.sqlite').read_bytes(), 'Projection changed the source database'
    print('Codex 0.171.0: database-only source, cursor pagination, tool ordering, path rebasing and source preservation passed')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--codex-bin', default=shutil.which('codex'))
    parser.add_argument('--claude-bin', default=shutil.which('claude'))
    parser.add_argument('--pi-image')
    parser.add_argument('--pi-child', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.pi_child:
        pi_child(args.pi_child, args.timeout)
        return
    if not args.pi_image:
        parser.error('--pi-image must name an existing cached Factory image with Pi 1.4.0')
    if args.source:
        sys.path.insert(0, str(args.source.resolve()))
    from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session, TargetFormat
    from session_migrate.conversion import ConversionOptions, convert_session, load_session, target_import_paths, write_artifact
    for binary, expected in ((args.codex_bin, '0.171.0'), (args.claude_bin, '2.1.330')):
        version = subprocess.check_output([binary, '--version'], text=True).strip()
        assert expected in version, f'Expected native client {expected}, found {version}'
    with tempfile.TemporaryDirectory(prefix='factory-session-native-') as temporary, provider() as server:
        root = Path(temporary); work = root / 'work'; work.mkdir()
        source = Session(AgentFormat.PI, root / 'synthetic.jsonl', '0' * 64, str(uuid.uuid4()), work,
                         '2026-11-23T12:00:00Z', '1.4.0', None, 'Synthetic migration',
                         (Event(EventKind.MESSAGE, Provenance(0), role=Role.USER, text=USER),
                          Event(EventKind.MESSAGE, Provenance(1), role=Role.ASSISTANT, text=ASSISTANT)), 2)
        sources = {'codex': codex_database(root, args.codex_bin)}
        for format_ in (TargetFormat.PI, TargetFormat.CLAUDE):
            artifact = convert_session(source, ConversionOptions(format_, cwd=work))
            path = root / (format_.value + '-source.jsonl')
            path.write_bytes(artifact.native_bytes)
            sources[format_.value] = load_session(path, AgentFormat(format_.value))
        for source_name, parsed_source in sources.items():
            for format_, version, binary in ((TargetFormat.CLAUDE, '2.1.330', args.claude_bin),
                                            (TargetFormat.CODEX, '0.171.0', args.codex_bin),
                                            (TargetFormat.PI, '1.4.0', None)):
                if source_name == format_.value:
                    continue
                route = root / (source_name + '-to-' + format_.value); route.mkdir()
                if format_ == TargetFormat.PI:
                    (route / 'work').mkdir()
                    artifact = convert_session(parsed_source, ConversionOptions(format_, cwd=Path('/fixture/work')))
                    (route / 'pi-import.jsonl').write_bytes(artifact.native_bytes)
                    subprocess.run(['podman', 'run', '--rm', '--pull=never', '--network=none', '--userns=keep-id', '--security-opt=label=disable',
                                    '--entrypoint', 'python3', '-v', f'{Path(__file__).resolve()}:/check.py:ro', '-v', f'{route}:/fixture',
                                    args.pi_image, '/check.py', '--pi-child', '/fixture', '--timeout', str(args.timeout)], check=True, timeout=args.timeout + 30)
                else:
                    client = route / 'client'; client.mkdir()
                    home = route / 'home'; home.mkdir()
                    artifact = convert_session(parsed_source, ConversionOptions(format_, cwd=work, target_cli_version=version, model_provider='fixture'))
                    destination, manifest = target_import_paths(artifact, client)
                    write_artifact(artifact, output_path=destination, manifest_path=manifest)
                    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'TERM': 'dumb', 'NO_COLOR': '1', 'TMPDIR': str(root)}
                    if format_ == TargetFormat.CLAUDE:
                        env.update(CLAUDE_CONFIG_DIR=str(client), ANTHROPIC_API_KEY='synthetic', ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_address[1]}',
                                   DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1', DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
                        command = [binary, '--bare', '--print', '--verbose', '--output-format', 'stream-json', '--resume', artifact.session_id,
                                   '--model', 'claude-haiku-4-5-20251001', '--disable-slash-commands', '--no-chrome', FOLLOWUP]
                    else:
                        env.update(CODEX_HOME=str(client), FIXTURE_API_KEY='synthetic')
                        (client / 'config.toml').write_text(f'model = "fixture-model"\nmodel_provider = "fixture"\n[model_providers.fixture]\nname = "Fixture"\nbase_url = "http://127.0.0.1:{server.server_address[1]}/v1"\nenv_key = "FIXTURE_API_KEY"\nwire_api = "responses"\nrequires_openai_auth = false\n')
                        command = [binary, 'exec', 'resume', artifact.session_id, '--skip-git-repo-check', '--json', FOLLOWUP]
                    verify(command, env, work, server, args.timeout)
                print(f'{source_name} → {format_.value} {version}: parsed history replay and native follow-up passed', flush=True)


if __name__ == '__main__':
    main()
