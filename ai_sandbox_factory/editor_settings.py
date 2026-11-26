"""Host-side editor preferences, independent of container images and credentials."""
import re
import ssl
from argon2 import PasswordHasher
import ipaddress
import json
from pathlib import Path

from .sandbox_options import write_json
from . import host_platform, activity

MODES = {'browser': 'Browser editor', 'pi': 'Pi CLI'}
DEFAULTS = {'mode': 'browser', 'address': '', 'port': 0, 'password_hash': '', 'certificate': '', 'private_key': '', 'allow_uploads': False}


def modes(agent='pi'):
    if agent == 'pi':
        return MODES
    from .primary_workspace import AGENTS
    terminal = {'pi': AGENTS[agent] + ' CLI'}
    return {'browser': MODES['browser'], **terminal} if agent in ('codex', 'claude', 'agy') else terminal


def default_for_agent(agent, engine='podman'):
    return default_mode(engine) if agent == 'pi' else (
        'browser' if agent in ('codex', 'claude', 'agy') and default_mode(engine) == 'browser' else 'pi')


def validate(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('Editor settings must be a JSON object.')
    if unknown := set(value) - DEFAULTS.keys():
        raise ValueError('Unknown editor settings: ' + ', '.join(sorted(unknown)) +
                         '. Choose from ' + ', '.join(DEFAULTS) + '.')
    result = {**DEFAULTS, **value}
    if result['mode'] == 'cli':
        result['mode'] = 'pi'
    if not isinstance(result['allow_uploads'], bool):
        raise ValueError('Allow file uploads must be true or false')
    if result['mode'] not in MODES:
        raise ValueError(f'Unknown editor mode {result["mode"]!r}. Choose browser or cli (pi is an alias for cli).')
    try:
        address = ipaddress.ip_address(result['address'] or '127.0.0.1')
    except ValueError:
        raise ValueError('Enter an IP address assigned to this computer, such as 127.0.0.1') from None
    if address.is_unspecified or address.is_multicast:
        raise ValueError('Choose a specific interface IP address instead of a wildcard address')
    if (host_platform.is_mac() or host_platform.is_wsl()) and address.is_loopback and str(address) not in ('127.0.0.1', '::1'):
        raise ValueError('Use automatic addressing or 127.0.0.1 on macOS and WSL.')
    result['address'] = str(address) if result['address'] else ''
    if type(result['port']) is not int or not 0 <= result['port'] <= 65535:
        raise ValueError('Port must be 0 (automatic) or a number from 1 to 65535')
    digest = result['password_hash']
    if not isinstance(digest, str) or (digest and not re.fullmatch(r'\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+', digest)):
        raise ValueError('Invalid saved editor password')
    if result['mode'] == 'browser' and result['address'] and not digest:
        raise ValueError('Set a password when choosing a custom listen address')
    for key in ('certificate', 'private_key'):
        if not isinstance(result[key], str):
            raise ValueError('HTTPS certificate and key must be file paths')
    if bool(result['certificate']) != bool(result['private_key']):
        raise ValueError('Provide both the HTTPS certificate and private key')
    if result['mode'] == 'browser' and not address.is_loopback and not result['certificate']:
        raise ValueError('HTTPS certificate and key are required on a non-local interface')
    return result


def tls_context(value):
    if not value['certificate']:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    key = Path(value['private_key']).expanduser()

    def encrypted():
        raise ValueError('Use an unencrypted HTTPS key; the background editor cannot prompt for its passphrase')

    try:
        context.load_cert_chain(str(Path(value['certificate']).expanduser()), str(key), password=encrypted)
    except OSError as error:
        raise ValueError(f'Could not load the editor HTTPS certificate and key. {error}') from error
    return context


def saved_choice(value):
    """Read saved preferences, including retired client choices."""
    if isinstance(value, dict):
        value = dict(value)
        value.pop('frostpi_uploads', None)
        value.pop('frostpi_downloads', None)
        if value.get('mode') == 'frostpi':
            value['mode'] = 'browser'
        elif value.get('mode') == 'desktop':
            value['mode'] = 'pi'
    return validate(value)


def load(state):
    path = Path(state) / 'editor.json'
    if path.exists():
        return saved_choice(json.loads(path.read_text()))
    return validate()


def save(state, value, *, record_edit=True):
    value = validate(value)
    tls_context(value)
    previous = load(state)
    path = Path(state) / 'editor.json'
    existed = path.exists()
    write_json(path, value)
    try:
        if record_edit and previous != value and (Path(state) / 'config.json').exists():
            activity.record(state, 'edited')
    except Exception as error:
        try:
            if existed:
                write_json(path, previous)
            else:
                path.unlink()
        except OSError as rollback:
            raise ValueError(f'Editor settings could not be recorded: {error}. Restoring the previous settings also failed: {rollback}. Check Editor settings before reopening.') from error
        raise
    return value


def copy_choice(value):
    value = saved_choice(value)
    return {**DEFAULTS, 'mode': value['mode'], 'allow_uploads': value['allow_uploads']}


def password_hash(password):
    if password and len(password) < 12:
        raise ValueError('Use at least 12 characters, or Generate a password')
    return PasswordHasher().hash(password) if password else ''


def connection(state, value):
    """Keep each sandbox on its own loopback origin, including across restarts."""
    path = Path(state) / 'editor-address.json'
    saved = json.loads(path.read_text()) if path.exists() else {}
    address = value['address'] or saved.get('automatic_address')
    if not address:
        address = host_platform.automatic_address()
    port = value['port'] or (saved.get('port', 0) if saved.get('address') == address else 0)
    return address, port


def remember_connection(state, value, address, port):
    path = Path(state) / 'editor-address.json'
    saved = json.loads(path.read_text()) if path.exists() else {}
    automatic = saved.get('automatic_address', '') if value['address'] else address
    write_json(path, {'automatic_address': automatic, 'address': address, 'port': port})


def authority(address, port):
    return f'[{address}]:{port}' if ':' in address else f'{address}:{port}'


def default_mode(engine='podman'):
    import platform
    import os
    return 'browser' if engine in ('podman', 'auto') and platform.system() in ('Linux', 'Darwin') and platform.machine() in ('x86_64', 'aarch64', 'arm64') and os.geteuid() else 'pi'
