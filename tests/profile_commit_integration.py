"""A failed transaction commit restores actual containers and application data."""
import copy
import errno
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import operation_locks, profile_edit, profile_replacement, profile_services, sandbox
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset


def main():
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
        try:
            service.create(draft, None, log)
            folder = root / 'editable-profile'
            service.run(['profile-fork',name,'--directory',str(folder)],log)
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
                except OSError as error:
                    assert error.errno == errno.ENOSPC, error
                else:
                    raise AssertionError('The injected commit failure was not reported')
            assert len(failures) == 1
            assert not (state/'profile-preparation').exists()
            assert config() == original
            after = runtime()
            assert after.keys() == before.keys()
            for key, previous in before.items():
                assert after[key]['Id'] == previous['Id'], key
                assert after[key]['State']['Running'] == previous['State']['Running'], key
            assert sql('SELECT value FROM commit_marker') == 'preserved'
            assert json.loads((state/'profile-services/client/connections.json').read_text())['database']['password'] == password
            print('PASS commit failure: previous container IDs, running state, settings, database and password restored before returning the error',flush=True)
            service.run(['profile-apply',name,'--revision',review['revision']],log)
            assert sql('SELECT value FROM commit_marker') == 'preserved'
            result = sandbox.run(['podman','exec',profile_services.container(config(),'database'),
                                  'printenv','EXAMPLE_VERSION'],capture=True)
            assert result.stdout.strip() == 'updated'
            print('PASS retry: the same reviewed service replacement succeeds and preserves database data',flush=True)
        finally:
            if (state/'config.json').exists():
                service.run(['delete',name],log)
                for command in (['ps','-aq'],['volume','ls','-q'],['network','ls','-q']):
                    remaining = sandbox.run(['podman',*command,'--filter',f'label={sandbox.LABEL}={name}'],capture=True)
                    assert not remaining.stdout.strip(),remaining.stdout
                print('PASS commit failure fixture: all labeled resources removed',flush=True)


if __name__ == '__main__':
    main()
