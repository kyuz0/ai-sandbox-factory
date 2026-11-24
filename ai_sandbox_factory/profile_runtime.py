"""Applied workspace recipes, separate from runtime credentials and editable sources."""
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

PATH = '/opt/sandbox/base-profile'


def identity(config, image_id):
    return hashlib.sha256(json.dumps([image_id, config['profile']['digest'],
        config['common']['digest'], config.get('primary_agent', 'pi')]).encode()).hexdigest()


@contextmanager
def recording(config, state):
    """Freeze inputs before building; publish them only for a successful image."""
    from . import sandbox, primary_workspace
    if state is None:
        yield None
        return
    state = Path(state)
    for part in ('profile', 'common'):
        if sandbox.tree_digest(Path(config[part]['path'])) != config[part]['digest']:
            raise ValueError(f'The saved {part} build sources were modified. Select the profile again before building.')
    base = state / 'applied-profiles'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.building-', dir=base) as temporary:
        target = Path(temporary) / 'profile'
        target.mkdir()
        shutil.copytree(config['profile']['path'], target / 'profile')
        common = Path(config['common']['path'])
        agent = primary_workspace.agent(config)
        if agent == 'pi':
            shutil.copytree(common, target / 'base')
        else:
            # Native workspaces consume CLI tools and optional browser inputs,
            # not Pi's complete bootstrap/extension graph.
            (target / 'base').mkdir()
            parts = ['cli-tools']
            from .sandbox_options import options
            if options(config.get('options'))['browser']:
                parts += ['browser', 'security']
            for part in parts:
                if (common / part).exists():
                    shutil.copytree(common / part, target / 'base' / part)
        if agent != 'pi':
            shutil.copytree(sandbox.ROOT / 'containers/primary-worker', target / 'workspace')
            client = target / 'workspace' / 'client'
            client.mkdir()
            for name in ('worker.sh', 'worker-tools.mjs', agent + '_client.py'):
                shutil.copyfile(sandbox.ROOT / 'containers/primary' / name, client / name)
        yield target
        for part in ('profile', 'common'):
            if sandbox.tree_digest(Path(config[part]['path'])) != config[part]['digest']:
                raise ValueError(f'The {part} sources changed while building. Retry the build with unchanged sources.')
        image = sandbox.image_names(config)[0]
        result = sandbox.run([config['engine'], 'image', 'inspect', image], capture=True)
        try:
            image_id = json.loads(result.stdout)[0]['Id']
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ValueError(f'Could not record the applied profile for image {image}: image inspection returned no image ID.') from error
        metadata = {
            'name': config['profile']['name'],
            'description': config['profile'].get('description', config['profile']['name']),
            'profile_digest': config['profile']['digest'],
            'common_inputs_digest': config['common']['digest'],
            'base_recipe_digest': (config['common']['digest'] if agent == 'pi'
                                   else sandbox.tree_digest(sandbox.ROOT / 'containers/primary-worker')),
            'agent': agent, 'image': image, 'image_id': image_id,
            'profile_layer_applied': True,
            'base_recipe': 'base/Dockerfile' if agent == 'pi' else 'workspace/Dockerfile',
        }
        from . import profile_services
        metadata['services'] = profile_services.resolved_images(config)
        if agent != 'pi':
            metadata['workspace_recipe'] = 'workspace/Dockerfile'
        (target / 'build.json').write_text(json.dumps(metadata, indent=2) + '\n')
        destination = base / identity(config, image_id)
        if not destination.exists():
            target.rename(destination)


def select(config, state, current=None):
    """Select the recipe for the actual container, never a newer image tag."""
    from . import sandbox
    state = Path(state)
    if current is not None and not any(mount.get('Destination') == PATH for mount in current.get('Mounts', [])):
        # An older container cannot acquire a new bind mount on resume.
        (state / 'profile-runtime/applied.json').unlink(missing_ok=True)
        return
    if not (state / 'applied-profiles').is_dir():
        return
    image_id = (current or {}).get('Image')
    if not image_id:
        found = sandbox.run([config['engine'], 'image', 'inspect', sandbox.image_names(config)[0]],
                            capture=True, check=False, stderr=subprocess.DEVNULL)
        if found.returncode:
            return
        image_id = json.loads(found.stdout)[0]['Id']
    # An existing container's actual mount is authoritative even when a new
    # build produced the same image bytes from different profile metadata.
    mounted = next((mount['Source'] for mount in (current or {}).get('Mounts', [])
                    if mount.get('Destination') == PATH), None)
    source = Path(mounted) if mounted else state / 'applied-profiles' / identity(config, image_id)
    if source.is_dir():
        sandbox.write_json(state / 'profile-runtime' / 'applied.json', {
            'path': str(source), **json.loads((source / 'build.json').read_text())})
    else:
        (state / 'profile-runtime/applied.json').unlink(missing_ok=True)


def ensure_cached(config, state):
    """A fresh state needs its own source record even when the images are cached."""
    if not all(isinstance(config.get(part), dict) and all(key in config[part] for key in ('path', 'digest'))
               for part in ('profile', 'common')):
        return
    select(config, state)
    if applied(state):
        return
    from . import profile_services
    profile_services.build(config)
    with recording(config, state):
        pass
    select(config, state)


def applied(state):
    file = Path(state) / 'profile-runtime/applied.json'
    return json.loads(file.read_text()) if file.is_file() else None


def mounts(state):
    from . import host_platform
    value = applied(state)
    if not value:
        return []
    return ['-v', f'{value["path"]}:{PATH}:{host_platform.bind_mode()}']


def guidance(state):
    value = applied(state)
    if not value:
        return ''
    return (f'\n## Applied profile\n\nThis workspace was built from profile {value["name"]!r}, '
            f'revision {value["profile_digest"]}. Read its applied Dockerfile and base-image recipes at {PATH}. '
            f'The selected package is in profile/ and its base inputs are in base/. '
            'That directory is read-only. build.json identifies the workspace image and recipes actually used. '
            'The profile describes the image build; packages installed later and current Factory settings may differ. '
            'Project instructions in /workspace remain separate.\n')
