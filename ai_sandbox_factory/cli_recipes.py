"""Reproducible public CLI requests shared by previews and completed jobs."""
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import shutil
import sys

from . import credential_inputs, editor_settings
from .sandbox_options import options, SANDBOX_NAME_LIMIT

LABEL = 'CLI commands…'


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    stdin: str | None = None
    factory: bool = True


@dataclass(frozen=True)
class Recipe:
    title: str
    steps: tuple[Step, ...]
    state_root: str
    notes: tuple[str, ...] = ()

    def render(self, *, width=None, launcher=None):
        width = width or shutil.get_terminal_size().columns
        prefix = tuple(launcher or command())
        default = (Path.home() / '.local/state/ai-sandbox-factory').resolve()
        if Path(self.state_root).expanduser().resolve() != default:
            prefix += ('--state-dir', str(Path(self.state_root).expanduser().resolve()))
        blocks = []
        for step in self.steps:
            argv = (*prefix, *step.argv) if step.factory else step.argv
            tokens = [shlex.quote(str(item)) for item in argv]
            if step.stdin is not None:
                delimiter = 'FACTORY_CONFIG'
                while delimiter in step.stdin.splitlines():
                    delimiter += '_'
                tokens += ["<<'" + delimiter + "'"]
            lines, line = [], ''
            for token in tokens:
                if line and len(line) + len(token) + 3 > width:
                    lines.append(line + ' \\')
                    line = '  ' + token
                else:
                    line += (' ' if line else '') + token
            lines.append(line)
            if step.stdin is not None:
                lines.extend((step.stdin.rstrip('\n'), delimiter))
            blocks.append('\n'.join(lines))
        text = '\n\n'.join(blocks) + '\n'
        # Stop a pasted multi-command recipe after failure without exiting the user's shell.
        return '(\nset -e\n\n' + text + ')\n' if len(blocks) > 1 else text


def command():
    """Use this checkout when the PATH command belongs to another installation."""
    candidate = Path(sys.executable).parent / 'ai-sandbox-factory'
    found = shutil.which('ai-sandbox-factory')
    if candidate.exists():
        return (str(candidate),) if not found or Path(found).resolve() != candidate.resolve() else ('ai-sandbox-factory',)
    # A checkout launched with python -m may not have a console script installed.
    # Keep its recipes on this version even when PATH contains an older installation.
    from .host_platform import python_command
    return tuple(python_command('ai_sandbox_factory'))


def document(argv, value):
    return Step(tuple(str(part) for part in argv), json.dumps(value, indent=2, ensure_ascii=False))


def update(name, changes, state_root, *, title='Apply changes', following=()):
    return Recipe(title, (document(('update', name, '--settings-file', '-'), changes), *following), str(state_root))


def credential_choice(name, provider, choice, *, refresh=True):
    argv = ['credentials', 'assign', name, '--provider', provider]
    if choice.get('ref'):
        argv += ['--ref', choice['ref']]
    else:
        for field, flag in (('label', '--remember'), ('replace', '--replace')):
            if choice.get(field):
                argv += [flag, choice[field]]
    if not refresh:
        argv.append('--no-refresh')
    return Step(tuple(argv))


def key_changes(name, values, *, communication=False):
    argv = ['communication-key' if communication else 'web-key', name]
    for provider, value in values.items():
        argv += [('--token' if value else '--clear-token') if communication else
                 ('--prompt-provider' if value else '--clear-provider'), provider]
    return Step(tuple(argv))


def full_update(draft, keys, state_root):
    from .operations import setup_requests, update_changes
    name = draft['name']
    following = []
    for action, provider, value in setup_requests(draft, keys):
        if action == 'local-keys':
            following.extend(credential_choice(name, 'local-' + str(port), choice, refresh=False)
                             for port, choice in value.items())
        elif action == 'provider-key':
            following.append(Step(('configure', name, '--provider', provider, '--key')))
        elif action == 'provider-choice':
            following.append(credential_choice(name, provider, value))
        elif action == 'communication-keys':
            following.append(key_changes(name, value, communication=True))
        elif action == 'web-keys':
            following.append(key_changes(name, value))
        elif action == 'discover':
            following.append(Step(('configure', name)))
    if draft.get('open_editor'):
        following.append(Step(('open', name)))
    return update(name, update_changes(draft), state_root, following=following)


