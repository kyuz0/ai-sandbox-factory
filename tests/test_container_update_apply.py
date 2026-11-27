"""A reviewed image update prepares only its targets and preserves unrelated work."""
import copy
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_sandbox_factory import (container_update_apply as apply, container_updates, image_sources,
                                profile_services, sandbox, sandbox_advisors, primary_agents)


class ContainerUpdateApplyTests(unittest.TestCase):
    def test_gateway_updates_all_egress_containers_without_starting_workers_or_apps(self):
        config = {'name': 'fixture', 'primary_agent': 'codex', 'profile': sandbox.load_profile('developer-postgres')}
        advisor, advisor_proxy = sandbox_advisors.names(config)[:2]
        controller, controller_proxy = primary_agents.names(config)[:2]
        gateway = sandbox.names(config)[1]
        base = profile_services.prefix(config)
        names = {gateway, advisor_proxy, controller_proxy, base + '-gateway', base + '-proxy'}
        with TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'allowlist.txt').write_text('')
            with patch.object(sandbox, 'create_gateway') as main, patch.object(primary_agents, 'up') as native, \
                 patch.object(sandbox_advisors, 'up') as advisors, patch.object(profile_services, 'up') as services, \
                 patch.object(sandbox, 'up') as workspace:
                apply._launch(config, state, {'gateway': names}, {}, {})
                main.assert_called_once_with(config, state, [])
                native.assert_called_once_with(config, state, only=names, check_ready=False)
                advisors.assert_called_once_with(config, state, only=names)
                services.assert_called_once_with(config, state, only=names)
                self.assertTrue({advisor, controller, profile_services.container(config, 'database')}.isdisjoint(names))
                workspace.assert_not_called()

    def test_stopped_advisor_is_probed_before_retiring_previous_container(self):
        config = {'name': 'demo', 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                  'advisors': {'providers': ['codex']}, 'image_refs': {'advisor': 'previous-image'}}
        target = {**config, 'image_refs': {'advisor': 'candidate-image'},
                  'image_artifacts': [{'role': 'advisor', 'image_id': 'candidate-image'}]}
        plan = dict(status='available', revision='reviewed', roles=['advisor'], workspace=False, profile_services=False)
        advisor, proxy = sandbox_advisors.names(config)[:2]
        workspace = sandbox.names(config)[0]
        for healthy in (False, True):
            with self.subTest(healthy=healthy), TemporaryDirectory() as directory:
                state = Path(directory)
                sandbox.write_json(state / 'config.json', config)
                containers = {name: {'Name': name, 'Id': 'old-' + name, 'Image': 'previous-image',
                                     'State': {'Running': name == workspace}}
                              for name in (advisor, proxy, workspace)}
                previous = copy.deepcopy(containers)
                probes = []
                def inspect(cfg, key):
                    info = containers.get(key) or next((i for i in containers.values() if i['Id'] == key), None)
                    return copy.deepcopy(info)

                def run(args, **kwargs):
                    op, *rest = args[1:]
                    result = SimpleNamespace(stdout='', stderr='', returncode=0)
                    if op == 'run':
                        self.assertEqual(rest[rest.index('--name') + 1], advisor)
                        containers[advisor] = {'Name': advisor, 'Id': 'candidate', 'Image': 'candidate-image',
                                               'State': {'Running': True}}
                    elif op == 'exec':
                        self.assertEqual(rest[0], advisor)
                        self.assertEqual(rest[-2:], ['/run/advisor/advisor.sock', '/run/advisor-bus/bus'])
                        probes.append(containers[advisor]['Id'])
                        result.returncode = 0 if healthy else 1
                        result.stderr = '' if healthy else '/run/advisor/advisor.sock: [Errno 111] Connection refused'
                    elif op in ('rm', 'rename', 'stop'):
                        identifier = rest[1] if rest[0] == '-f' else rest[0]
                        key = next(n for n, i in containers.items() if i['Id'] == identifier)
                        if op == 'rename':
                            info = containers.pop(key)
                            info['Name'] = rest[1]
                            containers[rest[1]] = info
                        elif op == 'stop':
                            containers[key]['State']['Running'] = False
                        else:
                            containers.pop(key)
                    elif op == 'volume':
                        result.stdout = json.dumps([{'Labels': {sandbox.LABEL: 'demo'}}])
                    return result

                with patch.object(container_updates, 'plan', return_value=plan), \
                     patch.object(container_updates, 'target_config', return_value=copy.deepcopy(target)), \
                     patch.object(sandbox, 'image_graph', return_value=nullcontext()), patch.object(apply, '_prepare'), \
                     patch.object(sandbox, 'inspect', side_effect=inspect), patch.object(sandbox, 'run', side_effect=run), \
                     patch.object(sandbox_advisors.time, 'sleep'), patch.object(container_updates, 'record_applied') as record:
                    if healthy:
                        apply.apply(config, state, plan)
                        record.assert_called_once()
                        self.assertEqual(containers[advisor]['Id'], 'candidate')
                    else:
                        with self.assertRaisesRegex(ValueError, r'Advisor did not become ready.*status 1:.*Connection refused.*podman logs'):
                            apply.apply(config, state, plan)
                        record.assert_not_called()
                        self.assertEqual(containers, previous)
                        self.assertEqual(json.loads((state / 'config.json').read_text()), config)
                self.assertTrue(probes)
                self.assertEqual(set(probes), {'candidate'})
                self.assertFalse(containers[advisor]['State']['Running'])
                self.assertEqual(containers[workspace], previous[workspace])
                self.assertEqual(containers[proxy], previous[proxy])

    def test_helper_images_use_current_recipe_without_advancing_workspace_or_other_artifacts(self):
        cfg = {'engine': 'fixture', 'common': {'path': 'applied'},
               'image_refs': {'workspace': 'workspace-id', 'gateway': 'old', 'ssh': 'original'},
               'image_artifacts': [{'role': 'gateway'}, {'role': 'ssh', 'reference': 'original'}]}
        original = copy.deepcopy(cfg)
        def gateway(build):
            self.assertEqual(build['common'], {'path': 'installed'})
            self.assertNotIn('gateway', build['image_refs'])
            build['image_refs']['gateway'] = 'replacement'
            image_sources._used.get()['gateway'] = {'role': 'gateway', 'image_id': 'replacement'}
        with patch.object(sandbox, 'common_profile', return_value={'path': 'installed'}), \
             patch.object(apply.image_builds, 'gateway', side_effect=gateway), \
             patch.object(apply.operation_locks, 'proxy_image', return_value=nullcontext()):
            apply._prepare(cfg, None, {'workspace': False}, {'gateway'})
        self.assertEqual(cfg['common'], original['common'])
        self.assertEqual(cfg['image_refs'], {**original['image_refs'], 'gateway': 'replacement'})
        self.assertEqual(cfg['image_artifacts'], [original['image_artifacts'][1],
                                                 {'role': 'gateway', 'image_id': 'replacement'}])

    def test_changed_revision_after_preparation_and_preparation_failure_do_not_interrupt_anything(self):
        config = {'name': 'fixture'}
        reviewed = {'status': 'available', 'revision': 'reviewed', 'workspace': False, 'roles': ['gateway']}
        for failure in ('build', 'review', 'cancel'):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                gate = Path(directory) / 'gate'
                if failure == 'cancel':
                    gate.mkdir()
                plans = [reviewed, {**reviewed, 'revision': 'changed' if failure == 'review' else 'reviewed'}]
                with patch.object(container_updates, 'plan', side_effect=plans), \
                     patch.object(container_updates, 'target_config', return_value=config.copy()), \
                     patch.object(container_updates, 'runtime_roles', return_value={'gateway': {'proxy'}}), \
                     patch.object(sandbox, 'image_graph', return_value=nullcontext()), \
                     patch.object(apply, '_prepare', side_effect=ValueError('could not pull image') if failure == 'build' else None), \
                     patch.object(apply.profile_replacement, 'prepared_services') as transaction:
                    with self.assertRaises(apply.updating.UpdateCancelled if failure == 'cancel' else ValueError):
                        apply.apply(config, Path(directory), reviewed, apply_gate=gate)
                    transaction.assert_not_called()

    def test_stopped_helper_only_update_restores_its_state_and_checks_identity_before_publishing(self):
        config = {'name': 'fixture', 'engine': 'podman', 'image_refs': {'gateway': 'image-new'},
                  'image_artifacts': [{'role': 'gateway', 'image_id': 'sha256:image-new'}]}
        reviewed = dict(status='available', revision='known', workspace=False, roles=['gateway'])
        old = {'Id': 'old', 'Image': 'image-old', 'State': {'Running': False}}
        new = {'Id': 'new', 'Image': 'image-new', 'State': {'Running': True}}
        with TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(container_updates, 'plan', return_value=reviewed), \
                 patch.object(container_updates, 'target_config', return_value=config.copy()), \
                 patch.object(container_updates, 'runtime_roles', return_value={'gateway': {'proxy'}}), \
                 patch.object(sandbox, 'image_graph', return_value=nullcontext()), patch.object(apply, '_prepare'), \
                 patch.object(apply.profile_replacement, 'prepared_services', return_value=nullcontext(lambda: None)), \
                 patch.object(sandbox, 'inspect', side_effect=[old, new, new]), patch.object(apply, '_launch'), \
                 patch.object(sandbox, 'run') as run, patch.object(apply.activity, 'save_config'), \
                 patch.object(container_updates, 'record_applied') as publish, patch.object(sandbox, 'up') as up:
                apply.apply(config, state, reviewed)
                self.assertEqual(run.call_args_list[0].args[0][:3], ['podman', 'rename', 'old'])
                run.assert_called_with(['podman', 'stop', 'new'])
                publish.assert_called_once_with(config, state, roles_updated=['gateway'])
                up.assert_not_called()

    def test_builtin_postgres_service_image_change_uses_journal_and_candidate_readiness_before_workspace(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            installed = sandbox.load_profile('developer-postgres')
            previous = state / 'prior-profile'
            shutil.copytree(installed['path'], previous)
            previous.joinpath('services/database/Dockerfile').write_text('FROM postgres:17\n')
            config = {'name': 'fixture', 'engine': 'podman', 'profile': {
                **installed, 'path': str(previous), 'digest': sandbox.tree_digest(previous)}}
            updated = {**config, 'profile': installed}
            reviewed = {'revision': 'reviewed', 'status': 'available', 'workspace': True,
                        'profile_services': True, 'roles': ['workspace']}
            changes = profile_services.changes(config, updated)
            self.assertEqual(changes, {'added_services': [], 'removed_services': [], 'replaced_services': ['database']})
            workspace, gateway = sandbox.names(config)[:2]
            role_names = {'workspace': {workspace}, 'gateway': {gateway}}
            old_db = {'Id': 'previous-db', 'Image': 'previous-image', 'State': {'Running': True}}
            events = []
            @contextmanager
            def journal(old, target, path, **options):
                self.assertIs(old, config)
                self.assertIs(target, updated)
                self.assertEqual(options['replacement_names'], {workspace, gateway})
                self.assertTrue(options['defer_preparation'])
                yield lambda: events.append('prepare service data')
                events.append('commit joint transaction')

            def select(cfg, path):
                # A changed service recipe resolves its prepared target even
                # while the original database container still owns its name.
                with patch.object(profile_services, '_run',
                                  return_value=SimpleNamespace(stdout='[{"Id":"updated-database-image"}]')) as inspect:
                    self.assertEqual(profile_services.resolved_images(cfg, config), {'database': 'updated-database-image'})
                    inspect.assert_called_once_with(cfg, 'image', 'inspect',
                                                    profile_services.image(cfg, 'database', installed['services']['database']),
                                                    capture=True)
                events.append('select candidate images')

            def cutover(old, target, path, **options):
                self.assertEqual(options, {'retain_previous': True, 'allow_service_changes': True,
                                          'workspace_only': True})
                events.append('replace workspace')

            with patch.object(container_updates, 'plan', return_value=reviewed), \
                 patch.object(container_updates, 'target_config', return_value=updated), \
                 patch.object(container_updates, 'runtime_roles', return_value=role_names), \
                 patch.object(sandbox, 'image_graph', return_value=nullcontext()), \
                 patch.object(sandbox, 'inspect', return_value=old_db), patch.object(apply, '_prepare'), \
                 patch.object(apply.profile_replacement, 'prepared_services', side_effect=journal), \
                 patch.object(apply.profile_runtime, 'select', side_effect=select), \
                 patch.object(profile_services, 'up', side_effect=lambda *args: events.append('start candidate service')), \
                 patch.object(profile_services, 'ready', side_effect=lambda *args, **kw: events.append('service healthy')), \
                 patch.object(apply.profile_replacement, 'apply', side_effect=cutover), \
                 patch.object(container_updates, 'record_applied'):
                apply.apply(config, state, reviewed)
            self.assertEqual(events, ['prepare service data', 'select candidate images', 'start candidate service',
                                      'service healthy', 'replace workspace', 'commit joint transaction'])

    def test_rollback_reseeds_only_restarted_private_processes_by_their_retained_ids(self):
        from ai_sandbox_factory.connectors import runtime
        cfg = {'name': 'fixture', 'engine': 'podman'}
        aws, connector = 'fixture-aws', 'fixture-connector'
        def info(identifier, running, started):
            return {'Id': identifier, 'State': {'Running': running, 'StartedAt': started}}
        originals = {aws: info('original-aws', True, 'before'),
                     connector: info('original-connector', True, 'before')}
        roles = {'aws': {aws}, 'connector': {connector}}
        credentials = {'aws': {'synthetic': 'session'}, 'connector': {'example': ('scope', 'synthetic')}}
        with patch.object(container_updates, 'runtime_roles', return_value=roles), \
             patch.object(sandbox, 'inspect', side_effect=[info('original-aws', True, 'before'),
                                                          info('original-connector', False, 'before')]), \
             patch.object(sandbox, 'run') as run, patch.object(runtime, 'restore_token') as seed, \
             patch.object(apply.aws_access, 'restore_session') as aws_seed:
            apply.restore_runtime(cfg, 'state', originals, credentials, lambda label, action: action())
            run.assert_called_once_with(['podman', 'start', 'original-connector'])
            seed.assert_called_once_with(cfg, 'state', originals[connector], prepared=credentials['connector'])
            aws_seed.assert_not_called()

    def test_rollback_replays_in_memory_credentials_after_a_partial_process_restart(self):
        from ai_sandbox_factory.connectors import runtime
        config = {'name': 'fixture', 'engine': 'podman', 'network': 'unrestricted', 'connector_ids': ['status'],
                  'aws_access': {'region': 'us-east-1', 'profile': 'fixture', 'role': 'arn:aws:iam::123456789012:role/test',
                                 'grants': [{'Action': 'ec2:DescribeRegions', 'Resource': '*'}]}}
        signer, broker = apply.aws_access.worker_name(config), runtime.worker_name(config, 'status')
        plugin = SimpleNamespace(id='status', account='example-scope')
        session = {'SessionToken': 'synthetic-private-session', 'Expiration': '2030-01-01T00:00:00Z'}
        token = 'synthetic-private-token'
        def worker(name, identifier, labels):
            return {'Name': name, 'Id': identifier, 'Config': {'Labels': labels},
                    'State': {'Running': True, 'StartedAt': 'before'}}
        live = {signer: worker(signer, 'original-signer', {'io.pi-sandbox-factory.aws': 'identity'}),
                broker: worker(broker, 'original-broker', {runtime.ROLE: plugin.id, runtime.SCOPE: plugin.account})}
        def inspect(cfg, key):
            return copy.deepcopy(live.get(key) or next((i for i in live.values() if i['Id'] == key), None))
        with TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(sandbox, 'inspect', side_effect=inspect), \
                 patch.object(sandbox, 'run', return_value=SimpleNamespace(stdout='', stderr='', returncode=0)) as engine, \
                 patch.object(runtime, 'saved', return_value=[plugin]), patch.object(runtime, 'permitted_routes', return_value=[]), \
                 patch.object(runtime, '_owned', return_value=live[broker]), patch.object(runtime, '_token', return_value=token) as keyring, \
                 patch.object(runtime, '_deliver') as broker_seed, patch.object(apply.aws_access, '_deliver') as aws_seed, \
                 patch.object(apply.aws_access, 'ensure_renewal') as renew, patch.object(apply.aws_access, 'assume') as assume:
                self.assertEqual(runtime.prepare(config, state), {})
                credentials = {'aws': session, 'connector': runtime.prepare(config, state, replacing=True)}
                self.assertEqual(credentials['connector'], {'status': (plugin.account, token)})
                with self.assertRaisesRegex(ValueError, 'candidate unavailable'):
                    with apply.profile_replacement.prepared_services(
                            config, copy.deepcopy(config), state, replacement_names={signer, broker}, credentials=credentials):
                        journal = (state / 'profile-preparation/record.json').read_text()
                        self.assertNotIn(session['SessionToken'], journal)
                        self.assertNotIn(token, journal)
                        for item in live.values():
                            item['State']['StartedAt'] = 'partially-recovered'
                        raise ValueError('candidate unavailable')
                for identifier in ('original-signer', 'original-broker'):
                    calls = [call.args[0][1] for call in engine.call_args_list if call.args[0][-1] == identifier]
                    self.assertEqual(calls, ['stop', 'start'])
                aws_seed.assert_called_once_with('podman', 'original-signer', 'us-east-1', session)
                renew.assert_called_once_with('podman', 'original-signer', state / 'aws-access/identity.json',
                                              'fixture', session['Expiration'])
                broker_seed.assert_called_once_with('podman', 'original-broker', {'routes': {}, 'token': token}, plugin)
                keyring.assert_called_once_with(plugin)
                assume.assert_not_called()
                self.assertFalse((state / 'profile-preparation').exists())

    def test_writing_receipt_requires_exact_prepared_image(self):
        cfg = {'image_artifacts': [{'role': 'gateway', 'image_id': 'expected'}]}
        data = {'State': {'Running': True}, 'Image': 'retagged'}
        with patch.object(sandbox, 'inspect', return_value=data):
            with self.assertRaisesRegex(ValueError, 'expected image expected, found retagged'):
                apply._verify(cfg, {'gateway': {'proxy'}})


if __name__ == '__main__':
    unittest.main()
