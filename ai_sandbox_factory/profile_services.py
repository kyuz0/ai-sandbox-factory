"""Profile-owned application containers, private networking and administrative SSH."""
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import time

from . import host_platform
from .sandbox_options import RESOURCE_PREFIX, write_json

CLIENT = '/run/profile-services'
DOMAIN = 'profile.internal'
LABEL_ROLE = 'io.pi-sandbox-factory.profile-role'


def selected(config):
    profile = config.get('profile') or {}
    if isinstance(profile, str):
        from . import sandbox
        profile = sandbox.load_profile(profile)
    return profile.get('services', {})


def prefix(config):
    return RESOURCE_PREFIX + config['name'] + '-profile'


def hostname(name):
    return name + '.' + DOMAIN


def container(config, name):
    return prefix(config) + '-service-' + name


def data_volume(config, name, volume):
    key = hashlib.sha256(json.dumps([name, volume]).encode()).hexdigest()[:16]
    return prefix(config) + '-data-' + key


def resources(config):
    if not selected(config):
        return {'container': set(), 'volume': set(), 'network': set()}
    base = prefix(config)
    containers = {base + '-gateway', base + '-proxy'}
    volumes = {base + '-socket'}
    for name, spec in selected(config).items():
        containers.add(container(config, name))
        volumes.update(data_volume(config, name, volume) for volume in spec['volumes'])
    return {'container': containers, 'volume': volumes, 'network': {base}}


def _run(config, *args, **kwargs):
    from . import sandbox
    return sandbox.run([config['engine'], *args], **kwargs)


def _labels(config, role):
    from . import sandbox
    return ['--label', f'{sandbox.LABEL}={config["name"]}', '--label', f'{LABEL_ROLE}={role}']


def validate(config):
    if not selected(config):
        return
    if config['engine'] != 'podman':
        raise ValueError('Profiles with application services require rootless Podman. Select Podman as the container engine.')
    if config.get('network', 'allowlist') != 'allowlist':
        raise ValueError('Profiles with application services currently require Restricted networking. Select Restricted before creating this sandbox.')


def check_ownership(config):
    from . import sandbox
    wanted = resources(config)
    for name in wanted['container']:
        data = sandbox.inspect(config, name)
        if data and data.get('Config', {}).get('Labels', {}).get(LABEL_ROLE) != name:
            raise ValueError(f'Container {name!r} is not a profile service. No unrelated container was changed.')
    for kind in ('volume', 'network'):
        for name in wanted[kind]:
            found = _run(config, kind, 'inspect', name, capture=True, check=False, stderr=subprocess.DEVNULL)
            if not found.returncode:
                data = json.loads(found.stdout)[0]
                labels = data.get('Labels', data.get('labels', {})) or {}
                if labels.get(sandbox.LABEL) != config['name']:
                    raise ValueError(f'{kind.capitalize()} {name!r} belongs to another owner. Choose another sandbox name or inspect it before removal.')


def image(config, name, spec):
    if 'image' in spec:
        return spec['image']
    digest = hashlib.sha256((config['profile']['digest'] + json.dumps(spec['build'], sort_keys=True)).encode()).hexdigest()[:16]
    return f'localhost/pi-sandbox-factory-service:{name}-{digest}'


def resolved_images(config):
    from . import sandbox
    result = {}
    for name, spec in selected(config).items():
        current = sandbox.inspect(config, container(config, name))
        if current:
            result[name] = current['Image']
            continue
        found = _run(config, 'image', 'inspect', image(config, name, spec), capture=True)
        result[name] = json.loads(found.stdout)[0]['Id']
    return result


def validate_update(original, updated):
    """Profile editing/data migrations are separate from workspace rebuilding."""
    def contract(config):
        from . import sandbox
        result = {}
        for name, spec in selected(config).items():
            result[name] = dict(spec)
            if 'build' in spec:
                result[name]['source_digest'] = sandbox.tree_digest(
                    Path(config['profile']['path']) / spec['build']['context'])
        return result
    if contract(original) != contract(updated):
        raise ValueError('This profile changes the sandbox’s application containers. '
                         'The existing services and their data were preserved. '
                         'Create a separate sandbox with the new profile; export/import application data if needed. '
                         'Workspace-only rebuilds keep the existing services.')


def build(config):
    validate(config)
    for name, spec in selected(config).items():
        tag = image(config, name, spec)
        if 'build' in spec:
            if not _run(config, 'image', 'inspect', tag, capture=True, check=False, stderr=subprocess.DEVNULL).returncode:
                continue
            build = spec['build']
            context = Path(config['profile']['path']) / build['context']
            _run(config, 'build', '-t', tag, '-f', context / build['dockerfile'], context)
        elif _run(config, 'image', 'inspect', tag, capture=True, check=False, stderr=subprocess.DEVNULL).returncode:
            _run(config, 'pull', tag)


