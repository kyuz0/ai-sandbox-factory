"""A failed transaction commit restores actual containers and application data."""
import copy
import argparse
import errno
import json
import shlex
import shutil
import subprocess
import tarfile
import threading
from urllib.parse import urlencode
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import browser_editor, editor_settings, operation_locks, profile_edit, profile_replacement, profile_services, sandbox
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset


def check_internal_http(config):
    """Use an existing service's environment to reach a later internal HTTP host."""
    environment = sandbox.inspect(config,profile_services.container(config,'database'))['Config']['Env']
    no_proxy = next(item.partition('=')[2] for item in environment if item.startswith('NO_PROXY='))
    server = profile_services.prefix(config) + '-http-test'
    client = server + '-client'
    image = sandbox.image_names(config)[1]
    labels = ['--label',f'{sandbox.LABEL}={config["name"]}']
    network = ['--network',profile_services.prefix(config),'--cap-drop=ALL']
    script = ('from http.server import HTTPServer,BaseHTTPRequestHandler\n'
              'class Handler(BaseHTTPRequestHandler):\n'
              ' def do_GET(self):\n'
              '  self.send_response(200); self.end_headers(); self.wfile.write(b"profile-peer")\n'
              'server=HTTPServer(("0.0.0.0",8080),Handler)\n'
              'print("PROFILE_HTTP_READY",flush=True)\nserver.serve_forever()\n')
    ready = threading.Event()
    output = []
    follower = None
    try:
        sandbox.run(['podman','run','-d','--name',server,*labels,*network,
                     '--network-alias','new-service.profile.internal','--entrypoint','python3',image,'-c',script],capture=True)
        follower = subprocess.Popen(['podman','logs','--follow',server],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        def follow():
            for line in follower.stdout:
                output.append(line)
                if 'PROFILE_HTTP_READY' in line: ready.set()
        reader = threading.Thread(target=follow,daemon=True)
        reader.start()
        assert ready.wait(60), 'HTTP peer did not start: '+''.join(output)
        check = ('from urllib.request import urlopen,getproxies_environment,proxy_bypass_environment; '
                 'assert urlopen("http://new-service.profile.internal:8080",timeout=30).read()==b"profile-peer"; '
                 'assert not proxy_bypass_environment("outside.example:443",getproxies_environment())')
        sandbox.run(['podman','run','--rm','--name',client,*labels,*network,
                     '-e','HTTP_PROXY=http://unreachable.invalid:1','-e','NO_PROXY='+no_proxy,
                     '--entrypoint','python3',image,'-c',check],capture=True)
        actual = sandbox.run(['podman','exec',sandbox.names(config)[0],'env',
                              'HTTP_PROXY=http://unreachable.invalid:1','http_proxy=http://unreachable.invalid:1',
                              'NO_PROXY='+no_proxy,'no_proxy='+no_proxy,'curl','--fail','--silent','--show-error',
                              'http://new-service.profile.internal:8080'],capture=True,stderr=subprocess.PIPE)
        assert actual.stdout == 'profile-peer', actual.stdout
        print('PASS internal HTTP: an unchanged service environment reaches a later profile host directly while external hosts still require the proxy',flush=True)
    finally:
        for container in (client,server):
            sandbox.run(['podman','rm','-f',container],capture=True,check=False,stderr=subprocess.PIPE)
        if follower:
            follower.wait(timeout=30)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-editor',type=Path,help='Pinned code-server archive for active-editor replacement checks')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-profile-commit-') as temporary:
        root = Path(temporary)
        workspace = root / 'project'
        workspace.mkdir()
        service = Service(root / 'state')
        name = 'profile-commit-' + uuid4().hex[:8]
        state = service.state / name
        draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
        draft.update(name=name, primary_agent='codex', primary_auth='login', workspace=str(workspace),
                     engine='podman', network='allowlist', open_editor=False, allowlist='', model_providers={},
                     local_model_ports=None)
        draft['options']['browser'] = False
        def log(line):
            print(line, flush=True)
        def config():
            return service.config(name)
        def runtime():
            current = config()
            return {key: sandbox.inspect(current, key) for key in
                    profile_replacement.runtime_names(current) | profile_services.resources(current)['container']
                    if sandbox.inspect(current, key)}
        def sql(statement):
            return sandbox.run(['podman','exec',profile_services.container(config(),'database'),
                                'psql','-U','app','-d','app','-Atc',statement],capture=True).stdout.strip()
        connection=None
        cookie=None
        def check_editor(stage):
            if not connection:
                return
            from browser_editor_lifecycle import request
            current=browser_editor.runtime(state)
            assert current and current['url']==connection['url'], (stage,current,connection)
            assert current['container']==sandbox.inspect(config(),sandbox.names(config())[0])['Id'],stage
            assert request(connection['url'],headers={'Cookie':cookie})[0]==200,stage
            assert request(connection['url'])[0]==302,stage
            assert current['preferences']==connection['preferences'],stage
            print('PASS active browser editor: same address, password and authenticated cookie after '+stage,flush=True)
        try:
            service.create(draft, None, log)
            if args.browser_editor:
                from browser_editor_lifecycle import request
                cache=state.parent/'.browser-editor-downloads'
                cache.mkdir(exist_ok=True)
                shutil.copyfile(args.browser_editor,cache/args.browser_editor.name)
                password='disposable-editor-fixture'
                editor_settings.save(state,{'password_hash':editor_settings.password_hash(password)})
                browser_editor.open_browser_editor(config(),state,archive=str(args.browser_editor),background=True,open_tab=False)
                connection=browser_editor.runtime(state)
                status,headers,_=request(connection['url'],'/login',method='POST',body=urlencode({'password':password}),
                                         headers={'Content-Type':'application/x-www-form-urlencoded'})
                assert status==302, (status,headers)
                cookie=headers['Set-Cookie'].split(';')[0]
                check_editor('initial launch')
            folder = root / 'editable-profile'
            service.run(['profile-fork',name,'--directory',str(folder)],log)
            check_editor('attaching an editable profile')
            check_internal_http(config())
            sql("CREATE TABLE commit_marker (value text); INSERT INTO commit_marker VALUES ('preserved')")
            before = runtime()
            original = config()
            password = json.loads((state/'profile-services/client/connections.json').read_text())['database']['password']
            manifest = json.loads((folder/'profile.json').read_text())
            manifest['services']['database']['environment']['EXAMPLE_VERSION'] = 'updated'
            (folder/'profile.json').write_text(json.dumps(manifest))
            review = service.review_profile(name)
            write_json = sandbox.write_json
            failures = []
            def publish(path, value):
                if Path(path) == state/'profile-preparation/record.json' and value.get('committed'):
                    failures.append(path)
                    raise OSError(errno.ENOSPC, 'No space left while committing profile settings')
                return write_json(path, value)
            with operation_locks.sandbox(name,'test profile commit failure for'), patch.object(sandbox,'write_json',side_effect=publish):
                try:
                    profile_edit.apply(original,state,review['revision'])
                except ValueError as error:
                    assert isinstance(error.__cause__,OSError) and error.__cause__.errno == errno.ENOSPC, error
                    for text in (name,'commit profile changes','were restored','Free storage space','retry the same profile review'):
                        assert text in str(error), str(error)
                else:
                    raise AssertionError('The injected commit failure was not reported')
            assert len(failures) == 1
            assert not (state/'profile-preparation').exists()
            assert config() == original
            check_editor('failed commit and rollback')
            after = runtime()
            assert after.keys() == before.keys()
            for key, previous in before.items():
                assert after[key]['Id'] == previous['Id'], key
                assert after[key]['State']['Running'] == previous['State']['Running'], key
            assert sql('SELECT value FROM commit_marker') == 'preserved'
            assert json.loads((state/'profile-services/client/connections.json').read_text())['database']['password'] == password
            print('PASS commit failure: previous container IDs, running state, settings, database and password restored before returning the error',flush=True)
            service.run(['profile-apply',name,'--revision',review['revision']],log)
            check_editor('applying a profile')
            assert sql('SELECT value FROM commit_marker') == 'preserved'
            result = sandbox.run(['podman','exec',profile_services.container(config(),'database'),
                                  'printenv','EXAMPLE_VERSION'],capture=True)
            assert result.stdout.strip() == 'updated'
            previous_volume = profile_services.data_volume(original,'database','data')
            subprocess.run(shlex.split(profile_services.export_command(previous_volume)),cwd=root,check=True)
            with tarfile.open(root/(previous_volume+'.tar')) as archive:
                member=next(item for item in archive.getmembers() if item.name.lstrip('./')=='PG_VERSION')
                assert archive.extractfile(member).read().strip()==b'17'
            print('PASS saved-data export: the displayed command exports the exact retained PostgreSQL volume to a readable archive',flush=True)
            print('PASS retry: the same reviewed service replacement succeeds and preserves database data',flush=True)
        finally:
            if args.browser_editor:
                browser_editor.stop(state)
            if (state/'config.json').exists():
                service.run(['delete',name],log)
                for command in (['ps','-aq'],['volume','ls','-q'],['network','ls','-q']):
                    remaining = sandbox.run(['podman',*command,'--filter',f'label={sandbox.LABEL}={name}'],capture=True)
                    assert not remaining.stdout.strip(),remaining.stdout
                print('PASS commit failure fixture: all labeled resources removed',flush=True)


if __name__ == '__main__':
    main()
