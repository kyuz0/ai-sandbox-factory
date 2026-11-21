"""Apply per-sandbox connector grants without replacing the workspace."""
import getpass
import subprocess
import sys
from pathlib import Path

from . import activity, connectors, updating


def parse_cli_grants(arguments):
    """Read repeated --connector ID[:OPERATION,...] choices."""
    identifiers = []
    operations = {}
    for argument in arguments:
        identifier, separator, choices = argument.partition(':')
        identifiers.append(identifier)
        if separator:
            names = choices.split(',')
            if not all(names):
                raise ValueError('List the operation names after the colon in --connector, for example build-status:latest.')
            operations[identifier] = names
    return connectors.validate_ids(identifiers), operations


def capture(ids, state, reviewed=None, reviewed_instructions=None):
    connectors.snapshot(ids, state, refresh=True)
    if reviewed is None and reviewed_instructions is None:
        return
    plugins = connectors.saved({'connector_ids': ids}, state)
    if reviewed is not None and reviewed != {c.id: dict(c.routes) for c in plugins}:
        raise ValueError('The connector requests changed. Open Connectors and review them again.')
    if reviewed_instructions is not None and reviewed_instructions != {c.id: c.reviewed_instructions for c in plugins}:
        raise ValueError('The instructions for the coding agent changed. Open Connectors and review them again.')


def check_mount(config, state, container):
    if not container:
        return
    native = config.get('primary_agent', 'pi') != 'pi'
    source = Path(state) / ('primary-runtime' if native else 'runtime-config')
    target = '/run/primary-runtime' if native else '/opt/pi-security/config'
    if not any(m.get('Destination') == target and m.get('Source') == str(source)
               and m.get('Type') == 'bind' and m.get('RW') is False for m in container.get('Mounts', [])):
        raise ValueError('This sandbox predates connector support. To use this connector, update the '
                         'sandbox first in Actions… → Update sources and recreate…. '
                         'Review what will restart before continuing.')


def helper_states(config):
    """Record which optional services were already running before connector startup."""
    from . import aws_access, inference, primary_agents, sandbox, sandbox_advisors, sandbox_ssh
    names = []
    if sandbox_advisors.enabled(config):
        names.extend(reversed(sandbox_advisors.names(config)[:2]))
    if sandbox_ssh.enabled(config):
        names.append(sandbox_ssh.names(config)[0])
    if inference.enabled(config):
        names.append(inference.names(config)[0])
    if config.get('aws_access'):
        names.append(aws_access.worker_name(config))
    if primary_agents.enabled(config):
        names.extend(reversed(primary_agents.names(config)[:2]))
    return {name: sandbox.inspect(config, name) for name in names}


def restore_start(config, previous, new_workers):
    """Stop only processes started by this attempt, keeping containers and saved volumes."""
    from . import sandbox
    engine = config['engine']
    failures = []
    for worker in new_workers:
        result = sandbox.run([engine, 'rm', '-f', worker], capture=True, check=False, stderr=subprocess.DEVNULL)
        if result.returncode:
            failures.append(worker)
    for name, before in reversed(previous.items()):
        if before and before['State']['Running']:
            continue
        owned = sandbox.inspect(config, name)
        if owned and owned['State']['Running']:
            result = sandbox.run([engine, 'stop', owned['Id']], capture=True, check=False, stderr=subprocess.DEVNULL)
            if result.returncode:
                failures.append(name)
    if failures:
        raise ValueError('Connector startup failed. Check these containers, which could not be stopped: ' +
                         ', '.join(failures))