def network(name, text, groups, state_root, *, revision=None, search_change=None, reopen=False):
    argv = ['network', name, '--allowlist', '-', '--website-groups', *groups]
    if revision:
        argv += ['--curlie-list', revision]
    steps = [Step(tuple(argv), text)]
    if search_change:
        steps.append(document(('update', name, '--settings-file', '-'), search_change))
    if reopen:
        steps.append(Step(('open', name)))
    return Recipe('Apply network rules', tuple(steps), str(state_root))


def editor(name, changes, state_root):
    value = copy.deepcopy(changes)
    argv = ['editor', 'set', name]
    if 'password_hash' in value:
        password = value.pop('password_hash')
        argv.append('--prompt-password' if password else '--clear-password')
    if value:
        argv += ['--settings-file', '-']
        step = document(argv, value)
    else:
        step = Step(tuple(argv))
    return Recipe('Save editor settings', (step,), str(state_root),
                  ('Asks for the editor password.',) if '--prompt-password' in argv else ())


def _prompt(path, label, *, settings=None):
    return {'path': list(path), 'label': label, **({'options': settings} if settings else {})}


def creation(draft, state_root, *, web_keys=None, optional_credentials=()):
    """Project selected public settings, never raw tokens or internal worker files."""
    value = {}
    for key in ('workspace', 'profile', 'engine', 'primary_agent', 'primary_auth', 'network',
                'memory', 'subagents', 'allowlist', 'working_directory', 'extra_folders', 'model_providers',
                'local_model_ports', 'ssh', 'aws_access', 'advisors', 'library_skills',
                'skill_repo', 'skill_path', 'system_prompt', 'env_file', 'pi_config', 'image_source'):
        if key in draft:
            value[key] = copy.deepcopy(draft[key])
    if value.get('advisors') == {'providers': []}:
        value.pop('advisors')
    value.setdefault('primary_agent', 'pi')
    value.setdefault('engine', 'podman')
    selected_options = options(draft.get('options'))
    for row in selected_options['web_access']['providers'].values():
        row['has_key'] = False
    baseline = options()
    value['options'] = {key: setting for key, setting in selected_options.items() if setting != baseline[key]}
    prompts, refs = [], {}
    for group, field in (('_provider_keys', 'provider_keys'), ('_communication_keys', 'communication_keys')):
        for identifier, secret in draft.get(group, {}).items():
            if secret:
                prompts.append(_prompt((field, identifier),
                    f'{identifier} API key' if field == 'provider_keys' else f'Slack {identifier}'))
    for field, group in (('_provider_credential_choices', 'provider_credentials'), ('_local_model_keys', 'local_model_keys')):
        for identifier, choice in draft.get(field, {}).items():
            if choice.get('ref'):
                refs['local-' + str(identifier) if group == 'local_model_keys' else identifier] = choice['ref']
            elif choice.get('key'):
                prompts.append(_prompt((group, str(identifier), 'key'), f'{identifier} API key',
                    settings={key: choice[key] for key in ('label', 'replace') if choice.get(key)}))
    hosted = {}
    for provider, row in draft.get('_hosted_changes', {}).items():
        hosted[provider] = {key: row[key] for key in ('endpoint', 'region', 'method') if key in row}
        choice = row.get('credential', {})
        if choice.get('ref'):
            refs[provider] = choice['ref']
        elif choice.get('key'):
            prompts.append(_prompt(('hosted', provider, 'credential', 'key'), f'{provider} API key',
                settings={key: choice[key] for key in ('label', 'replace') if choice.get(key)}))
        else:
            for field in ('key', 'access_key', 'secret_key', 'session_token'):
                if row.get(field):
                    prompts.append(_prompt(('hosted', provider, field), f'{provider} {field.replace("_", " ")}'))
    if hosted:
        value['hosted_connections'] = hosted
    if refs:
        value['credential_refs'] = refs
    for provider, key in (web_keys or {}).items():
        if key:
            prompts.append(_prompt(('web_keys', provider), f'{provider} Web Search API key'))
    selected_editor = copy.deepcopy(draft.get('editor', editor_settings.DEFAULTS))
    if isinstance(selected_editor, str):
        selected_editor = {'mode': selected_editor}
    if selected_editor.pop('password_hash', ''):
        prompts.append(_prompt(('editor', 'password'), 'Editor password'))
    value['editor'] = {key: setting for key, setting in selected_editor.items()
                       if key == 'mode' or setting != editor_settings.DEFAULTS[key]}
    if prompts:
        value['credential_prompts'] = [
            {key: setting for key, setting in row.items() if key != 'required' and (key != 'options' or setting)}
            for row in credential_inputs.declarations(prompts)]
    if '_advisor_imports' in draft:
        value['advisor_imports'] = list(draft['_advisor_imports'])
    identifiers = draft.get('connector_ids', [])
    if identifiers:
        grants = draft.get('connector_operations', {})
        reviewed = '_connector_routes' in draft or '_connector_instructions' in draft
        value['connectors'] = [identifier + (':' + ','.join(grants[identifier]) if not reviewed and grants.get(identifier) else '')
                               for identifier in identifiers]
        if reviewed:
            value['connector_review'] = {'routes': copy.deepcopy(draft.get('_connector_routes', {})),
                'agent_instructions': copy.deepcopy(draft.get('_connector_instructions', {})),
                'operations': copy.deepcopy(grants)}
    if draft.get('_curlie_list'):
        value['curlie_list'] = draft['_curlie_list']
    for prompt in value.get('credential_prompts', []):
        if tuple(prompt['path']) in optional_credentials:
            prompt['required'] = False
            prompt['label'] += ' (press Enter to skip)'
    argv = ['create', draft['name'], '--config', '-', '--start']
    notes = []
    if draft.get('reuse_from'):
        argv += ['--reuse-from', draft['reuse_from']]
        if draft.get('_reuse_config_revision'):
            argv += ['--reuse-config-revision', draft['_reuse_config_revision']]
        for skill in draft.get('remove_skills', []):
            argv += ['--omit-skill', skill]
        if draft.get('share_editable_profile') and draft.get('source_editable_profile'):
            argv += ['--reuse-editable-profile']
        notes.append(f'Uses the saved packages from {draft["reuse_from"]} on this computer.')
    if draft.get('open_editor'):
        argv.append('--open')
    for field in ('allowlist', 'extra_folders', 'model_providers', 'local_model_ports', 'advisors',
                  'library_skills', 'skill_repo', 'skill_path', 'env_file', 'pi_config', 'ssh', 'aws_access'):
        if value.get(field) in (None, '', [], {}):
            value.pop(field, None)
    if value.get('working_directory') == '.':
        value.pop('working_directory')
    steps = []
    if draft.get('mkdir'):
        steps.append(Step(('mkdir', '-p', '--', draft['workspace']), factory=False))
    simple_fields = {'workspace', 'profile', 'engine', 'primary_agent', 'primary_auth', 'memory',
                     'subagents', 'network', 'working_directory', 'image_source', 'options', 'editor'}
    meaningful = {key: setting for key, setting in value.items() if setting not in (None, '', [], {})}
    simple = (not draft.get('reuse_from') and value.get('profile') == 'base'
              and set(meaningful) <= simple_fields and not value['options']
              and set(selected_editor) <= set(editor_settings.DEFAULTS)
              and all(setting == editor_settings.DEFAULTS[key] for key, setting in selected_editor.items() if key != 'mode'))
    if simple:
        argv = ['create', draft['name']]
        for field, flag in (('workspace', '--workspace'), ('profile', '--profile'), ('engine', '--engine'),
                            ('primary_agent', '--primary-agent'), ('primary_auth', '--primary-auth'),
                            ('memory', '--memory'), ('network', '--network'),
                            ('working_directory', '--working-directory'), ('image_source', '--image-source')):
            if value.get(field) is not None and value.get(field) != '':
                argv += [flag, str(value[field])]
        argv += ['--subagents' if value.get('subagents') else '--no-subagents',
                 '--editor', selected_editor.get('mode', 'browser'), '--start']
        if draft.get('open_editor'):
            argv.append('--open')
        steps.append(Step(tuple(argv)))
    else:
        steps.append(document(argv, value))
    if prompts:
        notes.append('Asks for the required keys and passwords before creating the sandbox.')
    return Recipe('Create sandbox', tuple(steps), str(state_root), tuple(notes))


