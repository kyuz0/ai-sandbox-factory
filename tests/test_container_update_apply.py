"""A reviewed image update prepares only its targets and preserves unrelated work."""
import copy
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
                advisors.assert_called_once_with(config, state, only=names, check_ready=False)
                services.assert_called_once_with(config, state, only=names)
                self.assertTrue({advisor, controller, profile_services.container(config, 'database')}.isdisjoint(names))
                workspace.assert_not_called()

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

    def test_writing_receipt_requires_exact_prepared_image(self):
        cfg = {'image_artifacts': [{'role': 'gateway', 'image_id': 'expected'}]}
        data = {'State': {'Running': True}, 'Image': 'retagged'}
        with patch.object(sandbox, 'inspect', return_value=data):
            with self.assertRaisesRegex(ValueError, 'expected image expected, found retagged'):
                apply._verify(cfg, {'gateway': {'proxy'}})


if __name__ == '__main__':
    unittest.main()
