"""Create the documented two-host profile and verify its commands and bundled guide."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import profile_edit, profile_runtime, profile_services, registry, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, copy_draft, preset

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
            configurations = {}
            try:
                service.create(draft, None, print, cancellation=Creation())
                config = service.config(name)
                configurations[name] = config
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
                editable = root / 'editable-profile'
                service.run(['profile-fork',name,'--directory',str(editable)], print)
                service.run(['down',name], print)
                moved = editable.with_name('moved-editable-profile')
                editable.rename(moved)
                try:
                    missing = service.capture('up',name)
                    assert missing.returncode and str(editable) in missing.stderr and 'Restore it' in missing.stderr, missing.stderr
                    assert not sandbox.inspect(service.config(name),sandbox.names(config)[0])['State']['Running']
                finally:
                    moved.rename(editable)
                service.run(['up',name], print)
                print('PASS missing editable folder: useful saved-path error, stopped runtime and successful retry after restoring the folder',flush=True)
                # Make an actual package edit through the agent's writable mount.
                dev = sandbox.names(service.config(name))[0]
                inside('python3','-c',
                       'from pathlib import Path; p=Path('+repr(profile_edit.PATH)+'); '
                       'f=p/"Dockerfile"; f.write_text(f.read_text()+"\\nUSER root\\nRUN apt-get update && apt-get install -y --no-install-recommends figlet && rm -rf /var/lib/apt/lists/*\\nUSER node\\n")')
                review = service.review_profile(name)
                service.run(['profile-apply',name,'--revision',review['revision']], print)
                config = service.config(name)
                configurations[name] = config
                dev = sandbox.names(config)[0]
                assert inside('sh','-c','command -v figlet').returncode == 0
                assert sql('database','app','app','SELECT value FROM authoring_marker') == '42'
                assert (state/'profile-services/client/connections.json').read_bytes() == before
                for host, identifier in ids.items():
                    assert sandbox.inspect(config,profile_services.container(config,host))['Id'] == identifier
                def git(*args):
                    return subprocess.run(['git','-c','commit.gpgsign=false','-C',str(editable),*args],
                                          check=True,capture_output=True,text=True).stdout.strip()
                git('init','-b','main')
                git('config','user.name','Example Profile Maintainer')
                git('config','user.email','maintainer@example.test')
                git('add','.')
                git('commit','-m','Track the customized two-host profile')
                first_commit = git('rev-parse','HEAD')
                installed = registry.add('profile','team-custom',str(editable))
                assert installed['commit'] == first_commit
                fresh = preset('team-custom', copy.deepcopy(DEFAULT_SETTINGS))
                fresh.update({key:copy.deepcopy(draft[key]) for key in (
                    'workspace','engine','network','model_providers','local_model_ports','allowlist','open_editor','options')})
                fresh['name'] = name + '-git'
                service.create(fresh, None, print, cancellation=Creation())
                fresh_config = service.config(fresh['name'])
                configurations[fresh['name']] = fresh_config
                fresh_dev = sandbox.names(fresh_config)[0]
                assert sandbox.run(['podman','exec',fresh_dev,'sh','-c','command -v figlet'],capture=True).returncode == 0
                assert not fresh_config.get('editable_profile')
                assert fresh_config['profile']['digest'] == config['profile']['digest']
                fresh_connections = json.loads((service.state/fresh['name']/'profile-services/client/connections.json').read_text())
                assert fresh_connections['database']['password'] != settings['database']['password']
                print('PASS edited Git profile: register a real commit and create a fresh sandbox with the added package and both services',flush=True)
                # Neither a Library update nor broken, unapplied source may leak into a copy.
                marker = 'NEW_LIBRARY_REVISION_NOT_APPLIED'
                with (editable/'profile.md').open('a') as target:
                    target.write('\n'+marker+'\n')
                git('add','.')
                git('commit','-m','Change profile instructions')
                updated = registry.update('profile','team-custom')
                assert updated['commit'] != first_commit
                (editable/'profile.json').write_text('{invalid unapplied json\n')
                duplicate = copy_draft({'name':name,'config':config},service.state)
                # Review sandbox converts saved provider records to form selections.
                duplicate['model_providers'] = {row['name']:row['settings'] for row in duplicate['model_providers']}
                duplicate.update(name=name+'-copy',open_editor=False)
                service.create(duplicate, None, print, cancellation=Creation())
                copied_config = service.config(duplicate['name'])
                configurations[duplicate['name']] = copied_config
                assert copied_config['profile']['digest'] == config['profile']['digest']
                assert not copied_config.get('editable_profile')
                assert copied_config['profile']['customized_from'] == config['profile']['customized_from']
                copied_dev = sandbox.names(copied_config)[0]
                def copied_inside(*args):
                    return sandbox.run(['podman','exec',copied_dev,*args],capture=True,stderr=subprocess.PIPE).stdout
                assert marker not in copied_inside('cat',profile_runtime.PATH+'/profile/profile.md')
                assert marker not in copied_inside('cat','/home/node/.pi/agent/AGENTS.md')
                assert copied_inside('sh','-c','command -v figlet').strip()
                assert copied_inside('ssh','-F','/run/profile-services/config','database.profile.internal',
                    "psql -U app -d app -Atc \"SELECT count(*) FROM pg_tables WHERE tablename='authoring_marker'\"").strip() == '0'
                copied_connections = json.loads((service.state/duplicate['name']/'profile-services/client/connections.json').read_text())
                for host in ('database','reporting'):
                    assert len({settings[host]['password'],fresh_connections[host]['password'],copied_connections[host]['password']}) == 3
                    assert len({profile_services.data_volume(c,host,'data') for c in configurations.values()}) == 3
                assert sql('database','app','app','SELECT value FROM authoring_marker') == '42'
                assert (editable/'profile.json').read_text() == '{invalid unapplied json\n'
                rebuilding = []
                def rebuild_log(line):
                    rebuilding.append(line)
                    print(line,flush=True)
                service.run(['recreate',duplicate['name']], rebuild_log)
                assert any('STEP 1/' in line for line in rebuilding), 'Customized-copy Rebuild skipped image preparation'
                copied_config = service.config(duplicate['name'])
                configurations[duplicate['name']] = copied_config
                copied_dev = sandbox.names(copied_config)[0]
                assert copied_config['profile']['digest'] == config['profile']['digest']
                assert copied_inside('sh','-c','command -v figlet').strip()
                assert marker not in copied_inside('cat',profile_runtime.PATH+'/profile/profile.md')
                print('PASS Create from existing: frozen applied profile despite newer Library and invalid editable source; fresh data/passwords/volumes and unchanged original database/source',flush=True)
            finally:
                for sandbox_name in (name+'-copy',name+'-git',name):
                    if (service.state/sandbox_name/'config.json').exists():
                        configurations[sandbox_name] = service.config(sandbox_name)
                        service.run(['delete',sandbox_name], print)
                for config in configurations.values():
                    for kind, names in profile_services.resources(config).items():
                        for resource in names:
                            assert subprocess.run(['podman',kind,'exists',resource],capture_output=True).returncode, resource
                assert (workspace/'AGENTS.md').read_text() == 'USER_PROJECT_INSTRUCTIONS\n'
                print('PASS disposable multi-host containers, volumes and network removed',flush=True)


if __name__ == '__main__':
    main()
