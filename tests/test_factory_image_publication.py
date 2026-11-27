"""Exercise candidate builds, anonymous digest verification and the CI publication gate."""
from contextlib import nullcontext, redirect_stdout
from hashlib import sha256
import io
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ai_sandbox_factory import sandbox_extensions
CHECKOUT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('factory_image_publisher', CHECKOUT / 'scripts/build_factory_images.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
spec = importlib.util.spec_from_file_location('prebuilt_image_fixture', CHECKOUT / 'tests/prebuilt_images_integration.py')
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)


class PrebuiltFixtureIdentity(unittest.TestCase):
    def test_fixture_uses_podman_container_identity_on_a_different_host_uid(self):
        from ai_sandbox_factory import creation, sandbox
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(sandbox.os, 'getuid', return_value=1001), \
                    patch.object(sandbox.os, 'getgid', return_value=1002), \
                    patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=root), \
                    patch.object(sandbox, 'choose_engine', return_value='podman'), \
                    patch.object(sandbox.host_platform, 'check_engine', return_value=None), \
                    patch.object(creation, 'resource_conflicts'), \
                    patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 0, 'true\n')) as engine, \
                    redirect_stdout(io.StringIO()):
                config = integration.build_configuration(root)
            self.assertEqual((config['uid'], config['gid']), (1000, 1000))
            self.assertEqual(config['image_refs'], {})
            engine.assert_called_once_with(['podman', 'info', '--format', '{{.Host.Security.Rootless}}'], capture=True)
            published = publisher.public_config()
            for role in ('pi-common', 'pi-stock', 'ssh', 'native-common'):
                with self.subTest(role=role):
                    self.assertEqual(publisher.image_sources.recipe(config, role),
                                     publisher.image_sources.recipe(published, role))


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.images = {}
        self.registry = {}
        self.fail = ''
        self.bad_pull = ''

    def run(self, argv, *, capture=False, check=True, **kwargs):
        command = list(map(str, argv))
        self.commands.append(command)
        engine, operation, *arguments = command
        assert engine == 'podman', command
        if operation == 'info':
            return subprocess.CompletedProcess(command, 0, 'amd64')
        if operation == 'build':
            if self.fail == 'build':
                raise subprocess.CalledProcessError(2, command)
            tag = arguments[arguments.index('-t') + 1]
            image_id = sha256(tag.encode()).hexdigest()
            data = {'Id': image_id, 'Os': 'linux', 'Architecture': 'amd64', 'RepoDigests': []}
            self.images[tag] = self.images[image_id] = data
        elif operation == 'image':
            assert arguments[0] == 'inspect', command
            image = self.images.get(arguments[1])
            if image is None:
                assert not check, command
                return subprocess.CompletedProcess(command, 1, '', 'Image not found')
            return subprocess.CompletedProcess(command, 0, json.dumps([image]))
        elif operation == 'run':
            assert '--network=none' in arguments and '--pull=never' in arguments
            image = arguments[arguments.index('--entrypoint') + 2]
            assert image in self.images, command
            phase = 'registry-smoke' if '@' in image else 'local-smoke'
            if self.fail == phase:
                raise subprocess.CalledProcessError(9, command)
        elif operation == 'push':
            if self.fail == 'push':
                raise subprocess.CalledProcessError(3, command)
            image_id, destination = arguments[-2:]
            repository = destination.removeprefix('docker://').rsplit(':', 1)[0]
            digest = 'sha256:' + sha256(('manifest-' + image_id).encode()).hexdigest()
            self.registry[repository + '@' + digest] = self.images[image_id]
            Path(arguments[arguments.index('--digestfile') + 1]).write_text(
                'not-a-digest' if self.fail == 'digest' else digest)
        elif operation == 'pull':
            option, auth_path, reference = arguments
            assert option == '--authfile', command
            authfile = Path(auth_path)
            assert json.loads(authfile.read_text()) == {'auths': {}}
            if self.fail == 'pull':
                raise subprocess.CalledProcessError(4, command)
            image = {**self.registry[reference], 'RepoDigests': [reference]}
            if self.bad_pull == 'id':
                image['Id'] = 'd' * 64
            elif self.bad_pull == 'digest':
                image['RepoDigests'] = []
            elif self.bad_pull == 'architecture':
                image['Architecture'] = 'arm64'
            self.images[reference] = image
        else:
            raise AssertionError(f'Unexpected Podman command {command}')
        versions = ''
        if operation == 'run' and '--version' in arguments:
            executable = arguments[arguments.index('--entrypoint') + 1]
            pins = json.loads((publisher.sandbox.ROOT / 'containers/primary/agent-versions.json').read_text())
            versions = ' '.join([publisher.sandbox.PI_VERSION, publisher.browser_editor.VERSION,
                                 *(pin['version'] for pin in pins.values()), re.search(r'^ARG NPM_VERSION=(\S+)$', (publisher.sandbox.ROOT / 'containers/dev/Dockerfile').read_text(), re.M).group(1), re.search(r'aws-cli:([\d.]+)', (publisher.sandbox.ROOT / 'containers/aws-access/client/Dockerfile').read_text()).group(1)])
        return subprocess.CompletedProcess(command, 0, versions)


