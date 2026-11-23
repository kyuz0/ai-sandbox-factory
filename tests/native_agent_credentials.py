"""Host-side checks for a real VS Code extension attempting credential access."""
import json
from pathlib import Path
import subprocess
import time


def read(container, *args):
    return subprocess.check_output(['podman', 'exec', container, *args])


def private_auth(controller, agent):
    if agent == 'claude':
        return read(controller, 'cat', '/home/node/.claude/.credentials.json')
    return read(controller, 'secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity')


def install_attack_extension(editor, controller, agent):
    auth = json.loads(private_auth(controller, agent))
    fields = auth['claudeAiOauth'] if agent == 'claude' else auth['token']
    names = ('accessToken', 'refreshToken') if agent == 'claude' else ('access_token', 'refresh_token', 'id_token')
    secrets = [fields[name].encode() for name in names if fields.get(name)]
    folder = '/opt/editor-extensions/factory.credential-attack-0.0.1'
    subprocess.run(['podman', 'exec', '--user', '0', editor, 'mkdir', '-p', folder], check=True)
    manifest = {'name': 'credential-attack', 'publisher': 'factory', 'version': '0.0.1',
                'engines': {'vscode': '^1.90.0'}, 'activationEvents': ['onStartupFinished'],
                'main': './extension.cjs'}
    source = Path(__file__).with_name('native_auth_attack_extension.cjs').read_text()
    source = 'const agent = ' + json.dumps(agent) + ';\n' + source
    for name, content in [('package.json', json.dumps(manifest)), ('extension.cjs', source)]:
        subprocess.run(['podman', 'exec', '-i', '--user', '0', editor, 'sh', '-c',
                        'cat > "$1"', 'sh', folder + '/' + name], input=content.encode(), check=True)
    return secrets


def verify_attack_extension(editor, controller, agent, secrets):
    deadline = time.monotonic() + 60
    while subprocess.run(['podman', 'exec', editor, 'test', '-f', '/qa/credential-attack-result.json']).returncode:
        if time.monotonic() >= deadline:
            raise AssertionError('The credential-access test extension did not report a result')
        time.sleep(.2)
    raw = read(editor, 'cat', '/qa/credential-attack-result.json')
    report = json.loads(raw)
    assert not report['errors'], report['errors']
    assert len(report['checks']) == 4, report
    private = private_auth(controller, agent)
    assert b'factory-hostile-extension-marker' not in private, 'Workspace write reached private authentication'
    current = json.loads(private)
    fields = current['claudeAiOauth'] if agent == 'claude' else current['token']
    secrets += [value.encode() for key, value in fields.items()
                if key in ('accessToken', 'refreshToken', 'access_token', 'refresh_token', 'id_token') and value]
    assert all(secret not in raw for secret in secrets), 'Real token was accessible to the extension'
    print(f'{agent}: actual VS Code extension could read only placeholders; workspace writes did not affect private auth.', flush=True)
