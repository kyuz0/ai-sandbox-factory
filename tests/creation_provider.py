"""Exercise provider setup through the real Factory creation job and Podman."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_sandbox_factory import inference, operation_locks, registry, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset
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


def main():
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
                        instructions = sandbox.run(['podman', 'exec', container, 'cat',
                                                    '/home/node/.pi/agent/AGENTS.md'], capture=True).stdout
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
