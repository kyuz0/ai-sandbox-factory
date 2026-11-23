"""Run the real connector settings path against a disposable offline workspace."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest.mock as mock
import uuid

from ai_sandbox_factory import connectors, sandbox
from ai_sandbox_factory.sandbox_options import write_json


def podman(*args, **kwargs):
    try:
        return subprocess.run(['podman', *args], text=True, capture_output=True, check=True, **kwargs).stdout.strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or 'No diagnostic output was returned.').strip()
        raise RuntimeError(f'podman {args[0]} exited {error.returncode}: {detail}') from None


def main():
    name = 'connector-check-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / name
        runtime = state / 'runtime-config'
        runtime.mkdir(parents=True)
        plugin_dir = Path(tmp) / 'plugins'
        manifest = {'version': 1, 'id': 'release', 'title': 'Release', 'description': 'Build status',
                    'documentation': 'Test data only.', 'operations': {
                        'build': 'https://127.0.0.1:1/build', 'report': 'https://127.0.0.1:1/report'}}
        write_json(plugin_dir / 'release' / 'connector.json', manifest)
        config = {'name': name, 'engine': 'podman', 'uid': 1000, 'gid': 1000}
        write_json(state / 'config.json', config)
        workspace = sandbox.names(config)[0]
        worker = connectors.worker_name(config, 'release')
        socket = connectors.client_socket(config, 'release')
        workspace_id = podman('run', '-d', '--name', workspace, '--label', sandbox.LABEL + '=' + name,
                              '--network=none', '--user=1000:1000', '--userns=keep-id:uid=1000,gid=1000',
                              '--security-opt=label=disable', '-v', str(runtime) + ':/opt/pi-security/config:ro',
                              'docker.io/library/python:3.13-slim', 'sleep', 'infinity')
        def status(operation):
            return podman('exec', workspace_id, 'python3', '-c',
                          'import socket,http.client,sys; c=http.client.HTTPConnection("localhost"); '
                          'c.sock=socket.socket(socket.AF_UNIX); c.sock.connect(sys.argv[1]); '
                          'c.request("GET","/"+sys.argv[2]); print(c.getresponse().status)', socket, operation)
        def apply(ids, ops):
            current = json.loads((state / 'config.json').read_text())
            sandbox.apply_settings(current, state, {'connector_ids': ids, 'connector_operations': ops,
                'expected_connector_ids': current.get('connector_ids', []),
                'expected_connector_operations': current.get('connector_operations', {})})

        try:
            with mock.patch.object(connectors, 'directory', return_value=plugin_dir), \
                    mock.patch('ai_sandbox_factory.provider_credentials.os_store') as store:
                store.return_value.get_password.return_value = 'fixture-only-token'
                with mock.patch.object(connectors, 'client_socket', return_value='/missing-mount/api.sock'):
                    try:
                        apply(['release'], {'release': ['build']})
                    except ValueError as exc:
                        assert 'could not open the socket' in str(exc)
                    else:
                        raise AssertionError('An unreachable workspace socket passed its readiness check')
                    assert subprocess.run(['podman', 'container', 'exists', worker]).returncode == 1
                    assert json.loads((state / 'config.json').read_text()) == config
                    assert podman('inspect', '-f', '{{.State.Running}}', workspace_id) == 'true'
                apply(['release'], {'release': ['build']})
                worker_id = podman('inspect', '-f', '{{.Id}}', worker)
                assert status('report') == '404' and status('build') == '502'
                store.return_value.get_password.side_effect = AssertionError('Unnecessary token read')
                apply(['release'], {'release': ['report']})
                assert status('report') == '502' and status('build') == '404'
                assert podman('inspect', '-f', '{{.Id}}', worker) == worker_id
                assert podman('inspect', '-f', '{{.Id}}', workspace) == workspace_id
                assert 'fixture-only-token' not in podman('inspect', worker, workspace)
                apply([], {})
                assert subprocess.run(['podman', 'container', 'exists', worker]).returncode == 1
                podman('stop', workspace)
                store.return_value.get_password.side_effect = None
                store.return_value.get_password.return_value = None
                try:
                    apply(['release'], {'release': ['build']})
                except ValueError as exc:
                    assert 'connector-login release' in str(exc)
                else:
                    raise AssertionError('A stopped sandbox accepted a grant without a saved token')
                store.return_value.get_password.return_value = 'fixture-only-token'
                apply(['release'], {'release': ['build']})
                assert subprocess.run(['podman', 'container', 'exists', worker]).returncode == 1
                assert podman('inspect', '-f', '{{.State.Running}}', workspace) == 'false'
                apply([], {})
                print('PASS socket-failure rollback, live connection probe, hot grant changes, revocation, and stopped-sandbox token preflight.')
        finally:
            subprocess.run(['podman', 'rm', '-f', '--ignore', worker, workspace_id], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
