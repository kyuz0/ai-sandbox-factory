"""Build and configure the development container for native coding agents."""

from contextlib import suppress
import fcntl
import hashlib
from pathlib import Path
import os
import subprocess
import tempfile

from . import git_settings
from .sandbox_options import options

AGENTS = {
    'pi': 'Pi',
    'codex': 'Codex',
    'claude': 'Claude Code',
    'agy': 'Antigravity',
}
DEFAULT_AUTH = 'copy'
WEB_SEARCH_OPTIONS = {'codex': 'codex_web_search', 'claude': 'claude_web_search', 'agy': 'agy_web_search'}


def agent(config):
    value = config.get('primary_agent', 'pi')
    if not isinstance(value, str) or value not in AGENTS:
        raise ValueError('Choose Pi, Codex, Claude Code, or Antigravity.')
    return value


def auth(config):
    value = config.get('primary_auth', 'login')
    if value not in ('login', 'copy'):
        raise ValueError('Choose a new sign-in or copy your host sign-in.')
    return value


def normalize(config):
    """Older saved sandboxes always use Pi. No image or container changes are required."""
    selected = agent(config)
    method = auth(config)
    return {**config, 'primary_agent': selected, 'primary_auth': method}


def validate_mounts(config, state_root):
    from . import folders

    home = Path.home()
    protected = [
        state_root, os.environ.get('CODEX_HOME', home / '.codex'),
        os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'),
        home / '.gemini', home / '.local/share/keyrings',
    ]
    paths = [config['workspace'], *(row['source'] for row in config.get('extra_folders', []))]
    env_file = config.get('env_file')
    if env_file:
        paths.append(env_file)
    if any(folders.overlaps(Path(path), Path(private)) for path in paths for private in protected):
        raise ValueError('Choose workspace files and folders outside Factory state and stored coding-agent sign-ins.')


def initial_sign_in(config, state):
    if auth(config) != 'copy':
        return
    from . import primary_agents

    copied = state / 'primary-sign-in-copied'
    with (state / 'primary-sign-in.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not copied.exists():
            primary_agents.copy_host(config, state)
            copied.touch(mode=0o600)


def prepare_git(config, state):
    selected = options(config.get('options'))
    path = state / 'git-identity.gitconfig'
    fd, name = tempfile.mkstemp(prefix='.git-identity-', dir=state)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(git_settings.config(selected['git_identity']))
        os.replace(name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)


def image(config):
    from . import sandbox
    source = sandbox.ROOT / 'containers/primary'
    worker_files = ['worker.sh', 'worker-tools.mjs']
    if agent(config) == 'codex':
        # The official Codex binaries are copied from the credential image.
        worker_files.append('Dockerfile')
        worker_files.append('codex_client.py')
    elif agent(config) in ('claude', 'agy'):
        worker_files.extend(['Dockerfile', agent(config) + '_client.py'])
    digest = hashlib.sha256()
    for filename in worker_files:
        digest.update((source / filename).read_bytes())
    digest.update(sandbox.tree_digest(Path(config.get('common', {}).get('path', sandbox.ROOT / 'containers/dev')) / 'cli-tools').encode())
    worker_source = sandbox.tree_digest(sandbox.ROOT / 'containers/primary-worker')
    identity = f'{agent(config)}-{config["uid"]}-{config["gid"]}-{worker_source}-{digest.hexdigest()[:16]}'
    if config.get('profile'):
        identity += '-profile-' + config['profile']['digest']
    selected = options(config.get('options'))
    if selected['browser']:
        common = Path(config['common']['path'])
        identity += '-browser-' + '-'.join(sandbox.tree_digest(common / part) for part in ('browser', 'security'))
    if config.get('aws_access'):
        identity += '-aws-' + sandbox.tree_digest(sandbox.ROOT / 'containers/aws-access/client')
    if config.get('extra_skills'):
        skills = '\n'.join(s['name'] + ':' + s['digest'] for s in config['extra_skills'])
        identity += '-skills-' + hashlib.sha256(skills.encode()).hexdigest()[:16]
    return sandbox.stage_image(f'localhost/pi-sandbox-factory-workspace:{identity}')


def build(config, target):
    from . import sandbox, primary_agents, sandbox_ssh, sandbox_skills
    from types import SimpleNamespace

    engine = config['engine']
    root = sandbox.ROOT / 'containers/primary-worker'
    common = Path(config['common']['path'])
    primary_agents.build(config)
    sandbox_ssh.build(config)
    for skill in config.get('extra_skills', []):
        if sandbox.tree_digest(Path(skill['path'])) != skill['digest']:
            raise ValueError('Additional skill snapshot was modified; add its remote source again')
    from . import image_builds
    worker = image_builds.native_common(config, sandbox.stage_image(target, '-base'))
    if config.get('profile'):
        profile_image = sandbox.stage_image(target, '-profile')
        sandbox.run([engine, 'build', '-t', profile_image, '--build-arg', f'BASE_IMAGE={worker}',
                     config['profile']['path']])
        worker = profile_image
    if agent(config) in ('codex', 'claude', 'agy'):
        native = sandbox.stage_image(target, '-' + agent(config))
        sandbox.run([engine, 'build', '-t', native, '--build-arg', f'BASE_IMAGE={worker}',
                     *primary_agents.worker_context(config), '-f', root / ('Dockerfile.' + agent(config)), root])
        worker = native
    if options(config.get('options'))['browser']:
        with_sources = sandbox.stage_image(target, '-browser-source')
        sandbox.run([engine, 'build', '-t', with_sources, '--build-arg', f'BASE_IMAGE={worker}',
                     '--build-context', f'primary-dev={common}', '-f', root / 'Dockerfile.browser', root])
        browser = sandbox.stage_image(target, '-browser')
        sandbox.run([engine, 'build', '-t', browser, '--build-arg', f'BASE_IMAGE={with_sources}', common / 'browser'])
        worker = browser
    if config.get('aws_access'):
        aws = sandbox.stage_image(target, '-aws')
        sandbox.run([engine, 'build', '-t', aws, '--build-arg', f'BASE_IMAGE={worker}',
                     sandbox.ROOT / 'containers/aws-access/client'])
        worker = aws
    if config.get('extra_skills'):
        sandbox_skills.build(config, target, SimpleNamespace(run=sandbox.run, tree_digest=sandbox.tree_digest),
                             base_image=worker)
    else:
        sandbox.run([engine, 'tag', worker, target])
