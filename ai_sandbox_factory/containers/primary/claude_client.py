"""Run official Claude Code in its workspace with companion-owned authentication."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from urllib.request import ProxyHandler, Request, build_opener


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure(home, settings):
    """Add managed defaults without disabling native settings or customizations."""
    marker = home / '.factory-settings.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    preferences = home / 'settings.json'
    native = json.loads(preferences.read_text()) if preferences.exists() else {}
    if previous.get('native_approval') != settings.get('native_approval') or not marker.exists():
        native.setdefault('permissions', {})['defaultMode'] = (
            'auto' if settings.get('native_approval', 'automatic') == 'automatic' else 'default')
        atomic_write(preferences, json.dumps(native).encode())
    # Factory owns this single native tool rule; all unrelated user rules and
    # WebFetch remain unchanged. A disabled search choice wins over native
    # allow rules because Claude's deny rules take precedence.
    permissions = native.setdefault('permissions', {})
    denied = [rule for rule in permissions.get('deny', []) if rule != 'WebSearch']
    if not settings.get('claude_web_search', False):
        denied.append('WebSearch')
    if denied:
        permissions['deny'] = denied
    else:
        permissions.pop('deny', None)
    atomic_write(preferences, json.dumps(native).encode())
    # Native trust records accept only the configured project. Project settings,
    # skills and hooks execute in this workspace, never beside real credentials.
    # CLAUDE_CONFIG_DIR places the native global config inside that directory,
    # unlike the unset-variable default at ~/.claude.json.
    path = home / '.claude.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    # Native Claude resolves a starting subfolder to its Git project root.
    # /workspace is the user's selected project mount, so trust both it and
    # the selected starting directory without changing any filesystem grants.
    for directory in ('/workspace', settings['working_directory']):
        config.setdefault('projects', {}).setdefault(directory, {})['hasTrustDialogAccepted'] = True
    config['hasCompletedOnboarding'] = True
    atomic_write(path, json.dumps(config).encode())
    mcp_path = path
    if previous.get('browser') != settings.get('browser') or not marker.exists():
        config = json.loads(mcp_path.read_text())
        servers = config.setdefault('mcpServers', {})
        if settings.get('browser'):
            servers['factory_workspace'] = {'type': 'stdio', 'command': '/usr/bin/socat',
                'args': ['STDIO', 'UNIX-CONNECT:/run/primary-workspace/tools.sock']}
        else:
            servers.pop('factory_workspace', None)
        atomic_write(mcp_path, json.dumps(config).encode())
    atomic_write(marker, json.dumps(settings).encode())


def instructions(home, context):
    path = home / 'CLAUDE.md'
    start, end = '<!-- Factory managed instructions -->', '<!-- End Factory managed instructions -->'
    content = path.read_text() if path.exists() else ''
    block = start + '\n' + context.rstrip() + '\n' + end
    if start in content and end in content:
        before, remainder = content.split(start, 1)
        _, after = remainder.split(end, 1)
        content = before + block + after
    else:
        content = content.rstrip() + ('\n\n' if content.strip() else '') + block + '\n'
    atomic_write(path, content.encode())


def prepare():
    home = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (home / '.factory-setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ca = home / 'factory-ca.pem'
        atomic_write(ca, Path('/etc/ssl/certs/ca-certificates.crt').read_bytes() +
                     Path('/run/primary-editor/claude-ca.pem').read_bytes())
        proxy = 'http://127.0.0.1:3130'
        opener = build_opener(ProxyHandler({'http': proxy}))
        with opener.open('http://factory-claude.internal/auth') as response:
            state = json.load(response)
        generation = home / '.factory-auth-generation'
        if state['auth'] and (not generation.exists() or generation.read_text() != state['generation']):
            atomic_write(home / '.credentials.json', json.dumps(state['auth']).encode())
            atomic_write(generation, state['generation'].encode())
        settings = json.loads(Path('/run/primary-runtime/settings.json').read_text())
        configure(home, settings)
        instructions(home, Path('/run/primary-runtime/context.md').read_text())
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = proxy
    for key in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS'):
        os.environ[key] = str(ca)
    os.environ['CLAUDE_CONFIG_DIR'] = str(home)
    return settings


def arguments(settings, supplied, context):
    # The extension prefixes wrapper invocations with its bundled executable.
    # Execute only the binary installed in this image, retaining all its flags.
    if supplied and Path(supplied[0]).is_absolute() and Path(supplied[0]).name == 'claude':
        supplied = supplied[1:]
    return supplied


def main():
    os.umask(0o077)
    settings = prepare()
    if sys.argv[1:] == ['--factory-prepare']:
        return
    args = arguments(settings, sys.argv[1:], '')
    if args[-2:] == ['auth', 'logout']:
        result = subprocess.run(['/usr/local/bin/factory-agent', *args], check=False)
        if result.returncode == 0:
            opener = build_opener(ProxyHandler({'http': 'http://127.0.0.1:3130'}))
            with opener.open(Request('http://factory-claude.internal/logout', method='POST')) as response:
                json.load(response)
        raise SystemExit(result.returncode)
    os.execv('/usr/local/bin/factory-agent', ['claude', *args])


if __name__ == '__main__':
    main()
