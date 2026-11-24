"""Replace a workspace while retaining its profile applications and recovery copy."""
from pathlib import Path
from uuid import uuid4

from . import profile_services


def runtime_names(config):
    from . import aws_access, connectors, inference, primary_agents, primary_workspace, sandbox, sandbox_advisors, sandbox_ssh
    names = set(sandbox.names(config)[:2])
    names.update(sandbox_advisors.names(config)[:2])
    names.update((sandbox_ssh.names(config)[0], inference.names(config)[0], aws_access.worker_name(config)))
    names.update(connectors.worker_name(config, identifier) for identifier in config.get('connector_ids', []))
    if primary_workspace.agent(config) != 'pi':
        names.update(primary_agents.resources(config)['container'])
    return names


def apply(original, updated, state, *, text=None):
    from . import activity, sandbox
    state = Path(state)
    profile_services.validate_update(original, updated)
    engine = original['engine']
    names = runtime_names(original) | runtime_names(updated)
    workspace = sandbox.names(original)[0]
    prior = {name: sandbox.inspect(original, name) for name in sorted(names)}
    services = {name: sandbox.inspect(original, name)
                for name in profile_services.resources(original)['container']}
    old_text = (state / 'allowlist.txt').read_text()
    old_policy = (state / 'squid.conf').read_text()
    pointer = state / 'profile-runtime/applied.json'
    old_pointer = pointer.read_bytes() if pointer.exists() else None
    moved = {}
    try:
        for name, info in prior.items():
            if not info:
                continue
            if info['State']['Running']:
                sandbox.run([engine, 'stop', info['Id']])
            sandbox.run([engine, 'rename', info['Id'], name + '-previous-' + uuid4().hex])
            moved[name] = info['Id']
        if text is not None:
            (state / 'allowlist.txt').write_text(text)
        (state / 'squid.conf').write_text(sandbox.network_policy(
            updated, sandbox.parse_rules((state / 'allowlist.txt').read_text())))
        activity.save_config(state, updated)
        profile_services.update_policy(updated, state, (state / 'allowlist.txt').read_text(), updated.get('website_groups', {}))
        sandbox.up(updated, state, prepare_sign_in=False)
        # Validation may start a stopped sandbox, but never changes its saved state.
        if prior[workspace] and not prior[workspace]['State']['Running']:
            for name in names:
                if sandbox.inspect(updated, name):
                    sandbox.run([engine, 'stop', name])
        for name, info in services.items():
            if info and not info['State']['Running']:
                sandbox.run([engine, 'stop', name])
    except Exception:
        for name in names:
            current = sandbox.inspect(updated, name)
            if current and current['Id'] != (prior[name] or {}).get('Id'):
                sandbox.run([engine, 'rm', '-f', current['Id']])
            if name in moved:
                sandbox.run([engine, 'rename', moved[name], name])
        (state / 'allowlist.txt').write_text(old_text)
        (state / 'squid.conf').write_text(old_policy)
        activity.save_config(state, original)
        if old_pointer is None:
            pointer.unlink(missing_ok=True)
        else:
            pointer.write_bytes(old_pointer)
        profile_services.update_policy(original, state, old_text, original.get('website_groups', {}))
        if prior[workspace] and prior[workspace]['State']['Running']:
            sandbox.up(original, state, prepare_sign_in=False)
        else:
            for name, info in prior.items():
                if info and info['State']['Running']:
                    sandbox.run([engine, 'start', info['Id']])
        for name, info in {**prior, **services}.items():
            if info and not info['State']['Running']:
                sandbox.run([engine, 'stop', name])
        print('The replacement failed. Previous workspace containers, settings and profile services were restored.', flush=True)
        raise
    for name, container_id in moved.items():
        sandbox.run([engine, 'rm', container_id])
    if text is not None and text != old_text:
        activity.record(state, 'edited')
