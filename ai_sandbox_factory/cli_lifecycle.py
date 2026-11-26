"""Foreground supervision for the same workers used by the TUI."""
from contextlib import contextmanager
from collections import deque
import copy
import errno
from pathlib import Path
import signal
import sys
import tempfile
import threading

from . import (aws_access, connector_settings, creation, editor_settings, folders, host_platform, image_sources,
               operations, operation_locks, primary_workspace, profile_spec,
               sandbox, sandbox_advisors, sandbox_ssh, updating, web_access)
from .cli_support import emit, read_json, redact, remember_secret, silence_closed_stream
from .sandbox_options import options, memory_limit, SANDBOX_NAME, SANDBOX_NAME_ERROR

GATED = ('update', 'recreate', 'install-system-packages', 'profile-fork', 'profile-apply',
         'service-data-export', 'service-data-restore')
REQUEST_FIELDS = {'profile', 'workspace', 'primary_agent', 'primary_auth', 'network', 'memory',
                  'options', 'editor', 'working_directory', 'extra_folders', 'model_providers',
                  'local_model_ports', 'ssh', 'aws_access', 'advisors', 'library_skills',
                  'skill_repo', 'skill_path', 'system_prompt', 'connectors', 'allowlist',
                  'subagents', 'env_file', 'pi_config', 'hosted_connections', 'credential_refs', 'engine',
                  'advisor_imports', 'image_source'}
SECRET_FIELDS = {'provider_keys', 'provider_credentials', 'local_model_keys', 'hosted',
                 'communication_keys', 'web_keys'}


class Foreground:
    def __init__(self, controller, *, structured=False):
        self.controller = controller
        self.stream = sys.stderr if structured else sys.stdout
        self.closed = False
        self.cancelling = False

    def cancel(self):
        accepted = self.controller.cancel()
        self.cancelling = self.cancelling or accepted
        return accepted

    def log(self, text):
        if self.closed:
            return
        try:
            print(operations.safe_text(redact(text)), file=self.stream, flush=True)
        except OSError as error:
            if error.errno not in (errno.EPIPE, errno.EIO):
                raise
            self.closed = True
            silence_closed_stream(self.stream)
            self.cancel()

    def progress(self, number, total, label):
        try:
            print(f'Step {number} of {total} · {label}', file=sys.stderr, flush=True)
        except OSError as error:
            if error.errno not in (errno.EPIPE, errno.EIO):
                raise
            silence_closed_stream(sys.stderr)
            self.cancel()

    @contextmanager
    def signals(self):
        previous = {}
        pending = deque()
        finished = threading.Event()
        def request(number, frame):
            # A signal can interrupt a controller while its non-reentrant lock is
            # held. Do not acquire locks or write to buffered streams here.
            pending.append(number)
        def cancel():
            accepted = self.cancel()
            message = ('Cancellation requested; waiting for cleanup.' if accepted else
                       'Cancellation is already in progress; waiting for cleanup.' if self.cancelling else
                       'The operation is applying or finishing; waiting for a consistent result.')
            try:
                print(message, file=sys.stderr, flush=True)
            except OSError as error:
                if error.errno not in (errno.EPIPE, errno.EIO):
                    raise
                silence_closed_stream(sys.stderr)
        def dispatch():
            while not finished.is_set() or pending:
                while pending:
                    pending.popleft()
                    cancel()
                finished.wait(0.05)
        dispatcher = None
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[number] = signal.signal(number, request)
            dispatcher = threading.Thread(target=dispatch, name='factory-cli-signals')
            dispatcher.start()
        try:
            yield self
        finally:
            finished.set()
            if dispatcher:
                dispatcher.join()
            for number, handler in previous.items():
                signal.signal(number, handler)


