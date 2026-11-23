"""Run official Antigravity clients with a workspace-only placeholder keyring."""
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.request import ProxyHandler, build_opener
from urllib.error import HTTPError, URLError

BUS = Path('/tmp/factory-agy-session/bus')
KEYRING = ['service', 'gemini', 'username', 'antigravity']


def read_configuration(path, *, optional=False):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        if optional:
            return {}
        raise ValueError(f'Cannot read Antigravity configuration {path}: the file does not exist. Reopen the sandbox from Factory to recreate its managed settings.') from None
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read Antigravity configuration {path}: {type(error).__name__}: {error}. Fix that file and reopen Antigravity.') from error
    if not isinstance(value, dict):
        raise ValueError(f'Cannot read Antigravity configuration {path}: expected a JSON object. Fix that file and reopen Antigravity.')
    return value


def public_auth(opener):
    endpoint = 'http://factory-agy.internal/auth'
    try:
        with opener.open(endpoint) as response:
            state = json.load(response)
    except HTTPError as error:
        try:
            detail = error.read().decode('utf-8', errors='replace')
        except (OSError, HTTPException) as read_error:
            partial = getattr(read_error, 'partial', b'')
            detail = (partial.decode('utf-8', errors='replace') if isinstance(partial, bytes) else '')
            detail += f' [Error body incomplete: {type(read_error).__name__}: {read_error}]'
        finally:
            error.close()
        raise OSError(f'Cannot prepare Antigravity sign-in from {endpoint}: HTTP {error.code}: {detail}. Check the credential companion logs and retry.') from error
    except (URLError, OSError, ValueError) as error:
        raise OSError(f'Cannot prepare Antigravity sign-in from {endpoint}: {type(error).__name__}: {error}. Check the credential companion connection and retry.') from error
    if (not isinstance(state, dict) or not isinstance(state.get('generation'), str)
            or not (state.get('auth') is None or isinstance(state.get('auth'), dict))):
        raise ValueError(f'Cannot prepare Antigravity sign-in from {endpoint}: expected an auth object or null and a string generation. Check the credential companion logs and retry.')
    return state


def atomic_write(path, data):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure_search(home):
    """Use Antigravity's native hook without replacing other customizations."""
    path = home / 'config/hooks.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    hooks = read_configuration(path, optional=True)
    hooks['factory-native-web-search'] = {'PreToolUse': [{'matcher': '^search_web$', 'hooks': [{
        'type': 'command', 'command': '/usr/bin/python3 -I /opt/primary/agy_client.py --factory-search-policy'}]}]}
    atomic_write(path, json.dumps(hooks, indent=2).encode())


def configure_browser(home, enabled):
    path = home / 'config/mcp_config.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    config = read_configuration(path, optional=True)
    if not isinstance(config.get('mcpServers', {}), dict):
        raise ValueError(f'Cannot configure the browser tool: {path} must contain a JSON object with an mcpServers object. Fix that file and reopen Antigravity.')
    servers = config.setdefault('mcpServers', {})
    if enabled:
        servers['factory_workspace'] = {'command': '/usr/bin/socat',
            'args': ['STDIO', 'UNIX-CONNECT:/run/primary-workspace/tools.sock']}
    else:
        servers.pop('factory_workspace', None)
    atomic_write(path, json.dumps(config, indent=2).encode())


def configure_hub_permissions(home, settings):
    """Apply the native App preset separately from CLI session settings."""
    selection = {'native_approval': settings.get('native_approval', 'automatic'),
                 'browser': settings.get('browser', False)}
    marker = home / '.factory-hub-settings.json'
    previous = read_configuration(marker, optional=True)
    if previous.get('selection') == selection:
        return
    path = home / 'config/config.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    config = read_configuration(path, optional=True)
    user = config.setdefault('userSettings', {})
    if not isinstance(user, dict):
        raise ValueError(f'Cannot configure native Antigravity approvals: {path} userSettings must be a JSON object. Fix that field and reopen Antigravity.')
    grants = user.setdefault('globalPermissionGrants', {})
    if not isinstance(grants, dict) or not isinstance(grants.get('allow', []), list):
        raise ValueError(f'Cannot configure native Antigravity approvals: {path} globalPermissionGrants must contain an allow list. Fix that field and reopen Antigravity.')
    allowed = [rule for rule in grants.get('allow', []) if rule not in previous.get('managed_allow', [])]
    desired = []
    if selection['native_approval'] == 'automatic':
        desired = ['command(*)', 'read_file(*)', 'write_file(*)']
        if selection['browser']:
            desired.append('mcp(factory_workspace/browser)')
    managed = [rule for rule in desired if rule not in allowed]
    grants['allow'] = allowed + managed
    if previous.get('selection', {}).get('native_approval') != selection['native_approval']:
        # These are the pinned native App's Request Review preset values.
        # Explicit configured grants retain the native engine's precedence.
        user.update({'permissionPreset': 'AGENT_PERMISSION_PRESET_REQUEST_REVIEW',
                     'cascadeAutoExecutionPolicy': 'CASCADE_COMMANDS_AUTO_EXECUTION_OFF',
                     'enableTerminalSandbox': False, 'nonWorkspaceFileAccessPolicy': 'AGENT_SETTING_POLICY_ASK'})
    atomic_write(path, json.dumps(config, indent=2).encode())
    atomic_write(marker, json.dumps({'selection': selection, 'managed_allow': managed}).encode())


