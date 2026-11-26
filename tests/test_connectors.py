import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from ai_sandbox_factory import connectors
from ai_sandbox_factory.connectors import runtime

spec = importlib.util.spec_from_file_location('connector_broker', connectors.ROOT / 'companion/broker.py')
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


def manifest(url='https://status.example.test/v1/summary?format=text'):
    return {'version': 1, 'id': 'status', 'title': 'Service status',
            'description': 'Read service status.', 'documentation': 'Use a read-only service token.',
            'operations': {'summary': url}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.installed = self.root / 'installed/status/connector.json'
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text(json.dumps(manifest()))
        self.state = self.root / 'sandbox'
        self.state.mkdir()
        self.config = {'name': 'test', 'engine': 'podman', 'connector_ids': ['status'], 'uid': 1000, 'gid': 1000}
        patched = patch.object(connectors, 'directory', return_value=self.root / 'installed')
        patched.start()
        self.addCleanup(patched.stop)

    def test_registered_builtin_editors_and_settings(self):
        from ai_sandbox_factory.aws_ui import AWSProfilePicker
        from ai_sandbox_factory.repository_ui import CreateSSHDialog
        installed = {item.id: item for item in connectors.catalog() if item.available and not item.external}
        self.assertEqual(set(installed), set(connectors.ADAPTERS))
        draft = {'options': {}, '_ssh_key_file': 'old', '_ssh_key_duration': '1h'}
        ssh = connectors.ADAPTERS['ssh']
        self.assertEqual(ssh.saved_action, 'ssh-settings')
        self.assertIsInstance(ssh.draft_editor(draft, installed['ssh']), CreateSSHDialog)
        self.assertEqual(ssh.status(draft), 'Off')
        ssh.apply_draft(draft, ({'ssh': {'hosts': [{'alias': 'git'}]}}, {'name': 'Author'}))
        self.assertEqual(ssh.status(draft), '1 destination · Git commit identity set')
        self.assertNotIn('_ssh_key_file', draft)
        self.assertNotIn('_ssh_key_duration', draft)
        aws = connectors.ADAPTERS['aws']
        self.assertEqual(aws.saved_action, 'aws-access')
        self.assertIsInstance(aws.draft_editor(draft, installed['aws']), AWSProfilePicker)
        aws.apply_draft(draft, {'profile_name': 'Research'})
        self.assertIn('Research', aws.status(draft))

    def test_discovery_snapshot_and_keyring_scope(self):
        options = connectors.catalog()
        self.assertEqual([p.id for p in options], ['ssh', 'aws', 'status', 'azure', 'gcp'])
        self.assertTrue(options[2].external)
        self.assertFalse(options[-1].available)
        plugin = options[2]
        self.assertEqual(plugin.documentation, manifest()['documentation'])
        self.assertEqual(connectors.catalog_with_errors(self.config)[0], options)
        connectors.snapshot(['status'], self.state)
        self.installed.write_text(json.dumps(manifest('https://different.example.test/summary')))
        self.assertNotEqual(connectors.catalog()[2].account, plugin.account)
        connectors.snapshot(['status'], self.state)
        self.assertEqual(connectors.saved(self.config, self.state), (plugin,))
        self.installed.unlink()
        self.assertEqual(connectors.for_sandbox(self.config, self.state)[2], plugin)

    def test_reject_invalid_manifests_without_disabling_builtins(self):
        for bad in [dict(manifest(), command='curl anything'), dict(manifest(), version=True),
                    manifest('http://host/'), manifest('https://user:passwd@host/'),
                    manifest('https://service:bad/x'), manifest('https://service/path#fragment')]:
            with self.subTest(bad=bad):
                self.installed.write_text(json.dumps(bad))
                choices, errors = connectors.catalog_with_errors()
                self.assertEqual([p.id for p in choices], ['ssh', 'aws', 'azure', 'gcp'])
                self.assertTrue(any('status' in error for error in errors))
                with self.assertRaises(ValueError):
                    connectors.snapshot(['status'], self.state)
        for ids in [['aws'], ['../status'], ['status', 'status'], 'status']:
            with self.assertRaises(ValueError):
                connectors.snapshot(ids, self.state)

    def test_unreadable_active_snapshot_can_still_be_revoked(self):
        connectors.snapshot(['status'], self.state)
        (self.state / 'connector-manifests/status.json').write_text('{')
        choices, errors = connectors.catalog_with_errors(self.config, self.state)
        active = next(plugin for plugin in choices if plugin.id == 'status')
        self.assertEqual(active.routes, ())
        self.assertTrue(active.available and active.external)
        self.assertIn('saved connector configuration', errors[0])
        self.installed.write_text('{')
        self.assertIn('Fix the JSON in its connector.json file', connectors.catalog_with_errors()[1][0])
        self.assertEqual(len(connectors.catalog_with_errors(self.config, self.state)[1]), 1)

    def test_start_delivers_only_to_separate_worker_stdin(self):
        raw = manifest()
        raw['operations']['billing'] = 'https://status.example.test/billing'
        self.installed.write_text(json.dumps(raw))
        self.config['connector_operations'] = {'status': ['summary']}
        connectors.snapshot(['status'], self.state)
        plugin = connectors.saved(self.config, self.state)[0]
        def engine(args, **kwargs):
            if args[1:3] == ['image', 'inspect']:
                return subprocess.CompletedProcess(args, 0, '[{"Id":"fixture-image-id"}]', '')
            return subprocess.CompletedProcess(args, 0, 'worker-id', '')
        with (patch('ai_sandbox_factory.provider_credentials.os_store') as store,
              patch('ai_sandbox_factory.sandbox.inspect', return_value=None),
              patch('ai_sandbox_factory.sandbox.run', side_effect=engine) as run,
              patch('subprocess.run') as deliver):
            store.return_value.get_password.return_value = 'private-token'
            deliver.return_value.returncode = 0
            connectors.up(self.config, self.state)
            args = run.call_args.args[0]
            self.assertNotIn('private-token', str(args))
            self.assertNotIn('private-token', str(deliver.call_args.args))
            self.assertEqual(deliver.call_args.args[0][:4], ['podman', 'exec', '-i', 'worker-id'])
            self.assertEqual(json.loads(deliver.call_args.kwargs['input']), {'token': 'private-token', 'routes': manifest()['operations']})
            self.assertEqual(args.count('-v'), 1)
            self.assertIn('/runtime-config/connectors/status:/socket:', args[args.index('-v') + 1])
            store.return_value.get_password.assert_called_once_with(plugin.service, plugin.account)
            connectors.store_token(plugin, 'new-token')
            store.return_value.set_password.assert_called_once_with(plugin.service, plugin.account, 'new-token')

    def test_operation_grants_match_manifest(self):
        plugin = connectors.catalog()[2]
        self.assertEqual(connectors.permitted_routes(plugin, {}), plugin.routes)
        for bad in [None, {'status': []}, {'status': ['other']}, {'status': ['summary', 'summary']}, {'status': 'summary'}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                connectors.permitted_routes(plugin, {'connector_operations': bad})

    def test_hot_permissions_update_uses_existing_credential(self):
        connectors.snapshot(['status'], self.state)
        plugin = connectors.saved(self.config, self.state)[0]
        current = {'Id': 'same-worker', 'State': {'Running': True},
                   'Config': {'Labels': {runtime.ROLE: plugin.id, runtime.SCOPE: plugin.account}}}
        with (patch('ai_sandbox_factory.sandbox.inspect', return_value=current),
              patch('ai_sandbox_factory.provider_credentials.os_store') as keyring,
              patch('subprocess.run') as run):
            run.return_value.returncode = 0
            connectors.configure_running(self.config, self.state)
            self.assertEqual(connectors.up(self.config, self.state), [])
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.args[0], ['podman', 'exec', '-i', 'same-worker', 'python3', '/app/receive.py'])
                self.assertEqual(json.loads(call.kwargs['input']), {'routes': dict(plugin.routes)})
            keyring.assert_not_called()

    def test_start_checks_missing_token_before_build_or_workspace_start(self):
        connectors.snapshot(['status'], self.state)
        with (patch('ai_sandbox_factory.sandbox.inspect', return_value=None),
              patch('ai_sandbox_factory.provider_credentials.os_store') as store,
              patch('ai_sandbox_factory.sandbox.run') as run):
            store.return_value.get_password.return_value = None
            with self.assertRaisesRegex(ValueError, 'connector-login status'):
                connectors.up(self.config, self.state)
            run.assert_not_called()

    def test_unix_socket_path_boundary(self):
        limit = 107 - len(connectors.client_socket({}, ''))
        for size in (limit, limit + 1):
            identifier = 'a' * size
            raw = dict(manifest(), id=identifier)
            if size == limit:
                self.assertEqual(connectors._parse(raw, identifier).id, identifier)
            else:
                with self.assertRaisesRegex(ValueError, 'Unix socket path'):
                    connectors._parse(raw, identifier)

    def test_do_not_remove_container_without_connector_role(self):
        with (patch('ai_sandbox_factory.sandbox.inspect', return_value={'Config': {'Labels': {}}}),
              patch('ai_sandbox_factory.sandbox.run') as run,
              self.assertRaisesRegex(ValueError, 'unrelated container')):
            connectors.down(self.config)
        run.assert_not_called()

    def test_socket_paths_match_existing_client_mounts(self):
        self.assertEqual(connectors.client_socket({}, 'status'), '/opt/pi-security/config/connectors/status/api.sock')
        self.assertEqual(connectors.client_socket({'primary_agent': 'codex'}, 'status'), '/run/primary-runtime/connectors/status/api.sock')


class BrokerTests(unittest.TestCase):
    def test_socket_invocation_and_denied_requests(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(broker, 'HTTPSConnection') as tls:
            path = str(Path(folder) / 'api.sock')
            with broker.Server(path, broker.Handler) as worker:
                worker.token = 'service-secret'
                worker.routes = manifest()['operations']
                upstream = tls.return_value
                reply = MagicMock(wraps=io.BytesIO(b'status: ready'))
                reply.length = 0
                reply.status = 200
                upstream.getresponse.return_value = reply
                thread = threading.Thread(target=worker.serve_forever)
                thread.start()
                try:
                    def request(method, route, **kwargs):
                        with socket.socket(socket.AF_UNIX) as sock:
                            sock.connect(path)
                            client = http.client.HTTPConnection('unused')
                            client.sock = sock
                            client.request(method, route, **kwargs)
                            response = client.getresponse()
                            return response.status, response.read()

                    self.assertEqual(request('GET', '/summary', headers={'Authorization': 'client-value',
                                     'Host': 'other.test', 'Cookie': 'private'}), (200, b'status: ready'))
                    tls.assert_called_once_with('status.example.test', None)
                    upstream.request.assert_called_once_with('GET', '/v1/summary?format=text',
                                                           headers={'Authorization': 'Bearer service-secret'})
                    for method, route, kwargs in [('POST', '/summary', {}), ('GET', '/unknown', {}),
                                                   ('GET', '/summary?url=https://other.test', {}),
                                                   ('GET', '/summary', {'body': 'custom'})]:
                        self.assertGreaterEqual(request(method, route, **kwargs)[0], 400)
                    self.assertEqual(upstream.request.call_count, 1)
                    reply.status = 302
                    code, message = request('GET', '/summary')
                    self.assertEqual(code, 502)
                    self.assertIn(b'The service returned HTTP 302.', message)
                    self.assertNotIn(b'service-secret', message)
                    self.assertEqual(tls.call_count, 2)
                finally:
                    worker.shutdown()
                    thread.join()