def secrets(args):
    value = read_json('-') if getattr(args, 'secrets_stdin', False) else {}
    if not isinstance(value, dict) or set(value) - SECRET_FIELDS:
        raise ValueError('Creation secrets must use provider_keys, provider_credentials, local_model_keys, hosted, communication_keys or web_keys.')
    for field, rows in value.items():
        if not isinstance(rows, dict):
            raise ValueError(f'Creation secrets field {field} must be a JSON object keyed by provider.')
        for provider, choice in rows.items():
            if field in ('provider_keys', 'communication_keys', 'web_keys'):
                if not isinstance(choice, str):
                    raise ValueError(f'Creation secrets field {field}.{provider} must be a string.')
            elif not isinstance(choice, dict):
                raise ValueError(f'Creation secrets field {field}.{provider} must be an object with credential choices.')
            elif field in ('provider_credentials', 'local_model_keys'):
                unknown = set(choice) - {'key', 'ref', 'label', 'replace'}
                if unknown:
                    raise ValueError(f'Unknown credential choices for {field}.{provider}: ' + ', '.join(sorted(unknown)))
                if any(item is not None and not isinstance(item, str) for item in choice.values()):
                    raise ValueError(f'Credential choices for {field}.{provider} must be strings.')
            elif field == 'hosted':
                if set(choice) - {'key', 'access_key', 'secret_key', 'session_token', 'credential'}:
                    raise ValueError(f'Hosted credentials for {provider} must use key, access_key, secret_key, session_token or credential.')
                for name, item in choice.items():
                    if name == 'credential':
                        if not isinstance(item, dict) or set(item) - {'key', 'ref', 'label', 'replace'}:
                            raise ValueError(f'Hosted credential choice for {provider} must use key, ref, label or replace.')
                        if any(child is not None and not isinstance(child, str) for child in item.values()):
                            raise ValueError(f'Hosted credential choices for {provider} must be strings.')
                    elif not isinstance(item, str):
                        raise ValueError(f'Hosted credential field {provider}.{name} must be a string.')
    for key in ('provider_keys', 'communication_keys', 'web_keys'):
        for secret in value.get(key, {}).values():
            remember_secret(secret)
    def register_fields(choice):
        if isinstance(choice, dict):
            for field, child in choice.items():
                if field in ('key', 'access_key', 'secret_key', 'session_token'):
                    remember_secret(child)
                elif isinstance(child, dict):
                    register_fields(child)
    for key in ('provider_credentials', 'local_model_keys', 'hosted'):
        for choice in value.get(key, {}).values():
            register_fields(choice)
    return value


