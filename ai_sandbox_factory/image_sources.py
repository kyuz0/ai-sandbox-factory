"""Select tested Factory images without changing the local build recipes."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess

LABEL = 'Image setup'
DEFAULT = 'prebuilt'
CHOICES = (('Use prebuilt images', 'prebuilt'), ('Build images locally', 'local'))
INDEX = Path(__file__).with_name('images.lock.json')
_used = ContextVar('factory_image_artifacts', default=None)
_override = ContextVar('factory_image_source_override', default=None)


def choice(value):
    if value not in ('prebuilt', 'local'):
        raise ValueError(f'Unknown image setup {value!r}. Choose prebuilt or local.')
    return value


def mode(config):
    return choice(_override.get() or config.get('image_source', 'local'))


@contextmanager
def override(value):
    token = _override.set(choice(value) if value is not None else None)
    try:
        yield
    finally:
        _override.reset(token)


def _files(path, *, executable_bits_only=False):
    """Hash relative paths, contents and modes. Packaged recipes ignore read/write umasks."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f'Factory image input {path} is missing. Restore the saved build sources and retry.')
    files = sorted(path.rglob('*')) if path.is_dir() else [path]
    digest = hashlib.sha256()
    for file in files:
        relative = file.relative_to(path).as_posix() if path.is_dir() else file.name
        if any(part in ('.git', '__pycache__') for part in Path(relative).parts):
            continue
        if file.is_symlink():
            raise ValueError(f'Factory image input {file} is a symlink. Restore the saved build sources.')
        if file.is_file():
            digest.update(relative.encode() + b'\0')
            mode = stat.S_IMODE(file.stat().st_mode)
            digest.update(str(mode & 0o111 if executable_bits_only else mode).encode() + b'\0')
            digest.update(file.read_bytes() + b'\0')
    return digest.hexdigest()


def recipe(config, role):
    """Finite public roles only. User profiles and provider payloads stay local."""
    from . import sandbox, browser_editor, sandbox_advisors, sandbox_extensions
    root = sandbox.ROOT
    common = Path(config.get('common', {}).get('path', root / 'containers/dev'))
    assembly = Path(__file__).with_name('image_builds.py')
    extensions = Path(__file__).with_name('sandbox_extensions.py')
    identity = {'format': 1, 'role': role}
    sources = {
        'gateway': [root / 'containers/proxy'],
        'inference': [root / 'containers/inference' / name for name in
                      ('Dockerfile', 'package.json', 'package-lock.json', 'broker.mjs', 'credentials.mjs')],
        'ssh': [root / 'containers/ssh-access'],
        'aws': [root / 'containers/aws-access/Dockerfile',
                root / 'containers/aws-access/signer.py', root / 'containers/aws-access/receive.py'],
        'connector': [root / 'connectors/companion'],
        'advisor': [root / 'containers/advisor', extensions],
        'pi-common': [common, assembly, extensions],
        'pi-stock': [common, root / 'profiles/base', root / 'containers/aws-access/client',
                     root / 'containers/prebuilt', root / 'providers/provider-runner.mjs',
                     root / 'providers/provider-lock.mjs', assembly, extensions,
                     Path(__file__).with_name('model_providers.py')],
        'native-common': [root / 'containers/primary-worker', root / 'containers/primary', common / 'cli-tools', assembly],
    }
    if role.startswith('controller-') and role.removeprefix('controller-') in ('codex', 'claude', 'agy'):
        sources[role] = [root / 'containers/primary']
        identity['agent'] = role.removeprefix('controller-')
    if role not in sources:
        raise ValueError(f'Unknown Factory image role {role!r}. Use a declared public image recipe.')
    if role in ('ssh', 'advisor', 'pi-common', 'pi-stock', 'native-common') or role.startswith('controller-'):
        identity.update(uid=config['uid'], gid=config['gid'])
    if role in ('pi-common', 'pi-stock'):
        identity['pi'] = sandbox.pendant_versions(config) or (sandbox.PI_VERSION,)
    if role == 'advisor':
        identity['extension'] = sandbox_extensions.read_lock(sandbox_advisors.source_path(config))['pi-advisor']
    if role == 'pi-stock':
        identity['editor'] = [browser_editor.VERSION, browser_editor.SHA256, browser_editor.ARM64_SHA256]
    identity['sources'] = [_files(path, executable_bits_only=True) for path in sources[role]]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def index():
    try:
        value = json.loads(INDEX.read_text())
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value['images'], dict):
            raise ValueError('expected version 1 and an images object')
        return value['images']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f'Cannot read the tested Factory image list at {INDEX}: {error}. '
                         'Reinstall Factory or select Build images locally.') from error


@lru_cache(maxsize=None)
def _platform(engine, connection):
    from . import sandbox
    template = '{{.Host.Arch}}' if engine == 'podman' else '{{.Architecture}}'
    result = sandbox.run([engine, 'info', '--format', template], capture=True)
    arch = result.stdout.strip()
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(arch, arch)
    if not re.fullmatch('[a-z0-9_]+', arch):
        raise ValueError(f'Cannot select a Factory image: {engine} reported architecture {arch!r}. '
                         'Check the container engine and retry.')
    return 'linux/' + arch


