"""Validated, shared settings for the CLI, cockpit and container bootstrap."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from . import web_access, network_groups, communications, git_settings
from .settings_catalog import CATEGORIES, INSTALL_SYSTEM_PACKAGES, NETWORK_PATH

SKILLS = {}
PROFILE_SKILLS = {'base': []}
RESOURCE_PREFIX = 'pi-sandbox-factory-'
MAIN_AGENT_SANDBOX = 'Main agent sandbox'
# The longest generated name is a Podman volume stored as a Linux directory.
SANDBOX_NAME_LIMIT = 255 - len(RESOURCE_PREFIX + '-inference-socket')
SANDBOX_NAME = re.compile(rf'[a-z0-9][a-z0-9-]{{0,{SANDBOX_NAME_LIMIT - 1}}}')
SANDBOX_NAME_ERROR = (
    f'Use up to {SANDBOX_NAME_LIMIT} lowercase letters, digits or hyphens for the sandbox name. '
    'Start with a letter or digit.'
)
# Label, inline help, and the full explanation shown by Explain.
APPROVAL_MODES = {
    'automatic': ('Auto (non-interactive)',
                  'The reviewer model checks actions without asking you.',
                  'uses a reviewer model to check actions that need approval. If the reviewer blocks an action, '
                  'the agent gets the reason and can try another way. You won’t get an approval prompt.'),
    'assisted': ('Auto (interactive)',
                 'You can approve an action the reviewer rejects or leaves open.',
                 'uses a reviewer model to check actions that need approval. If the reviewer rejects an action '
                 'or leaves the decision to you, Pi shows you the exact action and the reviewer’s reason. '
                 'You can approve that action once or deny it.'),
    'manual': ('Manual approval (ask me)',
               'Pi asks you to approve actions that need permission.',
               'with the inner sandbox on, Pi can run Bash inside it without your approval but if a command '
               'needs to run outside the inner sandbox, Pi asks you to approve the escalation. '
               'With the inner sandbox off, you need to approve every Bash command. File tools such as '
               'read and write ask for approval when their permission rules require it.'),
    'off': ('YOLO (no approvals)',
            'Runs without approval prompts or reviewer checks.',
            'skips approval prompts and reviewer checks. To run Bash directly in the main agent sandbox, '
            'turn off the inner sandbox too. If you leave it on, Bash stays inside it.'),
}
DEFAULTS = {'approval': 'automatic', 'approval_model': 'current', 'native_approval': 'automatic', 'inner_sandbox': True, 'goals': True,
            'scheduling': True, 'question_forms': True, 'browser': True, 'codex_web_search': False,
            'claude_web_search': False, 'agy_web_search': False,
            'protect_credentials': True, 'protect_browser_credentials': True, 'compaction_recall': 'lite',
            'knowledge_embedding': 'off', 'knowledge_embedding_provider': '', 'knowledge_embedding_url': '', 'knowledge_embedding_model': '',
            'disabled_skills': [], 'web_access': None, 'website_groups': [], 'communications': None,
            'git_identity': None}

COMMON_CREDENTIAL_PATTERNS = ('*.pem', '*.key', '*.env', '*.env.*')
BROWSER_VAULT_PATTERNS = ('/home/node/.betterwright/vault', '/home/node/.betterwright/vault/*')


def embedding_url(url):
    if not isinstance(url, str) or not url:
        raise ValueError('Enter the embedding server URL.')
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError('Enter a valid embedding server URL.') from None
    if parsed.scheme not in ('https', 'http'):
        raise ValueError('Start the embedding server URL with http:// or https://.')
    if not parsed.hostname:
        raise ValueError('Enter the server address after http:// or https://.')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('Remove the username and password from the embedding server URL.')
    if parsed.query or parsed.fragment or '?' in url or '#' in url:
        raise ValueError('Remove the query and fragment from the embedding server URL.')
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError('Remove spaces and control characters from the embedding server URL.')
    if any(c in url for c in '\\%'):
        raise ValueError('Use an embedding server URL without backslashes or encoded characters.')
    if any(part in ('.', '..') for part in parsed.path.split('/')) or '//' in parsed.path:
        raise ValueError('Remove repeated slashes and dot segments from the API path.')
    try:
        port = parsed.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError('Enter a valid port for the embedding server.')
    url = url.rstrip('/')
    if url.endswith('/embeddings'):
        raise ValueError('Enter the API base URL without /embeddings. Factory adds that path.')
    return url


def embedding_model(model):
    if not isinstance(model, str) or not model.strip():
        raise ValueError('Enter the embedding model ID.')
    if any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise ValueError('Use a model ID without line breaks or control characters.')
    return model


def options(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Unknown sandbox options')
    result = {**DEFAULTS, **value}
    if result['approval'] not in APPROVAL_MODES:
        raise ValueError('Approval must be automatic, assisted, manual or off')
    if result['native_approval'] not in ('automatic', 'manual'):
        raise ValueError('Choose an approval mode from the list.')
    ref = result['approval_model']
    if ref != 'current' and (not isinstance(ref, str) or not re.fullmatch(r'[^/\s]+(?:/[^/\s]+)+', ref)
                             or not ref.isprintable()):
        raise ValueError('Select a provider and model for approvals.')
    for key in ('inner_sandbox', 'goals', 'scheduling', 'question_forms', 'browser', 'codex_web_search', 'claude_web_search', 'agy_web_search', 'protect_credentials', 'protect_browser_credentials'):
        if type(result[key]) is not bool:
            raise ValueError(key + ' must be a boolean')
    if result['compaction_recall'] not in ('lite', 'full', 'off'):
        raise ValueError('Conversation recall must be lite, full or off')
    if result['knowledge_embedding'] not in ('off', 'endpoint', 'local'):
        raise ValueError('Choose keyword search or an embedding server.')
    if result['knowledge_embedding'] == 'endpoint':
        provider = result['knowledge_embedding_provider']
        if not isinstance(provider, str) or (provider not in ('', '@custom') and not re.fullmatch(r'[a-z][a-z0-9-]{0,254}', provider)):
            raise ValueError('Choose a valid embedding provider.')
        result['knowledge_embedding_url'] = embedding_url(result['knowledge_embedding_url'])
        result['knowledge_embedding_model'] = embedding_model(result['knowledge_embedding_model'])
    else:
        result.update(knowledge_embedding_provider='', knowledge_embedding_url='', knowledge_embedding_model='')
    disabled = result['disabled_skills']
    if not isinstance(disabled, list) or any(x not in SKILLS for x in disabled):
        raise ValueError('Unknown disabled skill')
    result['web_access'] = web_access.settings(result['web_access'])
    result['communications'] = communications.settings(result['communications'])
    result['git_identity'] = git_settings.identity(result['git_identity'])
    result['website_groups'] = network_groups.selected(result['website_groups'])
    return result


def endpoint_rule(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('Use an HTTP(S) endpoint without credentials or fragments')
    return f'{parsed.hostname}:{parsed.port or (443 if parsed.scheme == "https" else 80)}'


def memory_limit(value):
    if value in (None, '', 'none'):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*[bBkKmMgG]?', value):
        raise ValueError('Memory must be a positive size such as 8g, or none')
    return value


def permissions(template, selected):
    selected = options(selected)
    policy = json.loads(json.dumps(template))
    policy['authorizerChain'] = ['pi-auto-review'] if selected['approval'] in ('automatic', 'assisted') else []
    policy['yoloMode'] = selected['approval'] == 'off'
    rules = policy['permission']
    paths = rules.setdefault('path', {})
    for pattern in BROWSER_VAULT_PATTERNS:
        if selected['protect_browser_credentials']:
            paths[pattern] = 'deny'
        elif paths.get(pattern) == 'deny':
            paths.pop(pattern)
    if not selected['protect_credentials']:
        paths = rules.get('path')
        if isinstance(paths, dict):
            for pattern in COMMON_CREDENTIAL_PATTERNS:
                if paths.get(pattern) == 'deny':
                    paths.pop(pattern)
    # Native read tools already admit these trusted image paths. Give proven
    # shell reads the same scope; unknown effects and writes still need review.
    reads = rules.setdefault('external_directory_read', {})
    for path in policy.get('piInfrastructureReadPaths', []) if isinstance(reads, dict) else []:
        if path.startswith('/') and not any(c in path for c in '*?'):
            reads.setdefault(path.rstrip('/'), 'allow')
            reads.setdefault(path.rstrip('/') + '/*', 'allow')
    rules['bash'] = ('allow' if selected['inner_sandbox'] or selected['approval'] == 'off' else 'ask')
    if selected['browser']:
        rules.update({name: 'allow' for name in ('browser', 'browser_login', 'browser_evidence')})
    if selected['knowledge_embedding'] != 'off':
        rules.update({name: 'allow' for name in ('knowledge_add', 'knowledge_update', 'knowledge_search',
                                                 'knowledge_symbol_search', 'knowledge_status', 'knowledge_show')})
    return policy


def check_reviewer_provider(config, provider):
    selected = config.get('options') or {}
    model = selected.get('approval_model', 'current')
    if selected.get('approval', 'automatic') in ('automatic', 'assisted') and model.startswith(provider + '/'):
        raise ValueError('Approvals use this provider. In Settings → Approvals & sandbox, choose another model '
                         'or use the current model first.')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.new')
    # Never follow a pre-existing temporary symlink.
    temporary.unlink(missing_ok=True)
    with temporary.open('x') as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)


def access_guidance(network):
    lines = [
        'You are working in an AI Sandbox Factory workspace. Your shell commands and development tools '
        f'run in the {MAIN_AGENT_SANDBOX}, with access to your project files. '
        'The agent and workspace terminal run without sudo. '
        f'If a system package is missing, tell the user which Debian package the task needs and why. Ask them to use '
        f'Actions → {INSTALL_SYSTEM_PACKAGES} on this sandbox in Factory.',
    ]
    if network == 'allowlist':
        lines.append('Network is Restricted. Workspace requests use the inherited proxy and configured allowlist. '
                     f'If access is denied, ask the user to allow the specific hostname and port under {NETWORK_PATH}.')
    else:
        lines.append('Network is Unrestricted at the container boundary.')
    lines.append('If the task needs a capability that is off or unavailable, explain why, ask the user for the smallest '
                 'change that would unblock the task, and wait for confirmation before using it. '
                 'Check the tools available in the current session after a change.')
    return lines


def guidance(selected, subagents=False, *, network='allowlist'):
    selected = options(selected)
    mode, inner = selected['approval'], selected['inner_sandbox']
    features = CATEGORIES['agent-tools'].label
    search_settings = CATEGORIES['network-settings'].label
    lines = access_guidance(network)
    lines.append('Use ask_user_question for a settings request when a question form would help, or ask in chat.'
                 if selected['question_forms'] else f'Question forms are off in Settings → {features}. Ask in chat.')
    if inner:
        lines.append('Ordinary bash runs inside the inner OS sandbox without command-level model review. '
                     'Workspace edits are allowed; private home and security configuration are protected.')
        lines.append({'assisted': 'The reviewer checks boundary requests. Pi asks the user if the reviewer rejects the action or leaves the decision open. If the reviewer fails, the action stays blocked. Use bash_escalate with an exact command and justification.',
                      'automatic': 'Boundary requests use the approval reviewer without human approval dialogs. Review failures return a reason; retry the review or choose an allowed alternative without bypassing a rejection. Use bash_escalate with an exact command and justification when access outside the inner sandbox is required.',
                      'manual': 'Boundary requests require human approval. Use bash_escalate with an exact command and justification when access outside the inner sandbox is required.',
                      'off': 'Approval prompts are disabled. Requests beyond the inner sandbox are refused; bash_escalate is unavailable.'}[mode])
    else:
        lines.append('The inner command sandbox is disabled. Bash executes inside the main agent sandbox. '
                     + {'automatic': 'Shell commands require automatic review without human approval dialogs. Failures return a reason to the agent.',
                        'assisted': 'The reviewer checks shell commands. Pi asks the user if the reviewer rejects a command or leaves the decision open. If the reviewer fails, the command stays blocked.',
                        'manual': 'Shell commands require human approval.',
                        'off': 'Approval prompts are disabled; commands execute directly.'}[mode])
    if inner:
        credential_text = ('Common credential files such as .env and private keys are protected from agent reads.'
                           if selected['protect_credentials'] else
                           'The agent may read and write common credential files such as .env and private keys in the workspace. '
                           'Their contents are visible to the model and the agent may change them.')
        lines.append('Read installed skill instructions and references with read, grep, find or ls. '
                     'Read-only shell commands can also read the trusted installed tool and skill directories. '
                     'This does not permit changing those files. ' + credential_text)
        lines.append('Each bash call has its own temporary directory in $TMPDIR, removed when the call ends. '
                     'Container /tmp is also writable scratch space shared between calls; it is not the host /tmp. '
                     'Private runtime files live outside it.')
        if mode != 'off':
            lines.append('For Pi resource files outside the workspace, use request_file_access to ask to read a specific '
                         'instruction, skill or prompt text file, or list one resource folder. '
                         'Check active tools and skills to explain the sandbox’s capabilities.')
            lines.append('The inner sandbox blocks Unix sockets, which tmux needs. '
                         'Use bash_escalate for tmux creation, send-keys, capture-pane, session checks and cleanup. '
                         "Use tmux -S with the session's socket path on every call. "
                         'Do not retry tmux in ordinary bash or change socket directories to work around the restriction. '
                         'The reviewer must approve the exact outer-container command; a denial still stops it.')
        else:
            lines.append('tmux cannot run in the inner sandbox because Unix sockets are blocked. '
                         'bash_escalate is unavailable with approval disabled; ask the user to select an approval mode if tmux is needed.')
    if inner:
        lines.append(('For network commands in bash, keep the inherited proxy settings. The outer network allowlist still applies. '
                      if network == 'allowlist' else '')
                     + 'Configured network destinations are preapproved in the inner shell. '
                     + ('Other outbound connections are reviewed. ' if mode != 'off' else 'Other outbound connections are refused while approval is off. ')
                     + 'The inner shell has a separate network namespace. '
                     + ('Container-local services, including the inference proxy on outer-container loopback, may require '
                        'bash_escalate. Escalation does not grant new network destinations or reveal provider keys.'
                        if mode != 'off' else 'Ask the user to choose an approval mode if you need to reach a service on '
                        'outer-container loopback.'))
    if selected['browser']:
        server_shell = ('bash_escalate (or the editor terminal)' if mode != 'off' else 'the editor terminal') if inner else 'bash (or the editor terminal)'
        lines.append('Use the native browser tool for Playwright automation. For browser testing, start the dev server with '
                     + server_shell + ', running it in the background with nohup and redirected input/output '
                     '(for example, nohup npm run dev >/tmp/dev.log 2>&1 </dev/null &), then open http://localhost:PORT in browser. '
                     + ('Ordinary bash has a separate network namespace and cannot host a server reachable by this browser. ' if inner else '')
                     + 'Local pages, API calls, WebSockets and popups need no network approval.')
        if selected['protect_browser_credentials']:
            lines.append('The saved Betterwright credential vault at ~/.betterwright/vault is protected from native file tools. '
                         'Use browser account selection and login on matching sites; do not inspect, dump or copy vault files through bash_escalate. '
                         'An explicitly authorized browser_session_export is a separate scoped login handoff for a same-service local script, '
                         'not permission to read the vault. Keep session values out of output, logs and Git.')
    else:
        lines.append(f'The Factory browser is off. If interactive browser access is needed, ask the user to enable '
                     f'Agent browser under Settings → {features}.')
    lines.append('Explicit permission denials must be respected. Commands cannot execute on the physical host. '
                 'Use inherited proxy settings in restricted network mode.')
    lines.append('Standard pi-subagents are enabled and load the sandbox permission rules.' if subagents
                 else f'Subagents are off in Settings → {features}. Work in the current agent until enabled.')
    if selected['goals']:
        lines.append('The shared Pi goal extension continues work in the current session. '
                     'Use /goal only when the user explicitly requests a goal. '
                     'Set turn or cost limits only when the user specifies them. '
                     'Use goal_update to report verified completion or request help when blocked.')
    else:
        lines.append(f'Goals are off in Settings → {features}.')
    if selected['scheduling']:
        lines.append('Use schedule_prompt for requested future or recurring prompts. Jobs inject into this chat, '
                     'with its model and approval rules. Timers run only while this Pi session is open; '
                     'never promise delivery while Pi or the container is stopped. /schedule-prompt manages jobs.')
    else:
        lines.append(f'Scheduled prompts are off in Settings → {features}.')
    lines.append(f'Conversation recall is set to {selected["compaction_recall"].capitalize()} under Settings → {features}.')
    lines.append(web_access.guidance(selected['web_access']) or
                 f'Web Search is off. Ask the user to configure it under Settings → {search_settings} if a search is needed.')
    return '\n\n'.join(x for x in lines if x) + '\n'
