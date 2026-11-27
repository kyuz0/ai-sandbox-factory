"""Offline update plans for the Factory software actually applied to a sandbox."""
import copy
import hashlib
import json
import re
from pathlib import Path
from .sandbox_options import MAIN_AGENT_SANDBOX

BUILTINS = ('base', 'developer-postgres', 'deep-research', 'local-knowledge')
LABELS = {'workspace': MAIN_AGENT_SANDBOX, 'gateway': 'Network gateways',
          'ssh': 'SSH & Git', 'inference': 'Model connections', 'aws': 'AWS',
          'advisor': 'Advisors', 'connector': 'Service connectors',
          'pi-common': 'Pi tools', 'pi-stock': 'Pi tools', 'native-common': 'Agent tools',
          'controller-codex': 'Codex', 'controller-claude': 'Claude Code',
          'controller-agy': 'Antigravity'}
STATUS = {'available': 'Available', 'current': '', 'custom': 'Custom profile', 'unknown': 'Cannot compare'}


def image_id(value):
    """Docker and Podman can spell the same content ID with or without sha256:."""
    if isinstance(value, str) and re.fullmatch(r'(sha256:)?[a-f0-9]{64}', value):
        return value.removeprefix('sha256:')
    return value


def workspace_inputs(config):
    """Fingerprint only Factory layers consumed by this workspace variant."""
    from . import image_sources, primary_workspace, sandbox
    from .sandbox_options import options
    if primary_workspace.agent(config) == 'pi':
        return config['common']['digest']
    common = Path(config['common']['path'])
    paths = [common / 'cli-tools']
    if options(config.get('options'))['browser']:
        paths += [common / 'browser', common / 'security']
    if config.get('aws_access'):
        paths += [sandbox.ROOT / 'containers/aws-access/client']
    return hashlib.sha256(json.dumps([image_sources._files(path, executable_bits_only=True) for path in paths]).encode()).hexdigest()


def builtin(config):
    """A user folder named 'base' is still a custom profile."""
    profile = config.get('profile') or {}
    if config.get('editable_profile') or profile.get('customized_from'):
        return None
    identifier = profile.get('factory_profile') or profile.get('registry_name')
    return identifier if identifier in BUILTINS else None


def runtime_roles(config):
    from . import sandbox, sandbox_advisors, sandbox_ssh, inference, aws_access, primary_agents, profile_services
    from .connectors.runtime import worker_name
    roles = {'workspace': {sandbox.names(config)[0]}}
    gateways = set()
    if sandbox.network_mode(config) == 'allowlist':
        gateways.add(sandbox.names(config)[1])
    if sandbox_advisors.enabled(config):
        worker, gateway, *_ = sandbox_advisors.names(config)
        roles['advisor'] = {worker}
        gateways.add(gateway)
    if primary_agents.enabled(config):
        worker, gateway, *_ = primary_agents.names(config)
        roles['controller-' + primary_agents.agent(config)] = {worker}
        gateways.add(gateway)
    if profile_services.selected(config):
        gateways.update({profile_services.prefix(config) + '-gateway', profile_services.prefix(config) + '-proxy'})
    if gateways:
        roles['gateway'] = gateways
    if sandbox_ssh.enabled(config):
        roles['ssh'] = {sandbox_ssh.names(config)[0]}
    if inference.enabled(config):
        roles['inference'] = {inference.names(config)[0]}
    if config.get('aws_access'):
        roles['aws'] = {aws_access.worker_name(config)}
    if config.get('connector_ids'):
        roles['connector'] = {worker_name(config, identifier) for identifier in config['connector_ids']}
    return roles


def _receipt(state):
    path = Path(state) / 'container-updates/applied.json'
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('roles'), dict):
        raise ValueError(f'Cannot read applied container versions at {path}. Rebuild this sandbox to record its versions.')
    for role, item in value['roles'].items():
        if not isinstance(item, dict):
            raise ValueError(f'Cannot read the {role} applied version at {path}: expected an image record. Rebuild this sandbox to record its versions.')
    return value['roles']


def _artifacts(rows, source):
    if not isinstance(rows, list) or any(not isinstance(item, dict) or not isinstance(item.get('role'), str) for item in rows):
        raise ValueError(f'Cannot read image versions in {source}: expected a list of image records with role names. Rebuild this sandbox to record its versions.')
    return {item['role']: item for item in rows}


def _live(config, roles, current=None):
    from . import sandbox
    return {name: (current.get(name) if current is not None else sandbox.inspect(config, name))
            for names in roles.values() for name in names}


