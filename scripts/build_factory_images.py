"""Build and smoke-test public Factory images. Publish candidate digests only on request."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import os

from ai_sandbox_factory import (
    aws_access, browser_editor, image_builds, image_sources, inference,
    primary_agents, sandbox, sandbox_advisors, sandbox_ssh,
)
from ai_sandbox_factory.connectors import runtime as connectors

def sdk_check():
    pins = {}
    for line in (sandbox.ROOT / 'containers/aws-access/client/requirements.txt').read_text().splitlines():
        if '==' in line and not line.startswith('#'):
            name, version = line.split('==', 1)
            pins[name] = version
    return ('python3', '-c', 'import importlib.metadata as m; expected=' + repr(pins) +
            '; actual={name:m.version(name) for name in expected}; assert actual==expected, (actual,expected)')


PLATFORM = 'linux/amd64'
REPOSITORY = 'docker.io/kyuz0/ai-sandbox-factory-'
DIGEST = r'sha256:[0-9a-f]{64}'
NATIVE_VERSION = ('/usr/local/bin/factory-agent', '--version')
# Each command runs in its own disposable container, with networking disabled.
SMOKE = {
    'gateway': [('squid', '-v'), ('bash', '-n', '/usr/local/bin/proxy-entrypoint')],
    'pi-common': [('pi', '--version'), ('python3', '/opt/cli-tools/check.py')],
    'pi-stock': [
        ('pi', '--version'), sdk_check(), (browser_editor.INSTALL + '/bin/code-server', '--version'),
        ('/opt/betterwright/chromium', '--version'),
        ('python3', '/opt/sandbox/profile-check.py'), ('aws', '--version'),
        ('node', '-e', """
            const fs = require('node:fs');
            for (const file of ['/opt/betterwright/chromium', '/opt/pi-web-access/extension.mjs',
                '/opt/pi-communications/extension.mjs', '/opt/pi-knowledge/extension.mjs'])
                fs.accessSync(file, fs.constants.R_OK);
            if (JSON.stringify(require('/opt/sandbox/providers.json')) !== '[]')
                throw Error('Stock images must have an empty provider list');
        """),
    ],
    'native-common': [('python3', '/opt/cli-tools/check.py')],
    'controller-codex': [NATIVE_VERSION],
    'controller-claude': [NATIVE_VERSION],
    'controller-agy': [NATIVE_VERSION],
    'advisor': [('advisor-codex', '--version'), ('advisor-claude', '--version'), ('agy', '--version')],
    'ssh': [('ssh', '-V'), ('python3', '-c', "import runpy; runpy.run_path('/opt/ssh-access/service.py')")],
    'aws': [('python3', '-c', "import json, signer; assert 'sts' in json.load(open('/app/services.json'))")],
    'connector': [('python3', '-c', 'import broker, receive; assert callable(broker.serve)')],
    'inference': [('node', '--input-type=module', '-e',
                   "import {compileRoutes} from '/app/broker.mjs'; compileRoutes({})")],
}


def public_config():
    profile = sandbox.ROOT / 'profiles/base'
    return {'engine': 'podman', 'uid': 1000, 'gid': 1000, 'image_source': 'local',
            'common': sandbox.common_profile(), 'image_refs': {},
            'profile': {'name': 'base', 'path': str(profile), 'digest': sandbox.tree_digest(profile)}}


def build(config, role, recipe, cache):
    target = f'localhost/ai-sandbox-factory-{role}:candidate-{recipe}'
    if role == 'pi-stock':
        return image_builds.stock(config, target, cache)
    if role == 'native-common':
        return image_builds.native_common(config, target)
    if role.startswith('controller-'):
        config['primary_agent'] = role.removeprefix('controller-')
        primary_agents.build(config)
    elif role in ('advisor', 'ssh', 'inference'):
        module = {'advisor': sandbox_advisors, 'ssh': sandbox_ssh, 'inference': inference}[role]
        module.build(config, required=True)
    else:
        builder = {'gateway': image_builds.gateway, 'pi-common': image_builds.common,
                   'aws': aws_access.build, 'connector': connectors.build}[role]
        return builder(config)
    return config['image_refs'][role]


def inspect_image(reference):
    raw = sandbox.run(['podman', 'image', 'inspect', reference], capture=True).stdout
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError(f'Podman returned invalid JSON for image {reference}: {error}. Inspect the image and retry.') from error
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError(f'Podman returned no image metadata for {reference}. Inspect the image and retry.')
    image = data[0]
    value = image.get('Id')
    image_id = value.removeprefix('sha256:') if isinstance(value, str) else ''
    platform = f'{image.get("Os")}/{image.get("Architecture")}'
    if not re.fullmatch(r'[0-9a-f]{64}', image_id) or platform != PLATFORM:
        raise ValueError(f'Cannot use image {reference} with ID {image.get("Id")!r} and platform {platform!r}. '
                         f'Build it on a native {PLATFORM} runner and retry.')
    return image_id, image.get('RepoDigests') or []


def smoke(role, reference):
    print(f'Testing {role} image {reference}…', flush=True)
    pins = json.loads((sandbox.ROOT / 'containers/primary/agent-versions.json').read_text())
    expected = {}
    if role in ('pi-common', 'pi-stock'):
        expected['pi'] = sandbox.PI_VERSION
    if role.startswith('controller-'):
        expected[NATIVE_VERSION[0]] = pins[role.removeprefix('controller-')]['version']
    if role == 'advisor':
        expected.update({executable: pins[agent]['version'] for agent, executable in
                         [('codex', 'advisor-codex'), ('claude', 'advisor-claude'), ('agy', 'agy')]})
    if role == 'pi-stock':
        expected[browser_editor.INSTALL + '/bin/code-server'] = browser_editor.VERSION
        aws = (sandbox.ROOT / 'containers/aws-access/client/Dockerfile').read_text()
        expected['aws'] = re.search(r'aws-cli:([\d.]+)', aws).group(1)
    versions = {}
    for executable, *arguments in SMOKE[role]:
        result = sandbox.run(['podman', 'run', '--rm', '--pull=never', '--network=none',
                              '--entrypoint', executable, reference, *arguments], capture=True)
        if executable in expected:
            version = expected[executable]
            if not re.search(r'(?<![\d.])v?' + re.escape(version) + r'(?![\d.])', result.stdout):
                raise ValueError(f'{role}: {executable} reported {result.stdout.strip()!r}; expected {version}. '
                                 'Do not promote this image; check its build inputs.')
            versions[executable] = result.stdout.strip()
    return versions


def validate_index(path, roles=None):
    """Reject a missing/stale role before any candidate can be promoted."""
    value = json.loads(Path(path).read_text())
    if value.get('version') != 1 or not isinstance(value.get('images'), dict):
        raise ValueError(f'{path}: expected a version 1 Factory image index')
    config = public_config()
    references = {}
    for role in roles or SMOKE:
        recipe = image_sources.recipe(config, role)
        reference = value['images'].get(recipe, {}).get(PLATFORM)
        expected = re.escape(REPOSITORY + role + '@') + DIGEST
        if not isinstance(reference, str) or not re.fullmatch(expected, reference):
            raise ValueError(f'{path}: no tested {role} image matches recipe {recipe} for {PLATFORM}. '
                             'Rebuild the candidate after the source or pins changed.')
        references[role] = reference
    return references


def verify_index(path, roles=None):
    """Validate current recipes and pull/test the exact complete candidate set."""
    verified = {}
    for role, reference in validate_index(path, roles).items():
        with tempfile.TemporaryDirectory(prefix='factory-image-verify-') as directory:
            auth = Path(directory) / 'anonymous.json'
            auth.write_text('{"auths":{}}\n')
            sandbox.run(['podman', 'pull', '--authfile', auth, reference])
        image_id, repo_digests = inspect_image(reference)
        if not image_sources._matches_repo_digest(reference, repo_digests):
            raise ValueError(f'{role}: downloaded {reference} but the engine reported {repo_digests!r}')
        verified[role] = {'reference': reference, 'image_id': image_id, 'versions': smoke(role, reference)}
    return verified


def verify_published(role, image_id, reference):
    with tempfile.TemporaryDirectory(prefix='factory-image-pull-') as temporary:
        anonymous = Path(temporary) / 'anonymous.json'
        anonymous.write_text('{"auths":{}}\n')
        sandbox.run(['podman', 'pull', '--authfile', anonymous, reference])
    pulled_id, repo_digests = inspect_image(reference)
    if pulled_id != image_id or reference not in repo_digests:
        raise ValueError(f'Pulled {reference} has image ID {pulled_id} and repo digests {repo_digests!r}. '
                         f'Expected the locally tested image {image_id} at that digest. Check the push and retry.')
    smoke(role, reference)


def publish(role, image_id, authfile=None):
    repository = REPOSITORY + role
    tag = f'{repository}:candidate-{image_id}'
    with tempfile.TemporaryDirectory(prefix='factory-image-push-') as temporary:
        result = Path(temporary) / 'digest'
        auth = ['--authfile', authfile] if authfile else []
        sandbox.run(['podman', 'push', *auth, '--digestfile', result, image_id, 'docker://' + tag])
        digest = result.read_text().strip() if result.is_file() else ''
        if not re.fullmatch(DIGEST, digest):
            raise ValueError(f'Podman pushed {tag} but returned an invalid manifest digest {digest!r}. '
                             'Inspect the registry result and rerun publication.')
    reference = repository + '@' + digest
    verify_published(role, image_id, reference)
    return reference


def candidates(roles, cache, *, publishing, authfile=None, inventory=None):
    platform = image_sources._platform('podman', None)
    if platform != PLATFORM:
        raise ValueError(f'The candidate checks run on {PLATFORM}. Podman reported {platform}. '
                         'Use a native Linux amd64 runner.')
    local = []
    with image_sources.override('local'):
        for role in roles:
            config = public_config()
            fingerprint = image_sources.recipe(config, role)
            print(f'Building {role} recipe {fingerprint}…', flush=True)
            image_id, _ = inspect_image(build(config, role, fingerprint, cache))
            versions = smoke(role, image_id)
            if image_sources.recipe(public_config(), role) != fingerprint:
                raise ValueError(f'The {role} recipe changed during its build. Rebuild from the intended checkout.')
            local.append({'role': role, 'recipe': fingerprint, 'image_id': image_id, 'versions': versions})
    if not publishing:
        return {'platform': platform, 'local_images': local}
    images = {}
    for row in local:
        reference = publish(row['role'], row['image_id'], authfile)
        images[row['recipe']] = {platform: reference}
        if inventory is not None:
            inventory[row['role']] = {**row, 'reference': reference}
    return {'version': 1, 'images': images}


def merge_fragments(folder, roles):
    images = {}
    for role in roles:
        path = folder / f'{role}.json'
        fingerprint = image_sources.recipe(public_config(), role)
        try:
            fragment = json.loads(path.read_text())
        except ValueError as error:
            raise ValueError(f'Cannot read candidate fragment {path}: {error}. Download it again.') from error
        expected = re.escape(REPOSITORY + role + '@') + DIGEST
        if (not isinstance(fragment, dict) or fragment.get('version') != 1
                or not isinstance(fragment.get('images'), dict) or set(fragment['images']) != {fingerprint}):
            raise ValueError(f'{path} must contain the tested {role} recipe {fingerprint} for this checkout. '
                             'Download the fragments from the same publication run.')
        entry = fragment['images'][fingerprint]
        if (not isinstance(entry, dict) or set(entry) != {PLATFORM}
                or not isinstance(entry[PLATFORM], str) or not re.fullmatch(expected, entry[PLATFORM])):
            raise ValueError(f'{path} must name the published {PLATFORM} digest in {REPOSITORY + role}. '
                             'Rerun publication to obtain a verified fragment.')
        images[fingerprint] = entry
    return {'version': 1, 'images': images}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roles', nargs='+', choices=SMOKE, default=list(SMOKE),
                        help='Build the selected public image roles (default: all)')
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache/ai-sandbox-factory',
                        help='Directory for the verified code-server archive')
    parser.add_argument('--output', type=Path, required=True, help='Write the successful local report or candidate index here')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--publish', action='store_true', help='Push tested images to Docker Hub and recheck each exact digest')
    parser.add_argument('--push-authfile', type=Path, metavar='FILE',
                        help='Use this Podman registry login for the push')
    mode.add_argument('--merge-fragments', type=Path, metavar='DIR',
                      help='Combine the verified ROLE.json fragments for this checkout')
    mode.add_argument('--verify-index', type=Path, help='Pull and verify the complete exact published candidate set')
    args = parser.parse_args(argv)
    if args.push_authfile and not args.publish:
        parser.error('--push-authfile requires --publish.')
    if args.output.resolve() == image_sources.INDEX.resolve():
        parser.error('Choose a separate output file. The installed image list is updated through a reviewed release.')
    roles = list(dict.fromkeys(args.roles))
    inventory = {}
    result = (verify_index(args.verify_index, roles) if args.verify_index else
              merge_fragments(args.merge_fragments, roles) if args.merge_fragments else
              candidates(roles, args.cache, publishing=args.publish, authfile=args.push_authfile, inventory=inventory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temporary:
        staged = Path(temporary) / 'result.json'
        staged.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
        staged.replace(args.output)
    if inventory:
        metadata = {'source_commit': os.environ.get('FACTORY_SOURCE_SHA', os.environ.get('GITHUB_SHA', '')),
                    'images': inventory}
        args.output.with_name(args.output.stem + '-inventory.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'Wrote {args.output}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