class FactoryImagePublication(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'result.json'
        self.engine = FakeEngine()
        self.enterContext(patch.object(publisher.sandbox, 'run', self.engine.run))
        self.enterContext(patch.dict(os.environ, XDG_CACHE_HOME=str(self.root / 'cache')))
        self.enterContext(redirect_stdout(io.StringIO()))
        publisher.image_sources._platform.cache_clear()
        self.addCleanup(publisher.image_sources._platform.cache_clear)

    def execute(self, *args):
        publisher.main(['--output', str(self.output), *map(str, args)])
        return json.loads(self.output.read_text())

    def test_local_build_emits_only_tested_local_ids(self):
        config = publisher.public_config()
        self.assertEqual(config['common']['path'], str(publisher.sandbox.ROOT / 'containers/dev'))
        self.assertEqual(config['profile']['path'], str(publisher.sandbox.ROOT / 'profiles/base'))
        self.assertEqual((config['uid'], config['gid'], config['image_source']), (1000, 1000, 'local'))
        result = self.execute('--roles', 'gateway', 'inference')
        self.assertEqual(set(result), {'platform', 'local_images'})
        self.assertEqual(result['platform'], publisher.PLATFORM)
        self.assertEqual([row['role'] for row in result['local_images']], ['gateway', 'inference'])
        for row in result['local_images']:
            self.assertEqual(row['recipe'], publisher.image_sources.recipe(config, row['role']))
            smoke = [args for args in self.engine.commands if args[1] == 'run' and row['image_id'] in args]
            self.assertEqual(len(smoke), len(publisher.SMOKE[row['role']]))
        self.assertFalse(any(args[1] in ('tag', 'push', 'pull', 'login') for args in self.engine.commands))

    def test_every_public_recipe_runs_its_builder_and_offline_smoke(self):
        archive = self.root / 'code-server.tar.gz'
        archive.write_bytes(b'fixture data for the synthetic engine')
        with patch.object(sandbox_extensions, 'build_contexts', return_value=nullcontext([])), \
                patch.object(publisher.browser_editor, 'checked_archive', return_value=archive):
            report = self.execute('--cache', self.root / 'cache')
        local = {row['role']: row for row in report['local_images']}
        self.assertEqual(set(local), set(publisher.SMOKE))
        for role, row in local.items():
            with self.subTest(role=role):
                commands = [args for args in self.engine.commands if args[1] == 'run' and row['image_id'] in args]
                self.assertEqual(len(commands), len(publisher.SMOKE[role]))

    def test_published_index_uses_registry_manifests_pulled_anonymously_and_smoked(self):
        auth = self.root / 'private-login.json'
        auth.write_text('{"auths":{"docker.io":{"auth":"synthetic-private-token"}}}')
        result = self.execute('--roles', 'gateway', 'inference', '--publish', '--push-authfile', auth)
        self.assertEqual(set(result), {'version', 'images'})
        self.assertEqual(result['version'], 1)
        self.assertEqual(len(result['images']), 2)
        local_last = max(i for i, args in enumerate(self.engine.commands)
                         if args[1] == 'run' and not any('@sha256:' in arg for arg in args))
        first_push = next(i for i, args in enumerate(self.engine.commands) if args[1] == 'push')
        self.assertGreater(first_push, local_last)
        for role in ('gateway', 'inference'):
            key = publisher.image_sources.recipe(publisher.public_config(), role)
            reference = result['images'][key][publisher.PLATFORM]
            self.assertIn(reference, self.engine.registry)
            self.assertNotEqual(reference.split('@sha256:')[1], self.engine.registry[reference]['Id'])
            pull = next(i for i, args in enumerate(self.engine.commands) if args[1] == 'pull' and reference in args)
            smokes = [i for i, args in enumerate(self.engine.commands) if args[1] == 'run' and reference in args]
            self.assertEqual(len(smokes), len(publisher.SMOKE[role]))
            self.assertGreater(min(smokes), pull)
        for args in self.engine.commands:
            self.assertEqual(str(auth) in args, args[1] == 'push')
            self.assertNotIn('synthetic-private-token', ' '.join(args))
        self.assertNotIn('synthetic-private-token', self.output.read_text())

    def test_failed_build_smoke_or_publication_leaves_no_candidate_file(self):
        for phase in ('build', 'local-smoke', 'push', 'digest', 'pull', 'registry-smoke'):
            with self.subTest(phase=phase):
                self.engine.fail = phase
                self.engine.commands.clear()
                with self.assertRaises((subprocess.CalledProcessError, ValueError)):
                    self.execute('--roles', 'gateway', '--publish')
                self.assertFalse(self.output.exists())
                if phase in ('build', 'local-smoke'):
                    self.assertFalse(any(args[1] == 'push' for args in self.engine.commands))
        self.engine.fail = ''
        for mismatch in ('id', 'digest', 'architecture'):
            with self.subTest(mismatch=mismatch):
                self.engine.bad_pull = mismatch
                with self.assertRaisesRegex(ValueError, 'Pulled|platform'):
                    self.execute('--roles', 'gateway', '--publish')
                self.assertFalse(self.output.exists())

    def test_a_source_change_during_the_build_prevents_publication(self):
        with patch.object(publisher.image_sources, 'recipe', side_effect=['1' * 64, '2' * 64]):
            with self.assertRaisesRegex(ValueError, 'gateway recipe changed'):
                self.execute('--roles', 'gateway', '--publish')
        self.assertFalse(self.output.exists())
        self.assertFalse(any(args[1] == 'push' for args in self.engine.commands))

    def test_merge_requires_same_checkout_role_platform_and_registry_digest(self):
        folder = self.root / 'fragments'
        folder.mkdir()
        for role in ('gateway', 'inference'):
            fragment = self.execute('--roles', role, '--publish')
            (folder / (role + '.json')).write_text(json.dumps(fragment))
        self.engine.commands.clear()
        merged = self.execute('--roles', 'gateway', 'inference', '--merge-fragments', folder)
        self.assertEqual(set(merged['images']), {publisher.image_sources.recipe(publisher.public_config(), role)
                                                 for role in ('gateway', 'inference')})
        self.assertEqual(self.engine.commands, [])
        key = publisher.image_sources.recipe(publisher.public_config(), 'gateway')
        for bad in ({'local_images': []}, {'version': 1, 'images': {'stale': {}}},
                    {'version': 1, 'images': {key: {'linux/arm64': 'other'}}},
                    {'version': 1, 'images': {key: {publisher.PLATFORM: publisher.REPOSITORY + 'gateway:latest'}}},
                    {'version': 1, 'images': {key: {publisher.PLATFORM: publisher.REPOSITORY + 'ssh@sha256:' + '4' * 64}}}):
            with self.subTest(bad=bad):
                (folder / 'gateway.json').write_text(json.dumps(bad))
                with self.assertRaisesRegex(ValueError, 'gateway.json'):
                    self.execute('--roles', 'gateway', '--merge-fragments', folder)
                self.assertEqual(json.loads(self.output.read_text()), merged)

    def test_only_candidate_tags_and_explicit_main_dispatch_publish(self):
        path = CHECKOUT / '.github/workflows/images.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'push', 'workflow_call', 'workflow_dispatch'})
        self.assertEqual(workflow['on']['push'], {'tags': ['image-candidate-*']})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['publish']['default'], 'false')
        job = workflow['jobs']['candidate']
        index = workflow['jobs']['candidate-index']
        self.assertEqual(job['if'], "github.event.deleted != true && inputs.notification_run_id == ''")
        self.assertEqual(set(job['strategy']['matrix']['role']), set(publisher.SMOKE))
        gate = ("(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/image-candidate-')) || "
                "((github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/image-maintenance-test-')) && inputs.publish)")
        self.assertEqual(job['env']['PUBLISH'], '${{ ' + gate + ' }}')
        self.assertEqual((index['if'], index['needs']), (gate, 'candidate'))
        for task in (job, index):
            checkout = next(step for step in task['steps'] if step.get('uses', '').startswith('actions/checkout@'))
            self.assertEqual(checkout['with'], {'ref': '${{ inputs.ref || github.sha }}', 'persist-credentials': 'false'})
        protected = [step for step in job['steps'] if 'secrets.' in str(step)]
        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0]['if'], "env.PUBLISH == 'true'")
        self.assertEqual(set(protected[0]['env']), {'DOCKERHUB_USERNAME', 'DOCKERHUB_TOKEN'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})

    def test_prebuilt_lifecycle_has_a_separate_runner_and_is_required(self):
        workflow = yaml.load((CHECKOUT / '.github/workflows/checks.yml').read_text(), Loader=yaml.BaseLoader)
        jobs = workflow['jobs']
        prebuilt = jobs['prebuilt-images']
        commands = [step.get('run', '') for step in prebuilt['steps']]
        lifecycle = [command for command in commands if 'tests/prebuilt_images_integration.py' in command]
        self.assertEqual(len(lifecycle), 1)
        for flag in ('--profiles', '--recovery', '--interfaces'):
            self.assertIn(flag, lifecycle[0])
        self.assertFalse(any('tests/prebuilt_images_integration.py' in step.get('run', '')
                             for step in jobs['fresh-sandbox']['steps']))
        self.assertIn('prebuilt-images', jobs['test']['needs'])
        self.assertIn('needs.prebuilt-images.result', jobs['test']['steps'][0]['run'])
        self.assertEqual(jobs['release']['needs'], ['test', 'package'])
