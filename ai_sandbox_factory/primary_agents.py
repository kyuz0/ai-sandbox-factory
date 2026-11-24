"""Private sign-in companions and the workspace transport for primary agents."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import advisor_credentials, folders, host_platform, prompt_library, sandbox_advisors
from .primary_workspace import AGENTS, WEB_SEARCH_OPTIONS, agent
from .sandbox_options import RESOURCE_PREFIX, access_guidance, options, write_json
from .settings_catalog import CATEGORIES

SOURCE = Path(__file__).resolve().parent / 'containers' / 'primary'
PRIMARY_HOSTS = {**sandbox_advisors.HOSTS,
                 # The native editor loads approval-feature settings here.
                 'codex': [*sandbox_advisors.HOSTS['codex'], 'ab.chatgpt.com:443'],
                 # Antigravity 1.5.13 uses this endpoint during native account startup.
                 'agy': [*sandbox_advisors.HOSTS['agy'], 'play.googleapis.com:443']}


def enabled(config):
    return agent(config) != 'pi'


def network_rules(selected):
    from . import sandbox as core
    if selected == 'claude':
        return core.parse_rules('\n'.join(PRIMARY_HOSTS[selected]))
    catalog = json.loads((SOURCE / (selected + '_network.json')).read_text())
    # Only the shipped service catalog supports domain suffixes. User workspace
    # allowlists retain their existing exact-host validation.
    return [(host, 443) for host in catalog['transport_hosts']] + [
        ('.' + domain, 443) for domain in catalog['transport_domains']]


def names(config):
    prefix = RESOURCE_PREFIX + config['name'] + '-agent'
    return prefix, prefix + '-proxy', prefix + '-workspace', prefix + '-home', prefix + '-egress'


def editor_volume(config):
    return names(config)[0] + '-editor'


def resources(config):
    controller, proxy, *volumes = names(config)
    return {'container': {controller, proxy}, 'volume': {*volumes, editor_volume(config)}}


def describe(config):
    if not enabled(config):
        return {}
    controller, proxy, *_ = names(config)
    return {
        controller: (AGENTS[agent(config)], 'Keeps your account sign-in outside the workspace.'),
        proxy: ('Sign-in network gateway', 'Connects the coding agent to its provider.'),
    }


def image(config):
    from . import sandbox as core
    return f'localhost/pi-sandbox-factory-primary:{agent(config)}-{config["uid"]}-{config["gid"]}-{core.tree_digest(SOURCE)}'


def worker_context(config):
    args = ['--build-context', f'primary-files={SOURCE}']
    if agent(config) in ('codex', 'claude', 'agy'):
        args += ['--build-context', f'primary-controller=docker-image://{image(config)}']
    return args


def build(config):
    if not enabled(config):
        return
    from . import sandbox as core
    tag = image(config)
    engine = config['engine']
    if core.run([engine, 'image', 'inspect', tag], capture=True, check=False, stderr=subprocess.DEVNULL).returncode:
        core.run([engine, 'build', '-t', tag,
                  '--build-arg', f'AGENT={agent(config)}',
                  '--build-arg', f'USER_UID={config["uid"]}',
                  '--build-arg', f'USER_GID={config["gid"]}', SOURCE])


def volume(config, name):
    from . import sandbox as core
    found = core.run([config['engine'], 'volume', 'inspect', name], capture=True, check=False,
                    stderr=subprocess.DEVNULL)
    if found.returncode:
        return False
    labels = json.loads(found.stdout)[0].get('Labels') or {}
    if labels.get(core.LABEL) != config['name'] or labels.get(core.LABEL + '.role') != 'primary-agent':
        raise ValueError(f'Refusing to manage an unrelated volume named {name}')
    return True


def check_ownership(config):
    if not enabled(config):
        return
    from . import sandbox as core
    controller, proxy, *volumes = names(config)
    for name in (controller, proxy):
        data = core.inspect(config, name)
        if data and data['Config']['Labels'].get(core.LABEL + '.role') != 'primary-agent':
            raise ValueError(f'Refusing to manage an unrelated container named {name}')
    for name in [*volumes, editor_volume(config)]:
        volume(config, name)


def guidance(config, state):
    from . import sandbox as core
    from .connectors import context
    current = agent(config)
    selected = options(config.get('options'))
    lines = access_guidance(core.network_mode(config)) + [
        f'The starting directory in the workspace is {json.dumps(folders.container_cwd(config))}.',
        'Read relevant project instructions in the workspace before editing.',
        'Run shell commands and access files through the native tools, which run inside this workspace.',
        'Follow approval requests from the native client. For a change to mounted folders or connector access, '
        'ask the user to review Settings → Folders or Settings → Connectors in Factory.',
        'The workspace has the following mounts:',
    ]
    lines.extend(f'- {json.dumps(path)} ({"read-only" if read_only else "read and write"})'
                 for path, read_only in folders.roots(config))
    if selected['browser']:
        lines.append('Use the Factory browser tool for Playwright and share its live viewer link with the user when needed. '
                     'The browser can reach development servers running inside the workspace.')
    else:
        lines.append('The Factory browser is off. If interactive browser access is needed, ask the user to enable it '
                     'under Settings → Browser.')
    search_key = WEB_SEARCH_OPTIONS.get(current)
    if search_key:
        if selected[search_key]:
            lines.append('Factory has enabled built-in Web Search. Use it when available in the current client. '
                         'Opening a result in the workspace follows the workspace network policy.')
        else:
            lines.append('Factory has turned off built-in Web Search. If a search is needed, ask the user to enable it '
                         f'under Settings → {CATEGORIES["network-settings"].label}.')
    connector_prompt, _ = context.render(config, state)
    if connector_prompt:
        lines.extend(['', connector_prompt.rstrip()])
    skill_catalog = []
    for skill in config.get('extra_skills', []):
        source = Path(skill['path'])
        # Skills come from the same immutable snapshot used to build the worker.
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', skill['name']) or core.tree_digest(source) != skill['digest']:
            raise ValueError('Additional skill snapshot was modified; add its source again')
        skill_catalog.append(f'- {skill["name"]}: read /opt/extra-skills/{skill["name"]}/SKILL.md in the workspace')
    if skill_catalog:
        lines += ['', 'Installed skills. Read the relevant instructions and references in the workspace before using a skill.',
                  *skill_catalog]
    from . import profile_runtime, profile_services
    profile_context = profile_runtime.guidance(state) + profile_services.guidance(config)
    if profile_context:
        lines.append(profile_context)
    selected_prompt = prompt_library.selection(state)
    if selected_prompt:
        lines.extend(['', selected_prompt['text']])
    return '\n'.join(lines) + '\n'


def write_context(config, state):
    from .connectors.context import write_text
    text = guidance(config, state)
    write_text(Path(state) / 'primary-agent/context.md', text)
    if agent(config) in ('codex', 'claude', 'agy'):
        write_text(Path(state) / 'primary-runtime/context.md', text)


def prepare_workspace(config, state):
    write_json(Path(state) / 'primary-runtime' / 'cwd.json', folders.container_cwd(config))
    if agent(config) in ('codex', 'claude', 'agy'):
        settings = {
            'working_directory': folders.container_cwd(config),
            'native_approval': options(config.get('options'))['native_approval'],
            'browser': options(config.get('options'))['browser'],
            'write_roots': [path for path, read_only in folders.roots(config) if not read_only],
        }
        key = agent(config) + '_web_search'
        settings[key] = options(config.get('options'))[key]
        write_json(Path(state) / 'primary-runtime/settings.json', settings)


def refresh_workspace_settings(config, *, refresh_auth=False):
    """Apply Claude's native preferences without restarting its editor or tasks."""
    if agent(config) != 'claude':
        return
    from . import sandbox as core
    container = core.names(config)[0]
    current = core.inspect(config, container)
    if current and current.get('State', {}).get('Running'):
        commands = [['python3', '/opt/primary/claude_client.py', '--factory-prepare']]
        if refresh_auth:
            # Copied native credentials do not always carry an account UUID.
            # Their service generation may remain stable while subscription
            # metadata changes. Invalidate only Factory's public-state marker
            # so an explicit copy refreshes the complete masked native record.
            commands.insert(0, ['rm', '-f', '/home/node/.claude/.factory-auth-generation'])
        for command in commands:
            result = core.run([config['engine'], 'exec', container, *command],
                              capture=True, stderr=subprocess.PIPE, check=False)
            if result.returncode:
                diagnostic = (result.stderr or result.stdout or 'No diagnostic output was returned.').strip()
                raise ValueError(f'Cannot update Claude Code preferences or copied sign-in in {container} '
                                 f'(exit {result.returncode}): {diagnostic} '
                                 'Check the reported file or authentication connection and retry.')


