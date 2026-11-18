"""Exercise authenticated discovery and chat in a disposable Factory sandbox."""
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pi_sandbox_factory import inference, provider_credentials, sandbox
from pi_sandbox_factory.sandbox_tui import Service


@contextmanager
def model_server(model, key):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authenticated(self):
            header = self.headers.get('Authorization')
            self.server.requests.append((self.path, header))
            if header == (f'Bearer {self.server.key}' if self.server.key else None):
                return True
            self.send_error(401)
            return False

        def do_GET(self):
            if not self.authenticated():
                return
            if self.path == '/v1/models':
                value = {'data': [{'id': model, 'context_length': 65536}]}
            elif self.path == '/props':
                value = {'modalities': {'vision': False}}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_POST(self):
            if not self.authenticated():
                return
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path != '/v1/chat/completions' or data.get('model') != model:
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            chunk = {'id': 'local-test', 'object': 'chat.completion.chunk', 'model': model,
                     'choices': [{'index': 0, 'finish_reason': 'stop',
                                  'delta': {'role': 'assistant', 'content': 'LOCAL_MODEL_KEY_OK_' + model}}]}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        server.key, server.model, server.requests = key, model, []
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def main():
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(TemporaryDirectory(prefix='factory-local-key-check-')))
        servers = [cleanup.enter_context(model_server(model, key)) for model, key in
                   [('fixture-one', 'synthetic-local-one'), ('fixture-two', 'synthetic-local-two'), ('fixture-public', None)]]
        ports = [server.server_port for server in servers]
        name = 'local-key-check-' + uuid4().hex[:8]
        workspace = root / 'workspace'
        workspace.mkdir()
        state = root / 'state'
        service = Service(state)
        def log(message):
            print(message, flush=True)
        args = ['create', name, '--engine', 'podman', '--workspace', str(workspace),
                '--local-model-ports', ','.join(map(str, ports))]
        for port in ports:
            args += ['--allow-domain', f'local-model.internal:{port}']
        service.run(args, log)
        cleanup.callback(service.run, ['delete', name], log)
        service.run(['build', name], log)
        service.run(['up', name], log)
        config = service.config(name)
        dev = sandbox.names(config)[0]
        worker = inference.names(config)[0]

        def inside(*command, env=None):
            values = [arg for key, value in (env or {}).items() for arg in ('--env', f'{key}={value}')]
            result = subprocess.run(['podman', 'exec', *values, dev, *command],
                                    input='', capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr + result.stdout)
            return result.stdout

        service.provider(name, 'local', 'refresh', None, log)
        file = '/home/node/.pi/agent/models.json'
        assert 'fixture-public' in inside('cat', file)
        assert 'fixture-one' not in inside('cat', file)
        servers[2].key = 'synthetic-manual'
        public_route = f'http://127.0.0.1:19431/r/local-{ports[2]}'
        assert 'fixture-public' in inside('curl', '-fsS', '-H', 'Authorization: Bearer synthetic-manual',
                                          public_route + '/v1/models')
        servers[2].key = None

        from test_saved_provider_keys import Vault
        vault = Vault()
        with patch.object(provider_credentials, 'index_path', return_value=root / 'saved-keys.json'), \
             patch.object(provider_credentials, 'os_store', return_value=vault):
            first, second = (inference.local_route_id(port) for port in ports[:2])
            saved = service.assign_provider_key(name, first, {'key': servers[0].key, 'label': 'First'}, log)
            service.assign_provider_key(name, second, {'key': servers[1].key, 'label': 'Second'}, log)
            original_ids = [sandbox.inspect(config, container)['Id'] for container in (dev, worker)]
            try:
                service.assign_provider_key(name, first, {'key': 'synthetic-bad'}, log)
            except ValueError as error:
                assert 'rejected' in str(error)
            else:
                raise AssertionError('The invalid key was accepted')
            assert service.provider_key_status(name)['assignments'][first] == saved

            servers[0].key = 'synthetic-replacement'
            service.assign_provider_key(name, first, {'key': servers[0].key}, log)
            assert original_ids == [sandbox.inspect(config, container)['Id'] for container in (dev, worker)]

        models = inside('cat', file)
        assert all(fixture not in models for fixture in ('synthetic-local-', 'synthetic-replacement'))
        discovered = json.loads(models)['providers']
        expected = []
        for server, port in zip(servers, ports):
            provider = next(id for id, value in discovered.items()
                            if value['baseUrl'] == f'http://127.0.0.1:19431/r/local-{port}/v1')
            expected.append((provider, server.model))
            result = inside('pi', '--no-session', '--provider', provider, '--model', server.model, '-p', 'Return the synthetic response.')
            assert 'LOCAL_MODEL_KEY_OK_' + server.model in result, result
        log('PASS actual Pi chat with separate authenticated and public ports')
        subprocess.run(['podman', 'cp', str(ROOT / 'tests/check_pendant_worker.cjs'), dev + ':/tmp/check-local-keys.cjs'], check=True)
        subprocess.run(['podman', 'cp', str(ROOT / 'pi_sandbox_factory/containers/editor/watch-fallback.cjs'),
                        dev + ':/tmp/local-key-watch-fallback.cjs'], check=True)
        log(inside('node', '/tmp/check-local-keys.cjs', '--local-server-keys',
                   env={'TEST_LOCAL_MODELS': json.dumps(expected), 'NODE_OPTIONS': '--require=/tmp/local-key-watch-fallback.cjs'}).strip())
        assert {header for _, header in servers[0].requests} == {None, 'Bearer synthetic-local-one',
                                                               'Bearer synthetic-bad', 'Bearer synthetic-replacement'}
        assert {header for _, header in servers[1].requests} == {None, 'Bearer synthetic-local-two'}
        assert {header for _, header in servers[2].requests} == {None, 'Bearer synthetic-manual'}
        log('PASS per-port credentials, rejected replacement, and live reassignment')


if __name__ == '__main__':
    main()
