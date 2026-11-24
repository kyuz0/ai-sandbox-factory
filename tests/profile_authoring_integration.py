"""Create the documented two-host profile and verify its commands and bundled guide."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import profile_runtime, profile_services, registry, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset

EXAMPLE = sandbox.ROOT / 'containers/dev/profile-authoring/example'


def main():
    with tempfile.TemporaryDirectory(prefix='factory-authoring-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        (workspace / 'AGENTS.md').write_text('USER_PROJECT_INSTRUCTIONS\n')
        service = Service(root / 'state')
        name = 'authoring-' + uuid4().hex[:8]
        with patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(root/'library')):
            registry.add('profile', 'two-hosts', str(EXAMPLE))
            draft = preset('two-hosts', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name=name, workspace=str(workspace), engine='podman', network='allowlist',
                         model_providers={}, local_model_ports=None, allowlist='', open_editor=False)
            draft['options']['browser'] = False
            config = None
            try:
                service.create(draft, None, print, cancellation=Creation())
                config = service.config(name)
                dev = sandbox.names(config)[0]
                state = service.state / name
                def inside(*args, check=True):
                    return sandbox.run(['podman','exec',dev,*args], capture=True, check=check, stderr=subprocess.PIPE)
                guide = inside('cat', profile_runtime.AUTHORING).stdout
                assert guide == (sandbox.ROOT/'containers/dev/profile-authoring/README.md').read_text()
                assert profile_runtime.AUTHORING in inside('cat','/home/node/.pi/agent/AGENTS.md').stdout
                assert inside('sh','-c','echo changed >> ' + profile_runtime.AUTHORING,check=False).returncode
                assert inside('cat','/workspace/AGENTS.md').stdout == 'USER_PROJECT_INSTRUCTIONS\n'
                copied = root / 'host-profile-copy'
                copied.mkdir()
                sandbox.run(['podman','cp',dev + ':' + profile_runtime.PATH + '/authoring/example/.',str(copied)])
                assert set(sandbox.load_profile(str(copied))['services']) == {'database','reporting'}
                # Run the exact user-guide commands, not reconstructed variants.
                commands = (EXAMPLE/'start-here.md').read_text().split('```sh\n',1)[1].split('```',1)[0].strip().splitlines()
                for command, database in zip(commands, ('app','reports'), strict=True):
                    assert database in inside('bash','-lc',command).stdout
                direct = (EXAMPLE/'start-here.md').read_text().split('```sh\n')[2].split('```',1)[0]
                assert 'app' in inside('bash','-lc',direct).stdout
                def sql(host, user, database, statement):
                    import shlex
                    return inside('ssh','-F','/run/profile-services/config',host+'.profile.internal',
                                  f'psql -U {user} -d {database} -Atc ' + shlex.quote(statement)).stdout.strip()
                sql('database','app','app',"CREATE TABLE authoring_marker (value int); INSERT INTO authoring_marker VALUES (42)")
                assert sql('reporting','reporter','reports',"SELECT count(*) FROM pg_tables WHERE tablename='authoring_marker'") == '0'
                before = (state/'profile-services/client/connections.json').read_bytes()
                settings = json.loads(before)
                assert settings['database']['password'] != settings['reporting']['password']
                ids = {host:sandbox.inspect(config,profile_services.container(config,host))['Id']
                       for host in ('database','reporting')}
                # A profile host can reach its peer directly, without the external proxy.
                password = settings['reporting']['password']
                peer = sandbox.run(['podman','exec','--env','PGPASSWORD='+password,
                    profile_services.container(config,'database'),'psql','-h','reporting.profile.internal',
                    '-U','reporter','-d','reports','-Atc','SELECT current_database()'],
                    capture=True, check=False, stderr=subprocess.PIPE)
                assert peer.returncode == 0, f'Profile peer SQL failed (exit {peer.returncode}): {peer.stderr}'
                assert peer.stdout.strip() == 'reports'
                service.run(['down',name], print)
                service.run(['up',name], print)
                assert sql('database','app','app','SELECT value FROM authoring_marker') == '42'
                assert (state/'profile-services/client/connections.json').read_bytes() == before
                for host, identifier in ids.items():
                    assert sandbox.inspect(config,profile_services.container(config,host))['Id'] == identifier
                print('PASS documented two-host example: read-only authoring guide, host copy, exact SSH/SQL commands, peer access, isolated data/passwords and persistent Stop/Start',flush=True)
            finally:
                if (service.state/name/'config.json').exists():
                    config = service.config(name)
                    service.run(['delete',name], print)
                if config:
                    for kind, names in profile_services.resources(config).items():
                        for resource in names:
                            assert subprocess.run(['podman',kind,'exists',resource],capture_output=True).returncode, resource
                assert (workspace/'AGENTS.md').read_text() == 'USER_PROJECT_INSTRUCTIONS\n'
                print('PASS disposable multi-host containers, volumes and network removed',flush=True)


if __name__ == '__main__':
    main()