def _key(path):
    if not path.exists():
        from . import sandbox
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        sandbox.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)])
        path.chmod(0o600)


def prepare(config, state):
    """Generate each sandbox's application credentials once, outside the recipe."""
    if not selected(config):
        return
    state = Path(state)
    directory = state / 'profile-services'
    client = directory / 'client'
    client.mkdir(parents=True, exist_ok=True)
    _key(client / 'client-key')
    stored = directory / 'values.json'
    values = json.loads(stored.read_text()) if stored.exists() else {}
    connections = {}
    known = []
    ssh_config = []
    for name, spec in selected(config).items():
        server = directory / 'servers' / name
        _key(server / 'host-key')
        (server / 'authorized_keys').write_text((client / 'client-key.pub').read_text())
        ssh = spec['ssh']
        server.joinpath('sshd_config').write_text(
            f'Port {ssh["port"]}\nListenAddress 0.0.0.0\nListenAddress ::\n'
            'HostKey /run/profile-service/host-key\nAuthorizedKeysFile /etc/profile-service-authorized-keys\n'
            'PermitRootLogin yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n'
            'StrictModes no\nPidFile /run/profile-service-sshd.pid\n')
        host_key = (server / 'host-key.pub').read_text().strip()
        alias = hostname(name)
        host_entry = alias if ssh['port'] == 22 else f'[{alias}]:{ssh["port"]}'
        known.append(host_entry + ' ' + host_key)
        ssh_config.append(
            f'Host {name} {alias}\n    HostName {alias}\n    User {ssh["user"]}\n    Port {ssh["port"]}\n'
            f'    IdentityFile {CLIENT}/client-key\n    UserKnownHostsFile {CLIENT}/known_hosts\n'
            f'    IdentitiesOnly yes\n    IdentityAgent none\n    StrictHostKeyChecking yes\n'
            '    ProxyCommand none\n    BatchMode yes\n')
        environment = {}
        for key, value in spec['environment'].items():
            if isinstance(value, dict):
                reference = name + ':' + value['generate']
                value = values.setdefault(reference, secrets.token_urlsafe(32))
            environment[key] = value
        connection = {key: environment[value['environment']] if isinstance(value, dict) else value
                      for key, value in spec['connections'].items()}
        connections[name] = {'host': alias, 'description': spec['description'], 'ssh_user': ssh['user'],
                             'ssh_port': ssh['port'], **connection}
        write_json(server / 'environment.json', environment)
    write_json(stored, values)
    write_json(client / 'connections.json', connections)
    (client / 'known_hosts').write_text('\n'.join(known) + '\n')
    (client / 'config').write_text('\n'.join(ssh_config))
    source = Path(__file__).parent / 'containers/profile-services'
    # Keep the bound directory and files stable across ordinary starts.
    (directory / 'relay.py').write_text((source / 'relay.py').read_text())


def mounts(config, state):
    if not selected(config):
        return []
    client = Path(state) / 'profile-services/client'
    return ['-v', f'{client}:{CLIENT}:{host_platform.bind_mode()}']


def network_args(config):
    if not selected(config):
        return []
    return ['--network', prefix(config), '--network-alias', 'workspace',
            '--network-alias', hostname('workspace')]


def no_proxy(config):
    return ','.join(['localhost', '127.0.0.1', '::1', 'workspace', hostname('workspace'),
                     *(value for name in selected(config) for value in (name, hostname(name)))])


def _start(config, name, args, environment=None):
    from . import sandbox
    existing = sandbox.inspect(config, name)
    if existing:
        sandbox.resume_existing(config, name, existing)
    else:
        # Container values must never alter the host engine's environment.
        # Argument form preserves multiline values; errors redact those values.
        environment = environment or {}
        env_args = [arg for key, value in environment.items() for arg in ('-e', f'{key}={value}')]
        result = _run(config, 'run', '-d', '--name', name, *_labels(config, name),
                      '--security-opt=no-new-privileges', '--cap-drop=NET_ADMIN', '--cap-drop=NET_RAW',
                      *env_args, *args, capture=True, check=False, stderr=subprocess.PIPE)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    detail = detail.replace(value, '[redacted]')
            raise ValueError(f'Could not start profile container {name!r} (exit {result.returncode}): '
                             f'{detail}. Inspect its image and profile settings, then retry Start.')


