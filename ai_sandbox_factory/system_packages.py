"""Run an explicitly requested APT install in the existing workspace container."""
import re

from . import job_progress, updating
from .settings_catalog import NETWORK_PATH


INSTALL = r'''
set -eu
if ! grep -q '^CapBnd:[[:space:]]*0000000000000000$' /proc/self/status ||
   ! grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status; then
    echo 'Factory requires dropped Linux capabilities and no-new-privileges for this install.' >&2
    exit 1
fi
work=$(mktemp -d /tmp/factory-apt.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
export TMPDIR="$work"
mkdir "$work/lists" "$work/archives"
apt_get() {
    apt-get -o APT::Sandbox::User=root -o Dir::State::lists="$work/lists" \
        -o Dir::Cache::archives="$work/archives" "$@"
}
apt_get -o APT::Update::Error-Mode=any update
for package in "$@"; do
    if ! apt-cache -o Dir::State::lists="$work/lists" show --no-all-versions "$package" |
        grep -Fxq "Package: $package"; then
        echo "Package $package is unavailable in the configured APT repositories." >&2
        exit 1
    fi
done
apt_get install -y --no-install-recommends --no-remove -o DPkg::Options::=--force-confold -- "$@"
'''


def package_names(values):
    if not values:
        raise ValueError('Enter at least one package name.')
    if any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', name) or name.endswith('-') for name in values):
        raise ValueError('Enter Debian package names such as jq. Leave out commands, versions, options, URLs and paths.')
    return sorted(set(values))


def command(config, container, packages):
    from . import sandbox

    result = [config['engine'], 'exec', '--user', '0:0', '--workdir', '/']
    # Clear inherited variables before launching even the environment wrapper.
    # In particular, the root process must not load a workspace-supplied library.
    for entry in container['Config'].get('Env') or ():
        key = entry.split('=', 1)[0]
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            result += ['--env', key + '=']
    environment = ['PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'HOME=/root', 'TMPDIR=/tmp',
                   'LC_ALL=C', 'DEBIAN_FRONTEND=noninteractive']
    if sandbox.network_mode(config) == 'allowlist':
        environment += [key + '=' + sandbox.RUNTIME_ENV[key] for key in ('http_proxy', 'https_proxy', 'no_proxy')]
    return [*result, container['Id'], '/usr/bin/env', '-i', *environment,
            '/bin/sh', '-c', INSTALL, 'factory-apt', *packages]


def install(config, requested, apply_gate):
    from . import sandbox

    progress = job_progress.Progress(('Check running sandbox', 'Install packages with APT'))
    with progress.stage('Check running sandbox'):
        packages = package_names(requested)
        container = sandbox.inspect(config, sandbox.names(config)[0])
        if not container or not container['State']['Running']:
            raise ValueError('Start the sandbox, then choose Install system packages again.')
    if not updating.begin_apply(apply_gate):
        return
    with progress.stage('Install packages with APT'):
        result = sandbox.run(command(config, container, packages), check=False)
        if result.returncode:
            advice = (f' If the gateway blocked a repository, allow its hostname and port in {NETWORK_PATH} and retry.'
                      if sandbox.network_mode(config) == 'allowlist' else '')
            raise ValueError(f'APT could not install {", ".join(packages)} (exit {result.returncode}). '
                             f'See the package manager output above.{advice}')
    print(f'System packages ready in the running sandbox: {", ".join(packages)}.')
