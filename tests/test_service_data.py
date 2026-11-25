"""Recovery of saved profile-service volumes through the host lifecycle operations."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import browser_editor, job_progress, profile_replacement, profile_services, sandbox, service_data, updating


class ServiceDataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.config = {'name': 'fixture', 'engine': 'podman', 'profile': {'digest': 'revision', 'services': {}}}
        self.volumes = {}
        self.containers = {}
        self.calls = []
        specs = profile_services.selected(self.config)
        for name in ('origin', 'target', 'sibling'):
            spec = specs[name] = {'image': 'localhost/fixture', 'volumes': {'data': '/data', 'log': '/log'},
                                  'environment': {'PASSWORD': 'never-copy-this'}}
            for key in spec['volumes']:
                self.volumes[profile_services.data_volume(self.config, name, key)] = name + key
            self.containers[profile_services.container(self.config, name)] = {
                'Id': name, 'Name': profile_services.container(self.config, name),
                'State': {'Running': name != 'sibling'},
                'Config': {'Labels': {profile_services.LABEL_ROLE: profile_services.container(self.config, name)}}}
        volumes = {key: {'volume': profile_services.data_volume(self.config, 'origin', key), 'target': mount}
                   for key, mount in specs['origin']['volumes'].items()}
        with patch.object(sandbox, 'inspect', side_effect=self.inspect):
            service_data.record(self.config, self.config, 'origin', 'removed', volumes)
        self.entry = service_data.entries(self.config)[0]
        self.config['profile_service_retained_volumes'] = [data['volume'] for data in self.entry['volumes'].values()]
        del specs['origin']
        self.containers.pop(profile_services.container(self.config, 'origin'))
        self.old = copy.deepcopy(self.config)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(sandbox, 'inspect', side_effect=self.inspect))
        self.stack.enter_context(patch.object(sandbox, 'run', side_effect=self.run_engine))
        self.stack.enter_context(patch.object(sandbox, 'image_names', return_value=('dev', 'helper')))
        self.stack.enter_context(patch.object(profile_services, 'start_service', side_effect=self.start_target))
        self.ready = self.stack.enter_context(patch.object(profile_services, 'ready'))
        self.storage_error = False
        self.foreign = None

    def inspect(self, config, value):
        return copy.deepcopy(next((info for name, info in self.containers.items()
                                   if name == value or info['Id'] == value), None))

    def run_engine(self, argv, **kwargs):
        self.calls.append(argv)
        op = argv[1]
        output = ''
        status = 0
        detail = ''
        if op in ('volume', 'container', 'network'):
            action, name = argv[2:4]
            if action == 'inspect':
                if name in self.volumes:
                    labels = {sandbox.LABEL: 'other' if name == self.foreign else 'fixture', profile_services.LABEL_ROLE: 'data'}
                    output = json.dumps([{'Labels': labels}])
                else:
                    status, detail = 1, 'no such volume'
            elif action == 'exists':
                status = int(name not in self.volumes and name not in self.containers)
            elif action == 'create':
                self.volumes[argv[-1]] = ''
            elif action == 'rm':
                del self.volumes[name]
            elif action == 'export':
                Path(argv[-1]).write_text(self.volumes[name])
                if self.storage_error:
                    status, detail = 125, 'no space left'
        elif op == 'run':
            source, target = [argv[i + 1] for i, arg in enumerate(argv) if arg == '-v']
            self.assertTrue(source.endswith(':/source:ro'))
            self.volumes[target.split(':')[0]] = self.volumes[source.split(':')[0]]
        elif op in ('rename', 'rm', 'stop', 'start'):
            identifier = argv[-1] if op == 'rm' else argv[2]
            info = self.inspect(None, identifier)
            name = info['Name']
            if op == 'rename':
                self.containers[argv[3]] = self.containers.pop(name)
                self.containers[argv[3]]['Name'] = argv[3]
            elif op == 'rm':
                del self.containers[name]
            else:
                self.containers[name]['State']['Running'] = op == 'start'
        return subprocess.CompletedProcess(argv, status, output, detail)

    def start_target(self, config, state, target):
        self.assertEqual(target, 'target')
        name = profile_services.container(config, target)
        self.containers[name] = {'Id': 'candidate', 'Name': name, 'State': {'Running': True},
                                 'Config': {'Labels': {profile_services.LABEL_ROLE: name}}}

    def restore(self):
        return service_data.restore(self.config, self.state, self.entry['id'], 'target')

    def test_restore_set_on_stopped_service_retains_current_and_source(self):
        self.containers[profile_services.container(self.config, 'target')]['State']['Running'] = False
        before = copy.deepcopy(self.volumes)
        sibling = self.inspect(None, 'sibling')
        config = self.restore()
        self.assertEqual(config, json.loads((self.state / 'config.json').read_text()))
        retained, source = service_data.entries(config)
        self.assertEqual(source, self.entry)
        self.assertEqual((retained['service'], retained['reason']), ('target', 'restored'))
        self.assertNotIn('never-copy-this', json.dumps(retained))
        self.assertNotIn('profile_version', retained)
        self.assertFalse(self.inspect(None, 'candidate')['State']['Running'])
        self.assertEqual(sibling, self.inspect(None, 'sibling'))
        for key, data in retained['volumes'].items():
            self.assertEqual(self.volumes[data['volume']], 'target' + key)
            self.assertEqual(self.volumes[profile_services.data_volume(config, 'target', key)], 'origin' + key)
        for name, value in before.items():
            self.assertEqual(self.volumes[name], value)
        self.ready.assert_called_once()
        self.assertEqual(self.ready.call_args.kwargs['service'], 'target')

    def test_service_only_restore_does_not_stop_or_reopen_workspace_editor(self):
        with patch.object(browser_editor,'replacement_connection') as capture, \
                patch.object(browser_editor,'stop') as stop, patch.object(browser_editor,'resume_connection') as resume:
            self.restore()
            capture.assert_not_called()
            stop.assert_not_called()
            resume.assert_not_called()

    def test_failed_restore_rolls_back_running_target_data_and_catalog(self):
        self.ready.side_effect = ValueError('healthcheck could not run')
        containers, volumes = copy.deepcopy(self.containers), copy.deepcopy(self.volumes)
        with self.assertRaisesRegex(ValueError, 'healthcheck could not run') as failure:
            self.restore()
        self.assertIn('target image', str(failure.exception))
        self.assertIn('saved image', str(failure.exception))
        self.assertTrue(any(argv[1:5] == ['exec', '--user', '0', 'target'] for argv in self.calls))
        self.assertEqual(self.containers, containers)
        self.assertEqual(self.volumes, volumes)
        self.assertEqual(json.loads((self.state / 'config.json').read_text()), self.old)
        self.assertFalse((self.state / 'profile-preparation').exists())

    def test_interrupted_restore_recovery_leaves_unrelated_private_settings_untouched(self):
        containers, volumes = copy.deepcopy(self.containers), copy.deepcopy(self.volumes)
        private = self.state / 'profile-services'
        private.mkdir()
        (private / 'values.json').write_text('{"fixture":"private"}')
        sources = {key: data['volume'] for key, data in self.entry['volumes'].items()}
        updated = copy.deepcopy(self.config)
        transaction = profile_replacement.prepared_services(self.config, updated, self.state,
                                                            restore_target='target', restore_sources=sources)
        transaction.__enter__()
        self.start_target(updated, self.state, 'target')
        sandbox.write_json(self.state / 'config.json', updated)
        recovered = sandbox.recover_profile(updated, self.state)
        self.assertEqual(recovered, self.old)
        self.assertEqual(self.containers, containers)
        self.assertEqual(self.volumes, volumes)
        self.assertEqual((private / 'values.json').read_text(), '{"fixture":"private"}')
        transaction.gen.close()

    def test_cancel_before_commit_restores_target_and_keeps_extra_current_volume(self):
        spec = profile_services.selected(self.config)['target']
        spec['volumes']['extra'] = '/extra'
        volume = profile_services.data_volume(self.config, 'target', 'extra')
        self.volumes[volume] = 'additional data'
        volumes, containers = copy.deepcopy(self.volumes), copy.deepcopy(self.containers)
        gate = self.state / 'cancelled'

        def cancel_after_check(updated, state, **kwargs):
            copy_name = profile_services.data_volume(updated, 'target', 'extra')
            self.assertNotEqual(volume, copy_name)
            self.assertEqual(self.volumes[copy_name], 'additional data')
            gate.mkdir()

        self.ready.side_effect = cancel_after_check
        with self.assertRaises(updating.UpdateCancelled):
            service_data.restore(self.config, self.state, self.entry['id'], 'target', gate)
        self.assertEqual(self.volumes, volumes)
        self.assertEqual(self.containers, containers)

    def test_target_must_declare_saved_keys(self):
        del profile_services.selected(self.config)['target']['volumes']['log']
        with self.assertRaisesRegex(ValueError, 'lacks saved volume keys log'):
            self.restore()
        self.assertEqual(self.calls, [])

    def test_export_removed_service_and_reject_overwrite(self):
        path = self.state / 'saved.tar'
        service_data.export(self.config, self.state, self.entry['id'], 'data', path)
        self.assertEqual(path.read_text(), 'origindata')
        with patch.dict(os.environ, {'HOME': str(self.state)}):
            expanded = service_data.export(self.config, self.state, self.entry['id'], 'log', '~/home.tar')
        self.assertEqual(expanded, self.state / 'home.tar')
        self.assertEqual(expanded.read_text(), 'originlog')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            service_data.export(self.config, self.state, self.entry['id'], 'log', path)
        self.assertEqual(path.read_text(), 'origindata')

    def test_restore_and_export_publish_job_stages(self):
        channel = self.state / 'stage.json'
        observed = []
        self.ready.side_effect = lambda *args, **kwargs: observed.append(json.loads(channel.read_text()))
        with patch.dict(os.environ, {job_progress.ENVIRONMENT: str(channel)}):
            self.restore()
            self.assertEqual(observed, [[2, 3, 'Check restored service']])
            self.assertEqual(json.loads(channel.read_text()), [3, 3, 'Save restored service'])
            service_data.export(self.config, self.state, self.entry['id'], 'data', self.state / 'saved.tar')
            self.assertEqual(json.loads(channel.read_text()), [2, 2, 'Publish archive'])

    def test_failed_export_discards_partial_output(self):
        self.storage_error = True
        with self.assertRaisesRegex(ValueError, 'exit 125.*no space left'):
            service_data.export(self.config, self.state, self.entry['id'], 'data', self.state / 'saved.tar')
        self.assertEqual(list(self.state.iterdir()), [])

    def test_missing_or_foreign_source_never_stops_target(self):
        volume = self.entry['volumes']['data']['volume']
        self.foreign = volume
        with self.assertRaisesRegex(ValueError, 'ownership labels'):
            self.restore()
        del self.volumes[volume]
        with self.assertRaisesRegex(ValueError, 'exit 1.*no such volume'):
            self.restore()
        self.assertTrue(all(argv[1:3] == ['volume', 'inspect'] for argv in self.calls))
