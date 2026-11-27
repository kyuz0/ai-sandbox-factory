"""Exercise actual image pulls, creation and editable profiles with a local registry."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time
from unittest.mock import patch
from uuid import uuid4

CHECKOUT = Path(__file__).resolve().parents[1]


def build_configuration(root):
    """Use the public creation path so fixture recipes match the runtime's user mapping."""
    from ai_sandbox_factory import sandbox
    workspace = root / 'build-project'
    workspace.mkdir(exist_ok=True)
    providers = root / 'build-providers.json'
    providers.write_text('{}')
    state = root / 'build-state'
    name = 'prebuilt-image-inputs-' + uuid4().hex[:10]
    with patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(root / 'build-library')), \
            patch.object(sys, 'argv', ['ai-sandbox-factory', '--state-dir', str(state),
            'create', name, '--workspace', str(workspace), '--engine', 'podman',
            '--profile', str(sandbox.ROOT / 'profiles/base'), '--primary-auth', 'login',
            '--image-source', 'local', '--providers-file', str(providers), '--no-browser']):
        sandbox.main()
    return json.loads((state / name / 'config.json').read_text())


def verify_cached_candidate(registry, reference, environment):
    from scripts.build_factory_images import inspect_image, verify_published
    with patch.dict(os.environ, environment):
        subprocess.run(['podman', 'pull', reference], check=True)
        image_id, digests = inspect_image(reference)
        assert reference in digests, 'The published digest must be cached before verification'
        start = len(registry.requests)
        authenticated = len(registry.authenticated_requests)
        verify_published('gateway', image_id, reference)
        path = '/v2/gateway/manifests/' + reference.split('@')[1]
        assert ('GET', path) in registry.requests[start:], 'Verification must fetch the cached digest from the registry'
        assert len(registry.authenticated_requests) == authenticated, 'Verification sent registry credentials'


def prepare_worker(root, args):
    package = root / 'package'
    shutil.copytree(CHECKOUT / 'ai_sandbox_factory', package / 'ai_sandbox_factory',
                    ignore=shutil.ignore_patterns('__pycache__', 'node_modules'), dirs_exist_ok=True)
    bootstrap = (f'import sys,runpy;sys.path.insert(0,{str(package)!r});'
                 f'sys.path.insert(0,{str(CHECKOUT / "tests")!r});'
                 f'runpy.run_path({str(Path(__file__).resolve())!r},run_name="__main__")')
    command = [sys.executable, '-I', '-c', bootstrap, '--worker', '--output', str(root)]
    for option in ('profiles', 'sharing', 'interfaces', 'recipes'):
        if getattr(args, option):
            command.append('--' + option)
    return package / 'ai_sandbox_factory/images.lock.json', bootstrap, command


def pull_recovery(root):
    from ai_sandbox_factory import creation, sandbox
    from ai_sandbox_factory.operations import DEFAULT_SETTINGS, Service, preset
    root = Path(root)
    service = Service(root / 'recovery-state')
    workspace = root / 'recovery-project'
    workspace.mkdir(exist_ok=True)
    name = 'prebuilt-cancel-' + uuid4().hex[:10]
    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
    draft.update(name=name, workspace=str(workspace), engine='podman', image_source='prebuilt',
                 model_providers={}, open_editor=False, primary_auth='login')
    draft['options']['browser'] = False
    job = creation.Creation()
    def cancel():
        while not job.finished:
            if (root / 'cancel-pull').exists():
                job.cancel()
                return
            time.sleep(0.05)
    watcher = Thread(target=cancel, daemon=True)
    watcher.start()
    try:
        service.create(draft, None, lambda line: print(line, flush=True), cancellation=job)
    except creation.Cancelled:
        assert not (service.state / name).exists()
        assert sandbox.inspect({'name': name, 'engine': 'podman'}, sandbox.names({'name': name})[0]) is None
        (root / 'pull-cancelled').touch()
    else:
        raise AssertionError('The blocked real image pull was not cancelled')
    finally:
        watcher.join()
    while not (root / 'retry-pull').exists():
        time.sleep(0.05)
    try:
        service.create(draft, None, lambda line: print(line, flush=True), cancellation=creation.Creation())
        config = service.config(name)
        assert sandbox.inspect(config, sandbox.names(config)[0])['State']['Running']
        (root / 'pull-recovery-result.json').write_text(json.dumps({'cancelled_real_pull': True, 'same_name_retry': True}))
    finally:
        if (service.state / name / 'config.json').exists():
            service.run(['delete', name], print)


