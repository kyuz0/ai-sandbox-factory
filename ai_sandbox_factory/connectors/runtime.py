"""A connector receives only its approved route table and its own host keyring token."""
import hashlib
import json
from pathlib import Path
import subprocess

from . import ROOT, permitted_routes, saved, validate_ids


def worker_name(config, identifier):
    # Digest both components to fit the engine name limit for any valid sandbox name.
    identity = json.dumps([config['name'], identifier]).encode()
    return 'pi-sandbox-factory-connector-' + hashlib.sha256(identity).hexdigest()


def client_socket(config, identifier):
    directory = '/opt/pi-security/config' if config.get('primary_agent', 'pi') == 'pi' else '/run/primary-runtime'
    return f'{directory}/connectors/{identifier}/api.sock'


def _validate_token(value):
    if not isinstance(value, str) or not value or not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError('Enter an ASCII bearer token without whitespace.')


def store_token(plugin, value):
    from ..provider_credentials import os_store
    if not plugin.external:
        raise ValueError('Choose an installed external HTTPS connector.')
    _validate_token(value)
    try:
        os_store().set_password(plugin.service, plugin.account, value)
    except Exception:
        raise ValueError(f'Unlock the system credential store to save the token for {plugin.title}.') from None


def _saved_token(plugin):
    from ..provider_credentials import os_store
    try:
        value = os_store().get_password(plugin.service, plugin.account)
    except Exception:
        raise ValueError(f'Unlock the system credential store to use {plugin.title}.') from None
    if value:
        _validate_token(value)
    return value


def has_token(plugin):
    return bool(_saved_token(plugin))


def _token(plugin):
    value = _saved_token(plugin)
    if not value:
        raise ValueError(f'Run ai-sandbox-factory connector-login {plugin.id} on this computer to save a bearer token.')
    return value


def require_tokens(config, state):
    for plugin in saved(config, state):
        _token(plugin)


def _start(config, state, plugin, tag, old, token):
    from .. import host_platform, sandbox
    name = worker_name(config, plugin.id)
    engine = config['engine']
    routes = dict(permitted_routes(plugin, config))
    if old and old['State']['Running']:
        _update(engine, old, plugin, routes)
        return None
    if old:
        sandbox.run([engine, 'rm', old['Id']])
    runtime = 'runtime-config' if config.get('primary_agent', 'pi') == 'pi' else 'primary-runtime'
    sockets = Path(state) / runtime / 'connectors' / plugin.id
    sockets.mkdir(mode=0o755, parents=True, exist_ok=True)
    mount_mode = 'rw' if host_platform.is_mac() else 'rw,z'
    args = [engine, 'run', '-d', '--name', name, '--label', f'{sandbox.LABEL}={config["name"]}',
            '--label', f'{ROLE}={plugin.id}', '--label', f'{SCOPE}={plugin.account}', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user', f'{config["uid"]}:{config["gid"]}', '-v', f'{sockets}:/socket:{mount_mode}']
    if engine == 'podman':
        args += ['--userns=keep-id:uid=1000,gid=1000']
    worker = sandbox.run(args + [tag], capture=True).stdout.strip()
    try:
        _deliver(engine, worker, {'routes': routes, 'token': token}, plugin)
    except BaseException:
        sandbox.run([engine, 'rm', '-f', worker])
        raise
    return worker


ROLE = 'io.pi-sandbox-factory.connector.id'
SCOPE = 'io.pi-sandbox-factory.connector.scope'


def _owned(config, identifier):
    from .. import sandbox
    name = worker_name(config, identifier)
    current = sandbox.inspect(config, name)
    if current and current['Config'].get('Labels', {}).get(ROLE) != identifier:
        raise ValueError(f'Refusing to manage an unrelated container named {name}.')
    return current


def _deliver(engine, worker, payload, plugin):
    command = [engine, 'exec', '-i', worker, 'python3', '/app/receive.py']
    if 'token' in payload:
        command.append('--starting')
    result = subprocess.run(command,
                            input=json.dumps(payload), text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f'Could not apply permissions to the {plugin.title} connector.')


def _update(engine, worker, plugin, routes):
    if worker['Config']['Labels'].get(SCOPE) != plugin.account:
        raise ValueError(f'The running {plugin.title} connector uses a different service credential.')
    _deliver(engine, worker['Id'], {'routes': routes}, plugin)


def configure_running(config, state):
    """Update active grants in place. The existing credential stays in its companion."""
    updates = []
    for plugin in saved(config, state):
        routes = dict(permitted_routes(plugin, config))
        worker = _owned(config, plugin.id)
        if not worker or not worker['State']['Running']:
            raise ValueError(f'The {plugin.title} connector is not running.')
        updates.append((worker, plugin, routes))
    for worker, plugin, routes in updates:
        _update(config['engine'], worker, plugin, routes)


def prepare(config, state):
    """Validate grants early. Fresh workers need a token and healthy ones retain theirs."""
    tokens = {}
    for plugin in saved(config, state):
        permitted_routes(plugin, config)
        worker = _owned(config, plugin.id)
        if worker and worker['State']['Running']:
            if worker['Config']['Labels'].get(SCOPE) != plugin.account:
                raise ValueError(f'The running {plugin.title} connector uses a different service credential.')
        else:
            tokens[plugin.id] = (plugin.account, _token(plugin))
    return tokens


def build(config):
    from .. import sandbox, image_sources
    source = ROOT / 'companion'
    local = 'localhost/pi-sandbox-factory-connector:' + sandbox.tree_digest(source)
    def build_local():
        if sandbox.run([config['engine'], 'image', 'inspect', local], capture=True, check=False,
                       stderr=subprocess.PIPE).returncode:
            sandbox.run([config['engine'], 'build', '-t', local, source])
    return image_sources.ensure(config, 'connector', local, build_local)


def up(config, state, *, prepared=None):
    from .. import sandbox
    plugins = saved(config, state)
    if not plugins:
        return []
    source = ROOT / 'companion'
    tag = 'localhost/pi-sandbox-factory-connector:' + sandbox.tree_digest(source)
    engine = config['engine']
    workers = {plugin.id: _owned(config, plugin.id) for plugin in plugins}
    tokens = {}
    for plugin in plugins:
        current = workers[plugin.id]
        if not current or not current['State']['Running']:
            credential = (prepared or {}).get(plugin.id)
            if credential and credential[0] == plugin.account:
                tokens[plugin.id] = credential[1]
            else:
                tokens[plugin.id] = _token(plugin)
    if tokens:
        from .. import image_sources
        tag = image_sources.runtime(config, 'connector', tag, build)
    started = []
    try:
        for plugin in plugins:
            worker = _start(config, state, plugin, tag, workers[plugin.id], tokens.get(plugin.id))
            if worker:
                started.append(worker)
    except BaseException:
        for worker in reversed(started):
            sandbox.run([engine, 'rm', '-f', worker])
        raise
    return started


def down(config, remove=False):
    from .. import sandbox
    for identifier in validate_ids(config.get('connector_ids', [])):
        current = _owned(config, identifier)
        if current:
            sandbox.run([config['engine'], 'rm', '-f', current['Id']])
