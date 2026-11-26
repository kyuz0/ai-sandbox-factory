"""Model setup immediately after a successful, locked workspace start."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import inference, operation_locks, operations, provider_credentials, sandbox


class CreationReadyProvidersTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = operations.Service(self.root)
        self.name = 'provider-fixture'
        self.state = self.root / self.name
        private = patch.object(operation_locks, 'private_runtime_dir', return_value=self.root)
        private.start()
        self.addCleanup(private.stop)
        self.routes = {'origin': 'https://models.example.test', 'auth': {'type': 'bearer'}}
        self.rows = [{'name': name, 'manifest': {'inference': self.routes} if name == 'managed' else {}}
                     for name in ('managed', 'adapter', 'saved', 'public', 'local')]
        self.config = {'name': self.name, 'engine': 'podman', 'local_model_ports': '8080',
                       'model_providers': self.rows}
        self.raw_key = 'synthetic-adapter-key'
        self.managed_key = 'synthetic-inference-key'
        self.draft = operations.preset('base', operations.DEFAULT_SETTINGS)
        self.draft.update(name=self.name, workspace=str(self.root), open_editor=False,
                          model_providers={r['name']: {} for r in self.rows}, local_model_ports='8080',
                          _provider_keys={'managed': self.managed_key, 'adapter': self.raw_key},
                          _provider_credential_choices={'saved': {'ref': 'fixture-ref'}},
                          _local_model_keys={'8080': {'key': 'synthetic-local-key'}})
        self.messages = []

    def exercise(self, *, failing=False):
        requests = []
        def run_stage(args, log):
            if args[0] == 'create':
                self.state.mkdir()
                (self.state / 'config.json').write_text(json.dumps(self.config))
        def adapter(args, **kwargs):
            self.assertTrue(operation_locks.in_use(self.name))
            self.assertEqual(args[1:3], ['exec', '-i'])
            self.assertNotIn(self.raw_key, args)
            request = json.loads(kwargs['input'])
            requests.append(request)
            if request['id'] == 'adapter' and failing:
                return subprocess.CompletedProcess(args, 7, '', f'HTTP 503 fetching catalogue ({self.raw_key})')
            return subprocess.CompletedProcess(args, 0, json.dumps({'state': 'ready', 'message': request['id']
                                               + ' ' + request.get('secret', '')}), '')
        with patch.object(self.service, 'run', side_effect=run_stage) as lifecycle, \
                patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}) as inspect, \
                patch.object(sandbox, 'run', side_effect=adapter), \
                patch.object(inference, 'routes', return_value={}), \
                patch.object(inference, 'credentials') as credentials, \
                patch.object(self.service, 'provider_key_status', return_value={'configured': []}), \
                patch.object(provider_credentials, 'assign') as assign:
            self.service.create(self.draft, None, self.messages.append)
            self.assertEqual([call.args[0][0] for call in lifecycle.call_args_list], ['create', 'build', 'up'])
            create = lifecycle.call_args_list[0].args[0]
            self.assertEqual(create[create.index('--image-source') + 1], self.draft['image_source'])
            inspect.assert_called_once_with(self.config, sandbox.names(self.config)[0])
            credentials.assert_called_once_with(self.config, self.state, {
                'id': 'managed', 'origin': self.routes['origin'], 'auth': 'bearer',
                'credentials': {'key': self.managed_key}})
            self.assertEqual([(c.args[2], c.kwargs) for c in assign.call_args_list],
                             [(inference.local_route_id('8080'), {'key': 'synthetic-local-key'}),
                              ('saved', {'ref': 'fixture-ref'})])
        return requests

    def test_creation_refreshes_each_selected_provider_once_and_keeps_managed_key_in_companion(self):
        requests = self.exercise()
        self.assertEqual([r['id'] for r in requests], ['managed', 'adapter', 'saved', 'public', 'local'])
        self.assertEqual(requests[1]['secret'], self.raw_key)
        self.assertNotIn(self.managed_key, json.dumps(requests))
        self.assertNotIn(self.raw_key, str(self.messages))
        self.assertIn('[redacted]', str(self.messages))

    def test_editor_open_defers_providers_without_new_credentials(self):
        self.draft['open_editor'] = True
        self.assertEqual([r['id'] for r in self.exercise()], ['managed', 'adapter', 'saved'])

    def test_failure_retains_provider_stage_exit_code_and_redacts_entered_key(self):
        with self.assertRaisesRegex(ValueError, 'Could not refresh provider adapter.*status 7.*HTTP 503') as failure:
            self.exercise(failing=True)
        self.assertNotIn(self.raw_key, str(failure.exception))
        self.assertNotIn(self.raw_key, str(self.messages))
        self.assertFalse(operation_locks.in_use(self.name))

    def test_missing_workspace_stops_setup_before_adapter_or_key_delivery(self):
        self.state.mkdir()
        (self.state / 'config.json').write_text(json.dumps(self.config))
        with patch.object(sandbox, 'inspect', return_value=None), patch.object(sandbox, 'run') as run, \
                patch.object(provider_credentials, 'assign') as assign:
            with self.assertRaisesRegex(ValueError, 'workspace is no longer running'):
                self.service.setup(self.name, self.draft, None, self.messages.append, runtime_ready=True)
        run.assert_not_called()
        assign.assert_not_called()

    def test_update_caller_keeps_standalone_configure_semantics(self):
        draft = {'model_providers': {'adapter': {}}, 'open_editor': False}
        with patch.object(self.service, 'run') as run:
            self.service.setup(self.name, draft, None, self.messages.append)
        self.assertEqual(run.call_args.args[0], ['configure', self.name])
