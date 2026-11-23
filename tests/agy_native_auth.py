"""Exercise pinned native Antigravity token refresh against a synthetic TLS server.

Run inside the credential image with its pinned mitmproxy runtime. All tokens
are fixtures; the native process receives only the profile's placeholders.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import pty
import select

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).parent))
from agy_auth import AuthStore, KEYRING
from test_agy_auth import profile


def fixture_id_token():
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'RS256', 'typ': 'JWT'}) + '.' + encode({'iss': 'https://accounts.google.com',
        'sub': 'synthetic-account', 'email': 'synthetic@example.invalid', 'exp': 4070908800,
        'aud': 'synthetic-native-client'}) + '.private-google-id-signature'


def store(path):
    return AuthStore(path.parent, read=lambda: json.loads(path.read_text()),
                     write=lambda value: path.write_text(json.dumps(value)))


if os.environ.get('FACTORY_AGY_NATIVE_FIXTURE'):
    from mitmproxy import http
    from agy_auth_proxy import AntigravityProxy

    directory = Path(os.environ['FACTORY_AGY_NATIVE_FIXTURE'])
    private = store(directory / 'profile.json')

    class Upstream:
        def request(self, flow):
            request = flow.request
            observation = {'host': request.host, 'path': request.path.split('?', 1)[0],
                'real_access': request.headers.get('authorization') == 'Bearer private-google-access',
                'rotated_access': request.headers.get('authorization') == 'Bearer rotated-private-access',
                'real_refresh': b'private-google-refresh' in request.content,
                'authorization_code_grant': b'grant_type=authorization_code' in request.content}
            with (directory / 'requests.jsonl').open('a') as stream:
                stream.write(json.dumps(observation) + '\n')
            if request.host == 'oauth2.googleapis.com' and request.path == '/token':
                flow.response = http.Response.make(200, json.dumps({'access_token': 'rotated-private-access',
                    'refresh_token': 'rotated-private-refresh', 'id_token': fixture_id_token(),
                    'token_type': 'Bearer', 'expires_in': 3600}),
                    {'Content-Type': 'application/json'})
            elif request.host == 'www.googleapis.com' and request.path.startswith('/oauth2/v2/userinfo'):
                flow.response = http.Response.make(200, json.dumps({'email': 'synthetic@example.invalid', 'verified_email': True, 'id': 'synthetic-account'}),
                                                   {'Content-Type': 'application/json'})
            else:
                flow.response = http.Response.make(418, json.dumps({'error': {'code': 418,
                    'message': 'synthetic upstream diagnostic rotated-private-access'}}),
                    {'Content-Type': 'application/json', 'x-request-id': 'synthetic-native-request'})

    addons = [AntigravityProxy(private), Upstream()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', default='/usr/local/bin/factory-agent')
    parser.add_argument('--fresh', action='store_true', help='Drive the native terminal authorization-code flow instead of refresh.')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-agy-native-') as temporary:
        directory = Path(temporary)
        env = {**os.environ, 'HOME': str(directory / 'home'),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(directory / 'bus'),
               'AGY_CLI_DISABLE_AUTO_UPDATE': 'true'}
        Path(env['HOME']).mkdir()
        subprocess.run(['dbus-daemon', '--session', '--address=' + env['DBUS_SESSION_BUS_ADDRESS'],
                        '--fork', '--nopidfile'], env=env, check=True)
        subprocess.run(['gnome-keyring-daemon', '--unlock', '--components=secrets'], env=env,
                       input=b'\n', stdout=subprocess.DEVNULL, check=True)
        value = profile()
        value['token']['expiry'] = '2000-01-01T00:00:00Z'
        (directory / 'profile.json').write_text(json.dumps(None if args.fresh else value))
        if not args.fresh:
            native_profile = store(directory / 'profile.json').public()['auth']
            subprocess.run(['secret-tool', 'store', '--label=Synthetic native AGy', *KEYRING],
                           input=json.dumps(native_profile).encode(), env=env, check=True)
        env['FACTORY_AGY_NATIVE_FIXTURE'] = str(directory)
        command = [str(Path(sys.executable).with_name('mitmdump')), '--listen-host', '127.0.0.1',
            '--listen-port', '3130', '--set', 'confdir=' + str(directory / 'mitm'),
            '--set', 'connection_strategy=lazy', '--set', 'upstream_cert=false', '-s', str(Path(__file__).resolve())]
        with (directory / 'proxy.log').open('w') as log:
            proxy = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                certificate = directory / 'mitm/mitmproxy-ca-cert.pem'
                while not certificate.exists():
                    if proxy.poll() is not None:
                        raise RuntimeError('Synthetic TLS proxy exited: ' + (directory / 'proxy.log').read_text())
                    time.sleep(.05)
                env.update(HTTP_PROXY='http://127.0.0.1:3130', HTTPS_PROXY='http://127.0.0.1:3130',
                           SSL_CERT_FILE=str(certificate), AGY_CA_CERT=str(certificate))
                native = [args.binary, '--print', 'Say synthetic hello.', '--output-format', 'json']
                if args.fresh:
                    env['SSH_CONNECTION'] = '127.0.0.1 0 127.0.0.1 0'
                    master, slave = pty.openpty()
                    child = subprocess.Popen(native, env=env, cwd=directory, stdin=slave, stdout=slave, stderr=slave)
                    os.close(slave)
                    output, submitted = '', False
                    try:
                        while child.poll() is None:
                            if not select.select([master], [], [], .1)[0]:
                                continue
                            try:
                                output += os.read(master, 65536).decode(errors='replace')
                            except OSError:
                                break
                            if not submitted and 'paste the authorization code here and press Enter' in output:
                                os.write(master, b'4/fixture-code\n')
                                submitted = True
                        child.wait()
                    finally:
                        os.close(master)
                        if child.poll() is None:
                            child.terminate()
                            child.wait()
                    assert submitted, 'Native fresh sign-in did not display its authorization-code prompt'
                    result = subprocess.CompletedProcess(native, child.returncode, output, '')
                else:
                    result = subprocess.run(native, env=env, cwd=directory, capture_output=True, text=True)
                output = result.stdout + result.stderr
                lookup = subprocess.run(['secret-tool', 'lookup', *KEYRING], env=env, capture_output=True)
                saved = lookup.stdout.decode()
                requests = [json.loads(line) for line in (directory / 'requests.jsonl').read_text().splitlines()]
                if lookup.returncode and not (args.fresh and lookup.returncode == 1 and not saved):
                    detail = output
                    for secret in ('private-google-access', 'private-google-refresh', 'rotated-private-access', 'rotated-private-refresh', 'private-google-id-signature'):
                        detail = detail.replace(secret, '[redacted]')
                    raise AssertionError('Native profile was not saved: ' + detail + '\nRequests: ' + json.dumps(requests))
                assert any(item['authorization_code_grant' if args.fresh else 'real_refresh'] for item in requests), requests
                assert any(item['rotated_access'] for item in requests), requests
                assert json.loads((directory / 'profile.json').read_text())['token']['access_token'] == 'rotated-private-access'
                assert 'synthetic upstream diagnostic' in output, output
                assert '418' in output, output
                for secret in ('private-google-access', 'private-google-refresh', 'rotated-private-access', 'rotated-private-refresh', 'private-google-id-signature'):
                    assert secret not in saved and secret not in output, 'Native client exposed a fixture credential'
                print(json.dumps({'native_fresh_oauth_exchange' if args.fresh else 'native_refresh': 'passed', 'private_upstream_auth': 'passed',
                                  'native_keyring_and_output_redaction': 'passed',
                                  'native_status_and_diagnostic': 'HTTP 418 preserved',
                                  'subscription_entitlement': 'deliberately rejected by synthetic fixture'}))
            finally:
                proxy.terminate()
                proxy.wait()


if __name__ == '__main__':
    main()
