"""Real Podman cancellation fixtures; no inference or user sandbox changes.

Run with a cached --image. Only the expensive build/up bodies are replaced by
small real engine operations; reservation, worker transport, cancellation and
checked Factory deletion are production code.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pi_sandbox_factory import creation, host_platform, sandbox, sandbox_advisors, sandbox_ssh, inference
from pi_sandbox_factory.sandbox_tui import Service, DEFAULT_SETTINGS, preset


def engine(*args):
    return subprocess.check_output(['podman', *args], text=True).strip()


def resources(config):
    dev, proxy, *volumes = sandbox.names(config)
    advisor, advisor_proxy, *av = sandbox_advisors.names(config)
    ssh, *sv = sandbox_ssh.names(config)
    broker, *iv = inference.names(config)
    return [dev, proxy, advisor, advisor_proxy, ssh, broker], volumes + av + sv + iv


def worker(phase, image, fixture):
    original = Service.run
    def run(service, args, log):
        if args[0] == 'build':
            if phase == 'build':
                sandbox.run(['podman', 'build', '--pull=never', '--no-cache', '-t', fixture + ':latest',
                             '-f', Path(service.state).parent / 'Dockerfile', Path(service.state).parent])
            return
        if args[0] == 'up':
            config = service.config(args[1])
            containers, volumes = resources(config)
            for volume in volumes:
                engine('volume', 'create', '--label', f'{sandbox.LABEL}={config["name"]}', volume)
            for index, name in enumerate(containers):
                engine('run', '-d', '--pull=never', '--network=none', '--name', name,
                       '--label', f'{sandbox.LABEL}={config["name"]}',
                       '-v', volumes[index] + ':/fixture', image, 'sleep', '600')
            log('FIXTURE_STARTUP_READY')
            time.sleep(600)
            return
        return original(service, args, log)
    with patch.object(Service, 'run', run):
        creation.main()


def snapshot():
    ids = engine('ps', '-aq', '--no-trunc').splitlines()
    if not ids:
        return {}
    rows = json.loads(engine('inspect', *ids))
    return {row['Id']: row['State']['StartedAt'] for row in rows}


def check(image):
    image_id = engine('image', 'inspect', image, '--format', '{{.Id}}')
    before = snapshot()
    original_command = host_platform.python_command
    for phase in ('build', 'startup'):
        name = 'cancel-check-' + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory(prefix='factory-cancel-') as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'keep.txt').write_text('workspace survives cancellation\n')
            service = Service(root / 'state')
            draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name=name, workspace=str(workspace), network='unrestricted', engine='podman',
                         model_providers={}, local_model_ports='', open_editor=False)
            draft['options']['website_groups'] = []
            fixture = 'localhost/' + name
            engine('tag', image, fixture + ':base')
            (root / 'Dockerfile').write_text(f'FROM {fixture}:base\nRUN echo FIXTURE_BUILD_READY && sleep 600\n')
            job = creation.Creation()
            triggered = False
            def log(line):
                nonlocal triggered
                print(line, flush=True)
                # Wait for the RUN's output, not the printed Dockerfile instruction.
                if line == 'FIXTURE_BUILD_READY' or line == 'FIXTURE_STARTUP_READY':
                    triggered = True
                    job.cancel()
            def command(module, *args):
                if module == 'pi_sandbox_factory.creation':
                    return [sys.executable, str(Path(__file__).resolve()), '--worker', phase,
                            '--image', image, '--fixture', fixture]
                return original_command(module, *args)
            config = {'name': name, 'engine': 'podman'}
            containers, volumes = resources(config)
            try:
                with patch.object(host_platform, 'python_command', command):
                    try:
                        service.create(draft, None, log, cancellation=job)
                    except creation.Cancelled:
                        pass
                    else:
                        raise AssertionError('Creation did not report cancellation')
                assert triggered, 'Never reached the real cancellation phase'
                assert not (service.state / name).exists(), 'State retained after cancellation'
                assert (workspace / 'keep.txt').read_text() == 'workspace survives cancellation\n'
                assert not set(containers) & set(engine('ps', '-a', '--format', '{{.Names}}').splitlines())
                assert not set(volumes) & set(engine('volume', 'ls', '--format', '{{.Name}}').splitlines())
                assert engine('image', 'inspect', image, '--format', '{{.Id}}') == image_id
                after = snapshot()
                assert all(after.get(key) == start for key, start in before.items()), 'An existing container changed'
                print('PASS: ' + phase + ' cancellation, containers/volumes/state removed, workspace/cache/other containers preserved', flush=True)
            finally:
                # Emergency cleanup is scoped to this random fixture's names and labels.
                for name_ in containers:
                    result = subprocess.run(['podman', 'container', 'inspect', name_], capture_output=True, text=True)
                    if result.returncode == 0:
                        row = json.loads(result.stdout)[0]
                        if row['Config'].get('Labels', {}).get(sandbox.LABEL) == name:
                            engine('rm', '-f', name_)
                for volume in volumes:
                    result = subprocess.run(['podman', 'volume', 'inspect', volume], capture_output=True, text=True)
                    if result.returncode == 0 and json.loads(result.stdout)[0].get('Labels', {}).get(sandbox.LABEL) == name:
                        engine('volume', 'rm', volume)
                # Cancel intentionally preserves build cache. Remove only this
                # fixture's disposable intermediate storage after checking it.
                for row in json.loads(engine('ps', '-a', '--external', '--format', 'json')):
                    if row.get('State') == 'storage' and row.get('Names') == [name + '-working-container']:
                        engine('rm', '-f', row['Id'])
                engine('untag', image, fixture + ':base')
                assert engine('image', 'inspect', image, '--format', '{{.Id}}') == image_id


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='docker.io/library/debian:bookworm-slim')
    parser.add_argument('--worker', choices=('build', 'startup'))
    parser.add_argument('--fixture')
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.image, args.fixture)
    else:
        check(args.image)
