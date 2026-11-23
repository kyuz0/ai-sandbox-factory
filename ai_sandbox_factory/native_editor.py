"""Install pinned native coding-agent extensions in the untrusted editor container."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import tarfile
import urllib.request
import zipfile

from . import browser_editor, primary_workspace, sandbox_extensions

CATALOG = Path(__file__).parent / 'containers/editor/extensions.json'
CLAUDE_MODE_SOURCE = 'o=n||r;if(o==="bypassPermissions"'
CLAUDE_MODE_REPLACEMENT = 'o=n||r||"auto";if(o==="bypassPermissions"'


def prepare_claude(extension, automatic=True):
    """Keep explicit native choices; use the sandbox’s default in a new editor home.

    Claude's process-wrapper path otherwise defaults to Manual even when the
    private CLI starts in Auto. Its setting schema does not accept Auto. This
    pinned adjustment changes only the fallback after the user's saved choice.
    """
    source = extension / 'extension.js'
    content = source.read_text()
    if content.count(CLAUDE_MODE_SOURCE) != 1:
        raise ValueError('The Claude extension changed its approval settings. Update the integration before installing it.')
    source.write_text(content.replace(CLAUDE_MODE_SOURCE, CLAUDE_MODE_REPLACEMENT if automatic else CLAUDE_MODE_SOURCE))


def specification(agent):
    entries = json.loads(CATALOG.read_text())
    if agent not in entries:
        raise ValueError('This coding agent currently opens in the terminal.')
    return entries[agent]


def install(config, container, arch):
    from . import sandbox
    selected = primary_workspace.agent(config)
    spec = specification(selected)
    from .sandbox_options import options
    automatic = options(config.get('options'))['native_approval'] == 'automatic'
    platform = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64', 'arm64': 'linux-arm64'}[arch]
    package = spec['platforms'][platform]
    identity = f'{spec["publisher"]}.{spec["name"]}-{spec["version"]}'
    destination = '/opt/editor-extensions/' + identity
    marker = destination + '/.factory-sha256'
    command = {'codex': 'chatgpt.openSidebar', 'claude': 'claude-vscode.sidebar.open',
               'agy': 'workbench.view.extension.antigravity-sidebar'}[selected]
    helper_source = (
        'const vscode = require("vscode"); exports.activate = async () => {'
        f'await vscode.extensions.getExtension({json.dumps(spec["publisher"] + "." + spec["name"])}).activate();'
        f'await vscode.commands.executeCommand({json.dumps(command)}); }};')
    wrapper = '#!/bin/sh\nexec python3 /opt/primary/editor_client.py "$@"\n'
    installed_digest = hashlib.sha256((package['sha256'] + helper_source + wrapper +
        ((CLAUDE_MODE_REPLACEMENT if automatic else CLAUDE_MODE_SOURCE) if selected == 'claude' else '')).encode()).hexdigest()
    engine = config['engine']
    existing = sandbox.run([engine, 'exec', container, 'cat', marker], capture=True,
                           check=False, stderr=subprocess.DEVNULL)
    if existing.returncode == 0 and existing.stdout.strip() == installed_digest:
        return
    cache = sandbox_extensions.cache_root().parent / 'native-editors'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = cache / (package['sha256'] + '.vsix')
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with urllib.request.urlopen(package['url']) as response:
                    shutil.copyfileobj(response, stream)
                stream.close()
                browser_editor.verify_archive(temporary, package['sha256'])
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    browser_editor.verify_archive(archive, package['sha256'])
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        root = Path(temporary)
        extension = root / identity
        extension.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                path = PurePosixPath(item.filename)
                if not path.parts or path.parts[0] != 'extension':
                    continue
                if path.is_absolute() or '..' in path.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Invalid extension archive path')
                target = extension.joinpath(*path.parts[1:])
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(((item.external_attr >> 16) & 0o777) or 0o644)
        if selected == 'claude':
            prepare_claude(extension, automatic)
        helper = root / 'pi-sandbox-factory.native-editor-1.0.0'
        helper.mkdir()
        (helper / 'package.json').write_text(json.dumps({
            'publisher': 'pi-sandbox-factory', 'name': 'native-editor', 'version': '1.0.0',
            'engines': {'vscode': '^1.90.0'}, 'main': './index.cjs', 'activationEvents': ['onStartupFinished']}))
        (helper / 'index.cjs').write_text(helper_source)
        sandbox.run([engine, 'exec', '--user', '0', container, 'mkdir', '-p', '/opt/editor-extensions'])
        bundle = root / 'extensions.tar'
        with tarfile.open(bundle, 'w') as output:
            for path in (extension, helper):
                output.add(path, arcname=path.name)
        with bundle.open('rb') as source:
            sandbox.run([engine, 'exec', '-i', '--user', '0', container, 'tar', '-xf', '-',
                         '--no-same-owner', '-C', '/opt/editor-extensions'], stdin=source)
    sandbox.run([engine, 'exec', '-i', '--user', '0', container, 'tee', '/opt/primary/editor-cli'],
                input=wrapper, capture=True)
    sandbox.run([engine, 'exec', '--user', '0', container, 'chmod', '755', '/opt/primary/editor-cli'])
    sandbox.run([engine, 'exec', '-i', '--user', '0', container, 'tee', marker],
                input=installed_digest, capture=True)
    # Retire only this managed extension's older versions after installation succeeds.
    retire = ('from pathlib import Path; import shutil, sys; '
              'root=Path("/opt/editor-extensions"); '
              '[shutil.rmtree(p) for p in root.iterdir() '
              'if p.name.startswith(sys.argv[1]) and p.name != sys.argv[2] and not p.is_symlink()]')
    sandbox.run([engine, 'exec', '--user', '0', container, 'python3', '-c', retire,
                 spec['publisher'] + '.' + spec['name'] + '-', identity])


def settings(agent):
    if agent == 'codex':
        return {'chatgpt.cliExecutable': '/opt/primary/editor-cli'}
    if agent == 'claude':
        return {'claudeCode.claudeProcessWrapper': '/opt/primary/editor-cli',
                'claudeCode.disableLoginPrompt': True}
    return {}
