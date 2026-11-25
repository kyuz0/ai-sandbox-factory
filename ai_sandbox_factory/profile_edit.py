"""Editable profile sources, frozen reviews and explicit application to a sandbox."""
import copy
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from . import profile_runtime, profile_services, profile_spec

PATH = '/opt/sandbox/profile'
EDIT = 'editable_profile'

PURPOSE = (
    'Profile customization is occasional environment setup and maintenance. '
    'Propose it when a task needs persistent system software in the coding-agent container, '
    'a service such as a database, a change to those environment requirements, or user-requested environment maintenance. '
    'Once the installed software and services meet the task’s needs, use the existing environment '
    'and leave the profile unchanged. A new task, session or day alone is not a reason to fork, '
    'rebuild or modify the profile. Manage ordinary project dependencies in the project as usual. '
    'For later necessary changes, reuse an existing editable profile unless the user wants a separate fork.'
)


def roots(config):
    return [(PATH, False)] if config.get(EDIT) else []


def mounts(config):
    if not config.get(EDIT):
        return []
    recursion = 'bind-nonrecursive' if config['engine'] == 'podman' else 'bind-recursive=disabled'
    return ['--mount', f'type=bind,src={config[EDIT]["path"]},dst={PATH},{recursion},rw']


def guidance(config):
    agent = config.get('primary_agent', 'pi')
    if not config.get(EDIT):
        return ('\n' + PURPOSE + '\n\nTo make permanent changes to this profile, ask the user to choose '
                'Actions… → Edit profile… and create an editable copy. This is separate from the project. '
                'You can propose this fork when permanent package or service changes are useful. '
                'After the copy is attached, ask whether the user wants it tracked in Git. '
                'Do not edit the read-only applied recipes.\n')
    editable = config[EDIT]
    origin = (f'This editable profile was forked from {editable["origin_name"]!r}, '
              f'revision {editable["origin_digest"]}. ')
    return (f'\n## Edit this sandbox’s profile\n\n{PURPOSE}\n\nThe writable profile source is {PATH}, separate from /workspace. '
            + origin + f'The applied build record at {profile_runtime.PATH} is read-only and describes the current image. '
            'The editable folder survives restarts and image replacements and may contain changes not yet applied. '
            'Editing the source does not change the running sandbox. Use the user’s existing authorization for profile maintenance; ask before expanding it. '
            'For packages, edit the top-level Dockerfile; for an additional host, add its Dockerfile and a '
            'services entry in profile.json. Update profile.md to explain the host and its connections. '
            f'Read {profile_runtime.AUTHORING} for the format and complete examples. '
            f'Validate with `python3 -I - {PATH} --agent {agent} < {profile_runtime.PATH}/authoring/validate.py`. '
            'Static checks do not build images or test startup. Fix any reported file or field and validate again. '
            'Then tell the user what changed and ask them to choose Actions… → Edit profile… → Review changes… '
            'and Apply profile changes. Factory builds before replacing the coding-agent container; running '
            'agent work and terminals stop at replacement, while project files, home, chats, sign-ins and existing '
            'services and data are kept. New services can be added. Replacing or removing an existing service '
            'requires a separate sandbox and application-specific data export/import. '
            'Do not claim an edit is installed until the user has applied it successfully. '
            'Ask whether the user wants this profile tracked in Git when planning a necessary profile change, if that choice has not been made. '
            'If authorized, initialize a separate repository here, commit the approved profile changes, and '
            'use the supplied origin for authorized pushes. If not already specified, agree whether commits stay local or are pushed; '
            'honor an existing ongoing maintenance instruction without asking again. '
            'Use the configured SSH & Git connector for SSH-origin authentication; never copy its private keys into the profile. '
            'If access is missing, ask the user to configure Settings → Connectors → SSH & Git and any required Network destinations. '
            'When resuming profile maintenance, inspect this folder’s Git status, log and remotes before continuing; preserve uncommitted edits. '
            'Record the user’s Git tracking choice and approved ongoing maintenance instructions in profile.md so future sessions receive them after Apply. '
            'When resuming profile maintenance, read the editable profile.md for choices saved since the last Apply; its unapplied package and service changes are still only proposals. '
            'Keep the profile repository separate from the project repository and never initialize Git or publish without authorization.\n')


def source(config, state):
    value = profile_runtime.applied(state)
    return Path(value['path']) / 'profile' if value and value.get('path') else Path(config['profile']['path'])


def _folder(path, config, state):
    from . import folders, host_platform, primary_workspace
    path = host_platform.local_path(str(path)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f'Editable profile folder {path} is missing or is not a directory. '
                         'Restore it at this saved location, then retry. The applied profile and private sandbox state are kept.')
    folders.validate([{'source': str(path), 'name': 'profile', 'read_only': False}],
                     config['workspace'], Path(state).parent, private_paths=[config.get('pi_config')])
    if any(folders.overlaps(path, row['source']) for row in config.get('extra_folders', [])):
        raise ValueError(f'Editable profile folder {path} overlaps an additional project folder. Choose a separate profile folder.')
    primary_workspace.validate_mounts({**config, 'extra_folders': [{'source': str(path)}]}, Path(state).parent)
    return path