def saved(config, state_root, *, existing_names=(), credential_status=None):
    """An explicit local creation request retaining the applied package snapshots."""
    from . import connectors, prompt_library, sandbox
    name = original = config['name'][:SANDBOX_NAME_LIMIT - len('-copy')].rstrip('-') + '-copy'
    number = 2
    while name in existing_names:
        suffix = '-' + str(number)
        name, number = original[:SANDBOX_NAME_LIMIT - len(suffix)] + suffix, number + 1
    state = Path(state_root) / config['name']
    draft = copy.deepcopy(config)
    draft.update(name=name, profile=config['profile']['path'],
                 model_providers={row['name']: row.get('settings', {}) for row in config.get('model_providers', [])},
                 editor=editor_settings.copy_choice(editor_settings.load(state)), system_prompt=prompt_library.selection(state),
                 allowlist=(state / 'allowlist.txt').read_text(), reuse_from=config['name'],
                 _reuse_config_revision=sandbox.config_revision(config), open_editor=False, env_file='', pi_config='')
    # Creation's public schema uses paths; the reuse source supplies frozen skills/providers.
    draft.pop('common', None)
    draft.pop('extra_skills', None)
    notices = []
    draft['options'] = options(draft.get('options'))
    if draft['options']['knowledge_embedding'] == 'local':
        draft['options']['knowledge_embedding'] = 'off'
        notices.append('The new sandbox uses keyword document search. Choose an embedding server in Document search for semantic search.')
    draft['_hosted_changes'] = {provider: {**row, 'provider': provider}
                               for provider, row in config.get('hosted_connections', {}).items()}
    assignments, optional = (credential_status or {}).get('assignments', {}), set()
    for provider, row in draft['_hosted_changes'].items():
        if assignments.get(provider, {}).get('ref'):
            row['credential'] = {'ref': assignments[provider]['ref']}
            continue
        if provider == 'amazon-bedrock' and row.get('method') == 'iam':
            row.update(access_key=True, secret_key=True, session_token=True)
            optional.add(('hosted', provider, 'session_token'))
        else:
            row['key'] = True
    keys, choices = {}, {}
    for provider in config.get('model_providers', []):
        if provider['name'] == 'local':
            continue
        if assignments.get(provider['name'], {}).get('ref'):
            choices[provider['name']] = {'ref': assignments[provider['name']]['ref']}
            continue
        if any(spec.get('type') == 'secret' for spec in provider['manifest'].get('settings', {}).values()):
            keys[provider['name']] = True
        elif provider['manifest'].get('inference', {}).get('auth', {}).get('type') in ('bearer', 'header'):
            choices[provider['name']] = {'key': True}
    draft['_provider_keys'] = keys
    draft['_provider_credential_choices'] = choices
    if config.get('local_model_ports'):
        for port in sandbox.local_model_ports(config['local_model_ports']):
            provider = 'local-' + str(port)
            if assignments.get(provider, {}).get('ref'):
                choice = {'ref': assignments[provider]['ref']}
            elif credential_status is None or provider in credential_status.get('configured', []):
                choice = {'key': True}
                if credential_status is None:
                    optional.add(('local_model_keys', str(port), 'key'))
            else:
                continue
            draft.setdefault('_local_model_keys', {})[str(port)] = choice
    if config.get('connector_ids'):
        plugins = connectors.saved(config, state)
        draft.update(_connector_routes={item.id: dict(item.routes) for item in plugins},
                     _connector_instructions={item.id: item.reviewed_instructions for item in plugins})
    if options(config.get('options'))['communications']['slack']:
        draft['_communication_keys'] = {'botToken': True, 'userToken': True}
        optional.add(('communication_keys', 'userToken'))
    recipe = creation(draft, state_root, web_keys={provider: True for provider, row in
        options(config.get('options'))['web_access']['providers'].items() if row.get('has_key')}, optional_credentials=optional)
    return Recipe('Create with these settings', recipe.steps, recipe.state_root,
                  (*recipe.notes, *notices,
                   'Uses the existing workspace. Sign-ins, credential files and private editor connection settings are not copied.'))
