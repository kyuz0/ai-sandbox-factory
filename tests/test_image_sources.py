"""Image acquisition uses the saved recipes and keeps runtime references stable."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_sandbox_factory import (
    aws_access, connector_settings, connectors, hosted_models, image_builds, image_sources, inference,
    operation_locks, operations, primary_agents, profile_replacement,
    profile_runtime, sandbox, updating,
)


PUBLISHED = 'docker.io/example/factory-gateway@sha256:' + 'a' * 64
IMAGE_ID = 'sha256:' + 'b' * 64
LOCAL = 'localhost/factory-test-gateway:1'


class Engine:
    def __init__(self):
        self.images = {}
        self.calls = []
        self.arch = 'x86_64'
        self.on_pull = None
        self.inspect_error = None

    def run(self, args, **kwargs):
        self.calls.append(tuple(args))
        status, out, error = 0, '', ''
        if args[1] == 'info':
            out = self.arch
        elif args[1:3] == ['image', 'inspect']:
            if self.inspect_error:
                status, error = self.inspect_error
            elif args[3] in self.images:
                out = json.dumps([self.images[args[3]]])
            else:
                status, error = 125, f'Error: {args[3]}: image not known'
        elif args[1] == 'pull':
            if self.on_pull:
                self.on_pull()
            self.images[args[2]] = self.verified(args[2])
        else:
            raise AssertionError(f'Unexpected engine invocation: {args}')
        return subprocess.CompletedProcess(args, status, out, error)

    @staticmethod
    def verified(reference):
        return {'Id': IMAGE_ID, 'RepoDigests': [reference], 'Os': 'linux', 'Architecture': 'amd64'}


class ImageSourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(self.root / 'library')))
        self.config = {'name': 'image-fixture', 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                       'workspace': str(self.root), 'network': 'unrestricted', 'image_source': 'prebuilt'}
        self.engine = Engine()
        self.enterContext(patch.object(sandbox, 'run', side_effect=self.engine.run))
        self.enterContext(patch.object(operation_locks, 'private_runtime_dir', return_value=self.root))
        self.index = self.root / 'images.json'
        self.enterContext(patch.object(image_sources, 'INDEX', self.index))
        self.recipe = image_sources.recipe(self.config, 'gateway')
        self.publish({self.recipe: {'linux/amd64': PUBLISHED}})
        image_sources._platform.cache_clear()
        self.addCleanup(image_sources._platform.cache_clear)
        self.log = self.enterContext(redirect_stdout(io.StringIO()))

    def publish(self, images):
        self.index.write_text(json.dumps({'version': 1, 'images': images}))

    def prepare(self, config=None, builder=None):
        return image_sources.ensure(config if config is not None else self.config, 'gateway', LOCAL, builder)

    def test_docker_inspect_familiar_names_must_identify_the_catalogue_repository_and_digest(self):
        digest = '@sha256:' + 'a' * 64
        cases = (
            ('docker.io/kyuz0/example', 'kyuz0/example', digest, True),
            ('docker.io/library/ubuntu', 'ubuntu', digest, True),
            ('docker.io/library/ubuntu', 'library/ubuntu', digest, True),
            ('docker.io/kyuz0/example', 'other/example', digest, False),
            ('docker.io/kyuz0/example', 'registry.example/kyuz0/example', digest, False),
            ('docker.io/kyuz0/example', 'kyuz0/example', '@sha256:' + 'c' * 64, False),
            ('registry.example/kyuz0/example', 'kyuz0/example', digest, False),
            ('docker.io/localhost/example', 'localhost/example', digest, False),
        )
        for repository, spelling, reported_digest, matches in cases:
            with self.subTest(repository=repository, spelling=spelling, digest=reported_digest):
                selected = repository + digest
                self.publish({self.recipe: {'linux/amd64': selected}})
                self.engine.images[selected] = self.engine.verified(spelling + reported_digest)
                config = {**self.config, 'engine': 'docker'}
                with image_sources.recording() as used:
                    if matches:
                        self.assertEqual(self.prepare(config), selected)
                        self.assertEqual(config['image_refs']['gateway'], selected)
                        self.assertEqual(used['gateway']['reference'], selected)
                    else:
                        with self.assertRaisesRegex(ValueError, 'Cannot verify gateway image'):
                            self.prepare(config)
                        self.assertNotIn('image_refs', config)
                        self.assertEqual(used, {})

    def test_cached_and_downloaded_digest_record_the_same_artifact(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                self.engine.images.clear()
                self.engine.calls.clear()
                if cached:
                    self.engine.images[PUBLISHED] = self.engine.verified(PUBLISHED)
                config = dict(self.config)
                build = Mock()
                with image_sources.recording() as used:
                    self.assertEqual(self.prepare(config, build), PUBLISHED)
                    self.assertEqual(used['gateway'], {'role': 'gateway', 'recipe': self.recipe,
                                                      'reference': PUBLISHED, 'image_id': IMAGE_ID})
                self.assertEqual(config['image_refs'], {'gateway': PUBLISHED})
                self.assertEqual(self.engine.calls.count(('podman', 'pull', PUBLISHED)), int(not cached))
                build.assert_not_called()

    def test_source_change_during_pull_leaves_no_artifact_or_runtime_reference(self):
        source = self.root / 'containers/proxy'
        source.mkdir(parents=True)
        dockerfile = source / 'Dockerfile'
        dockerfile.write_text('FROM fixture-one\n')
        with patch.object(sandbox, 'ROOT', self.root):
            original = image_sources.recipe(self.config, 'gateway')
            self.publish({original: {'linux/amd64': PUBLISHED}})
            self.engine.on_pull = lambda: dockerfile.write_text('FROM fixture-two\n')
            with image_sources.recording() as used:
                with self.assertRaisesRegex(ValueError, 'gateway image sources changed') as failure:
                    self.prepare()
                self.assertIn(PUBLISHED, str(failure.exception))
                self.assertEqual(used, {})
                self.assertNotIn('image_refs', self.config)
                current = image_sources.recipe(self.config, 'gateway')
                self.assertNotEqual(current, original)
                self.publish({current: {'linux/amd64': PUBLISHED}})
                self.assertEqual(self.prepare(), PUBLISHED)
                self.assertEqual(used['gateway']['recipe'], current)

    def test_unpublished_recipe_or_platform_runs_the_existing_builder(self):
        for index in ({}, {'different-recipe': {'linux/amd64': PUBLISHED}},
                      {self.recipe: {'linux/arm64': PUBLISHED}}):
            with self.subTest(index=index):
                self.publish(index)
                self.engine.calls.clear()
                config = dict(self.config)
                build = Mock(side_effect=lambda: self.engine.images.update({LOCAL: {'Id': IMAGE_ID}}))
                self.assertEqual(self.prepare(config, build), LOCAL)
                build.assert_called_once_with()
                self.assertEqual(config['image_refs']['gateway'], IMAGE_ID)
                self.assertEqual(image_sources.reference(config, 'gateway', LOCAL), IMAGE_ID)
                self.assertFalse(any(call[1] == 'pull' for call in self.engine.calls))
                self.assertIn('gateway', self.log.getvalue())
        self.assertIsNone(image_sources.ensure(self.config, 'pi-stock', None))

    def test_local_override_runs_builder_even_with_cached_published_image(self):
        self.engine.images[PUBLISHED] = self.engine.verified(PUBLISHED)
        self.index.unlink()
        build = Mock(side_effect=lambda: self.engine.images.update({LOCAL: {'Id': IMAGE_ID}}))
        self.assertEqual(image_sources.mode({}), 'local')
        with image_sources.override('local'):
            self.assertEqual(self.prepare(builder=build), LOCAL)
        self.assertEqual(image_sources.mode(self.config), 'prebuilt')
        self.assertEqual(self.config['image_refs']['gateway'], IMAGE_ID)
        self.assertEqual(self.engine.calls, [('podman', 'image', 'inspect', LOCAL)])
        build.assert_called_once_with()

    def test_preparation_errors_keep_previous_refs_and_allow_retry(self):
        stale = 'sha256:' + 'c' * 64
        self.config['image_refs'] = {'gateway': stale}
        build = Mock()
        with patch.object(sandbox, 'run', wraps=self.engine.run) as run:
            def denied(args, **kwargs):
                if args[1] == 'pull':
                    return subprocess.CompletedProcess(args, 125, '', 'HTTP 429: registry rate limit exceeded')
                return self.engine.run(args, **kwargs)
            run.side_effect = denied
            with image_sources.recording() as used, self.assertRaises(ValueError) as failure:
                self.prepare(builder=build)
            self.assertEqual(used, {})
        for term in ('gateway', PUBLISHED, '125', 'HTTP 429', 'rate limit', 'locally'):
            self.assertIn(term, str(failure.exception))
        self.assertEqual(self.config['image_refs'], {'gateway': stale})
        build.assert_not_called()
        self.assertEqual(self.prepare(), PUBLISHED)

    def test_wrong_manifest_platform_or_missing_image_id_is_never_accepted(self):
        for invalid in ({'RepoDigests': [PUBLISHED.replace('a' * 64, 'd' * 64)]}, {'RepoDigests': None},
                        {'Architecture': 'arm64'}, {'Os': 'windows'}, {'Id': ''}):
            with self.subTest(invalid=invalid):
                self.engine.images[PUBLISHED] = {**self.engine.verified(PUBLISHED), **invalid}
                config = dict(self.config)
                with image_sources.recording() as used, self.assertRaises(ValueError) as failure:
                    self.prepare(config)
                self.assertIn(PUBLISHED, str(failure.exception))
                self.assertEqual(used, {})
                self.assertNotIn('image_refs', config)

    def test_invalid_catalogue_or_digest_reports_the_rejected_input(self):
        for value, reason in (([], 'version 1'), ({'version': 2, 'images': {}}, 'version 1'),
                              ({'version': 1, 'images': {self.recipe: ['linux/amd64']}}, 'platform'),
                              ({'version': 1, 'images': {self.recipe: {'linux/amd64': 'example:latest'}}},
                               'example:latest')):
            with self.subTest(value=value):
                self.index.write_text(json.dumps(value))
                with self.assertRaises(ValueError) as failure:
                    self.prepare()
                self.assertIn(reason, str(failure.exception))
                self.assertNotIn('image_refs', self.config)
        self.assertFalse(any(call[1] == 'pull' for call in self.engine.calls))

    def test_inspection_transport_error_retains_cause_without_pulling(self):
        self.engine.inspect_error = 125, 'Cannot connect to Podman socket: connection refused'
        with self.assertRaises(ValueError) as failure:
            self.prepare()
        for term in ('125', 'Podman socket', 'connection refused', PUBLISHED):
            self.assertIn(term, str(failure.exception))
        self.assertFalse(any(call[1] == 'pull' for call in self.engine.calls))

    def test_cancelled_pull_releases_lock_for_retry(self):
        self.engine.on_pull = Mock(side_effect=updating.UpdateCancelled)
        with image_sources.recording() as used, self.assertRaises(updating.UpdateCancelled):
            self.prepare()
        self.assertEqual(used, {})
        self.assertNotIn('image_refs', self.config)
        self.engine.on_pull = None
        self.assertEqual(self.prepare(), PUBLISHED)

    def test_concurrent_requests_share_one_verified_download(self):
        pulling, waiting, release = Event(), Event(), Event()
        flock = operation_locks.fcntl.flock
        def observe_lock(fd, mode):
            if mode == operation_locks.fcntl.LOCK_EX:
                waiting.set()
            return flock(fd, mode)
        def hold_pull():
            pulling.set()
            if not release.wait(10):
                raise AssertionError('test did not release the simulated registry')
        self.engine.on_pull = hold_pull
        first_config, second_config = dict(self.config), dict(self.config)
        with patch.object(operation_locks.fcntl, 'flock', side_effect=observe_lock), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.prepare, first_config)
            try:
                self.assertTrue(pulling.wait(10))
                second = pool.submit(self.prepare, second_config)
                self.assertTrue(waiting.wait(10), 'second request never waited for the active download')
            finally:
                release.set()
            self.assertEqual((first.result(), second.result()), (PUBLISHED, PUBLISHED))
        self.assertEqual(self.engine.calls.count(('podman', 'pull', PUBLISHED)), 1)
        self.assertEqual(first_config['image_refs'], second_config['image_refs'])

    def test_cooperative_cancellation_while_waiting_leaves_the_active_pull_running(self):
        pulling, waiting, cancel, release = Event(), Event(), Event(), Event()
        def hold_pull():
            pulling.set()
            if not release.wait(10):
                raise AssertionError('test did not release the simulated registry')
        def cancelled():
            waiting.set()
            return cancel.is_set()
        waiter = dict(self.config)
        def prepare_waiter():
            with sandbox.cancellable_build(cancelled):
                return self.prepare(waiter)
        self.engine.on_pull = hold_pull
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.prepare)
            try:
                self.assertTrue(pulling.wait(10))
                second = pool.submit(prepare_waiter)
                self.assertTrue(waiting.wait(10), 'the second job never reached the waiting lock')
                cancel.set()
                with self.assertRaises(updating.UpdateCancelled):
                    second.result(timeout=5)
                self.assertFalse(first.done())
                self.assertNotIn('image_refs', waiter)
            finally:
                release.set()
            self.assertEqual(first.result(), PUBLISHED)
        self.assertEqual(self.prepare(waiter), PUBLISHED)
        self.assertEqual(self.engine.calls.count(('podman', 'pull', PUBLISHED)), 1)

    def test_image_preference_edit_preserves_runtime_images_and_preview(self):
        state = self.root / self.config['name']
        state.mkdir()
        service = operations.Service(self.root)
        self.config.update(image_source='local', image_refs={'workspace': IMAGE_ID, 'gateway': LOCAL})
        saved = state / 'config.json'
        saved.write_text(json.dumps(self.config))
        changes = {'image_source': 'prebuilt'}
        preview = service.preview_settings(self.config['name'], changes)
        self.assertEqual(preview['runtime_effect'], 'next-build')
        sandbox.apply_settings(self.config, state, changes)
        config = json.loads(saved.read_text())
        self.assertEqual(config['image_source'], 'prebuilt')
        self.assertEqual(config['image_refs'], self.config['image_refs'])
        self.assertEqual(sandbox.image_names(config), (IMAGE_ID, LOCAL))
        self.assertEqual(service.preview_settings(self.config['name'], changes)['runtime_effect'], 'none')
        # Older sandboxes without recorded refs still resume their existing local role.
        self.assertEqual(image_sources.reference({'image_source': 'prebuilt', 'engine': 'podman'},
                                                 'gateway', LOCAL), LOCAL)
        self.assertEqual(self.engine.calls, [])

    def test_runtime_can_restore_a_missing_legacy_tag_but_preserves_an_explicit_reference(self):
        def build_local(config):
            self.assertEqual(image_sources.mode(config), 'local')
            self.engine.images[LOCAL] = {'Id': IMAGE_ID}
        build = Mock(side_effect=build_local)
        with image_sources.override('prebuilt'):
            self.assertEqual(image_sources.runtime(self.config, 'gateway', LOCAL, build), LOCAL)
        self.assertEqual(image_sources.mode(self.config), 'prebuilt')
        self.assertNotIn('image_refs', self.config)
        self.config['image_refs'] = {'gateway': PUBLISHED}
        with self.assertRaises(ValueError) as failure:
            image_sources.runtime(self.config, 'gateway', LOCAL, build)
        self.assertIn(PUBLISHED, str(failure.exception))
        self.assertIn('build', str(failure.exception))
        self.assertTrue(all(call[1:3] == ('image', 'inspect') for call in self.engine.calls))
        self.assertEqual(build.call_count, 1)

    def test_start_prepares_a_saved_new_sandbox_even_if_a_legacy_tag_is_cached(self):
        service = operations.Service(self.root)
        self.config['image_refs'] = {}
        steps = []
        def start_step(args, log):
            steps.append(args[0])
            if args[0] == 'build':
                self.config['image_refs'] = {'workspace': IMAGE_ID}
        with patch.object(service, 'config', return_value=self.config), \
                patch.object(sandbox, 'inspect', return_value=None) as container, \
                patch.object(sandbox, 'image_names', return_value=(IMAGE_ID, LOCAL)), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as cached, \
                patch.object(service, 'run', side_effect=start_step):
            service.start(self.config['name'], print)
            self.assertEqual(steps, ['build', 'up'])
            cached.assert_not_called()
            self.config['image_refs'] = {'connector': PUBLISHED}
            steps.clear()
            service.start(self.config['name'], print)
            self.assertEqual(steps, ['build', 'up'])
            cached.assert_not_called()
            service.start(self.config['name'], print)
            self.assertEqual(steps, ['build', 'up', 'up'])
            del self.config['image_refs']  # Older sandboxes resume using their original cached tag.
            service.start(self.config['name'], print)
            self.config['image_refs'] = {}
            container.return_value = {'State': {'Running': False}}
            service.start(self.config['name'], print)
            self.assertEqual(steps, ['build', 'up', 'up', 'up', 'up'])

    def test_recreate_with_only_an_image_override_builds_the_saved_snapshots(self):
        state = self.root / self.config['name']
        state.mkdir()
        self.config.update(profile=sandbox.snapshot_profile(sandbox.load_profile('base'), state),
                           common=sandbox.snapshot_profile(sandbox.common_profile(), state),
                           model_providers=[{'name': 'fixture', 'path': str(state / 'saved-provider'),
                                             'digest': 'saved-revision', 'settings': {}}])
        saved = state / 'config.json'
        args = SimpleNamespace(command='recreate', image_source='local', profile=None, subagents=None,
                               browser=None, apply_gate=None)
        for settings in ({}, {'primary_agent': 'codex'}, {'editable_profile': {'path': str(state / 'edit')}},
                         {'profile': {**self.config['profile'], 'customized_from': 'original'}}):
            expected = {**self.config, **settings}
            saved.write_text(json.dumps(expected))
            def build_config(candidate, directory):
                self.assertEqual(image_sources.mode(candidate), 'local')
                self.assertEqual(candidate, expected)
                self.assertEqual(directory, state)
            with self.subTest(settings=settings), \
                    patch.object(sandbox, 'common_profile', side_effect=AssertionError('unexpected source refresh')), \
                    patch.object(sandbox.model_providers, 'prepare', side_effect=AssertionError('unexpected provider refresh')), \
                    patch.object(sandbox, 'build', side_effect=build_config) as build, \
                    patch.object(profile_replacement, 'apply') as replace:
                sandbox.sandbox_command(args, state, None)
                build.assert_called_once()
                replace.assert_called_once_with(expected, expected, state)
                self.assertEqual(json.loads(saved.read_text())['image_source'], 'prebuilt')

    def test_native_build_context_uses_a_named_image_while_runtime_uses_its_immutable_id(self):
        config = dict(self.config, primary_agent='codex', image_refs={'controller-codex': IMAGE_ID})
        local = primary_agents.local_image(config)
        self.assertIn('primary-controller=docker-image://' + local, primary_agents.worker_context(config))
        self.assertEqual(primary_agents.image(config), IMAGE_ID)
        config['image_refs']['controller-codex'] = PUBLISHED
        self.assertIn('primary-controller=docker-image://' + PUBLISHED, primary_agents.worker_context(config))

    def test_first_hosted_connection_prepares_exact_inference_image_before_assigning_credentials(self):
        state = self.root / self.config['name']
        state.mkdir()
        self.config['image_refs'] = {'workspace': IMAGE_ID}
        saved = state / 'config.json'
        saved.write_text(json.dumps(self.config))
        self.publish({image_sources.recipe(self.config, 'inference'): {'linux/amd64': PUBLISHED}})
        value = {'provider': 'openrouter', 'key': 'synthetic-key'}
        self.engine.on_pull = Mock(side_effect=ValueError('synthetic HTTP 503'))
        with patch.object(hosted_models, 'request', return_value={}), patch.object(inference, 'up') as up, \
                patch.object(inference, 'down') as down, patch.object(inference, 'credentials') as credentials:
            with self.assertRaisesRegex(ValueError, 'HTTP 503'):
                hosted_models.save(self.config, state, value)
            up.assert_not_called()
            down.assert_not_called()
            credentials.assert_not_called()
            self.assertEqual(json.loads(saved.read_text()), self.config)
            self.engine.on_pull = None
            hosted_models.save(self.config, state, value)
        self.assertEqual(up.call_args.args[0]['image_refs']['inference'], PUBLISHED)
        self.assertEqual(json.loads(saved.read_text())['image_refs']['inference'], PUBLISHED)
        self.assertNotIn('inference', self.config['image_refs'])

    def test_first_connector_on_stopped_sandbox_prepares_before_apply_and_preserves_cancelled_state(self):
        state = self.root / self.config['name']
        state.mkdir()
        self.config['image_refs'] = {'workspace': IMAGE_ID}
        saved = state / 'config.json'
        saved.write_text(json.dumps(self.config))
        source = Path(__file__).resolve().parents[1] / 'examples/connectors/build-status/connector.json'
        registry = self.root / 'connectors'
        sandbox.write_json(registry / 'status/connector.json', {**json.loads(source.read_text()), 'id': 'status'})
        changes = {'connector_ids': ['status'], 'connector_operations': {'status': ['latest']}}
        self.publish({image_sources.recipe(self.config, 'connector'): {'linux/amd64': PUBLISHED}})
        gate = state / 'cancelled'
        gate.mkdir()
        with patch.object(connectors, 'directory', return_value=registry), \
                patch('ai_sandbox_factory.provider_credentials.os_store') as store, \
                patch.object(sandbox, 'inspect', return_value=None), patch.object(connectors, 'up') as start:
            store.return_value.get_password.return_value = 'synthetic-token'
            connector_settings.apply(self.config, state, changes, gate)
            self.assertEqual(self.engine.calls.count(('podman', 'pull', PUBLISHED)), 1)
            self.assertEqual(json.loads(saved.read_text()), self.config)
            self.assertFalse((state / 'connector-manifests/status.json').exists())
            connector_settings.apply(self.config, state, changes, None)
            start.assert_not_called()
        self.assertEqual(json.loads(saved.read_text())['image_refs']['connector'], PUBLISHED)
        self.assertNotIn('connector', self.config['image_refs'])
        self.assertTrue((state / 'connector-manifests/status.json').is_file())

    def test_first_aws_grant_keeps_the_prepared_ref_only_when_apply_succeeds(self):
        state = self.root / self.config['name']
        state.mkdir()
        self.config['image_refs'] = {'workspace': IMAGE_ID}
        saved = state / 'config.json'
        saved.write_text(json.dumps(self.config))
        self.publish({image_sources.recipe(self.config, 'aws'): {'linux/amd64': PUBLISHED}})
        gate = state / 'cancelled'
        gate.mkdir()
        value = {'region': 'us-east-1', 'profile': 'fixture', 'role': 'arn:aws:iam::123456789012:role/fixture',
                 'grants': [{'Action': 'ec2:DescribeRegions', 'Resource': '*'}]}
        with patch.object(sandbox, 'aws_installation', return_value=('ready', {})), \
                patch.object(sandbox, 'refresh_shell_network'), patch.object(connector_settings, 'refresh_agent_context'), \
                patch.object(aws_access, 'up') as start:
            sandbox.apply_aws_settings(self.config, state, value, apply_gate=gate)
            self.assertEqual(self.engine.calls.count(('podman', 'pull', PUBLISHED)), 1)
            self.assertEqual(json.loads(saved.read_text())['image_refs'], {'workspace': IMAGE_ID})
            self.assertEqual(self.config['image_refs'], {'workspace': IMAGE_ID})
            sandbox.apply_aws_settings(self.config, state, value)
            start.assert_not_called()
        self.assertEqual(json.loads(saved.read_text())['image_refs']['aws'], PUBLISHED)

    def test_one_shot_build_replaces_old_refs_with_new_images_but_keeps_saved_preference(self):
        self.config.update(profile=sandbox.load_profile('base'), common=sandbox.common_profile(),
                           image_refs={'workspace': 'older-workspace', 'gateway': 'older-gateway'})
        local_workspace = 'sha256:' + 'c' * 64
        self.engine.images['older-gateway'] = {'Id': 'older-gateway'}
        def build_workspace(config):
            self.assertFalse(config.get('image_refs'))
            self.prepare(config, lambda: self.engine.images.update({LOCAL: {'Id': IMAGE_ID}}))
            target = sandbox.image_names(config)[0]
            self.engine.images[target] = {'Id': local_workspace}
            return target
        with image_sources.override('local'), patch.object(sandbox, '_build', side_effect=build_workspace):
            sandbox.build(self.config)
        self.assertEqual(self.config['image_source'], 'prebuilt')
        self.assertEqual(self.config['image_refs'], {'gateway': IMAGE_ID, 'workspace': local_workspace})
        self.assertEqual(sandbox.image_names(self.config), (local_workspace, IMAGE_ID))
        self.assertFalse(any(call[1] == 'pull' for call in self.engine.calls))

    def test_starting_legacy_inference_after_preference_edit_keeps_its_running_worker(self):
        routes = {'fixture': {'origin': 'https://fixture.example.test'}}
        local = inference.local_image()
        self.engine.images[local] = {'Id': IMAGE_ID}
        self.publish({image_sources.recipe(self.config, 'inference'): {'linux/amd64': PUBLISHED}})
        label = hashlib.sha256((json.dumps(routes, sort_keys=True) + local).encode()).hexdigest()
        existing = {'State': {'Running': True}, 'Image': IMAGE_ID,
                    'Config': {'Labels': {'io.pi-sandbox-factory.inference': label}}}
        with patch.object(inference, 'enabled', return_value=True), \
                patch.object(inference, 'check_ownership'), patch.object(inference, 'routes', return_value=routes), \
                patch.object(sandbox, 'inspect', return_value=existing), patch.object(sandbox, 'resume_existing') as resume, \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as runtime:
            inference.up(self.config, self.root)
        worker = inference.names(self.config)[0]
        resume.assert_called_once_with(self.config, worker, existing)
        self.assertFalse(any(call[1] == 'pull' for call in self.engine.calls))
        self.assertFalse(any(call.args[0][1] == 'rm' for call in runtime.call_args_list))

    def test_builder_failure_is_returned_without_recording_an_old_local_tag(self):
        self.publish({})
        self.engine.images[LOCAL] = {'Id': IMAGE_ID}
        build = Mock(side_effect=ValueError('Package setup exited with status 71: repository refused request'))
        with self.assertRaisesRegex(ValueError, 'status 71: repository refused request'):
            self.prepare(builder=build)
        self.assertNotIn('image_refs', self.config)
        self.assertEqual(self.engine.calls, [])

    def test_successive_builds_keep_exact_applied_recipes_and_only_save_successful_refs(self):
        state = self.root / self.config['name']
        state.mkdir()
        self.config.update(profile=sandbox.snapshot_profile(sandbox.load_profile('base'), state),
                           common=sandbox.snapshot_profile(sandbox.common_profile(), state))
        saved = state / 'config.json'
        saved.write_text(json.dumps(self.config))
        workspace_image = 'sha256:' + 'c' * 64
        def build_workspace(config):
            self.prepare(config)
            target = sandbox.image_names(config)[0]
            self.engine.images[target] = self.engine.images[workspace_image] = {'Id': workspace_image}
            return target
        args = SimpleNamespace(command='build', image_source=None)
        def build_with(gateway):
            self.publish({self.recipe: {'linux/amd64': gateway}})
            sandbox.sandbox_command(args, state, None)
            config = json.loads(saved.read_text())
            self.assertEqual(config['image_refs'], {'workspace': workspace_image, 'gateway': gateway})
            profile_runtime.select(config, state)
            record = profile_runtime.applied(state)
            self.assertEqual(record['image_id'], workspace_image)
            self.assertEqual(record['image_refs'], config['image_refs'])
            self.assertEqual(record['image_artifacts'], config['image_artifacts'])
            self.assertEqual(record['image_artifacts'][0]['reference'], gateway)
            return record
        with patch.object(sandbox, '_build', side_effect=build_workspace):
            first = build_with(PUBLISHED)
            second = build_with(PUBLISHED.replace('a' * 64, 'd' * 64))
        self.assertNotEqual(first['path'], second['path'])
        current = {'Image': workspace_image, 'Mounts': [{'Destination': profile_runtime.PATH, 'Source': first['path']}]}
        profile_runtime.select(json.loads(saved.read_text()), state, current)
        self.assertEqual(profile_runtime.applied(state), first)
        original = saved.read_bytes()
        records = set((state / 'applied-profiles').iterdir())
        def fail_after_acquisition(config):
            self.prepare(config)
            raise ValueError('Cannot complete profile layer: fixture failed')
        with patch.object(sandbox, '_build', side_effect=fail_after_acquisition), \
                self.assertRaisesRegex(ValueError, 'Cannot complete profile layer'):
            sandbox.sandbox_command(args, state, None)
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(set((state / 'applied-profiles').iterdir()), records)
        self.assertEqual(profile_runtime.applied(state), first)


class ImageRecipeTests(unittest.TestCase):
    def test_recipes_follow_the_generators_that_supply_their_build_inputs(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = {'uid': 1000, 'gid': 1000}
        roles = ('pi-common', 'pi-stock', 'native-common', 'advisor', 'gateway')
        for filename in ('image_builds.py', 'sandbox_extensions.py', 'model_providers.py'):
            (root / filename).write_text('original generator')
        with patch.object(image_sources, '__file__', str(root / 'image_sources.py')):
            original = {role: image_sources.recipe(config, role) for role in roles}
            for filename, changed in (
                    ('image_builds.py', {'pi-common', 'pi-stock', 'native-common'}),
                    ('sandbox_extensions.py', {'pi-common', 'pi-stock', 'advisor'})):
                with self.subTest(filename=filename):
                    (root / filename).write_text('updated generator')
                    self.assertEqual({role for role in roles if image_sources.recipe(config, role) != original[role]}, changed)
                    (root / filename).write_text('original generator')

    def test_advisor_uses_the_same_extension_selection_as_its_builder(self):
        saved = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = {'uid': 1000, 'gid': 1000, 'common': {'path': str(saved)}}
        original = image_sources.recipe(config, 'advisor')  # Older snapshots use the bundled advisor lock.
        lock = json.loads((sandbox.ROOT / 'containers/dev/extensions.lock.json').read_text())
        lock['extensions']['pi-local-model-manager']['commit'] = 'a' * 40
        (saved / 'extensions.lock.json').write_text(json.dumps(lock))
        self.assertEqual(image_sources.recipe(config, 'advisor'), original)
        lock['extensions']['pi-advisor']['commit'] = 'b' * 40
        (saved / 'extensions.lock.json').write_text(json.dumps(lock))
        self.assertNotEqual(image_sources.recipe(config, 'advisor'), original)

    def test_inference_recipe_uses_the_files_consumed_by_its_dockerfile(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / 'containers/inference'
        source.mkdir(parents=True)
        for name in ('Dockerfile', 'package.json', 'package-lock.json', 'broker.mjs', 'credentials.mjs'):
            shutil.copyfile(sandbox.ROOT / 'containers/inference' / name, source / name)
        with patch.object(sandbox, 'ROOT', root):
            original = image_sources.recipe({}, 'inference')
            installed = source / 'node_modules/example/package.json'
            installed.parent.mkdir(parents=True)
            installed.write_text('{}')
            self.assertEqual(image_sources.recipe({}, 'inference'), original)
            (source / 'package-lock.json').write_text('{}')
            self.assertNotEqual(image_sources.recipe({}, 'inference'), original)

    def test_saved_snapshot_identity_depends_on_build_inputs_not_location(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root / 'common'
        source.mkdir()
        (source / 'Dockerfile').write_text('FROM scratch\n')
        (source / 'bootstrap.py').write_text('print("bootstrap")\n')
        (source / 'bootstrap.py').chmod(0o644)
        common = {'name': 'common', 'path': str(source), 'digest': sandbox.tree_digest(source)}
        saved = sandbox.snapshot_profile(common, root / 'sandbox')
        def identity(snapshot, **extra):
            return image_sources.recipe({'uid': 1000, 'gid': 1000, 'common': snapshot, **extra}, 'pi-common')
        initial = identity(common)
        self.assertEqual(identity(saved), initial)
        os.utime(source / 'bootstrap.py', (100, 100))
        self.assertEqual(identity(common), initial)
        (source / 'bootstrap.py').chmod(0o600)
        self.assertEqual(identity(common), initial)
        (source / 'bootstrap.py').chmod(0o755)
        self.assertNotEqual(identity(common), initial)
        self.assertEqual(identity(saved), initial)
        (source / 'bootstrap.py').chmod(0o644)
        (source / 'bootstrap.py').write_text('print("new bootstrap")\n')
        self.assertNotEqual(identity(common), initial)
        self.assertNotEqual(identity(saved, gid=2000), initial)
        self.assertNotEqual(identity(saved, uid=2000), initial)
        # The common Dockerfile copies security/ with its bundled files.
        dependency = source / 'security/node_modules/fixture/index.js'
        dependency.parent.mkdir(parents=True)
        dependency.write_text('module.exports = 1')
        with_dependency = identity(common)
        dependency.write_text('module.exports = 2')
        self.assertNotEqual(identity(common), with_dependency)
        for extra in ({'name': 'a-different-sandbox'}, {'image_source': 'local'},
                      {'profile': {'path': '/unused-private-profile'}, 'model_providers': [{'name': 'private'}]}):
            self.assertEqual(identity(saved, **extra), initial)

    def test_shared_roles_separate_uid_sensitive_recipes_and_controller_variants(self):
        config = {'uid': 1000, 'gid': 1000}
        shared = ('gateway', 'inference', 'aws', 'connector')
        uid_sensitive = ('pi-common', 'pi-stock', 'native-common', 'ssh', 'advisor',
                         'controller-codex', 'controller-claude', 'controller-agy')
        identities = set()
        for role in (*shared, *uid_sensitive):
            with self.subTest(role=role):
                identity = image_sources.recipe(config, role)
                other_uid = image_sources.recipe({**config, 'uid': 2000}, role)
                self.assertEqual(identity == other_uid, role in shared)
                identities.add(identity)
        self.assertEqual(len(identities), len(shared) + len(uid_sensitive))

    def test_custom_profile_from_changes_cannot_select_finished_stock(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        profile = sandbox.load_profile('base')
        copy = root / 'custom'
        shutil.copytree(profile['path'], copy)
        config = {'profile': {**profile, 'path': str(copy)}}
        self.assertTrue(image_builds.stock_eligible(config))
        original_mode = (copy / 'profile.md').stat().st_mode
        (copy / 'profile.md').chmod(0o600)
        self.assertFalse(image_builds.stock_eligible(config))
        (copy / 'profile.md').chmod(original_mode)
        (copy / 'Dockerfile').write_text('FROM scratch\n')
        config['profile'] = sandbox.load_profile(str(copy))
        self.assertFalse(image_builds.stock_eligible(config))