def _profile(path, original):
    from . import sandbox
    path = Path(path)
    metadata = profile_spec.validate(path, original.get('primary_agent', 'pi'))
    value = {key: content for key, content in original['profile'].items()
             if key not in ('git_source', 'source_commit', 'services', 'defaults', 'agents')}
    value.update(name=metadata['name'], description=metadata.get('description', ''),
                 path=str(path), digest=sandbox.tree_digest(path))
    for key in ('agents', 'defaults'):
        if key in metadata:
            value[key] = metadata[key]
    services = profile_spec.services(metadata, path)
    if services:
        value['services'] = services
    return value


def freeze(config, state):
    """Copy working-tree bytes without following links or consulting Git HEAD."""
    from . import sandbox
    folder = _folder(config[EDIT]['path'], config, state)
    first = _profile(folder, config)['digest']
    with tempfile.TemporaryDirectory(prefix='.profile-review-', dir=state) as temporary:
        copied = Path(temporary) / 'profile'
        shutil.copytree(folder, copied, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        value = _profile(copied, config)
        if value['digest'] != first or _profile(folder, config)['digest'] != first:
            raise ValueError(f'Profile files in {folder} changed while preparing the review. Finish the edit and choose Review changes again.')
        destination = Path(state) / 'profiles' / value['digest']
        sandbox.publish_snapshot(copied, destination)
        value['path'] = str(destination)
    return value


def differences(before, after):
    def files(root):
        return {str(path.relative_to(root)): path for path in root.rglob('*')
                if path.is_file() and not any(p in ('.git', '__pycache__') for p in path.relative_to(root).parts)}
    old, new = files(Path(before)), files(Path(after))
    changes, patches = [], []
    for name in sorted(old.keys() | new.keys()):
        a = old[name].read_bytes() if name in old else b''
        b = new[name].read_bytes() if name in new else b''
        if name in old and name in new and a == b:
            continue
        kind = 'Added' if name not in old else 'Removed' if name not in new else 'Changed'
        changes.append(f'{kind}: {name}')
        try:
            patches.extend(difflib.unified_diff(a.decode('utf-8').splitlines(True), b.decode('utf-8').splitlines(True),
                           fromfile='applied/' + name if name in old else '/dev/null',
                           tofile='edited/' + name if name in new else '/dev/null'))
        except UnicodeError:
            patches.append(f'{kind} binary file: {name}\n')
    return changes, ''.join(patches)


def review(config, state):
    from . import sandbox
    if not config.get(EDIT):
        raise ValueError('This sandbox has no editable profile. Choose Actions… → Edit profile… to create a copy first.')
    value = freeze(config, state)
    updated = dict(config, profile=value)
    profile_services.validate(updated)
    profile_services.validate_update(config, updated, allow_additions=True)
    changes, diff = differences(config['profile']['path'], value['path'])
    revision = hashlib.sha256(json.dumps([config['profile']['digest'], value['digest']]).encode()).hexdigest()[:16]
    result = {'revision': revision, 'base': config['profile']['digest'], 'profile': value,
              'folder': config[EDIT]['path'], 'changes': changes, 'diff': diff,
              'added_services': sorted(profile_services.selected(updated).keys() - profile_services.selected(config).keys())}
    sandbox.write_json(Path(state) / 'profile-reviews' / (revision + '.json'), result)
    return result


def _cancelled(gate):
    return gate is not None and Path(gate).exists()


def _copy_source(source, destination):
    from . import host_platform
    if not (host_platform.is_wsl() and host_platform.windows_drive(destination)):
        shutil.copytree(source, destination, symlinks=True,
                        ignore=shutil.ignore_patterns('.git', '__pycache__'))
        return
    # DrvFS without metadata supports file contents but can reject copystat.
    # Validation already requires regular files. Keep Windows ACLs and modes
    # under host control instead of changing them to Linux snapshot metadata.
    destination.mkdir()
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if any(part in ('.git', '__pycache__') for part in relative.parts):
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True)
        else:
            shutil.copyfile(path, target)


