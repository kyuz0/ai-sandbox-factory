"""Check connector access and native launch instructions in disposable, offline Factory containers."""
import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from connector_live_settings import podman
from ai_sandbox_factory import connectors, primary_agents, primary_workspace, sandbox
from ai_sandbox_factory.sandbox_options import write_json
from native_auth_fixture import AUTH_SERVICE


# Capture the actual native process at launch, before it asks the user to sign in.
# No prompts are sent to a provider, and no account data is mounted.
OBSERVE_LAUNCH = AUTH_SERVICE + """
import os
import pty
import subprocess
import sys
import time

agent = sys.argv[1]
Path('/run/primary-editor/' + agent + '-ca.pem').write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes())
server, thread = start_auth_fixture(agent)
master, slave = pty.openpty()
proc = subprocess.Popen(['/usr/local/bin/' + agent], cwd='/workspace', stdin=slave, stdout=slave, stderr=slave)
os.close(slave)
try:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        if Path(f'/proc/{proc.pid}/exe').resolve() == Path('/usr/local/bin/factory-agent'):
            # Read argv after observing exec. Reading it first can capture the
            # Python wrapper just before /proc/exe changes to the native CLI.
            args = Path(f'/proc/{proc.pid}/cmdline').read_bytes().split(b'\\0')
            context_path = {'agy': '.gemini/AGENTS.md', 'claude': '.claude/CLAUDE.md'}.get(agent)
            context = (Path.home() / context_path).read_text() if context_path else str(args)
            assert 'fixture connector instructions' in context, agent + ': native instructions are missing the connector guide'
            assert 'localhost/report' in context, agent + ': native instructions omitted the granted report operation'
            assert 'localhost/private' not in context, agent + ': native instructions exposed an ungranted operation'
            assert 'fixture-only-token' not in context, agent + ': native instructions exposed a connector credential'
            assert server.auth_requests, agent + ': native client did not request synthetic authentication state'
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
    server.shutdown()
    server.server_close()
    thread.join()
"""


def check(agent, root, image=None):
    name = 'connector-client-' + uuid4().hex[:10]
    state = root / name
    runtime = state / 'primary-runtime'
    controller_inputs = state / 'primary-agent'
    runtime.mkdir(parents=True)
    controller_inputs.mkdir()
    config = {'name': name, 'engine': 'podman', 'uid': 1000, 'gid': 1000, 'primary_agent': agent,
              'workspace': str(root / 'project'), 'options': {'browser': False}}
    write_json(state / 'config.json', config)
    write_json(controller_inputs / 'settings.json', {'agent': agent, 'browser': False, 'working_directory': '/workspace'})
    primary_agents.prepare_workspace(config, state)
    primary_agents.write_context(config, state)
    workspace = podman('run', '-d', '--pull=never', '--network=none', '--userns=keep-id:uid=1000,gid=1000',
                       '--security-opt=label=disable', '--name', sandbox.names(config)[0], '--label', sandbox.LABEL + '=' + name,
                       '-v', str(runtime) + ':/run/primary-runtime:ro', '--tmpfs', '/run/primary-editor:rw,mode=1777', '--entrypoint', 'sleep',
                       image or primary_workspace.image(config), 'infinity')
    try:
        grants = {'connector_ids': ['build'], 'connector_operations': {'build': ['report']}}
        sandbox.apply_settings(config, state, grants)
        guide = podman('exec', workspace, 'cat', '/run/primary-runtime/connectors.md')
        assert 'localhost/report' in guide and 'localhost/private' not in guide
        sock = connectors.client_socket(config, 'build')
        status = ['exec', workspace, 'curl', '-so', '/dev/null', '-w', '%{http_code}', '--unix-socket', sock]
        assert podman(*status, 'http://localhost/report') == '502'  # The granted upstream is deliberately offline.
        assert podman(*status, 'http://localhost/private') == '404'
        podman('exec', workspace, 'python3', '-c', OBSERVE_LAUNCH, agent)
        saved = json.loads((state / 'config.json').read_text())
        sandbox.apply_settings(saved, state, {'connector_ids': [], 'connector_operations': {}})
        assert 'fixture connector instructions' not in (controller_inputs / 'context.md').read_text()
        assert podman('inspect', '-f', '{{.State.Running}}', workspace) == 'true'
        print(f'PASS {agent}: native launch, workspace guide, granted and denied socket operations, live removal.', flush=True)
    finally:
        podman('rm', '-f', '--ignore', connectors.worker_name(config, 'build'), workspace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', action='append', choices=('codex', 'claude', 'agy'),
                        help='Repeat to select agents; default: all three native agents')
    parser.add_argument('--image', action='append', default=[], metavar='AGENT=IMAGE',
                        help='Use a specific built workspace image instead of the current checkout identity')
    args = parser.parse_args()
    images = {}
    for value in args.image:
        agent, separator, image = value.partition('=')
        if not separator or agent not in ('codex', 'claude', 'agy') or not image:
            parser.error(f'Invalid image override {value!r}; use codex=IMAGE, claude=IMAGE or agy=IMAGE.')
        images[agent] = image
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'project').mkdir()
        manifest = {'version': 1, 'id': 'build', 'title': 'Builds', 'description': 'Read build status.',
                    'documentation': 'Synthetic service for offline checks.',
                    'agent_instructions': 'Use the fixture connector instructions.',
                    'operations': {'report': 'https://127.0.0.1:1/report', 'private': 'https://127.0.0.1:1/private'}}
        write_json(root / 'plugins/build/connector.json', manifest)
        with patch.object(connectors, 'directory', return_value=root / 'plugins'), \
                patch('ai_sandbox_factory.provider_credentials.os_store') as store:
            store.return_value.get_password.return_value = 'fixture-only-token'
            for agent in args.agent or ('codex', 'claude', 'agy'):
                check(agent, root, images.get(agent))


if __name__ == '__main__':
    main()
