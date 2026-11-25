"""Exercise native Antigravity sandbox updates in an isolated credential image.

Run with the credential image's /opt/agy-proxy/bin/python, network disabled,
a writable empty /run/primary-runtime and the current worker hook installed at
/opt/sandbox/sandbox-update.py. Prompts and credentials are synthetic.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time


SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
SNAPSHOT = Path('/run/primary-runtime/sandbox-state.json')
MARKER = 'Sandbox update: current Factory settings.'
REPLY = 'Synthetic Antigravity reply.'


def append_event(path, data):
    with path.open('a') as stream:
        stream.write(json.dumps(data) + '\n')


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def observe(directory):
    event = json.load(sys.stdin)
    fields = ('hookEventName', 'conversationId', 'initialNumSteps', 'invocationNum', 'lastUserInput')
    append_event(directory / 'hooks.jsonl', {key: event[key] for key in fields})
    print('{}')


if os.environ.get('FACTORY_AGY_UPDATE_ENDPOINT'):
    from mitmproxy import http

    class SyntheticService:
        def request(self, flow):
            request = flow.request
            rpc = request.path.split('?', 1)[0].split(':')[-1]
            directory = Path(os.environ['FACTORY_AGY_UPDATE_ENDPOINT'])
            if rpc == 'streamGenerateContent':
                body = request.json()['request']
                # Count previous updates as well as new ones. The server receives
                # the conversation history again on each model invocation.
                updates = [
                    part['text'] for item in body['contents'] for part in item.get('parts', [])
                    if MARKER in part.get('text', '')
                ]
                append_event(directory / 'wire.jsonl', updates)
                if (directory / 'reject-next').exists():
                    (directory / 'reject-next').unlink()
                    flow.response = http.Response.make(400, json.dumps({'error': {
                        'code': 400, 'message': 'Synthetic rejected invocation'}}), {'Content-Type': 'application/json'})
                    return
                reply = {'response': {'candidates': [{'content': {'role': 'model', 'parts': [{'text': REPLY}]},
                                                       'finishReason': 'STOP'}]}}
                flow.response = http.Response.make(200, 'data: ' + json.dumps(reply) + '\n\n',
                                                   {'Content-Type': 'text/event-stream'})
                return
            if rpc == 'loadCodeAssist':
                value = {'cloudaicompanionProject': 'synthetic-project', 'currentTier': {'id': 'standard-tier'}}
            elif rpc == 'listExperiments':
                # The pinned native client discovers file hooks only when this
                # customization kind is enabled by the service.
                value = {'flags': [{'name': 'enable-customization-kind-hooks', 'boolValue': True}]}
            elif rpc == 'fetchAvailableModels':
                model = 'gemini-3-flash'
                value = {'models': {model: {'displayName': 'Synthetic model', 'displayProvider': 'Google',
                                            'model': 'MODEL_PLACEHOLDER_M26'}}, 'defaultAgentModelId': model,
                         'agentModelSorts': [{'displayName': 'Synthetic', 'groups': [{'displayName': 'Synthetic',
                                                                                   'modelIds': [model]}]}]}
            elif rpc == '/oauth2/v2/userinfo':
                value = {'email': 'synthetic@example.invalid', 'verified_email': True, 'id': 'synthetic-account'}
            else:
                value = {}
            flow.response = http.Response.make(200, json.dumps(value), {'Content-Type': 'application/json'})

    addons = [SyntheticService()]


@contextmanager
def client(binary, directory, env, conversation=None):
    from agy_client import native_preferences
    command = [binary, '--settings', json.dumps(native_preferences({'working_directory': '/workspace'})),
               '--input-format', 'stream-json', '--output-format', 'stream-json', '--print-timeout', '90s']
    if conversation:
        command += ['--conversation', conversation]
    with (directory / 'client.log').open('a') as log:
        proc = subprocess.Popen(command, cwd='/workspace', env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            yield proc
        finally:
            proc.stdin.close()
            proc.wait()
            proc.stdout.close()


def turn(proc, prompt, directory, *, success=True):
    proc.stdin.write(json.dumps({'event': 'user', 'message': {'content': prompt}}) + '\n')
    proc.stdin.flush()
    for line in proc.stdout:
        event = json.loads(line)
        if event.get('event') == 'result':
            result = event['result']
            if success:
                assert result['status'] == 'SUCCESS' and result['response'].strip() == REPLY, result
            else:
                assert result['status'] == 'ERROR' and 'Synthetic rejected invocation' in result['error'], result
            return result['conversation_id']
    raise RuntimeError(f'Antigravity exited {proc.wait()} before completing {prompt!r}: '
                       + (directory / 'client.log').read_text()[-4000:])


def set_network(mode):
    temporary = SNAPSHOT.with_suffix('.tmp')
    temporary.write_text(json.dumps({'revision': mode, 'network': mode, 'values': {}}))
    temporary.replace(SNAPSHOT)


def exercise(binary, directory, env):
    from agy_client import configure_search
    home = Path(env['HOME']) / '.gemini'
    configure_search(home)
    config = home / 'config/hooks.json'
    settings = json.loads(config.read_text())
    observer = shlex.join(['exec', '/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--observe'])
    settings['fixture-observer'] = {event: [{'command': observer}] for event in ('PreInvocation', 'PostInvocation')}
    config.write_text(json.dumps(settings))
    set_network('allowlist')
    with client(binary, directory, env) as proc:
        conversation = turn(proc, 'First update', directory)
        turn(proc, 'Same settings', directory)
        set_network('unrestricted')
        turn(proc, 'Mode changed', directory)
        turn(proc, 'Still unrestricted', directory)
        set_network('allowlist')
        (directory / 'reject-next').touch()
        turn(proc, 'Rejected update', directory, success=False)
    with client(binary, directory, env, conversation) as proc:
        assert turn(proc, 'Resumed update', directory) == conversation
        turn(proc, 'Same resumed settings', directory)

    hooks = read_events(directory / 'hooks.jsonl')
    all_wire = read_events(directory / 'wire.jsonl')
    assert len(all_wire) == 7, all_wire
    for prompt in ('First update', 'Same settings', 'Mode changed', 'Still unrestricted',
                   'Rejected update', 'Resumed update', 'Same resumed settings'):
        events = [item for item in hooks if item['lastUserInput'] == prompt]
        stages = [item['hookEventName'] for item in events]
        expected = ['PreInvocation'] if prompt == 'Rejected update' else ['PreInvocation', 'PostInvocation']
        assert stages == expected, (prompt, stages)
        assert {item['conversationId'] for item in events} == {conversation}, events
        assert len({(item['initialNumSteps'], item['invocationNum']) for item in events}) == 1, events

    modes = [['Restricted' if 'Network: Restricted.' in text else
              'Unrestricted' if 'Network: Unrestricted.' in text else 'unknown' for text in updates]
             for updates in all_wire]
    assert modes[0] == ['Restricted'], modes
    assert modes[1] == modes[0], modes
    assert modes[2] == modes[1] + ['Unrestricted'], modes
    assert modes[3] == modes[2], modes
    assert modes[4] == modes[3] + ['Restricted'], modes
    assert modes[5][-1:] == ['Restricted'] and modes[5].count('Restricted') > modes[4].count('Restricted'), modes
    assert modes[6] == modes[5], modes
    print(json.dumps({'native_model_requests': len(modes), 'network_updates': modes,
                      'successful_hook_pairs': 6, 'failed_invocation': 'no completion',
                      'resumed_conversation': 'verified'}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', default='/usr/local/bin/factory-agent')
    args = parser.parse_args()
    if SNAPSHOT.exists() or not SNAPSHOT.parent.is_dir():
        parser.error(f'Provide an empty writable container runtime directory at {SNAPSHOT.parent}.')
    sys.path.insert(0, str(SOURCE))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from agy_client import KEYRING
    from test_agy_auth import profile

    with tempfile.TemporaryDirectory(prefix='factory-agy-updates-') as tmp:
        directory = Path(tmp)
        home = directory / 'home'
        home.mkdir()
        env = {**os.environ, 'HOME': str(home), 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(directory / 'bus'),
               'AGY_CLI_DISABLE_AUTO_UPDATE': 'true', 'FACTORY_AGY_UPDATE_OBSERVER': str(directory)}
        subprocess.run(['dbus-daemon', '--session', '--address=' + env['DBUS_SESSION_BUS_ADDRESS'],
                        '--fork', '--nopidfile'], env=env, check=True)
        subprocess.run(['gnome-keyring-daemon', '--unlock', '--components=secrets'], env=env, input=b'\n',
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(['secret-tool', 'store', '--label=Synthetic Antigravity', *KEYRING], env=env,
                       input=json.dumps(profile()).encode(), check=True)
        with socket.socket() as port_probe:
            port_probe.bind(('127.0.0.1', 0))
            port = port_probe.getsockname()[1]
        command = [str(Path(sys.executable).with_name('mitmdump')), '--listen-host', '127.0.0.1',
                   '--listen-port', str(port), '--set', 'confdir=' + str(directory / 'cert'),
                   '--set', 'connection_strategy=lazy', '--set', 'upstream_cert=false',
                   '-s', str(Path(__file__).resolve())]
        with (directory / 'proxy.log').open('w') as log:
            proxy = subprocess.Popen(command, env={**env, 'FACTORY_AGY_UPDATE_ENDPOINT': str(directory)},
                                     stdout=log, stderr=subprocess.STDOUT)
            try:
                certificate = directory / 'cert/mitmproxy-ca-cert.pem'
                while not certificate.exists():
                    if proxy.poll() is not None:
                        raise RuntimeError('Synthetic proxy exited: ' + (directory / 'proxy.log').read_text())
                    time.sleep(.05)
                address = f'http://127.0.0.1:{port}'
                env.update(HTTPS_PROXY=address, HTTP_PROXY=address, https_proxy=address, http_proxy=address,
                           AGY_CA_CERT=str(certificate), SSL_CERT_FILE=str(certificate))
                exercise(args.binary, directory, env)
            finally:
                SNAPSHOT.unlink(missing_ok=True)
                proxy.terminate()
                proxy.wait()


if __name__ == '__main__':
    if sys.argv[1:] == ['--observe']:
        observe(Path(os.environ['FACTORY_AGY_UPDATE_OBSERVER']))
    else:
        main()
