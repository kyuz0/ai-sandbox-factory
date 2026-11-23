"""Real editor credential-isolation regression; token comparisons stay on the host."""
import hashlib
import json
from pathlib import Path
import subprocess
import time


def read(container, path):
    return subprocess.check_output(['podman', 'exec', container, 'cat', path])


def install_attack_extension(editor, controller):
    private = read(controller, '/home/node/.codex/auth.json')
    baseline = hashlib.sha256(private).digest()
    tokens = json.loads(private)['tokens']
    secrets = [tokens[key] for key in ('access_token', 'refresh_token', 'id_token')]
    folder = '/opt/editor-extensions/factory.credential-attack-0.0.1'
    subprocess.run(['podman', 'exec', '--user', '0', editor, 'mkdir', '-p', folder], check=True)
    manifest = {'name': 'credential-attack', 'publisher': 'factory', 'version': '0.0.1',
                'engines': {'vscode': '^1.90.0'}, 'activationEvents': ['onStartupFinished'],
                'main': './extension.cjs'}
    for name, content in [('package.json', json.dumps(manifest).encode()),
                          ('extension.cjs', Path(__file__).with_name('codex_auth_attack_extension.cjs').read_bytes())]:
        subprocess.run(['podman', 'exec', '-i', '--user', '0', editor, 'sh', '-c',
                        'cat > "$1"', 'sh', folder + '/' + name], input=content, check=True)
    return baseline, secrets


def verify_attack_extension(editor, controller, snapshot):
    baseline, secrets = snapshot
    deadline = time.monotonic() + 60
    while True:
        result = subprocess.run(['podman', 'exec', editor, 'test', '-f', '/qa/credential-attack-result.json'])
        if result.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise AssertionError('The credential-access test extension did not report a result')
        time.sleep(.2)
    report = json.loads(read(editor, '/qa/credential-attack-result.json'))
    assert not report['errors'], report['errors']
    assert len(report['checks']) == 4, report
    workspace = read(editor, '/home/node/.codex/auth.json')
    assert all(secret.encode() not in workspace for secret in secrets), 'Workspace contains a real token'
    assert hashlib.sha256(read(controller, '/home/node/.codex/auth.json')).digest() == baseline, \
        'The private credential file changed during the extension test'
    print('Real VS Code extension: credential reads contain only placeholders; writes cannot change private auth.', flush=True)