def record_applied(config, state, *, roles_updated=()):
    """Publish only identities verified against the actual containers after readiness."""
    from . import profile_runtime, sandbox
    if not config.get('profile') or not config.get('common'):
        return
    roles = runtime_roles(config)
    live = _live(config, roles)
    repair = None
    try:
        saved = _receipt(state)
    except ValueError as error:
        # This is derived evidence, not configuration or credentials. A rebuild
        # must be able to repair malformed evidence instead of failing forever.
        repair, saved = str(error), {}
    applied = profile_runtime.applied(state) or {}
    artifacts = _artifacts(config.get('image_artifacts', []), 'the prepared build')
    mounted_artifacts = _artifacts(applied.get('image_artifacts', []), 'the applied build')
    for role, names in roles.items():
        containers = {name: {'id': live[name]['Id'], 'image': live[name]['Image']}
                      for name in names if live.get(name)}
        if len(containers) != len(names):
            continue
        if role == 'workspace':
            if {image_id(item['image']) for item in containers.values()} != {image_id(applied.get('image_id'))}:
                continue
            saved[role] = {'containers': containers, 'image_id': applied['image_id'],
                           'common_digest': applied.get('common_inputs_digest'),
                           'workspace_inputs_digest': applied.get('workspace_inputs_digest'),
                           'profile_id': applied.get('factory_profile'),
                           'builtin_digest': applied.get('factory_profile_digest'),
                           'artifacts': applied.get('image_artifacts', [])}
        else:
            artifact = artifacts.get(role)
            previous = saved.get(role)
            if role not in roles_updated:
                if previous and previous.get('containers') == containers:
                    continue
                if not previous and artifact != mounted_artifacts.get(role):
                    continue
            if artifact and {image_id(item['image']) for item in containers.values()} == {image_id(artifact['image_id'])}:
                saved[role] = {**artifact, 'containers': containers}
    sandbox.write_json(Path(state) / 'container-updates/applied.json', {'version': 1, 'roles': saved})
    if repair:
        print(f'Repaired the applied container version record at {Path(state) / "container-updates/applied.json"}.', flush=True)


def _target(config, identifier):
    from . import sandbox
    target = copy.deepcopy(config)
    target['common'] = sandbox.common_profile()
    target['profile'] = sandbox.load_profile(identifier)
    return target


def target_config(config, state, reviewed):
    """Snapshot installed recipes only when an approved plan is being executed."""
    from . import sandbox
    target = _target(config, reviewed['profile_id'])
    if reviewed['workspace']:
        for key in ('profile', 'common'):
            target[key] = sandbox.snapshot_profile(target[key], Path(state))
    else:
        target['profile'] = copy.deepcopy(config['profile'])
        target['common'] = copy.deepcopy(config['common'])
    return target


