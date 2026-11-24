#!/usr/bin/env python3
"""Create and manage coding agent sandboxes with Podman or Docker."""
import argparse
from . import activity
from .cli import COMMAND
from . import sandbox_skills
from . import sandbox_extensions
from . import sandbox_advisors
from . import sandbox_ssh
from . import inference, aws_access, primary_workspace, connectors, connector_settings
from . import registry, model_providers, browser_viewer, folders, host_platform, updating, prompt_library
from . import editor_settings, operation_locks, system_packages
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import ipaddress
import json
from . import web_access, network_groups, communications
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from types import SimpleNamespace
from uuid import uuid4
from .sandbox_options import (RESOURCE_PREFIX, SANDBOX_NAME, SANDBOX_NAME_ERROR,
                              options, memory_limit, permissions, write_json, guidance)

ROOT = Path(__file__).resolve().parent
LABEL = 'io.pi-sandbox-factory.name'
PI_VERSION = '1.4.0'
RUNTIME_ENV = {
    'HTTP_PROXY': 'http://127.0.0.1:3128', 'HTTPS_PROXY': 'http://127.0.0.1:3128',
    'http_proxy': 'http://127.0.0.1:3128', 'https_proxy': 'http://127.0.0.1:3128',
    'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1',
    'NODE_USE_ENV_PROXY': '1',
}


def tree_digest(path):
    digest = hashlib.sha256()
    for file in sorted(path.rglob('*')):
        if any(part in {'.git', '__pycache__'} for part in file.relative_to(path).parts):
            continue
        if file.is_symlink():
            raise ValueError(f'Profile/build contexts must not contain symlinks: {file}')
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode() + b'\0' + file.read_bytes() + b'\0')
    return digest.hexdigest()[:16]


def load_profile(value):
    candidate = registry.resolve('profile', value)
    path = directory(candidate)
    metadata = json.loads((path / 'profile.json').read_text())
    if not registry.NAME.fullmatch(metadata.get('name', '')):
        raise ValueError('profile.json requires a lowercase name (letters, digits, hyphens)')
    for filename in ('Dockerfile', 'profile.md'):
        if not (path / filename).is_file():
            raise ValueError(f'Profile is missing {filename}: {path}')
    profile = {'name': metadata['name'], 'description': metadata.get('description', ''),
               'path': str(path), 'digest': tree_digest(path), 'registry_name': value}
    if metadata.get('defaults'):
        profile['defaults'] = metadata['defaults']
    if 'git_source' in metadata:
        source = metadata['git_source']
        if (not isinstance(source, str) or not source
                or source.startswith('-') or any(c.isspace() for c in source)):
            raise ValueError('git_source must be a Git repository URL without whitespace')
        if (path / 'source').exists():
            raise ValueError('git_source profiles must reserve source/ for fetched files')
        profile['git_source'] = source
    return profile


def publish_snapshot(source, destination):
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'snapshot'
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        try:
            staged.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not destination.is_dir():
                raise


def snapshot_profile(profile, state):
    if profile.get('git_source'):
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.profile-', dir=state) as temp:
            staging = Path(temp) / 'profile'
            checkout = Path(temp) / 'checkout'
            shutil.copytree(profile['path'], staging,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            # Authenticate on the host; never put Git credentials or metadata in images.
            run(['git', 'clone', '--recurse-submodules', '--', profile['git_source'], str(checkout)])
            tree_digest(checkout)  # Reject symlinked build inputs before copying.
            shutil.copytree(checkout, staging / 'source',
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            revision = run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], capture=True).stdout.strip()
            submodules = run(['git', '-C', str(checkout), 'submodule', 'status', '--recursive'],
                             capture=True).stdout
            (staging / 'source-revisions.json').write_text(json.dumps(
                {'commit': revision, 'submodules': [
                    {'commit': line[1:41], 'path': line[42:].split(' (', 1)[0]}
                    for line in submodules.splitlines()]}, indent=2) + '\n')
            digest = tree_digest(staging)
            destination = state / 'profiles' / digest
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(destination)
            # A saved snapshot builds offline from its fetched inputs. Only an explicit
            # recreate --profile fetches a new source version.
            return {key: value for key, value in dict(profile, path=str(destination),
                    digest=digest, source_commit=revision).items() if key != 'git_source'}
    destination = state / 'profiles' / profile['digest']
    publish_snapshot(profile['path'], destination)
    return dict(profile, path=str(destination))


def selected_profile(config):
    return config.get('profile') or load_profile('base')


def common_profile():
    path = ROOT / 'containers/dev'
    return dict(name='common', path=str(path), digest=tree_digest(path),
                **json.loads((path / 'package-source.json').read_text()))


def run(argv, capture=False, check=True, **kwargs):
    return subprocess.run([str(x) for x in argv], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs)


def directory(value):
    path = host_platform.local_path(value).resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f'Not a directory: {path}')
    if any(c in str(path) for c in ':,\n\r'):
        raise ValueError('Mount paths must not contain colons, commas, or newlines')
    return path


def parse_rules(text):
    rules = []
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        words = line.split('#', 1)[0].split()
        if not words:
            continue
        # Accept the old suffix for existing policy files; it no longer changes
        # behavior. An explicitly allowed hostname may resolve to any address.
        if len(words) > 2 or (len(words) == 2 and words[1] != 'private'):
            raise ValueError(f'Allowlist line {number}: expected hostname:port')
        match = re.fullmatch(r'([a-zA-Z0-9.-]+):([0-9]+)', words[0])
        if not match:
            raise ValueError(f'Allowlist line {number}: use an exact hostname:port')
        host, port = match[1].lower(), int(match[2])
        if (len(host) > 253 or '.' not in host or not 1 <= port <= 65535
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                       for p in host.split('.'))):
            raise ValueError(f'Allowlist line {number}: invalid hostname or port')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('Use DNS names, not literal IP addresses, in the allowlist')
        rule = (host, port)
        if rule not in seen:
            seen.add(rule)
            rules.append(rule)
    return rules


def squid_config(rules, groups=None):
    lines = [
        'http_port 127.0.0.1:3128',
        'visible_hostname pi-sandbox-factory-gateway',
        # The entrypoint supervises Squid directly. A persistent PID file can
        # mistake a reused container PID for the previous process after a crash.
        'pid_filename none',
        'cache_log /dev/stderr',
        'access_log stdio:/dev/stdout',
        'cache_store_log none',
        'cache deny all',
        'coredump_dir /tmp',
        'shutdown_lifetime 1 seconds',
        'forwarded_for delete',
        'via off',
        'acl sandbox_methods method GET HEAD POST PUT PATCH DELETE OPTIONS CONNECT',
        'http_access deny !sandbox_methods',
        # Reject unknown names before forwarding, so denied requests cause no DNS.
    ]
    category = network_groups.acl(groups or {})
    declarations = [line for line in category if line.startswith('acl ')]
    category_access = [line for line in category if not line.startswith('acl ')]
    lines += declarations
    if rules:
        lines += ['acl sandbox_hosts dstdomain -n ' + ' '.join(sorted({r[0] for r in rules})),
                  'http_access deny !sandbox_hosts' + (' !category_hosts' if category else '')]
    elif category:
        lines += ['http_access deny !category_hosts']
    else:
        return '\n'.join(lines + ['http_access deny all', ''])
    for i, (host, port) in enumerate(rules):
        lines += [f'acl host_{i} dstdomain -n {host}', f'acl port_{i} port {port}']
    for i, _ in enumerate(rules):
        lines.append(f'http_access allow host_{i} port_{i}')
    return '\n'.join(lines + category_access + ['http_access deny all', ''])


def network_policy(config, rules):
    from . import hosted_models
    selected = options(config.get('options'))
    destinations = [('aws-access.internal', 19432)] if config.get('aws_access') else []
    if primary_workspace.agent(config) == 'pi':
        destinations += (web_access.destinations(selected['web_access'])
                         + communications.destinations(selected['communications'])
                         + hosted_models.destinations(config.get('hosted_connections', {})))
    combined = list(dict.fromkeys(rules + destinations))
    return squid_config(combined, config.get('website_groups', {}))


