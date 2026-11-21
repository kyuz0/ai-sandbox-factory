"""Check connector access and native launch instructions in disposable, offline Factory containers."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from connector_live_settings import podman
from pi_sandbox_factory import connectors, primary_agents, primary_workspace, sandbox
from pi_sandbox_factory.sandbox_options import write_json


# Capture the actual native process at launch, before it asks the user to sign in.
# No prompts are sent to a provider, and no account data is mounted.
OBSERVE_LAUNCH = """
import os
from pathlib import Path
import pty
import subprocess
import sys
import time

master, slave = pty.openpty()
proc = subprocess.Popen(['python3', '/opt/primary/launch.py', 'chat'], stdin=slave, stdout=slave, stderr=slave)
os.close(slave)
try:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        args = Path(f'/proc/{proc.pid}/cmdline').read_bytes().split(b'\\0')
        if args[0] == b'/usr/local/bin/factory-agent':
            context = (Path.home() / '.gemini/AGENTS.md').read_text() if sys.argv[1] == 'agy' else str(args)
            assert 'fixture connector instructions' in context
            assert 'localhost/report' in context and 'localhost/private' not in context
            assert 'fixture-only-token' not in context
            break
        time.sleep(.02)
    else:
        raise AssertionError('The native client did not launch with connector instructions')
finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    os.close(master)
"""


def check(agent, root):
    name = 'connector-client-' + uuid4().hex[:10]
    state = root / name
    runtime = state / 'primary-runtime'
    controller_inputs = state / 'primary-agent'
    runtime.mkdir(parents=True)
    controller_inputs.mkdir()
    config = {'name': name, 'engine': 'podman', 'uid': 1000, 'gid': 1000, 'primary_agent': agent,
              'workspace': str(root / 'project')}
    write_json(state / 'config.json', config)
    write_json(controller_inputs / 'settings.json', {'agent': agent, 'browser': False, 'working_directory': '/workspace'})
    primary_agents.write_context(config, state)
    workspace = podman('run', '-d', '--pull=never', '--network=none', '--userns=keep-id:uid=1000,gid=1000',
                       '--security-opt=label=disable', '--name', sandbox.names(config)[0], '--label', sandbox.LABEL + '=' + name,
                       '-v', str(runtime) + ':/run/primary-runtime:ro', '--entrypoint', 'sleep',
                       primary_workspace.image(config), 'infinity')
    try:
        grants = {'connector_ids': ['build'], 'connector_operations': {'build': ['report']}}
        sandbox.apply_settings(config, state, grants)
        guide = podman('exec', workspace, 'cat', '/run/primary-runtime/connectors.md')
        assert 'localhost/report' in guide and 'localhost/private' not in guide
        sock = connectors.client_socket(config, 'build')
        status = ['exec', workspace, 'curl', '-so', '/dev/null', '-w', '%{http_code}', '--unix-socket', sock]
        assert podman(*status, 'http://localhost/report') == '502'  # The granted upstream is deliberately offline.
        assert podman(*status, 'http://localhost/private') == '404'
        podman('run', '--rm', '--pull=never', '--network=none', '--userns=keep-id:uid=1000,gid=1000',
               '--security-opt=label=disable', '-v', str(controller_inputs) + ':/run/primary-input:ro',
               '--entrypoint', 'python3', primary_agents.image(config), '-c', OBSERVE_LAUNCH, agent)
        saved = json.loads((state / 'config.json').read_text())
        sandbox.apply_settings(saved, state, {'connector_ids': [], 'connector_operations': {}})
        assert 'fixture connector instructions' not in (controller_inputs / 'context.md').read_text()
        assert podman('inspect', '-f', '{{.State.Running}}', workspace) == 'true'
        print(f'PASS {agent}: native launch, workspace guide, granted and denied socket operations, live removal.', flush=True)
    finally:
        podman('rm', '-f', '--ignore', connectors.worker_name(config, 'build'), workspace)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'project').mkdir()
        manifest = {'version': 1, 'id': 'build', 'title': 'Builds', 'description': 'Read build status.',
                    'documentation': 'Synthetic service for offline checks.',
                    'agent_instructions': 'Use the fixture connector instructions.',
                    'operations': {'report': 'https://127.0.0.1:1/report', 'private': 'https://127.0.0.1:1/private'}}
        write_json(root / 'plugins/build/connector.json', manifest)
        with patch.object(connectors, 'directory', return_value=root / 'plugins'), \
                patch('pi_sandbox_factory.provider_credentials.os_store') as store:
            store.return_value.get_password.return_value = 'fixture-only-token'
            for agent in ('codex', 'claude', 'agy'):
                check(agent, root)


if __name__ == '__main__':
    main()
