"""Shared settings normalization and optimistic revision checks."""
import hashlib
from pathlib import Path

from . import model_providers, settings_catalog


def revision(state):
    state = Path(state)
    digest = hashlib.sha256()
    for name in ('config.json', 'allowlist.txt'):
        value = (state / name).read_bytes()
        digest.update(name.encode() + b'\0' + str(len(value)).encode() + b'\0' + value)
    return digest.hexdigest()


def check(state, expected):
    if expected is not None and revision(state) != expected:
        raise ValueError(f'Settings for {Path(state).name} changed after review. '
                         'Review the current settings before applying again.')


def normalize_mode(config, state, changes):
    """A mode-only edit has the same destination semantics as the settings form."""
    if not isinstance(changes, dict) or set(changes) != {'network'}:
        return changes
    mode = changes['network']
    if not isinstance(mode, str) or mode not in dict((value, label) for label, value in settings_catalog.NETWORK_MODES):
        raise ValueError(f'Unknown Network mode {mode!r}. Choose allowlist (Restricted) or unrestricted.')
    if mode == config.get('network', 'allowlist'):
        return {}
    if mode == 'unrestricted' and config.get('local_model_ports'):
        raise ValueError(f'Turn off Local models in {settings_catalog.LOCAL_MODELS_PATH} '
                         'before choosing Unrestricted.')
    text = ''
    if mode == 'allowlist':
        from .sandbox import parse_rules
        text = (Path(state) / 'allowlist.txt').read_text()
        rules = parse_rules(text)
        for rule in parse_rules('\n'.join(model_providers.required_rules(model_providers.selections(config)))):
            if rule not in rules:
                rules.append(rule)
        text = '\n'.join(f'{host}:{port}' for host, port in rules)
    result = {'network': mode, 'allowlist': text}
    return result


def network_rules_only(config, changes):
    from .sandbox_options import options
    if (config.get('network','allowlist') != 'allowlist' or not isinstance(changes,dict) or not changes
            or not set(changes) <= {'allowlist','options','curlie_list'}):
        return False
    before = options(config.get('options'))
    after = options(changes.get('options',before))
    return {key for key in after if after[key] != before[key]} <= {'website_groups'}


def curlie_needed(config, changes):
    from . import curlie_catalog, network_groups
    from .sandbox_options import options
    if not isinstance(changes,dict) or changes.get('network', config.get('network','allowlist')) != 'allowlist':
        return False
    names = options(changes.get('options',config.get('options')))['website_groups']
    return (not changes.get('curlie_list') and not curlie_catalog.assigned(config.get('website_groups',{}))
            and any(network_groups.catalog()[name].get('curlie') for name in names))


def normalize(config, state, changes):
    result = normalize_mode(config,state,changes)
    if result and curlie_needed(config,result):
        from . import curlie_catalog
        if cached := curlie_catalog.metadata():
            result = {**result, 'curlie_list': cached['revision']}
    return result