def complete_draft(value, args):
    if not isinstance(value, dict) or set(value) - REQUEST_FIELDS:
        invalid = sorted(set(value) - REQUEST_FIELDS) if isinstance(value, dict) else ['not an object']
        raise ValueError(f'Unknown creation fields: {", ".join(invalid)}. Use the documented CLI creation fields.')
    for field in ('model_providers', 'hosted_connections', 'credential_refs', 'ssh', 'advisors', 'aws_access'):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(f'Creation field {field} must be a JSON object.')
    for field in ('library_skills', 'extra_folders', 'connectors', 'advisor_imports'):
        if field in value and not isinstance(value[field], list):
            raise ValueError(f'Creation field {field} must be a JSON list.')
    if 'profile' in value and not isinstance(value['profile'], str):
        raise ValueError('Creation field profile must be an installed profile name or folder path.')
    if 'credential_refs' in value and any(not isinstance(ref, str) for ref in value['credential_refs'].values()):
        raise ValueError('credential_refs must map provider IDs to saved key reference strings.')
    baseline = operations.load_defaults() if getattr(args, 'use_defaults', False) else copy.deepcopy(operations.DEFAULT_SETTINGS)
    if not getattr(args, 'use_defaults', False):
        baseline.update(memory=None, primary_agent='pi', primary_auth=primary_workspace.DEFAULT_AUTH,
                        library_skills=[], open_editor=False, editor={**editor_settings.DEFAULTS})
    profile = sandbox.load_profile(value.get('profile') or baseline['profile'])
    draft = operations.preset(value.get('profile') or baseline['profile'], baseline, profile.get('defaults'))
    if primary_workspace.agent(value) != 'pi':
        draft.update(model_providers={}, local_model_ports=None, subagents=False,
                     allowlist='', env_file='', pi_config='', advisors={},
                     editor={'mode': editor_settings.default_for_agent(primary_workspace.agent(value), draft['engine'])})
        draft['options'] = options({'git_identity': draft['options']['git_identity'],
                                    'website_groups': draft['options']['website_groups']})
    draft.update(copy.deepcopy(value), name=args.name)
    draft['options'] = options(draft.get('options'))
    if isinstance(draft.get('editor'), str):
        draft['editor'] = {**editor_settings.DEFAULTS, 'mode': draft['editor']}
    draft['editor'] = editor_settings.validate(draft.get('editor'))
    if draft['editor'].get('password_hash'):
        raise ValueError('Creation settings cannot contain an editor password hash. Set it later with editor set --password-stdin.')
    draft['open_editor'] = bool(getattr(args, 'open', False))
    draft.setdefault('allowlist', '')
    pending = draft.pop('hosted_connections', {})
    for provider, connection in pending.items():
        if not isinstance(connection, dict) or set(connection) - {'endpoint', 'region', 'method'}:
            raise ValueError('Hosted connection settings must contain only non-secret fields. Supply credentials through --secrets-stdin.')
    if pending:
        from . import hosted_models
        for provider, row in pending.items():
            hosted_models.validate({**row, 'provider': provider})
        draft['_hosted_changes'] = {provider: {**row, 'provider': provider} for provider, row in pending.items()}
    if refs := draft.pop('credential_refs', {}):
        draft['_provider_credential_choices'] = {provider: {'ref': ref} for provider, ref in refs.items()}
    draft.setdefault('working_directory', '.')
    draft.setdefault('extra_folders', [])
    draft.setdefault('primary_agent', 'pi')
    draft.setdefault('primary_auth', primary_workspace.DEFAULT_AUTH)
    if 'model_providers' not in value and draft.get('local_model_ports'):
        draft['model_providers'] = {**draft.get('model_providers', {}), 'local': {}}
    primary_workspace.agent(draft)
    if draft['primary_auth'] not in ('copy', 'login'):
        raise ValueError('primary_auth must be copy or login.')
    imports = draft.pop('advisor_imports', [])
    if any(provider not in ('codex', 'claude', 'agy') for provider in imports):
        raise ValueError('advisor_imports must list codex, claude or agy.')
    if imports:
        if primary_workspace.agent(draft) != 'pi':
            raise ValueError('Advisor sign-in imports apply to Pi sandboxes.')
        if set(imports) - set(draft.get('advisors', {}).get('providers', [])):
            raise ValueError('Each advisor_imports entry must also be enabled in advisors.providers.')
        draft['_advisor_imports'] = imports
    if not draft.get('workspace'):
        raise ValueError('A workspace is required. Use --workspace PATH or set workspace in --config.')
    try:
        draft['workspace'] = str(host_platform.local_path(draft['workspace']).resolve(strict=True))
    except FileNotFoundError as error:
        raise ValueError(f'Workspace {draft["workspace"]!r} does not exist. Choose an existing project folder.') from error
    if draft.get('model_providers') is None:
        draft['model_providers'] = {}
    validate_draft(draft)
    grants = draft.pop('connectors', [])
    if grants:
        ids, selected = connector_settings.parse_cli_grants(grants)
        draft['connector_ids'] = ids
        draft['connector_operations'] = selected
    return draft


def validate_draft(draft, state_root=None):
    image_sources.choice(draft['image_source'])
    if draft['network'] not in ('allowlist', 'unrestricted'):
        raise ValueError(f'Unknown network mode {draft["network"]!r}. Choose allowlist or unrestricted.')
    memory_limit(draft.get('memory'))
    if type(draft.get('subagents')) is not bool:
        raise ValueError('subagents must be true or false.')
    if draft['network'] == 'unrestricted' and draft.get('allowlist', '').strip():
        raise ValueError('Unrestricted networking cannot include an allowlist. Remove the destinations or use allowlist networking.')
    draft['workspace'] = str(sandbox.directory(draft['workspace']))
    if primary_workspace.agent(draft) != 'pi' and any(draft.get(field) for field in
            ('model_providers', 'local_model_ports', 'pi_config', 'advisors', 'subagents',
             '_hosted_changes', '_provider_credential_choices')):
        raise ValueError('Model providers, local models, Pi configuration, advisors and subagents apply only to Pi sandboxes.')
    if state_root is None:
        return
    workspace = Path(draft['workspace'])
    if host_platform.comparison_path(workspace) in (Path('/'), host_platform.comparison_path(Path.home())):
        raise ValueError('Choose a project directory, not / or your home directory.')
    shared = sandbox.directory(draft['pi_config']) if draft.get('pi_config') else None
    pairs = [(workspace, Path(state_root) / draft['name'])]
    if shared:
        pairs += [(workspace, shared), (shared, Path(state_root) / draft['name'])]
    if any(folders.overlaps(first, second) for first, second in pairs):
        raise ValueError('Workspace, shared Pi config, and sandbox state must be separate, non-nested directories.')
    folders.working_directory(workspace, draft.get('working_directory', '.'))
    folders.validate_config(draft, state_root)
    if primary_workspace.agent(draft) != 'pi':
        primary_workspace.validate_mounts(draft, state_root)
    sandbox.validate_env(draft.get('env_file'))
    if draft.get('ssh'):
        sandbox_ssh.validate(draft['ssh'])
    if draft.get('advisors'):
        sandbox_advisors.validate(draft['advisors'])
    if draft.get('aws_access'):
        aws_access.settings(draft['aws_access'])
    rules = sandbox.parse_rules(draft.get('allowlist', ''))
    sandbox.configure_local_gateway(dict(draft), draft.get('local_model_ports'), rules)
    profile = sandbox.load_profile(draft['profile'])
    if not profile_spec.compatible(profile, primary_workspace.agent(draft)):
        raise ValueError(f'Profile {profile["name"]!r} does not support {primary_workspace.AGENTS[primary_workspace.agent(draft)]}. Choose a compatible profile.')