def up(config, state):
    if not selected(config):
        return
    from . import sandbox, profile_runtime
    validate(config)
    check_ownership(config)
    state = Path(state)
    prepare(config, state)
    base = prefix(config)
    frozen = (profile_runtime.applied(state) or {}).get('services', {})
    directory = state / 'profile-services'
    known = _run(config, 'network', 'exists', base, check=False, capture=True)
    if known.returncode:
        _run(config, 'network', 'create', '--internal', '--ipv6', *_labels(config, 'network'), base)
    socket = base + '-socket'
    if _run(config, 'volume', 'exists', socket, check=False, capture=True).returncode:
        _run(config, 'volume', 'create', *_labels(config, 'socket'), socket)
    # This gateway has only the user's external rules, never credential companion sockets.
    policy = sandbox.squid_config(sandbox.parse_rules((state / 'allowlist.txt').read_text()), config.get('website_groups', {}))
    (directory / 'squid.conf').write_text(policy)
    gateway_args = ['--network=bridge', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=DAC_OVERRIDE',
                    *host_platform.gateway_labels(),
                    '-v', f'{directory / "squid.conf"}:/etc/squid/sandbox.conf:{host_platform.bind_mode()}',
                    '-v', f'{socket}:/run/egress:z']
    if config.get('local_model_gateway'):
        gateway_args += host_platform.model_gateway_args()
    if any(host == 'host.docker.internal' for host, _ in sandbox.parse_rules((state / 'allowlist.txt').read_text())):
        gateway_args += ['--add-host=host.docker.internal:host-gateway']
    _start(config, base + '-gateway', [*gateway_args, sandbox.image_names(config)[1]])
    _start(config, base + '-proxy', [
        '--network', base, '--network-alias', hostname('proxy'), '--cap-drop=ALL',
        '-v', f'{socket}:/run/egress:ro,z',
        '-v', f'{directory / "relay.py"}:/opt/profile-relay.py:{host_platform.bind_mode()}',
        '--entrypoint', 'python3', sandbox.image_names(config)[1], '/opt/profile-relay.py'])
    for name, spec in selected(config).items():
        args = ['--network', base, '--network-alias', name, '--network-alias', hostname(name),
                '--sysctl', 'net.ipv4.ping_group_range=0 0', '--security-opt=label=disable',
                '-v', f'{directory / "servers" / name}:/run/profile-service:{host_platform.bind_mode()}',
                '-v', f'{directory / "servers" / name / "authorized_keys"}:/etc/profile-service-authorized-keys:{host_platform.bind_mode()}',
                '-e', f'HTTP_PROXY=http://{hostname("proxy")}:3128',
                '-e', f'HTTPS_PROXY=http://{hostname("proxy")}:3128',
                '-e', f'http_proxy=http://{hostname("proxy")}:3128',
                '-e', f'https_proxy=http://{hostname("proxy")}:3128', '-e', 'NO_PROXY=' + no_proxy(config)]
        environment = json.loads((directory / 'servers' / name / 'environment.json').read_text())
        for volume, target in spec['volumes'].items():
            volume_name = data_volume(config, name, volume)
            if _run(config, 'volume', 'exists', volume_name, capture=True, check=False).returncode:
                _run(config, 'volume', 'create', *_labels(config, 'data'), volume_name)
            args += ['-v', volume_name + ':' + target]
        _start(config, container(config, name), [*args, frozen.get(name) or image(config, name, spec), *spec.get('command', [])], environment)
        # The application's original entrypoint remains PID 1; sshd runs inside the same container.
        check = _run(config, 'exec', '--user', '0', container(config, name), 'sh', '-c',
                     'test -d /run/sshd || mkdir -p /run/sshd; '
                     'pid=$(cat /run/profile-service-sshd.pid 2>/dev/null); '
                     'test -n "$pid" && test "$(cat /proc/$pid/comm 2>/dev/null)" = sshd',
                     capture=True, check=False)
        if check.returncode:
            started = _run(config, 'exec', '--user', '0', container(config, name),
                           spec['ssh']['server'], '-f', '/run/profile-service/sshd_config',
                           capture=True, check=False, stderr=subprocess.PIPE)
            if started.returncode:
                raise ValueError(f'Could not start administrative SSH in profile service {name!r} '
                                 f'(exit {started.returncode}): {(started.stderr or started.stdout).strip()}. '
                                 'Its image must provide a shell and SSH server. Check the service Dockerfile.')


def update_policy(config, state, text, groups):
    """Refresh the external-only gateway without touching application processes."""
    if not selected(config):
        return
    from . import sandbox
    path = Path(state) / 'profile-services/squid.conf'
    if not path.exists():
        return
    policy = sandbox.squid_config(sandbox.parse_rules(text), groups)
    if policy == path.read_text():
        return
    path.write_text(policy)
    gateway = prefix(config) + '-gateway'
    current = sandbox.inspect(config, gateway)
    if current and current['State']['Running']:
        sandbox.restart_existing(config, gateway)


