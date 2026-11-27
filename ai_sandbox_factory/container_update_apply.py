"""Apply a reviewed built-in container update under the sandbox lifecycle lock."""
from pathlib import Path
from uuid import uuid4

from . import (activity, aws_access, connector_settings, connectors, container_updates, image_builds,
               image_sources, inference, operation_locks, primary_agents, profile_replacement,
               profile_runtime, profile_services, sandbox, sandbox_advisors, sandbox_ssh, updating)
from .job_progress import Progress


def _check_review(config, state, reviewed):
    current = container_updates.plan(config, state)
    if current['status'] != 'available':
        raise ValueError(current.get('reason') or f'No container update is available for {config["name"]!r}.')
    if current['revision'] != reviewed['revision']:
        raise ValueError(f'The container update for {config["name"]!r} changed after review. '
                         'Run update-containers --preview again and review the new changes.')
    return current


def _prepare(config, state, reviewed, roles):
    if reviewed['workspace']:
        sandbox.build(config, state, keep_roles=set(container_updates.runtime_roles(config)) - roles - {'workspace'})
        return
    # The advisor recipe uses the installed extension lock. A helper-only update
    # must not change the source snapshot attached to the running workspace.
    build = {**config, 'common': sandbox.common_profile(), 'image_refs': dict(config.get('image_refs', {}))}
    builders = {'advisor': sandbox_advisors.build, 'aws': aws_access.build, 'ssh': sandbox_ssh.build,
                'inference': inference.build, 'connector': connectors.build}
    with image_sources.recording() as artifacts:
        for role in sorted(roles):
            build['image_refs'].pop(role, None)
            if role == 'gateway':
                with operation_locks.proxy_image(config['engine']):
                    image_builds.gateway(build)
            else:
                builders[role](build)
            if role not in artifacts:
                raise ValueError(f'Cannot update {role}: the build did not record its image identity. '
                                 'Check the selected image source and retry.')
    config['image_refs'] = build['image_refs']
    config['image_artifacts'] = [item for item in config.get('image_artifacts', []) if item['role'] not in roles]
    config['image_artifacts'].extend(artifacts.values())


def _launch(config, state, helpers, old, credentials):
    names = set().union(*helpers.values())
    gateway = sandbox.names(config)[1]
    if gateway in names:
        sandbox.create_gateway(config, state, sandbox.parse_rules((state / 'allowlist.txt').read_text()))
    if names.intersection(sandbox_advisors.names(config)[:2]):
        sandbox_advisors.up(config, state, only=names)
    controller, proxy = primary_agents.names(config)[:2]
    if names.intersection((controller, proxy)):
        # Workspace cutover brings up the native egress gateway and runs the
        # full controller probe before committing, including on a stopped sandbox.
        ready = bool((old.get(controller) or {}).get('State', {}).get('Running'))
        primary_agents.up(config, state, only=names, check_ready=ready)
    if profile_services.selected(config):
        base = profile_services.prefix(config)
        if names.intersection((base + '-gateway', base + '-proxy')):
            profile_services.up(config, state, only=names)
    for role, module in (('ssh', sandbox_ssh), ('inference', inference)):
        if role in helpers:
            module.up(config, state)
    if 'aws' in helpers:
        aws_access.up(config, state, credentials=credentials['aws'])
    if 'connector' in helpers:
        connectors.up(config, state, prepared=credentials['connector'])
        workspace = sandbox.inspect(config, sandbox.names(config)[0])
        if workspace and workspace['State']['Running']:
            connector_settings.check_sockets(config, workspace['Id'], config['connector_ids'])


def _verify(config, bindings):
    artifacts = {item['role']: item['image_id'] for item in config['image_artifacts']}
    for role, names in bindings.items():
        expected = artifacts[role]
        for name in names:
            info = sandbox.inspect(config, name) or {}
            actual = info.get('Image') or ''
            if not info.get('State', {}).get('Running') or actual.removeprefix('sha256:') != expected.removeprefix('sha256:'):
                raise ValueError(f'Cannot verify updated {name!r}: expected image {expected}, '
                                 f'found {actual or "no container image"} in state {info.get("State", {})}. '
                                 'Inspect the engine diagnostics and retry.')


def _retain(config, previous):
    for name, info in sorted(previous.items()):
        if not info:
            continue
        if info['State']['Running']:
            sandbox.run([config['engine'], 'stop', info['Id']])
        sandbox.run([config['engine'], 'rename', info['Id'], name + '-previous-' + uuid4().hex])