def _published(config, role):
    if mode(config) == 'local':
        return None
    entries = index()
    if not entries:
        return None
    recipe_key = recipe(config, role)
    selected = entries.get(recipe_key, {})
    if not isinstance(selected, dict):
        raise ValueError(f'The tested {role} image list must map platforms to digest references. '
                         'Reinstall Factory or select Build images locally.')
    if not selected:
        return None
    platform = _platform(config['engine'], config.get('podman_connection'))
    reference = selected.get(platform)
    if reference is None:
        return None
    # This is an OCI identity constraint, not a registry/namespace permission rule.
    if not isinstance(reference, str) or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', reference):
        raise ValueError(f'The tested {role} image for {platform} has an invalid digest reference {reference!r}. '
                         'Reinstall Factory or select Build images locally.')
    return recipe_key, platform, reference


def reference(config, role, local):
    # Acquisition is a build decision. Merely starting a saved sandbox must not
    # consult a catalog or change its prepared images after a preference edit.
    return config.get('image_refs', {}).get(role) or local


def _inspect(config, reference):
    from . import sandbox
    result = sandbox.run([config['engine'], 'image', 'inspect', reference], capture=True,
                         check=False, stderr=subprocess.PIPE)
    if result.returncode:
        from .model_providers import diagnostic
        detail = diagnostic(result.stderr or result.stdout)
        if any(message in detail.lower() for message in ('image not known', 'no such image', 'image not found')):
            return None
        raise ValueError(f'Cannot inspect Factory image {reference} (exit {result.returncode}): '
                         f'{detail or "The engine returned no diagnostic output."} Check the container engine and retry.')
    try:
        data = json.loads(result.stdout)[0]
        if not isinstance(data, dict) or not isinstance(data.get('Id'), str) or not data['Id']:
            raise ValueError('missing image ID')
        return data
    except (ValueError, IndexError, KeyError, TypeError) as error:
        raise ValueError(f'Cannot inspect Factory image {reference}: {config["engine"]} returned malformed image data. '
                         'Check the engine and retry.') from error


def ensure(config, role, local, build_local=None):
    """Return a fixed image reference; never turn a failed pull into a build."""
    from . import sandbox, operation_locks, model_providers
    prepared = config.get('image_refs', {}).get(role)
    if prepared and _inspect(config, prepared):
        return prepared if '@sha256:' in prepared else local
    candidate = _published(config, role)
    if candidate is None:
        if build_local is None:
            return local
        if mode(config) == 'prebuilt':
            print(f'No tested prebuilt {role} image matches these inputs; building it locally.', flush=True)
        build_local()
        data = _inspect(config, local)
        if not data or not data.get('Id'):
            raise ValueError(f'Cannot record the built {role} image {local}: the engine returned no image ID. Retry the build.')
        config.setdefault('image_refs', {})[role] = data['Id']
        return local
    recipe_key, platform, selected = candidate
    with operation_locks.image_artifact(config['engine'], selected, cancelled=sandbox._build_cancelled.get()):
        data = _inspect(config, selected)
        if data is None:
            print(f'Downloading {role} image {selected}…', flush=True)
            result = sandbox.run([config['engine'], 'pull', selected], capture=True, check=False,
                                 stderr=subprocess.PIPE)
            if result.returncode:
                cause = model_providers.diagnostic(result.stderr or result.stdout)
                raise ValueError(f'Cannot download {role} image {selected} (exit {result.returncode}): '
                                 f'{cause or "The engine returned no diagnostic output."} '
                                 'Retry, or select Build images locally.')
            data = _inspect(config, selected)
        if (not data or not isinstance(data.get('RepoDigests'), list) or selected not in data['RepoDigests']
                or str(data.get('Os', '')).lower() != 'linux'
                or data.get('Architecture') != platform.split('/')[1]):
            raise ValueError(f'Cannot verify {role} image {selected} for {platform}: '
                             'the engine did not report the requested digest and platform. '
                             'Retry the download or select Build images locally.')
        if recipe(config, role) != recipe_key:
            raise ValueError(f'The {role} image sources changed while preparing {selected}. '
                             'Retry the build with unchanged sources.')
        used = _used.get()
        if used is not None:
            used[role] = {'role': role, 'recipe': recipe_key, 'reference': selected,
                          'image_id': data['Id']}
        config.setdefault('image_refs', {})[role] = selected
        return selected


def runtime(config, role, local, build_local):
    """Resume prepared images; preference changes apply only to a later build."""
    selected = reference(config, role, local)
    if _inspect(config, selected):
        return selected
    if role in config.get('image_refs', {}):
        raise ValueError(f'The prepared {role} image {selected} is missing. '
                         'Run the sandbox build command before starting it again.')
    # Existing sandboxes used local recipe tags. Keep that identity rather than
    # reinterpreting their live helpers when an acquisition preference is edited.
    temporary = {**config, 'image_refs': {}, 'image_source': 'local'}
    with override('local'):
        build_local(temporary)
    return local


@contextmanager
def recording():
    used = {}
    token = _used.set(used)
    try:
        yield used
    finally:
        _used.reset(token)
