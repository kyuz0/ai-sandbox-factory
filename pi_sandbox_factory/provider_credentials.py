"""Named workstation credentials and their sandbox assignments."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from .sandbox_options import write_json

SERVICE = 'pi-sandbox-factory:model-provider'


def index_path():
    parent = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    return parent / 'pi-sandbox-factory' / 'provider-credentials.json'


def os_store():
    try:
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith('linux'):
            from keyring.backends.SecretService import Keyring
        else:
            raise RuntimeError()
        return Keyring()
    except (ImportError, RuntimeError, OSError):
        raise ValueError('The system credential store is unavailable. Enter a key for this sandbox.') from None


def scope(provider, route):
    """Bind a saved key to a provider, exact endpoint and authentication scheme."""
    if not route or route['auth']['type'] not in ('bearer', 'header'):
        return None
    if not route.get('credentialCheck') and not any(
            operation['method'] == 'GET' and operation['path'].endswith('/models')
            for operation in route.get('operations', ())):
        return None
    endpoint = urlsplit(route['origin'])
    if endpoint.scheme not in ('https', 'http') or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError('Invalid credential endpoint.')
    host = endpoint.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    port = endpoint.port
    default_port = 443 if endpoint.scheme == 'https' else 80
    authority = f'{host}:{port}' if port and port != default_port else host
    path = route.get('basePath', '').rstrip('/')
    auth = route['auth']
    return {'provider': provider, 'endpoint': f'{endpoint.scheme}://{authority}{path}',
            'auth': auth.get('header', auth['type'])}


def entries(binding=None):
    path = index_path()
    try:
        records = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError('Could not read saved model credentials.') from None
    if not isinstance(records, dict) or any(not re.fullmatch(r'[0-9a-f]{32}', ref)
            or not isinstance(row, dict) or not isinstance(row.get('label'), str)
            or not isinstance(row.get('scope'), dict) for ref, row in records.items()):
        raise ValueError('Could not read saved model credentials.')
    return {ref: row for ref, row in records.items() if binding is None or row['scope'] == binding}


@contextmanager
def locked_index():
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def read(ref, binding):
    with locked_index():
        if binding is None or ref not in entries(binding):
            raise ValueError('This saved key is unavailable for the selected provider and endpoint.')
        store = os_store()
        try:
            key = store.get_password(SERVICE, ref)
        except Exception:
            raise ValueError('Could not read the key from the system credential store.') from None
    if not key:
        raise ValueError('The saved key is unavailable. Unlock the credential store or enter a new key.')
    return key


def save(binding, label, key, *, replace=None):
    label = label.strip()
    if not binding or not label or len(label) > 64 or any(not character.isprintable() for character in label):
        raise ValueError('Enter a name for the saved key (up to 64 characters).')
    validate_key(key)
    with locked_index() as path:
        records = entries()
        same_name = next((ref for ref, row in records.items()
                          if ref != replace and row['scope'] == binding
                          and row['label'].casefold() == label.casefold()), None)
        if same_name:
            store = os_store()
            try:
                same_key = store.get_password(SERVICE, same_name) == key
            except Exception:
                raise ValueError('Could not read the key from the system credential store.') from None
            if same_key and records[same_name]['label'] == label:
                return same_name
            raise ValueError('A saved key already uses this name. Choose another name or replace it.')
        if replace and (replace not in records or records[replace]['scope'] != binding):
            raise ValueError('This saved key is unavailable for the selected provider and endpoint.')
        ref = uuid4().hex
        store = os_store()
        try:
            store.set_password(SERVICE, ref, key)
        except Exception:
            raise ValueError('Could not save the key. Unlock the system credential store or clear Remember on this computer.') from None
        records[ref] = {'label': label, 'scope': binding}
        if replace:
            records.pop(replace)
        try:
            write_json(path, records)
        except OSError:
            try:
                store.delete_password(SERVICE, ref)
            except Exception:
                pass
            raise ValueError('Could not save the model credential name.') from None
        if replace:
            try:
                store.delete_password(SERVICE, replace)
            except Exception:
                raise ValueError('The replacement was saved. The system store could not remove the old entry.') from None
    return ref


def forget(ref):
    with locked_index() as path:
        records = entries()
        if ref not in records:
            return
        store = os_store()
        try:
            if store.get_password(SERVICE, ref) is not None:
                store.delete_password(SERVICE, ref)
        except Exception:
            raise ValueError('Could not remove the key from the system credential store.') from None
        records.pop(ref)
        write_json(path, records)


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[!-~]{1,16384}', key):
        raise ValueError('Enter a single-line API key.')
    return key


def assign(config, state, provider, *, key='', ref=None, label='', replace=None):
    from . import inference
    route = inference.routes(config).get(provider)
    binding = scope('local' if route and route.get('local') else provider, route)
    if not binding:
        raise ValueError('This provider uses a different credential flow.')
    status = inference.credentials(config, state, {'status': True})
    if 'assignments' not in status:
        inference.up(config, state)
        status = inference.credentials(config, state, {'status': True})
        if 'assignments' not in status:
            raise ValueError('The inference service does not support saved key assignments.')
    if ref:
        if key or label or replace:
            raise ValueError('Choose a saved key or enter a new one.')
        key = read(ref, binding)
        label = entries(binding).get(ref, {}).get('label')
        if not label:
            raise ValueError('The saved key changed. Select it again.')
    key = validate_key(key)
    origin = urlsplit(binding['endpoint'])
    payload = {'id': provider, 'origin': f'{origin.scheme}://{origin.netloc}',
               'auth': route['auth']['type'], 'authHeader': route['auth'].get('header'),
               'basePath': route.get('basePath', ''), 'credentials': {'key': key}}
    checked = inference.credentials(config, state, {**payload, 'check': True})['status']
    if checked != 'accepted':
        raise ValueError('The provider rejected the API key.' if checked == 'rejected'
                         else 'Could not verify the API key. Check the provider connection and retry.')
    if label and not ref:
        ref = save(binding, label, key, replace=replace)
    assignment = {'ref': ref, 'label': label} if ref else None
    payload['assignment'] = assignment
    inference.credentials(config, state, payload)
    return assignment
