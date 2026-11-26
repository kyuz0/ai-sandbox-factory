"""Exercise public, non-TTY CLI creation with provider failure and same-name retry."""
import json
import argparse
import os
from pathlib import Path
import subprocess
import signal
import queue
import sys
import tempfile
import threading
from uuid import uuid4

from ai_sandbox_factory import registry, sandbox
from creation_provider import ADAPTER, KEY


def cancel_creation(command, state, name, number):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    output = []
    def read():
        for line in process.stderr:
            output.append(line)
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read)
    reader.start()
    process.stdin.write(json.dumps({'provider_keys': {'fixture': KEY}}))
    process.stdin.close()
    try:
        while True:
            line = lines.get(timeout=600)
            assert line is not None, ''.join(output)
            if 'Configure sandbox services' in line:
                break
        process.send_signal(number)
        while True:
            line = lines.get(timeout=120)
            assert line is not None, ''.join(output)
            if 'Cancellation requested' in line:
                break
        if process.poll() is None:
            process.send_signal(number)
        assert process.wait(timeout=120) == 130, ''.join(output)
        reader.join(timeout=10)
        assert not reader.is_alive(), 'CLI stderr reader did not finish'
        stdout = process.stdout.read()
        assert not stdout.strip(), stdout
        assert KEY not in ''.join(output)
        assert not (state / name).exists(), 'Cancelled creation retained private state'
        print('PASS public CLI cancellation and repeated signal: ' + signal.Signals(number).name, flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=120)
        reader.join(timeout=10)
        process.stdout.close()
        process.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true', help='Require an installed package outside the checkout')
    args = parser.parse_args()
    if args.installed:
        assert not (sandbox.ROOT.parent / '.git').exists(), 'CLI fixture loaded the source checkout'
    with tempfile.TemporaryDirectory(prefix='factory-cli-lifecycle-') as temporary:
        root = Path(temporary)
        provider = root / 'provider'; provider.mkdir()
        (provider / 'provider.json').write_text(json.dumps({
            'version': 1, 'name': 'fixture', 'description': 'CLI creation fixture', 'entry': 'adapter.mjs',
            'inference': {'origin': 'https://models.example.com', 'basePath': '/v1',
                          'auth': {'type': 'bearer'},
                          'operations': [{'method': 'GET', 'path': '/v1/models'},
                                         {'method': 'POST', 'path': '/v1/chat/completions'}]},
            'settings': {'model': {'type': 'text', 'label': 'Model ID'},
                         'key': {'type': 'secret', 'label': 'API key'}}}))
        (provider / 'adapter.mjs').write_text(ADAPTER.replace(
            "if (settings.model === 'unavailable') {",
            "if (settings.model === 'wait') await new Promise(() => setInterval(() => {}, 1000));\n  if (settings.model === 'unavailable') {"))
        library = root / 'library'
        previous = os.environ.get('SANDBOX_REGISTRY_DIR')
        os.environ['SANDBOX_REGISTRY_DIR'] = str(library)
        try:
            registry.add('provider', 'fixture', str(provider))
            name = 'cli-lifecycle-' + uuid4().hex[:10]
            state = root / 'state'
            project = root / 'project'; project.mkdir()
            (project / 'keep').write_text('project preserved')
            config = root / 'creation.json'
            command = [sys.executable, '-m', 'ai_sandbox_factory', '--state-dir', str(state),
                       'create', name, '--config', str(config), '--start', '--json', '--secrets-stdin']
            copied = name + '-copy'
            try:
                for model in ('unavailable', 'wait', 'available'):
                    config.write_text(json.dumps({'workspace': str(project), 'profile': 'base', 'engine': 'podman',
                        'network': 'allowlist', 'memory': '2g', 'options': {'browser': False},
                        'model_providers': {'fixture': {'model': model}}}))
                    if model == 'wait':
                        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                            cancel_creation(command, state, name, number)
                        continue
                    result = subprocess.run(command, input=json.dumps({'provider_keys': {'fixture': KEY}}),
                                            capture_output=True, text=True)
                    assert KEY not in result.stdout + result.stderr, 'Creation leaked its synthetic credential'
                    print(result.stderr, flush=True)
                    if model == 'unavailable':
                        assert result.returncode == 1, result
                        assert 'HTTP 503' in result.stderr and 'Catalogue request failed' in result.stderr, result.stderr
                        assert not (state / name).exists(), 'Failed CLI creation retained private state'
                        assert not result.stdout.strip(), result.stdout
                    else:
                        assert result.returncode == 0, result.stderr
                        assert json.loads(result.stdout) == {'name': name, 'status': 'ready'}, result.stdout
                        saved = json.loads((state / name / 'config.json').read_text())
                        dev = sandbox.names(saved)[0]
                        assert sandbox.inspect(saved, dev)['State']['Running']
                        marker = sandbox.run(['podman', 'exec', dev, 'cat', '/home/node/.pi/agent/fixture-provider-ready'], capture=True)
                        assert marker.stdout == 'available', marker.stdout
                    assert (project / 'keep').read_text() == 'project preserved'
                    print('PASS public CLI provider creation: ' + model, flush=True)
                destination = root / 'other-project'
                destination.mkdir()
                result = subprocess.run([sys.executable, '-m', 'ai_sandbox_factory', '--state-dir', str(state),
                    'copy', name, copied, '--workspace', str(destination), '--secrets-stdin', '--json'],
                    input=json.dumps({'provider_keys': {'fixture': KEY}}), capture_output=True, text=True)
                assert result.returncode == 0, result.stderr
                assert KEY not in result.stdout + result.stderr
                assert json.loads(result.stdout) == {'name': copied, 'status': 'ready'}
                source = json.loads((state / name / 'config.json').read_text())
                target = json.loads((state / copied / 'config.json').read_text())
                assert target['workspace'] == str(destination)
                assert target['model_providers'][0]['settings'] == source['model_providers'][0]['settings']
                assert target['model_providers'][0]['digest'] == source['model_providers'][0]['digest']
                assert sandbox.names(target)[3] != sandbox.names(source)[3]
                assert sandbox.inspect(target, sandbox.names(target)[0])['State']['Running']
                print('PASS public CLI copy reuses provider snapshot with a separate workspace and private home', flush=True)
            finally:
                for current in (copied, name):
                    if (state / current / 'config.json').exists():
                        subprocess.run([sys.executable, '-m', 'ai_sandbox_factory', '--state-dir', str(state), 'delete', current], check=True)
                    for kind in ('container', 'volume', 'network'):
                        args = ['ps', '-aq'] if kind == 'container' else [kind, 'ls', '-q']
                        result = sandbox.run(['podman', *args, '--filter', f'label={sandbox.LABEL}={current}'], capture=True)
                        assert not result.stdout.strip(), result.stdout
                print('PASS public CLI fixture resources removed', flush=True)
        finally:
            if previous is None:
                os.environ.pop('SANDBOX_REGISTRY_DIR', None)
            else:
                os.environ['SANDBOX_REGISTRY_DIR'] = previous


if __name__ == '__main__':
    main()
