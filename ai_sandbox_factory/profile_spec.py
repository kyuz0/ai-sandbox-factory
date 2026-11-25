"""Optional profile application services; older profile layouts stay unchanged."""
from pathlib import Path
import json
import re

AGENTS = ('pi', 'codex', 'claude', 'agy')


def _text(value, field):
    if not isinstance(value, str) or not value or '\0' in value or '\n' in value or '\r' in value:
        raise ValueError(f'{field}: enter nonempty single-line text without NUL characters.')
    return value


def _argv(value, field):
    if not isinstance(value, list) or not value:
        raise ValueError(f'{field}: provide a nonempty command argument list.')
    if any(not isinstance(arg, str) or '\0' in arg for arg in value):
        raise ValueError(f'{field}: command arguments must be strings without NUL characters.')
    return list(value)


def _port(value, field):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f'{field}: TCP ports must be integers between 1 and 65535.')
    return value


def _relative(root, value, field, *, directory=False):
    value = _text(value, field)
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'{field}: {value!r} must be a relative path within the profile build context.')
    target = root / path
    if not (target.is_dir() if directory else target.is_file()):
        raise ValueError(f'{field}: {value!r} does not name an existing {"directory" if directory else "file"}.')
    return path.as_posix()


def services(metadata, root):
    """Normalize only declared application services; never accept engine flags."""
    source = metadata.get('services', {})
    if not isinstance(source, dict):
        raise ValueError('profile.json services must be an object keyed by service name.')
    result = {}
    for name, spec in source.items():
        # A service name is also a DNS label (RFC 1035: at most 63 ASCII octets).
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name) or name.endswith('-'):
            raise ValueError(f'Service {name!r}: use a lowercase DNS label, at most 63 characters, ending in a letter or digit.')
        prefix = f'Service {name!r}'
        if name in ('workspace', 'proxy'):
            raise ValueError(f'{prefix}: {name!r} is already the private network name of a Factory endpoint. Choose another service name.')
        allowed = {'description', 'image', 'build', 'environment', 'volumes', 'healthcheck', 'connections', 'ssh', 'command'}
        if not isinstance(spec, dict) or spec.keys() - allowed:
            unknown = sorted(spec.keys() - allowed) if isinstance(spec, dict) else []
            raise ValueError(f'{prefix}: unsupported settings {unknown!r}. Declare application settings; Factory controls networks, host mounts and engine flags.')
        value = {'description': _text(spec.get('description'), prefix + ' description')}
        if ('image' in spec) == ('build' in spec):
            raise ValueError(f'{prefix}: declare exactly one of image or build.')
        if 'image' in spec:
            value['image'] = _text(spec['image'], prefix + ' image')
            if value['image'].startswith('-'):
                raise ValueError(f'{prefix}: image must be an image reference, not an engine option.')
        else:
            build = spec['build']
            if not isinstance(build, dict) or build.keys() - {'context', 'dockerfile'}:
                raise ValueError(f'{prefix} build: declare context and optionally dockerfile.')
            context = _relative(Path(root), build.get('context', '.'), prefix + ' build context', directory=True)
            dockerfile = _relative(Path(root) / context, build.get('dockerfile', 'Dockerfile'), prefix + ' Dockerfile')
            value['build'] = {'context': context, 'dockerfile': dockerfile}
        environment = spec.get('environment', {})
        if not isinstance(environment, dict):
            raise ValueError(f'{prefix} environment must be an object.')
        value['environment'] = {}
        for key, content in environment.items():
            if not isinstance(key, str) or not key or any(c in key for c in '=\0\n\r'):
                raise ValueError(f'{prefix}: environment variable {key!r} must have a nonempty name without equals or control characters.')
            if isinstance(content, dict):
                if set(content) != {'generate'}:
                    raise ValueError(f'{prefix} environment {key}: use a string or {{"generate": "secret-name"}}.')
                content = {'generate': _text(content['generate'], prefix + ' generated value')}
            elif not isinstance(content, str) or '\0' in content:
                raise ValueError(f'{prefix} environment {key}: provide a string or a generated value.')
            value['environment'][key] = content
        volumes = spec.get('volumes', {})
        if not isinstance(volumes, dict):
            raise ValueError(f'{prefix} volumes must map persistent volume names to container paths.')
        value['volumes'] = {}
        for key, target in volumes.items():
            if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', key):
                raise ValueError(f'{prefix}: volume name {key!r} must use lowercase letters, digits and hyphens.')
            target = _text(target, prefix + ' volume path')
            if not target.startswith('/') or '..' in Path(target).parts or any(c in target for c in ':,'):
                raise ValueError(f'{prefix}: volume path {target!r} must be absolute and contain no parent traversal, colon or comma.')
            value['volumes'][key] = target
        if 'command' in spec:
            value['command'] = _argv(spec['command'], prefix + ' command')
        if 'healthcheck' in spec:
            value['healthcheck'] = _argv(spec['healthcheck'], prefix + ' healthcheck')
        ssh = spec.get('ssh', {})
        if not isinstance(ssh, dict) or ssh.keys() - {'port', 'user', 'server'}:
            raise ValueError(f'{prefix} ssh: declare port, user and optional server executable.')
        value['ssh'] = {'port': _port(ssh.get('port', 22), prefix + ' SSH port'),
                        'user': _text(ssh.get('user', 'root'), prefix + ' SSH user'),
                        'server': _text(ssh.get('server', '/usr/sbin/sshd'), prefix + ' SSH server')}
        if any(c.isspace() for c in value['ssh']['user']):
            raise ValueError(f'{prefix}: SSH user must be a single OpenSSH account name without whitespace.')
        connections = spec.get('connections', {})
        if not isinstance(connections, dict):
            raise ValueError(f'{prefix} connections must be an object of agent-visible connection settings.')
        value['connections'] = {}
        for key, content in connections.items():
            if isinstance(content, dict) and set(content) == {'environment'}:
                if not isinstance(content['environment'], str) or content['environment'] not in environment:
                    raise ValueError(f'{prefix} connection {key}: environment variable {content["environment"]!r} is not declared.')
            elif not isinstance(content, (str, int)) or isinstance(content, bool):
                raise ValueError(f'{prefix} connection {key}: use text, a number or an environment reference.')
            value['connections'][_text(key, prefix + ' connection name')] = content
        result[name] = value
    return result