def from_arguments(args):
    if args.config == '-' and args.secrets_stdin:
        raise ValueError('Use a creation file when reading credentials from stdin; stdin cannot contain both documents.')
    value = read_json(args.config) if args.config else {}
    if not isinstance(value, dict):
        raise ValueError('Creation configuration must be a JSON object.')
    supplied = getattr(args, '_supplied', set())
    def choose(field, flag, arg=None):
        if flag in supplied or (not args.config and not args.use_defaults and getattr(args, arg or field, None) is not None):
            value[field] = getattr(args, arg or field)
    for field, flag in (('workspace','--workspace'),('profile','--profile'),('primary_agent','--primary-agent'),
                        ('primary_auth','--primary-auth'),('network','--network'),('memory','--memory'),
                        ('working_directory','--working-directory'),('local_model_ports','--local-model-ports'),
                        ('env_file','--env-file'),('pi_config','--pi-config'),('skill_path','--skill-path'),
                        ('image_source','--image-source')):
        choose(field, flag)
    if args.engine != 'auto' or '--engine' in supplied:
        value['engine'] = args.engine
    # Resolve engine precedence separately from the other saved creation choices.
    engine = value.pop('engine', args.engine if not args.use_defaults or '--engine' in supplied else None)
    for field, attribute in (('options','options_file'),('extra_folders','folders_file'),
                             ('model_providers','providers_file'),('ssh','ssh_file'),
                             ('aws_access','aws_access_file'),('system_prompt','system_prompt_file')):
        path = getattr(args, attribute, None)
        if path:
            value[field] = read_json(path)
    if args.model_provider:
        value['model_providers'] = {name: {} for name in args.model_provider}
    elif not args.config and args.local_model_ports and not args.providers_file:
        value['model_providers'] = {'local': {}}
    selected = dict(value.get('options') or {})
    for key in ('knowledge_embedding','knowledge_embedding_url','knowledge_embedding_model','browser'):
        setting = getattr(args, key, None)
        if setting is not None:
            selected[key] = setting
    if selected:
        value['options'] = selected
    if args.subagents is not None:
        value['subagents'] = args.subagents
    if args.editor:
        value['editor'] = args.editor
    if args.allowlist:
        value['allowlist'] = Path(args.allowlist).expanduser().read_text()
    elif args.allow_domain:
        value['allowlist'] = '\n'.join(args.allow_domain)
    if args.skill:
        value['library_skills'] = args.skill
    if args.skill_source:
        value['skill_repo'] = args.skill_source
    if args.advisor:
        value['advisors'] = {'providers': args.advisor}
    if args.connector:
        value['connectors'] = args.connector
    source = None
    if args.reuse_editable_profile:
        if not args.reuse_from:
            raise ValueError('--reuse-editable-profile requires --reuse-from NAME.')
        if not SANDBOX_NAME.fullmatch(args.reuse_from):
            raise ValueError(f'Invalid source sandbox {args.reuse_from!r}. {SANDBOX_NAME_ERROR}')
        manager = operations.Service(Path(args.state_dir).expanduser().resolve())
        with operation_locks.sandbox(args.reuse_from, 'copy settings from'):
            source = sandbox.recover_profile(manager.config(args.reuse_from), manager.state / args.reuse_from)
        if not source.get('editable_profile'):
            raise ValueError(f'Sandbox {args.reuse_from} has no editable profile folder to share. Make its profile editable first.')
        selected_profile = value.get('profile') or source['profile']['path']
        if selected_profile != source['profile']['path']:
            raise ValueError('Sharing requires the source sandbox’s applied profile. Omit --profile or select its saved profile path.')
        value['profile'] = selected_profile
    draft = complete_draft(value, args)
    draft['engine'] = sandbox.choose_engine(engine or draft['engine'], quiet=True)
    if 'editor' not in value and not args.use_defaults:
        draft['editor'] = {**editor_settings.DEFAULTS, 'mode': editor_settings.default_for_agent(primary_workspace.agent(draft), draft['engine'])}
    draft['reuse_from'] = args.reuse_from
    draft['remove_skills'] = args.omit_skill
    if args.reuse_editable_profile:
        draft.update(source_editable_profile=source.get('editable_profile'), share_editable_profile=True,
                     reused_profile=source.get('profile'), _reuse_config_revision=sandbox.config_revision(source))
    return draft


