"""CLI adapters for editor, history, libraries and read-only inspection."""
from contextlib import redirect_stdout
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from . import (editor_settings, operations, primary_workspace, profile_services,
               prompt_library, registry, sandbox, session_transfer, skill_library, updates, web_access)
from .cli_support import emit, read_json, read_secret, remember_secret
from .sandbox_options import SANDBOX_NAME, SANDBOX_NAME_ERROR, options, write_json


def service(args):
    for field in ('name', 'to'):
        value = getattr(args, field, None)
        if value is not None and not SANDBOX_NAME.fullmatch(value):
            raise ValueError(f'Invalid sandbox {field} {value!r}. {SANDBOX_NAME_ERROR}')
    return operations.Service(Path(args.state_dir).expanduser().resolve())


def editor(args):
    manager = service(args)
    state = manager.state / args.name
    manager.config(args.name)
    if args.action == 'show':
        value = editor_settings.load(state)
        value['password_enabled'] = bool(value.pop('password_hash'))
    else:
        if args.settings_file == '-' and args.password_stdin:
            raise ValueError('Use a settings file when reading the password from stdin; stdin cannot contain both inputs.')
        value = read_json(args.settings_file) if args.settings_file else {}
        if not isinstance(value, dict):
            raise ValueError('Editor settings must be a JSON object.')
        if 'password_hash' in value:
            raise ValueError('Use --password-stdin to set an editor password, or --clear-password to remove it.')
        if args.password_stdin:
            value['password_hash'] = editor_settings.password_hash(read_secret(True, 'Editor password'))
            if not value['password_hash']:
                raise ValueError('Editor password is empty. Use --clear-password to remove it.')
        elif args.clear_password:
            value['password_hash'] = ''
        value = manager.save_editor(args.name, value, lambda line: print(line, file=sys.stderr))
        if value['editor_stopped']:
            value['next_step'] = f'Run ai-sandbox-factory open {args.name} to reopen the editor.'
    emit(args, value)


def prompts(args):
    if args.action == 'list':
        value = prompt_library.entries()
    elif args.action == 'save':
        value = prompt_library.prompt(read_json(args.file))
        prompt_library.save(**value, key=args.id)
        value = {key: row for key, row in prompt_library.entries().items() if row['name'] == value['name']}
    elif args.action == 'remove':
        prompt_library.remove(args.id)
        value = {'removed': args.id}
    else:
        manager = service(args)
        if args.action == 'show':
            manager.config(args.name)
            value = prompt_library.selection(manager.state / args.name)
        else:
            value = None
            if args.id:
                value = prompt_library.entries().get(args.id)
                if value is None:
                    raise ValueError(f'Prompt add-on {args.id!r} is not installed. Run prompt list.')
            elif args.file:
                value = prompt_library.prompt(read_json(args.file))
            value = {'changed': manager.assign_prompt(args.name, value)}
    emit(args, value)


def skills(args):
    if args.action == 'list':
        emit(args, skill_library.entries())
        return
    if args.action == 'remove':
        skill_library.remove(args.id)
        emit(args, {'removed': args.id})
        return
    with tempfile.TemporaryDirectory(prefix='factory-cli-skills-') as temporary:
        with redirect_stdout(sys.stderr):
            rows = skill_library.discover(args.source, Path(temporary) / 'source', args.ref)
        if args.action == 'install':
            selected = [row for row in rows if row['folder'] in args.folder] if args.folder else rows if len(rows) == 1 else []
            missing = set(args.folder) - {row['folder'] for row in rows}
            if missing or not selected:
                choices = ', '.join(row['folder'] for row in rows)
                raise ValueError(f'Choose --folder from the discovered skills: {choices}. Missing: {", ".join(sorted(missing)) or "selection"}.')
            skill_library.install(selected)
            rows = selected
        emit(args, [{key: value for key, value in row.items() if key != 'path'} for row in rows])


def defaults(args):
    value = operations.load_defaults()
    if args.action == 'set':
        if args.settings_file == '-' and args.secrets_stdin:
            raise ValueError('Use a settings file when reading keys from stdin; stdin cannot contain both inputs.')
        changes = read_json(args.settings_file)
        if not isinstance(changes, dict):
            raise ValueError('Defaults must be an object using the fields returned by defaults show.')
        unknown = set(changes) - operations.DEFAULT_SETTINGS.keys()
        if unknown:
            raise ValueError('Unknown creation defaults: ' + ', '.join(sorted(unknown)) + '. Use the fields returned by defaults show.')
        value = operations.validate_defaults({**value, **changes})
        if args.secrets_stdin:
            secret = read_json('-')
            if (not isinstance(secret, dict) or set(secret) != {'web_keys'}
                    or not isinstance(secret['web_keys'], dict)
                    or any(not isinstance(key, str) for key in secret['web_keys'].values())):
                raise ValueError('Default search keys must be JSON {"web_keys": {"PROVIDER": "KEY"}}.')
            keys = secret['web_keys']
            for key in keys.values():
                remember_secret(key)
            unknown = set(keys) - value['options']['web_access']['providers'].keys()
            if unknown:
                raise ValueError('Search keys have no configured provider: ' + ', '.join(sorted(unknown)))
            existing = web_access.saved_keys(operations.search_keys_path(), value['options']['web_access'])
            value = operations.save_defaults_with_search(value, value['options']['web_access'], {**existing, **keys})
        else:
            keys = web_access.saved_keys(operations.search_keys_path(), value['options']['web_access'])
            for provider, row in value['options']['web_access']['providers'].items():
                row['has_key'] = bool(keys.get(provider))
            write_json(operations.defaults_path(), value)
    emit(args, value)


