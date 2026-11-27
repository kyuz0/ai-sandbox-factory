"""Check or update the declared inputs of Factory's public container images."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
AGENTS = 'ai_sandbox_factory/containers/primary/agent-versions.json'
CLAUDE = 'https://downloads.claude.ai/claude-code-releases'
AGY = 'https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests'


def request(url, *, json_result=True):
    headers = {'User-Agent': 'ai-sandbox-factory-maintenance', 'Accept': 'application/json'}
    if url.startswith('https://api.github.com/') and os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            data = response.read()
        return json.loads(data) if json_result else data.decode().strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace').strip()
        secret = os.environ.get('GITHUB_TOKEN', '')
        if secret:
            detail = detail.replace(secret, '[redacted]')
        raise ValueError(f'Cannot read update metadata from {url}: HTTP {error.code} {error.reason}; '
                         f'{detail or "the upstream returned no diagnostic body"}. Retry the maintenance check.') from error
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read update metadata from {url}: {error}. Retry the maintenance check.') from error


def stable(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError(f'Expected a stable version such as 1.2.3, received {value!r}')
    return value


def npm_version(package, major=None):
    url = 'https://registry.npmjs.org/' + urllib.parse.quote(package, safe='')
    if major is None:
        return stable(request(url + '/latest')['version'])
    # npm 12 changes native install-script and Git-dependency behavior. Keep the
    # supported installer family until those build contracts are adapted/tested.
    releases = request(url)['versions']
    versions = [value for value in releases if re.fullmatch(re.escape(str(major)) + r'\.\d+\.\d+', value)]
    if not versions:
        raise ValueError(f'{package}: no stable release in the supported major version {major}')
    return max(versions,key=lambda value:tuple(map(int,value.split('.'))))


def newer(candidate, current):
    stable(candidate)
    stable(current)
    return tuple(map(int, candidate.split('.'))) > tuple(map(int, current.split('.')))


class Edits:
    def __init__(self, root):
        self.root, self.files = Path(root), {}

    def read(self, name):
        return self.files.get(name, (self.root / name).read_text())

    def write(self, name, text):
        self.files[name] = text

    def json(self, name):
        return json.loads(self.read(name))

    def replace(self, name, pattern, replacement):
        text, count = re.subn(pattern, replacement, self.read(name), flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f'{name}: expected one managed pin matching {pattern!r}, found {count}. Update the pin resolver.')
        self.write(name, text)

    def save(self):
        # A component is resolved in memory first. A failed resolver cannot leave
        # its version updated without the associated hashes or lockfile.
        for name, text in self.files.items():
            path = self.root / name
            temporary = path.with_name(path.name + '.maintenance-tmp')
            temporary.write_text(text)
            temporary.replace(path)


def agent(edits, name):
    pins = edits.json(AGENTS)
    old = pins[name]['version']
    if name == 'codex':
        pin = {'version': npm_version('@openai/codex')}
    elif name == 'claude':
        version = stable(request(CLAUDE + '/latest', json_result=False))
        manifest = request(f'{CLAUDE}/{version}/manifest.json')
        pin = {'version': version, 'platforms': {}}
        for arch, platform in [('amd64', 'linux-x64'), ('arm64', 'linux-arm64')]:
            checksum = manifest['platforms'][platform]['checksum']
            if not re.fullmatch('[a-f0-9]{64}', checksum):
                raise ValueError(f'Claude {version}: invalid {platform} SHA-256 {checksum!r}')
            pin['platforms'][arch] = {'url': f'{CLAUDE}/{version}/{platform}/claude', 'sha256': checksum}
    else:
        pin = {'platforms': {}}
        for arch in ('amd64', 'arm64'):
            manifest = request(f'{AGY}/linux_{arch}.json')
            version, url, checksum = stable(manifest['version']), manifest['url'], manifest['sha512']
            if (not isinstance(url, str) or not url.startswith('https://storage.googleapis.com/antigravity-public/antigravity-cli/')
                    or not re.fullmatch('[a-f0-9]{128}', checksum)):
                raise ValueError(f'Antigravity {arch}: invalid archive URL or SHA-512 in the vendor manifest')
            if pin.get('version', version) != version:
                raise ValueError('Antigravity published different stable versions for amd64 and arm64; retry after its release completes')
            pin['version'] = version
            pin['platforms'][arch] = {'url': url, 'sha512': checksum}
    latest = pin['version']
    if newer(latest, old):
        pins[name] = pin
        edits.write(AGENTS, json.dumps(pins, indent=2) + '\n')
    return {'current': old, 'available': latest, 'changed': newer(latest, old)}


def pi(edits):
    docker = 'ai_sandbox_factory/containers/dev/Dockerfile'
    version = re.search(r'^ARG PI_VERSION=(\S+)$', edits.read(docker), re.M).group(1)
    python = 'ai_sandbox_factory/sandbox.py'
    if f"PI_VERSION = '{version}'" not in edits.read(python):
        raise ValueError('Pi version differs between the common Dockerfile and sandbox.PI_VERSION; reconcile both before updating')
    latest = npm_version('@earendil-works/pi-coding-agent')
    if newer(latest, version):
        edits.replace(docker, r'^ARG PI_VERSION=\S+$', f'ARG PI_VERSION={latest}')
        edits.replace(python, r"^PI_VERSION = '[^']+'$", f"PI_VERSION = '{latest}'")
    return {'current': version, 'available': latest, 'changed': newer(latest, version)}


def npm_packages(edits, *, apply):
    changed = []
    for path in sorted((edits.root / 'ai_sandbox_factory').rglob('package-lock.json')):
        if 'node_modules' in path.parts:
            continue
        name = str(path.with_name('package.json').relative_to(edits.root))
        package = edits.json(name)
        modifications = []
        for group in ('dependencies', 'devDependencies'):
            for dependency, current in package.get(group, {}).items():
                # Local packages and intentional compatibility ranges retain
                # their own source/peer contracts; npm ci resolves their locks.
                if not re.fullmatch(r'\d+\.\d+\.\d+', current):
                    continue
                latest = npm_version(dependency)
                if newer(latest, current):
                    package[group][dependency] = latest
                    modifications.append(f'{dependency}: {current} → {latest}')
        changed.extend(modifications)
        if apply:
            with tempfile.TemporaryDirectory(prefix='factory-npm-lock-') as temporary:
                folder = Path(temporary)
                (folder / 'package.json').write_text(json.dumps(package, indent=2) + '\n')
                (folder / 'package-lock.json').write_text(path.read_text())
                # npm update advances compatible transitive dependencies even
                # when a direct package did not publish a new version.
                subprocess.run(['npm', 'update', '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund',
                                '--registry=https://registry.npmjs.org'], cwd=folder, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                before = json.loads(path.read_text())
                after = json.loads((folder / 'package-lock.json').read_text())
                def installed(value):
                    return {name: {key: entry.get(key) for key in ('version','resolved','integrity','dependencies','optionalDependencies')}
                            for name, entry in value.get('packages', {}).items()}
                if modifications or installed(before) != installed(after):
                    if not modifications:
                        changed.append(name + ': compatible dependency lock refreshed')
                    edits.write(name, (folder / 'package.json').read_text())
                    edits.write(str(path.relative_to(edits.root)), (folder / 'package-lock.json').read_text())
    return {'current': 'locked packages', 'available': changed or 'direct versions unchanged; compatible locks refreshed weekly', 'changed': bool(changed)}


def base_images(edits):
    changes = []
    resolved = {}
    for path in sorted((edits.root / 'ai_sandbox_factory').rglob('Dockerfile*')):
        if 'node_modules' in path.parts or '/profile-authoring/example/' in str(path):
            continue
        name = str(path.relative_to(edits.root))
        text = edits.read(name)
        stages = {name.lower() for name in re.findall(r'^FROM\s+\S+\s+AS\s+(\S+)', text, re.M | re.I)}
        def resolve(match):
            prefix, reference = match.groups()
            if reference.startswith('$') or reference.lower() in stages:
                return match.group()
            tag = reference.split('@')[0]
            if tag not in resolved:
                raw = subprocess.check_output(['skopeo', 'inspect', '--raw', 'docker://' + tag], stderr=subprocess.PIPE)
                resolved[tag] = 'sha256:' + hashlib.sha256(raw).hexdigest()
            digest = resolved[tag]
            result = tag + '@' + digest
            if result != reference:
                changes.append(f'{tag}: {digest}')
            return prefix + result
        revised = re.sub(r'^(FROM\s+)(\S+)', resolve, text, flags=re.M)
        if revised != text:
            edits.write(name, revised)
    return {'current': 'declared base tags', 'available': list(dict.fromkeys(changes)) or 'unchanged', 'changed': bool(changes)}


def refresh_os_cache(edits):
    # An independent, attempt-specific marker also covers a second update on
    # the same day or a partial update whose base registry is unavailable.
    run = os.environ.get('GITHUB_RUN_ID')
    marker = run + '-' + os.environ.get('GITHUB_RUN_ATTEMPT','1') if run else datetime.now(timezone.utc).isoformat()
    for path in (edits.root/'ai_sandbox_factory').rglob('Dockerfile*'):
        if 'node_modules' in path.parts:
            continue
        name=str(path.relative_to(edits.root))
        before=edits.read(name)
        after=re.sub(r'^ARG FACTORY_APT_REFRESH=.*$', 'ARG FACTORY_APT_REFRESH=' + marker,before,flags=re.M)
        if before != after:
            edits.write(name,after)


def editor(edits):
    name = 'ai_sandbox_factory/browser_editor.py'
    old = re.search(r"^VERSION = '([^']+)'", edits.read(name), re.M).group(1)
    release = request('https://api.github.com/repos/coder/code-server/releases/latest')
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('code-server latest release is not a stable published release')
    latest = stable(release['tag_name'].removeprefix('v'))
    if newer(latest, old):
        checksums = {}
        for arch, field in [('amd64', 'SHA256'), ('arm64', 'ARM64_SHA256')]:
            asset = next(asset for asset in release['assets'] if asset['name'] == f'code-server-{latest}-linux-{arch}.tar.gz')
            checksum = asset.get('digest', '')
            if not re.fullmatch('sha256:[a-f0-9]{64}', checksum):
                raise ValueError(f'code-server {latest} {arch}: GitHub did not publish the archive SHA-256')
            checksums[field] = checksum.split(':')[1]
        edits.replace(name, r"^VERSION = '[^']+'$", f"VERSION = '{latest}'")
        for field, checksum in checksums.items():
            edits.replace(name, rf"^{field} = '[^']+'$", f"{field} = '{checksum}'")
    return {'current': old, 'available': latest, 'changed': newer(latest, old)}


def bundled_installers(edits):
    changes = []
    for argument, package in (('NPM_VERSION','npm'),('PIP_VERSION','pip')):
        def version(value):
            if package == 'pip':
                if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?',value):
                    raise ValueError(f'Expected a stable pip version, received {value!r}')
                return value
            return stable(value)
        key = lambda value: tuple(map(int,value.split('.'))) + (0,) * (3-len(value.split('.')))
        files = []
        versions = set()
        for path in sorted((edits.root / 'ai_sandbox_factory').rglob('Dockerfile*')):
            if 'node_modules' in path.parts:
                continue
            name = str(path.relative_to(edits.root))
            values = re.findall(r'^ARG ' + argument + r'=(\S+)$', edits.read(name), re.M)
            if values:
                versions.update(map(version, values))
                files.append(name)
        if not files:
            continue
        if len(versions) != 1:
            raise ValueError(f'{argument} differs between managed images: {sorted(versions)}. Reconcile the shared installer version before updating.')
        old = next(iter(versions))
        latest = npm_version('npm',major=old.split('.')[0]) if package == 'npm' else version(request('https://pypi.org/pypi/pip/json')['info']['version'])
        if key(latest) > key(old):
            for name in files:
                edits.write(name, re.sub(r'^ARG ' + argument + r'=\S+$', 'ARG ' + argument + '=' + latest, edits.read(name), flags=re.M))
            changes.append(f'{package}: {old} → {latest}')
    return changes


def tools(edits):
    changes = []
    name = 'ai_sandbox_factory/containers/primary/Dockerfile'
    old = re.search(r'mitmproxy==([\d.]+)', edits.read(name)).group(1)
    latest = stable(request('https://pypi.org/pypi/mitmproxy/json')['info']['version'])
    if newer(latest, old):
        edits.write(name, edits.read(name).replace('mitmproxy==' + old, 'mitmproxy==' + latest))
        changes.append(f'mitmproxy: {old} → {latest}')
    # The image remains in the supported AWS CLI v2 family.
    name = 'ai_sandbox_factory/containers/aws-access/client/Dockerfile'
    old = re.search(r'aws-cli:([\d.]+)', edits.read(name)).group(1)
    tags = request('https://api.github.com/repos/aws/aws-cli/tags?per_page=100')
    versions = [row['name'].removeprefix('v') for row in tags
                if re.fullmatch(r'v?2\.\d+\.\d+', row.get('name', ''))]
    if not versions:
        raise ValueError('AWS CLI did not publish a stable v2 tag in its current tag list')
    latest = max(versions, key=lambda value: tuple(map(int, value.split('.'))))
    if latest.split('.')[0] != '2':
        raise ValueError(f'AWS CLI {latest} needs a reviewed major-version change; Factory currently supports v2')
    if newer(latest, old):
        reference = 'public.ecr.aws/aws-cli/aws-cli:' + latest
        raw = subprocess.check_output(['skopeo', 'inspect', '--raw', 'docker://' + reference], stderr=subprocess.PIPE)
        digest = hashlib.sha256(raw).hexdigest()
        edits.replace(name, r'^(FROM )public\.ecr\.aws/aws-cli/aws-cli:\S+',
                      r'\g<1>' + reference + '@sha256:' + digest)
        changes.append(f'AWS CLI: {old} → {latest}')
    name = 'ai_sandbox_factory/containers/dev/Dockerfile'
    old = re.search(r'version=([\d.]+); asset="fd-', edits.read(name)).group(1)
    release = request('https://api.github.com/repos/sharkdp/fd/releases/latest')
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('fd latest release is not a stable published release')
    latest = stable(release['tag_name'].removeprefix('v'))
    if newer(latest, old):
        for architecture, binary in [('amd64', 'x86_64'), ('arm64', 'aarch64'), ('i386', 'i686')]:
            asset = next(asset for asset in release['assets'] if asset['name'] == f'fd-v{latest}-{binary}-unknown-linux-musl.tar.gz')
            checksum = asset.get('digest', '')
            if not re.fullmatch('sha256:[a-f0-9]{64}', checksum):
                raise ValueError(f'fd {latest}: no publisher SHA-256 for {architecture}')
            edits.replace(name, rf'({architecture}\) arch={binary}; sha=)[a-f0-9]+', r'\g<1>' + checksum.split(':')[1])
        edits.write(name, edits.read(name).replace(f'version={old}; asset="fd-', f'version={latest}; asset="fd-'))
        changes.append(f'fd: {old} → {latest}')
    changes.extend(bundled_installers(edits))
    return {'current': 'explicit tool versions', 'available': changes or 'unchanged', 'changed': bool(changes)}


def python_packages(edits, *, apply):
    base = 'ai_sandbox_factory/containers/dev/'
    if apply:
        import sys
        if sys.version_info[:2] != (3, 13):
            raise ValueError('Document packages are installed in Debian Trixie Python 3.13. '
                             'Run the weekly lock compiler with Python 3.13 so its resolved set matches the container.')
        changed = []
        for base in ('ai_sandbox_factory/containers/dev/document-requirements',
                     'ai_sandbox_factory/containers/aws-access/client/requirements'):
            with tempfile.TemporaryDirectory(prefix='factory-python-lock-') as temporary:
                output = Path(temporary) / 'requirements.txt'
                subprocess.run(['pip-compile', '--upgrade', '--strip-extras', '--no-header', '--no-annotate',
                                '--output-file', str(output), str(edits.root / (base + '.in'))],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=dict(os.environ, PIP_INDEX_URL='https://pypi.org/simple', PIP_EXTRA_INDEX_URL=''))
                before = edits.read(base + '.txt')
                after = '# Resolved build dependencies; direct requirements: ' + Path(base).name + '.in\n' + output.read_text()
                lines = [line for line in after.splitlines() if line and not line.startswith(('--index-url','--extra-index-url','--trusted-host'))]
                after = '\n'.join(lines) + '\n'
                if sorted(line for line in before.splitlines() if '==' in line) != sorted(line for line in after.splitlines() if '==' in line):
                    edits.write(base + '.txt', after)
                    changed.append(base)
        return {'current': 'resolved document and AWS SDK packages', 'available': changed or 'unchanged', 'changed': bool(changed)}
    return {'current': 'resolved Python packages', 'available': 'resolved during weekly build; vulnerability scan checks the released inventory', 'changed': False}


def update(root=ROOT, *, apply=False, components=None):
    all_edits = Edits(root)
    result = {}
    resolvers = {'pi': pi, **{name: (lambda edits, name=name: agent(edits, name)) for name in ('codex', 'claude', 'agy')},
                 'editor': editor, 'tools': tools, 'npm': lambda edits: npm_packages(edits, apply=apply),
                 'python': lambda edits: python_packages(edits, apply=apply), 'bases': base_images}
    if apply and (components is None or 'bases' in components):
        refresh_os_cache(all_edits)
    for name in components or resolvers:
        edits = Edits(root)
        edits.files = all_edits.files.copy()
        try:
            row = resolvers[name](edits)
            all_edits.files = edits.files
            result[name] = dict(row, status=('updated' if apply else 'available') if row['changed'] else 'current')
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.CalledProcessError) as error:
            diagnostic = str(error)
            if isinstance(error, KeyError):
                diagnostic = f'{name}: upstream metadata is missing required field {error.args[0]!r}. The component was left unchanged; inspect the vendor manifest and retry.'
            elif isinstance(error, StopIteration):
                diagnostic = f'{name}: the release is missing an expected platform archive. The component was left unchanged; inspect the published release assets and retry.'
            if isinstance(error, subprocess.CalledProcessError):
                diagnostic += '\n' + (error.stderr or b'').decode(errors='replace') if isinstance(error.stderr, bytes) else '\n' + (error.stderr or '')
            result[name] = {'status': 'failed', 'error': diagnostic, 'changed': False}
    if apply:
        all_edits.save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Write successfully resolved components; failed components remain unchanged')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--components', nargs='+', choices=('pi','codex','claude','agy','editor','tools','npm','python','bases'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = update(args.root, apply=args.apply, components=args.components)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return int(any(row['status'] == 'failed' for row in result.values()))


if __name__ == '__main__':
    raise SystemExit(main())