def attach_secrets(draft, value):
    for public, internal in (('provider_keys','_provider_keys'),('provider_credentials','_provider_credential_choices'),
                             ('local_model_keys','_local_model_keys'),('communication_keys','_communication_keys')):
        if public in value:
            draft[internal] = {**draft.get(internal, {}), **value[public]}
    if value.get('hosted'):
        from . import hosted_models
        hosted = {}
        for provider, row in value['hosted'].items():
            merged = {**draft.get('_hosted_changes', {}).get(provider, {}), **row, 'provider': provider}
            hosted_models.validate(merged)
            hosted[provider] = merged
        draft['_hosted_changes'] = {**draft.get('_hosted_changes', {}), **hosted}
    return value.get('web_keys')


def start_creation(args, draft, secret_values=None):
    manager = operations.Service(Path(args.state_dir).expanduser().resolve())
    if not SANDBOX_NAME.fullmatch(args.name):
        raise ValueError(SANDBOX_NAME_ERROR)
    values = secrets(args) if secret_values is None else secret_values
    if primary_workspace.agent(draft) != 'pi' and any(values.values()):
        raise ValueError('Pi model, Web Search and Slack credentials apply only to Pi sandboxes. Use primary-login for this coding agent.')
    keys = attach_secrets(draft, values)
    validate_draft(draft, manager.state)
    if getattr(args, 'use_defaults', False) and primary_workspace.agent(draft) == 'pi':
        defaults_keys = web_access.saved_keys(operations.search_keys_path(), draft['options']['web_access'])
        keys = {**defaults_keys, **(keys or {})}
    if primary_workspace.agent(draft) == 'pi':
        for provider, row in draft['options']['web_access']['providers'].items():
            row['has_key'] = bool((keys or {}).get(provider))
    conflicts = manager.check_creation(draft)
    if conflicts.containers or conflicts.volumes or conflicts.networks:
        print(conflicts.describe(), file=sys.stderr)
        approved = args.replace_conflicts
        if not approved and sys.stdin.isatty():
            print('Delete these exact conflicting resources? This permanently deletes their private data. [y/N] ', end='', file=sys.stderr, flush=True)
            approved = sys.stdin.readline().strip().lower() in ('y','yes')
        if not approved:
            raise ValueError('Creation conflicts remain. Choose another name or use --replace-conflicts to approve the exact listed resources.')
        manager.check_creation(draft, conflicts).require_available()
    controller = creation.Creation()
    foreground = Foreground(controller, structured=args.json)
    controller.progress = foreground.progress
    try:
        with foreground.signals():
            manager.create(draft, keys, foreground.log, cancellation=controller)
    except creation.Cancelled as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(130) from None
    if args.open:
        sandbox.open_selected(manager.config(args.name), manager.state / args.name)
    if args.json:
        emit(args, {'name': args.name, 'status': 'ready'})