def up(config, state):
    if not enabled(config):
        return
    from . import sandbox as core
    check_ownership(config)
    controller, proxy, socket, home, egress = names(config)
    engine = config['engine']
    selected = agent(config)
    label = ['--label', f'{core.LABEL}={config["name"]}',
             '--label', f'{core.LABEL}.role=primary-agent']
    for name in (socket, home, egress, editor_volume(config)):
        if not volume(config, name):
            core.run([engine, 'volume', 'create', *label, name])
    directory = Path(state) / 'primary-agent'
    directory.mkdir(mode=0o700, exist_ok=True)
    write_json(directory / 'settings.json', {
        'native_approval': options(config.get('options'))['native_approval'],
        'agent': selected, 'browser': options(config.get('options'))['browser'],
        'network_mode': core.network_mode(config),
        'working_directory': folders.container_cwd(config),
    })
    prepare_workspace(config, state)
    write_context(config, state)
    proxy_conf = directory / 'squid.conf'
    # This file stays on the same inode across startups because Squid bind-mounts it.
    proxy_conf.write_text(core.squid_config(network_rules(selected)))
    for name in (proxy, controller):
        data = core.inspect(config, name)
        if data:
            core.resume_existing(config, name, data)
            continue
        args = [engine, 'run', '-d', '--name', name, *label, '--cap-drop=ALL', '--security-opt=no-new-privileges']
        if name == proxy:
            args += ['--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=DAC_OVERRIDE',
                     *host_platform.gateway_labels(),
                     '-v', f'{proxy_conf}:/etc/squid/sandbox.conf:{host_platform.bind_mode()}',
                     '-v', f'{egress}:/run/egress:z', core.image_names(config)[1]]
        else:
            network = 'bridge' if selected in ('codex', 'claude', 'agy') and core.network_mode(config) != 'allowlist' else 'none'
            args += ['--network=' + network, '--security-opt=label=disable',
                     '--user', f'{config["uid"]}:{config["gid"]}',
                     '-v', f'{home}:/home/node', '-v', f'{socket}:/run/primary-workspace:ro',
                     '-v', f'{editor_volume(config)}:/run/primary-editor',
                     '-v', f'{egress}:/run/primary-egress:ro',
                     '-v', f'{directory}:/run/primary-input:ro',
                     '--env', f'FACTORY_PRIMARY_AGENT={selected}']
            if selected in ('codex', 'claude', 'agy') and core.network_mode(config) == 'allowlist':
                args += ['-v', f'{core.names(config)[2]}:/run/workspace-egress:ro']
            if engine == 'podman':
                args += ['--userns=keep-id:uid=1000,gid=1000']
            for key, value in core.RUNTIME_ENV.items():
                args += ['--env', f'{key}={value}']
            args.append(image(config))
        core.run(args)
    editor_ready = ''
    if selected in ('codex', 'claude', 'agy'):
        # A saved CA and the socket relay can exist before the HTTPS proxy has
        # bound its listener. Do not launch a client into that startup window.
        editor_ready = (f' && test -S /run/primary-editor/auth.sock && test -s /run/primary-editor/{selected}-ca.pem'
                        " && bash -c 'true </dev/tcp/127.0.0.1/3130'")
    _wait(config, controller, 'test -f /tmp/factory-primary-ready && test -S /run/primary-egress/proxy.sock' + editor_ready,
          'The coding agent did not start')


