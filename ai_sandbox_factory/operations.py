"""Factory operations shared by terminal and command-line interfaces."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import NamedTuple
import re
import subprocess
import sys
import tempfile

from . import (activity, chat_history, editor_settings, host_platform, hosted_models,
               inference, model_providers, operation_locks, primary_workspace, profile_edit,
               provider_credentials, prompt_library, registry, runtime_context, sandbox, web_access)
from .primary_workspace import DEFAULT_AUTH, agent as coding_agent
from .sandbox_options import DEFAULTS, memory_limit, options, write_json
from .settings_catalog import ADVISORS_PATH


def profile_titles():
    installed = registry.installed('profile')
    return {name: installed_profile_title(name) if name in installed else title
            for name, title in registry.choices('profile').items()}


def installed_profile_title(name):
    return name.replace('-', ' ').title()


DEFAULT_SETTINGS = {'profile': 'base', 'model_providers': {}, 'workspace_parent': '', 'network': 'allowlist',
                    'engine': 'podman', 'memory': '8g', 'subagents': False, 'env_file': '',
                    'pi_config': '', 'skill_repo': '', 'skill_path': '', 'library_skills': [], 'open_editor': True,
                    'local_model_ports': None, 'options': DEFAULTS, 'editor': {**editor_settings.DEFAULTS, 'mode': editor_settings.default_mode()}}


def safe_text(value):
    return ''.join(c for c in str(value) if c.isprintable() or c in '\n\t')


def defaults_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'ai-sandbox-factory/cockpit.json'


def search_keys_path():
    return defaults_path().with_name('web-access-keys.json')


def save_defaults_with_search(defaults, value, keys):
    path = search_keys_path()
    previous = json.loads(path.read_text()) if path.exists() and not path.is_symlink() else None
    defaults['options'] = options(defaults.get('options'))
    defaults['options']['web_access'] = web_access.save_keys(path, value, keys)
    try:
        write_json(defaults_path(), defaults)
    except OSError:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            write_json(path, previous)
        raise
    return defaults


def save_web_defaults(value, keys):
    return save_defaults_with_search(load_defaults(), value, keys)


def load_defaults():
    path = defaults_path()
    if not path.exists():
        return {**DEFAULT_SETTINGS, 'options': options()}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) - DEFAULT_SETTINGS.keys():
        raise ValueError('Invalid cockpit defaults file')
    return validate_defaults({**DEFAULT_SETTINGS, **value})


def validate_defaults(value):
    """Validate reusable choices without starting or changing a sandbox."""
    result = copy.deepcopy(value)
    result['options'] = options(result['options'])
    result['editor'] = editor_settings.copy_choice(result['editor'])
    memory_limit(result['memory'])
    if result['network'] not in ('allowlist', 'unrestricted'):
        raise ValueError(f'Unknown network mode {result["network"]!r}. Choose allowlist or unrestricted.')
    if result['engine'] not in ('auto', 'podman', 'docker'):
        raise ValueError(f'Unknown container engine {result["engine"]!r}. Choose auto, podman or docker.')
    for field in ('subagents', 'open_editor'):
        if type(result[field]) is not bool:
            raise ValueError(f'{field} must be true or false.')
    for field in ('profile', 'workspace_parent', 'env_file', 'pi_config', 'skill_repo', 'skill_path'):
        if not isinstance(result[field], str):
            raise ValueError(f'{field} must be a string.')
    if not isinstance(result['model_providers'], dict) or any(not isinstance(row, dict) for row in result['model_providers'].values()):
        raise ValueError('model_providers must map provider names to settings objects.')
    if not isinstance(result['library_skills'], list) or any(not isinstance(row, str) for row in result['library_skills']):
        raise ValueError('library_skills must be a list of installed skill identifiers.')
    if result['local_model_ports']:
        sandbox.local_model_ports(result['local_model_ports'])
    return result


def preset(profile, defaults, profile_defaults=None):
    value = {**defaults, 'profile': profile, 'options': options(defaults.get('options'))}
    value['options'] = dict(value['options'])
    value['options']['disabled_skills'] = list(value['options']['disabled_skills'])
    profile_defaults = profile_defaults or {}
    if 'web_search' in profile_defaults:
        value['options']['web_access']['enabled'] = profile_defaults['web_search']
    if 'subagents' in profile_defaults:
        value['subagents'] = profile_defaults['subagents']
    rules = [f'local-model.internal:{p}' for p in sandbox.local_model_ports(value['local_model_ports'])] if value.get('local_model_ports') else []
    value['allowlist'] = '\n'.join(rules)
    return value


def copy_draft(record, state_root):
    """Copy declarative settings, never private state or credential file paths."""
    config = copy.deepcopy(record['config'])
    value = copy.deepcopy(DEFAULT_SETTINGS)
    for field in ('engine', 'network', 'memory', 'subagents', 'local_model_ports',
                  'model_providers', 'advisors', 'extra_skills', 'extra_folders'):
        if field in config:
            value[field] = copy.deepcopy(config[field])
    value.update(primary_agent=coding_agent(config), primary_auth=DEFAULT_AUTH,
                 profile=config['profile']['path'], options=options(config.get('options')),
                 name='', workspace=config['workspace'], workspace_parent=config['workspace'],
                 env_file='', pi_config='', working_directory='.',
                 reuse_from=record['name'], reused_profile=copy.deepcopy(config['profile']))
    value['_reuse_config_revision'] = sandbox.config_revision(config)
    value['share_editable_profile'] = False
    value['source_editable_profile'] = copy.deepcopy(config.get(profile_edit.EDIT))
    value['options']['approval_model'] = 'current'  # Copies start without provider credentials.
    value['editor'] = editor_settings.copy_choice(editor_settings.load(Path(state_root) / record['name']))
    value['allowlist'] = (Path(state_root) / record['name'] / 'allowlist.txt').read_text()
    value['system_prompt'] = prompt_library.selection(Path(state_root) / record['name'])
    for row in value['options']['web_access']['providers'].values():
        row['has_key'] = False
    return value


def shares_editable_profile(draft):
    return bool(draft.get('share_editable_profile') and draft.get('source_editable_profile')
                and draft.get('reused_profile', {}).get('path') == draft.get('profile'))


def has_web_search_keys(config):
    return any(row['has_key'] for row in options(config.get('options'))['web_access']['providers'].values())


def copy_for_agent(value, agent):
    draft = copy.deepcopy(value)
    source = coding_agent(value)
    if agent == source:
        return draft
    selected = options(value.get('options'))
    shared = {key: selected[key] for key in ('browser', 'git_identity', 'website_groups')}
    if source != 'pi' and agent != 'pi':
        shared['native_approval'] = selected['native_approval']
    draft.update(primary_agent=agent, model_providers={}, local_model_ports=None, subagents=False, advisors={},
                 options=options(shared), _copy_source_agent=source)
    draft['allowlist'] = '\n'.join(
        f'{host}:{port}' for host, port in sandbox.parse_rules(draft['allowlist'])
        if host != 'local-model.internal')
    return draft


class Service:
    """Subprocess argument lists only; keys are sent to the container over stdin."""
    def __init__(self, state):
        self.state = Path(state)

    def command(self, *args):
        return host_platform.python_command('ai_sandbox_factory', '--state-dir', str(self.state), *args)

    def inventory(self):
        result = subprocess.run(self.command('list', '--json'), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def capture(self, command, name, *args, payload=None):
        return subprocess.run(self.command(command, name, *args), input=payload, capture_output=True, text=True,
                              **operation_locks.factory_child(name))

    def run(self, args, log):
        visible = list(args)
        for index, value in enumerate(visible):
            if value == '--creation-token' and index + 1 < len(visible):
                visible[index + 1] = '[redacted]'
            elif str(value).startswith('--creation-token='):
                visible[index] = '--creation-token=[redacted]'
        log('› ' + ' '.join(str(x) for x in visible))
        last_output = ''
        inherited = operation_locks.factory_child(args[1]) if len(args) > 1 else {}
        with subprocess.Popen(self.command(*args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors='replace', **inherited) as process:
            for line in process.stdout:
                line = safe_text(line.rstrip())
                if line:
                    last_output = line
                log(line)
            code = process.wait()
            if code:
                raise ValueError(f'{args[0]} command exited with status {code}: '
                                 + (last_output or 'No diagnostic output was returned; inspect the command log.'))

    def run_update(self, args, log, update):
        if update is None:
            self.run(args, log)
        else:
            log('› ' + ' '.join(args))
            update.run(self.command(*args), log, **operation_locks.factory_child(args[1]))

    def start(self, name, log):
        with operation_locks.sandbox(name, 'start'):
            config = self.config(name)
            dev, proxy = sandbox.names(config)[:2]
            required = [dev, proxy] if sandbox.network_mode(config) == 'allowlist' else [dev]
            # Existing containers can resume even after their original image tags are removed.
            if any(sandbox.inspect(config, container) is None for container in required):
                images = sandbox.image_names(config)
                needed = images if sandbox.network_mode(config) == 'allowlist' else images[:1]
                if any(subprocess.run([config['engine'], 'image', 'inspect', image],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode for image in needed):
                    self.run(['build', name], log)
            self.run(['up', name], log)

    def config(self, name):
        path = self.state / name / 'config.json'
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as error:
            raise ValueError(f'No saved configuration for sandbox {name!r} at {path}. '
                             'Run list to choose a saved sandbox or check its state directory.') from error
        except (OSError, ValueError) as error:
            raise ValueError(f'Cannot read saved sandbox {name!r} at {path}: {error}. '
                             'Run list to choose a saved sandbox or check its state directory.') from error

    def pi_config(self, name):
        config = self.config(name)
        if coding_agent(config) != 'pi':
            raise ValueError('Factory model connections apply to Pi sandboxes. '
                             'Use primary-login or open for this sandbox’s coding agent.')
        return config

    @contextmanager
    def changing(self, name, action, *, pi=False):
        with operation_locks.sandbox(name, action), runtime_context.applied(self.state / name, require_save=True):
            config = self.config(name)
            if pi and coding_agent(config) != 'pi':
                raise ValueError('Factory model connections apply to Pi sandboxes. '
                                 'Use primary-login or open for this sandbox’s coding agent.')
            yield config

    def preview_settings(self, name, changes):
        from . import aws_access, connector_settings, folders, sandbox_advisors, sandbox_ssh
        config = self.config(name)
        native = coding_agent(config) != 'pi'
        special = {'approval_model', 'native_approval', 'codex_web_search', 'claude_web_search', 'agy_web_search',
                   'connector_ids', 'connector_operations'}
        if not isinstance(changes, dict):
            raise ValueError('Settings must be a JSON object.')
        unknown = set(changes) - (sandbox.EDITABLE_SETTINGS | special)
        if unknown:
            raise ValueError('Unknown editable sandbox settings: ' + ', '.join(sorted(unknown)))
        shortcuts = special - {'connector_ids', 'connector_operations'}
        requested = set(changes) & shortcuts
        if requested:
            if len(changes) != 1:
                raise ValueError('Change an agent approval or Web Search shortcut in a separate update.')
            key = next(iter(requested))
            agent = coding_agent(config)
            if key == 'approval_model' and agent != 'pi':
                raise ValueError('approval_model applies only to Pi sandboxes.')
            if key == 'native_approval' and agent == 'pi':
                raise ValueError('native_approval applies to Codex, Claude Code or Antigravity sandboxes.')
            if key in primary_workspace.WEB_SEARCH_OPTIONS.values() and primary_workspace.WEB_SEARCH_OPTIONS.get(agent) != key:
                raise ValueError(f'{key} does not apply to {agent}; use its own Web Search setting.')
            options({**options(config.get('options')), key: changes[key]})
        candidate = {**config, **changes}
        if 'options' in changes:
            candidate['options'] = options(changes['options'])
        if 'memory' in changes:
            memory_limit(changes['memory'])
        if 'subagents' in changes and type(changes['subagents']) is not bool:
            raise ValueError('subagents must be a boolean')
        if 'network' in changes and changes['network'] not in ('allowlist', 'unrestricted'):
            raise ValueError('Network must be allowlist or unrestricted.')
        if 'ssh' in changes:
            candidate['ssh'] = sandbox_ssh.validate(changes['ssh'])
        if 'advisors' in changes:
            sandbox_advisors.validate(changes['advisors'])
        if {'network', 'allowlist', 'local_model_ports'} & changes.keys():
            text = changes['allowlist'] if 'allowlist' in changes else (self.state / name / 'allowlist.txt').read_text()
            if not isinstance(text, str):
                raise ValueError('allowlist must contain one hostname:port rule per line as a string.')
            rules = sandbox.parse_rules(text)
            sandbox.configure_local_gateway(candidate, candidate.get('local_model_ports'), rules)
            if sandbox.network_mode(candidate) == 'unrestricted' and rules:
                raise ValueError('Unrestricted networking cannot include an active allowlist')
            if 'model_providers' not in changes:
                model_providers.validate_network(candidate, rules)
        if 'env_file' in changes:
            sandbox.validate_env(changes['env_file'])
        if 'working_directory' in changes:
            folders.working_directory(candidate['workspace'], candidate['working_directory'])
        if 'extra_folders' in changes:
            candidate['extra_folders'] = folders.validate_config(candidate, self.state)
        if native and ({'working_directory', 'extra_folders', 'env_file'} & changes.keys()):
            primary_workspace.validate_mounts(candidate, self.state)
        if 'aws_access' in changes:
            candidate['aws_access'] = aws_access.settings(changes['aws_access'])
        effect = 'replace'
        notes = ['Builds the replacement before stopping work.',
                 'If running, replaces the main agent sandbox and interrupts its active tasks.',
                 'Keeps project files, private home and saved chats.']
        if not changes:
            effect, notes = 'none', ['No settings were supplied.']
        elif set(changes) == {'aws_access'} and candidate['aws_access'] == config.get('aws_access', {}):
            effect, notes = 'none', ['No AWS access settings changed.']
        elif set(changes) == {'aws_access'}:
            effect = sandbox.aws_installation(config, self.state / name, candidate['aws_access'])[0] if candidate['aws_access'] else 'ready'
            notes = (['Updates AWS access without replacing the main agent sandbox.'] if effect not in ('restart','replace','prepare') else notes)
        elif {'connector_ids', 'connector_operations'} & changes.keys():
            if connector_settings.preview(config, self.state / name, changes):
                effect, notes = 'connectors', ['Updates connector services and grants. Required mounts are checked before applying.']
            else:
                effect, notes = 'none', ['No connector settings changed.']
        elif ('ssh' in changes and changes.keys() <= {'ssh', 'options'}
              and candidate['ssh'] == config.get('ssh', {'hosts': []})
              and options(candidate.get('options')) == options(config.get('options'))):
            effect, notes = 'none', ['No SSH or Git settings changed.']
        elif set(changes) <= {'working_directory', 'extra_folders'} and candidate.get('extra_folders', []) == config.get('extra_folders', []):
            effect, notes = 'next-launch', ['Saves the starting directory for the next client or terminal launch.']
        elif native and set(changes) == {'native_approval'}:
            effect, notes = 'restart', ['Saves the approval mode; restart this sandbox to use it.']
        elif native and set(changes) <= set(primary_workspace.WEB_SEARCH_OPTIONS.values()):
            effect, notes = 'next-launch', ['Saves Web Search; restart the selected agent to use it.']
        elif not native and set(changes) == {'approval_model'}:
            effect, notes = 'next-turn', ['Saves the approval model without replacing the main agent sandbox.']
        elif not native and set(changes) == {'options'}:
            before, after = options(config.get('options')), candidate['options']
            if {key for key in after if after[key] != before[key]} == {'approval_model'}:
                effect, notes = 'next-turn', ['Saves the approval model without replacing the main agent sandbox.']
        return {'name': name, 'changes': changes, 'runtime_effect': effect, 'notes': notes,
                'validation': 'Settings format checked. Builds, credentials and live prerequisites are checked when applying.'}

    def save_editor(self, name, changes, log=lambda _: None):
        from . import browser_editor
        with self.changing(name, 'save editor settings for'):
            state = self.state / name
            previous = editor_settings.load(state)
            value = editor_settings.validate({**previous, **changes})
            editor_settings.tls_context(value)
            if value == previous:
                return {'changed': False, 'editor_stopped': False}
            running = browser_editor.active(state)
            if running:
                log('Saving editor settings stops this browser editor and interrupts its active reply. Saved chats are kept.')
                browser_editor.stop(state)
            try:
                editor_settings.save(state, value)
            except (OSError, ValueError) as error:
                try:
                    kept = editor_settings.load(state) == previous
                except (OSError, ValueError):
                    kept = False
                effect = 'The browser editor stopped. ' if running else ''
                effect += ('The previous editor settings were kept. ' if kept else
                           'Editor settings may have changed; check them before reopening. ')
                raise ValueError(effect + str(error)) from error
            return {'changed': True, 'editor_stopped': bool(running)}

    def assign_prompt(self, name, value):
        # The prompt is saved separately from config.json and publishes its own context.
        with operation_locks.sandbox(name, 'save the system prompt add-on for'):
            self.config(name)
            changed = prompt_library.assign(self.state / name, value)
            if changed:
                activity.record(self.state / name, 'edited')
            return changed

    def review_profile(self, name):
        with tempfile.TemporaryDirectory(prefix='factory-profile-review-') as temporary:
            path = Path(temporary) / 'review.json'
            result = self.capture('profile-review', name, '--output', str(path))
            if result.returncode:
                raise ValueError(f'Cannot review the profile for {name} (exit {result.returncode}): '
                                 + (result.stderr.strip() or result.stdout.strip() or 'The review worker returned no diagnostic output.'))
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError) as error:
                raise ValueError(f'Cannot read the profile review for {name}: {error}. '
                                 f'Worker output: {result.stdout}. Retry Review changes.') from error

    def export_chats(self, name, path):
        with operation_locks.sandbox(name, "export chats from"):
            return chat_history.export_chats(self.chat_config(name), self.state / name, path)

    def import_chats(self, name, path):
        with operation_locks.sandbox(name, "import chats into"):
            return chat_history.import_chats(self.chat_config(name), path, state=self.state / name)

    def chat_config(self, name):
        config = self.config(name)
        if config.get('name') != name:
            raise ValueError('Factory could not verify this sandbox’s saved configuration.')
        return config

    def communication_status(self, name):
        result = subprocess.run(self.command('communication-key', name, '--status'), text=True, capture_output=True)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def search_status(self, name):
        result = subprocess.run(self.command('web-key', name, '--status'), text=True, capture_output=True)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def search_keys(self, name):
        result = subprocess.run(self.command('web-key', name, '--export-keys'), text=True, capture_output=True)
        if result.returncode:
            # stdout is the credential export; never include it in an error.
            cause = model_providers.diagnostic(result.stderr) or 'The key export command returned no diagnostic output.'
            raise ValueError(f'Cannot copy Web Search API keys from {name} (exit {result.returncode}): {cause}. '
                             'Try copying again, or leave Include Web Search API keys unchecked.')
        return json.loads(result.stdout)

    def check_creation(self, draft, approved=None):
        from .creation import check_draft
        return check_draft(self.state, draft, approved)

    def save_configuration(self, draft, log, *, creation_token=None, progress=None):
        from .job_progress import Progress
        pending = draft.get('_hosted_changes', {})
        progress = progress or Progress(['Save sandbox configuration', 'Save connection and editor settings'])
        name = draft['name']
        state = self.state / name
        if state.exists() and not creation_token:
            raise ValueError('That sandbox name is already saved; choose another name.')
        with tempfile.TemporaryDirectory(prefix='pi-sandbox-cockpit-') as temp:
            selected = Path(temp) / 'options.json'
            write_json(selected, draft['options'])
            args = ['create', name, '--profile', draft['profile'], '--workspace', draft['workspace'],
                    '--engine', draft['engine'], '--options-file', str(selected)]
            if draft.get('_curlie_list'):
                args += ['--curlie-list', draft['_curlie_list']]
            if coding_agent(draft) != 'pi':
                args += ['--primary-agent', draft['primary_agent'], '--primary-auth', draft['primary_auth']]
            if draft.get('ssh', {}).get('hosts'):
                ssh_file = Path(temp) / 'ssh.json'
                write_json(ssh_file, draft['ssh'])
                args += ['--ssh-file', str(ssh_file)]
            if draft.get('connector_ids'):
                reviewed_choices = '_connector_routes' in draft or '_connector_instructions' in draft
                for identifier in draft['connector_ids']:
                    grants = draft.get('connector_operations', {}).get(identifier)
                    choice = identifier if reviewed_choices or grants is None else identifier + ':' + ','.join(grants)
                    args += ['--connector', choice]
                if reviewed_choices:
                    reviewed = Path(temp) / 'connector-review.json'
                    write_json(reviewed, {'routes': draft.get('_connector_routes', {}),
                                          'agent_instructions': draft.get('_connector_instructions', {}),
                                          'operations': draft.get('connector_operations', {})})
                    args += ['--connector-review-file', str(reviewed)]
            if draft.get('aws_access'):
                grant_file = Path(temp) / 'aws-access.json'
                write_json(grant_file, draft['aws_access'])
                args += ['--aws-access-file', str(grant_file)]
            if 'system_prompt' in draft:
                prompt_file = Path(temp) / 'system-prompt.json'
                write_json(prompt_file, draft['system_prompt'])
                args += ['--system-prompt-file', str(prompt_file)]
            if creation_token:
                args += ['--creation-token', creation_token]
            folder_file = Path(temp) / 'folders.json'
            write_json(folder_file, draft.get('extra_folders', []))
            args += ['--folders-file', str(folder_file), '--working-directory', draft.get('working_directory', '.')]
            if draft.get('reuse_from'):
                args += ['--reuse-from', draft['reuse_from']]
                if draft.get('_reuse_config_revision'):
                    args += ['--reuse-config-revision', draft['_reuse_config_revision']]
                if shares_editable_profile(draft):
                    args += ['--reuse-editable-profile']
                for skill in draft.get('remove_skills', []):
                    args += ['--omit-skill', skill]
            if draft['network'] == 'allowlist':
                rules = Path(temp) / 'allowlist.txt'
                rules.write_text(draft['allowlist'])
                args += ['--allowlist', str(rules)]
            else:
                args += ['--network', 'unrestricted']
            for field, flag in [('memory', '--memory'), ('env_file', '--env-file'), ('pi_config', '--pi-config')]:
                if draft.get(field):
                    args += [flag, draft[field]]
            for skill in draft.get('library_skills', []):
                args += ['--skill', skill]
            if draft.get('skill_repo'):
                args += ['--skill-source', draft['skill_repo'], '--skill-path', draft['skill_path']]
            if draft.get('local_model_ports'):
                args += ['--local-model-ports', draft['local_model_ports']]
            providers=Path(temp)/'providers.json'
            write_json(providers,draft.get('model_providers',{'local':{}}))
            args += ['--providers-file',str(providers)]
            args += ['--subagents' if draft['subagents'] else '--no-subagents']
            for provider in draft.get('advisors', {}).get('providers', []):
                args += ['--advisor', provider]
            with progress.stage('Save sandbox configuration'):
                self.run(args, log)
        with progress.stage('Save connection and editor settings'):
            if pending:
                config = self.config(name)
                config['hosted_connections'] = {p: hosted_models.connection(v) for p,v in pending.items()}
                activity.save_config(state, config)
                (state / 'squid.conf').write_text(sandbox.network_policy(config, sandbox.parse_rules(draft['allowlist'])))
            editor_settings.save(state, draft.get('editor', editor_settings.DEFAULTS))
        return state

    def create(self, draft, key, log, cancellation=None, *, creation_token=None):
        if cancellation is not None:
            return cancellation.run(self, draft, key, log)
        from .job_progress import Progress
        pending = draft.get('_hosted_changes', {})
        imports = draft.get('_advisor_imports', [])
        labels = ['Save sandbox configuration', 'Save connection and editor settings']
        if creation_token:
            labels.append('Check sandbox resource ownership')
        labels += ['Build sandbox images', 'Start sandbox containers', 'Configure sandbox services']
        labels += ['Save ' + hosted_models.PROVIDERS[value['provider']] + ' connection' for value in pending.values()]
        labels += ['Import ' + provider + ' advisor sign-in' for provider in imports]
        progress = Progress(labels)
        name = draft['name']
        state = self.save_configuration(draft, log, creation_token=creation_token, progress=progress)
        if creation_token:
            from .creation import prepare_resources
            with progress.stage('Check sandbox resource ownership'):
                prepare_resources(state, creation_token)
        with progress.stage('Build sandbox images'):
            self.run(['build', name], log)
        with progress.stage('Start sandbox containers'):
            self.run(['up', name], log)
        with progress.stage('Configure sandbox services'):
            self.setup(name, draft, key, log)
        for value in pending.values():
            with progress.stage('Save ' + hosted_models.PROVIDERS[value['provider']] + ' connection'):
                self.hosted_save(name, value)
                log(hosted_models.PROVIDERS[value['provider']] + ' connection saved.')
        for provider in imports:
            with progress.stage('Import ' + provider + ' advisor sign-in'):
                self.run(['advisor-import', name, provider], log)
        if draft.get('advisors', {}).get('providers'):
            log(f'Advisors added. Open {ADVISORS_PATH} to sign in or check the copied sign-ins.')

    def approval_models(self, name):
        config = self.pi_config(name)
        models = hosted_models.request(config, 'approval-catalogue')['models']
        routes = inference.routes(config)
        managed = {name for name, route in routes.items() if not route.get('local')}
        if managed:
            connected = inference.credentials(config, self.state / name, {'status': True})['configured']
            models = [m for m in models if m['provider'] not in managed or m['provider'] in connected]
        return sorted(models, key=lambda m: (m['provider'], m['id']))

    def save_approval_model(self, name, model, log):
        with self.changing(name, 'select an approval model in', pi=True):
            if model != 'current' and not any(m['provider'] + '/' + m['id'] == model for m in self.approval_models(name)):
                raise ValueError('This model is no longer available. Choose another model for approvals.')
            self.update_settings(name, {'approval_model': model}, log)

    def hosted_request(self, name, action, **values):
        config = self.pi_config(name)
        if action == 'status':
            from . import inference
            status = inference.credentials(config, self.state / name, {'status': True}) if inference.enabled(config) else {'configured': []}
            connected = status['configured']
            return {'providers': [{'provider': provider, 'configured': provider in connected, 'has_key': provider in connected,
                                    'assignment': status.get('assignments', {}).get(provider), **value}
                                  for provider, value in config.get('hosted_connections', {}).items()]}
        return hosted_models.request(config, action, **values)

    def hosted_save(self, name, value):
        with self.changing(name, 'save the model connection for', pi=True):
            return hosted_models.save(self.config(name), self.state / name, value)

    def hosted_remove(self, name, provider):
        with self.changing(name, 'remove the model connection for', pi=True):
            return hosted_models.remove(self.config(name), self.state / name, provider)

    def hosted_select(self, name, provider, model, **values):
        with self.changing(name, 'select a model in', pi=True):
            result = hosted_models.select(self.config(name), provider, model, **values)
            activity.record(self.state / name, 'edited')
            return result

    def hosted_test(self, name, provider, model, **values):
        with self.changing(name, 'test a model in', pi=True):
            return hosted_models.select(self.config(name), provider, model, test=True, **values)

    def local_models(self, name, *, refresh=False):
        with operation_locks.sandbox(name, 'read local models from'):
            config = self.pi_config(name)
            if not config.get('local_model_ports'):
                return {'models': []}
            if refresh:
                if 'model_providers' in config:
                    model_providers.configure(config, self.state / name, 'refresh', provider='local')
                else:
                    sandbox.initialize_models(config, self.state / name)
            return hosted_models.request(config, 'local-catalogue',
                                         localPorts=sandbox.local_model_ports(config['local_model_ports']))

    def local_select(self, name, provider, model):
        with self.changing(name, 'select a local model in', pi=True) as config:
            if not config.get('local_model_ports'):
                raise ValueError(f'No local model ports are configured for {name}. Enable a local server in Model Providers.')
            result = hosted_models.request(config, 'select', provider=provider, model=model,
                                          localPorts=sandbox.local_model_ports(config['local_model_ports']))
            activity.record(self.state / name, 'edited')
            return result

    def plugin_model(self, name, action, *, provider, refresh=False, model=None):
        with self.changing(name, 'configure a plugin model in', pi=True):
            config = self.config(name)
            manifest = next((row['manifest'] for row in config.get('model_providers', ())
                             if row['name'] == provider), {})
            if provider == 'local' or not provider_credentials.scope(provider, manifest.get('inference')):
                raise ValueError('Choose an enabled provider plugin with a managed key.')
            if action == 'catalogue':
                if refresh:
                    self.provider(name, provider, 'refresh', None, lambda line: print(line, file=sys.stderr))
                return hosted_models.request(config, 'catalogue', provider=provider, plugin=True)
            if action != 'select':
                raise ValueError('Unsupported model operation.')
            result = hosted_models.request(config, 'select', provider=provider, plugin=True, model=model)
            activity.record(self.state / name, 'edited')
            return result

    def save_communication_keys(self, name, changes, log):
        if changes:
            result = self.capture('communication-key', name, '--keys-stdin', payload=json.dumps(changes))
            if result.returncode:
                detail = model_providers.diagnostic(result.stderr or result.stdout)
                for key in changes.values():
                    detail = model_providers.diagnostic(detail, key)
                raise ValueError(f'Cannot save Slack credentials for {name} (exit {result.returncode}): '
                                 + (detail or 'The credential command returned no diagnostic output.'))
            log('Slack credentials saved in the sandbox.')

    def setup(self, name, draft, key, log):
        if coding_agent(draft) != 'pi':
            return
        if local_keys := draft.get('_local_model_keys'):
            config = self.config(name)
            for port, choice in local_keys.items():
                provider_credentials.assign(config, self.state / name, inference.local_route_id(port), **choice)
            log('Local server keys assigned.')
        for provider, credential in draft.get('_provider_keys', {}).items():
            self.provider(name, provider, 'refresh', credential, log)
        for provider, choice in draft.get('_provider_credential_choices', {}).items():
            self.assign_provider_key(name, provider, choice, log)
        self.save_communication_keys(name, draft.get('_communication_keys'), log)
        if key:
            result = self.capture('web-key', name, '--keys-stdin', payload=json.dumps(key))
            if result.returncode:
                detail = model_providers.diagnostic(result.stderr or result.stdout)
                for secret in key.values():
                    detail = model_providers.diagnostic(detail, secret)
                raise ValueError(f'Cannot save Web Search credentials for {name} (exit {result.returncode}): '
                                 + (detail or 'The credential command returned no diagnostic output.'))
            log('Search API keys saved in the sandbox.')
        if (draft.get('local_model_ports') or draft.get('model_providers')) and not draft['open_editor']:
            self.run(['configure', name], log)
    def provider(self, name, provider, action, key, log):
        args = ['configure', name, '--provider', provider]
        if action in ('status', 'preview'):
            args += ['--' + action]
        if key is not None:
            args += ['--key-stdin']
        result = self.capture(*args, payload=key or '')
        output = model_providers.diagnostic(result.stdout, key)
        if output:
            log(output)
        error = model_providers.diagnostic(result.stderr, key)
        if error:
            log(error)
        if result.returncode:
            # The full diagnostic is in the job output; keep the summary actionable.
            lines = (error or output).splitlines()
            cause = lines[-1] if lines else 'The configure command returned no diagnostic output.'
            raise ValueError(f'Could not {action} provider {provider} for {name} '
                             f'(exit {result.returncode}): {cause}')
        if action not in ('status','preview'):
            log('Reload an open Pi/FrostPi session before selecting newly discovered models.')

    def provider_key_status(self, name, *, local=False):
        config = self.pi_config(name)
        if local:
            dev = sandbox.names(config)[0]
            runtime = sandbox.inspect(config, dev)
            if not runtime or not runtime['State']['Running']:
                raise ValueError('Start the sandbox to manage its local server keys.')
            result = subprocess.run([config['engine'], 'exec', dev, 'node', '-e',
                "const fs=require('fs');const config=JSON.parse(fs.readFileSync(process.env.PI_CODING_AGENT_DIR+"
                "'/local-pi-models.json'));if(!config.gateway)process.exit(1);"],
                capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError('Use Actions → Update sources and recreate… to enable local server keys.')
        return inference.credentials(config, self.state / name, {'status': True})

    def assign_provider_key(self, name, provider, choice, log):
        with self.changing(name, 'assign a model key to', pi=True):
            config = self.config(name)
            state = self.state / name
            local = bool(inference.routes(config).get(provider, {}).get('local'))
            status = self.provider_key_status(name, local=local)
            assignment = provider_credentials.assign(config, state, provider, **choice)
            if provider not in status['configured'] and provider not in config.get('hosted_connections', {}):
                self.provider(name, 'local' if local else provider, 'refresh', None, log)
            log('API key assigned. Subsequent requests use it.')
            return assignment

    def disconnect_provider_key(self, name, provider):
        with self.changing(name, 'disconnect a model key from', pi=True):
            from . import inference
            from .sandbox_options import check_reviewer_provider
            config = self.config(name)
            check_reviewer_provider(config, provider)
            return inference.credentials(config, self.state / name, {'id': provider, 'remove': True})

    def update(self, name, draft, key, log, update=None):
        changes = {k: draft[k] for k in ('network', 'memory', 'subagents', 'env_file', 'options')}
        changes['local_model_ports'] = draft.get('local_model_ports')
        changes['model_providers'] = draft.get('model_providers', {'local':{}})
        changes.update(skill_source=draft.get('skill_repo', ''), skill_path=draft.get('skill_path', ''),
                       remove_skills=draft.get('remove_skills', []), library_skills=draft.get('library_skills', []))
        changes['allowlist'] = draft['allowlist'] if draft['network'] == 'allowlist' else ''
        if draft.get('_curlie_list'):
            changes['curlie_list'] = draft['_curlie_list']
        with operation_locks.sandbox(name, 'apply settings to'), \
                tempfile.TemporaryDirectory(prefix='pi-sandbox-settings-') as temp:
            path = Path(temp) / 'settings.json'
            write_json(path, changes)
            self.run_update(['update', name, '--settings-file', str(path)], log, update)
            self.setup(name, draft, key, log)

    def update_settings(self, name, settings, log, update=None):
        with tempfile.TemporaryDirectory(prefix='sandbox-settings-') as temp:
            path = Path(temp) / 'settings.json'
            write_json(path, settings)
            self.run_update(['update', name, '--settings-file', str(path)], log, update)

    def ssh_settings(self, name, settings, log, git_identity, update=None):
        with tempfile.TemporaryDirectory(prefix='sandbox-ssh-') as temp:
            path = Path(temp) / 'settings.json'
            selected = options(self.config(name).get('options'))
            selected['git_identity'] = git_identity
            write_json(path, {'ssh': settings, 'options': options(selected)})
            self.run_update(['update', name, '--settings-file', str(path)], log, update)

    def ssh_key(self, name, generate=False):
        result = subprocess.run(self.command('ssh-generate-key' if generate else 'ssh-public-key', name),
                                text=True, capture_output=True)
        if result.returncode:
            raise ValueError(safe_text(result.stderr.strip() or 'Could not read the SSH public key'))
        rows = result.stdout.strip().splitlines()
        if not rows or not re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?', rows[-1]):
            raise ValueError('The SSH companion did not return a public key')
        return rows[-1]

    def advisors(self, name, settings, log, update=None):
        with tempfile.TemporaryDirectory(prefix='sandbox-advisors-') as temp:
            path = Path(temp) / 'settings.json'
            write_json(path, {'advisors': settings})
            self.run_update(['update', name, '--settings-file', str(path)], log, update)

    def models(self, name, mode, log):
        if mode not in ('preview', 'refresh'):
            raise ValueError('Unknown discovery action')
        with operation_locks.sandbox(name, f'{mode} models for'):
            self.start(name, log)
            self.run(['configure', name] + (['--preview'] if mode == 'preview' else []), log)
        if mode == 'refresh':
            log('In an already open Pi/FrostPi session, refresh there too before selecting a model with /model.')



class SandboxAlert(NamedTuple):
    title: str
    description: str
    button: str
    action: str
    blocking: bool = False


def sandbox_alerts(record):
    if record.get('creation') == 'interrupted':
        return [SandboxAlert('Creation interrupted',
            'Delete the incomplete sandbox before trying again. The host workspace is kept.',
            'Delete sandbox…', 'delete', True)]
    if record.get('creation') == 'running':
        return [SandboxAlert('Creation in progress',
            'Another Factory process is still creating this sandbox. Refresh to check it again.',
            'Refresh sandbox list', 'refresh-creation', True)]
    alerts = []
    ssh_alert = {
        'locked': SandboxAlert('SSH key locked',
            'Git over SSH is unavailable until this key is unlocked.',
            'Unlock existing key…', 'ssh-key-alert', True),
        'missing': SandboxAlert('SSH key missing',
            'Add a key to use Git over SSH in this sandbox.',
            'Set up SSH key…', 'ssh-key-alert', True),
        'unavailable': SandboxAlert('SSH service unavailable',
            'Couldn’t reach the SSH service. Check this sandbox’s containers.',
            'View containers', 'containers', True),
    }.get(record.get('ssh_key_state'))
    if ssh_alert:
        alerts.append(ssh_alert)
    if coding_agent(record.get('config', {})) == 'pi' and record.get('chat_editor') == 'pendant':
        alerts.append(SandboxAlert('Editor update available',
            'This sandbox still uses Pendant. Switching to FrostPi restarts the sandbox and keeps your workspace and saved chats.',
            'Update to FrostPi…', 'rebuild'))
    return alerts