def plan(config, state, *, current=None, cache=None):
    """No downloads, builds, snapshot writes or custom Dockerfile inspection."""
    from . import image_sources, profile_runtime, profile_services
    identifier = builtin(config)
    result = dict(status='custom' if identifier is None else 'current', profile_id=identifier,
                  roles=[], workspace=False, profile_services=False, changes=[])
    if identifier is None:
        result['reason'] = 'You maintain this custom profile’s software and services. Use Rebuild sandbox to rebuild it.'
        return result
    try:
        if cache is None:
            target = _target(config, identifier)
        else:
            # One inventory has one installed source set. Do not rehash the
            # shared base separately for every saved sandbox.
            from . import sandbox
            if 'common' not in cache:
                cache['common'] = sandbox.common_profile()
            profiles = cache.setdefault('profiles', {})
            if identifier not in profiles:
                profiles[identifier] = sandbox.load_profile(identifier)
            target = {**config, 'common': cache['common'], 'profile': profiles[identifier]}
        roles = runtime_roles(config)
        live = _live(config, roles, current)
        saved = _receipt(state)
        applied = {}
        main = live.get(next(iter(roles['workspace'])))
        # The container mount, not a later build or mutable tag, owns this evidence.
        if main:
            mounted = next((m.get('Source') for m in main.get('Mounts', [])
                            if m.get('Destination') == profile_runtime.PATH), None)
            if mounted:
                applied = json.loads((Path(mounted) / 'build.json').read_text())
                if not isinstance(applied, dict):
                    raise ValueError(f'Cannot read {Path(mounted) / "build.json"}: expected an applied build record')
        verified_workspace = main and image_id(applied.get('image_id')) == image_id(main.get('Image'))
        artifacts = _artifacts(applied.get('image_artifacts', []), 'the applied build') if verified_workspace else {}
        changed = set()
        targets = {}
        missing = []
        if not verified_workspace:
            missing.append('the main agent sandbox’s applied build record')
        else:
            old_profile = applied.get('factory_profile_digest')
            from . import primary_workspace
            old_inputs = (applied.get('common_inputs_digest') if primary_workspace.agent(config) == 'pi'
                          else applied.get('workspace_inputs_digest'))
            target_inputs = workspace_inputs(target)
            if old_profile is None or old_inputs is None:
                missing.append('the main agent sandbox’s Factory source versions')
            else:
                if old_profile != target['profile']['factory_profile_digest'] or old_inputs != target_inputs:
                    changed.add('workspace')
                if old_profile != target['profile']['factory_profile_digest']:
                    service_changes = profile_services.changes(config, target)
                    result['profile_services'] = any(service_changes.values())
                    result['service_changes'] = service_changes
            targets['workspace'] = [target['profile']['factory_profile_digest'], target_inputs]
        catalog = image_sources.index() if image_sources.mode(config) == 'prebuilt' else {}
        for role in sorted((set(roles) - {'workspace'}) | (set(artifacts) & {'pi-stock', 'pi-common', 'native-common'})):
            old = saved.get(role) or artifacts.get(role)
            if not old or not old.get('recipe'):
                missing.append(LABELS[role] + ' applied version')
                continue
            if role in roles:
                instances = {name: live.get(name) for name in roles[role]}
                if any(not info or image_id(info.get('Image')) != image_id(old.get('image_id')) for info in instances.values()):
                    missing.append(LABELS[role] + ' container versions')
                    continue
            key = (role, config['uid'], config['gid'])
            recipes = cache.setdefault('recipes', {}) if cache is not None else {}
            if key not in recipes:
                recipes[key] = image_sources.recipe(target, role)
            recipe = recipes[key]
            if old.get('source', 'prebuilt') == 'prebuilt' and not old.get('platform'):
                missing.append(LABELS[role] + ' build platform')
                continue
            candidates = catalog.get(recipe, {})
            if not isinstance(candidates, dict):
                raise ValueError(f'The tested {role} image list at {image_sources.INDEX} must map platforms to digest references. Reinstall Factory or select Build images locally')
            target_ref = candidates.get(old.get('platform'))
            if target_ref is not None and (not isinstance(target_ref, str) or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', target_ref)):
                raise ValueError(f'The tested {role} image for {old.get("platform")} has an invalid digest reference {target_ref!r}. Reinstall Factory or select Build images locally')
            targets[role] = [recipe, target_ref]
            if old['recipe'] != recipe or (old.get('source', 'prebuilt') == 'prebuilt'
                                          and target_ref and target_ref != old.get('reference')):
                changed.add(role)
        result['roles'] = sorted(changed)
        result['workspace'] = bool(changed & {'workspace', 'pi-stock', 'pi-common', 'native-common',
                                            'controller-codex', 'controller-claude', 'controller-agy'})
        result['changes'] = [{'role': role, 'label': LABELS[role]} for role in sorted(changed)]
        result['effects'] = ([
            'Running work in the main agent sandbox stops after preparation. Workspace files, saved chats and sign-ins are kept.',
            'Packages installed directly in the container must be installed again.',
        ] if result['workspace'] else ['Only the listed Factory helper containers restart.']) if changed else []
        if result['profile_services']:
            result['effects'].append('Changed profile services restart. Factory keeps their previous data under Saved service data.')
        if 'ssh' in changed:
            result['effects'].append('Unlocked SSH & Git keys need to be unlocked again after this update.')
        # Incomplete evidence never silently turns into a claim that everything is current.
        result['unknown'] = missing
        result['status'] = 'available' if changed else 'unknown' if missing else 'current'
        if missing:
            result['reason'] = 'Cannot compare ' + ', '.join(missing) + '. Rebuild this sandbox to record its applied versions.'
        evidence = {'applied': applied, 'receipts': saved, 'live': {n: [v.get('Id'), v.get('Image')] if v else None for n, v in live.items()},
                    'targets': targets, 'roles': result['roles'], 'profile_id': identifier}
        result['revision'] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(status='unknown', reason=f'Cannot compare container updates for {config["name"]}: {error}. Check the engine and saved build records, then retry.')
    return result
