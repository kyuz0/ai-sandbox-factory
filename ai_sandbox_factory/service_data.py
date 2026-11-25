"""Browse and recover service data retained by reviewed sandbox changes."""
import copy
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from .job_progress import Progress
from . import activity, host_platform, profile_replacement, profile_services, sandbox, updating


def entries(config):
    return list(reversed(config.get('profile_service_recovery', [])))


def record(updated, original, service, reason, volumes):
    if not volumes:
        return
    spec = profile_services.selected(original)[service]
    current = sandbox.inspect(original, profile_services.container(original, service)) or {}
    profile = original['profile']
    entry = {
        'id': uuid4().hex, 'time': datetime.now(timezone.utc).isoformat(),
        'reason': reason, 'service': service, 'image': profile_services.image(original, service, spec),
        'image_id': current.get('Image'), 'profile_digest': profile['digest'],
        'volumes': volumes,
    }
    updated['profile_service_recovery'] = [*updated.get('profile_service_recovery', []), entry]


def _entry(config, set_id):
    for entry in entries(config):
        if entry['id'] == set_id:
            return entry
    raise ValueError(f'No saved service data matches {set_id!r} in sandbox {config["name"]!r}. Open Saved service data to select an available copy.')


def _owned(config, volume):
    result = sandbox.run([config['engine'], 'volume', 'inspect', volume], capture=True, check=False, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(f'Cannot inspect service data volume {volume!r} (exit {result.returncode}): '
                         f'{(result.stderr or result.stdout).strip()}. Check the volume in Podman and retry.')
    info = json.loads(result.stdout)[0]
    labels = info.get('Labels') or {}
    if labels.get(sandbox.LABEL) != config['name'] or labels.get(profile_services.LABEL_ROLE) != 'data':
        raise ValueError(f'Volume {volume!r} lacks the data-volume ownership labels for sandbox {config["name"]!r}. Inspect its Podman labels before retrying.')


def _source(config, entry, key):
    if key not in entry['volumes']:
        raise ValueError(f'Saved service data {entry["id"]!r} has no volume {key!r}. Choose one of {", ".join(entry["volumes"])}.')
    volume = entry['volumes'][key]['volume']
    if volume not in config.get('profile_service_retained_volumes', []):
        raise ValueError(f'Volume {volume!r} is absent from the retained volumes for sandbox {config["name"]!r}. Check the sandbox settings before retrying.')
    _owned(config, volume)
    return volume


def export(config, state, set_id, key, path, gate=None):
    volume = _source(config, _entry(config, set_id), key)
    path = host_platform.local_path(path).absolute()
    if os.path.lexists(path):
        raise ValueError(f'Cannot export service data to {path}: a file already exists there. Choose a new filename.')
    progress = Progress(['Export saved data', 'Publish archive'])
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.service-export-', delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with progress.stage('Export saved data'):
                with sandbox.cancellable_build(lambda: gate is not None and Path(gate).exists()):
                    result = sandbox.run([config['engine'], 'volume', 'export', volume, '--output', str(temporary)],
                                         capture=True, check=False, stderr=subprocess.PIPE)
                if result.returncode:
                    raise ValueError(f'Cannot export volume {volume!r} to {path} (exit {result.returncode}): '
                                     f'{(result.stderr or result.stdout).strip()}. Check the volume and destination before retrying.')
            if not updating.begin_apply(gate):
                raise updating.UpdateCancelled()
            with progress.stage('Publish archive'):
                try:
                    with temporary.open('rb') as archive:
                        os.fsync(archive.fileno())
                    os.link(temporary, path)
                except OSError as error:
                    raise ValueError(f'Cannot publish archive at {path}: {error}. Choose a writable destination and a new filename.') from error
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise ValueError(f'Cannot export volume {volume!r} to {path}: {error}. Choose a writable destination and a new filename.') from error
    return path


def restore(config, state, set_id, target_service, gate=None):
    entry = _entry(config, set_id)
    spec = profile_services.selected(config).get(target_service)
    if spec is None:
        raise ValueError(f'Service {target_service!r} is absent from this sandbox. Add it through Edit profile and Apply profile changes, or choose an existing service.')
    missing = entry['volumes'].keys() - spec['volumes'].keys()
    if missing:
        raise ValueError(f'Service {target_service!r} lacks saved volume keys {", ".join(sorted(missing))}. Choose a service that declares all saved volume keys.')
    sources = {key: _source(config, entry, key) for key in entry['volumes']}
    for key in spec['volumes']:
        _owned(config, profile_services.data_volume(config, target_service, key))
    current = sandbox.inspect(config, profile_services.container(config, target_service))
    if not current:
        raise ValueError(f'Service {target_service!r} has no container. Start the sandbox to initialize the service, then retry Restore.')
    updated = copy.deepcopy(config)
    def cancelled():
        return gate is not None and Path(gate).exists()
    progress = Progress(['Copy service data', 'Check restored service', 'Save restored service'])
    with ExitStack() as transaction:
        with progress.stage('Copy service data'):
            prepare = transaction.enter_context(profile_replacement.prepared_services(
                config, updated, state, cancelled=cancelled, defer_preparation=True,
                restore_target=target_service, restore_sources=sources))
            prepare()
        with progress.stage('Check restored service'):
            try:
                profile_services.start_service(updated, state, target_service)
                profile_services.ready(updated, state, service=target_service, check_ssh=False, cancelled=cancelled)
            except (ValueError, OSError, subprocess.CalledProcessError) as error:
                target_image = profile_services.image(config, target_service, spec)
                raise ValueError(f'Cannot restore data to service {target_service!r}: {error}. '
                                 f'Check that the target image {target_image!r} can read data from '
                                 f'the saved image {entry["image"]!r} before retrying Restore data.') from error
        if not updating.begin_apply(gate):
            raise updating.UpdateCancelled()
        with progress.stage('Save restored service'):
            activity.save_config(state, updated)
            transaction.close()
    return updated