def restore_runtime(config, state, previous, credentials, restore):
    """Resume retained IDs, including the private sign-ins lost when a process stopped."""
    from .connectors import runtime
    roles = container_updates.runtime_roles(config)
    services = {profile_services.container(config, service): service for service in profile_services.selected(config)}
    workspace = sandbox.names(config)[0]

    def resume(name, record):
        held = sandbox.inspect(config, record['Id'])
        if not held:
            raise ValueError(f'The original {name!r} container {record["Id"]} is missing.')
        if held['State']['Running'] and held['State'].get('StartedAt') == record['State'].get('StartedAt'):
            return
        is_aws = name in roles.get('aws', ())
        is_connector = name in roles.get('connector', ())
        if held['State']['Running'] and (is_aws or is_connector):
            # A previous recovery can have stopped after starting the process
            # but before delivering its RAM-only credential. Replay both steps.
            sandbox.run([config['engine'], 'stop', record['Id']])
        sandbox.run([config['engine'], 'start', record['Id']])
        if is_aws:
            aws_access.restore_session(config, state, record, credentials=credentials.get('aws'))
        elif is_connector:
            runtime.restore_token(config, state, record, prepared=credentials.get('connector'))
        elif name in services:
            profile_services.ensure_ssh(config, services[name], record['Id'])

    for name, record in previous.items():
        if name != workspace and record and record['State']['Running']:
            restore('running state ' + name, lambda name=name, record=record: resume(name, record))
    if previous.get(workspace) and previous[workspace]['State']['Running']:
        restore('previous sandbox startup', lambda: sandbox.up(config, state, prepare_sign_in=False, workspace_only=True))


def apply(config, state, reviewed, *, apply_gate=None):
    state = Path(state)
    gate = Path(apply_gate) if apply_gate is not None else None
    cancelled = lambda: gate is not None and gate.exists()
    reviewed = _check_review(config, state, reviewed)
    updated = container_updates.target_config(config, state, reviewed)
    roles = set(reviewed['roles'])
    before, after = container_updates.runtime_roles(config), container_updates.runtime_roles(updated)
    helpers = {role: before[role] & names for role, names in after.items()
               if role in roles and role != 'workspace' and role in before}
    helper_names = set().union(*helpers.values())
    main = before['workspace'] if reviewed['workspace'] else set()
    # Workspace readiness uses the main gateway and native agent controller.
    # A stopped sandbox can need them briefly just to validate its replacement.
    dependencies = set()
    if main and sandbox.network_mode(config) == 'allowlist':
        dependencies.add(sandbox.names(config)[1])
    if main and primary_agents.enabled(config):
        dependencies.update(primary_agents.names(config)[:2])
    progress = Progress(['Prepare container images', 'Apply container updates'])
    with sandbox.image_graph(updated):
        with progress.stage('Prepare container images'), sandbox.cancellable_build(cancelled):
            _prepare(updated, state, reviewed, roles)
            credentials = {}
            if primary_agents.enabled(config) and (main or 'gateway' in helpers):
                primary_agents.check_ownership(config)
            if 'connector' in helpers:
                credentials['connector'] = connectors.prepare(config, state, replacing=True)
            if 'aws' in helpers:
                credentials['aws'] = aws_access.assume(config['name'], aws_access.settings(config['aws_access']))
        _check_review(config, state, reviewed)
        if not updating.begin_apply(gate):
            raise updating.UpdateCancelled()
        with progress.stage('Apply container updates'), profile_replacement.prepared_services(
                config, updated, state, replacement_names=main | helper_names | dependencies,
                defer_preparation=True, credentials=credentials) as prepare:
            old = {name: sandbox.inspect(config, name) for name in helper_names | dependencies}
            prepare()
            if helpers:
                _retain(config, {name: old[name] for name in helper_names})
                _launch(updated, state, helpers, old, credentials)
                _verify(updated, helpers)
            if reviewed['workspace']:
                if reviewed['profile_services']:
                    profile_runtime.select(updated, state)
                    profile_services.up(updated, state)
                    profile_services.ready(updated, state, check_ssh=False)
                profile_replacement.apply(config, updated, state, retain_previous=True,
                                          allow_service_changes=reviewed['profile_services'], workspace_only=True)
            else:
                activity.save_config(state, updated)
            for name, info in old.items():
                if not info or not info['State']['Running']:
                    candidate = sandbox.inspect(updated, name)
                    if candidate:
                        if not info:
                            sandbox.run([updated['engine'], 'rm', '-f', candidate['Id']])
                        elif candidate['State']['Running']:
                            sandbox.run([updated['engine'], 'stop', candidate['Id']])
            container_updates.record_applied(updated, state, roles_updated=reviewed['roles'])
    print(f'Container updates applied to {config["name"]!r}.', flush=True)
