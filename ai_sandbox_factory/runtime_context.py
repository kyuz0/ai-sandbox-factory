"""Public, model-readable settings for the next turn of a sandbox conversation."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial, wraps
import hashlib
from inspect import signature
import json
from pathlib import Path

from . import folders, network_groups, primary_workspace, prompt_library, web_access
from .connectors.context import PREVIEW_CHARACTERS, write_text
from .sandbox_options import APPROVAL_MODES, guidance, options

_transactions = ContextVar('sandbox_settings_transactions', default=frozenset())
REFERENCE = '/opt/sandbox/context.md'


def _digest(public_value):
    data = json.dumps(public_value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def _names(values):
    return ', '.join(sorted(set(values))) or 'None'


def _switch(enabled):
    return 'On' if enabled else 'Off'


def pi_security(config, selected, network):
    return guidance(selected, config.get('subagents', False), network=network) + folders.guidance(config)


def snapshot(config, state):
    """Project only sandbox-visible choices. Never serialize config, environment or credentials."""
    from . import profile_edit, sandbox
    state = Path(state)
    agent = primary_workspace.agent(config)
    selected = options(config.get('options'))
    mode = sandbox.network_mode(config)
    values = {'Network': 'Restricted' if mode == 'allowlist' else 'Unrestricted'}
    if mode == 'allowlist':
        rules = state / 'allowlist.txt'
        destinations = sandbox.parse_rules(rules.read_text()) if rules.is_file() else []
        values['Additional allowed destinations'] = _names(f'{host}:{port}' for host, port in destinations)
        categories = []
        for name in selected['website_groups']:
            domains = config.get('website_groups', {}).get(name, {}).get('domains', [])
            title = network_groups.catalog()[name]['title']
            categories.append(f'{title} ({len(domains)} domains, list {_digest(sorted(domains))[:12]})')
        values['Website categories'] = _names(categories)

    profile = config.get('profile') or {}
    if isinstance(profile, str):
        profile = {'name': Path(profile).name}
    if profile.get('name'):
        revision = profile.get('digest', '')
        values['Profile'] = profile['name'] + (f' (revision {revision})' if revision else '')
    values['Profile services'] = _names(profile.get('services', {}))
    values['Editable profile'] = _switch(config.get(profile_edit.EDIT))
    start = config.get('working_directory') or '.'
    values['Starting folder for new sessions'] = str(Path('/workspace') / start)
    values['Folders'] = _names(f'{path} ({"read-only" if ro else "read and write"})'
                               for path, ro in folders.roots(config))
    if config.get('memory'):
        values['Memory limit'] = config['memory']
    values['Browser'] = _switch(selected['browser'])
    values['Additional skills'] = _names(row['name'] + (f' ({row["digest"][:12]})' if row.get('digest') else '')
                                       for row in config.get('extra_skills', []))
    author = selected['git_identity']
    values['Git commit author'] = f'{author["name"]} <{author["email"]}>' if author else 'None'
    connectors = config.get('connector_operations', {})
    values['HTTPS connector grants'] = _names(
        f'{name} ({_names(connectors[name]) if name in connectors else "all assigned operations"})'
        for name in config.get('connector_ids', []))
    values['SSH destinations'] = _names(f'{row["alias"]} ({row["user"]}@{row["hostname"]}:{row["port"]})'
                                        for row in config.get('ssh', {}).get('hosts', []))
    aws = config.get('aws_access')
    if aws:
        from .aws_access import session_policy
        grants = (f'{", ".join(row["Action"])} on {", ".join(row["Resource"])}'
                  for row in session_policy(aws['grants'])['Statement'] if row['Effect'] == 'Allow')
        values['AWS permissions'] = f'{aws["region"]}, role {aws["role"]}: {_names(grants)}'
    else:
        values['AWS permissions'] = 'Off'
    custom = prompt_library.selection(state)
    label = 'System prompt add-on' if agent == 'pi' else 'System prompt add-on for newly opened clients'
    values[label] = f'{custom["name"]} (revision {_digest(custom["text"])[:12]})' if custom else 'None'
    client_context = {}
    if agent == 'pi':
        client_context['securityGuidance'] = pi_security(config, selected, mode)
        values['Shell approval'] = APPROVAL_MODES[selected['approval']][0]
        values['Saved approval model'] = selected['approval_model']
        search = selected['web_access']
        providers = _names(web_access.SERVICES[name][0] for name in web_access.active(search))
        values['Web Search'] = (f'On ({search["mode"]}, available providers: {providers})'
                                if search['enabled'] else 'Off')
        if search['enabled']:
            values['Web Search destinations'] = _names(f'{host}:{port}' for host, port in web_access.destinations(search))
        for label, key in (('Inner command sandbox', 'inner_sandbox'), ('Goal tracking', 'goals'),
                           ('Scheduled prompts', 'scheduling'), ('Question forms', 'question_forms')):
            values[label] = _switch(selected[key])
        if selected['inner_sandbox']:
            values['Protect workspace credentials'] = _switch(selected['protect_credentials'])
        if selected['browser']:
            values['Protect browser credentials'] = _switch(selected['protect_browser_credentials'])
        values['Subagents'] = _switch(config.get('subagents'))
        values['Conversation recall'] = selected['compaction_recall'].capitalize()
        if selected['knowledge_embedding'] != 'off':
            values['Document search embeddings'] = selected['knowledge_embedding_model'] or 'Local embeddings'
            if selected['knowledge_embedding'] == 'endpoint':
                values['Document search endpoint'] = selected['knowledge_embedding_url']
        values['Slack'] = _switch(selected['communications']['slack'])
        values['Advisors'] = _names(primary_workspace.AGENTS[name]
                                   for name in config.get('advisors', {}).get('providers', []))
        if config.get('local_model_ports'):
            values['Host-local model ports'] = config['local_model_ports']
        configured = config.get('model_providers', [])
        providers = configured.keys() if isinstance(configured, dict) else (row['name'] for row in configured)
        values['Model providers'] = _names([*providers, *config.get('hosted_connections', {})])
    else:
        values['Web Search for newly opened clients'] = _switch(selected[primary_workspace.WEB_SEARCH_OPTIONS[agent]])
        values['Approval after sandbox restart'] = selected['native_approval'].capitalize()
    revision = _digest({'values': values, **client_context})
    details = {}
    for label, value in values.items():
        if len(value) > PREVIEW_CHARACTERS:
            # Keep long lists out of every turn while retaining the entire public value on disk.
            details[label] = value
            values[label] = (f'{value[:PREVIEW_CHARACTERS]}… (full value in {REFERENCE}, '
                             f'revision {_digest(value)[:12]})')
    result = {'revision': revision, 'network': mode, 'values': values, **client_context}
    if details:
        result['details'] = details
    return result


def publish_saved(state):
    """Use the committed configuration and the same directory-mounted file on every revision."""
    state = Path(state)
    if state in _transactions.get() or (state / 'profile-preparation/record.json').exists():
        return
    config_file = state / 'config.json'
    if not config_file.is_file():
        return
    config = json.loads(config_file.read_text())
    directory = 'runtime-config' if primary_workspace.agent(config) == 'pi' else 'primary-runtime'
    current = snapshot(config, state)
    text = reference_text(config, state, current)
    current['guidanceRevision'] = _digest(text)
    current['revision'] = _digest([current['revision'], current['guidanceRevision']])
    reference = state / directory / 'sandbox-context.md'
    previous = reference.read_text() if reference.is_file() else ''
    try:
        write_text(reference, text)
        write_text(state / directory / 'sandbox-state.json',
                   json.dumps(current, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        write_text(reference, previous)
        raise


def reference_text(config, state, current):
    """Describe only agent-visible choices and instructions, never the private config object."""
    from . import primary_agents, profile_runtime, profile_services
    from .connectors import context
    state = Path(state)
    selected = options(config.get('options'))
    pi = primary_workspace.agent(config) == 'pi'
    values = {**current['values'], **current.get('details', {})}
    lines = ['# Current sandbox configuration', '',
             'Factory maintains this read-only reference after settings are successfully applied. '
             'It describes this sandbox, including changes made after a conversation started.', '',
             '## Settings', '', '```json', json.dumps(values, ensure_ascii=False, indent=2), '```', '',
             '## Guidance', '']
    if pi:
        lines.append(current['securityGuidance'].rstrip())
        lines.append(profile_runtime.guidance(state, config) + profile_services.guidance(config))
        profile = config.get('profile') or {}
        source = profile.get('path') if isinstance(profile, dict) else None
        if source and (Path(source) / 'profile.md').is_file():
            lines.extend(['## Profile instructions', '', (Path(source) / 'profile.md').read_text().rstrip()])
        if source and Path(source).is_dir():
            from . import registry
            lines.append(registry.profile_system_prompt(source, profile.get('name', 'Profile')))
        addon = prompt_library.selection(state)
        if addon:
            lines.extend(['## System prompt add-on', '', addon['text']])
    else:
        lines.append(primary_agents.guidance(config, state).rstrip())
    _, connector_reference = context.render(config, state)
    if connector_reference:
        lines.append(connector_reference.rstrip())
    if current['network'] == 'allowlist':
        categories = config.get('website_groups', {})
        if selected['website_groups']:
            lines.extend(['## Allowed website categories', '', '```json',
                          json.dumps({name: categories.get(name, {}).get('domains', [])
                                      for name in selected['website_groups']}, indent=2), '```'])
    shell = 'bash_escalate' if pi and selected['inner_sandbox'] and selected['approval'] != 'off' else 'bash'
    lines.extend(['## Examples for this sandbox', '',
                  'Inspect the project with the shell or file tools:', '', '```sh', 'pwd', 'ls /workspace', '```', '',
                  'If a system package is missing, tell the user its Debian package name and why it is needed. '
                  'Ask them to use Actions → Install system packages… in Factory.'])
    if current['network'] == 'unrestricted':
        lines.extend(['', 'Use an ordinary network request, for example:', '', '```sh',
                      'curl --head --fail-with-body https://example.com', '```'])
    else:
        lines.extend(['', 'Use the inherited proxy settings for HTTP and HTTPS. If a required destination is blocked, '
                      'ask the user to allow its hostname and port in Settings → Network and Web Search → Network.'])
    if selected['browser']:
        launch = shell if not (pi and selected['inner_sandbox'] and selected['approval'] == 'off') else 'the editor terminal'
        lines.extend(['', f'To preview a web page, start a server through {launch}, then open '
                      'http://localhost:8000 with the browser tool:', '', '```sh',
                      'nohup python3 -m http.server 8000 --bind 0.0.0.0 >/tmp/preview.log 2>&1 </dev/null &', '```'])
    return '\n\n'.join(part for part in lines if part).rstrip() + '\n'


def _config_version(state):
    # Factory saves config.json by atomic replacement under the sandbox lock.
    try:
        info = (state / 'config.json').stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_ctime_ns


@contextmanager
def applied(state, *, require_save=False):
    """Defer publication until the outer operation, including its readiness checks, succeeds."""
    state = Path(state)
    active = _transactions.get()
    if state in active:
        yield
        return
    before = _config_version(state) if require_save else None
    token = _transactions.set(active | {state})
    try:
        yield
    finally:
        _transactions.reset(token)
    if not require_save or before != _config_version(state):
        publish_saved(state)


def publish_after(operation, *, require_save=False):
    arguments = signature(operation)

    @wraps(operation)
    def run(*args, **kwargs):
        state = arguments.bind(*args, **kwargs).arguments['state']
        with applied(state, require_save=require_save):
            return operation(*args, **kwargs)
    return run


# Settings Apply can return successfully when cancelled or when a review has no changes.
publish_after_save = partial(publish_after, require_save=True)