def chats(args):
    manager = service(args)
    if args.action == 'export':
        with redirect_stdout(sys.stderr):
            path, count = manager.export_chats(args.name, args.output)
        emit(args, {'path': str(path), 'chats': count})
        return
    if args.action == 'import':
        with redirect_stdout(sys.stderr):
            added, skipped = manager.import_chats(args.name, args.file)
        emit(args, {'added': added, 'skipped': skipped})
        return
    if args.action == 'copy' and not (args.preview or args.yes or sys.stdin.isatty()):
        raise ValueError('Chat copy needs confirmation. Use --preview to inspect conversion or --yes to approve copying.')
    if args.action == 'copy' and args.to == args.name:
        raise ValueError('Choose a different destination sandbox for chat copy.')
    with redirect_stdout(sys.stderr):
        transfer = session_transfer.Transfer(manager.chat_config(args.name), manager.state / args.name)
    try:
        if args.action == 'list':
            value = transfer.rows
        else:
            target = manager.chat_config(args.to)
            with redirect_stdout(sys.stderr):
                preview = transfer.preview(args.chat, target, manager.state / args.to)
            value = {'preview': preview, 'copied': False}
            if not args.preview:
                if not args.yes:
                    print(json.dumps(preview, indent=2, ensure_ascii=False), file=sys.stderr)
                    print(f'Copy this conversation from {args.name} into a new chat in {args.to}? [y/N] ', end='', file=sys.stderr, flush=True)
                    if sys.stdin.readline().strip().lower() not in ('y', 'yes'):
                        emit(args, value)
                        return
                with manager.changing(args.to, 'copy a chat into'), redirect_stdout(sys.stderr):
                    value = {'preview': preview, 'copied': True, 'result': transfer.apply()}
        emit(args, value)
    finally:
        transfer.close()


def public_settings(config):
    fields = ('network', 'memory', 'subagents', 'env_file', 'working_directory', 'extra_folders',
              'ssh', 'advisors', 'local_model_ports', 'aws_access', 'connector_ids', 'connector_operations')
    value = {key: copy.deepcopy(config[key]) for key in fields if key in config}
    value['options'] = options(config.get('options'))
    value['model_providers'] = {row['name']: row.get('settings', {}) for row in config.get('model_providers', [])}
    return value


def inspect_sandbox(args):
    manager = service(args)
    record = {}
    if not args.guide and args.section != 'settings':
        record = next((row for row in manager.inventory() if row['name'] == args.name), {})
        alerts = [row._asdict() for row in operations.sandbox_alerts(record)] if record else []
        if args.section in ('containers', 'alerts'):
            if not record:
                manager.config(args.name)  # Retain the missing-sandbox diagnostic.
            emit(args, alerts if args.section == 'alerts' else record.get('containers'))
            return
        if record.get('creation'):
            emit(args, {'name': args.name, 'creation': record['creation'],
                        'containers': record.get('containers'), 'alerts': alerts})
            return
    config = manager.config(args.name)
    state = manager.state / args.name
    settings = public_settings(config)
    settings['allowlist'] = (state / 'allowlist.txt').read_text() if (state / 'allowlist.txt').exists() else ''
    if args.guide:
        profile = sandbox.selected_profile(config)
        emit(args, registry.profile_guide(profile['path']))
        return
    if args.section == 'settings':
        emit(args, settings)
        return
    profile = config.get('profile', {})
    value = {'name': args.name, 'agent': primary_workspace.agent(config), 'settings': settings,
             'containers': record.get('containers'),
             'alerts': [row._asdict() for row in operations.sandbox_alerts(record)],
             'profile': {'name': profile.get('name'), 'description': profile.get('description'),
                         'revision': profile.get('digest'), 'editable': config.get('editable_profile'),
                         'services': profile_services.status(config)}}
    emit(args, value[args.section] if args.section != 'all' else value)


