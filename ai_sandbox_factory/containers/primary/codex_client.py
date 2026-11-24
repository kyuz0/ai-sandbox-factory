"""Start the official Codex client in the workspace with protected authentication."""
import json
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import tomlkit
from urllib.request import ProxyHandler, build_opener


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure(home, settings):
    """Apply changed Factory choices without resetting native client settings."""
    path = home / 'config.toml'
    marker = home / '.factory-settings.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    config = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    if previous == settings and path.exists():
        return
    if not marker.exists():
        config.setdefault('cli_auth_credentials_store', 'file')
        config.setdefault('approval_policy', 'on-request')
        config.setdefault('sandbox_mode', 'workspace-write')
    if previous.get('native_approval') != settings.get('native_approval') or not marker.exists():
        config['approvals_reviewer'] = 'auto_review' if settings.get('native_approval', 'automatic') == 'automatic' else 'user'
    if (previous.get('codex_web_search', False) != settings.get('codex_web_search', False)
            or not marker.exists()):
        config['web_search'] = 'cached' if settings.get('codex_web_search', False) else 'disabled'
    config.setdefault('projects', {}).setdefault(settings['working_directory'], {})['trust_level'] = 'trusted'
    if (previous.get('write_roots') != settings.get('write_roots')
            or previous.get('working_directory') != settings['working_directory']):
        # Native workspace-write already permits the working directory. Listing
        # it again makes the native editor classify the default as Custom.
        config.setdefault('sandbox_workspace_write', {})['writable_roots'] = [
            root for root in settings.get('write_roots', ['/workspace'])
            if root != settings['working_directory']]
    if previous.get('browser') != settings.get('browser'):
        servers = config.setdefault('mcp_servers', {})
        if settings.get('browser'):
            servers['factory_workspace'] = {'command': '/usr/bin/socat',
                'args': ['STDIO', 'UNIX-CONNECT:/run/primary-workspace/tools.sock']}
        else:
            servers.pop('factory_workspace', None)
    atomic_write(path, tomlkit.dumps(config).encode())
    atomic_write(marker, json.dumps(settings).encode())


def prepare():
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    home.mkdir(mode=0o700, exist_ok=True)
    # CLI and editor can start together. Only their initial configuration and
    # placeholder seeding are serialized, never the native client processes.
    with (home / '.factory-setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return prepare_locked(home)


def prepare_locked(home):
    public = Path('/run/primary-editor/codex-ca.pem')
    ca = home / 'factory-ca.pem'
    atomic_write(ca, Path('/etc/ssl/certs/ca-certificates.crt').read_bytes() + public.read_bytes())
    proxy = 'http://127.0.0.1:3130'
    opener = build_opener(ProxyHandler({'http': proxy}))
    with opener.open('http://factory-codex.internal/auth') as response:
        state = json.load(response)
    generation = home / '.factory-auth-generation'
    if state['auth'] and (not generation.exists() or generation.read_text() != state['generation']):
        atomic_write(home / 'auth.json', json.dumps(state['auth']).encode())
        atomic_write(generation, state['generation'].encode())
    settings = json.loads(Path('/run/primary-runtime/settings.json').read_text())
    configure(home, settings)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = proxy
    for key in ('CODEX_CA_CERTIFICATE', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS'):
        os.environ[key] = str(ca)
    os.environ['CODEX_HOME'] = str(home)
    os.environ.pop('CODEX_EXEC_SERVER_URL', None)
    return settings


def main():
    os.umask(0o077)
    prepare()
    if sys.argv[1:] == ['--factory-prepare']:
        return
    context = Path('/run/primary-runtime/context.md').read_text()
    args = ['-c', 'developer_instructions=' + json.dumps(context), *sys.argv[1:]]
    # No JSON-RPC filtering or tool remapping: this is the official executable.
    os.execv('/usr/local/bin/factory-agent', ['codex', *args])


if __name__ == '__main__':
    main()