def compatible(metadata, agent):
    declared = metadata.get('agents', ['pi'] if metadata.get('name') != 'base' else list(AGENTS))
    if not isinstance(declared, list) or not declared or any(a not in AGENTS for a in declared):
        raise ValueError('Profile agents must be a nonempty list of pi, codex, claude or agy.')
    return agent in declared


def validate(root, agent=None):
    """Static profile checks shared by Factory and the in-sandbox authoring tool."""
    root = Path(root)
    # Build snapshots use regular files; never follow a link to copy host files.
    for path in root.rglob('*'):
        if any(part in ('.git', '__pycache__') for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f'Profile build snapshots require regular files: {path}. Copy the required file into the profile instead of linking it.')
    file = root / 'profile.json'
    try:
        metadata = json.loads(file.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'{file}: line {error.lineno}, column {error.colno}: {error.msg}. Fix the JSON and validate again.') from error
    except (OSError, UnicodeError) as error:
        raise ValueError(f'Cannot read {file}: {error}. Restore this profile file and validate again.') from error
    if not isinstance(metadata, dict):
        raise ValueError(f'{file}: the profile must be a JSON object, not {type(metadata).__name__}.')
    name = metadata.get('name')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,254}', name):
        raise ValueError(f'{file}: name {name!r} must start with a lowercase letter and contain up to 255 lowercase letters, digits or hyphens.')
    if type(metadata.get('version', 1)) is not int or metadata.get('version', 1) != 1:
        raise ValueError(f'{file}: version {metadata.get("version")!r} must be the supported manifest format 1. Use Git revisions to version a profile.')
    if not isinstance(metadata.get('description', ''), str):
        raise ValueError(f'{file}: description must be text, not {type(metadata["description"]).__name__}.')
    for name in ('Dockerfile', 'profile.md'):
        path = root / name
        try:
            path.read_text(encoding='utf-8')
        except UnicodeError as error:
            raise ValueError(f'{path} must be valid UTF-8 text: {error}. Fix the file and validate again.') from error
        except OSError as error:
            raise ValueError(f'Cannot read required profile file {path}: {error}. Restore it and validate again.') from error
    for name in ('system-prompt.md', 'start-here.md'):
        path = root / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError as error:
            raise ValueError(f'{path} must be valid UTF-8 text: {error}. Fix the file and validate again.') from error
        except OSError as error:
            raise ValueError(f'Cannot read profile guide {path}: {error}. Fix the file and validate again.') from error
        if name == 'system-prompt.md' and any(marker in text for marker in ('<!-- sandbox-profile -->', '<!-- /sandbox-profile -->')):
            raise ValueError(f'{path} uses a reserved Factory marker. Remove the marker; Factory supplies it when installing the instructions.')
    compatible(metadata, agent or 'pi')
    if agent and not compatible(metadata, agent):
        raise ValueError(f'{file}: agents {metadata.get("agents", ["pi"])!r} do not include this sandbox’s coding agent {agent!r}. Keep that agent enabled or create a separate sandbox with another agent.')
    services(metadata, root)
    defaults = metadata.get('defaults', {})
    if (not isinstance(defaults, dict) or defaults.keys() - {'web_search', 'subagents'}
            or any(type(value) is not bool for value in defaults.values())):
        raise ValueError(f'{file}: defaults accept only boolean web_search and subagents settings.')
    summary = metadata.get('summary', '')
    if not isinstance(summary, str) or '\n' in summary or '\r' in summary:
        raise ValueError(f'{file}: summary must be single-line text.')
    return metadata


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Check profile files without building or changing the sandbox.')
    parser.add_argument('path', type=Path)
    parser.add_argument('--agent', choices=AGENTS)
    args = parser.parse_args()
    try:
        validate(args.path, args.agent)
    except (ValueError, OSError) as error:
        parser.exit(1, f'Error: {error}\n')
    print('Static checks passed: profile.json, required files, paths, agents and services. Dockerfiles and service startup are checked when you apply.')


if __name__ == '__main__':
    main()