def logs(args):
    manager = service(args)
    config = manager.config(args.name)
    name = args.container or sandbox.names(config)[0]
    if sandbox.inspect(config, name) is None:
        raise ValueError(f'Container {name!r} does not exist for {args.name}. Run list --json to inspect its containers.')
    command = [config['engine'], 'logs']
    if args.tail is not None:
        command += ['--tail', str(args.tail)]
    if args.follow:
        command += ['--follow']
    result = subprocess.run([*command, name])
    if result.returncode:
        raise ValueError(f'{config["engine"]} logs {name} exited with status {result.returncode}.')


def self_update(args):
    if args.check:
        emit(args, {'installed': updates.installed_version(), 'available': updates.available_update()})
    else:
        if not updates.pipx_install():
            raise ValueError('This installation is not managed by pipx. Update it using its original installation method.')
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError('Factory upgrade needs confirmation. Run self-update --check, then self-update --yes to install it.')
            print('Upgrade this Factory installation? [y/N] ', file=sys.stderr, end='', flush=True)
            if sys.stdin.readline().strip().lower() not in ('y', 'yes'):
                emit(args, 'Factory upgrade cancelled.')
                return
        updates.upgrade(lambda line: print(line, file=sys.stderr))
        emit(args, 'Factory updated. Restart any open Factory TUI to use it.')


def register(sub):
    group = sub.add_parser('editor', help='Read or change saved editor settings').add_subparsers(dest='action', required=True)
    for action in ('show', 'set'):
        p = group.add_parser(action); p.add_argument('name'); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=editor)
        if action == 'set':
            p.add_argument('--settings-file', help='Partial JSON editor settings; use - for stdin')
            passwords = p.add_mutually_exclusive_group()
            passwords.add_argument('--password-stdin', action='store_true')
            passwords.add_argument('--clear-password', action='store_true')
    group = sub.add_parser('prompt', help='Manage system prompt add-ons').add_subparsers(dest='action', required=True)
    for action in ('list', 'save', 'remove', 'show', 'assign'):
        p = group.add_parser(action); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=prompts)
        if action in ('show', 'assign'): p.add_argument('name')
        if action == 'remove': p.add_argument('id')
        if action == 'save': p.add_argument('--file', required=True); p.add_argument('--id')
        if action == 'assign':
            choice = p.add_mutually_exclusive_group(required=True)
            choice.add_argument('--id'); choice.add_argument('--file'); choice.add_argument('--clear', action='store_true')
    group = sub.add_parser('skill', help='Discover and manage reusable skills').add_subparsers(dest='action', required=True)
    for action in ('list', 'discover', 'install', 'remove'):
        p = group.add_parser(action); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=skills)
        if action == 'remove': p.add_argument('id')
        if action in ('discover', 'install'): p.add_argument('source'); p.add_argument('--ref')
        if action == 'install': p.add_argument('--folder', action='append', default=[])
    group = sub.add_parser('defaults', help='Read or change workstation creation defaults').add_subparsers(dest='action', required=True)
    for action in ('show', 'set'):
        p = group.add_parser(action); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=defaults)
        if action == 'set':
            p.add_argument('--settings-file', required=True)
            p.add_argument('--secrets-stdin', action='store_true', help='Save Web Search keys separately as JSON: {"web_keys": {"PROVIDER": "KEY"}}')
    group = sub.add_parser('chat', help='Export, restore or copy conversations').add_subparsers(dest='action', required=True)
    for action in ('list', 'export', 'import', 'copy'):
        p = group.add_parser(action); p.add_argument('name'); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=chats)
        if action == 'export': p.add_argument('--output', required=True)
        if action == 'import': p.add_argument('--file', required=True)
        if action == 'copy':
            p.add_argument('--to', required=True); p.add_argument('--chat', required=True)
            choice = p.add_mutually_exclusive_group(); choice.add_argument('--preview', action='store_true'); choice.add_argument('--yes', action='store_true')
    p = sub.add_parser('show', help='Inspect settings, profiles, containers and alerts without starting the sandbox')
    p.add_argument('name'); p.add_argument('--section', choices=('all','settings','profile','containers','alerts'), default='all')
    p.add_argument('--guide', action='store_true'); p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=inspect_sandbox)
    p = sub.add_parser('logs', help='Read a sandbox container’s logs')
    p.add_argument('name'); p.add_argument('--container'); p.add_argument('--tail', type=int); p.add_argument('--follow', action='store_true'); p.set_defaults(cli_handler=logs)
    p = sub.add_parser('self-update', help='Check for or install a Factory update')
    choice = p.add_mutually_exclusive_group()
    choice.add_argument('--check', action='store_true'); choice.add_argument('--yes', action='store_true', help='Confirm installing the upgrade')
    p.add_argument('--json', action='store_true'); p.set_defaults(cli_handler=self_update)