def _wait(config, name, command, message):
    from . import sandbox as core
    # Containers usually become ready immediately. Allow time for mount setup and keyring start.
    stop = time.monotonic() + 30
    while time.monotonic() < stop:
        ready = core.run([config['engine'], 'exec', name, 'sh', '-c', command],
                         capture=True, check=False, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            return
        time.sleep(.25)
    raise ValueError(f'{message}. Check {config["engine"]} logs {name}.')


def mounts(config, state):
    if not enabled(config):
        return []
    return ['-v', f'{names(config)[2]}:/run/primary-workspace',
            '-v', f'{editor_volume(config)}:/run/primary-editor:ro',
            '-v', f'{Path(state) / "primary-runtime"}:/run/primary-runtime:ro']


def worker_start(config, container):
    if not enabled(config):
        return
    from . import sandbox as core
    current = agent(config)
    engine = config['engine']
    cwd = folders.container_cwd(config)
    if current in ('codex', 'claude', 'agy'):
        from . import codex_history
        codex_history.migrate(config, container, current)
    if current in ('codex', 'claude', 'agy'):
        _wait(config, container, "bash -c 'true </dev/tcp/127.0.0.1/3130'", f'The {AGENTS[current]} authentication connection did not start')
        core.run([engine, 'exec', container, 'python3', f'/opt/primary/{current}_client.py', '--factory-prepare'])
    if current not in ('codex', 'claude', 'agy') or options(config.get('options'))['browser']:
        core.run([engine, 'exec', '-d', '-w', cwd, container, 'bash', '/opt/primary/worker.sh', current])
    sockets = []
    if current not in ('codex', 'claude', 'agy') or options(config.get('options'))['browser']:
        sockets.append('tools')
    ready = ' && '.join(f'socat -u /dev/null UNIX-CONNECT:/run/primary-workspace/{socket}.sock'
                        for socket in sockets)
    if ready:
        _wait(config, container, ready, 'The workspace tool connection did not start')


def down(config, remove=False):
    if not enabled(config):
        return
    from . import sandbox as core
    check_ownership(config)
    for name in names(config)[:2]:
        if core.inspect(config, name):
            core.run([config['engine'], 'rm', '-f', name] if remove else [config['engine'], 'stop', name])


def delete_volumes(config):
    if not enabled(config):
        return
    from . import sandbox as core
    check_ownership(config)
    for name in [*names(config)[2:], editor_volume(config)]:
        if volume(config, name):
            core.run([config['engine'], 'volume', 'rm', name])


def _terminal(config, action):
    from . import sandbox as core
    if not enabled(config):
        raise ValueError('Choose Codex, Claude or Antigravity to use this sign-in')
    if not sys.stdin.isatty():
        raise ValueError('Opening the coding agent requires an interactive terminal')
    if agent(config) == 'claude':
        args = ['auth', 'login'] if action == 'login' else []
        core.run([config['engine'], 'exec', '-it', '-w', folders.container_cwd(config), core.names(config)[0],
                  '/usr/local/bin/claude', *args])
    elif agent(config) == 'agy':
        args = ['--factory-login'] if action == 'login' else []
        core.run([config['engine'], 'exec', '-it', '-w', folders.container_cwd(config), core.names(config)[0],
                  '/usr/local/bin/agy', *args])
    elif agent(config) == 'codex':
        args = ['login', '--device-auth'] if action == 'login' else []
        core.run([config['engine'], 'exec', '-it', '-w', folders.container_cwd(config), core.names(config)[0],
                  '/usr/local/bin/codex', *args])
    else:
        raise ValueError('Choose Codex, Claude Code or Antigravity to open a native coding agent.')


def open_terminal(config, state):
    _terminal(config, 'open')


def has_sign_in(config):
    from . import sandbox as core
    path = {'codex': '/home/node/.codex/auth.json', 'claude': '/home/node/.claude/.credentials.json'}[agent(config)]
    return core.run([config['engine'], 'exec', names(config)[0], 'test', '-s', path],
                    capture=True, check=False).returncode == 0


def login(config, state):
    _terminal(config, 'login')


def copy_host(config, state):
    from . import sandbox as core
    selected = agent(config)
    if selected == 'pi':
        raise ValueError('Choose Codex, Claude or Antigravity to copy a host sign-in')
    if host_platform.is_mac() and selected == 'agy':
        raise ValueError('This host stores the Antigravity sign-in in its keychain. Choose Sign in instead.')
    try:
        payload = advisor_credentials.read_host(selected)
    except ValueError as error:
        raise ValueError(str(error).replace('Use the advisor sign-in button', 'Choose Sign in')
                         .replace('the advisor sign-in button', 'Sign in')) from None
    up(config, state)
    command = [config['engine'], 'exec', '-i', names(config)[0]]
    if selected == 'agy':
        command += ['secret-tool', 'store', '--label=Antigravity CLI',
                    'service', 'gemini', 'username', 'antigravity']
    else:
        target = '/home/node/.codex/auth.json' if selected == 'codex' else '/home/node/.claude/.credentials.json'
        command += ['python3', '-c', advisor_credentials._WRITE_FILE, target]
    result = core.run(command, input=payload, capture=True, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise ValueError('Could not save the host sign-in. Choose Sign in to connect the coding agent.')
    # Initial startup prepares the worker before copying the host sign-in. The
    # official Claude editor runs its bundled CLI directly, so seed its public
    # placeholders now rather than waiting for a terminal-wrapper invocation.
    refresh_workspace_settings(config, refresh_auth=True)
