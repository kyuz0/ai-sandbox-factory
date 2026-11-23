import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_sandbox_factory import activity, connector_settings, connectors, sandbox
from ai_sandbox_factory.sandbox_options import write_json


class ConnectorSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'sandbox'
        self.enterContext(patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(self.root / 'registry')))
        self.file = self.root / 'installed' / 'status' / 'connector.json'
        source = Path(__file__).resolve().parents[1] / 'examples/connectors/build-status/connector.json'
        self.manifest = {**json.loads(source.read_text()), 'id': 'status'}
        write_json(self.file, self.manifest)
        self.config = {'name': 'sandbox', 'engine': 'podman', 'uid': 1000, 'gid': 1000}
        write_json(self.state / 'config.json', self.config)
        self.enterContext(patch.object(connectors, 'directory', return_value=self.file.parent.parent))
        self.store = self.enterContext(patch('ai_sandbox_factory.provider_credentials.os_store')).return_value
        self.store.get_password.return_value = 'fixture-token'
        self.inspect = self.enterContext(patch.object(sandbox, 'inspect', return_value=None))
        self.probe = self.enterContext(patch.object(connector_settings, 'check_sockets'))
        self.start = self.enterContext(patch.object(connectors, 'up'))
        self.stop = self.enterContext(patch.object(connectors, 'down'))
        self.configure = self.enterContext(patch.object(connectors, 'configure_running'))

    def apply(self, ids, grants, **extra):
        saved = json.loads((self.state / 'config.json').read_text())
        sandbox.apply_settings(saved, self.state, {'connector_ids': ids, 'connector_operations': grants, **extra})

    def running(self):
        self.inspect.return_value = {'Id': 'workspace-id', 'State': {'Running': True}, 'Mounts': [
            {'Type': 'bind', 'Destination': '/opt/pi-security/config', 'Source': str(self.state / 'runtime-config'),
             'RW': False}]}

    def test_failed_workspace_dependency_never_activates_prepared_worker(self):
        self.apply(['status'], {'status': ['latest']})
        saved = json.loads((self.state / 'config.json').read_text())
        saved.update(workspace='/tmp/workspace', network='unrestricted')
        (self.state / 'allowlist.txt').write_text('')
        self.running()
        with (patch.object(sandbox.folders, 'validate_config'),
              patch.object(sandbox.sandbox_advisors, 'check_ownership'),
              patch.object(sandbox.sandbox_ssh, 'check_ownership'),
              patch.object(sandbox.inference, 'check_ownership'),
              patch.object(sandbox.host_platform, 'needs_mount_check', return_value=False),
              patch.object(connectors, 'prepare', return_value={}) as preflight,
              patch.object(sandbox.sandbox_advisors, 'up', side_effect=ValueError('dependency failed')),
              self.assertRaisesRegex(ValueError, 'dependency failed')):
            sandbox.up(saved, self.state)
        preflight.assert_called_once_with(saved, self.state)
        self.start.assert_not_called()

    def test_failed_probe_restores_only_containers_started_by_this_attempt(self):
        from unittest.mock import call
        running = {'State': {'Running': True}, 'Id': 'old'}
        stopped = {'State': {'Running': False}, 'Id': 'existing-stopped'}
        active = {'State': {'Running': True}, 'Id': 'now-running'}
        self.inspect.return_value = active
        with patch.object(sandbox, 'run') as run:
            run.return_value.returncode = 0
            connector_settings.restore_start(self.config, {'ssh': running, 'inference': stopped, 'dev': None}, ['worker'])
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['podman', 'rm', '-f', 'worker'], ['podman', 'stop', 'now-running'], ['podman', 'stop', 'now-running']])
        self.assertEqual(self.inspect.call_args_list, [call(self.config, 'dev'), call(self.config, 'inference')])

    def test_missing_token_blocks_new_assignment_without_touching_workspace(self):
        for running in (False, True):
            if running:
                self.running()
            self.store.get_password.return_value = None
            with self.assertRaisesRegex(ValueError, 'connector-login status'):
                self.apply(['status'], {'status': ['latest']})
            self.assertEqual(json.loads((self.state / 'config.json').read_text()), self.config)
            self.start.assert_not_called()
            self.stop.assert_not_called()
            self.assertFalse((self.state / 'connector-manifests/status.json').exists())

    def test_changed_manifest_or_invented_operation_cannot_connect(self):
        for extra, selected in [({'expected_connector_routes': {'status': {'latest': 'https://different.test'}}}, ['latest']),
                                ({}, ['not-in-manifest']), ({}, [])]:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.apply(['status'], {'status': selected}, **extra)
        self.start.assert_not_called()
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), self.config)

    def test_omitted_operations_preserve_existing_scope_and_cannot_grant_a_new_service(self):
        with self.assertRaisesRegex(ValueError, 'allowed operations for each new'):
            self.apply(['status'], {})
        self.apply(['status'], {'status': ['latest']})
        self.apply(['status'], {})
        current = json.loads((self.state / 'config.json').read_text())
        self.assertEqual(current['connector_operations'], {'status': ['latest']})
        self.assertEqual(list(connector_settings.operations(current, self.state)['status']), ['latest'])

    def test_same_sandbox_rejects_a_second_editor_with_stale_permissions(self):
        self.apply(['status'], {'status': ['latest']})
        self.apply(['status'], {'status': ['test-results']})
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.apply([], {}, expected_connector_ids=['status'], expected_connector_operations={'status': ['latest']})
        guide = (self.state / 'runtime-config/connectors.md').read_text()
        self.assertIn('/test-results', guide)
        self.assertNotIn('/latest', guide)
        self.start.assert_not_called()
        self.stop.assert_not_called()

    def test_changed_agent_instructions_require_review_before_creation_and_assignment(self):
        plugin = next(item for item in connectors.catalog() if item.id == 'status')
        reviewed_routes = {'status': dict(plugin.routes)}
        instructions = {'status': plugin.reviewed_instructions}
        self.manifest['operations']['latest'] = {'url': dict(plugin.routes)['latest'],
                                                'instructions': 'Use different request instructions.'}
        write_json(self.file, self.manifest)
        self.running()
        with self.assertRaisesRegex(ValueError, 'instructions for the coding agent changed'):
            self.apply(['status'], {'status': ['latest']}, expected_connector_routes=reviewed_routes,
                       expected_connector_instructions=instructions)
        self.start.assert_not_called()
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), self.config)

        review = self.root / 'review.json'
        write_json(review, {'routes': reviewed_routes, 'operations': {'status': ['latest']},
                            'agent_instructions': instructions})
        workspace = self.root / 'workspace'
        workspace.mkdir()
        state = self.root / 'new-sandbox'
        args = sandbox.argparse.Namespace(name=state.name, profile='base', workspace=str(workspace),
                                          pi_config=None, allowlist=None, allow_domain=[], env_file=None,
                                          engine='docker', memory='4g', connector=['status'], connector_review_file=str(review))
        with patch.object(sandbox.host_platform, 'check_engine', return_value=None), \
                patch.object(sandbox, 'choose_engine', return_value='docker'), \
                patch.object(sandbox.os, 'getuid', return_value=1000), patch.object(sandbox.os, 'getgid', return_value=1000):
            with self.assertRaisesRegex(ValueError, 'instructions for the coding agent changed'):
                sandbox.create(args, state)
        self.assertFalse(state.exists())

    def test_rollback_when_saving_a_live_change_fails(self):
        self.running()
        self.apply(['status'], {'status': ['latest']})
        previous = (self.state / 'config.json').read_text()
        guide = self.state / 'runtime-config/connectors.md'
        old_guide = guide.read_text()
        with patch.object(activity, 'save_config', side_effect=OSError('full')), self.assertRaisesRegex(OSError, 'full'):
            self.apply(['status'], {'status': ['test-results']})
        self.assertEqual((self.state / 'config.json').read_text(), previous)
        self.assertEqual(guide.read_text(), old_guide)
        self.assertEqual([args[0]['connector_operations']['status'] for args, _ in self.configure.call_args_list],
                         [['test-results'], ['latest']])

    def test_socket_mount_failure_undoes_new_live_access(self):
        self.running()
        self.probe.side_effect = ValueError('The workspace could not open the socket')
        with self.assertRaisesRegex(ValueError, 'open the socket'):
            self.apply(['status'], {'status': ['latest']})
        self.start.assert_called_once()
        self.stop.assert_called_once()
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), self.config)
        self.assertFalse((self.state / 'runtime-config/connectors.md').exists())

    def test_revoke_even_if_snapshotted_plugin_is_unreadable(self):
        self.apply(['status'], {'status': ['latest']})
        (self.state / 'connector-manifests/status.json').write_text('<unreadable>')
        self.apply([], {})
        self.stop.assert_called_once()
        self.assertFalse((self.state / 'runtime-config/connectors.md').exists())

    def test_unprepared_workspace_is_never_replaced_to_enable_plugin(self):
        self.inspect.return_value = {'Mounts': [], 'State': {'Running': True}}
        with self.assertRaisesRegex(ValueError, 'update the sandbox first'):
            self.apply(['status'], {'status': ['latest']})
        self.start.assert_not_called()
        self.stop.assert_not_called()

    def test_cli_create_with_selected_connector_requests(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        profile = sandbox.load_profile('base')
        self.enterContext(patch.object(sandbox.host_platform, 'check_engine', return_value=None))
        self.enterContext(patch.object(sandbox, 'choose_engine', return_value='docker'))
        self.enterContext(patch.object(sandbox.os, 'getuid', return_value=1000))
        self.enterContext(patch.object(sandbox.os, 'getgid', return_value=1000))
        self.enterContext(patch.object(sandbox, 'snapshot_profile', return_value=profile))
        self.enterContext(patch('ai_sandbox_factory.provider_credentials.os_store')).return_value.get_password.return_value = 'fixture'
        args = sandbox.argparse.Namespace(name='example', profile='base', workspace=str(workspace),
                                          pi_config=None, allowlist=None, allow_domain=[], env_file=None,
                                          engine='docker', memory=None)
        for argument in ('status:unknown', 'status:', 'status:latest,latest', 'status:latest,'):
            args.connector = [argument]
            state = self.root / args.name
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                sandbox.create(args, state)
            self.assertFalse(state.exists())
        for index, (argument, names) in enumerate((('status:latest', ['latest']),
                                                   ('status:latest,test-results', ['latest', 'test-results']),
                                                   ('status', ['latest', 'test-results']))):
            args.name = f'example-{index}'
            args.connector = [argument]
            state = self.root / args.name
            sandbox.create(args, state)
            config = json.loads((state / 'config.json').read_text())
            operations = connector_settings.operations(config, state)
            self.assertEqual(list(operations['status']), names)
            guide = (state / 'runtime-config' / 'connectors.md').read_text()
            self.assertEqual('http://localhost/test-results' in guide, 'test-results' in names)

    def test_cli_creation_explicit_flag_defaults_to_all_operations(self):
        connector_settings.capture(['status'], self.state)
        self.assertEqual(connector_settings.operations({'connector_ids': ['status']}, self.state),
                         {'status': self.manifest['operations']})