def worker(root, profiles=False, sharing=False, interfaces=False, recipes=False):
    from ai_sandbox_factory import creation, editor_settings, image_sources, registry, sandbox
    from ai_sandbox_factory.operations import DEFAULT_SETTINGS, Service, preset
    from creation_provider import ADAPTER, KEY
    from browser_editor_lifecycle import request
    from ai_sandbox_factory import browser_editor
    from urllib.parse import urlencode

    root = Path(root)
    service = Service(root / 'state')
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    (workspace / 'AGENTS.md').write_text('PROJECT_INSTRUCTIONS_UNCHANGED\n')
    provider = root / 'provider'
    provider.mkdir(exist_ok=True)
    (provider / 'provider.json').write_bytes((CHECKOUT / 'examples/provider/provider.json').read_bytes())
    (provider / 'adapter.mjs').write_text(ADAPTER)
    os.environ['SANDBOX_REGISTRY_DIR'] = str(root / 'library')
    if 'fixture' in registry.installed('provider'):
        registry.update('provider', 'fixture')
    else:
        registry.add('provider', 'fixture', str(provider))
    public_key = root / 'host-key'
    if not public_key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(public_key)], check=True)
    key_text = ' '.join(public_key.with_suffix('.pub').read_text().split()[:2])
    results = []
    configurations = []
    generated_names = []
    def log(line):
        assert KEY not in line, 'Managed key leaked into job output'
        print(line, flush=True)
    try:
        # Failure and retry reuse the same sandbox name through the real worker.
        name = 'prebuilt-provider-' + uuid4().hex[:10]
        generated_names.append(name)
        for model in ('unavailable', 'available'):
            draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name=name, workspace=str(workspace), engine='podman', image_source='prebuilt',
                         model_providers={'fixture': {'model': model}}, _provider_keys={'fixture': KEY},
                         primary_auth='login', open_editor=False,
                         ssh={'hosts': [{'alias': 'fixture', 'hostname': '192.0.2.10', 'user': 'fixture',
                                         'port': 22, 'host_keys': [key_text]}]})
            output = []
            def record(line):
                output.append(line)
                log(line)
            started = time.monotonic()
            try:
                service.create(draft, None, record, cancellation=creation.Creation())
            except ValueError as error:
                assert model == 'unavailable', str(error)
                assert 'HTTP 503' in str(error) and 'provider fixture' in str(error), str(error)
                assert not (service.state / name).exists(), 'Failed worker retained creation state'
                results.append({'case': 'provider-failure-rollback', 'passed': True})
                continue
            assert model == 'available', 'Unavailable provider succeeded'
            config = service.config(name)
            configurations.append(config)
            results.append({'case': 'provider-retry', 'seconds': time.monotonic() - started, 'passed': True})
            assert sum('Fixture catalogue loaded' in line for line in output) == 1
            assert not any(line.startswith('› configure') for line in output), 'Creation restarted the stack for configuration'
            artifacts = {row['role'] for row in config['image_artifacts']}
            assert {'pi-stock', 'gateway', 'inference', 'ssh'} <= artifacts, artifacts
            container = sandbox.names(config)[0]
            assert sandbox.run(['podman', 'exec', container, 'cat', '/home/node/.pi/agent/fixture-provider-ready'], capture=True).stdout == 'available'
            assert KEY not in sandbox.run(['podman', 'exec', container, 'env'], capture=True).stdout
            state = service.state / name
            applied = json.loads((state / 'profile-runtime/applied.json').read_text())
            actual = sandbox.inspect(config, container)
            assert applied['image_id'] == actual['Image'] == config['image_refs']['workspace']
            assert applied['image_artifacts'] == config['image_artifacts']
            from ai_sandbox_factory import inference, sandbox_ssh
            resources = (*sandbox.names(config)[:2], inference.names(config)[0], sandbox_ssh.names(config)[0])
            prior = {resource: sandbox.inspect(config, resource)['Id'] for resource in resources}
            refs = copy.deepcopy(config['image_refs'])
            service.update_settings(name, {'image_source': 'local'}, log)
            current = service.config(name)
            assert current['image_refs'] == refs
            service.run(['up', name], log)
            assert {resource: sandbox.inspect(current, resource)['Id'] for resource in resources} == prior
            results.append({'case': 'preference-edit-and-up-preserve-runtime', 'passed': True})
            password = 'synthetic-editor-password'
            editor_settings.save(state, {'mode': 'browser', 'password_hash': editor_settings.password_hash(password)})
            sandbox.run(['podman', 'exec', container, 'test', '-x', browser_editor.INSTALL + '/bin/code-server'])
            started = time.monotonic()
            from unittest.mock import patch
            with patch.object(browser_editor, 'checked_archive', side_effect=AssertionError('Stock editor attempted a per-container installation')):
                browser_editor.open_browser_editor(current, state, background=True, open_tab=False)
            info = browser_editor.runtime(state)
            assert request(info['url'])[0] == 302
            code, headers, _ = request(info['url'], '/login', method='POST',
                body=urlencode({'password': password}), headers={'Content-Type': 'application/x-www-form-urlencoded'})
            assert code == 302 and 'Set-Cookie' in headers
            cookie = headers['Set-Cookie'].split(';')[0]
            assert request(info['url'], headers={'Cookie': cookie})[0] == 200
            results.append({'case': 'preinstalled-editor-authentication', 'seconds': time.monotonic() - started, 'passed': True})
            browser_editor.stop(state)
            if recipes:
                from cli_recipe_runtime import check
                results.append(check(service, current, root, generated_names))
        # Reusing the stock image must not register disabled integrations.
        for browser in (False, True):
            name = 'prebuilt-options-' + uuid4().hex[:10]
            generated_names.append(name)
            draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name=name, workspace=str(workspace), engine='podman', image_source='prebuilt',
                         model_providers={}, open_editor=False, primary_auth='login')
            draft['options']['browser'] = browser
            started = time.monotonic()
            service.create(draft, None, log, cancellation=creation.Creation())
            config = service.config(name)
            configurations.append(config)
            container = sandbox.names(config)[0]
            settings = json.loads(sandbox.run(['podman', 'exec', container, 'cat', '/home/node/.pi/agent/settings.json'], capture=True).stdout)
            assert ('/opt/betterwright/extension.mjs' in settings['extensions']) == browser
            for extension in ('/opt/pi-web-access/extension.mjs', '/opt/pi-communications/extension.mjs', '/opt/pi-knowledge/extension.mjs'):
                assert extension not in settings['extensions']
            sandbox.run(['podman', 'exec', container, 'test', '-x', '/opt/betterwright/chromium'])
            assert sandbox.inspect(config, container)['HostConfig']['NetworkMode'] == 'none'
            sandbox.run(['podman', 'exec', container, 'python3', '-c',
                         "import socket\ntry:\n socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
                         "except OSError:\n pass\nelse:\n raise AssertionError('Restricted sandbox allowed direct external TCP')"])
            if browser:
                assert config['image_refs']['workspace'] == configurations[-2]['image_refs']['workspace']
            results.append({'case': 'browser-on' if browser else 'browser-off', 'seconds': time.monotonic() - started, 'passed': True})
        if interfaces:
            empty_providers = root / 'empty-providers.json'
            empty_providers.write_text('{}')
            for mode in ('local', 'prebuilt'):
                name = 'image-cli-' + uuid4().hex[:10]
                generated_names.append(name)
                command = ['create', name, '--workspace', str(workspace), '--engine', 'podman',
                           '--image-source', mode, '--providers-file', str(empty_providers), '--no-browser']
                if mode == 'local':
                    command.append('--start')
                service.run(command, log)
                config = service.config(name)
                configurations.append(config)
                if mode == 'prebuilt':
                    assert config['image_refs'] == {}
                    assert sandbox.inspect(config, sandbox.names(config)[0]) is None
                    service.start(name, log)
                    config = service.config(name)
                    assert 'pi-stock' in {row['role'] for row in config['image_artifacts']}
                else:
                    assert {'pi-common', 'gateway'} <= {row['role'] for row in config['image_artifacts']}
                    for artifact in config['image_artifacts']:
                        assert artifact['source'] == 'local', artifact
                        assert artifact['reference'] == artifact['image_id'] == config['image_refs'][artifact['role']], artifact
                    sources = {part: config[part]['digest'] for part in ('profile', 'common')}
                    sandbox.run(['podman', 'exec', sandbox.names(config)[0], 'sh', '-c',
                                 'printf PRIVATE_HOME_SURVIVES > /home/node/image-override-marker'])
                    service.run(['recreate', name, '--image-source', 'prebuilt'], log)
                    config = service.config(name)
                    assert config['image_source'] == 'local'
                    assert {part: config[part]['digest'] for part in sources} == sources
                    assert 'pi-stock' in {row['role'] for row in config['image_artifacts']}
                    assert sandbox.run(['podman', 'exec', sandbox.names(config)[0], 'cat',
                                        '/home/node/image-override-marker'], capture=True).stdout == 'PRIVATE_HOME_SURVIVES'
                assert sandbox.inspect(config, sandbox.names(config)[0])['State']['Running']
                results.append({'case': 'cli-local-and-one-shot-override' if mode == 'local'
                                else 'saved-configuration-first-start', 'passed': True})
        if profiles:
            # Run existing profile lifecycle assertions against this private package/index.
            import profile_edit_integration
            previous = sys.argv
            sys.argv = ['profile_edit_integration.py', '--agent', 'pi', '--prebuilt-common']
            try:
                profile_edit_integration.main()
            finally:
                sys.argv = previous
            results.append({'case': 'editable-profile-service-lifecycle', 'passed': True})
        if sharing:
            import profile_sharing_integration
            profile_sharing_integration.main(require_prebuilt=True)
            results.append({'case': 'shared-profile-copy-and-agent-switch', 'passed': True})
        (root / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    finally:
        for name in reversed(generated_names):
            state = service.state / name
            if (state / 'config.json').exists():
                browser_editor.stop(state)
                service.run(['delete', name], log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stock-image')
    parser.add_argument('--common-image')
    parser.add_argument('--candidate-index', type=Path,
                        help='Test the digest index from the image-candidate workflow instead of a loopback registry')
    parser.add_argument('--profiles', action='store_true')
    parser.add_argument('--sharing', action='store_true', help='Exercise shared profiles and Pi/Codex copies')
    parser.add_argument('--interfaces', action='store_true', help='Exercise CLI creation, a one-shot override and first Start')
    parser.add_argument('--recipes', action='store_true', help='Replay exported shell recipes with masked input and live editor updates')
    parser.add_argument('--recovery', action='store_true', help='Cancel a real blocked pull and retry with the same sandbox name')
    parser.add_argument('--pull-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.pull_worker:
        return pull_recovery(root)
    if args.worker:
        return worker(root, args.profiles, args.sharing, args.interfaces, args.recipes)
    if args.candidate_index and (args.recovery or args.stock_image or args.common_image):
        parser.error('--candidate-index cannot use local fixture images or gate a public registry pull with --recovery')
    root.mkdir(parents=True, exist_ok=True)
    if args.candidate_index:
        lock, _, command = prepare_worker(root, args)
        lock.write_bytes(args.candidate_index.read_bytes())
        subprocess.run(command, check=True, start_new_session=args.recipes)
        print('PASS published candidate acquisition, lifecycle and cleanup', flush=True)
        return
    sys.path.insert(0, str(CHECKOUT))
    from ai_sandbox_factory import sandbox, image_builds, image_sources, sandbox_ssh, inference
    from image_registry import export, serve
    config = build_configuration(root)
    stock = args.stock_image or 'localhost/factory-prebuilt-integration-stock:' + uuid4().hex[:10]
    if not args.stock_image:
        image_builds.stock(config, stock, root / 'editor-downloads')
    common = args.common_image or image_builds.common(config)
    gateway = image_builds.gateway(config)
    sandbox_ssh.build(config, required=True)
    inference.build(config, required=True)
    roles = {'pi-stock': stock, 'pi-common': common, 'gateway': gateway,
             'ssh': sandbox_ssh.image(config), 'inference': inference.image(config)}
    lock, bootstrap, command = prepare_worker(root, args)
    with serve() as registry:
        images = {}
        for role, image in roles.items():
            image_id = image_sources._inspect(config, image)['Id'].removeprefix('sha256:')
            directory = root / ('registry-' + role + '-' + image_id)
            export(image, directory)
            reference = registry.add(role, directory)
            images[image_sources.recipe(config, role)] = {'linux/amd64': reference}
        lock.write_text(json.dumps({'version': 1, 'images': images}))
        engine_settings = root / 'registries.conf'
        engine_settings.write_text(f'[[registry]]\nlocation = "localhost:{registry.server_port}"\ninsecure = true\n')
        environment = {**os.environ, 'CONTAINERS_REGISTRIES_CONF': str(engine_settings)}
        verify_cached_candidate(registry, images[image_sources.recipe(config, 'gateway')]['linux/amd64'], environment)
        if args.recovery:
            for marker in ('cancel-pull', 'pull-cancelled', 'retry-pull'):
                (root / marker).unlink(missing_ok=True)
            blocked_role = 'blocked-gateway-' + uuid4().hex[:8]
            blocked_ref = registry.alias(blocked_role, 'gateway')
            blocked = {**images, image_sources.recipe(config, 'gateway'): {'linux/amd64': blocked_ref}}
            lock.write_text(json.dumps({'version': 1, 'images': blocked}))
            registry.block_repository = blocked_role
            recovery = subprocess.Popen([sys.executable, '-I', '-c', bootstrap, '--pull-worker', '--output', str(root)], env=environment)
            try:
                deadline = time.monotonic() + 240
                while not registry.blocked.wait(0.1):
                    assert recovery.poll() is None, 'Pull worker exited before requesting the manifest'
                    assert time.monotonic() < deadline, 'Real pull never reached the gated manifest; inspect the creation output'
                (root / 'cancel-pull').touch()
                while not (root / 'pull-cancelled').exists():
                    assert recovery.poll() is None, 'Pull worker exited before cancellation cleanup completed'
                    assert time.monotonic() < deadline, 'Cancellation did not finish while the registry was blocked'
                    time.sleep(0.05)
                registry.block_repository = None
                registry.release.set()
                (root / 'retry-pull').touch()
                assert recovery.wait(timeout=240) == 0
            finally:
                registry.release.set()
                if recovery.poll() is None:
                    recovery.terminate()
                    recovery.wait()
            lock.write_text(json.dumps({'version': 1, 'images': images}))
        subprocess.run(command, check=True, env=environment)
        (root / 'registry-requests.json').write_text(json.dumps(registry.requests, indent=2))
        assert any('/manifests/' in path for _, path in registry.requests), 'No real registry acquisition occurred'
    print('PASS prebuilt acquisition, creation, configuration, editor, options and cleanup', flush=True)


if __name__ == '__main__':
    main()