def ready(config, state):
    """Wait for initialization while the service runs; cancellation kills this job."""
    if not selected(config):
        return
    from . import sandbox
    values = Path(state) / 'profile-services/values.json'
    secrets_to_hide = list(json.loads(values.read_text()).values()) if values.exists() else []
    def diagnostic(text):
        for value in secrets_to_hide:
            text = text.replace(value, '[redacted]')
        return text
    dev = sandbox.names(config)[0]
    for name, spec in selected(config).items():
        command = spec.get('healthcheck', ['true'])
        announced = False
        while True:
            info = sandbox.inspect(config, container(config, name))
            if not info or not info.get('State', {}).get('Running'):
                logs = _run(config, 'logs', container(config, name), capture=True,
                            check=False, stderr=subprocess.PIPE)
                raise ValueError(f'Profile service {name!r} stopped during initialization: '
                                 f'{diagnostic(logs.stdout + (logs.stderr or ""))}. Inspect its Dockerfile and retry Start.')
            result = _run(config, 'exec', container(config, name), *command,
                          capture=True, check=False, stderr=subprocess.PIPE)
            if result.returncode == 0:
                break
            if result.returncode in (126, 127):
                raise ValueError(f'Profile service {name!r} healthcheck {command!r} could not run '
                                 f'(exit {result.returncode}): {diagnostic((result.stderr or result.stdout).strip())}. '
                                 'Install the command in the service image or fix its healthcheck.')
            if not announced:
                print(f'Waiting for profile service {name}: {diagnostic((result.stderr or result.stdout).strip())}', flush=True)
                announced = True
            time.sleep(0.5)
        check = _run(config, 'exec', dev, 'ssh', '-F', CLIENT + '/config', '-T', hostname(name), 'true',
                     capture=True, check=False, stderr=subprocess.PIPE)
        if check.returncode:
            raise ValueError(f'Could not authenticate SSH from the workspace to profile service {name!r} '
                             f'(exit {check.returncode}): {(check.stderr or check.stdout).strip()}. '
                             'Inspect the service SSH setup and retry Start.')


def down(config, remove=False):
    if not selected(config):
        return
    from . import sandbox
    check_ownership(config)
    for name in sorted(resources(config)['container']):
        if sandbox.inspect(config, name):
            _run(config, 'stop', name)
            if remove:
                _run(config, 'rm', name)


def delete(config):
    if not selected(config):
        return
    check_ownership(config)
    down(config, remove=True)
    wanted = resources(config)
    for name in wanted['volume']:
        if not _run(config, 'volume', 'exists', name, capture=True, check=False).returncode:
            _run(config, 'volume', 'rm', name)
    for name in wanted['network']:
        if not _run(config, 'network', 'exists', name, capture=True, check=False).returncode:
            _run(config, 'network', 'rm', name)


def guidance(config):
    if not selected(config):
        return ''
    lines = ['\n## Profile containers', '',
             'These application containers share a private network with this workspace. '
             'You have administrative SSH access inside them. External requests follow Factory proxy rules. '
             f'Read {CLIENT}/connections.json for application connection settings and credentials. '
             'These credentials are deliberately available to you; they are separate from Factory connector credentials.']
    for name, spec in selected(config).items():
        lines.append(f'- {name}: {spec["description"]}. SSH: ssh -F {CLIENT}/config {hostname(name)} '
                     f'(user {spec["ssh"]["user"]}, port {spec["ssh"]["port"]}).')
    from . import primary_workspace
    from .sandbox_options import options
    current = options(config.get('options'))
    if primary_workspace.agent(config) == 'pi' and current['inner_sandbox']:
        if current['approval'] == 'off':
            lines.append('The current Pi inner shell blocks direct TCP and approvals are off. '
                         'The user can run the SSH command in the workspace terminal, or enable approvals to use bash_escalate.')
        else:
            lines.append('With the current Pi inner shell enabled, use bash_escalate for direct SSH, raw TCP clients and ping. '
                         'This uses the existing approval mode and does not bypass an approval denial.')
    else:
        lines.append('Run the SSH command directly in Bash. Profile hosts do not use the external SSH connector.')
    lines.append('Changes inside a running container do not update its profile recipe.')
    return '\n'.join(lines) + '\n'


def status(config):
    from . import sandbox
    return [{'name': name, 'host': hostname(name), 'description': spec['description'],
             'ssh': f'ssh -F {CLIENT}/config {hostname(name)}',
             'running': bool((sandbox.inspect(config, container(config, name)) or {}).get('State', {}).get('Running'))}
            for name, spec in selected(config).items()]
