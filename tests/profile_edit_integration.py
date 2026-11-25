"""Disposable real creation, profile edits, service additions and rollback."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import operation_locks, profile_edit, profile_replacement, profile_runtime, profile_services, registry, sandbox, updating
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset
from creation_provider import ADAPTER, KEY


def exercise(agent, root):
    service = Service(root / 'state')
    workspace = root / (agent + '-project')
    workspace.mkdir()
    (workspace / 'AGENTS.md').write_text('PROJECT_INSTRUCTIONS_UNCHANGED\n')
    name = 'profile-edit-' + agent + '-' + uuid4().hex[:8]
    state = service.state / name
    destination = root / (agent + '-editable-profile')
    draft = preset('base' if agent == 'pi' else 'developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
    draft.update(name=name, primary_agent=agent, primary_auth='login', workspace=str(workspace), engine='podman',
                 network='allowlist', open_editor=False, allowlist='', model_providers={}, local_model_ports=None)
    draft['options']['browser'] = False
    if agent == 'pi':
        draft.update(model_providers={'fixture':{'model':'available'}}, _provider_keys={'fixture':KEY})
    output = []
    def log(line):
        assert KEY not in line
        output.append(line)
        print(line, flush=True)
    def config():
        return service.config(name)
    def inside(*args, check=True):
        return sandbox.run(['podman','exec',sandbox.names(config())[0],*args], capture=True,
                           check=check, stderr=subprocess.PIPE)
    def edit(script):
        return inside('python3','-c', 'from pathlib import Path; import json,shutil; p=Path(' + repr(profile_edit.PATH) + '); ' + script)
    def validate():
        return inside('python3',profile_runtime.PATH+'/authoring/validate.py',profile_edit.PATH,'--agent',agent)
    def apply(review):
        service.run(['profile-apply',name,'--revision',review['revision']], log)
    def ids():
        current = config()
        return {resource:sandbox.inspect(current,resource)['Id']
                for resource in profile_replacement.runtime_names(current) | profile_services.resources(current)['container']
                if sandbox.inspect(current,resource)}
    def sql_marker():
        return inside('ssh','-F','/run/profile-services/config','database.profile.internal',
                      'psql -U app -d app -Atc "SELECT value FROM profile_edit_marker"').stdout.strip()
    def assert_no_added(previous, candidate):
        for kind, names in profile_services.resources(candidate).items():
            for resource in names - profile_services.resources(previous)[kind]:
                result = sandbox.run(['podman',kind,'exists',resource], capture=True, check=False)
                assert result.returncode == 1, (kind,resource,result.returncode)
    try:
        service.create(draft, None, log, cancellation=Creation())
        original = config()
        original_password = None
        if 'database' in profile_services.selected(original):
            inside('ssh','-F','/run/profile-services/config','database.profile.internal',
                   "psql -U app -d app -c \"CREATE TABLE profile_edit_marker (value text); INSERT INTO profile_edit_marker VALUES ('preserved');\"")
            original_password=json.loads((state/'profile-services/client/connections.json').read_text())['database']['password']
        service.run(['profile-fork',name,'--directory',str(destination)], log)
        assert sandbox.tree_digest(destination) == original['profile']['digest']
        assert inside('cat','/workspace/AGENTS.md').stdout == 'PROJECT_INSTRUCTIONS_UNCHANGED\n'
        context_path = '/home/node/.pi/agent/AGENTS.md' if agent == 'pi' else '/run/primary-runtime/context.md'
        context = inside('cat',context_path).stdout
        for expected in (profile_edit.PATH,'Review changes','Editing the source does not change the running sandbox','validate.py'):
            assert expected in context, (agent,expected)
        assert inside('sh','-c','echo wrong >> '+profile_runtime.PATH+'/profile/profile.md',check=False).returncode
        assert not (destination/'.git').exists()
        if agent != 'pi':
            settings = json.loads((state/'primary-runtime/settings.json').read_text())
            assert profile_edit.PATH in settings['write_roots']
            inside('/usr/local/bin/'+agent,'--factory-prepare')
        inside('sh','-c','printf KEEP_PRIVATE_HOME > /home/node/profile-edit-marker')
        # Invalid edits give the same useful error to the agent and Factory.
        manifest = (destination/'profile.json').read_text()
        edit("(p/'profile.json').write_text('{\\n invalid json\\n}')")
        invalid = inside('python3',profile_runtime.PATH+'/authoring/validate.py',profile_edit.PATH,check=False)
        assert invalid.returncode and 'line 2, column' in invalid.stderr, invalid.stderr
        try:
            service.review_profile(name)
        except ValueError as error:
            assert 'line 2, column' in str(error), str(error)
        else:
            raise AssertionError('Factory accepted invalid profile JSON')
        (destination/'profile.json').write_text(manifest)
        # The edit is made by the sandbox user in its actual writable mount.
        initial_dockerfile = (destination/'Dockerfile').read_text()
        package_layer = "\nUSER root\nRUN apt-get update && apt-get install -y --no-install-recommends figlet postgresql-client && rm -rf /var/lib/apt/lists/*\nUSER node\n"
        edit("f=p/'Dockerfile'; f.write_text(f.read_text()+" + repr(package_layer) + ')')
        assert 'Static checks passed' in validate().stdout
        review = service.review_profile(name)
        with operation_locks.sandbox(name, 'check the profile worker for'):
            assert service.review_profile(name)['revision'] == review['revision'], 'Factory child did not inherit the held lifecycle lock'
        old_ids = ids()
        edit("f=p/'profile.md'; f.write_text(f.read_text()+'\\nUnreviewed edit\\n')")
        try:
            apply(review)
        except ValueError as error:
            assert 'changed after review' in str(error), str(error)
        else:
            raise AssertionError('An unreviewed edit was applied')
        assert ids() == old_ids
        (destination/'profile.md').write_bytes((Path(original['profile']['path'])/'profile.md').read_bytes())
        # A real build error must leave the old runtime alive and retain diagnostics.
        valid_dockerfile = (destination/'Dockerfile').read_text()
        edit("f=p/'Dockerfile'; f.write_text(f.read_text()+'\\nRUN echo PROFILE_BUILD_FAILURE >&2; exit 31\\n')")
        broken = service.review_profile(name)
        try:
            apply(broken)
        except ValueError:
            assert 'PROFILE_BUILD_FAILURE' in '\n'.join(output)
        else:
            raise AssertionError('A broken Dockerfile unexpectedly built')
        assert ids() == old_ids and config()['profile']['digest'] == original['profile']['digest']
        (destination/'Dockerfile').write_text(valid_dockerfile)
        if agent == 'pi':
            edit("f=p/'Dockerfile'; f.write_text(f.read_text()+'\\nRUN echo PROFILE_BUILD_WAIT; sleep 600\\n')")
            waiting_build=service.review_profile(name)
            ready=threading.Event()
            control=updating.Update(cooperative=True)
            def build_log(line):
                log(line)
                if line.strip()=='PROFILE_BUILD_WAIT':ready.set()
            with ThreadPoolExecutor() as pool:
                future=pool.submit(service.run_update,['profile-apply',name,'--revision',waiting_build['revision']],build_log,control)
                try:
                    assert ready.wait(600),'Build did not reach waiting command: '+'\n'.join(output[-15:])
                    assert control.cancel()
                finally:
                    if not ready.is_set():control.cancel()
                try:future.result(timeout=180)
                except updating.UpdateCancelled:pass
                else:raise AssertionError('Build cancellation did not complete')
            assert ids()==old_ids
            (destination/'Dockerfile').write_text(valid_dockerfile)
        apply(service.review_profile(name))
        assert inside('sh','-c','command -v figlet').returncode == 0
        assert inside('cat','/home/node/profile-edit-marker').stdout == 'KEEP_PRIVATE_HOME'
        for resource, identifier in old_ids.items():
            if resource in profile_services.resources(original)['container']:
                assert ids()[resource] == identifier
        if original_password:
            assert sql_marker()=='preserved'
            assert json.loads((state/'profile-services/client/connections.json').read_text())['database']['password']==original_password
        # A successfully built image can still fail at startup. The old runtime
        # must survive that cutover, and diagnostics must outlive the candidate.
        before_boot=ids()
        bootstrap=('/usr/local/lib/sandbox-bootstrap.py' if agent=='pi' else '/opt/primary/entrypoint.py')
        edit("f=p/'Dockerfile'; f.write_text(f.read_text()+"+repr("\nUSER root\nRUN printf 'raise SystemExit(\"PROFILE_BOOT_FAILURE\")\\n' > "+bootstrap+"\nUSER node\n")+")")
        broken_boot=service.review_profile(name)
        try:apply(broken_boot)
        except ValueError:
            assert 'PROFILE_BOOT_FAILURE' in '\n'.join(output)
        else:raise AssertionError('Broken bootstrap unexpectedly started')
        assert ids()==before_boot
        assert inside('cat','/home/node/profile-edit-marker').stdout=='KEEP_PRIVATE_HOME'
        (destination/'Dockerfile').write_text(valid_dockerfile)
        print('PASS '+agent+': editable mount/context, validation, stale review, failed build and package apply with preserved private home',flush=True)
        # A new host has its own build context. Existing service recipes are untouched.
        host = 'database' if agent == 'pi' else 'reporting'
        edit("example=Path('/opt/sandbox/base-profile/authoring/example'); "
             "shutil.copytree(example/'services/postgres',p/'services/new-host',dirs_exist_ok=True); "
             "m=json.loads((p/'profile.json').read_text()); s=json.loads((example/'profile.json').read_text())['services']["+repr(host)+"]; "
             "s['build']['context']='services/new-host'; m.setdefault('services',{})["+repr(host)+"]=s; "
             "(p/'profile.json').write_text(json.dumps(m)); f=p/'profile.md'; f.write_text(f.read_text()+'\\nUse "+host+".profile.internal for the project database; read /run/profile-services/connections.json.\\n')")
        valid_manifest = (destination/'profile.json').read_text()
        before_services = config()
        before_ids = ids()
        edit("m=json.loads((p/'profile.json').read_text()); m['services']["+repr(host)+"]['healthcheck']=['missing-profile-healthcheck']; (p/'profile.json').write_text(json.dumps(m))")
        broken = service.review_profile(name)
        candidate = {**config(),'profile':broken['profile']}
        try:
            apply(broken)
        except ValueError:
            diagnostic='\n'.join(output)
            assert 'missing-profile-healthcheck' in diagnostic and '127' in diagnostic, diagnostic[-3000:]
        else:
            raise AssertionError('Missing service healthcheck unexpectedly succeeded')
        assert ids() == before_ids
        assert_no_added(before_services,candidate)
        # Waiting for readiness is cancellable without killing the cleanup owner.
        edit("m=json.loads((p/'profile.json').read_text()); m['services']["+repr(host)+"]['healthcheck']=['sh','-c','echo PROFILE_WAITING; exit 1']; (p/'profile.json').write_text(json.dumps(m))")
        waiting = service.review_profile(name)
        candidate = {**config(),'profile':waiting['profile']}
        control = updating.Update(cooperative=True)
        ready = threading.Event()
        def cancellation_log(line):
            log(line)
            if 'PROFILE_WAITING' in line: ready.set()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(service.run_update,['profile-apply',name,'--revision',waiting['revision']],cancellation_log,control)
            try:
                assert ready.wait(600), 'No readiness milestone; recent output: '+ '\n'.join(output[-20:])
                competing = service.capture('profile-review',name)
                assert competing.returncode and 'another Factory operation' in competing.stderr, competing
                assert 'Traceback' not in competing.stderr
                assert control.cancel(), 'Preparation cancellation was refused'
            finally:
                if not ready.is_set(): control.cancel()
            try:
                future.result(timeout=180)
            except updating.UpdateCancelled:
                pass
            else:
                raise AssertionError('Cancelled preparation did not report cancellation')
        assert ids() == before_ids
        assert_no_added(before_services,candidate)
        if agent=='pi':
            # Simulate an abrupt worker death during readiness. The next review
            # must recover resources and still return a usable structured review.
            ready=threading.Event()
            process=subprocess.Popen(service.command('profile-apply',name,'--revision',waiting['revision']),
                                     stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            def reader():
                for line in process.stdout:
                    log(line.rstrip())
                    if 'PROFILE_WAITING' in line and not line.startswith('STEP '):ready.set()
            thread=threading.Thread(target=reader,daemon=True);thread.start()
            try:
                assert ready.wait(600),'Interrupted-worker fixture never reached service readiness'
                process.kill();process.wait();thread.join(timeout=30)
            finally:
                if process.poll() is None:process.kill();process.wait()
            assert (state/'profile-preparation/record.json').is_file()
            recovered=service.review_profile(name)
            assert recovered['revision']==waiting['revision']
            assert ids()==before_ids
            assert_no_added(before_services,candidate)
            assert not (state/'profile-preparation').exists()
        (destination/'profile.json').write_text(valid_manifest)
        apply(service.review_profile(name))
        settings = json.loads(inside('cat','/run/profile-services/connections.json').stdout)
        user, database = ('app','app') if host == 'database' else ('reporter','reports')
        assert database in inside('ssh','-F','/run/profile-services/config',host+'.profile.internal',
                                 f'psql -U {user} -d {database} -Atc "SELECT current_database()"').stdout
        for resource, identifier in before_ids.items():
            if resource in profile_services.resources(before_services)['container']:
                assert ids()[resource] == identifier
        assert settings[host]['password']
        if original_password:
            assert settings['database']['password']==original_password
            assert sql_marker()=='preserved'
        applied_manifest=(destination/'profile.json').read_text()
        edit("m=json.loads((p/'profile.json').read_text()); m['services']["+repr(host)+"]['volumes']['data']='/different-data'; (p/'profile.json').write_text(json.dumps(m))")
        try:service.review_profile(name)
        except ValueError as error:
            assert host in str(error) and 'volumes' in str(error)
        else:raise AssertionError('Existing service storage was silently changed')
        (destination/'profile.json').write_text(applied_manifest)
        assert config()['profile']['digest'] == profile_runtime.applied(state)['profile_digest']
        service.run(['down',name],log)
        with (destination/'profile.md').open('a') as stream: stream.write('\nAdditional profile instructions.\n')
        apply(service.review_profile(name))
        for resource in ids():
            assert not sandbox.inspect(config(),resource)['State']['Running'], resource
        service.run(['up',name],log)
        assert inside('cat','/home/node/profile-edit-marker').stdout == 'KEEP_PRIVATE_HOME'
        print('PASS '+agent+': new host, diagnostic readiness failure, cooperative cancel, exact rollback/retry and stopped-state apply',flush=True)
    finally:
        if (state/'config.json').exists():
            current = config()
            wanted = profile_services.resources(current)
            service.run(['delete',name],log)
            for kind, names in wanted.items():
                for resource in names:
                    assert sandbox.run(['podman',kind,'exists',resource],capture=True,check=False).returncode == 1, resource
        assert (workspace/'AGENTS.md').read_text() == 'PROJECT_INSTRUCTIONS_UNCHANGED\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=('pi','codex','claude','agy'), action='append')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-profile-edit-') as temporary:
        root=Path(temporary)
        provider=root/'provider'
        provider.mkdir()
        (provider/'provider.json').write_bytes((sandbox.ROOT.parent/'examples/provider/provider.json').read_bytes())
        (provider/'adapter.mjs').write_text(ADAPTER)
        with patch.dict(os.environ,SANDBOX_REGISTRY_DIR=str(root/'library')):
            registry.add('provider','fixture',str(provider))
            for agent in args.agent or ('pi','codex','claude','agy'):
                exercise(agent,root)


if __name__ == '__main__':
    main()
