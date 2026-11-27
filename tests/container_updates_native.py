"""Replace a real Codex controller, workspace and built-in PostgreSQL service together."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import (container_update_apply, container_updates, image_sources, operation_locks,
                                primary_agents, profile_runtime, profile_services, sandbox, service_data)
from browser_editor_updates import workspace_config


def check(root, image_source):
    name = 'native-update-' + uuid4().hex[:12]
    repository = 'localhost/' + name
    state = root / 'state' / name
    project = root / 'project'
    state.mkdir(parents=True)
    project.mkdir()
    (project / 'marker').write_text('project data')
    config = {**workspace_config(), 'name': name, 'workspace': str(project), 'network': 'allowlist',
              'primary_agent': 'codex', 'primary_auth': 'login', 'image_source': image_source}
    # A previous built-in service shipped a different Dockerfile.
    source = root / 'previous-profile'
    shutil.copytree(sandbox.load_profile('developer-postgres')['path'], source)
    for path in ('Dockerfile', 'services/database/Dockerfile'):
        with (source / path).open('a') as recipe:
            recipe.write('\nLABEL factory-test-revision="previous"\n')
    prior = sandbox.load_profile(str(source))
    prior.update(factory_profile='developer-postgres', registry_name='developer-postgres',
                 factory_profile_digest=prior['digest'])
    config['profile'] = sandbox.snapshot_profile(prior, state)
    config['common'] = sandbox.snapshot_profile(config['common'], state)
    (state / 'allowlist.txt').write_text('')
    (state / 'squid.conf').write_text(sandbox.network_policy(config, []))
    workspace = sandbox.names(config)[0]
    controller = primary_agents.names(config)[0]
    database = profile_services.container(config, 'database')
    engine = sandbox.run

    def stage(reference, suffix=''):
        return repository + ':' + hashlib.sha256((reference + suffix).encode()).hexdigest()[:20]

    substitutions = ('localhost/pi-sandbox-factory-proxy:1', primary_agents.local_image(config))

    def private_build(argv, **options):
        # Local builds reference these tags in both engine commands and build contexts.
        args = [str(value) for value in argv]
        for shared in substitutions:
            args = [arg.replace(shared, stage(shared)) for arg in args]
        return engine(args, **options)

    def saved():
        return json.loads((state / 'config.json').read_text())

    def ids():
        names = set.union(*container_updates.runtime_roles(config).values()) | {database}
        return {item: sandbox.inspect(config, item)['Id'] for item in names}

    def inside(*args):
        return engine(['podman', 'exec', workspace, *args], capture=True).stdout.strip()

    def sql(query):
        return engine(['podman', 'exec', database, 'sh', '-c',
                       'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U app -d app -Atc "$1"',
                       'psql', query], capture=True).stdout.strip()

    def verify(value):
        assert inside('/usr/local/bin/codex', '--version').startswith('codex-cli ')
        assert inside('cat', '/workspace/marker', '/home/node/private-marker') == 'project dataprivate data'
        marker = engine(['podman', 'exec', controller, 'cat', '/home/node/controller-marker'], capture=True)
        assert marker.stdout == 'controller data'
        inside('test', '!', '-e', '/home/node/controller-marker')
        assert sql('SELECT value FROM update_marker') == value
        assert (state / 'profile-services/client/connections.json').read_bytes() == connections

    service_image = profile_services.image
    with operation_locks.sandbox(name, 'test native container updates for'), \
            patch.object(sandbox, 'stage_image', stage), patch.object(sandbox, 'run', private_build), \
            patch.object(profile_services, 'image', lambda *args: stage(service_image(*args))):
        try:
            sandbox.build(config)
            artifact = next(item for item in config['image_artifacts'] if item['role'] == 'controller-codex')
            current_image = artifact['image_id']
            context = root / 'previous-controller'
            context.mkdir()
            (context / 'Dockerfile').write_text(f'FROM {current_image}\nLABEL factory-test-revision="previous"\n')
            previous_tag = stage('previous-controller')
            engine(['podman', 'build', '--pull=never', '-t', previous_tag, context])
            previous_id = image_sources._inspect(config, previous_tag)['Id']
            artifact.update(image_id=previous_id, reference=previous_id, source='local', recipe='previous-controller')
            config['image_refs']['controller-codex'] = previous_id
            with profile_runtime.recording(config, state):
                pass
            sandbox.write_json(state / 'config.json', config)
            sandbox.up(config, state, prepare_sign_in=False)
            before = ids()
            prior_images = {item: sandbox.inspect(config, item)['Image'] for item in (workspace, controller, database)}
            connections = (state / 'profile-services/client/connections.json').read_bytes()
            inside('sh', '-c', 'printf "private data" >/home/node/private-marker')
            engine(['podman', 'exec', controller, 'sh', '-c', 'printf "controller data" >/home/node/controller-marker'])
            sql("CREATE TABLE update_marker (value text); INSERT INTO update_marker VALUES ('original')")
            review = container_updates.plan(saved(), state)
            assert review['roles'] == ['controller-codex', 'workspace'], review
            assert review['service_changes']['replaced_services'] == ['database'], review
            verify('original')

            def fail_after_cutover(*args, **kwargs):
                # The candidate must write to its own data copy before rollback.
                if kwargs.get('roles_updated'):
                    assert ids()[database] != before[database]
                    sql("UPDATE update_marker SET value='candidate'")
                    raise ValueError('fixture failure after database write')
                return original_record(*args, **kwargs)

            original_record = container_updates.record_applied
            with patch.object(container_updates, 'record_applied', fail_after_cutover):
                try:
                    container_update_apply.apply(saved(), state, review)
                except ValueError as error:
                    assert 'fixture failure after database write' in str(error), str(error)
                else:
                    raise AssertionError('Expected the candidate to roll back')
            assert ids() == before
            assert not service_data.entries(saved())
            verify('original')
            retry = container_updates.plan(saved(), state)
            container_update_apply.apply(saved(), state, retry)
            after = ids()
            assert {key for key in before if before[key] != after[key]} == {workspace, controller, database}
            assert all(sandbox.inspect(config, item)['Image'] != old for item, old in prior_images.items())
            installed = sandbox.inspect(config, controller)['Image']
            assert container_updates.image_id(installed) == container_updates.image_id(current_image)
            assert container_updates.plan(saved(), state)['status'] == 'current'
            verify('original')
            recovery, = service_data.entries(saved())
            assert recovery['service'] == 'database' and recovery['reason'] == 'replaced'
            sql("UPDATE update_marker SET value='after'")
            service_data.restore(saved(), state, recovery['id'], 'database')
            verify('original')
            print('PASS native controller, workspace and PostgreSQL cutover, rollback and SQL data recovery', flush=True)

            # An older recipe can produce identical bytes. Recreate just the native
            # proxy with that recipe while the other three gateways retain theirs.
            prior_gateway = saved()
            gateway_artifact = next(item for item in prior_gateway['image_artifacts'] if item['role'] == 'gateway')
            gateway_artifact['recipe'] = 'fixture-old'
            proxy = primary_agents.names(prior_gateway)[1]
            original = sandbox.inspect(config, proxy)
            engine(['podman', 'rm', '-f', original['Id']])
            primary_agents.up(prior_gateway, state, only={proxy})
            mixed = sandbox.inspect(config, proxy)
            assert mixed['Id'] != original['Id'] and mixed['Image'] == original['Image']
            container_updates.record_applied(saved(), state)
            review = container_updates.plan(saved(), state)
            assert review['status'] == 'available' and review['roles'] == ['gateway'], review

            def peer_state():
                peers = [sandbox.inspect(config, item) for item in (workspace, controller, database)]
                return [(peer['Id'], peer['State']['StartedAt']) for peer in peers]

            gateways = container_updates.runtime_roles(saved())['gateway']
            assert len(gateways) == 4
            before = ids()
            peers = peer_state()
            container_update_apply.apply(saved(), state, review)
            after = ids()
            assert {key for key in before if before[key] != after[key]} == gateways
            assert peer_state() == peers
            verify('original')
            assert container_updates.plan(saved(), state)['status'] == 'current'
            print('PASS mixed gateway recipes replaced all four gateways and kept their consumers running', flush=True)
        finally:
            try:
                sandbox.delete(saved() if (state / 'config.json').exists() else config, state)
            finally:
                images = engine(['podman', 'images', '--format', '{{.Repository}}:{{.Tag}}'], capture=True)
                owned = [tag for tag in images.stdout.splitlines() if tag.split(':')[0] == repository]
                if owned:
                    engine(['podman', 'rmi', '--no-prune', *owned])
            for command in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
                remaining = engine(['podman', *command, '--filter', f'label={sandbox.LABEL}={name}'], capture=True)
                assert not remaining.stdout.strip(), remaining.stdout


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-source', choices=['prebuilt', 'local'], default='prebuilt')
    parser.add_argument('--candidate-index', type=Path)
    with tempfile.TemporaryDirectory(prefix='factory-native-update-') as temporary:
        args = parser.parse_args()
        if args.candidate_index:
            image_sources.INDEX = args.candidate_index.resolve()
        check(Path(temporary), args.image_source)
