"""Recover real PostgreSQL and multi-volume service data in a disposable sandbox."""
import copy
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4

from ai_sandbox_factory import operation_locks, profile_replacement, profile_services, sandbox
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset


def log(line):
    print(line, flush=True)


def main(root):
    workspace = root / 'project'
    workspace.mkdir()
    service = Service(root / 'state')
    name = 'service-data-' + uuid4().hex[:8]
    state = service.state / name
    draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
    draft.update(name=name, primary_agent='codex', primary_auth='login', workspace=str(workspace),
                 engine='podman', network='allowlist', open_editor=False, allowlist='', model_providers={},
                 local_model_ports=None)
    draft['options']['browser'] = False
    folder = root / 'editable-profile'
    manifest = folder / 'profile.json'

    def run(*args):
        service.run([args[0], name, *args[1:]], log)

    def catalog():
        result = service.capture('service-data-list', name)
        assert not result.returncode, result.stderr
        return json.loads(result.stdout)

    def apply(profile):
        manifest.write_text(json.dumps(profile))
        revision = service.review_profile(name)['revision']
        run('profile-apply', '--revision', revision)

    def engine(*args):
        return sandbox.run(['podman', *args], capture=True).stdout.strip()

    def restore(saved, target):
        run('service-data-restore', '--saved', saved['id'], '--service', target)

    def export(saved, key, path, member):
        run('service-data-export', '--saved', saved['id'], '--volume', key, '--output', str(path))
        with tarfile.open(path) as archive:
            item = next(item for item in archive if item.name.lstrip('./') == member)
            return archive.extractfile(item).read().decode().strip()

    def sql(query):
        return engine('exec', database, 'sh', '-c',
                      'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U app -d app -Atc "$1"',
                      'psql', query)

    def runtime(exclude):
        config = service.config(name)
        names = profile_replacement.runtime_names(config) | profile_services.resources(config)['container']
        result = {}
        for item in names - {exclude}:
            info = sandbox.inspect(config, item)
            if info:
                result[item] = (info['Id'], info['State']['Running'])
        return result

    def target_info(container):
        return sandbox.inspect(service.config(name), container)

    def write_pair(value):
        engine('exec', files, 'sh', '-c', 'printf "%s-a" "$1" >/var/lib/postgresql/data/value; printf "%s-b" "$1" >/second/value',
               'fixture', value)

    def ssh_pair():
        workspace_container = sandbox.names(service.config(name))[0]
        value = engine('exec', workspace_container, 'ssh', '-F', profile_services.CLIENT + '/config',
                       '-T', profile_services.hostname('files'), 'cat /second/value')
        assert value == 'current-b', value

    def read_pair(value):
        assert engine('exec', files, 'cat', '/var/lib/postgresql/data/value') == value + '-a'
        assert engine('exec', files, 'cat', '/second/value') == value + '-b'

    try:
        service.create(draft, None, log)
        run('profile-fork', '--directory', str(folder))
        config = service.config(name)
        database = profile_services.container(config, 'database')
        files = profile_services.container(config, 'files')
        profile = json.loads(manifest.read_text())
        health = ('if test -f /run/profile-service/reject; then echo candidate-write > /var/lib/postgresql/data/value; '
                  'echo fixture-health-rejected >&2; exit 127; fi; '
                  'if test -f /run/profile-service/wait; then echo fixture-waiting-for-cancel >&2; exit 1; fi')
        profile['services']['files'] = {
            'description': 'Store two independent application files',
            'build': copy.deepcopy(profile['services']['database']['build']),
            'command': ['sh', '-c', 'trap "exit 0" INT TERM; while :; do sleep 3600 & wait "$!"; done'], 'healthcheck': ['sh', '-c', health],
            'volumes': {'first': '/var/lib/postgresql/data', 'second': '/second'},
        }
        apply(profile)
        assert not catalog()
        sql("CREATE TABLE marker (value text); INSERT INTO marker VALUES ('saved')")
        write_pair('saved')
        for spec in profile['services'].values():
            spec.setdefault('environment', {})['TEST_REVISION'] = 'current'
        apply(profile)
        db_saved, files_saved = (next(entry for entry in catalog() if entry['service'] == target)
                                 for target in ('database', 'files'))
        connections = state / 'profile-services/client/connections.json'
        credentials = connections.read_bytes()
        assert sql('SELECT value FROM marker') == 'saved'
        sql("UPDATE marker SET value='current'")
        write_pair('current')
        engine('stop', files)
        unrelated = runtime(database)
        image = target_info(database)['Image']
        restore(db_saved, 'database')
        assert sql('SELECT value FROM marker') == 'saved'
        assert target_info(database)['Image'] == image
        assert runtime(database) == unrelated
        assert connections.read_bytes() == credentials
        db_displaced = catalog()[0]
        assert db_displaced['reason'] == 'restored' and db_displaced['service'] == 'database'
        engine('stop', database)
        restore(db_displaced, 'database')
        assert not target_info(database)['State']['Running']
        assert runtime(database) == unrelated
        engine('start', database)
        profile_services.ready(service.config(name), state, service='database', check_ssh=False)
        assert sql('SELECT value FROM marker') == 'current'
        restore(db_saved, 'database')
        assert sql('SELECT value FROM marker') == 'saved'
        assert connections.read_bytes() == credentials
        log('PASS authenticated SQL restore, reusable source, displaced database, frozen image, credentials, stopped target and unchanged peers')

        others = runtime(files)
        restore(files_saved, 'files')
        assert not target_info(files)['State']['Running']
        assert runtime(files) == others
        engine('start', files)
        read_pair('saved')
        files_displaced = catalog()[0]
        restore(files_displaced, 'files')
        read_pair('current')
        assert runtime(files) == others
        log('PASS two-volume file recovery, including restoring a stopped target and retrieving its prior files')

        private = state / 'profile-services/servers/files'
        original = target_info(files)['Id']
        before = catalog()
        ssh_pair()
        (private / 'reject').touch()
        failure = service.capture('service-data-restore', name, '--saved', files_saved['id'], '--service', 'files')
        assert failure.returncode and 'fixture-health-rejected' in failure.stdout + failure.stderr, failure
        (private / 'reject').unlink()
        assert target_info(files)['Id'] == original
        read_pair('current')
        assert catalog() == before and runtime(files) == others
        ssh_pair()
        assert not (state / 'profile-preparation').exists()

        (private / 'wait').touch()
        gate = root / 'cancel-restore'
        output = root / 'restore-job.log'
        command = service.command('service-data-restore', name, '--saved', files_saved['id'],
                                  '--service', 'files', '--apply-gate', str(gate))
        with output.open('w') as stream, subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT) as job:
            try:
                deadline = time.monotonic() + 120
                while 'fixture-waiting-for-cancel' not in output.read_text():
                    assert job.poll() is None and time.monotonic() < deadline, output.read_text()
                    time.sleep(.1)
                gate.mkdir()
                job.wait(timeout=120)
                assert job.returncode == 0 and 'cancelled' in output.read_text(), output.read_text()
            finally:
                # A failed milestone assertion must also release a still-waiting worker.
                gate.mkdir(exist_ok=True)
        (private / 'wait').unlink()
        assert catalog() == before and target_info(files)['Id'] == original
        assert runtime(files) == others
        read_pair('current')
        ssh_pair()
        assert not (state / 'profile-preparation').exists()
        log('PASS failed readiness after candidate writes and cancellation during readiness restored the exact running target, both original files and authenticated workspace SSH')

        declarations = profile['services']
        profile['services'] = {}
        apply(profile)
        removed_db, removed_files = (next(entry for entry in catalog()
                                         if entry['service'] == target and entry['reason'] == 'removed')
                                     for target in ('database', 'files'))
        assert export(removed_db, 'data', root / 'postgres.tar', 'PG_VERSION') == '17'
        for key, expected in [('first', 'current-a'), ('second', 'current-b')]:
            assert export(removed_files, key, root / (key + '.tar'), 'value') == expected
        profile['services'] = {'files': declarations['files']}
        apply(profile)
        assert engine('exec', files, 'sh', '-c', 'test ! -f /var/lib/postgresql/data/value && test ! -f /second/value; echo $?') == '0'
        restore(removed_files, 'files')
        read_pair('current')
        log('PASS removal of the final services, export of exact database and file archives, and restore to the re-added service')
    finally:
        if (state / 'config.json').exists():
            while operation_locks.in_use(name):
                time.sleep(.25)
            run('delete')
        for command in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
            remaining = engine(*command, '--filter', f'label={sandbox.LABEL}={name}')
            assert not remaining, (name, remaining)
        log('PASS disposable sandbox cleanup: no labeled containers, volumes or networks remain')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='factory-service-data-') as temporary:
        main(Path(temporary))