def native_preferences(settings):
    automatic = settings.get('native_approval', 'automatic') == 'automatic'
    # The workspace container supplies process, filesystem and network isolation.
    # Auto mode forces another namespace sandbox, which is unavailable on some
    # supported container hosts. Native permissions still govern tool approval.
    result = {'trustedWorkspaces': [settings['working_directory']], 'enableTerminalSandbox': False,
              'agentMode': 'accept-edits' if automatic else '', 'toolPermission': 'request-review'}
    if automatic:
        allowed = ['command(*)', 'read_file(*)', 'write_file(*)']
        if settings.get('browser', False):
            allowed.append('mcp(factory_workspace/browser)')
        result['permissions'] = {'allow': allowed}
    return result


def search_policy(request, settings):
    if request.get('toolCall', {}).get('name') != 'search_web' or settings.get('agy_web_search', False):
        return {'decision': 'allow'}
    return {'decision': 'deny', 'reason': 'Native Web Search is off in this sandbox. Enable Web Search in Factory Settings to use search_web.'}


def keyring():
    BUS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + str(BUS)
    connected = False
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(str(BUS))
            connected = True
        except (FileNotFoundError, ConnectionRefusedError):
            BUS.unlink(missing_ok=True)
    if not connected:
        subprocess.run(['dbus-daemon', '--session', '--address=' + os.environ['DBUS_SESSION_BUS_ADDRESS'],
                        '--fork', '--nopidfile'], check=True)
    owner = subprocess.run(['dbus-send', '--session', '--dest=org.freedesktop.DBus', '--type=method_call',
        '--print-reply', '/org/freedesktop/DBus', 'org.freedesktop.DBus.NameHasOwner',
        'string:org.freedesktop.secrets'], capture_output=True, text=True)
    if owner.returncode:
        detail = owner.stderr.strip() or owner.stdout.strip() or '(no diagnostic output)'
        raise OSError(f'Cannot check the Antigravity workspace keyring at {BUS}: dbus-send exited {owner.returncode}: {detail}. Check this D-Bus session and reopen Antigravity.')
    if 'boolean true' not in owner.stdout:
        subprocess.run(['gnome-keyring-daemon', '--unlock', '--components=secrets',
                        '--control-directory=' + str(BUS.parent / 'keyring')], input=b'\n', check=True,
                       stdout=subprocess.DEVNULL)


def prepare():
    home = Path.home() / '.gemini'
    home.mkdir(mode=0o700, exist_ok=True)
    with (home / '.factory-setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        keyring()
        configure_search(home)
        settings = read_configuration(Path('/run/primary-runtime/settings.json'))
        configure_browser(home, settings.get('browser', False))
        configure_hub_permissions(home, settings)
        ca = home / 'factory-ca.pem'
        atomic_write(ca, Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()
                     + Path('/run/primary-editor/agy-ca.pem').read_bytes())
        proxy = 'http://127.0.0.1:3130'
        opener = build_opener(ProxyHandler({'http': proxy}))
        state = public_auth(opener)
        generation = home / '.factory-auth-generation'
        if state['auth'] and (not generation.exists() or generation.read_text() != state['generation']):
            subprocess.run(['secret-tool', 'store', '--label=Antigravity CLI', *KEYRING],
                           input=json.dumps(state['auth']).encode(), check=True)
            atomic_write(generation, state['generation'].encode())
        binary = home / 'bin/agy'
        if not binary.exists():
            binary.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(binary, b'#!/bin/sh\nexec /usr/local/bin/agy "$@"\n')
            binary.chmod(0o755)
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[name] = proxy
    for name in ('AGY_CA_CERT', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS'):
        os.environ[name] = str(ca)
    return home


def main():
    os.umask(0o077)
    if sys.argv[1:] == ['--factory-search-policy']:
        settings = read_configuration(Path('/run/primary-runtime/settings.json'))
        print(json.dumps(search_policy(json.load(sys.stdin), settings)))
        return
    home = prepare()
    args = sys.argv[1:]
    if args == ['--factory-prepare']:
        return
    if args == ['--factory-login']:
        subprocess.run(['secret-tool', 'clear', *KEYRING], check=False)
        os.environ['SSH_CONNECTION'] = '127.0.0.1 0 127.0.0.1 0'
        args = []
    settings = read_configuration(Path('/run/primary-runtime/settings.json'))
    context = Path('/run/primary-runtime/context.md').read_text()
    target = home / 'AGENTS.md'
    begin, end = '<!-- factory-context -->', '<!-- /factory-context -->'
    content = target.read_text() if target.exists() else ''
    if begin in content and end in content:
        start = content.index(begin)
        finish = content.index(end, start) + len(end)
        content = content[:start] + content[finish:]
    atomic_write(target, (content.rstrip() + '\n\n' + begin + '\n' + context + '\n' + end + '\n').encode())
    os.execv('/usr/local/bin/factory-agent', ['agy', '--settings', json.dumps(native_preferences(settings)), *args])


if __name__ == '__main__':
    main()
