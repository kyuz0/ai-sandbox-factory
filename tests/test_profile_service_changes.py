"""Service reconciliation retains recoverable data and restores interrupted changes."""
import copy
import errno
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import profile_replacement, profile_services, sandbox, updating


class ServiceChanges(unittest.TestCase):
    def test_ownership_is_checked_before_stopping_existing_services(self):
        config = {'name':'fixture','engine':'podman','profile':sandbox.load_profile('developer-postgres')}
        updated = copy.deepcopy(config)
        updated['profile']['services'] = {}
        with tempfile.TemporaryDirectory() as tmp, patch.object(profile_services,'check_ownership',
                side_effect=ValueError('Container database is not a profile service')), patch.object(sandbox,'run') as run:
            with self.assertRaisesRegex(ValueError,'not a profile service'):
                with profile_replacement.prepared_services(config,updated,Path(tmp)):
                    self.fail('An unowned container reached preparation')
            run.assert_not_called()

    def test_changed_service_records_built_image_and_unchanged_service_keeps_running_image(self):
        original = {'profile': sandbox.load_profile('developer-postgres')}
        original['profile']['services']['reporting'] = copy.deepcopy(original['profile']['services']['database'])
        updated = copy.deepcopy(original)
        updated['profile']['services']['database']['environment']['VERSION'] = 'two'
        with patch.object(sandbox, 'inspect', return_value={'Image': 'previous-image'}), \
                patch.object(profile_services, 'image', return_value='built-service-image'), \
                patch.object(profile_services, 'container', side_effect=lambda config, name: name), \
                patch.object(profile_services, '_run', return_value=subprocess.CompletedProcess([],0,'[{"Id":"new-image"}]','')):
            self.assertEqual(profile_services.resolved_images(updated, original),
                             {'database':'new-image','reporting':'previous-image'})

    def exercise(self, *, remove=False, fail=False, copy_failure=False, copy_cancel=False, stopped=False,
                 previously_replaced=False, missing_volume=False, commit_failure=False):
        original = {'name': 'fixture', 'engine': 'podman', 'primary_agent': 'pi',
                    'profile': sandbox.load_profile('developer-postgres')}
        if previously_replaced:
            original['profile_service_volumes'] = {'database': {'data':
                profile_services.data_volume_base(original, 'database', 'data') + '-revision-previous'}}
        updated = copy.deepcopy(original)
        if remove:
            updated['profile']['services'] = {}
        else:
            updated['profile']['services']['database']['environment']['EXAMPLE_VERSION'] = 'two'
        name = profile_services.container(original, 'database')
        old_volume = profile_services.data_volume(original, 'database', 'data')
        containers = {name: {'Id': 'previous-database', 'Name': name, 'State': {'Running': not stopped},
                            'Config': {'Labels': {profile_services.LABEL_ROLE: name}}}}
        before = copy.deepcopy(containers)
        volumes = {} if missing_volume else {old_volume: b'original database'}
        calls = []

        def inspect(config, value):
            return copy.deepcopy(next((entry for key, entry in containers.items()
                                       if key == value or entry['Id'] == value), None))

        def run(argv, **kwargs):
            calls.append(argv)
            operation = argv[1]
            if operation in ('container', 'volume', 'network'):
                if argv[2] == 'exists':
                    exists = argv[3] in (containers if operation == 'container' else volumes if operation == 'volume' else ())
                    return subprocess.CompletedProcess(argv, 0 if exists else 1, '', '')
                if argv[2] == 'inspect':
                    return subprocess.CompletedProcess(argv, 0, json.dumps([{'Labels': {sandbox.LABEL: 'fixture'}}]), '')
                if argv[2] == 'create':
                    volumes[argv[-1]] = b''
                elif argv[2] == 'rm':
                    volumes.pop(argv[3], None)
            elif operation in ('stop', 'start', 'rename', 'rm'):
                identifier = argv[-1] if operation == 'rm' else argv[2]
                key = next(key for key, entry in containers.items() if key == identifier or entry['Id'] == identifier)
                if operation == 'rename':
                    entry = containers.pop(key)
                    entry['Name'] = argv[3]
                    containers[argv[3]] = entry
                elif operation == 'rm':
                    containers.pop(key)
                else:
                    containers[key]['State']['Running'] = operation == 'start'
            elif operation == 'run':
                if copy_failure:
                    raise subprocess.CalledProcessError(125, argv, stderr='volume copy I/O error')
                mounts = [argv[index + 1] for index, value in enumerate(argv) if value == '-v']
                source, target = (mount.split(':')[0] for mount in mounts)
                if copy_cancel:
                    helper = argv[argv.index('--name') + 1]
                    containers[helper] = {'Id':'copy-worker','Name':helper,'State':{'Running':True},
                                          'Config':{'Labels':{profile_services.LABEL_ROLE:helper}}}
                    volumes[target] = b'partial copy'
                    raise updating.UpdateCancelled()
                volumes[target] = volumes[source]
            return subprocess.CompletedProcess(argv, 0, '', '')

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'allowlist.txt').write_text('')
            (state / 'squid.conf').write_text('old policy')
            directory = state / 'profile-services'
            directory.mkdir()
            (directory / 'values.json').write_text('{"database:password":"synthetic-value"}')
            write_json = sandbox.write_json
            def publish(path, value):
                if commit_failure and Path(path).name == 'record.json' and value.get('committed'):
                    raise OSError(errno.ENOSPC,'No space left while committing profile settings')
                return write_json(path, value)
            with patch.object(sandbox, 'inspect', side_effect=inspect), patch.object(sandbox, 'run', side_effect=run), \
                    patch.object(sandbox, 'image_names', return_value=('workspace-image', 'copy-image')), \
                    patch.object(sandbox, 'write_json', side_effect=publish):
                def transaction():
                    with profile_replacement.prepared_services(original, updated, state):
                        self.assertFalse(inspect(original, 'previous-database')['State']['Running'])
                        if not remove:
                            candidate_volume = profile_services.data_volume(updated, 'database', 'data')
                            self.assertNotEqual(candidate_volume, old_volume)
                            self.assertEqual(candidate_volume.count('-revision-'), 1)
                            self.assertEqual(volumes[candidate_volume], b'original database')
                            self.assertEqual(volumes[old_volume], b'original database')
                            volumes[candidate_volume] = b'candidate migration'
                            containers[name] = {'Id': 'candidate-database', 'Name': name,
                                                'State': {'Running': True},
                                                'Config': {'Labels': {profile_services.LABEL_ROLE: name}}}
                        (directory / 'values.json').write_text('{"wrong":"candidate"}')
                        if fail:
                            raise ValueError('candidate failed after changing its data')
                if fail or copy_failure or copy_cancel or missing_volume or commit_failure:
                    with self.assertRaises((OSError, ValueError, subprocess.CalledProcessError, updating.UpdateCancelled)) as failure:
                        transaction()
                    if copy_failure:
                        self.assertIn(old_volume, str(failure.exception))
                        self.assertIn('exit 125', str(failure.exception))
                        self.assertIn('volume copy I/O error', str(failure.exception))
                    if missing_volume:
                        self.assertIn(old_volume, str(failure.exception))
                        self.assertIn('volume is missing', str(failure.exception))
                    if commit_failure:
                        message = str(failure.exception)
                        for value in ('commit profile changes', 'fixture', 'record.json', 'No space left', 'were restored',
                                      'Free storage space', 'retry the same profile review'):
                            self.assertIn(value, message)
                        self.assertIsInstance(failure.exception.__cause__, OSError)
                    self.assertEqual(containers, before)
                    self.assertEqual(volumes, {} if missing_volume else {old_volume: b'original database'})
                    self.assertEqual(json.loads((directory / 'values.json').read_text()), {'database:password': 'synthetic-value'})
                else:
                    transaction()
                    self.assertFalse(any(entry['Id'] == 'previous-database' for entry in containers.values()))
                    self.assertEqual(volumes[old_volume], b'original database')
                    self.assertIn(old_volume, updated['profile_service_retained_volumes'])
                    if remove:
                        self.assertNotIn(name, containers)
                        self.assertEqual(profile_services.resources(updated)['volume'], {old_volume})
                    else:
                        self.assertEqual(containers[name]['State']['Running'], not stopped)
                        self.assertEqual(volumes[profile_services.data_volume(updated, 'database', 'data')], b'candidate migration')
                self.assertFalse((state / 'profile-preparation').exists())
        self.assertFalse(any(argv[1:3] == ['volume', 'rm'] and argv[3] == old_volume for argv in calls))

    def test_replacement_copies_data_and_retires_old_container(self):
        self.exercise()

    def test_replacement_preserves_stopped_state(self):
        self.exercise(stopped=True)

    def test_repeated_replacement_uses_the_current_data_without_growing_volume_names(self):
        self.exercise(previously_replaced=True)

    def test_failed_candidate_cannot_modify_previous_database(self):
        self.exercise(fail=True)

    def test_copy_failure_restores_running_original(self):
        self.exercise(copy_failure=True)

    def test_interrupted_copy_removes_its_running_helper_and_partial_volume(self):
        self.exercise(copy_cancel=True)

    def test_missing_previous_data_is_reported_instead_of_silently_starting_empty(self):
        self.exercise(missing_volume=True)

    def test_failed_commit_restores_previous_runtime_immediately(self):
        self.exercise(commit_failure=True)

    def test_removal_keeps_data_even_without_remaining_services(self):
        self.exercise(remove=True)

    def test_failed_removal_restores_exact_original_container(self):
        self.exercise(remove=True, fail=True)

    def test_delete_removes_retained_data_when_no_services_remain(self):
        config = {'profile': {'services': {}}, 'profile_service_retained_volumes': ['previous-data']}
        with patch.object(profile_services, 'check_ownership'), patch.object(profile_services, '_run',
                return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            profile_services.delete(config)
        self.assertTrue(any(call.args[1:] == ('volume', 'rm', 'previous-data') for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