def validate_models(config, rules, strict=True):
    models = config / 'models.json'
    if not models.is_file():
        raise ValueError(f'Missing {models}')
    providers = json.loads(models.read_text()).get('providers', {})
    allowed = set(rules)
    for name, provider in providers.items():
        if 'baseUrl' not in provider:
            continue
        url = urlsplit(provider['baseUrl'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError(f'Provider {name}: baseUrl must be an HTTP(S) URL without credentials')
        target = (url.hostname, url.port or (443 if url.scheme == 'https' else 80))
        if target not in allowed:
            if strict:
                raise ValueError(f'Provider {name}: add {target[0]}:{target[1]} to the allowlist')
            print(f'Provider {name}: {target[0]}:{target[1]} remains blocked by the network policy.')


def validate_env(path):
    # Never source the file in a shell. Limit it to credentials and provider options.
    if not path:
        return
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, _ = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('Env file must contain literal UPPERCASE_NAME=value entries')
        if key in {'SLACK_BOT_TOKEN', 'SLACK_USER_TOKEN', 'SLACK_CREDENTIALS_FILE'}:
            raise ValueError('Configure Slack tokens through Settings → Communications, not an environment file')
        if (key in {'HOME', 'PATH', 'ENV', 'BASH_ENV', 'LD_PRELOAD', 'LD_LIBRARY_PATH',
                    'NODE_OPTIONS', 'NODE_USE_ENV_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                    'SSH_AUTH_SOCK', 'PI_ADVISOR_SOCKET', 'SANDBOX_NETWORK_MODE', 'SANDBOX_SUBAGENTS', 'SANDBOX_APPROVAL_MODE', 'COMPACTION_RECALL_MODE', 'PI_SANDBOX_CONTAINER', 'PI_AUTO_REVIEW_ALLOW_UNTRUSTED_DEV'}
                or key.startswith('PI_KNOWLEDGE_') or key == 'SANDBOX_KNOWLEDGE_ROOTS'
                or any(x in key for x in ('PROXY', 'PI_CODING_AGENT_DIR'))):
            raise ValueError(f'{key} is controlled by the sandbox, not the credentials file')


def choose_engine(requested, quiet=False):
    engine = host_platform.find_engine(requested)
    if requested == 'docker' and 'podman' in run(['docker', '--version'], capture=True).stdout.lower():
        if not quiet:
            print('The docker command is a Podman wrapper; using rootless Podman mode.')
        return host_platform.find_engine('podman')
    return engine


def names(config):
    prefix = RESOURCE_PREFIX + config['name']
    return prefix, prefix + '-proxy', prefix + '-socket', prefix + '-home'


def stage_image(image, suffix=''):
    # Docker and Podman accept image tags with up to 128 characters.
    repository, tag = image.rsplit(':', 1)
    tag += suffix
    if len(tag) > 128:
        digest = hashlib.sha256(tag.encode()).hexdigest()[:16]
        tag = tag[:128 - len(digest) - 1] + '-' + digest
    return f'{repository}:{tag}'


def image_names(config):
    if primary_workspace.agent(config) != 'pi':
        return primary_workspace.image(config), 'localhost/pi-sandbox-factory-proxy:1'
    profile = selected_profile(config)
    suffix = ('-' + hashlib.sha256(json.dumps([s['digest'] for s in config['extra_skills']]).encode()).hexdigest()[:12]) if config.get('extra_skills') else ''
    if options(config.get('options'))['browser']:
        suffix += '-browser'
    if options(config.get('options'))['web_access']['enabled']:
        suffix += '-web'
    if options(config.get('options'))['communications']['slack']:
        suffix += '-slack'
    knowledge_mode = options(config.get('options'))['knowledge_embedding']
    if knowledge_mode != 'off':
        suffix += '-knowledge-endpoint' if knowledge_mode == 'endpoint' else '-knowledge'
    if config.get('aws_access'):
        suffix += '-aws-' + tree_digest(ROOT / 'containers/aws-access/client')
    if 'model_providers' in config:
        suffix += '-models-' + model_providers.digest(config)
    return (stage_image(f'localhost/pi-sandbox-factory-dev:{profile["name"]}-{base_tag(config)}-{profile["digest"]}{suffix}'),
            'localhost/pi-sandbox-factory-proxy:1')


def network_mode(config):
    mode = config.get('network', 'allowlist')
    if mode not in ('allowlist', 'unrestricted'):
        raise ValueError(f'Invalid saved network mode: {mode}')
    return mode


def pendant_versions(config):
    """Read the runtime pins from a saved Pendant build, without asking the engine."""
    common = config.get('common')
    path = common.get('path') if isinstance(common, dict) else None
    if not isinstance(path, str) or not path:
        return None
    try:
        dockerfile = (Path(path) / 'Dockerfile').read_text()
    except (OSError, ValueError):
        return None
    if 'cdervis.vscode-pi-' not in dockerfile or '--from=frostpi' in dockerfile:
        return None
    pi = re.findall(r'^ARG PI_VERSION=([0-9]+\.[0-9]+\.[0-9]+)$', dockerfile, re.MULTILINE)
    pendant = re.findall(r'^ARG PENDANT_VERSION=([0-9]+\.[0-9]+\.[0-9]+)$', dockerfile, re.MULTILINE)
    if len(pi) == len(pendant) == 1:
        return pi[0], pendant[0]
    return None


def base_tag(config):
    digest = config.get('common', {}).get('digest') or tree_digest(ROOT / 'containers/dev')
    runtime = '-'.join(pendant_versions(config) or (PI_VERSION,))
    identity = f'{runtime}-{config["uid"]}-{config["gid"]}-' + digest
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def image_graph(config):
    agent = primary_workspace.agent(config)
    # Pi profiles inherit a shared base. Native workspace variants share the
    # agent's credential image across otherwise distinct workspace tags.
    key = base_tag(config) if agent == 'pi' else f'{agent}:{config["uid"]}:{config["gid"]}'
    return operation_locks.image_graph(config['engine'], key)


def build(config):
    with image_graph(config):
        _build(config)


def _build(config):
    host_platform.check_engine(config['engine'], config.get('podman_connection'))
    dev, proxy = image_names(config)
    if primary_workspace.agent(config) != 'pi':
        with operation_locks.proxy_image(config['engine']):
            run([config['engine'], 'build', '-t', proxy, ROOT / 'containers/proxy'])
        primary_workspace.build(config, dev)
        return
    profile = selected_profile(config)
    if tree_digest(Path(profile['path'])) != profile['digest']:
        raise ValueError('Saved profile was modified; select its source again with recreate --profile')
    common = config['common']
    if config.get('aws_access'):
        entrypoint = Path(common['path']) / 'entrypoint.sh'
        if not entrypoint.is_file() or '/run/aws-access/request.sock' not in entrypoint.read_text():
            raise ValueError('Choose Actions → Update sources and recreate… before connecting AWS. '
                             'The saved sandbox runtime lacks the AWS CLI connection.')
    if tree_digest(Path(common['path'])) != common['digest']:
        raise ValueError('Saved common image inputs were modified; use recreate --profile to refresh them')
    knowledge_mode = options(config.get('options'))['knowledge_embedding']
    knowledge = knowledge_mode != 'off'
    knowledge_dir = Path(common['path']) / 'local-knowledge'
    if knowledge_mode == 'endpoint' and not (knowledge_dir / 'Dockerfile.endpoint').is_file():
        raise ValueError('Choose Actions → Update sources and recreate… before enabling an embedding server.')
    if knowledge_mode == 'local' and not (knowledge_dir / 'Dockerfile').is_file():
        raise ValueError('This version uses an embedding server for semantic search. Select keyword search '
                         'in Settings → Document search before updating sources, then configure your server.')
    base = f'localhost/pi-sandbox-factory-base:{base_tag(config)}'
    engine = config['engine']
    pi, *pendant = pendant_versions(config) or (PI_VERSION,)
    build_args = ['--build-arg', f'USER_UID={config["uid"]}', '--build-arg', f'USER_GID={config["gid"]}',
                  '--build-arg', f'PI_VERSION={pi}']
    if pendant:
        build_args += ['--build-arg', f'PENDANT_VERSION={pendant[0]}']
    with operation_locks.proxy_image(engine):
        run([engine, 'build', '-t', proxy, ROOT / 'containers/proxy'])
    with sandbox_extensions.build_contexts(common['path']) as contexts:
        run([engine, 'build', '-t', base, *contexts, *build_args, common['path']])
    if config.get('aws_access'):
        cli_base = stage_image(dev, '-aws-cli-base')
        run([engine, 'build', '-t', cli_base, '--build-arg', f'BASE_IMAGE={base}',
             ROOT / 'containers/aws-access/client'])
        base = cli_base
    browser = options(config.get('options'))['browser']
    has_providers = 'model_providers' in config
    web = options(config.get('options'))['web_access']['enabled']
    channels = options(config.get('options'))['communications']['slack']
    tools = stage_image(dev, '-tools') if config.get('extra_skills') or browser or web or channels or knowledge or has_providers else dev
    run([engine, 'build', '-t', tools, '--build-arg', f'BASE_IMAGE={base}', profile['path']])
    if browser:
        target = stage_image(dev, '-browser') if config.get('extra_skills') or web or channels or knowledge or has_providers else dev
        run([engine, 'build', '-t', target, '--build-arg', f'BASE_IMAGE={tools}',
             Path(common['path']) / 'browser'])
        tools = target
    if web:
        target = stage_image(dev, '-web') if config.get('extra_skills') or channels or knowledge or has_providers else dev
        run([engine, 'build', '-t', target, '--build-arg', f'BASE_IMAGE={tools}',
             Path(common['path']) / 'web-access'])
        tools = target
    if channels:
        target = stage_image(dev, '-communications') if config.get('extra_skills') or knowledge or has_providers else dev
        run([engine, 'build', '-t', target, '--build-arg', f'BASE_IMAGE={tools}',
             Path(common['path']) / 'communications'])
        tools = target
    if knowledge:
        target = stage_image(dev, '-local-knowledge') if config.get('extra_skills') or has_providers else dev
        if knowledge_mode == 'endpoint':
            run([engine, 'build', '-t', target, '--build-arg', f'BASE_IMAGE={tools}',
                 '-f', knowledge_dir / 'Dockerfile.endpoint', knowledge_dir])
        elif run([engine, 'image', 'inspect', target], capture=True, check=False,
                 stderr=subprocess.DEVNULL).returncode:
            raise ValueError('The previously bundled embedding model is missing. Choose keyword search '
                             'in Settings → Document search, then update sources to configure an embedding server.')
        tools = target
    if has_providers:
        target = stage_image(dev, '-models') if config.get('extra_skills') else dev
        model_providers.build(config, tools, target)
        tools = target
    sandbox_ssh.build(config)
    inference.build(config)
    sandbox_advisors.build(config)
    sandbox_skills.build(config, dev, SimpleNamespace(run=run, tree_digest=tree_digest), base_image=tools)


def inspect(config, name):
    host_platform.check_engine(config['engine'], config.get('podman_connection'), full=False)
    result = run([config['engine'], 'container', 'inspect', name], capture=True,
                 check=False, stderr=subprocess.DEVNULL)
    if result.returncode:
        return None
    data = json.loads(result.stdout)[0]
    if data['Config'].get('Labels', {}).get(LABEL) != config['name']:
        raise ValueError(f'Refusing to manage an unrelated container named {name}')
    return data


def describe_containers(config, states, state=None):
    dev, gateway, *_ = names(config)
    advisor, advisor_gateway, *_ = sandbox_advisors.names(config)
    roles = {
        dev: ('Development', 'Runs Pi, the editor backend and development tools.'),
        gateway: ('Network gateway', 'Lets the development container reach only allowed destinations.'),
        inference.names(config)[0]: ('Model connections', 'Connects Pi to model providers and keeps their API keys separate.'),
        aws_access.worker_name(config): ('AWS access', 'Sends requests using the AWS permissions granted to this sandbox.'),
        sandbox_ssh.names(config)[0]: ('SSH access', 'Authenticates to configured SSH hosts and keeps private keys separate.'),
        advisor: ('Advisors', 'Runs advisor tools and keeps their sign-ins separate from Pi.'),
        advisor_gateway: ('Advisor network gateway', 'Lets the advisor container reach only allowed destinations.'),
    }
    for identifier in config.get('connector_ids', []):
        title = identifier
        if state:
            try:
                plugin, = connectors.saved({'connector_ids': [identifier]}, state)
                title = plugin.title
            except ValueError:
                pass  # A damaged manifest must not hide an existing container.
        roles[connectors.worker_name(config, identifier)] = ('Connector · ' + title,
            'Sends requests for the operations granted to this sandbox.')
    selected = primary_workspace.agent(config)
    if selected != 'pi':
        from . import primary_agents
        roles[dev] = ('Workspace', 'Runs commands and holds project files and development tools.')
        roles.update(primary_agents.describe(config))
    ordered = [name for name in roles if name in states]
    ordered.extend(sorted(states.keys() - roles.keys()))
    result = []
    for name in ordered:
        role, purpose = roles.get(name, ('Additional container', 'No description is available for this container.'))
        result.append(dict(name=name, status=states[name], role=role, purpose=purpose))
    return result


def list_sandboxes(state_root, structured=False):
    from . import creation
    reservations = {path.parent.name: ('running' if operation_locks.in_use(path.parent.name) else 'interrupted')
                    for path in state_root.glob('*/' + creation.MARKER)
                    if SANDBOX_NAME.fullmatch(path.parent.name) and creation.saved_reservation(path.parent)}
    configs = []
    for path in sorted(state_root.glob('*/config.json')):
        try:
            config = primary_workspace.normalize(json.loads(path.read_text()))
            profile = config.get('profile') or {'name': 'base'}
            if (config.get('name') != path.parent.name
                    or not SANDBOX_NAME.fullmatch(config['name'])
                    or config.get('engine') not in ('podman', 'docker')
                    or not isinstance(config.get('workspace'), str)
                    or not isinstance(profile.get('name'), str)):
                raise ValueError('Invalid saved configuration')
            selected = primary_workspace.agent(config)
            configs.append((config, profile['name'] if selected == 'pi' else primary_workspace.AGENTS[selected]))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            print(f'Warning: cannot read sandbox configuration for {path.parent.name}', file=sys.stderr)
            configs.append(({'name': path.parent.name}, '-'))
    configured = {config['name'] for config, _ in configs}
    configs.extend(({'name': name}, '-') for name in sorted(reservations.keys() - configured))
    if not configs:
        print('[]' if structured else f'No saved sandboxes in {state_root}')
        return

    # Query each saved engine once. Missing containers and an unavailable engine
    # are different states; listing must remain useful when the engine is offline.
    statuses = {}
    owned = {}
    for engine in sorted({c['engine'] for c, _ in configs if 'engine' in c}):
        try:
            result = run([engine, 'ps', '--all', '--format',
                          '{{.Names}}\t{{.State}}\t{{.Label "' + LABEL + '"}}'],
                         capture=True, check=False, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise ValueError('Engine unavailable')
            containers = {}
            owners = {}
            for line in result.stdout.splitlines():
                name, status, owner = line.split('\t')
                containers[name] = (status, owner)
                if structured and owner:
                    owners.setdefault(owner, {})[name] = status
            statuses[engine] = containers
            owned[engine] = owners
        except (OSError, ValueError):
            statuses[engine] = None
            print(f'Warning: cannot query {engine}; container status is unknown', file=sys.stderr)

    inventory = []
    rows = [('NAME', 'PROFILE', 'ENGINE', 'DEV', 'PROXY', 'WORKSPACE')]
    for config, profile in configs:
        engine = config.get('engine')
        states = []
        for name in names(config)[:2]:
            if engine is None:
                status = 'invalid config'
            elif statuses[engine] is None:
                status = 'unknown'
            elif name not in statuses[engine]:
                status = 'missing'
            else:
                status, owner = statuses[engine][name]
                if owner != config['name']:
                    status = 'unrelated'
            states.append(status)
        if config.get('network') == 'unrestricted' and states[1] == 'missing':
            states[1] = 'not used'
        sandbox_name = config['name']
        reservation = reservations.get(sandbox_name)
        if reservation:
            states[0] = 'creating' if reservation == 'running' else 'creation interrupted'
        entry = dict(name=sandbox_name, profile=profile, engine=engine, dev=states[0],
                     proxy=states[1], workspace=config.get('workspace'), config=config,
                     activity=activity.load(state_root / sandbox_name),
                     primary_agent=config.get('primary_agent', 'pi'))
        if structured:
            if reservation:
                entry['creation'] = reservation
            entry['containers'] = (describe_containers(config, owned[engine].get(sandbox_name, {}), state_root / sandbox_name)
                                   if engine in owned else None)
            if states[0] == 'running' and isinstance(config.get('ssh'), dict) and sandbox_ssh.enabled(config):
                worker = sandbox_ssh.names(config)[0]
                entry['ssh_key_state'] = sandbox_ssh.key_state(
                    config, companion_running=statuses[engine].get(worker) == ('running', sandbox_name))
            if pendant_versions(config):
                entry['chat_editor'] = 'pendant'
        inventory.append(entry)
        rows.append((config['name'], profile, engine or '-', *states, config.get('workspace', '-')))
    if structured:
        print(json.dumps(inventory))
        return
    # Keep paths and other saved values from introducing terminal control codes.
    rows = [tuple(''.join(c if c.isprintable() else '?' for c in cell) for cell in row) for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    for row in rows:
        print('  '.join(cell.ljust(widths[i]) if i < 5 else cell for i, cell in enumerate(row)))


def restart_existing(config, name):
    # Podman can return an error from stop after successfully clearing stale
    # conmon state. Verify the resulting state before deciding whether to start.
    if not inspect(config, name):
        raise ValueError(f'Container disappeared during recovery: {name}')
    run([config['engine'], 'stop', '--time', '10', name], capture=True, check=False,
        stderr=subprocess.PIPE)
    current = inspect(config, name)
    if not current or current['State']['Running']:
        raise ValueError(f'Could not stop {name} safely; inspect with {config["engine"]} logs {name}')
    run([config['engine'], 'start', name], capture=True, stderr=subprocess.PIPE)


def resume_existing(config, name, current):
    if current['State']['Running']:
        # Metadata can survive a crash even though the OCI runtime is stopped.
        result = run([config['engine'], 'exec', name, 'true'], capture=True,
                     check=False, stderr=subprocess.PIPE)
        if result.returncode == 0:
            return False
        if 'not running' not in (result.stderr or '').lower():
            raise ValueError(f'Cannot execute in {name}; inspect with {config["engine"]} logs {name}. '
                             'The container was not restarted.')
        print(f'Recovering stale running state: {name}')
        restart_existing(config, name)
    else:
        run([config['engine'], 'start', name])
    return True


def ensure_gateway(config):
    try:
        wait_gateway(config)
    except ValueError:
        _, proxy, _, _ = names(config)
        print(f'Recovering unresponsive gateway: {proxy}; network requests may be interrupted')
        restart_existing(config, proxy)
        try:
            wait_gateway(config)
        except ValueError as exc:
            raise ValueError(f'{exc}. Automatic gateway recovery was attempted once; '
                             'workspace and home volumes were preserved.') from exc


def upgrade_gateway_config(state):
    """Make saved launcher configurations safe to resume after an abrupt stop."""
    path = state / 'squid.conf'
    if not path.is_file():
        return
    original = path.read_text()
    updated = re.sub(r'(?m)^pid_filename /tmp/squid\.pid$', 'pid_filename none', original)
    if updated != original:
        # Keep the inode and SELinux label: an existing container bind-mounts
        # this file, so atomic replacement would leave it seeing the old copy.
        path.write_text(updated)
        print('Updated gateway configuration for restart-safe startup.')


@contextmanager
def native_chat_lock(state):
    fd = os.open(state / 'native-chat.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def up(config, state, *, prepare_sign_in=True):
    with native_chat_lock(state):
        return _up(config, state, prepare_sign_in=prepare_sign_in)


def _up(config, state, *, prepare_sign_in=True):
    from . import primary_agents

    folders.validate_config(config, state.parent, saved=True)
    engine = config['engine']
    dev, proxy, socket_volume, home_volume = names(config)
    restricted = network_mode(config) == 'allowlist'
    native = primary_workspace.agent(config) != 'pi'
    if native:
        primary_workspace.validate_mounts(config, state.parent)
        primary_agents.check_ownership(config)
    sandbox_advisors.check_ownership(config)
    sandbox_ssh.check_ownership(config)
    inference.check_ownership(config)
    # Verify both ownership labels before starting or recovering either one.
    current = {name: inspect(config, name) for name in (proxy, dev)}
    if restricted:
        host_platform.check_model_gateway(config)
        upgrade_gateway_config(state)
        groups = network_groups.snapshots(options(config.get('options'))['website_groups'],
                                          config.get('website_groups'), download_curlie=False)
        if groups != config.get('website_groups', {}):
            config.update(update_network(config, state, (state / 'allowlist.txt').read_text(), download_curlie=False))
    active = (proxy, dev) if restricted else (dev,)
    if any(current[name] is None for name in active):
        dev_image, proxy_image = image_names(config)
        for name, tag in ((proxy, proxy_image), (dev, dev_image)):
            if name not in active:
                continue
            if current[name] is None and run([engine, 'image', 'inspect', tag], capture=True,
                                            check=False, stderr=subprocess.DEVNULL).returncode:
                raise ValueError('Images are missing; run the build command first')
    # Report blocked endpoints in an optional host seed; private models remain
    # writable inside the sandbox, and the proxy enforces their runtime policy.
    rules = parse_rules((state / 'allowlist.txt').read_text())
    if restricted and config.get('pi_config'):
        validate_models(Path(config['pi_config']), rules, strict=False)
    if host_platform.needs_mount_check(config):
        if native:
            (state / 'runtime-config').mkdir(exist_ok=True, mode=0o700)
        else:
            prepare_options(config, state)
        host_platform.check_mounts(config, state, image_names(config)[0])
    prepared_connectors = {}
    if config.get('connector_ids'):
        connector_settings.check_mount(config, state, current[dev])
        prepared_connectors = connectors.prepare(config, state)
        prior_connector_start = {**connector_settings.helper_states(config), **{name: current[name] for name in active}}
    connector_settings.refresh_agent_context(config, state)
    sandbox_advisors.up(config, state)
    sandbox_ssh.up(config, state)
    inference.up(config, state)
    if config.get('aws_access'):
        aws_access.up(config, state)
    if native:
        primary_agents.up(config, state)
    started_dev = current[dev] is None
    for name in active:
        if current[name]:
            restarted = resume_existing(config, name, current[name])
            if name == dev:
                started_dev = bool(restarted)
            if name == proxy:
                ensure_gateway(config)
            continue
        base = [engine, 'run', '-d', '--name', name, '--label', f'{LABEL}={config["name"]}',
                '--security-opt=no-new-privileges', '--pids-limit=-1']
        if name == proxy:
            args = [
                '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
                '--cap-add=DAC_OVERRIDE',
                '-v', f'{state / "squid.conf"}:/etc/squid/sandbox.conf:{host_platform.bind_mode()}',
                '-v', f'{socket_volume}:/run/egress:z',
            ]
            args += host_platform.gateway_labels()
            if config.get('local_model_gateway'):
                args += host_platform.model_gateway_args()
            if any(host == 'host.docker.internal' for host, _ in rules):
                args += ['--add-host=host.docker.internal:host-gateway']
            if config.get('aws_access'):
                args += aws_access.client_mount(state)
            run(base + args + [proxy_image])
            ensure_gateway(config)
        else:
            # Files moved into a running workspace retain their host SELinux
            # labels. Mount-time :z relabeling cannot make later imports usable.
            # Disable label separation only for the development container;
            # the gateway keeps its SELinux confinement and labeled volumes.
            args = ['--network=' + ('none' if restricted else 'bridge'), '--cap-drop=ALL',
                    # Allow ordinary echo requests without granting raw sockets.
                    '--sysctl', f'net.ipv4.ping_group_range={config["gid"]} {config["gid"]}',
                    '--security-opt=label=disable',
                    '--user', f'{config["uid"]}:{config["gid"]}',
                    '-v', f'{config["workspace"]}:/workspace:rw',
                    '-v', f'{home_volume}:/home/node']
            args += folders.mounts(config)
            args += sandbox_ssh.mounts(config, state)
            if native:
                args += primary_agents.mounts(config, state)
                primary_workspace.prepare_git(config, state)
                args += ['-v', f'{state / "git-identity.gitconfig"}:/opt/sandbox/git-identity.gitconfig:{host_platform.bind_mode()}']
            else:
                args += sandbox_advisors.mounts(config) + inference.mounts(config)
            if config.get('aws_access'):
                args += aws_access.client_mount(state)
                for key, value in aws_access.CLIENT_ENV.items():
                    args += ['--env', f'{key}={value}']
            if config.get('memory'):
                args += ['--memory=' + memory_limit(config['memory'])]
            if not native and 'options' in config:
                prepare_options(config, state)
                # The connector socket directories are also mounted in their credential containers.
                mode = 'ro,z' if config.get('connector_ids') and not host_platform.is_mac() else host_platform.bind_mode()
                args += ['-v', f'{state / "runtime-config"}:/opt/pi-security/config:{mode}',
                         '-v', f'{state / "options.json"}:/opt/sandbox/options.json:{host_platform.bind_mode()}',
                         '-v', f'{state / "git-identity.gitconfig"}:/opt/sandbox/git-identity.gitconfig:{host_platform.bind_mode()}']
            if restricted:
                args += ['-v', f'{socket_volume}:/run/egress:ro']
            if config.get('pi_config'):
                args += ['-v', f'{config["pi_config"]}:/pi-config:ro']
            if engine == 'podman':
                args += ['--userns=keep-id:uid=1000,gid=1000']
            if config.get('env_file'):
                validate_env(config['env_file'])
                args += ['--env-file', config['env_file']]
            runtime_env = RUNTIME_ENV if restricted else {'NODE_USE_ENV_PROXY': '1'}
            if config.get('local_model_gateway'):
                args += ['--env', 'LOCAL_MODEL_PORTS=' + config['local_model_ports']]
            args += ['--env', 'SANDBOX_NETWORK_MODE=' + network_mode(config)]
            selected = options(config.get('options'))
            if native:
                args += ['--env', 'SANDBOX_PROFILE=base', '--env', 'PI_SANDBOX_CONTAINER=1']
            else:
                args += ['--env', 'SANDBOX_SUBAGENTS=' + ('1' if config.get('subagents', False) else '0')]
                args += ['--env', 'SANDBOX_APPROVAL_MODE=' + selected['approval']]
                args += ['--env', 'COMPACTION_RECALL_MODE=' + ('full' if selected['compaction_recall'] == 'full' else 'lite')]
            if not native and selected['knowledge_embedding'] != 'off':
                args += ['--env', 'SANDBOX_KNOWLEDGE_ROOTS=' + json.dumps([path for path, _ in folders.roots(config)])]
            if not native and selected['knowledge_embedding'] == 'endpoint':
                args += ['--env', 'PI_KNOWLEDGE_EMBEDDING=openai:' + selected['knowledge_embedding_model'],
                         '--env', 'PI_KNOWLEDGE_EMBEDDING_BASE_URL=' + inference.embedding_client_url(config),
                         '--env', 'PI_KNOWLEDGE_EMBEDDING_API_KEY=not-needed']
                batch_size = inference.embedding_batch_size(config)
                if batch_size is not None:
                    args += ['--env', f'PI_KNOWLEDGE_EMBEDDING_BATCH_SIZE={batch_size}']
            for key, value in runtime_env.items():
                args += ['--env', f'{key}={value}']
            run(base + args + [dev_image])
    # Readiness also catches bootstrap failures (permissions, malformed settings).
    if native:
        ready = 'test -f /tmp/factory-workspace-ready'
    else:
        entrypoint = Path(config.get('common', {}).get('path', '/nonexistent')) / 'entrypoint.sh'
        marker = '/var/tmp/pi/pi-sandbox-factory-ready'
        if not entrypoint.is_file() or marker not in entrypoint.read_text():
            # Older Pi images signaled readiness by writing the editor settings.
            marker = '/home/node/.vscode-server/data/Machine/settings.json'
        ready = 'test -f ' + marker
    if restricted:
        ready += ' && test -S /run/egress/proxy.sock && echo >/dev/tcp/127.0.0.1/3128'
    for _ in range(40):
        result = run([engine, 'exec', dev, 'bash', '-c', ready], capture=True, check=False,
                     stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            if native:
                primary_agents.worker_start(config, dev)
            if options(config.get('options'))['browser']:
                browser_viewer.ensure_viewer(engine, inspect(config, dev)['Id'])
            if native and prepare_sign_in:
                primary_workspace.initial_sign_in(config, state)
            if config.get('connector_ids'):
                connector_workers = []
                try:
                    workspace = inspect(config, dev)
                    connector_settings.check_mount(config, state, workspace)
                    connector_workers = connectors.up(config, state, prepared=prepared_connectors)
                    connector_settings.check_sockets(config, workspace['Id'], config['connector_ids'])
                except (OSError, ValueError, subprocess.SubprocessError):
                    connector_settings.restore_start(config, prior_connector_start, connector_workers)
                    raise
            if started_dev:
                activity.record(state, 'started')
            print(f'Ready: {dev}; workspace /workspace; engine {engine}; network {network_mode(config)}')
            return
        time.sleep(0.25)
    raise ValueError(f'Sandbox did not become ready; inspect with {engine} logs {dev}')


def wait_gateway(config):
    _, proxy, _, _ = names(config)
    for _ in range(40):
        result = run([config['engine'], 'exec', proxy, 'bash', '-c',
                      'test -S /run/egress/proxy.sock && echo >/dev/tcp/127.0.0.1/3128'],
                     capture=True, check=False, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise ValueError(f'Gateway did not become ready; inspect with {config["engine"]} logs {proxy}')


def down(config, remove=False):
    for name in names(config)[:2]:
        inspect(config, name)
    sandbox_advisors.check_ownership(config)
    sandbox_ssh.check_ownership(config)
    inference.check_ownership(config)
    if primary_workspace.agent(config) != 'pi':
        from . import primary_agents
        primary_agents.check_ownership(config)
        primary_agents.down(config, remove=remove)
    if config.get('connector_ids'):
        connectors.down(config)
    if config.get('aws_access'):
        aws_access.down(config)
    sandbox_advisors.down(config, remove=remove)
    sandbox_ssh.down(config, remove=remove)
    inference.down(config, remove=remove)
    for name in names(config)[:2]:
        if inspect(config, name):
            run([config['engine'], 'rm', '-f', name] if remove
                else [config['engine'], 'stop', name])
    # Named volumes are deliberately kept, including sessions and editor state.


def delete(config, state):
    with native_chat_lock(state):
        return _delete(config, state)


def _delete(config, state):
    if config['name'] != state.name:
        raise ValueError('Saved sandbox name does not match its state directory')
    workspace = Path(config['workspace']).resolve()
    if workspace == state or state in workspace.parents:
        raise ValueError('Refusing to delete state containing the host workspace')
    sandbox_advisors.check_ownership(config)
    sandbox_ssh.check_ownership(config)
    inference.check_ownership(config)
    # Verify ownership of both containers before removing either one.
    for name in names(config)[:2]:
        inspect(config, name)
    # A checked listing also prevents discarding state when the engine is unavailable.
    volumes = set(run([config['engine'], 'volume', 'ls', '--format', '{{.Name}}'],
                      capture=True).stdout.splitlines())
    down(config, remove=True)
    for volume in names(config)[2:]:
        if volume in volumes:
            run([config['engine'], 'volume', 'rm', volume])
    sandbox_advisors.delete_volumes(config)
    sandbox_ssh.delete_volumes(config)
    inference.delete_volumes(config)
    if primary_workspace.agent(config) != 'pi':
        from . import primary_agents
        primary_agents.delete_volumes(config)
    shutil.rmtree(state)
    print(f'Deleted {config["name"]}: containers, private volumes, and saved state. Host workspace preserved.')


def delete_incomplete(state):
    # Creation fetches sources before writing config.json or creating containers.
    # Accept only that pre-configuration layout, not arbitrary directories.
    if not state.is_dir() or state.is_symlink():
        raise ValueError(f'No incomplete sandbox state directory: {state}')
    for entry in state.iterdir():
        if (not entry.is_dir() or entry.is_symlink()
                or not (entry.name == 'profiles' or entry.name.startswith('.profile-'))):
            raise ValueError('Missing config.json and unrecognized state contents; refusing automatic removal')
    shutil.rmtree(state)
    print(f'Deleted incomplete creation state for {state.name}. You can create it again.')


def local_model_ports(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Enter local model ports, for example 8000 or 8000-8003')
    ports = set()
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('Invalid local model port range')
        ends = list(map(int, part.split('-')))
        start, end = ends[0], ends[-1]
        if not 1 <= start <= end <= 65535:
            if end < start:
                raise ValueError('Enter the lower port first in each range.')
            raise ValueError('Enter ports from 1 to 65535.')
        ports.update(range(start, end + 1))
    return sorted(ports)


def configure_local_gateway(config, value, rules):
    config['local_model_ports'] = value or None
    config['local_model_gateway'] = bool(value)
    if not value:
        return
    if config['engine'] != 'podman' or network_mode(config) != 'allowlist':
        raise ValueError('Host-local models require rootless Podman with restricted networking')
    allowed = set(rules)
    for port in local_model_ports(value):
        if ('local-model.internal', port) not in allowed:
            raise ValueError(f'Add local-model.internal:{port} to the allowlist')


def create(args, state):
    selected = primary_workspace.agent({'primary_agent': getattr(args, 'primary_agent', 'pi') or 'pi'})
    auth_method = primary_workspace.auth({'primary_auth': getattr(args, 'primary_auth', None) or primary_workspace.DEFAULT_AUTH})
    native = selected != 'pi'
    reuse = {}
    if getattr(args, 'reuse_from', None):
        if not SANDBOX_NAME.fullmatch(args.reuse_from):
            raise ValueError('Invalid source sandbox name')
        reuse = json.loads((state.parent / args.reuse_from / 'config.json').read_text())
    prompt_file = getattr(args, 'system_prompt_file', None)
    saved_prompt = (prompt_library.prompt(json.loads(Path(prompt_file).read_text(encoding='utf-8'))) if prompt_file else
                    (prompt_library.selection(state.parent / args.reuse_from) if reuse else None))
    if native:
        profile = load_profile('base')
    elif reuse and args.profile == reuse['profile']['path']:
        profile = reuse['profile']
        if tree_digest(Path(profile['path'])) != profile['digest']:
            raise ValueError('Source profile snapshot was modified')
    else:
        profile = load_profile(args.profile)
    mode = getattr(args, 'network', 'allowlist')
    if mode == 'unrestricted' and (args.allowlist or args.allow_domain):
        raise ValueError('--network unrestricted cannot be combined with an allowlist')
    preset = profile.get('defaults', {})
    selected_options = (json.loads(Path(args.options_file).read_text()) if getattr(args, 'options_file', None)
                        else {'web_access': {'enabled': preset.get('web_search', False)}})
    if not isinstance(selected_options, dict):
        raise ValueError('Expected a JSON object of sandbox options.')
    for key in ('knowledge_embedding', 'knowledge_embedding_url', 'knowledge_embedding_model'):
        if getattr(args, key, None) is not None:
            selected_options[key] = getattr(args, key)
    if selected_options.get('knowledge_embedding') == 'local':
        raise ValueError('Choose keyword search or configure an embedding server for this new sandbox.')
    if (getattr(args, 'knowledge_embedding_url', None) or getattr(args, 'knowledge_embedding_model', None)) and selected_options.get('knowledge_embedding') != 'endpoint':
        raise ValueError('Use --knowledge-embedding endpoint with the embedding server URL and model ID.')
    selected_options = options(selected_options)
    workspace = directory(args.workspace)
    host_platform.validate_data_path(workspace)
    shared = directory(args.pi_config) if args.pi_config else None
    if host_platform.comparison_path(workspace) in (Path('/'), host_platform.comparison_path(Path.home())):
        raise ValueError('Select a project directory, not / or your home directory')
    pairs = [(workspace, state)]
    if shared:
        pairs += [(workspace, shared), (shared, state)]
    for first, second in pairs:
        if folders.overlaps(first, second):
            raise ValueError('Workspace, shared Pi config, and sandbox state must be separate, non-nested directories')
    policy_text = (Path(args.allowlist).expanduser().read_text() if args.allowlist
                   else '\n'.join(args.allow_domain) + '\n')
    rules = parse_rules(policy_text)
    if shared and mode == 'allowlist':
        validate_models(shared, rules, strict=False)
    env_file = str(Path(args.env_file).expanduser().resolve(strict=True)) if args.env_file else None
    validate_env(env_file)
    creation_token = getattr(args, 'creation_token', None)
    if creation_token:
        from .creation import reserved
        reserved(state, creation_token)
    elif state.exists():
        raise ValueError(f'Sandbox already exists: {state}; choose a new name')
    engine = choose_engine(args.engine)
    connection = host_platform.check_engine(engine)
    uid, gid = (1000, 1000) if engine == 'podman' else (os.getuid(), os.getgid())
    if engine == 'podman':
        result = run([engine, 'info', '--format', '{{.Host.Security.Rootless}}'], capture=True)
        if result.stdout.strip() != 'true':
            raise ValueError('Podman mode requires rootless Podman')
    if uid == 0:
        raise ValueError('Run the launcher as your normal user, not sudo/root')
    subagents = getattr(args, 'subagents', None)
    config = dict(name=args.name, engine=engine, uid=uid, gid=gid, workspace=str(workspace),
                  pi_config=str(shared) if shared else None, env_file=env_file, memory=memory_limit(args.memory),
                  network=mode, subagents=preset.get('subagents', False) if subagents is None else subagents,
                  primary_agent=selected, primary_auth=auth_method)
    if connection:
        config['podman_connection'] = connection
    config['working_directory'] = folders.working_directory(workspace, getattr(args, 'working_directory', '.'))
    config['extra_folders'] = folders.validate(
        json.loads(Path(args.folders_file).read_text()) if getattr(args, 'folders_file', None) else [],
        workspace, state.parent, private_paths=[shared])
    if native:
        if shared or getattr(args, 'local_model_ports', None) or getattr(args, 'model_provider', None) or getattr(args, 'advisor', []):
            raise ValueError('Pi configuration, model providers, local models and advisors apply to Pi sandboxes.')
        primary_workspace.validate_mounts(config, state.parent)
    model_ports = getattr(args, 'local_model_ports', None)
    configure_local_gateway(config, model_ports, rules)
    config['options'] = selected_options
    if getattr(args, 'browser', None) is not None:
        config['options'] = options({**config.get('options', {}), 'browser': args.browser})
    if getattr(args, 'aws_access_file', None):
        config['aws_access'] = aws_access.settings(json.loads(Path(args.aws_access_file).read_text()))
    if getattr(args, 'ssh_file', None):
        config['ssh'] = sandbox_ssh.validate(json.loads(Path(args.ssh_file).read_text()))
    ids, grants = connector_settings.parse_cli_grants(getattr(args, 'connector', None) or [])
    from .creation import resource_conflicts
    resource_conflicts({**config, 'connector_ids': ids}).require_available()
    config['website_groups'] = (network_groups.snapshots(options(config.get('options'))['website_groups'],
        curlie_revision=getattr(args, 'curlie_list', None)) if mode == 'allowlist' else {})
    if not native:
        config['advisors'] = sandbox_advisors.validate({**reuse.get('advisors', {}), 'providers': getattr(args, 'advisor', [])})
    if not creation_token:
        state.mkdir(parents=True, mode=0o700)
    try:
        reviewed_file = getattr(args, 'connector_review_file', None)
        if ids or reviewed_file:
            reviewed = json.loads(Path(reviewed_file).read_text()) if reviewed_file else None
            if reviewed is None:
                connector_settings.capture(ids, state)
            else:
                if grants:
                    raise ValueError('Specify connector permissions once, either with --connector or in the review file.')
                connector_settings.capture(ids, state, reviewed['routes'], reviewed['agent_instructions'])
                grants = reviewed['operations']
            config['connector_ids'] = ids
            if grants:
                config['connector_operations'] = grants
            connector_settings.operations(config, state)
        connectors.require_tokens(config, state)
        connector_settings.refresh_agent_context(config, state)
        if getattr(args, 'providers_file', None):
            values = json.loads(Path(args.providers_file).read_text())
        else:
            enabled_providers = getattr(args, 'model_provider', None) or (['local'] if model_ports else [])
            values = {name: {} for name in enabled_providers}
        if not native:
            config['model_providers'] = model_providers.prepare(values, state, saved=reuse.get('model_providers', []))
            model_providers.validate_network(config, rules)
        config['profile'] = snapshot_profile(profile, state)
        config['common'] = snapshot_profile(common_profile(), state)
        if saved_prompt:
            prompt_library.assign(state, saved_prompt)
        config['extra_skills'] = []
        for skill in reuse.get('extra_skills', []):
            if skill['name'] in getattr(args, 'omit_skill', []):
                continue
            source = Path(skill['path'])
            if tree_digest(source) != skill['digest']:
                raise ValueError('Source skill snapshot was modified: ' + skill['name'])
            target = state / 'skills' / skill['digest']
            if not target.exists():
                shutil.copytree(source, target)
            config['extra_skills'].append(dict(skill, path=str(target)))
        from . import skill_library
        config['extra_skills'] = skill_library.snapshot(getattr(args, 'skill', []) or [], state, config['extra_skills'])
        if getattr(args, 'skill_source', None):
            added = sandbox_skills.snapshot(args.skill_source, args.skill_path, state, SimpleNamespace(run=run, tree_digest=tree_digest))
            config['extra_skills'] = [s for s in config['extra_skills'] if s['name'] != added['name']] + [added]
    except (Exception, KeyboardInterrupt):
        shutil.rmtree(state)  # This create owns the newly created, incomplete state.
        raise
    activity.save_config(state, config)
    (state / 'allowlist.txt').write_text(policy_text)
    (state / 'squid.conf').write_text(network_policy(config, rules))
    editor_settings.save(state, {'mode': getattr(args, 'editor', None) or editor_settings.default_for_agent(selected, engine)}, record_edit=False)
    activity.record(state, 'created')
    description = f'Coding agent: {primary_workspace.AGENTS[selected]}' if native else f'Profile: {profile["name"]}'
    print(f'Created {state}\n{description}. Network: {mode}; {len(rules)} hostname/port rules. Run build, then up.')


def shell_network_policy(config, state):
    """Mirror user-approved egress; tool-owned destinations retain their review boundary."""
    policy_file = state / 'allowlist.txt'
    rules = parse_rules(policy_file.read_text()) if policy_file.is_file() else []
    allowed = {f'{host}:{port}' for host, port in rules}
    if config.get('aws_access'):
        allowed.add('aws-access.internal:19432')
        if network_mode(config) == 'unrestricted':
            # Pi's DNS guard requires a separate, port-specific address grant
            # for the fixed signer relay on the development container's loopback.
            allowed.add('127.0.0.1:19432')
    for domain in network_groups.collapse(config.get('website_groups', {})):
        names = [domain] if not domain.startswith('.') else [domain[1:], '*' + domain]
        allowed.update(f'{name}:{port}' for name in names for port in (80, 443))
    return {'allowedDomains': sorted(allowed)}


def refresh_shell_network(config, state):
    path = state / 'runtime-config/sandbox.json'
    if path.is_file():
        value = json.loads(path.read_text())
        value.setdefault('network', {}).update(shell_network_policy(config, state))
        write_json(path, value)


def prepare_options(config, state):
    from . import git_settings
    selected = options(config.get('options'))
    write_json(state / 'options.json', selected)
    identity_file = state / 'git-identity.gitconfig'
    temporary = identity_file.with_suffix('.new')
    temporary.unlink(missing_ok=True)
    with temporary.open('x') as stream:
        temporary.chmod(0o600)
        stream.write(git_settings.config(selected['git_identity']))
    temporary.replace(identity_file)
    target = state / 'runtime-config'
    target.mkdir(exist_ok=True, mode=0o700)
    profile = selected_profile(config)
    write_json(target / 'profile-system-prompt.json', {
        'text': registry.profile_system_prompt(profile['path'], profile['name']),
    })
    write_json(target / 'profile-guide.json', {
        'id': profile['name'],
        'title': profile.get('description') or profile['name'],
        'text': registry.profile_guide(profile['path']),
    })
    write_json(target / 'web-providers.json', selected['web_access'])
    inference.write_connection_checks(config, state)
    write_json(target / 'web-search.json', web_access.extension_config(selected['web_access'], network_mode(config) == 'allowlist'))
    source = Path(config['common']['path']) / 'security/config'
    for path in source.glob('*.json'):
        value = json.loads(path.read_text())
        if path.name == 'auto-review.json':
            value['model'] = selected['approval_model']
            value['interactionMode'] = 'interactive' if selected['approval'] == 'assisted' else 'automatic'
            if 'trustedModelEndpoints' in value:
                value['trustedModelEndpoints'] = inference.trusted_model_endpoints(config)
        if path.name == 'permissions.json':
            value = folders.permissions(permissions(value, selected), config)
        if path.name == 'sandbox.json':
            filesystem = value.setdefault('filesystem', {})
            filesystem['additionalWriteRoots'] = [p for p, ro in folders.roots(config) + folders.scratch_roots() if not ro]
            value.setdefault('network', {}).update(shell_network_policy(config, state))
            filesystem['protectCommonCredentialFiles'] = selected['protect_credentials']
        write_json(target / path.name, value)
    guide = target / 'security.md'
    guide.write_text(guidance(selected, config.get('subagents', False), network=network_mode(config))
                     + folders.guidance(config))
    guide.chmod(0o600)


def aws_installation(config, state, access):
    """Determine which existing containers need the AWS client mount."""
    dev, gateway = names(config)[:2]
    workspace = inspect(config, dev)
    proxy = inspect(config, gateway) if network_mode(config) == 'allowlist' else None
    saved_for_next_start = not workspace and not proxy and config.get('aws_access')
    has_mounts = aws_access.has_client_mount(workspace) and (
        network_mode(config) == 'unrestricted' or aws_access.has_client_mount(proxy))
    if saved_for_next_start or has_mounts:
        effect = 'ready'
        if proxy and proxy['State']['Running']:
            rules = parse_rules((state / 'allowlist.txt').read_text())
            if network_policy({**config, 'aws_access': access}, rules) != (state / 'squid.conf').read_text():
                effect = 'gateway'
    elif workspace and workspace['State']['Running']:
        effect = 'restart'
    elif workspace or proxy:
        effect = 'replace'
    else:
        effect = 'prepare'
    return effect, {name: data for name, data in ((dev, workspace), (gateway, proxy)) if data}


def replace_for_aws(config, updated, state, effect, containers):
    """Keep the old writable layers until the new runtime is ready."""
    engine = config['engine']
    policy = state / 'squid.conf'
    prior_policy = policy.read_text()
    next_policy = network_policy(updated, parse_rules((state / 'allowlist.txt').read_text()))
    moved = {}
    stopped = []
    if effect == 'restart':
        aws_access.up(updated, state)  # Check the host sign-in before interrupting work.
    try:
        for name, info in containers.items():
            container_id = info['Id']
            if info['State']['Running']:
                run([engine, 'stop', container_id])
                stopped.append((name, container_id))
            run([engine, 'rename', container_id, f'{name}-before-aws-{uuid4().hex}'])
            moved[name] = container_id
        policy.write_text(next_policy)
        if effect == 'restart':
            up(updated, state, prepare_sign_in=False)
            run([engine, 'exec', names(config)[0], 'sh', '-c',
                 'command -v aws >/dev/null && test -S /run/aws-access/request.sock'])
        connector_settings.refresh_agent_context(updated, state)
        activity.save_config(state, updated)
    except Exception:
        try:
            for name in names(config)[:2]:
                current = inspect(updated, name)
                old = containers.get(name, {})
                if current and current['Id'] != old.get('Id'):
                    run([engine, 'rm', '-f', current['Id']])
                if name in moved:
                    run([engine, 'rename', moved[name], name])
            policy.write_text(prior_policy)
            connector_settings.refresh_agent_context(config, state)
            refresh_shell_network(config, state)
            if not config.get('aws_access'):
                aws_access.down(updated)
            if effect == 'restart' and stopped:
                up(config, state, prepare_sign_in=False)
            else:
                for _, container_id in reversed(stopped):
                    run([engine, 'start', container_id])
            write_json(state / 'config.json', config)
        except Exception as error:
            raise ValueError('AWS setup failed and Factory could not restore the previous runtime. '
                             'Your project files and saved chats remain. See the output above.') from error
        print('AWS setup failed. Factory restored the previous containers and settings.', flush=True)
        raise
    for container_id in moved.values():
        try:
            run([engine, 'rm', container_id])
        except (OSError, subprocess.SubprocessError):
            print(f'AWS access was saved. Could not remove the previous stopped container {container_id}.')


def apply_aws_settings(config, state, value, *, approval=None, apply_gate=None):
    current = config.get('aws_access', {})
    if value == current:
        return
    updated = {**config, 'aws_access': value}
    effect, containers = aws_installation(config, state, value) if value else ('ready', {})
    if approval is not None and effect != approval:
        raise ValueError('The sandbox changed while AWS access was open. Open AWS access to review the change again.')
    if effect in ('restart', 'replace', 'prepare'):
        build(updated)
        latest_effect, latest_containers = aws_installation(config, state, value)
        if latest_effect != effect or {n: c['Id'] for n, c in latest_containers.items()} != {
                n: c['Id'] for n, c in containers.items()}:
            raise ValueError('The sandbox changed while AWS tools were building. Open AWS access to review the change again.')
        if not updating.begin_apply(apply_gate):
            return
        replace_for_aws(config, updated, state, effect, latest_containers)
        print('AWS access connected.' if effect == 'restart' else 'AWS access saved for the next start.')
        return
    if not updating.begin_apply(apply_gate):
        return
    running = containers.get(names(config)[0], {}).get('State', {}).get('Running')
    policy = state / 'squid.conf'
    previous = None
    if value and network_mode(config) == 'allowlist':
        old_policy = policy.read_text()
        updated_policy = network_policy(updated, parse_rules((state / 'allowlist.txt').read_text()))
        if updated_policy != old_policy:
            policy.write_text(updated_policy)
            previous = old_policy
    gateway = names(config)[1]
    restarting_gateway = False
    signer_changed = False
    try:
        if not value:
            workspace = inspect(config, names(config)[0])
            running = workspace and workspace['State']['Running']
            aws_access.down(config)
            signer_changed = True
        elif running:
            aws_access.up(updated, state)
            signer_changed = True
        if effect == 'gateway':
            restarting_gateway = True
            restart_existing(config, gateway)
            ensure_gateway(config)
        refresh_shell_network(updated, state)
        connector_settings.refresh_agent_context(updated, state)
        activity.save_config(state, updated)
    except Exception:
        if signer_changed and running:
            if current:
                aws_access.up(config, state)
            else:
                aws_access.down(updated)
        if previous is not None:
            policy.write_text(previous)
        if restarting_gateway:
            restart_existing(config, gateway)
            ensure_gateway(config)
        refresh_shell_network(config, state)
        connector_settings.refresh_agent_context(config, state)
        raise
    print('AWS permissions updated.' if value else 'AWS disconnected.')


def apply_settings(config, state, changes, *, apply_gate=None):
    config = primary_workspace.normalize(config)
    native = primary_workspace.agent(config) != 'pi'
    search_keys = {key: agent for agent, key in primary_workspace.WEB_SEARCH_OPTIONS.items()}
    if isinstance(changes, dict) and len(changes) == 1 and next(iter(changes)) in search_keys:
        key = next(iter(changes))
        search_agent = search_keys[key]
        if primary_workspace.agent(config) != search_agent:
            raise ValueError(f'Enable web search is available only for {primary_workspace.AGENTS[search_agent]} sandboxes.')
        selected = options({**options(config.get('options')), key: changes[key]})
        if updating.begin_apply(apply_gate):
            from . import primary_agents
            updated = {**config, 'options': selected}
            try:
                primary_agents.prepare_workspace(updated, state)
                primary_agents.write_context(updated, state)
                primary_agents.refresh_workspace_settings(updated)
                activity.save_config(state, updated)
            except Exception as error:
                try:
                    primary_agents.prepare_workspace(config, state)
                    primary_agents.write_context(config, state)
                    primary_agents.refresh_workspace_settings(config)
                except Exception as rollback:
                    raise ValueError(f'{error} Restoring the previous native settings also failed: {rollback}') from error
                raise
            print(f'Web search saved. Restart {primary_workspace.AGENTS[search_agent]} to use it.')
        return
    if native and isinstance(changes, dict) and set(changes) == {'native_approval'}:
        selected = options({**options(config.get('options')), 'native_approval': changes['native_approval']})
        if updating.begin_apply(apply_gate):
            activity.save_config(state, {**config, 'options': selected})
            print('Approval mode saved. Restart the sandbox to use it.')
        return
    if isinstance(changes, dict) and 'connector_ids' in changes:
        connector_settings.apply(config, state, changes, apply_gate)
        return
    if not native and isinstance(changes, dict) and set(changes) == {'options'}:
        previous = options(config.get('options'))
        selected = options(changes['options'])
        if {key for key in selected if selected[key] != previous[key]} == {'approval_model'}:
            changes = {'approval_model': selected['approval_model']}
    if isinstance(changes, dict) and set(changes) == {'approval_model'} and not native:
        previous = options(config.get('options'))
        selected = options({**previous, 'approval_model': changes['approval_model']})
        if not updating.begin_apply(apply_gate):
            return
        target = state / 'runtime-config' / 'auto-review.json'
        # Pi loads this trusted file when its extension starts. Its parent is mounted
        # as a directory, so an atomic replacement is visible without replacing the container.
        original = json.loads(target.read_text()) if target.is_file() else None
        if original is not None:
            write_json(target, {**original, 'model': selected['approval_model']})
        try:
            activity.save_config(state, {**config, 'options': selected})
        except Exception:
            if original is not None:
                write_json(target, original)
            raise
        print('Approval model saved.')
        return
    aws_install_approval = None
    if isinstance(changes, dict) and 'expected_aws_installation' in changes:
        if set(changes) != {'expected_aws_installation', 'expected_aws_access', 'aws_access'}:
            raise ValueError('An AWS profile update can only change AWS access.')
        changes = dict(changes)
        aws_install_approval = changes.pop('expected_aws_installation')
    if isinstance(changes, dict) and 'expected_aws_access' in changes:
        if set(changes) != {'expected_aws_access', 'aws_access'}:
            raise ValueError('An AWS profile update can only change AWS access.')
        if config.get('aws_access', {}) == changes['aws_access']:
            return
        if config.get('aws_access', {}) != changes['expected_aws_access']:
            raise ValueError('AWS access changed since this sandbox was selected. Reopen the profile to review it.')
        if aws_install_approval is None:
            # A saved profile rollout only changes permissions on prepared sandboxes.
            aws_install_approval = 'ready'
        changes = {'aws_access': changes['aws_access']}
    allowed = {'aws_access', 'working_directory', 'extra_folders', 'ssh', 'advisors', 'network', 'allowlist', 'memory', 'subagents', 'env_file', 'options', 'skill_source', 'skill_path', 'remove_skills', 'library_skills', 'local_model_ports', 'model_providers', 'curlie_list'}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError('Unknown editable sandbox setting')
    if 'aws_access' in changes:
        changes = {**changes, 'aws_access': aws_access.settings(changes['aws_access'])}
        if set(changes) == {'aws_access'}:
            apply_aws_settings(config, state, changes['aws_access'], approval=aws_install_approval, apply_gate=apply_gate)
            return
    if 'ssh' in changes and changes.keys() <= {'ssh', 'options'}:
        saved_options = options(config.get('options'))
        ssh = sandbox_ssh.validate(changes['ssh'])
        if ssh == config.get('ssh', {'hosts': []}) and options(changes.get('options', saved_options)) == saved_options:
            print('No SSH or Git settings changed.')
            return
    updated = dict(config)
    updated.update({k: v for k, v in changes.items() if k not in
                    ('allowlist', 'skill_source', 'skill_path', 'remove_skills', 'library_skills', 'curlie_list')})
    updated['working_directory'] = folders.working_directory(updated['workspace'], updated.get('working_directory', '.'))
    updated['extra_folders'] = folders.validate_config(updated, state.parent)
    if native:
        primary_workspace.validate_mounts(updated, state.parent)
    if set(changes) <= {'working_directory', 'extra_folders'} and updated['extra_folders'] == config.get('extra_folders', []):
        if not updating.begin_apply(apply_gate):
            return
        activity.save_config(state, updated)
        if native:
            from . import primary_agents
            primary_agents.prepare_workspace(updated, state)
        print('Starting folder saved. Open a new terminal to use it.' if native
              else 'Starting folder saved. Open a new Pi session or reopen VS Code to use it.')
        return
    updated['extra_skills'] = [s for s in config.get('extra_skills', []) if s['name'] not in changes.get('remove_skills', [])]
    if 'ssh' in updated:
        updated['ssh'] = sandbox_ssh.validate(updated['ssh'])
    if 'advisors' in updated:
        updated['advisors'] = sandbox_advisors.validate(updated['advisors'])
    updated['options'] = options(updated.get('options'))
    updated['website_groups'] = (network_groups.snapshots(updated['options']['website_groups'],
        config.get('website_groups'), curlie_revision=changes.get('curlie_list'))
        if network_mode(updated) == 'allowlist' else {})
    updated['memory'] = memory_limit(updated.get('memory'))
    if type(updated.get('subagents', False)) is not bool:
        raise ValueError('subagents must be a boolean')
    validate_env(updated.get('env_file'))
    old_text = (state / 'allowlist.txt').read_text()
    text = changes.get('allowlist', old_text)
    rules = parse_rules(text)
    configure_local_gateway(updated, updated.get('local_model_ports'), rules)
    if network_mode(updated) == 'unrestricted' and rules:
        raise ValueError('Unrestricted networking cannot include an active allowlist')
    from . import skill_library
    updated['extra_skills'] = skill_library.snapshot(changes.get('library_skills', []), state, updated['extra_skills'])
    if changes.get('skill_source'):
        added = sandbox_skills.snapshot(changes['skill_source'], changes.get('skill_path', '.'), state, SimpleNamespace(run=run, tree_digest=tree_digest))
        updated['extra_skills'] = [s for s in updated['extra_skills'] if s['name'] != added['name']] + [added]
    if 'model_providers' in changes and not native:
        updated['model_providers'] = model_providers.prepare(changes['model_providers'], state, saved=config.get('model_providers', []))
    if 'model_providers' in changes or 'local_model_ports' in changes:
        from .sandbox_options import check_reviewer_provider
        for removed in inference.routes(config).keys() - inference.routes(updated).keys():
            check_reviewer_provider(updated, removed)
    model_providers.validate_network(updated, rules)
    # Refresh common inputs so old sandboxes acquire configurable bootstrap.
    updated['common'] = snapshot_profile(common_profile(), state)
    build(updated)  # Leave the old running container intact if the build fails.
    if not updating.begin_apply(apply_gate):
        return
    # Verify the first AWS session while the previous workspace is still running.
    if updated.get('aws_access') and not config.get('aws_access'):
        aws_access.up(updated, state)
    print('Applying settings: recreating containers; saved workspace and home are preserved.')
    down(config, remove=True)
    activity.save_config(state, updated)
    (state / 'allowlist.txt').write_text(text)
    (state / 'squid.conf').write_text(network_policy(updated, rules))
    if text != old_text:
        activity.record(state, 'edited')
    up(updated, state)


def update_network(config, state, text, groups=None, *, download_curlie=True, curlie_revision=None):
    if network_mode(config) != 'allowlist':
        raise ValueError('Use Settings to switch to restricted networking first.')
    rules = parse_rules(text)
    configure_local_gateway(dict(config), config.get('local_model_ports'), rules)
    model_providers.validate_network(config, rules)
    proxy = names(config)[1]
    current = inspect(config, proxy)  # Verify ownership before changing live policy.
    old_text = (state / 'allowlist.txt').read_text()
    old_policy = (state / 'squid.conf').read_text()
    updated = dict(config)
    selected_options = options(config.get('options'))
    if groups is not None:
        selected_options['website_groups'] = network_groups.selected(groups)
    updated['options'] = selected_options
    updated['website_groups'] = network_groups.snapshots(selected_options['website_groups'],
        config.get('website_groups'), download_curlie=download_curlie, curlie_revision=curlie_revision)
    policy = network_policy(updated, rules)
    changed = policy != old_policy
    try:
        # Preserve the inode of the bind-mounted policy file.
        (state / 'squid.conf').write_text(policy)
        if changed and current and current['State']['Running']:
            restart_existing(config, proxy)
            ensure_gateway(config)
        (state / 'allowlist.txt').write_text(text)
        refresh_shell_network(updated, state)
        activity.save_config(state, updated)
        if old_text != text:
            activity.record(state, 'edited')
    except Exception:
        (state / 'squid.conf').write_text(old_policy)
        (state / 'allowlist.txt').write_text(old_text)
        refresh_shell_network(config, state)
        if current and current['State']['Running']:
            restart_existing(config, proxy)
            ensure_gateway(config)
        raise
    print('Network rules updated. Development container and terminals preserved; gateway connections may need to reconnect.')
    return updated


def initialize_models(config, state, preview=False):
    if 'model_providers' in config:
        model_providers.configure(config, state, 'preview' if preview else 'refresh')
        return True
    if not config.get('local_model_ports'):
        raise ValueError('Enable host-local models and choose their ports in Settings first')
    print('Discovering local models before opening an agent session…', flush=True)
    command = [config['engine'], 'exec', names(config)[0], 'node',
               '/opt/pi-local-model-manager/scripts/initialize-models.mjs']
    result = run(command + (['--preview'] if preview else []), capture=True, check=False, stderr=subprocess.PIPE)
    # Exit 3 is an expected disconnected state. Recognize the previous image's
    # exact diagnostic too, so an existing sandbox needs no disruptive rebuild.
    disconnected = result.returncode == 3 or (result.returncode == 1 and
        any(line.startswith('No usable local model was discovered.') for line in result.stderr.splitlines()))
    if result.stdout:
        print(result.stdout, end='' if result.stdout.endswith('\n') else '\n', flush=True)
    if disconnected:
        print('Model status: Not connected. The sandbox is ready; model chat is not available yet.', flush=True)
        print('Start the model server on the ports in Settings, then choose Refresh models in this manager.', flush=True)
        return False
    if result.returncode:
        raise ValueError('Model configuration failed: '+(result.stderr.strip() or f'exit {result.returncode}'))
    return True


def configure_models(config, state, preview=False):
    up(config, state)
    initialize_models(config, state, preview=preview)


def open_selected(config, state, mode=None):
    from . import browser_editor
    mode = mode or editor_settings.load(state)['mode']
    selected = primary_workspace.agent(config)
    if mode not in editor_settings.modes(selected):
        raise ValueError('Choose a client available for this coding agent in Settings → Editor.')
    with operation_locks.sandbox(state.name, 'open'):
        if selected != 'pi' or mode == 'pi':
            up(config, state)
        if mode == 'browser':
            return browser_editor.open_browser_editor(config, state, background=True)
        if selected == 'pi' and (config.get('local_model_ports') or config.get('model_providers')):
            initialize_models(config, state)
    # The interactive terminal must not hold the lock for the whole session.
    if selected != 'pi':
        from . import primary_agents
        return primary_agents.open_terminal(config, state)
    tty = ['-it'] if sys.stdin.isatty() else ['-i']
    run([config['engine'], 'exec', '--workdir', folders.container_cwd(config),
         *tty, names(config)[0], 'pi'])


def package_command(args):
    kind, action = args.command, args.package_action
    if action == 'list':
        for name, description in registry.choices(kind).items():
            print(f'{name}: {description}')
        return
    name = args.package_name
    if action == 'show':
        path = registry.resolve(kind, name)
        print(json.dumps(registry.installed(kind).get(name) or registry.manifest(kind, path), indent=2))
    elif action == 'add':
        row = registry.add(kind, name, args.source, args.ref, subdir=args.subdir)
        print(f'Installed {kind} {row["name"]} at {row["commit"] or row["digest"]}.')
    elif action == 'update':
        row = registry.update(kind, name)
        print(f'Updated {kind} {row["name"]} at {row["commit"] or row["digest"]}.')
        print('To apply it to an existing sandbox, choose Actions → Update sources and recreate…')
    elif action == 'remove':
        registry.remove(kind, name)
        print('Registration removed. Existing sandboxes and their saved snapshots are preserved.')


def main(*, startup_notice=''):
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__)
    from .updates import installed_version
    parser.add_argument('--version', action='version', version=installed_version())
    parser.add_argument('--state-dir', default=str(Path.home() / '.local/state/ai-sandbox-factory'))
    sub = parser.add_subparsers(dest='command')
    parser.set_defaults(command='tui')
    sub.add_parser('tui', help='Open the terminal manager')
    sub.add_parser('profiles', help='List available profiles')
    connector_login = sub.add_parser('connector-login', help='Save a service token for an installed HTTPS connector')
    connector_login.add_argument('connector_id')
    for kind in ('profile','provider'):
        package=sub.add_parser(kind,help='Manage installed '+kind+' packages')
        operations=package.add_subparsers(dest='package_action',required=True)
        operations.add_parser('list')
        add=operations.add_parser('add');add.add_argument('package_name');add.add_argument('source');add.add_argument('--ref')
        add.add_argument('--subdir', default='.', help='Plugin folder inside the source repository')
        for operation in ('update','remove','show'):
            item=operations.add_parser(operation);item.add_argument('package_name')
    listing = sub.add_parser('list', help='List saved sandboxes, container states, and workspace paths')
    listing.add_argument('--json', action='store_true', help='Structured saved inventory')
    new = sub.add_parser('create', help='Validate and save a new sandbox configuration')
    new.add_argument('name')
    new.add_argument('--primary-agent', choices=list(primary_workspace.AGENTS), default='pi',
                     help='Coding agent to open (default: Pi)')
    new.add_argument('--primary-auth', choices=['copy', 'login'], default=primary_workspace.DEFAULT_AUTH,
                     help='Copy the host sign-in (default), or sign in inside the sandbox')
    new.add_argument('--profile', default='base', help='Installed profile name or custom profile directory (default: base)')
    new.add_argument('--workspace', required=True)
    new.add_argument('--creation-token', help=argparse.SUPPRESS)
    new.add_argument('--working-directory', default='.', help='Starting folder relative to the workspace')
    new.add_argument('--folders-file', help='JSON list of additional host folders: source, name, read_only')
    new.add_argument('--reuse-from', help='Reuse saved provider and skill packages and advisor settings from NAME; other settings use the supplied flags')
    new.add_argument('--omit-skill', action='append', default=[], help='Omit an added skill when using --reuse-from')
    new.add_argument('--model-provider', action='append', help='Installed provider name; repeat for multiple sources (default: local)')
    new.add_argument('--providers-file', help='JSON object mapping provider names to non-secret settings')
    new.add_argument('--pi-config',
                     help='Optional Pi agent directory: seed private models/settings/auth; share other resources read-only. Default: fresh private configuration')
    new.add_argument('--network', choices=['allowlist', 'unrestricted'], default='allowlist',
                     help='Outer container network policy (default: allowlist); unrestricted uses ordinary bridge networking')
    new.add_argument('--subagents', action=argparse.BooleanOptionalAction, default=None,
                     help='Enable or disable standard pi-subagents (defaults to the profile choice)')
    new.add_argument('--knowledge-embedding', choices=['off', 'endpoint'],
                     help='Use keyword search (default: off) or install pi-knowledge with an embedding server')
    new.add_argument('--knowledge-embedding-url', help='OpenAI-compatible embedding API base URL, including its API path')
    new.add_argument('--knowledge-embedding-model', help='Model ID accepted by the embedding server')
    new.add_argument('--browser', action=argparse.BooleanOptionalAction, default=None,
                     help='Enable or disable Betterwright browser tools (default: enabled; preserves an explicit options-file choice)')
    policy = new.add_mutually_exclusive_group()
    policy.add_argument('--allowlist')
    policy.add_argument('--allow-domain', action='append', default=[], metavar='HOST:PORT',
                        help='Allow an exact hostname and port, whether it resolves publicly or privately; repeat as needed')
    new.add_argument('--env-file')
    new.add_argument('--engine', choices=['auto', 'podman', 'docker'], default='auto')
    new.add_argument('--memory', default=None, help='Optional memory limit, e.g. 8g; default: no explicit limit')
    new.add_argument('--skill', action='append', default=[], help='Skill library identifier; repeat to select several')
    new.add_argument('--skill-source', help='Additional skill Git repository URL')
    new.add_argument('--skill-path', default='.', help='Skill folder in the additional repository')
    new.add_argument('--advisor', action='append', choices=['codex', 'claude', 'agy'], default=[],
                     help='Add an advisor companion during creation; repeat for multiple advisors, sign in afterwards')
    new.add_argument('--aws-access-file', help='JSON host AWS profile, role, region and sandbox permissions')
    new.add_argument('--connector', action='append', metavar='ID[:OPERATION,...]',
                     help='Grant all requests from an installed HTTPS connector, or list the allowed requests, '
                          'for example build-status:latest,test-results. Repeat for another connector.')
    new.add_argument('--connector-review-file', help=argparse.SUPPRESS)
    new.add_argument('--ssh-file', help='JSON SSH destinations and trusted host public keys')
    new.add_argument('--options-file', help='JSON agent, skill and MCP settings')
    new.add_argument('--curlie-list', help=argparse.SUPPRESS)
    new.add_argument('--system-prompt-file', help='JSON file containing a system prompt name and text')
    new.add_argument('--start', action='store_true', help='Build images and start after creation')
    new.add_argument('--local-model-ports', help='Host-loopback model ports, e.g. 8000 or 8000-8003; rootless Podman only')
    new.add_argument('--editor', choices=editor_settings.MODES, help='Client to open (default: browser on supported hosts)')
    new.add_argument('--open', action='store_true', help='Open the selected editor after startup; requires --start')
    descriptions = {
        'install-system-packages': 'Install Debian packages in a running sandbox',
        'browser-editor': 'Open the browser editor (rootless Podman)',
        'update': 'Apply settings to a saved sandbox',
        'up': 'Resume containers and recover stale runtime or gateway state',
        'delete': 'Remove both containers, private volumes, and saved state; keep the host workspace',
        'configure': 'Check or refresh enabled providers',
        'advisor-import': 'Copy a host sign-in into the advisor container',
        'ssh-generate-key': 'Generate a repository key inside the SSH companion',
        'ssh-public-key': 'Show the unlocked SSH public key',
    }
    for command in ('update', 'install-system-packages', 'build', 'up', 'down', 'delete', 'recreate', 'open', 'configure', 'shell', 'pi', 'exec', 'check', 'policy', 'network', 'web-key', 'communication-key', 'advisor-login', 'advisor-import', 'advisor-check', 'primary-login', 'primary-import', 'ssh-import-key', 'ssh-unlock', 'ssh-generate-key', 'ssh-public-key', 'browser-editor'):
        cmd = sub.add_parser(command, **({'help': descriptions[command]} if command in descriptions else {}))
        cmd.add_argument('name')
        if command in ('update', 'recreate', 'install-system-packages'):
            cmd.add_argument('--apply-gate', type=Path, help=argparse.SUPPRESS)
        if command == 'install-system-packages':
            cmd.add_argument('packages', nargs='+', help='Debian package names')
        if command == 'browser-editor':
            cmd.add_argument('--archive', help='Use a downloaded copy of the pinned code-server archive')
            cmd.add_argument('--background', action='store_true', help='Keep the editor running after this command exits')
            cmd.add_argument('--no-open', action='store_true', help='Print the address without opening a browser tab')
            cmd.add_argument('--stop', action='store_true', help='Stop only the browser editor')
        if command == 'open':
            cmd.add_argument('--editor', choices=editor_settings.MODES, help='Open with a different client this time')
        if command == 'ssh-import-key':
            cmd.add_argument('--key-file', required=True)
        if command in ('ssh-import-key', 'ssh-unlock'):
            cmd.add_argument('--duration', default=sandbox_ssh.DEFAULT_UNLOCK_DURATION,
                             help='How long to unlock the key, e.g. 8h or 2d (default: 8h)')
        if command in ('advisor-login', 'advisor-import'):
            cmd.add_argument('provider', choices=['codex', 'claude', 'agy'])
        if command == 'communication-key':
            cmd.add_argument('--keys-stdin', action='store_true', help=argparse.SUPPRESS)
            cmd.add_argument('--status', action='store_true', help='Show Slack token status')
        if command == 'web-key':
            cmd.add_argument('--provider', choices=list(web_access.SERVICES))
            cmd.add_argument('--keys-stdin', action='store_true', help=argparse.SUPPRESS)
            cmd.add_argument('--export-keys', action='store_true', help=argparse.SUPPRESS)
            cmd.add_argument('--status', action='store_true', help='Show saved-key status without revealing keys')
            cmd.add_argument('--key-stdin', action='store_true', help='Read a key from stdin instead of a masked prompt')
        if command == 'network':
            cmd.add_argument('--allowlist', required=True)
            cmd.add_argument('--website-groups', nargs='*', choices=list(network_groups.catalog()), default=None)
            cmd.add_argument('--curlie-list', help=argparse.SUPPRESS)
        if command == 'configure':
            cmd.add_argument('--preview', action='store_true', help='Read-only model discovery')
            cmd.add_argument('--provider', help='Configure one selected provider')
            cmd.add_argument('--key', action='store_true', help='Request a credential at a masked terminal prompt')
            cmd.add_argument('--key-stdin', action='store_true', help='Read a credential from stdin')
            cmd.add_argument('--status', action='store_true', help='Read provider status')
        if command == 'update':
            cmd.add_argument('--settings-file', required=True, help='JSON object with editable saved settings')
        if command in ('exec', 'pi'):
            cmd.add_argument('args', nargs=argparse.REMAINDER)
        if command == 'check':
            cmd.add_argument('--allowed-url', required=True)
        if command == 'policy':
            cmd.add_argument('--network', choices=['allowlist', 'unrestricted'], default='allowlist')
            cmd.add_argument('--allowlist')
        if command == 'recreate':
            cmd.add_argument('--profile', help='Select and build a new profile before replacing containers')
            cmd.add_argument('--browser', action=argparse.BooleanOptionalAction, default=None,
                             help='Enable or disable Betterwright; omitted preserves the saved choice')
            cmd.add_argument('--subagents', action=argparse.BooleanOptionalAction, default=None,
                             help='Enable or disable optional subagent orchestration; omitted preserves the saved choice')
    args = parser.parse_args()
    if startup_notice and args.command != 'tui':
        print(startup_notice, file=sys.stderr)
    if args.command == 'connector-login':
        connector_settings.login(args.connector_id)
        return
    if args.command in ('profile', 'provider'):
        try:
            package_command(args)
        except subprocess.CalledProcessError as error:
            raise SystemExit(f'Error: Git exited with status {error.returncode}. See the Git message above. '
                             'Check the saved repository access and revision.') from None
        except (OSError, ValueError) as error:
            raise SystemExit(f'Error: {error}') from None
        return
    if args.command == 'tui':
        from .sandbox_tui import launch
        state_root = Path(args.state_dir).expanduser().resolve()
        launch(state_root, startup_notice=startup_notice) if startup_notice else launch(state_root)
        return
    if args.command == 'list':
        list_sandboxes(Path(args.state_dir).expanduser().resolve(), **({'structured': True} if args.json else {}))
        return
    if args.command == 'profiles':
        for name,description in registry.choices('profile').items(): print(f'{name}: {description}')
        return
    if not SANDBOX_NAME.fullmatch(args.name):
        parser.error(SANDBOX_NAME_ERROR)
    state = Path(args.state_dir).expanduser().resolve() / args.name
    if args.command in ('create', 'build', 'up', 'down', 'delete', 'update', 'recreate', 'network', 'policy', 'install-system-packages',
                        'configure', 'primary-login', 'primary-import'):
        with operation_locks.sandbox(args.name, args.command):
            sandbox_command(args, state, parser)
        if args.command == 'create' and args.open:
            open_selected(json.loads((state / 'config.json').read_text()), state)
        return
    return sandbox_command(args, state, parser)


def sandbox_command(args, state, parser):
    if args.command == 'create':
        if args.open and not args.start:
            parser.error('--open requires --start')
        create(args, state)
        if args.start:
            config = json.loads((state / 'config.json').read_text())
            build(config)
            up(config, state)
            if not args.open and (config.get('local_model_ports') or config.get('model_providers')):
                initialize_models(config, state)
        return
    if args.command == 'delete':
        from . import creation
        reservation = creation.saved_reservation(state)
        if reservation and not os.path.lexists(state / creation.READY):
            if not creation.owns(state, reservation):
                raise ValueError('Creation ownership changed; no resources were removed.')
            shutil.rmtree(state)
            print(f'Deleted incomplete creation state for {state.name}. You can create it again.')
            return
        if not (state / 'config.json').exists():
            delete_incomplete(state)
            return
    config = json.loads((state / 'config.json').read_text())
    native = primary_workspace.agent(config) != 'pi'
    if native and args.command in ('configure', 'web-key', 'communication-key', 'advisor-login', 'advisor-import', 'advisor-check'):
        raise ValueError('This command applies to Pi sandboxes. Use open to launch the selected coding agent.')
    if native and args.command == 'pi':
        open_selected(config, state, 'pi')
        return
    if args.command == 'build':
        if not config.get('common'):
            config['common'] = snapshot_profile(common_profile(), state)
            activity.save_config(state, config)
        build(config)
    elif args.command == 'ssh-import-key':
        sandbox_ssh.import_key(config, state, args.key_file, args.duration)
        activity.record(state, 'edited')
    elif args.command in ('ssh-generate-key', 'ssh-public-key'):
        print(sandbox_ssh.repository_key(config, state, generate=args.command == 'ssh-generate-key'))
    elif args.command == 'ssh-unlock':
        sandbox_ssh.unlock(config, state, args.duration)
    elif args.command == 'advisor-check':
        sandbox_advisors.check(config, state)
    elif args.command == 'advisor-import':
        from .advisor_credentials import import_from_host, LABELS
        import_from_host(config, state, args.provider)
        activity.record(state, 'edited')
        print(f'{LABELS[args.provider]} host sign-in copied into the advisor container.')
    elif args.command == 'advisor-login':
        sandbox_advisors.login(config, state, args.provider)
    elif args.command in ('primary-login', 'primary-import'):
        from . import primary_agents
        if not native:
            raise ValueError('This sandbox uses Pi. Configure model connections in Settings.')
        up(config, state, prepare_sign_in=False)
        if args.command == 'primary-login':
            primary_agents.login(config, state)
            config['primary_auth'] = 'login'
        else:
            primary_agents.copy_host(config, state)
            (state / 'primary-sign-in-copied').touch(mode=0o600)
            config['primary_auth'] = 'copy'
        activity.save_config(state, config)
    elif args.command == 'up':
        up(config, state)
    elif args.command == 'down':
        down(config)
    elif args.command == 'delete':
        delete(config, state)
    elif args.command == 'update':
        apply_settings(config, state, json.loads(Path(args.settings_file).read_text()), apply_gate=args.apply_gate)
    elif args.command == 'install-system-packages':
        try:
            system_packages.install(config, args.packages, args.apply_gate)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise SystemExit(f'Error: {error}') from None
    elif args.command == 'recreate':
        from .job_progress import Progress
        if native and (args.profile or args.subagents is not None):
            raise ValueError('Profiles and Pi subagent settings apply to Pi sandboxes.')
        building = native or args.profile or args.subagents is not None or args.browser is not None
        labels = (['Prepare sandbox sources', 'Build sandbox images'] if building else []) + [
            'Save sandbox configuration', 'Stop sandbox containers', 'Start sandbox containers']
        progress = Progress(labels)
        if args.subagents is not None:
            config['subagents'] = args.subagents
        if args.browser is not None:
            config['options'] = options({**config.get('options', {}), 'browser': args.browser})
        if building:
            with progress.stage('Prepare sandbox sources'):
                if native:
                    config['common'] = snapshot_profile(common_profile(), state)
                else:
                    profile = config.get('profile')
                    if args.profile or not profile:
                        profile = snapshot_profile(load_profile(args.profile) if args.profile else selected_profile(config), state)
                    updated = dict(config, profile=profile, common=snapshot_profile(common_profile(), state))
                    if 'model_providers' in config:
                        updated['model_providers'] = model_providers.prepare({p['name']:p['settings'] for p in config['model_providers']}, state, saved=config['model_providers'], refresh=True)
                    config = updated
        with image_graph(config):
            if building:
                with progress.stage('Build sandbox images'):
                    build(config)
            if not updating.begin_apply(args.apply_gate):
                return
            with progress.stage('Save sandbox configuration'):
                activity.save_config(state, config)
            with progress.stage('Stop sandbox containers'):
                down(config, remove=True)
            with progress.stage('Start sandbox containers'):
                up(config, state)
    elif args.command == 'browser-editor':
        from . import browser_editor
        if not args.stop and not args.background:
            browser_editor.open_browser_editor(config, state, args.archive, args.background, not args.no_open)
        else:
            with operation_locks.sandbox(args.name, 'change the browser editor for'):
                if args.stop:
                    browser_editor.stop(state)
                else:
                    browser_editor.open_browser_editor(config, state, args.archive, args.background, not args.no_open)
    elif args.command == 'open':
        open_selected(config, state, args.editor)
    elif args.command == 'configure':
        if 'model_providers' not in config:
            configure_models(config, state, preview=args.preview)
        else:
            if (args.key or args.key_stdin) and not args.provider:parser.error('Choose --provider when supplying a credential')
            secret=None
            if args.key:
                import getpass
                secret=getpass.getpass('Provider key: ')
            elif args.key_stdin:
                secret=sys.stdin.read(65537).strip()
                if len(secret)>65536:parser.error('Credential input too large')
            up(config,state)
            action = 'status' if args.status else 'preview' if args.preview else 'refresh'
            try:
                model_providers.configure(config, state, action, args.provider, secret)
            except ValueError as error:
                raise SystemExit(f'Error: {error}') from None
    elif args.command == 'communication-key':
        current = inspect(config, names(config)[0])
        if not current or not current['State']['Running']:
            raise ValueError('Start this sandbox before configuring Slack credentials')
        command = [config['engine'], 'exec', '-i', names(config)[0], 'python3',
                   '/usr/local/lib/sandbox-communications.py']
        if args.status:
            payload = None
        elif args.keys_stdin:
            payload = sys.stdin.read(32769)
            if len(payload) > 32768:
                raise ValueError('Communication credentials too large')
        else:
            import getpass
            changes = {}
            for field, label in [('botToken', 'Slack bot token'), ('userToken', 'Slack user token for search')]:
                value = getpass.getpass(label + ' (empty keeps saved, - removes): ').strip()
                if value:
                    changes[field] = '' if value == '-' else value
            payload = json.dumps(changes)
        result = subprocess.run(command + (['--status'] if args.status else []),
                                input=payload, text=True, capture_output=True)
        if result.returncode:
            raise ValueError('Could not save Slack credentials; check the token format and private storage')
        if not args.status:
            activity.record(state, 'edited')
        print(result.stdout.strip() if args.status else 'Slack credentials saved.')
    elif args.command == 'web-key':
        current = inspect(config, names(config)[0])
        if not current or not current['State']['Running']:
            raise ValueError('Start this sandbox before setting its search API key')
        selected = options(config.get('options'))
        search = selected['web_access']
        command = [config['engine'], 'exec', '-i', names(config)[0], 'python3',
                   '/usr/local/lib/sandbox-web-access.py']
        if args.status or args.export_keys:
            result = subprocess.run(command + ['--export-keys' if args.export_keys else '--status'], text=True, capture_output=True)
        else:
            import getpass
            if args.keys_stdin:
                payload = sys.stdin.read(100001)
                if len(payload) > 100000:
                    raise ValueError('Search credentials too large')
            else:
                provider = args.provider or (search['mode'] if search['mode'] != 'auto' else
                    next(iter(search['providers'])) if len(search['providers']) == 1 else None)
                if provider not in search['providers'] or provider == 'searxng':
                    raise ValueError('Choose a configured search provider with --provider')
                key = sys.stdin.read(16385) if args.key_stdin else getpass.getpass('Search API key (empty removes saved key): ')
                payload = json.dumps({provider: key.strip()})
            result = subprocess.run(command + ['--keys-stdin'], input=payload, text=True, capture_output=True)
        if result.returncode:
            raise ValueError('Could not read or save search API keys; check the selected provider and key format')
        try:
            statuses = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ValueError('Could not read the sandbox search key storage') from None
        if args.status or args.export_keys:
            print(json.dumps(statuses))
        else:
            selected['web_access'] = web_access.update_key_status(search, statuses, json.loads(payload))
            updated = dict(config, options=selected)
            if network_mode(config) == 'allowlist':
                update_network(updated, state, (state / 'allowlist.txt').read_text())
            else:
                activity.save_config(state, updated)
            prepare_options(updated, state)
            result = subprocess.run(command, text=True, capture_output=True)
            if result.returncode:
                raise ValueError('Search key saved, but configuration refresh failed')
            activity.record(state, 'edited')
            print('Search keys updated. Reload an open Pi or FrostPi session.')
    elif args.command == 'network':
        update_network(config, state, Path(args.allowlist).expanduser().read_text(), args.website_groups,
                       curlie_revision=args.curlie_list)
    elif args.command == 'policy':
        if args.network == 'unrestricted' and args.allowlist:
            parser.error('--network unrestricted cannot be combined with --allowlist')
        if args.network == 'allowlist' and not args.allowlist:
            parser.error('--allowlist is required for allowlist networking')
        text = Path(args.allowlist).expanduser().read_text() if args.allowlist else ''
        rules = parse_rules(text)
        if args.network == 'allowlist' and config.get('pi_config'):
            validate_models(Path(config['pi_config']), rules, strict=False)
        down(config, remove=True)
        config['network'] = args.network
        activity.save_config(state, config)
        (state / 'allowlist.txt').write_text(text)
        (state / 'squid.conf').write_text(network_policy(config, rules))
        up(config, state)
    else:
        dev = names(config)[0]
        if args.command == 'check':
            source = (ROOT / 'checks/check_inside.py').read_text()
            run([config['engine'], 'exec', '-i', dev, 'python3', '-', args.allowed_url], input=source)
            if options(config.get('options'))['browser']:
                run([config['engine'], 'exec', dev, 'node', '/opt/betterwright/check.mjs', args.allowed_url])
        else:
            command = ['bash'] if args.command == 'shell' else getattr(args, 'args', [])
            if command[:1] == ['--']:
                command = command[1:]
            if args.command == 'pi':
                with operation_locks.sandbox(args.name, 'start Pi in'):
                    up(config, state)
                    if config.get('local_model_ports') or config.get('model_providers'):
                        initialize_models(config, state)
                command = ['pi'] + command
            if not command:
                parser.error('exec requires a command after the sandbox name')
            tty = ['-it'] if sys.stdin.isatty() else ['-i']
            run([config['engine'], 'exec', '--workdir', folders.container_cwd(config)] + tty + [dev] + command)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