def check_sockets(config, workspace, identifiers):
    from . import sandbox
    # A bind can appear correct in engine inspect yet fail to carry a Unix
    # socket through a VM filesystem. Test from the actual workspace user.
    probe = 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.close()'
    for identifier in identifiers:
        result = sandbox.run([config['engine'], 'exec', '--user', f'{config["uid"]}:{config["gid"]}',
                              workspace, 'python3', '-c', probe, connectors.client_socket(config, identifier)],
                             capture=True, check=False, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError(f'The workspace could not open the socket for {identifier}. Its runtime folder must '
                             'support Unix sockets shared between containers. To start without this connector, '
                             'disconnect it in Settings → Connectors.')


def operations(config, state):
    ids = connectors.validate_ids(config.get('connector_ids', []))
    settings = config.get('connector_operations', {})
    if not isinstance(settings, dict) or set(settings) - set(ids):
        raise ValueError('Choose operations only for connectors assigned to this sandbox.')
    return {plugin.id: dict(connectors.permitted_routes(plugin, config)) for plugin in connectors.saved(config, state)}


def refresh_agent_context(config, state):
    from . import primary_agents, prompt_library
    from .connectors import context
    context.write(config, state)
    if not primary_agents.enabled(config):
        prompt_library.refresh_connectors(state)
    elif (Path(state) / 'primary-agent/context.md').is_file():
        primary_agents.write_context(config, state)


def discard_snapshots(identifiers, state):
    for identifier in identifiers:
        (Path(state) / 'connector-manifests' / (identifier + '.json')).unlink(missing_ok=True)


def apply(config, state, changes, apply_gate):
    from . import sandbox
    allowed = {'connector_ids', 'connector_operations', 'expected_connector_ids',
               'expected_connector_operations', 'expected_connector_routes', 'expected_connector_instructions'}
    if set(changes) - allowed:
        raise ValueError('Apply connector changes separately from other settings.')
    new = connectors.validate_ids(changes['connector_ids'])
    old = connectors.validate_ids(config.get('connector_ids', []))
    prior = config.get('connector_operations', {})
    grants = {i: prior[i] for i in new if i in prior}
    if 'connector_operations' in changes:
        supplied = changes['connector_operations']
        if not isinstance(supplied, dict) or set(supplied) - set(new):
            raise ValueError('Choose operations only for connectors assigned to this sandbox.')
        if any(i not in supplied for i in new if i not in old):
            raise ValueError('Select the allowed operations for each new connector.')
        grants.update(supplied)
    updated = {**config, 'connector_ids': new, 'connector_operations': grants}
    if old == new and prior == grants:
        return
    if changes.get('expected_connector_ids', old) != old or changes.get('expected_connector_operations', prior) != prior:
        raise ValueError('The connectors for this sandbox changed. Open Connectors again.')
    added = [i for i in new if i not in old]
    removed = [i for i in old if i not in new]
    workspace = sandbox.inspect(config, sandbox.names(config)[0])
    if added:
        check_mount(config, state, workspace)
    if not updating.begin_apply(apply_gate):
        return
    try:
        capture(added, state, changes.get('expected_connector_routes'), changes.get('expected_connector_instructions'))
        next_routes = operations(updated, state)
        connectors.require_tokens({'connector_ids': added}, state)
        # A damaged snapshot must never prevent the user from revoking a connector.
        retained = [i for i in old if i in new]
        changed = [i for i, routes in operations({**config, 'connector_ids': retained,
            'connector_operations': {i: prior[i] for i in retained if i in prior}}, state).items()
            if routes != next_routes[i]]
    except Exception:
        discard_snapshots(added, state)
        raise
    running = workspace and workspace['State']['Running']
    try:
        if running and added:
            connectors.up({**updated, 'connector_ids': added}, state)
            check_sockets(updated, workspace['Id'], added)
        if running and changed:
            connectors.configure_running({**updated, 'connector_ids': changed}, state)
        if removed:
            connectors.down({**config, 'connector_ids': removed})
        refresh_agent_context(updated, state)
        activity.save_config(state, updated)
    except Exception:
        if added:
            connectors.down({**updated, 'connector_ids': added})
        if running and changed:
            connectors.configure_running({**config, 'connector_ids': changed}, state)
        if running and removed:
            connectors.up({**config, 'connector_ids': removed}, state)
        refresh_agent_context(config, state)
        discard_snapshots(added, state)
        raise
    discard_snapshots(removed, state)
    if running:
        print('Connector settings applied to the running sandbox.')
    else:
        print('Connector settings saved. They will apply the next time this sandbox starts.')


def login(identifier):
    plugin = next((item for item in connectors.catalog() if item.external and item.id == identifier), None)
    if plugin is None:
        raise ValueError('Choose an installed HTTPS connector.')
    if not sys.stdin.isatty():
        raise ValueError('Run connector-login in an interactive terminal on your computer.')
    print(f'{plugin.title} will send this token to these service URLs:')
    for _, url in plugin.routes:
        print('  ' + url)
    token = getpass.getpass('Service token (hidden): ')
    connectors.store_token(plugin, token)
    print('Token saved in your computer’s keyring.')