def copy_sandbox(args):
    manager = operations.Service(Path(args.state_dir).expanduser().resolve())
    if not SANDBOX_NAME.fullmatch(args.source):
        raise ValueError(SANDBOX_NAME_ERROR)
    with operation_locks.sandbox(args.source, 'copy settings from'):
        state = manager.state / args.source
        config = sandbox.recover_profile(manager.config(args.source), state)
        draft = operations.copy_draft({'name': args.source, 'config': config}, manager.state)
        if args.primary_agent:
            draft = operations.copy_for_agent(draft, args.primary_agent)
        if args.profile:
            draft['profile'] = args.profile
        if isinstance(draft.get('model_providers'), list):
            draft['model_providers'] = {row['name']: row.get('settings', {}) for row in draft['model_providers']}
        if args.share_editable_profile and not draft.get('source_editable_profile'):
            raise ValueError(f'Sandbox {args.source} has no editable profile folder to share. Copy without --share-editable-profile or make its profile editable first.')
        if args.share_editable_profile and not operations.shares_editable_profile({
                **draft, 'share_editable_profile': True}):
            raise ValueError('Sharing requires the source sandbox’s applied profile. Omit --profile or use its saved profile path.')
        draft.update(name=args.name, workspace=args.workspace or config['workspace'], open_editor=args.open,
                     share_editable_profile=args.share_editable_profile)
        draft['editor'] = editor_settings.copy_choice(draft['editor'])
        if args.include_web_search_keys and primary_workspace.agent(draft) != 'pi':
            raise ValueError('--include-web-search-keys applies only to Pi copies.')
        source_keys = manager.search_keys(args.source) if args.include_web_search_keys else None
    if source_keys:
        # Same endpoint matching as the TUI; no workstation default keys are inherited.
        settings = options(config.get('options'))['web_access']
        source_keys = web_access.matching_keys(source_keys, settings) if primary_workspace.agent(draft) == 'pi' else {}
        for provider, row in draft['options']['web_access']['providers'].items():
            row['has_key'] = bool(source_keys.get(provider))
    else:
        source_keys = None
    # Preserve the same immutable source snapshot through creation, rechecked by create --reuse-from.
    original_secrets = secrets(args)
    if source_keys:
        original_secrets['web_keys'] = {**source_keys, **original_secrets.get('web_keys', {})}
    # Read stdin once. start_creation accepts the parsed document through this local binding.
    start_creation(args, draft, original_secrets)


def supervise(args):
    if not SANDBOX_NAME.fullmatch(args.name):
        raise ValueError(SANDBOX_NAME_ERROR)
    manager = operations.Service(Path(args.state_dir).expanduser().resolve())
    command = [args.command, args.name]
    for field, flag in (('settings_file','--settings-file'),('profile','--profile'),('directory','--directory'),
                        ('revision','--revision'),('saved','--saved'),('volume','--volume'),
                        ('output','--output'),('service','--service'),('image_source','--image-source')):
        value = getattr(args, field, None)
        if value is not None:
            command += [flag, str(value)]
    for field in ('browser','subagents'):
        value = getattr(args, field, None)
        if value is not None:
            command += ['--' + ('' if value else 'no-') + field]
    if args.command == 'install-system-packages':
        command += args.packages
    controller = updating.Update(cooperative=args.command in ('profile-fork','profile-apply','service-data-export','service-data-restore'))
    foreground = Foreground(controller, structured=args.json)
    controller.progress = foreground.progress
    with tempfile.TemporaryDirectory(prefix='factory-cli-input-') as temporary:
        if args.command == 'update' and args.settings_file == '-':
            path = Path(temporary) / 'settings.json'
            from .sandbox_options import write_json
            write_json(path, read_json('-'))
            command[command.index('--settings-file') + 1] = str(path)
        try:
            with operation_locks.sandbox(args.name, args.command), foreground.signals():
                manager.run_update(command, foreground.log, controller)
        except updating.UpdateCancelled as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(130) from None
    if args.json:
        emit(args, {'name': args.name, 'operation': args.command, 'status': 'complete'})


def register(sub):
    p = sub.add_parser('copy', help='Create a sandbox from an existing sandbox’s settings')
    p.add_argument('source'); p.add_argument('name'); p.add_argument('--workspace')
    p.add_argument('--primary-agent', choices=primary_workspace.AGENTS)
    p.add_argument('--profile', help='Choose another compatible profile; omitted uses the source’s applied profile')
    p.add_argument('--share-editable-profile', action='store_true'); p.add_argument('--include-web-search-keys', action='store_true')
    p.add_argument('--open', action='store_true'); p.add_argument('--secrets-stdin', action='store_true')
    p.add_argument('--replace-conflicts', action='store_true'); p.add_argument('--json', action='store_true')
    p.set_defaults(cli_handler=copy_sandbox)