def fork(config, state, destination, gate=None):
    from . import sandbox, profile_replacement, updating, host_platform
    if config.get(EDIT):
        raise ValueError(f'This sandbox already has an editable profile at {config[EDIT]["path"]}. Choose Review changes to apply edits.')
    state = Path(state)
    destination = host_platform.local_path(destination).resolve()
    pending = state / 'editable-profile-pending.json'
    previous = json.loads(pending.read_text()) if pending.exists() else {}
    retry = previous.get('path') == str(destination) and previous.get('base') == config['profile']['digest']
    created = False
    if destination.exists():
        if not destination.is_dir() or (any(destination.iterdir()) and not retry):
            raise ValueError(f'Cannot create editable profile at {destination}: the destination is not an empty folder. Choose a new or empty folder; existing files were not changed.')
    else:
        destination.mkdir(parents=True)
        created = True
    try:
        _folder(destination, config, state)
        if not retry:
            profile_spec.validate(source(config, state), config.get('primary_agent', 'pi'))
            actual = sandbox.tree_digest(source(config, state))
            if actual != config['profile']['digest']:
                raise ValueError(f'Cannot copy the applied profile: its recorded files have revision {actual}, '
                                 f'but the saved sandbox expects {config["profile"]["digest"]}. Rebuild the saved profile before creating an editable copy.')
            with tempfile.TemporaryDirectory(prefix='.profile-fork-', dir=destination.parent) as temporary:
                staged = Path(temporary) / 'profile'
                _copy_source(source(config, state), staged)
                profile_spec.validate(staged, config.get('primary_agent', 'pi'))
                # Directory rename may replace an empty directory, never one
                # another process populated while the copy was prepared.
                staged.rename(destination)
            sandbox.write_json(pending, {'path': str(destination), 'base': config['profile']['digest']})
        profile_spec.validate(destination, config.get('primary_agent', 'pi'))
    except Exception:
        if created and not any(destination.iterdir()):
            destination.rmdir()
        raise
    updated = copy.deepcopy(config)
    updated[EDIT] = {'path': str(destination), 'origin_name': config['profile']['name'],
                     'origin_digest': config['profile']['digest']}
    with sandbox.image_graph(updated):
        pointer = state / 'profile-runtime/applied.json'
        previous = pointer.read_bytes() if pointer.exists() else None
        try:
            profile_runtime.ensure_cached(updated, state)
        finally:
            if previous is None:
                pointer.unlink(missing_ok=True)
            else:
                pointer.write_bytes(previous)
        if _cancelled(gate) or not updating.begin_apply(gate):
            print(f'Profile attachment cancelled. The editable copy remains at {destination}; the sandbox is unchanged.', flush=True)
            return
        with profile_replacement.prepared_services(config, updated, state):
            profile_replacement.apply(config, updated, state, retain_previous=True)
    pending.unlink(missing_ok=True)
    print(f'Editable profile ready at {destination}; the coding agent can edit {PATH}.', flush=True)


def apply(config, state, revision, gate=None):
    from . import sandbox, profile_replacement, updating
    from .job_progress import Progress
    if not re.fullmatch(r'[a-f0-9]{16}', revision):
        raise ValueError(f'Unknown profile review {revision!r}. Choose Review changes again.')
    file = Path(state) / 'profile-reviews' / (revision + '.json')
    if not file.is_file():
        raise ValueError(f'Profile review {revision} is missing. Choose Review changes again.')
    reviewed = json.loads(file.read_text())
    if (not config.get(EDIT) or reviewed['base'] != config['profile']['digest']
            or reviewed['folder'] != config[EDIT]['path']):
        raise ValueError('The sandbox’s applied profile or editable folder changed after this review. Choose Review changes again.')
    profile = _profile(config[EDIT]['path'], config)
    if profile['digest'] != reviewed['profile']['digest']:
        raise ValueError(f'Files in {config[EDIT]["path"]} changed after review {revision}. Choose Review changes again before applying.')
    updated = copy.deepcopy(config)
    updated['profile'] = reviewed['profile']
    if sandbox.tree_digest(Path(updated['profile']['path'])) != profile['digest']:
        raise ValueError(f'Saved profile review {revision} was modified. Choose Review changes again.')
    profile_services.validate(updated)
    profile_services.validate_update(config, updated, allow_additions=True)
    if profile['digest'] == config['profile']['digest']:
        print('The edited profile already matches the applied revision; no containers changed.', flush=True)
        return
    progress = Progress(['Build profile images', 'Prepare new profile services', 'Apply profile changes'])
    with sandbox.image_graph(updated):
        with progress.stage('Build profile images'):
            with sandbox.cancellable_build(lambda: _cancelled(gate)):
                sandbox.build(updated, state)
        if _cancelled(gate):
            raise updating.UpdateCancelled()
        with profile_replacement.prepared_services(config, updated, state):
            with progress.stage('Prepare new profile services'):
                profile_services.up(updated, state)
                profile_services.ready(updated, state, check_ssh=False, cancelled=lambda: _cancelled(gate))
            if not updating.begin_apply(gate):
                raise updating.UpdateCancelled()
            with progress.stage('Apply profile changes'):
                profile_replacement.apply(config, updated, state, allow_additions=True, retain_previous=True)
    print('Profile changes applied. Open the coding agent again to use the new image and instructions.', flush=True)
