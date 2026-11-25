"""Check Restricted and Unrestricted workspace networking with cached rootless Podman images."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import operation_locks, primary_agents, sandbox


def provider_route(root, agent, url, image, proxy_image):
    """A successful CONNECT is separate from whether an unauthenticated vendor request succeeds."""
    name = 'factory-provider-network-' + uuid4().hex[:12]
    policy = root / (agent + '.conf')
    policy.write_text(sandbox.squid_config(primary_agents.network_rules(agent)))
    subprocess.run(['podman', 'run', '--pull=never', '--http-proxy=false', '-d', '--name', name,
                    '--security-opt=label=disable', '--tmpfs', '/run/egress:rw', '-v',
                    f'{policy}:/etc/squid/sandbox.conf:ro', proxy_image], check=True)
    try:
        for _ in range(100):
            ready = subprocess.run(['podman', 'exec', name, 'test', '-S', '/run/egress/proxy.sock'],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f'{name} did not start its gateway listener')
        for target, wanted in ((url, '200'), ('https://unselected.invalid/', '403')):
            result = subprocess.run(['podman', 'run', '--rm', '--pull=never', '--http-proxy=false',
                                     '--network=container:' + name, '--entrypoint', 'curl', image,
                                     '--noproxy', '', '--proxy', 'http://127.0.0.1:3128', '--max-time', '30',
                                     '--silent', '--show-error', '--output', '/dev/null',
                                     '--write-out', '%{http_connect} %{http_code}', target],
                                    capture_output=True, text=True)
            assert result.stdout.split()[0] == wanted, (agent, target, result)
            if wanted == '200':
                assert result.returncode == 0 and result.stdout.split()[1] != '000', result
            print(f'PASS {agent} {target}: CONNECT and origin status {result.stdout}', flush=True)
    finally:
        subprocess.run(['podman', 'rm', '-f', name], check=True)


def exercise(root, mode, image, proxy_image):
    name = 'network-runtime-' + uuid4().hex[:12]
    state = root / 'sandboxes' / name
    state.mkdir(parents=True)
    project = root / name
    project.mkdir()
    config = {'engine': 'podman', 'name': name, 'workspace': str(project), 'network': mode,
              'uid': os.getuid(), 'gid': os.getgid(), 'options': {'browser': False},
              'profile': sandbox.load_profile('base'), 'common': {'path': str(sandbox.ROOT / 'containers/dev')}}
    rules = 'example.com:443\n' if mode == 'allowlist' else ''
    (state / 'allowlist.txt').write_text(rules)
    (state / 'squid.conf').write_text(sandbox.network_policy(config, sandbox.parse_rules(rules)))
    sandbox.write_json(state / 'config.json', config)
    workspace, gateway, *_ = sandbox.names(config)
    sentinel = 'http://127.0.0.1:1'
    proxy_keys = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy')

    def inside(*argv, check=True):
        return subprocess.run(['podman', 'exec', workspace, *argv], text=True, capture_output=True, check=check)

    def request(url, *extra, check=True):
        return inside('curl', '--silent', '--show-error', '--max-time', '30', '--fail',
                      '--output', '/dev/null', '--write-out', '%{http_code}', *extra, url, check=check)

    with operation_locks.sandbox(name, 'test networking in'):
        try:
            with (patch.object(sandbox, 'image_names', return_value=(image, proxy_image)),
                  patch.dict(os.environ, {key: sentinel for key in proxy_keys})):
                sandbox.up(config, state)
            info = sandbox.inspect(config, workspace)
            env = dict(row.split('=', 1) for row in info['Config']['Env'] if '=' in row)
            assert sentinel not in env.values(), 'The workspace inherited the test host proxy'
            assert env['NO_PROXY'] == env['no_proxy'] == 'localhost,127.0.0.1,::1', mode
            mounted = json.loads(inside('cat', '/opt/pi-security/config/sandbox-state.json').stdout)
            assert mounted['network'] == mode, mounted['network']
            assert request('https://example.com/').stdout == '200'
            if mode == 'allowlist':
                assert sandbox.inspect(config, gateway) and env['HTTP_PROXY'] == 'http://127.0.0.1:3128'
                denied = request('https://www.iana.org/', check=False)
                assert denied.returncode and '403' in denied.stderr, denied
                assert request('https://example.com/', '--noproxy', '*', check=False).returncode
                for family, address in [('AF_INET', '1.1.1.1'), ('AF_INET6', '2606:4700:4700::1111')]:
                    probe = ('import socket; s=socket.socket(socket.' + family + '); s.settimeout(3); '
                             f's.connect(({address!r},443))')
                    failed = inside('python3', '-c', probe, check=False)
                    assert failed.returncode and 'Network is unreachable' in failed.stderr, failed
                previous = mounted['revision']
                sandbox.update_network(config, state, 'www.iana.org:443\n')
                assert sandbox.inspect(config, workspace)['Id'] == info['Id']
                denied = request('https://example.com/', check=False)
                assert denied.returncode and '403' in denied.stderr, denied
                assert request('https://www.iana.org/').stdout == '200'
                latest = json.loads(inside('cat', '/opt/pi-security/config/sandbox-state.json').stdout)
                assert latest['revision'] != previous
                assert latest['values']['Additional allowed destinations'] == 'www.iana.org:443'
            else:
                assert sandbox.inspect(config, gateway) is None
                assert all(not env.get(key) for key in proxy_keys), 'Unexpected workspace proxy'
                assert request('https://example.com/', '--noproxy', '*', '--ipv4').stdout == '200'
                assert request('https://www.iana.org/').stdout == '200'
            # Source the current image input as BASH_ENV, just as the native worker image does.
            helper = sandbox.ROOT / 'containers/primary-worker/shell-network.sh'
            subprocess.run(['podman', 'cp', str(helper), workspace + ':/tmp/factory-shell-network.sh'], check=True)
            transports = [f'{key}=http://127.0.0.1:3130' for key in proxy_keys[:4]]
            # The native parent's sign-in transport must leave project development servers on workspace loopback.
            local_http = '''
import http.server, socket, subprocess, threading
class Server(http.server.HTTPServer):
    pass
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'workspace-local')
for family, address, host in [(socket.AF_INET, '127.0.0.1', 'localhost'), (socket.AF_INET6, '::1', '[::1]')]:
    Server.address_family = family
    with Server((address, 0), Handler) as server:
        threading.Thread(target=server.handle_request, daemon=True).start()
        result = subprocess.run(['curl', '-fsS', '--max-time', '5', f'http://{host}:{server.server_port}/'],
                                capture_output=True, text=True, check=True)
        assert result.stdout == 'workspace-local', result
'''
            inside('env', *transports, 'python3', '-c', local_http)
            print_proxies = 'import json,os; print(json.dumps({k:v for k,v in os.environ.items() if k.lower().endswith("_proxy")}))'
            output = inside('env', *transports, 'BASH_ENV=/tmp/factory-shell-network.sh', 'bash', '-c',
                            'exec python3 -c "$1"', 'shell', print_proxies).stdout
            child = json.loads(output)
            for key in proxy_keys[:4]:
                assert child.get(key) == ('http://127.0.0.1:3128' if mode == 'allowlist' else None), key
            assert child['no_proxy'] == env['no_proxy'] and child['NO_PROXY'] == env['NO_PROXY']
            print(f'PASS {mode}: workspace HTTP, proxy environment, shell transport and mounted revision', flush=True)
        except Exception:
            for container in (workspace, gateway):
                if sandbox.inspect(config, container):
                    subprocess.run(['podman', 'logs', '--tail', '20', container])
            raise
        finally:
            sandbox.delete(config, state)
        assert sandbox.inspect(config, workspace) is None and sandbox.inspect(config, gateway) is None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Cached Pi workspace image with its usual entrypoint')
    parser.add_argument('--proxy-image', default='localhost/pi-sandbox-factory-proxy:1')
    parser.add_argument('--providers-only', action='store_true', help='Check the three native transport gateways')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-workspace-network-') as directory:
        if args.providers_only:
            for agent, url in [('codex', 'https://api.openai.com/v1/models'),
                               ('claude', 'https://api.anthropic.com/v1/models'),
                               ('agy', 'https://accounts.google.com/.well-known/openid-configuration')]:
                provider_route(Path(directory), agent, url, args.image, args.proxy_image)
        else:
            for mode in ('allowlist', 'unrestricted'):
                exercise(Path(directory), mode, args.image, args.proxy_image)
