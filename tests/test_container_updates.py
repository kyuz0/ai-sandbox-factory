"""Applied container versions, source provenance and offline update eligibility."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_sandbox_factory import container_updates as updates, image_sources, profile_runtime, sandbox


class UpdatePlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.build = self.state / 'applied'
        self.build.mkdir()
        self.config = dict(name='example', engine='podman', uid=1000, gid=1000, network='allowlist',
                           profile=dict(name='base', registry_name='base', factory_profile='base',
                                        factory_profile_digest='profile-1', path=str(self.build), digest='snapshot-1'),
                           common=dict(path=str(self.build), digest='common-1'), image_source='local')
        self.target = copy.deepcopy(self.config)
        self.target['profile']['factory_profile_digest'] = 'profile-1'
        self.roles = {'workspace': {'main'}, 'gateway': {'gateway'}}
        self.live = {'main': dict(Id='main-1', Image='image-main-1', Mounts=[dict(Destination=profile_runtime.PATH, Source=str(self.build))]),
                     'gateway': dict(Id='gateway-1', Image='image-gateway-1')}
        self.artifact = dict(role='gateway', image_id='image-gateway-1', recipe='recipe-1', source='local',
                             reference='image-gateway-1', platform='linux/amd64')
        self.metadata = dict(image_id='image-main-1', common_inputs_digest='common-1', workspace_layers={'common': 'common-1'},
                             factory_profile='base', factory_profile_digest='profile-1', image_artifacts=[self.artifact])
        self.save_build()
        stack = self.enterContext(ExitStack())
        self.target_mock = stack.enter_context(patch.object(updates, '_target', side_effect=lambda *a: copy.deepcopy(self.target)))
        stack.enter_context(patch.object(updates, 'runtime_roles', return_value=self.roles))
        self.recipe = stack.enter_context(patch.object(image_sources, 'recipe', return_value='recipe-1'))
        self.inspect = stack.enter_context(patch.object(sandbox, 'inspect', side_effect=lambda c, n: self.live.get(n)))
        self.index = stack.enter_context(patch.object(image_sources, 'index', return_value={}))
        stack.enter_context(patch.object(profile_runtime, 'applied', side_effect=lambda s: self.metadata))
        self.run = stack.enter_context(patch.object(sandbox, 'run', side_effect=AssertionError('Status must not run an engine command')))

    def save_build(self):
        (self.build / 'build.json').write_text(json.dumps(self.metadata))

    def plan(self):
        return updates.plan(self.config, self.state)

    def test_current_local_build_does_not_read_registry_or_write_state(self):
        before = sorted(self.state.rglob('*'))
        self.assertEqual(self.plan()['status'], 'current')
        self.index.assert_not_called()
        self.assertEqual(sorted(self.state.rglob('*')), before)
        self.run.assert_not_called()

    def test_changed_recipe_flags_only_used_helper_without_workspace_restart(self):
        self.recipe.return_value = 'recipe-2'
        plan = self.plan()
        self.assertEqual(plan['roles'], ['gateway'])
        self.assertFalse(plan['workspace'])
        self.assertEqual(plan['status'], 'available')
        self.assertIn('Only the listed', plan['effects'][0])

    def test_changed_installed_common_or_builtin_recipe_replaces_workspace(self):
        for part, field in [('common', 'digest'), ('profile', 'factory_profile_digest')]:
            with self.subTest(part=part):
                old = self.target[part][field]
                self.target[part][field] = 'new'
                plan = self.plan()
                self.assertEqual(plan['roles'], ['workspace'])
                self.assertTrue(plan['workspace'])
                self.target[part][field] = old

    def test_prepared_build_does_not_hide_old_applied_version(self):
        self.target['common']['digest'] = 'common-2'
        self.config['common']['digest'] = 'common-2'
        self.config['image_refs'] = {'workspace': 'image-main-2'}
        self.config['image_artifacts'] = [{**self.artifact, 'recipe': 'recipe-2'}]
        self.assertEqual(self.plan()['roles'], ['workspace'])
        updates.record_applied(self.config, self.state)
        self.assertEqual(self.plan()['roles'], ['workspace'])
        receipt = updates._receipt(self.state)
        self.assertEqual(receipt['workspace']['common_digest'], 'common-1')
        self.assertNotIn('gateway', receipt)  # A prepared artifact is not proof of replacement.

    def test_disconnecting_aws_keeps_workspace_current_and_still_compares_used_layers(self):
        self.config.update(aws_access={}, model_providers=[])
        self.target.update(self.config)
        self.metadata['workspace_layers'] = updates.workspace_layers(
            {**self.config, 'aws_access': {'profile': 'synthetic'}}, stock=False)
        self.save_build()
        self.assertIn('aws', self.metadata['workspace_layers'])
        self.assertEqual(self.plan()['status'], 'current')
        self.metadata['workspace_layers']['providers'] = 'previous-provider-layer'
        self.save_build()
        self.assertEqual(self.plan()['roles'], ['workspace'])

    def test_newly_prepared_stock_image_cannot_mask_a_mounted_nonstock_provider_update(self):
        self.target.update(model_providers=[], image_artifacts=[{'role': 'pi-stock'}])
        self.metadata['workspace_layers']['providers'] = 'previous-provider-layer'
        self.save_build()
        self.assertEqual(self.plan()['roles'], ['workspace'])
        self.metadata['image_artifacts'].append({**self.artifact, 'role': 'pi-stock'})
        self.target['image_artifacts'] = []
        self.save_build()
        self.assertEqual(self.plan()['status'], 'current')

    def test_missing_layer_evidence_is_unknown_and_keeps_known_positive_changes(self):
        self.target['model_providers'] = []
        self.assertEqual(self.plan()['status'], 'unknown')
        self.target['common']['digest'] = 'new-common'
        result = self.plan()
        self.assertEqual(result['roles'], ['workspace'])
        self.assertTrue(result['unknown'])

    def test_repair_after_helper_only_update_recovers_its_distinct_live_image(self):
        self.config['image_artifacts'] = [{**self.artifact, 'image_id': 'image-gateway-2', 'recipe': 'recipe-2'}]
        self.live['gateway'].update(Id='gateway-2', Image='image-gateway-2')
        self.recipe.return_value = 'recipe-2'
        labels = image_sources.container_labels(self.config, 'gateway')
        self.live['gateway']['Config'] = {'Labels': dict(value.split('=', 1) for value in labels[1::2])}
        updates.record_applied(self.config, self.state, roles_updated=['gateway'])
        receipt = self.state / 'container-updates/applied.json'
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    receipt.write_text('[]')
                else:
                    receipt.unlink()
                updates.record_applied(self.config, self.state)
                self.assertEqual(self.plan()['status'], 'current')
                self.assertEqual(updates._receipt(self.state)['gateway']['recipe'], 'recipe-2')

    def test_repair_never_credits_same_image_prepared_recipe_over_container_proof(self):
        artifact = {**self.artifact, 'recipe': 'recipe-B', 'image_id': 'image-B', 'reference': 'image-B'}
        self.config['image_artifacts'] = [artifact]
        labels = image_sources.container_labels(self.config, 'gateway')
        self.live['gateway'].update(Id='container-B', Image='image-B',
            Config={'Labels': dict(value.split('=', 1) for value in labels[1::2])})
        updates.record_applied(self.config, self.state, roles_updated=['gateway'])
        self.config['image_artifacts'] = [{**artifact, 'recipe': 'recipe-C'}]
        self.recipe.return_value = 'recipe-C'
        receipt = self.state / 'container-updates/applied.json'
        for corrupt in (False, True):
            if corrupt:
                receipt.write_text('[]')
            else:
                receipt.unlink()
            updates.record_applied(self.config, self.state)
            self.assertEqual(updates._receipt(self.state)['gateway']['recipe'], 'recipe-B')
            self.assertEqual(self.plan()['roles'], ['gateway'])

    def test_never_started_sandbox_requests_start_not_workspace_replacement(self):
        self.live.clear()
        plan = self.plan()
        self.assertEqual(plan['status'], 'unknown')
        self.assertIn('Start this sandbox', plan['reason'])
        self.assertNotIn('Rebuild this sandbox', plan['reason'])

    def test_shared_gateway_recipe_is_compared_for_every_consumer_even_when_image_bytes_match(self):
        def label(name, artifact):
            args = image_sources.container_labels({'image_artifacts': [artifact]}, 'gateway')
            self.live[name]['Config'] = {'Labels': dict(option.split('=', 1) for option in args[1::2])}
        older, current = self.artifact, {**self.artifact, 'recipe': 'recipe-2'}
        self.recipe.return_value = current['recipe']
        label('gateway', older)
        self.config['image_artifacts'] = [older]
        updates.record_applied(self.config, self.state)
        self.roles['gateway'].add('advisor-proxy')
        self.live['advisor-proxy'] = dict(Id='new-proxy', Image=older['image_id'])
        self.config['image_artifacts'] = [current]
        label('advisor-proxy', current)
        updates.record_applied(self.config, self.state)
        # The previous receipt can still identify the original gateway. It
        # must never claim both containers run the new recipe.
        receipt = updates._receipt(self.state)['gateway']
        self.assertEqual(receipt['recipe'], older['recipe'])
        self.assertEqual(set(receipt['containers']), {'gateway'})
        plan = self.plan()
        self.assertEqual(plan['roles'], ['gateway'])
        self.assertFalse(plan['unknown'])
        (self.state / 'container-updates/applied.json').unlink()
        updates.record_applied(self.config, self.state)
        self.assertNotIn('gateway', updates._receipt(self.state))
        self.assertEqual(self.plan()['roles'], ['gateway'])
        label('gateway', current)
        updates.record_applied(self.config, self.state)
        self.assertEqual(set(updates._receipt(self.state)['gateway']['containers']), self.roles['gateway'])
        self.assertEqual(self.plan()['status'], 'current')

    def test_unlabelled_peer_uses_only_its_own_receipt_alongside_labelled_consumers(self):
        self.config['image_artifacts'] = [self.artifact]
        updates.record_applied(self.config, self.state, roles_updated=['gateway'])
        self.roles['gateway'].add('advisor-proxy')
        current = {**self.artifact, 'recipe': 'recipe-2'}
        args = image_sources.container_labels({'image_artifacts': [current]}, 'gateway')
        labels = dict(option.split('=', 1) for option in args[1::2])
        self.live['advisor-proxy'] = dict(Id='new-proxy', Image=current['image_id'], Config={'Labels': labels})
        self.config['image_artifacts'] = [current]
        self.recipe.return_value = 'recipe-2'
        self.assertEqual(self.plan()['roles'], ['gateway'])
        self.assertFalse(self.plan()['unknown'])
        self.live['gateway']['Id'] = 'different-unlabelled-container'
        updates.record_applied(self.config, self.state)
        self.assertEqual(self.plan()['status'], 'unknown')
        self.recipe.return_value = 'recipe-3'
        self.assertEqual(self.plan()['roles'], ['gateway'])
        self.assertTrue(self.plan()['unknown'])
        del self.live['gateway']
        self.assertEqual(self.plan()['roles'], ['gateway'])
        self.assertTrue(self.plan()['unknown'])

    def test_helper_receipt_advances_only_after_actual_replacement(self):
        self.config['image_artifacts'] = [{**self.artifact, 'image_id': 'image-gateway-2', 'recipe': 'recipe-2'}]
        self.recipe.return_value = 'recipe-2'
        updates.record_applied(self.config, self.state)
        self.assertEqual(self.plan()['roles'], ['gateway'])
        self.live['gateway'].update(Id='gateway-2', Image='image-gateway-2')
        updates.record_applied(self.config, self.state, roles_updated=['gateway'])
        self.assertEqual(self.plan()['status'], 'current')
        self.assertEqual(updates._receipt(self.state)['gateway']['containers']['gateway']['id'], 'gateway-2')

    def test_same_recipe_new_tested_digest_is_available_for_prebuilt_only(self):
        self.artifact.update(source='prebuilt', reference='registry/image@sha256:' + 'a' * 64)
        self.save_build()
        self.config['image_source'] = 'prebuilt'
        self.index.return_value = {'recipe-1': {'linux/amd64': 'registry/image@sha256:' + 'b' * 64}}
        self.assertEqual(self.plan()['roles'], ['gateway'])
        self.artifact.update(source='local')
        self.save_build()
        self.assertEqual(self.plan()['status'], 'current')

    def test_acquisition_preference_alone_is_not_an_update(self):
        self.config['image_source'] = 'prebuilt'
        self.index.return_value = {'recipe-1': {'linux/amd64': 'registry/image@sha256:' + 'b' * 64}}
        self.assertEqual(self.plan()['status'], 'current')

    def test_missing_or_wrong_mount_never_uses_newer_pointer(self):
        self.live['main']['Mounts'] = []
        self.assertEqual(self.plan()['status'], 'unknown')
        self.assertIn('applied build record', self.plan()['reason'])
        self.assertNotIn('revision', updates.plan({'name': 'custom', 'profile': {'name': 'base'}}, self.state))

    def test_unknown_helper_does_not_hide_known_workspace_update(self):
        del self.live['gateway']
        self.target['common']['digest'] = 'common-2'
        plan = self.plan()
        self.assertEqual(plan['status'], 'available')
        self.assertEqual(plan['roles'], ['workspace'])
        self.assertTrue(plan['unknown'])

    def test_revision_changes_with_container_identity_or_installed_target(self):
        first = self.plan()['revision']
        self.live['gateway']['Id'] = 'replacement'
        self.assertNotEqual(self.plan()['revision'], first)
        self.live['gateway']['Id'] = 'gateway-1'
        self.target['common']['digest'] = 'common-2'
        self.assertNotEqual(self.plan()['revision'], first)

    def test_custom_named_base_and_editable_profiles_are_owner_maintained(self):
        for profile, extra in [(dict(name='base', registry_name='/tmp/base'), {}),
                               (self.config['profile'], {'editable_profile': {'path': '/tmp/edit'}}),
                               ({**self.config['profile'], 'customized_from': {'name': 'base'}}, {})]:
            with self.subTest(profile=profile, extra=extra):
                self.target_mock.reset_mock()
                config = {**self.config, 'profile': profile, **extra}
                plan = updates.plan(config, self.state)
                self.assertEqual(plan['status'], 'custom')
                self.assertFalse(plan['roles'])
                self.target_mock.assert_not_called()

    def test_malformed_build_record_has_actionable_diagnostic(self):
        (self.build / 'build.json').write_text('{bad')
        plan = self.plan()
        self.assertEqual(plan['status'], 'unknown')
        self.assertIn('example', plan['reason'])
        self.assertIn('Expecting', plan['reason'])


    def test_wrong_workspace_image_cannot_offer_updates_from_its_base_artifacts(self):
        self.metadata['image_artifacts'].append(dict(role='pi-stock', recipe='obsolete', image_id='other-image', source='local'))
        self.metadata['image_id'] = 'other-image'
        self.save_build()
        plan = self.plan()
        self.assertEqual(plan['status'], 'unknown')
        self.assertFalse(plan['workspace'])
        self.assertEqual(plan['roles'], [])

    def test_malformed_receipt_and_catalog_shapes_are_actionable(self):
        receipt = self.state / 'container-updates/applied.json'
        receipt.parent.mkdir()
        for value in ([], {'version': 1, 'roles': {'gateway': []}}):
            with self.subTest(value=value):
                receipt.write_text(json.dumps(value))
                result = self.plan()
                self.assertEqual(result['status'], 'unknown')
                self.assertIn(str(receipt), result['reason'])
        receipt.unlink()
        self.config['image_source'] = 'prebuilt'
        for value in (None, []):
            self.index.return_value = {'recipe-1': value}
            self.assertEqual(self.plan()['status'], 'unknown')
            self.assertIn('map platforms', self.plan()['reason'])
        (self.build / 'build.json').write_text('[]')
        self.assertEqual(self.plan()['status'], 'unknown')
        self.assertIn('build.json', self.plan()['reason'])

    def test_recording_repairs_malformed_derived_receipt_but_retains_storage_errors(self):
        receipt = self.state / 'container-updates/applied.json'
        receipt.parent.mkdir()
        receipt.write_text('[]')
        self.config['image_artifacts'] = [self.artifact]
        updates.record_applied(self.config, self.state)
        self.assertEqual(self.plan()['status'], 'current')
        with patch.object(updates, '_receipt', side_effect=PermissionError('read denied')):
            with self.assertRaisesRegex(PermissionError, 'read denied'):
                updates.record_applied(self.config, self.state)

    def test_engine_image_id_spelling_does_not_create_a_false_unknown(self):
        self.metadata['image_id'] = 'sha256:' + 'a' * 64
        self.live['main']['Image'] = 'a' * 64
        self.save_build()
        self.assertEqual(self.plan()['status'], 'current')


class RuntimeRoleTests(unittest.TestCase):
    def test_all_actual_gateway_instances_are_included(self):
        config = dict(name='example', primary_agent='codex', network='allowlist',
                      advisors={'providers': ['claude']}, profile={'services': {'database': {}}})
        roles = updates.runtime_roles(config)
        self.assertEqual(len(roles['gateway']), 5)
        self.assertIn('controller-codex', roles)
        self.assertNotIn('ssh', roles)
        self.assertNotIn('inference', roles)

    def test_unrestricted_pi_has_no_unused_helper_roles(self):
        roles = updates.runtime_roles(dict(name='example', network='unrestricted'))
        self.assertEqual(list(roles), ['workspace'])

    def test_pi_non_stock_layers_track_only_enabled_factory_additions(self):
        from ai_sandbox_factory import model_providers
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ('providers/provider-runner.mjs', 'providers/provider-lock.mjs',
                     'model_providers.py', 'containers/aws-access/client/client.py')
            for name in paths:
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('version-1')
            with patch.object(sandbox, 'ROOT', root), patch.object(model_providers, '__file__', str(root / 'model_providers.py')):
                bare = dict(primary_agent='pi', common={'digest': 'common-1'})
                enabled = {**bare, 'model_providers': [], 'aws_access': {'enabled': True}}
                original = updates.workspace_layers(enabled, stock=False)
                for name in paths:
                    file = root / name
                    file.write_text('version-2')
                    self.assertNotEqual(updates.workspace_layers(enabled, stock=False), original, name)
                    self.assertEqual(updates.workspace_layers(bare, stock=False), {'common': 'common-1'}, name)
                    file.write_text('version-1')

    def test_stock_settings_changes_do_not_claim_a_software_update(self):
        config = dict(primary_agent='pi', common={'digest': 'common-1'},
                      image_artifacts=[{'role': 'pi-stock'}])
        self.assertEqual(updates.workspace_layers(config, stock=True), updates.workspace_layers({
            **config, 'aws_access': {'enabled': True}, 'model_providers': []}, stock=True))

    def test_native_workspace_ignores_pi_and_disabled_browser_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            common = Path(directory)
            for name in ('cli-tools', 'browser', 'security', 'pi-tools'):
                (common / name).mkdir()
                (common / name / 'source').write_text('version-1')
            cfg = dict(primary_agent='codex', common={'path': str(common), 'digest': 'common-1'}, options={'browser': False})
            original = updates.workspace_layers(cfg, stock=False)
            (common / 'pi-tools/source').write_text('version-2')
            (common / 'browser/source').write_text('version-2')
            self.assertEqual(updates.workspace_layers(cfg, stock=False), original)
            cfg['options']['browser'] = True
            browser = updates.workspace_layers(cfg, stock=False)
            (common / 'browser/source').write_text('version-3')
            self.assertNotEqual(updates.workspace_layers(cfg, stock=False), browser)
            cfg['options']['browser'] = False
            (common / 'cli-tools/source').write_text('version-2')
            self.assertNotEqual(updates.workspace_layers(cfg, stock=False), original)

    def test_bundled_identity_is_assigned_by_location_not_metadata_name(self):
        for identifier in updates.BUILTINS:
            profile = sandbox.load_profile(identifier)
            self.assertEqual(profile['factory_profile'], identifier)
            self.assertEqual(profile['factory_profile_digest'], profile['digest'])
