"""Exercise reviewed helper and workspace updates with an authenticated Pi editor."""
import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from ai_sandbox_factory import (browser_editor, container_update_apply, container_updates,
                                editor_settings, image_builds, image_sources, operation_locks, profile_runtime, sandbox)
from browser_editor_lifecycle import request
from browser_editor_updates import workspace_config


def check(root, fresh_image=None):
    name = 'update-containers-' + uuid4().hex[:12]
    repository = 'localhost/' + name
    state = root / 'state' / name
    project = root / 'project'
    state.mkdir(parents=True)
    project.mkdir()
    (project / 'marker').write_text('workspace data')
    config = {**workspace_config(), 'name': name, 'workspace': str(project),
              'network': 'allowlist', 'image_source': 'prebuilt'}
    for key in ('profile', 'common'):
        config[key] = sandbox.snapshot_profile(config[key], state)
    tags = set()

    def stage(image, suffix=''):
        tag = repository + ':' + hashlib.sha256((image + suffix).encode()).hexdigest()[:16]
        tags.add(tag)
        return tag

    def saved():
        return json.loads((state / 'config.json').read_text())

    def apply(role, *, fault=None):
        cfg = saved()
        reviewed = container_updates.plan(cfg, state)
        assert reviewed['status'] == 'available' and reviewed['roles'] == [role], reviewed
        if fault:
            with fault:
                try:
                    container_update_apply.apply(cfg, state, reviewed)
                except ValueError as error:
                    assert 'fixture fault after readiness' in str(error), str(error)
                else:
                    raise AssertionError('The update ignored the injected failure')
        else:
            container_update_apply.apply(cfg, state, reviewed)

    factory_recipe = image_sources.recipe
    artifacts = []
    refs = {}
    old_recipes = {}
    engine = sandbox.run
    def isolated_build_tags(argv, **options):
        # Building a local fallback must not retag a developer's shared images.
        def private(argument):
            if not isinstance(argument, str):
                return argument
            prefix = 'BASE_IMAGE=' if argument.startswith('BASE_IMAGE=') else ''
            ref = argument.removeprefix(prefix)
            if ref == 'localhost/pi-sandbox-factory-proxy:1' or ref.startswith('localhost/pi-sandbox-factory-base:'):
                return prefix + stage(ref)
            return argument
        return engine([private(arg) for arg in argv], **options)

    platform = image_sources._platform('podman', None)
    with operation_locks.sandbox(name, 'test reviewed container updates for'), \
            patch.object(sandbox, 'stage_image', stage), patch.object(sandbox, 'run', isolated_build_tags):
        try:
            # A labeled child of each published image gives the prior runtime a
            # different immutable ID with the same fully functional applications.
            catalog = image_sources.index()
            stock = catalog.get(factory_recipe(config, 'pi-stock'), {}).get(platform)
            base_role = 'pi-stock' if stock else 'pi-common'
            workspace_source = stock or fresh_image
            if not workspace_source:
                preparation = copy.deepcopy(config)
                sandbox.build(preparation)
                workspace_source = preparation['image_refs']['workspace']
            gateway_source = catalog.get(factory_recipe(config, 'gateway'), {}).get(platform)
            if not gateway_source:
                preparation = copy.deepcopy(config)
                with operation_locks.proxy_image(config['engine']):
                    image_builds.gateway(preparation)
                gateway_source = preparation['image_refs']['gateway']
            for role, ref in ((base_role, workspace_source), ('gateway', gateway_source)):
                if not ref.startswith(('docker.io/', 'localhost/')):
                    alias = repository + ':source-' + role
                    subprocess.run(['podman', 'tag', ref, alias], check=True)
                    tags.add(alias)
                    ref = alias
                old_recipes[role] = hashlib.sha256(('fixture prior ' + role).encode()).hexdigest()
                tag = repository + ':prior-' + role
                tags.add(tag)
                recipe = root / 'Dockerfile'
                recipe.write_text(f'FROM {ref}\nLABEL factory-test-prior={name}\n')
                subprocess.run(['podman', 'build', '--pull=missing', '-t', tag, '-f', str(recipe), str(root / 'context')], check=True)
                image_id = image_sources._inspect(config, tag)['Id']
                refs[role] = image_id
                artifacts.append(dict(role=role, source='local', recipe=old_recipes[role],
                                      reference=image_id, image_id=image_id, platform=platform))
            config.update(image_refs={**refs, 'workspace': refs[base_role]}, image_artifacts=artifacts)
            (state / 'allowlist.txt').write_text('')
            (state / 'squid.conf').write_text(sandbox.network_policy(config, []))
            sandbox.write_json(state / 'config.json', config)
            with profile_runtime.recording(config, state):
                pass
            sandbox.up(config, state)
            workspace, gateway = sandbox.names(config)[:2]
            sandbox.run(['podman', 'exec', workspace, 'sh', '-c', 'echo private > /home/node/update-marker'])
            password = 'disposable-update-test'
            editor_settings.save(state, {'mode': 'browser', 'password_hash': editor_settings.password_hash(password)})
            browser_editor.open_browser_editor(config, state, background=True, open_tab=False)
            connection = browser_editor.runtime(state)
            status, headers, _ = request(connection['url'], '/login', method='POST',
                                         body=urlencode({'password': password}),
                                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
            assert status == 302
            cookie = headers['Set-Cookie'].split(';')[0]

            def verify_editor():
                sandbox.wait_gateway(saved())
                active = browser_editor.runtime(state)
                assert active['url'] == connection['url'] and active['preferences'] == connection['preferences']
                assert active['container'] == sandbox.inspect(saved(), workspace)['Id']
                assert request(active['url'], headers={'Cookie': cookie})[0] == 200
                assert request(active['url'])[0] == 302
                assert (project / 'marker').read_text() == 'workspace data'
                assert sandbox.run(['podman', 'exec', workspace, 'cat', '/home/node/update-marker'],
                                   capture=True).stdout.strip() == 'private'

            workspace_id = sandbox.inspect(config, workspace)['Id']
            gateway_id = sandbox.inspect(config, gateway)['Id']
            def gateway_release(cfg, role):
                return old_recipes[role] if role == base_role else factory_recipe(cfg, role)
            # Two successive Factory releases: first the gateway, then the workspace.
            with patch.object(image_sources, 'recipe', gateway_release):
                apply('gateway', fault=patch.object(container_update_apply, '_verify',
                                                    side_effect=ValueError('fixture fault after readiness')))
                assert sandbox.inspect(config, gateway)['Id'] == gateway_id
                assert sandbox.inspect(config, workspace)['Id'] == workspace_id
                verify_editor()
                print('PASS gateway rollback retained both IDs and the authenticated editor', flush=True)
                apply('gateway')
                assert sandbox.inspect(saved(), gateway)['Id'] != gateway_id
                assert sandbox.inspect(saved(), workspace)['Id'] == workspace_id
                verify_editor()
                assert container_updates.plan(saved(), state)['status'] == 'current'
                print('PASS gateway update preserved workspace ID, data and authenticated editor', flush=True)
            gateway_id = sandbox.inspect(saved(), gateway)['Id']
            resume = browser_editor.resume_connection
            def fail_once(cfg, directory, snapshot):
                if sandbox.inspect(cfg, workspace)['Id'] != workspace_id:
                    raise ValueError('fixture fault after readiness')
                return resume(cfg, directory, snapshot)
            apply(base_role, fault=patch.object(browser_editor, 'resume_connection', side_effect=fail_once))
            assert sandbox.inspect(saved(), workspace)['Id'] == workspace_id
            assert sandbox.inspect(saved(), gateway)['Id'] == gateway_id
            verify_editor()
            print('PASS workspace rollback restored the previous ID and authenticated editor', flush=True)
            apply(base_role)
            assert sandbox.inspect(saved(), workspace)['Id'] != workspace_id
            assert sandbox.inspect(saved(), gateway)['Id'] == gateway_id
            verify_editor()
            assert container_updates.plan(saved(), state)['status'] == 'current'
            print('PASS workspace update kept the gateway ID, data and authenticated editor', flush=True)

            workspace_id = sandbox.inspect(saved(), workspace)['Id']
            receipt = state / 'container-updates/applied.json'
            sandbox.write_json(receipt, [])
            sandbox.up(saved(), state)
            assert container_updates.plan(saved(), state)['status'] == 'current'
            assert sandbox.inspect(saved(), workspace)['Id'] == workspace_id
            verify_editor()
            print('PASS ordinary startup repaired the receipt with the workspace and editor connected', flush=True)

            evidence = json.loads(receipt.read_text())
            evidence['roles']['gateway']['recipe'] = old_recipes['gateway']
            sandbox.write_json(receipt, evidence)
            cli_dir = sandbox.ROOT.parent
            if image_sources.INDEX.read_bytes() != (sandbox.ROOT / 'images.lock.json').read_bytes():
                cli_dir = root / 'candidate-cli'
                shutil.copytree(sandbox.ROOT, cli_dir / 'ai_sandbox_factory', ignore=shutil.ignore_patterns('__pycache__'))
                shutil.copyfile(image_sources.INDEX, cli_dir / 'ai_sandbox_factory/images.lock.json')
            handoff = operation_locks.factory_child(config['name'])
            if not catalog.get(factory_recipe(config, 'gateway'), {}).get(platform):
                # A local-mode child must use the same private mutable build tag
                # as the in-process stages, while invoking the real Podman engine.
                tools = root / 'bin'
                tools.mkdir()
                wrapper = tools / 'podman'
                podman = shutil.which('podman')
                alias = stage('localhost/pi-sandbox-factory-proxy:1')
                wrapper.write_text('#!' + sys.executable + '\nimport os, sys\n'
                                   + f'engine = {podman!r}\n'
                                   + f'args = [{alias!r} if arg == "localhost/pi-sandbox-factory-proxy:1" else arg for arg in sys.argv[1:]]\n'
                                   + 'os.execv(engine, [engine, *args])\n')
                wrapper.chmod(0o755)
                handoff['env']['PATH'] = str(tools) + os.pathsep + handoff['env']['PATH']
            command = [sys.executable, '-m', 'ai_sandbox_factory', '--state-dir', str(state.parent),
                       'update-containers', config['name']]
            def cli(*args):
                result = subprocess.run([*command, *args, '--json'], cwd=cli_dir, **handoff,
                                        text=True, capture_output=True)
                assert result.returncode == 0, result.stderr + result.stdout
                return json.loads(result.stdout)
            plan = cli('--preview')
            assert plan['roles'] == ['gateway'] and plan['status'] == 'available', plan
            assert cli('--revision', plan['revision'])['status'] == 'complete'
            assert cli('--preview')['status'] == 'current'
            assert sandbox.inspect(saved(), gateway)['Id'] != gateway_id
            assert sandbox.inspect(saved(), workspace)['Id'] == workspace_id
            verify_editor()
            print('PASS CLI preview and supervised update replaced only the gateway and cleared the update', flush=True)

            # Reopen the fixture's original versions, then exercise the same
            # mixed-role update while its editor and both containers are stopped.
            browser_editor.stop(state)
            sandbox.down(saved(), remove=True)
            sandbox.write_json(state / 'config.json', config)
            sandbox.up(config, state)
            sandbox.down(config)
            stopped = {name: sandbox.inspect(config, name) for name in (workspace, gateway)}
            assert all(not info['State']['Running'] for info in stopped.values())
            reviewed = container_updates.plan(config, state)
            assert reviewed['status'] == 'available' and set(reviewed['roles']) == {base_role, 'gateway'}, reviewed
            container_update_apply.apply(config, state, reviewed)
            for name, previous in stopped.items():
                after = sandbox.inspect(saved(), name)
                assert after['Id'] != previous['Id'] and not after['State']['Running'], name
            assert browser_editor.runtime(state) is None
            assert container_updates.plan(saved(), state)['status'] == 'current'
            print('PASS mixed update kept the workspace, gateway and editor stopped', flush=True)
        finally:
            browser_editor.stop(state)
            if (state / 'config.json').exists():
                sandbox.delete(saved(), state)
            for tag in tags:
                found = subprocess.run(['podman', 'image', 'exists', tag])
                if found.returncode == 0:
                    subprocess.run(['podman', 'rmi', '--no-prune', tag], check=True)
                else:
                    assert found.returncode == 1, found.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Workspace image from the successful fresh startup check')
    parser.add_argument('--candidate-index', type=Path, help='Exact tested image catalog for this run')
    args = parser.parse_args()
    if args.candidate_index:
        image_sources.INDEX = args.candidate_index.resolve()
    with tempfile.TemporaryDirectory(prefix='factory-reviewed-updates-') as temporary:
        root = Path(temporary)
        (root / 'context').mkdir()
        check(root, args.image)
