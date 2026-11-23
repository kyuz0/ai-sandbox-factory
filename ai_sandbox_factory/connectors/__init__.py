"""Discovery and immutable per-sandbox configuration for connector plugins."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .builtin import ADAPTERS

ROOT = Path(__file__).parent
SLUG = re.compile(r'[a-z][a-z0-9-]*')
BUILTIN_DIR = ROOT / 'builtin'
BUILTINS = (*ADAPTERS, *sorted(path.parent.name for path in BUILTIN_DIR.glob('*/connector.json')
                               if path.parent.name not in ADAPTERS))


@dataclass(frozen=True)
class Connector:
    id: str
    title: str
    description: str
    documentation: str
    available: bool
    external: bool
    routes: tuple = ()
    agent_instructions: str = ''
    operation_instructions: tuple = ()

    @property
    def reviewed_instructions(self):
        return {'service': self.agent_instructions, 'operations': dict(self.operation_instructions)}

    @property
    def account(self):
        return hashlib.sha256(json.dumps(self.routes).encode()).hexdigest()

    @property
    def service(self):
        return 'pi-sandbox-factory.connector.' + self.id


def directory():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'ai-sandbox-factory/connectors'


def permitted_routes(plugin, config):
    """The selected subset of the manifest routes approved for this sandbox."""
    grants = config.get('connector_operations', {})
    if not isinstance(grants, dict):
        raise ValueError('Connector permissions must map connector identifiers to operations.')
    if plugin.id not in grants:
        return plugin.routes
    names = grants[plugin.id]
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise ValueError(f'Select at least one operation for {plugin.title}.')
    known = dict(plugin.routes)
    if len(names) != len(set(names)) or any(name not in known for name in names):
        raise ValueError(f'Choose unique operations declared by {plugin.title}.')
    return tuple(route for route in plugin.routes if route[0] in names)


def _slug(value):
    return isinstance(value, str) and SLUG.fullmatch(value)


def _parse(raw, identifier):
    if not _slug(identifier) or identifier in BUILTINS:
        raise ValueError(f'Invalid external connector identifier {identifier!r}.')
    # Linux sockaddr_un.sun_path holds 107 bytes plus its terminator.
    if len(f'/opt/pi-security/config/connectors/{identifier}/api.sock') > 107:
        raise ValueError('The connector identifier exceeds the Unix socket path limit.')
    fields = {'version', 'id', 'title', 'description', 'documentation', 'operations'}
    if not isinstance(raw, dict) or set(raw) not in (fields, fields | {'agent_instructions'}) or raw['id'] != identifier or type(raw['version']) is not int or raw['version'] != 1:
        raise ValueError(f'Invalid version 1 connector manifest for {identifier}.')
    for field in ('title', 'description', 'documentation', 'agent_instructions'):
        if field not in raw:
            continue
        value = raw[field]
        if not isinstance(value, str) or not value.strip() or not all(c.isprintable() or (field in ('documentation', 'agent_instructions') and c == '\n') for c in value):
            raise ValueError(f'Enter plain text for connector {field}.')
    operations = raw['operations']
    if not isinstance(operations, dict) or not operations:
        raise ValueError('Specify at least one named HTTPS GET operation.')
    urls = {}
    instructions = {}
    for name, operation in operations.items():
        if isinstance(operation, dict):
            if set(operation) not in ({'url'}, {'url', 'instructions'}):
                raise ValueError('An operation accepts a URL and optional instructions.')
            if 'instructions' in operation:
                note = operation['instructions']
                if not isinstance(note, str) or not note.strip() or not all(c.isprintable() or c == '\n' for c in note):
                    raise ValueError('Use plain text for the operation instructions.')
                instructions[name] = note
            url = operation['url']
        else:
            url = operation
        urls[name] = url
        if not _slug(name) or not isinstance(url, str) or not url.isascii() or not url.isprintable() or ' ' in url:
            raise ValueError('Use operation names containing lowercase letters, digits and hyphens and exact HTTPS URLs.')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.fragment or '\\' in url:
            raise ValueError('Each connector operation requires a fixed HTTPS URL without credentials or a fragment.')
        _ = parsed.port  # Reject invalid ports during discovery.
    routes = tuple(sorted(urls.items()))
    return Connector(identifier, raw['title'], raw['description'], raw['documentation'], True, True, routes,
                     raw.get('agent_instructions', ''), tuple(sorted(instructions.items())))


def _read(path, identifier):
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return _parse(raw, identifier), raw
    except json.JSONDecodeError as exc:
        if path.name == 'connector.json':
            detail = f'Fix the JSON in its connector.json file at line {exc.lineno}, column {exc.colno}.'
        else:
            detail = f'The saved connector configuration has invalid JSON at line {exc.lineno}, column {exc.colno}.'
        raise ValueError(f'Cannot load {identifier}. {detail}') from exc
    except (ValueError, OSError) as exc:
        raise ValueError(f'Cannot load connector {identifier}: {exc}') from exc


def catalog_with_errors(config=None, state=None):
    available = {}
    planned = []
    errors = []
    for identifier in BUILTINS:
        raw = json.loads((BUILTIN_DIR / identifier / 'connector.json').read_text())
        plugin = Connector(external=False, **raw)
        if plugin.available:
            available[identifier] = plugin
        else:
            planned.append(plugin)
    external = {}
    active = validate_ids(config.get('connector_ids', [])) if config is not None and state is not None else []
    for path in sorted(directory().glob('*/connector.json')):
        if path.parent.name in active:
            continue
        try:
            plugin, _ = _read(path, path.parent.name)
            external[plugin.id] = plugin
        except ValueError as exc:
            errors.append(str(exc))
    for identifier in active:
        try:
            external[identifier] = _read(Path(state) / 'connector-manifests' / (identifier + '.json'), identifier)[0]
        except ValueError as exc:
            # A saved grant can still be revoked when its manifest is unreadable.
            external[identifier] = Connector(identifier, identifier, 'The saved connector file could not be read.',
                                             '', True, True)
            errors.append(str(exc))
    return (*available.values(), *(external[i] for i in sorted(external)), *planned), tuple(errors)


def catalog():
    return catalog_with_errors()[0]


def validate_ids(ids):
    if not isinstance(ids, list) or any(not _slug(i) or i in BUILTINS for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Select unique installed external connector identifiers.')
    return ids


def snapshot(ids, state, *, refresh=False):
    """Freeze selected routes. Reopening a sandbox never reads updated workstation plugins."""
    from ..sandbox_options import write_json
    for identifier in validate_ids(ids):
        path = Path(state) / 'connector-manifests' / (identifier + '.json')
        if path.exists() and not refresh:
            _read(path, identifier)
            continue
        _, raw = _read(directory() / identifier / 'connector.json', identifier)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        write_json(path, raw)


def saved(config, state):
    return tuple(_read(Path(state) / 'connector-manifests' / (i + '.json'), i)[0]
                 for i in validate_ids(config.get('connector_ids', [])))


def for_sandbox(config, state):
    return catalog_with_errors(config, state)[0]


SSH_GIT_LABEL = json.loads((BUILTIN_DIR / 'ssh/connector.json').read_text())['title']

from .runtime import client_socket, configure_running, down, has_token, prepare, require_tokens, store_token, up, worker_name
