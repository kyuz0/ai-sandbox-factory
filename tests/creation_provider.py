"""Exercise provider setup through the real Factory creation job and Podman."""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_sandbox_factory import creation, inference, operation_locks, registry, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.operations import DEFAULT_SETTINGS, Service, preset
from ai_sandbox_factory.updating import Update


KEY = 'synthetic-creation-fixture-key'
ADAPTER = """
import {writeFile} from 'node:fs/promises';
import {join} from 'node:path';

export async function run({agentDir, settings, secret}) {
  if (secret !== 'factory-inference') throw Error('Expected a managed credential placeholder');
  if (settings.model === 'unavailable') {
    throw Error('Catalogue request failed', {cause: Error('HTTP 503 from the fixture')});
  }
  await writeFile(join(agentDir, 'fixture-provider-ready'), settings.model);
  return {state: 'ready', message: 'Fixture catalogue loaded'};
}
"""


def check_resource_conflicts(service, workspace, image):
    """CLI preflight must preserve stopped containers and unclaimed volume data."""
    def engine(*args):
        return sandbox.run(['podman', *args], capture=True).stdout

    if image is None:
        images = engine('images', '--filter', 'reference=localhost/pi-sandbox-factory-dev:*',
                        '--format', '{{.ID}}').splitlines()
        assert images, 'Build a Factory development image or supply --image with a cached image containing /bin/sh'
        image = images[0]
    for kind in ('container', 'volume'):
        name = 'creation-conflict-' + uuid4().hex
        config = {'name': name, 'engine': 'podman'}
        dev, _, _, home = sandbox.names(config)
        resource = dev if kind == 'container' else home
        created = False
        try:
            if kind == 'container':
                engine('create', '--pull=never', '--network=none', '--name', resource,
                       '--entrypoint', '/bin/sh', image, '-c', 'exit 0')
                created = True
                engine('start', '--attach', resource)
                before = json.loads(engine('inspect', resource))[0]
                assert before and not before['State']['Running'], before
            else:
                engine('volume', 'create', resource)
                created = True
                engine('run', '--rm', '--pull=never', '--network=none', '-v', resource + ':/fixture',
                       '--entrypoint', '/bin/sh', image, '-c',
                       'printf preserved-volume-data > /fixture/keep.txt')
                before = json.loads(engine('volume', 'inspect', resource))
                assert sandbox.inspect(config, dev) is None, 'Volume-only fixture has a container'
            result = subprocess.run(service.command('create', name, '--engine', 'podman',
                                    '--workspace', str(workspace)), capture_output=True, text=True)
            assert result.returncode, f'CLI accepted an existing {kind}: {resource}'
            assert 'This name is already used by container resources. Choose another sandbox name.' in result.stderr, result.stderr
            assert ('Containers' if kind == 'container' else 'Volumes') + '\n' + resource in result.stderr, result.stderr
            assert not (service.state / name).exists(), 'Collision preflight wrote sandbox state'
            if kind == 'container':
                after = json.loads(engine('inspect', resource))[0]
                assert after['Id'] == before['Id'] and after['State'] == before['State'], after
            else:
                assert json.loads(engine('volume', 'inspect', resource)) == before
                assert engine('run', '--rm', '--pull=never', '--network=none', '-v', resource + ':/fixture:ro',
                              '--entrypoint', '/bin/sh', image, '-c',
                              'cat /fixture/keep.txt') == 'preserved-volume-data'
            print(f'PASS CLI creation rejects {kind} conflict before saving state and preserves {resource}', flush=True)
        finally:
            if created:
                engine(*(['rm', resource] if kind == 'container' else ['volume', 'rm', resource]))
    check_approved_cleanup(service, workspace, image)


