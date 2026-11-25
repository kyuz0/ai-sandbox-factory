"""Profile recipes and application lifecycle contracts, without real credentials."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.request import proxy_bypass_environment
from unittest.mock import patch

from ai_sandbox_factory import profile_runtime, profile_services as services, profile_spec, sandbox
from ai_sandbox_factory.sandbox_options import options


class ProfileServicesTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(name='profile-test', engine='podman', network='allowlist', primary_agent='pi',
                           profile=sandbox.load_profile('developer-postgres'), options=options())

    def test_existing_profiles_stay_valid_and_compatibility_is_explicit(self):
        self.assertEqual(profile_spec.services({}, Path('.')), {})
        self.assertTrue(profile_spec.compatible({'name': 'base'}, 'codex'))
        self.assertFalse(profile_spec.compatible({'name': 'old-pi-profile'}, 'codex'))
        for agent in profile_spec.AGENTS:
            self.assertTrue(profile_spec.compatible(self.config['profile'], agent))

    def test_rejected_fields_identify_actual_constraint(self):
        manifest = json.loads((sandbox.ROOT / 'profiles/developer-postgres/profile.json').read_text())
        for field, value, message in [('ports', ['5432:5432'], 'Factory controls'),
                                      ('network_mode', 'host', 'Factory controls'),
                                      ('ssh', {'port': 65536}, '65535')]:
            with self.subTest(field=field):
                invalid = copy.deepcopy(manifest)
                invalid['services']['database'][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    profile_spec.services(invalid, sandbox.ROOT / 'profiles/developer-postgres')
        for name in ('workspace', 'proxy'):
            invalid = copy.deepcopy(manifest)
            invalid['services'][name] = invalid['services'].pop('database')
            with self.assertRaisesRegex(ValueError, 'already.*network name'):
                profile_spec.services(invalid, sandbox.ROOT / 'profiles/developer-postgres')

    def test_command_arguments_preserve_multiline_and_empty_strings(self):
        self.assertEqual(profile_spec._argv(['sh', '-c', 'echo one\necho two', ''], 'healthcheck'),
                         ['sh', '-c', 'echo one\necho two', ''])

    def test_invalid_service_mode_is_rejected_before_conflict_cleanup(self):
        from ai_sandbox_factory import creation
        from contextlib import nullcontext
        draft = dict(self.config, network='unrestricted')
        with tempfile.TemporaryDirectory() as tmp, patch('ai_sandbox_factory.operation_locks.sandbox', return_value=nullcontext()), \
                patch.object(sandbox, 'choose_engine', return_value='podman'), \
                patch('ai_sandbox_factory.host_platform.check_engine', return_value=None), \
                patch.object(creation, 'resource_conflicts') as conflicts:
            with self.assertRaisesRegex(ValueError, 'Restricted'):
                creation.check_draft(Path(tmp), draft, approved=object())
            conflicts.assert_not_called()

    def test_roles_and_volume_names_do_not_alias(self):
        spec = self.config['profile']['services']['database']
        self.config['profile']['services'] = {'a': dict(spec, volumes={'b-c': '/data'}),
                                            'a-b': dict(spec, volumes={'c': '/data'}),
                                            'gateway': dict(spec, volumes={})}
        found = services.resources(self.config)
        self.assertEqual(len(found['container']), 5)
        self.assertEqual(len(found['volume']), 3)
        self.assertNotEqual(services.data_volume(self.config, 'a', 'b-c'), services.data_volume(self.config, 'a-b', 'c'))

    def test_generated_credentials_and_host_keys_survive_repeated_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            services.prepare(self.config, state)
            directory = state / 'profile-services'
            before = {name: (directory / name).read_bytes() for name in
                      ('values.json', 'client/client-key', 'client/known_hosts', 'servers/database/host-key')}
            services.prepare(self.config, state)
            self.assertEqual(before, {name: (directory / name).read_bytes() for name in before})
            config = (directory / 'client/config').read_text()
            for value in ('BatchMode yes', 'StrictHostKeyChecking yes', 'IdentityAgent none', 'ProxyCommand none'):
                self.assertIn(value, config)
            self.assertNotIn('ProxyJump', config)
            host_key=(directory/'servers/database/host-key.pub').read_text().strip()
            self.assertIn(services.hostname('database')+' '+host_key,
                          (directory/'client/known_hosts').read_text().splitlines())
            effective=subprocess.run(['ssh','-G','-F',str(directory/'client/config'),services.hostname('database')],
                                     capture_output=True,text=True,check=True)
            settings=dict(line.split(' ',1) for line in effective.stdout.splitlines())
            self.assertIn(settings['batchmode'],('yes','true'))
            self.assertIn(settings['stricthostkeychecking'],('yes','true'))
            self.assertEqual(settings['userknownhostsfile'],services.CLIENT+'/known_hosts')
            connection = json.loads((directory / 'client/connections.json').read_text())['database']
            self.assertEqual(connection['password'], json.loads((directory / 'values.json').read_text())['database:database-password'])
            self.assertIn('AuthorizedKeysFile /etc/profile-service-authorized-keys',
                          (directory / 'servers/database/sshd_config').read_text())

    def test_container_environment_does_not_configure_host_engine(self):
        environment = {'CONTAINER_HOST': 'ssh://not-the-host-engine', 'MULTILINE': 'line1\nline2'}
        with patch.object(sandbox, 'inspect', return_value=None), patch.object(sandbox, 'run',
                return_value=subprocess.CompletedProcess([], 0, 'id', '')) as run:
            services._start(self.config, 'fixture', ['image'], environment)
        argv = run.call_args.args[0]
        self.assertIn('CONTAINER_HOST=ssh://not-the-host-engine', argv)
        self.assertIn('MULTILINE=line1\nline2', argv)
        self.assertNotIn('env', run.call_args.kwargs)

    def test_internal_http_bypass_keeps_working_when_another_service_is_added(self):
        # Unchanged services keep their existing environment after Apply.
        retained = {'no': services.no_proxy(self.config)}
        self.assertTrue(proxy_bypass_environment('new-service.profile.internal:8080', retained))
        self.assertFalse(proxy_bypass_environment('example.net:443', retained))

    def test_application_passwords_are_service_scoped_and_not_in_agent_instructions(self):
        spec = copy.deepcopy(self.config['profile']['services']['database'])
        self.config['profile']['services']['reporting'] = spec
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'profile-services'
            services.prepare(self.config, Path(tmp))
            connections = json.loads((directory / 'client/connections.json').read_text())
            passwords = {name: entry['password'] for name, entry in connections.items()}
            self.assertNotEqual(passwords['database'], passwords['reporting'])
            for name, password in passwords.items():
                environment = json.loads((directory / f'servers/{name}/environment.json').read_text())
                self.assertEqual(environment['POSTGRES_PASSWORD'], password)
                self.assertNotIn(password, json.dumps(self.config['profile']))
            for agent in ('pi', 'codex', 'claude', 'agy'):
                with self.subTest(agent=agent):
                    text = services.guidance(dict(self.config, primary_agent=agent))
                    self.assertIn('/run/profile-services/connections.json', text)
                    self.assertIn('Read them at runtime', text)
                    self.assertIn('do not put passwords in profile sources or Git', text)
                    for password in passwords.values():
                        self.assertNotIn(password, text)

    def test_start_error_preserves_diagnostic_and_redacts_container_values(self):
        with patch.object(sandbox, 'inspect', return_value=None), patch.object(sandbox, 'run',
                return_value=subprocess.CompletedProcess([], 125, '', 'runtime denied secret-value')):
            with self.assertRaisesRegex(ValueError, 'exit 125.*runtime denied.*redacted') as error:
                services._start(self.config, 'fixture', ['image'], {'TOKEN': 'secret-value'})
        self.assertNotIn('secret-value', str(error.exception))

    def test_inner_policy_is_not_relaxed_for_profile_ssh(self):
        for approval in ('manual', 'off'):
            config = dict(self.config, options=options({'inner_sandbox': True, 'approval': approval}))
            text = services.guidance(config)
            self.assertIn('bash_escalate', text)
            self.assertIn('off' if approval == 'off' else 'existing approval', text)
            self.assertNotIn('ProxyCommand', text)
        for agent in ('codex', 'claude', 'agy'):
            text = services.guidance(dict(self.config, primary_agent=agent))
            self.assertIn('Connect directly to the profile host', text)
            self.assertIn('Follow any shell approval request', text)
            self.assertNotIn('bash_escalate', text)

    def test_ssh_guidance_explains_preconfigured_authentication_for_every_agent(self):
        for agent in ('pi','codex','claude','agy'):
            with self.subTest(agent=agent):
                text=services.guidance(dict(self.config,primary_agent=agent))
                self.assertIn('ssh -F /run/profile-services/config database.profile.internal',text)
                self.assertIn('already enables batch mode and pins each service’s host key',text)
                self.assertIn('without identity or host-key overrides',text)
                self.assertIn('report its actual error',text)
                self.assertNotIn('StrictHostKeyChecking=no',text)

    def test_service_health_waits_for_initialization(self):
        info = {'State': {'Running': True}}
        outcomes = [subprocess.CompletedProcess([], 1, 'initializing', ''),
                    subprocess.CompletedProcess([], 0, 'ready', ''),
                    subprocess.CompletedProcess([], 0, '', '')]
        with patch.object(sandbox, 'inspect', return_value=info), patch.object(services, '_run', side_effect=outcomes), \
                patch.object(services.time, 'sleep') as sleep:
            services.ready(self.config, Path('/unused'))
        sleep.assert_called_once_with(0.5)

    def test_stopped_service_retains_startup_diagnostic(self):
        with patch.object(sandbox, 'inspect', return_value={'State': {'Running': False}}), \
                patch.object(services, '_run', return_value=subprocess.CompletedProcess([], 0, '', 'bad database config')):
            with self.assertRaisesRegex(ValueError, 'database.*bad database config'):
                services.ready(self.config, Path('/unused'))

    def test_rebuild_preserves_service_contract_and_directs_changes_to_reviewed_apply(self):
        original = copy.deepcopy(self.config)
        services.validate_update(original, self.config)
        updated = dict(self.config, profile=sandbox.load_profile('base'))
        with patch.object(sandbox, 'inspect', return_value={'Id': 'running-db'}):
            with self.assertRaisesRegex(ValueError, 'Apply profile changes'):
                services.validate_update(original, updated)

    def test_bad_profile_modes_do_not_affect_ordinary_profiles(self):
        services.validate(dict(self.config, profile=sandbox.load_profile('base'), engine='docker', network='unrestricted'))
        for change in ({'engine': 'docker'}, {'network': 'unrestricted'}):
            with self.assertRaises(ValueError):
                services.validate({**self.config, **change})

    def test_stop_is_graceful_before_container_removal(self):
        with patch.object(services, 'check_ownership'), patch.object(sandbox, 'inspect', return_value={'Id': 'a'}), \
                patch.object(services, '_run') as run:
            services.down(self.config, remove=True)
        calls = [call.args[1:] for call in run.call_args_list]
        for start in range(0, len(calls), 2):
            self.assertEqual(calls[start][0], 'stop')
            self.assertEqual(calls[start+1], ('rm', calls[start][1]))


class AppliedRecipeTests(unittest.TestCase):
    def test_identical_image_bytes_keep_distinct_applied_profile_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile, common = root / 'input', root / 'base'
            profile.mkdir(); common.mkdir()
            (profile / 'Dockerfile').write_text('FROM scratch\n')
            (common / 'Dockerfile').write_text('FROM scratch\n')
            config = {'engine': 'podman', 'primary_agent': 'pi', 'profile': {'name': 'test', 'path': str(profile),
                      'digest': sandbox.tree_digest(profile)}, 'common': {'path': str(common), 'digest': sandbox.tree_digest(common)}}
            with patch.object(sandbox, 'image_names', return_value=('fixture', 'proxy')), patch.object(sandbox, 'run',
                    return_value=subprocess.CompletedProcess([], 0, '[{"Id":"sha256:same-image"}]', '')):
                with profile_runtime.recording(config, root / 'state'):
                    pass
                profile_runtime.select(config, root / 'state')
                first = profile_runtime.applied(root / 'state')
                (profile / 'guide.md').write_text('new guide without changing the image\n')
                config['profile']['digest'] = sandbox.tree_digest(profile)
                with profile_runtime.recording(config, root / 'state'):
                    pass
                profile_runtime.select(config, root / 'state')
                second = profile_runtime.applied(root / 'state')
                self.assertNotEqual(first['path'], second['path'])
                self.assertTrue(Path(second['path'], 'profile/guide.md').exists())
                current = {'Image': 'sha256:same-image', 'Mounts': [{'Destination': profile_runtime.PATH, 'Source': first['path']}]}
                profile_runtime.select(config, root / 'state', current)
                self.assertEqual(profile_runtime.applied(root / 'state')['path'], first['path'])
            self.assertEqual(profile_runtime.mounts(root / 'state')[-1].split(':')[1], profile_runtime.PATH)
            # A running older container without that bind mount must not be told it exists.
            profile_runtime.select(config, root / 'state', {'Image':'sha256:same-image','Mounts':[]})
            self.assertIsNone(profile_runtime.applied(root / 'state'))


    def test_cached_workspace_images_get_a_record_in_each_fresh_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / 'profile'; profile.mkdir()
            common = root / 'base'; common.mkdir()
            (profile / 'Dockerfile').write_text('FROM scratch\n')
            (common / 'Dockerfile').write_text('FROM scratch\n')
            config = {'engine':'podman', 'profile':{'name':'fixture','path':str(profile),'digest':sandbox.tree_digest(profile)},
                      'common':{'path':str(common),'digest':sandbox.tree_digest(common)}}
            with patch.object(sandbox,'image_names',return_value=('fixture','proxy')), patch.object(sandbox,'run',
                    return_value=subprocess.CompletedProcess([],0,'[{"Id":"same-cached-image"}]','')):
                for name in ('first','second'):
                    profile_runtime.ensure_cached(config, root / name)
                    self.assertTrue(profile_runtime.applied(root / name))
                    self.assertTrue(profile_runtime.mounts(root / name))
            self.assertNotEqual(profile_runtime.applied(root/'first')['path'], profile_runtime.applied(root/'second')['path'])

    def test_native_record_includes_only_selected_workspace_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = sandbox.ROOT / 'containers/dev'
            config = {'engine':'podman','primary_agent':'codex','profile':sandbox.load_profile('base'),
                      'options':{'browser':False},'common':{'path':str(common),'digest':sandbox.tree_digest(common)}}
            with patch.object(sandbox,'image_names',return_value=('native-fixture','proxy')), patch.object(sandbox,'run',
                    return_value=subprocess.CompletedProcess([],0,'[{"Id":"native-image"}]','')):
                with profile_runtime.recording(config,root): pass
                profile_runtime.select(config,root)
            folder = Path(profile_runtime.applied(root)['path'])
            self.assertTrue((folder/'workspace/client/codex_client.py').is_file())
            self.assertTrue((folder/'workspace/Dockerfile.codex').is_file())
            self.assertTrue((folder/'base/cli-tools').is_dir())
            self.assertFalse((folder/'base/bootstrap.py').exists())
            self.assertFalse((folder/'workspace/client/Dockerfile').exists())

    def test_native_source_change_during_build_cannot_publish_a_mismatched_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'factory'
            worker = source / 'containers/primary-worker'; worker.mkdir(parents=True)
            client = source / 'containers/primary'; client.mkdir()
            (worker / 'Dockerfile').write_text('FROM original\n')
            for name in ('worker.sh','worker-tools.mjs','codex_client.py'):
                (client / name).write_text('original\n')
            profile = root / 'profile'; profile.mkdir()
            common = root / 'common'; common.mkdir()
            config = {'engine':'podman','primary_agent':'codex','options':{'browser':False},
                      'profile':{'name':'fixture','path':str(profile),'digest':sandbox.tree_digest(profile)},
                      'common':{'path':str(common),'digest':sandbox.tree_digest(common)}}
            for changed in (worker/'Dockerfile',client/'worker-tools.mjs'):
                with self.subTest(changed=changed.name), patch.object(sandbox,'ROOT',source):
                    state = root / changed.name
                    with self.assertRaisesRegex(ValueError,'coding-agent container sources changed'):
                        with profile_runtime.recording(config,state):
                            changed.write_text('changed while building\n')
                    self.assertFalse(list((state/'applied-profiles').iterdir()))
                    changed.write_text('FROM original\n' if changed.name=='Dockerfile' else 'original\n')
