"""Reusable AWS access profiles and the snapshots assigned to sandboxes."""

import json
import uuid

from . import aws_access, registry
from .sandbox_options import write_json

LIBRARY_FILE = 'aws-access-profiles.json'


def entries(base=None):
    path = (base or registry.root()) / LIBRARY_FILE
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(values, dict):
        raise ValueError('Could not read saved AWS access profiles.')
    for key, value in values.items():
        if not isinstance(value, dict) or value.get('profile_id') != key:
            raise ValueError('Could not read a saved AWS access profile.')
        aws_access.settings(value)
    return values


def save(name, connection, *, original=None):
    name = name.strip()
    if not name or not name.isprintable():
        raise aws_access.InvalidSetting('name', 'Enter a profile name on one line.')
    key = original['profile_id'] if original else uuid.uuid4().hex
    value = aws_access.settings({**connection, 'profile_id': key, 'profile_name': name})
    with registry.locked() as base:
        values = entries(base)
        if original and values.get(key) != original:
            raise ValueError('Another edit changed this profile. Reopen it before saving.')
        if any(other != key and row['profile_name'].casefold() == name.casefold() for other, row in values.items()):
            raise aws_access.InvalidSetting('name', 'An AWS access profile already uses this name.')
        values[key] = value
        write_json(base / LIBRARY_FILE, values)
    return value


def pending(state_root, profile):
    """Read assignments from disk, including stopped sandboxes."""
    found = {}
    for path in sorted(state_root.glob('*/config.json')):
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        if config.get('name') != path.parent.name:
            continue
        current = config.get('aws_access') or {}
        if current.get('profile_id') == profile['profile_id'] and current != profile:
            found[path.parent.name] = current
    return found


def apply_selected(service, profile, selected, log):
    failed = []
    for name, previous in selected.items():
        try:
            service.update_settings(name, {'aws_access': profile, 'expected_aws_access': previous}, log)
        except (OSError, ValueError) as exc:
            log(f'{name} · {exc}')
            failed.append(name)
        else:
            log(f'{name} · Updated')
    if failed:
        raise ValueError('Could not update ' + ', '.join(failed) + '. Reopen the profile to retry.')
