"""Profile application runtime checks using cached workspace/proxy images (Linux/WSL2)."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from ai_sandbox_factory import profile_services as services, sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Cached workspace image with Python, curl and SSH client')
    parser.add_argument('--proxy-image', default='localhost/pi-sandbox-factory-proxy:1')
    args = parser.parse_args()
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix='factory-profile-runtime-') as tmp:
        state = Path(tmp)
        config = {'name':'profile-runtime-' + uuid4().hex[:8], 'engine':'podman', 'network':'allowlist',
                  'profile':sandbox.load_profile('developer-postgres')}
        (state / 'allowlist.txt').write_text('example.com:443\n')
        workspace = sandbox.names(config)[0]
        def run(*cmd, check=True):
            return sandbox.run(['podman', *cmd], capture=True, check=check, stderr=subprocess.PIPE)
        def ssh(command):
            return run('exec', workspace, 'ssh', '-F', '/run/profile-services/config',
                       'database.profile.internal', command).stdout.strip()
        try:
            services.build(config)
            with patch.object(sandbox, 'image_names', return_value=(args.image,args.proxy_image)):
                services.up(config, state)
            run('run', '-d', '--name', workspace, '--label', sandbox.LABEL + '=' + config['name'],
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--security-opt=label=disable',
                '--userns=keep-id:uid=1000,gid=1000', '--user=1000:1000',
                '--sysctl', 'net.ipv4.ping_group_range=1000 1000',
                *services.network_args(config), *services.mounts(config,state), '--entrypoint', 'sleep', args.image, 'infinity')
            services.ready(config,state)
            assert ssh('psql -U app -d app -Atc "SELECT 42"') == '42'
            guide = (Path(config['profile']['path']) / 'start-here.md').read_text().split('```sh\n',1)[1].split('```',1)[0].strip()
            assert 'PostgreSQL' in run('exec', workspace, 'bash', '-lc', guide).stdout
            ssh('psql -U app -d app -c "CREATE TABLE preserved (id int); INSERT INTO preserved VALUES (42)"')
            original = sandbox.inspect(config, services.container(config,'database'))['Id']
            assert run('exec',workspace,'ping','-c','1','database.profile.internal').returncode == 0
            for address, family in (('1.1.1.1','AF_INET'),('2606:4700:4700::1111','AF_INET6')):
                source = f'import socket; s=socket.socket(socket.{family}); s.settimeout(2); s.connect(({address!r},443))'
                assert run('exec',workspace,'python3','-c',source,check=False).returncode
            proxy = 'http://proxy.profile.internal:3128'
            assert run('exec',workspace,'curl','--proxy',proxy,'-fsS','https://example.com').returncode == 0
            services.update_policy(config,state,'www.iana.org:443\n',{})
            denied = run('exec',workspace,'curl','--proxy',proxy,'-sS','https://example.com',check=False)
            assert denied.returncode and '403' in denied.stderr, denied
            services.update_policy(config,state,'example.com:443\n',{})
            assert run('exec',workspace,'curl','--proxy',proxy,'-fsS','https://example.com').returncode == 0
            run('stop',services.prefix(config)+'-proxy')
            assert ssh('psql -U app -d app -Atc "SELECT id FROM preserved"') == '42'
            services.down(config)
            with patch.object(sandbox,'image_names',return_value=(args.image,args.proxy_image)):
                services.up(config,state)
            services.ready(config,state)
            assert sandbox.inspect(config, services.container(config,'database'))['Id'] == original
            assert ssh('psql -U app -d app -Atc "SELECT id FROM preserved"') == '42'
            print('PASS profile runtime: direct SSH/SQL, ping, blocked external IPv4/IPv6, live proxy revocation, relay outage, stable keys/data and Stop/Start',flush=True)
        finally:
            run('rm','-f',workspace,check=False)
            services.delete(config)
            for kind, values in services.resources(config).items():
                for value in values:
                    assert run(kind,'exists',value,check=False).returncode, value
            print('PASS disposable profile containers, volumes and private network removed',flush=True)

if __name__ == '__main__':
    main()