def check_approved_cleanup(service, workspace, image):
    """Approved cleanup removes exact resources and preserves similarly named ones."""
    def engine(*args):
        return sandbox.run(['podman', *args], capture=True).stdout

    name = 'creation-cleanup-' + uuid4().hex
    draft = {'name': name, 'engine': 'podman', 'workspace': str(workspace)}
    container, _, _, volume = sandbox.names(draft)
    neighbor = container + '-neighbor'
    neighbor_volume = volume + '-neighbor'
    containers, volumes = [], []
    try:
        for current_container, current_volume in ((container, volume), (neighbor, neighbor_volume)):
            engine('volume', 'create', current_volume)
            volumes.append(current_volume)
            engine('create', '--pull=never', '--network=none', '--name', current_container,
                   '-v', current_volume + ':/fixture', '--entrypoint', '/bin/sh', image,
                   '-c', 'printf preserved-cleanup-data > /fixture/keep.txt')
            containers.append(current_container)
            engine('start', '--attach', current_container)
        neighbor_before = json.loads(engine('inspect', neighbor))[0]
        neighbor_volume_before = json.loads(engine('volume', 'inspect', neighbor_volume))
        approved = creation.check_draft(service.state, draft)
        identifier = json.loads(engine('inspect', container))[0]['Id']
        assert approved.containers == ((container, identifier),), approved
        assert len(approved.volumes) == 1 and approved.volumes[0][0] == volume, approved
        fingerprint = json.loads(approved.volumes[0][1])
        inspected_volume = json.loads(engine('volume', 'inspect', volume))[0]
        assert fingerprint == {field: inspected_volume.get(field) for field in
                               ('CreatedAt', 'Driver', 'Mountpoint', 'Labels', 'Options')}, fingerprint
        remaining = creation.check_draft(service.state, draft, approved)
        assert not remaining.containers and not remaining.volumes, remaining
        assert subprocess.run(['podman', 'container', 'exists', container]).returncode == 1
        assert subprocess.run(['podman', 'volume', 'exists', volume]).returncode == 1
        assert not (service.state / name).exists(), 'Approved cleanup wrote sandbox state'
        neighbor_after = json.loads(engine('inspect', neighbor))[0]
        assert neighbor_after['Id'] == neighbor_before['Id'] and neighbor_after['State'] == neighbor_before['State']
        assert json.loads(engine('volume', 'inspect', neighbor_volume)) == neighbor_volume_before
        assert engine('run', '--rm', '--pull=never', '--network=none',
                      '-v', neighbor_volume + ':/fixture:ro', '--entrypoint', '/bin/sh', image,
                      '-c', 'cat /fixture/keep.txt') == 'preserved-cleanup-data'
        print('PASS approved creation cleanup removes only listed resources and preserves neighboring resources and data', flush=True)
    finally:
        for current in reversed(containers):
            if subprocess.run(['podman', 'container', 'exists', current]).returncode == 0:
                engine('rm', current)
        for current in reversed(volumes):
            if subprocess.run(['podman', 'volume', 'exists', current]).returncode == 0:
                engine('volume', 'rm', current)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--conflicts-only', action='store_true', help='Only check CLI name conflicts; do not build images')
    parser.add_argument('--image', help='Cached image containing /bin/sh for disposable conflict fixtures')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-creation-provider-') as directory:
        root = Path(directory)
        workspace = root / 'workspace'
        workspace.mkdir()
        (workspace / 'project').mkdir()
        (workspace / 'keep.txt').write_text('project files stay in place')
        provider = root / 'provider'
        provider.mkdir()
        example = Path(__file__).resolve().parents[1] / 'examples/provider/provider.json'
        (provider / 'provider.json').write_bytes(example.read_bytes())
        (provider / 'adapter.mjs').write_text(ADAPTER)
        service = Service(root / 'state')
        check_resource_conflicts(service, workspace, args.image)
        if args.conflicts_only:
            return
        with patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(root / 'packages')):
            registry.add('provider', 'fixture', str(provider))
            name = 'provider-create-' + uuid4().hex[:10]
            state = service.state / name
            for model in ('unavailable', 'available'):
                draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name=name, workspace=str(workspace), engine='podman',
                             model_providers={'fixture': {'model': model}},
                             _provider_keys={'fixture': KEY})
                output = []
                def log(line):
                    assert KEY not in line, 'Credential was written to job output'
                    output.append(line)
                    print(line, flush=True)
                try:
                    try:
                        service.create(draft, None, log, cancellation=Creation())
                    except ValueError as error:
                        assert model == 'unavailable', str(error)
                        assert all(part in str(error) for part in (
                            'Step 6 of 6', 'refresh provider fixture', 'HTTP 503')), str(error)
                        assert any('Catalogue request failed' in line for line in output), output
                        assert not any(line.startswith('Traceback') for line in output), output
                        assert not state.exists(), 'Failed attempt retained its state'
                        config = {'name': name, 'engine': 'podman'}
                        assert all(sandbox.inspect(config, n) is None for n in
                                   (*sandbox.names(config)[:2], inference.names(config)[0]))
                    else:
                        assert model == 'available', 'Provider error did not fail creation'
                        config = service.config(name)
                        container = sandbox.names(config)[0]
                        assert sandbox.inspect(config, container)['State']['Running']
                        result = sandbox.run(['podman', 'exec', container, 'cat',
                                              '/home/node/.pi/agent/fixture-provider-ready'], capture=True)
                        assert result.stdout == model, result.stdout
                        snapshot = sandbox.run(['podman', 'exec', container, 'cat',
                                                '/opt/pi-security/config/sandbox-state.json'], capture=True).stdout
                        instructions = json.loads(snapshot)['securityGuidance']
                        assert 'AI Sandbox Factory workspace' in instructions
                        assert 'Network is Restricted' in instructions
                        assert 'wait for confirmation' in instructions
                        assert 'without sudo' in instructions
                        assert any('Fixture catalogue loaded' in line for line in output), output
                        check_existing(service, name, config, container, root, log)
                    assert (workspace / 'keep.txt').read_text() == 'project files stay in place'
                    print(f'PASS managed provider creation: {model}', flush=True)
                finally:
                    if (state / 'config.json').exists():
                        service.run(['delete', name], log)


def check_existing(service, name, config, container, root, log):
    previous = sandbox.inspect(config, container)['Id']
    settings = root / 'settings.json'
    settings.write_text(json.dumps({'working_directory': 'project'}))
    with operation_locks.sandbox(name, 'check update for'):
        blocked = subprocess.run(service.command('down', name), capture_output=True, text=True)
        assert blocked.returncode and 'another Factory operation is using this sandbox' in blocked.stderr
        # An actual competing command must remain blocked, but Factory's own
        # cancellable update child must reuse the lock already held by the job.
        service.run_update(['update', name, '--settings-file', str(settings)], log, Update())
    assert service.config(name)['working_directory'] == 'project'
    service.models(name, 'refresh', log)
    current = sandbox.inspect(config, container)
    assert current['Id'] == previous and current['State']['Running']
    print('PASS settings and model refresh reuse the lifecycle lock without restarting the sandbox', flush=True)


if __name__ == '__main__':
    main()
