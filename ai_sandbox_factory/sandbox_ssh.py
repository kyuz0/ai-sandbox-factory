"""Host-managed SSH destinations and private keys, isolated from the development container."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from .sandbox_options import RESOURCE_PREFIX


DEFAULT_UNLOCK_DURATION = '8h'
# The Linux companion's sun_path has 108 bytes, including the terminating NUL.
MAX_ALIAS_LENGTH = 107 - len('/run/ssh-access/.sock')
# Aliases are embedded in an OpenSSH Host pattern, a known_hosts entry and an
# unquoted ProxyCommand relay path. Keep shell/pattern separators out of that path.
ALIAS = re.compile(r'[a-zA-Z0-9_.+=-]+')
# OpenSSH's sshbuf_load_fd rejects files beyond SSHBUF_SIZE_MAX.
MAX_KEY_BYTES = 128 * 1024 * 1024


def unlock_seconds(duration):
    value = re.fullmatch(r'([1-9][0-9]{0,9})([smhd])', duration.strip().lower()) if isinstance(duration, str) else None
    if value:
        seconds = int(value[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[value[2]]
        if seconds <= 2**31 - 1:
            return seconds
    raise ValueError('Enter a positive unlock duration, such as 8h or 2d.')


def key_state(config, *, companion_running):
    """Inspect only a running SSH companion already verified as owned."""
    if not companion_running:
        return 'unavailable'
    from . import sandbox as core
    probe = (
        'ssh-add -l >/dev/null 2>&1; status=$?; '
        'if [ "$status" = 0 ]; then printf loaded; '
        'elif [ "$status" != 1 ]; then printf unavailable; '
        'elif [ -f /private/id_key ]; then printf locked; '
        'else printf missing; fi'
    )
    try:
        result = core.run([config['engine'], 'exec', names(config)[0], 'sh', '-c', probe],
                          capture=True, check=False, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'
    state = result.stdout.strip()
    return state if result.returncode == 0 and state in ('loaded', 'locked', 'missing') else 'unavailable'


def validate(value):
    if not isinstance(value, dict) or set(value) - {'hosts'} or not isinstance(value.get('hosts', []), list):
        raise ValueError('SSH settings require a hosts list')
    hosts = value.get('hosts', [])
    seen = set()
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {'alias','hostname','port','user','host_keys'}:
            raise ValueError('Each SSH destination needs alias, hostname, port, user and trusted host_keys')
        alias = host['alias']
        validate_alias(alias)
        if alias in seen:
            raise ValueError(f'Duplicate SSH alias {alias!r}. Give each destination a different alias.')
        seen.add(alias)
        name = host['hostname']
        if not isinstance(name, str) or not name or len(name) > 253: raise ValueError('Invalid SSH hostname')
        try: ipaddress.ip_address(name)
        except ValueError:
            if any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part) for part in name.split('.')):
                raise ValueError('Use an exact IP address or hostname for SSH')
        if not isinstance(host['user'], str) or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]*', host['user']):
            raise ValueError('Invalid SSH username')
        if type(host['port']) is not int or not 1 <= host['port'] <= 65535: raise ValueError('Invalid SSH port')
        keys = host['host_keys']
        if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}', k) for k in keys):
            raise ValueError('Supply trusted plain OpenSSH host public keys; wildcard/CA entries are not accepted')
    return {'hosts': hosts}


class MissingHostKey(ValueError): pass


def validate_alias(alias):
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias.startswith('-'):
        raise ValueError(f'Invalid SSH alias {alias!r}. This connector uses aliases in SSH commands and relay filenames. '
                         'Use letters, digits, dots, hyphens, underscores, + or =; do not start with a hyphen. '
                         'Enter Host names from your SSH config, not a command or wildcard pattern.')
    if len(alias) > MAX_ALIAS_LENGTH:
        raise ValueError(f'SSH alias {alias!r} has {len(alias)} characters. The relay socket path allows '
                         f'at most {MAX_ALIAS_LENGTH} characters; use a shorter Host alias in your SSH config.')

def trusted_keys(hostname, port, known_hosts):
    target = hostname if port == 22 else f'[{hostname}]:{port}'
    if not Path(known_hosts).expanduser().is_file(): raise MissingHostKey(f'Known hosts file not found: {Path(known_hosts).expanduser()}')
    result = subprocess.run(['ssh-keygen', '-F', target, '-f', str(Path(known_hosts).expanduser())], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError(f'Could not read {known_hosts}: ssh-keygen exited with status {result.returncode}. '
                         + (result.stderr.strip() or result.stdout.strip() or 'ssh-keygen returned no diagnostic output.'))
    keys = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'): continue
        if fields[0].startswith('@'): raise ValueError('Marked host-key entries need explicit review; import a trusted plain host key')
        if len(fields) >= 3: keys.append(' '.join(fields[1:3]))
    if not keys: raise MissingHostKey(f'No trusted host key for {hostname}. Verify this host from your host terminal first.')
    return sorted(set(keys))


def parse_hosts(text, known_hosts):
    hosts = []
    for line in text.splitlines():
        fields = line.split('#',1)[0].split()
        if not fields: continue
        if len(fields) not in (3,4): raise ValueError('Use: alias username hostname-or-IP [port]')
        alias,user,hostname = fields[:3]
        try: port = int(fields[3]) if len(fields) == 4 else 22
        except ValueError: raise ValueError('SSH port must be a number')
        # Validate targets before passing them to OpenSSH helpers.
        item = {'alias':alias,'user':user,'hostname':hostname,'port':port,'host_keys':['ssh-ed25519 AAAA']}
        validate({'hosts':[item]})
        item['host_keys'] = trusted_keys(hostname,port,known_hosts)
        hosts.append(item)
    return validate({'hosts':hosts})


def import_aliases(aliases, ssh_config=None):
    hosts = []
    if not aliases.split():
        raise ValueError('Enter one or more Host aliases from your SSH config, separated by spaces.')
    for alias in aliases.split():
        validate_alias(alias)
        args = ['ssh'] + (['-F',str(Path(ssh_config).expanduser())] if ssh_config else []) + ['-G',alias]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError(f'Could not import SSH alias {alias!r}: ssh -G exited with status {result.returncode}. '
                             + (result.stderr.strip() or result.stdout.strip() or 'ssh returned no diagnostic output.'))
        config = {}
        for line in result.stdout.splitlines():
            key,_,value = line.partition(' '); config.setdefault(key,[]).append(value)
        if config.get('proxyjump',['none'])[0] != 'none' or config.get('proxycommand',['none'])[0] != 'none':
            directive = 'ProxyJump' if config.get('proxyjump', ['none'])[0] != 'none' else 'ProxyCommand'
            raise ValueError(f'SSH alias {alias!r} uses {directive}, which this connector does not support. '
                             'Choose a directly reachable host.')
        hostname,user,port = config['hostname'][0],config['user'][0],int(config['port'][0])
        keys, missing = [], []
        for path in config.get('userknownhostsfile',['~/.ssh/known_hosts'])[0].split():
            try: keys += trusted_keys(config.get('hostkeyalias',[hostname])[0],port,path)
            except MissingHostKey as error: missing.append(str(error))
        if not keys:
            raise ValueError(f'No trusted host key for SSH alias {alias!r} ({hostname}:{port}). '
                             'Verify this host in your host terminal first.\n' + '\n'.join(missing))
        hosts.append({'alias':alias,'hostname':hostname,'user':user,'port':port,'host_keys':sorted(set(keys))})
    return validate({'hosts':hosts})


def enabled(config): return bool(config.get('ssh',{}).get('hosts'))


def names(config):
    prefix = RESOURCE_PREFIX + config['name'] + '-ssh'
    return prefix, prefix+'-socket', prefix+'-keys'


def image(config):
    from . import sandbox as core
    return f'localhost/pi-sandbox-factory-ssh:{config["uid"]}-{config["gid"]}-{core.tree_digest(core.ROOT / "containers/ssh-access")}'


def build(config):
    if not enabled(config): return
    from . import sandbox as core
    core.run([config['engine'],'build','-t',image(config),'--build-arg',f'USER_UID={config["uid"]}',
              '--build-arg',f'USER_GID={config["gid"]}',core.ROOT/'containers/ssh-access'])


def check_ownership(config):
    from . import sandbox as core
    worker,*volumes=names(config)
    core.inspect(config,worker)
    for volume in volumes:
        r=core.run([config['engine'],'volume','inspect',volume],capture=True,check=False,stderr=subprocess.DEVNULL)
        if r.returncode==0 and (json.loads(r.stdout)[0].get('Labels') or {}).get(core.LABEL)!=config['name']:
            raise ValueError('Refusing to manage unrelated SSH volume: '+volume)


def client_config(hosts):
    lines=[]
    for h in hosts:
        lines += [f'Host {h["alias"]}',f'  HostName {h["hostname"]}',f'  User {h["user"]}',f'  Port {h["port"]}',
                  f'  HostKeyAlias {h["alias"]}',f'  ProxyCommand socat - UNIX-CONNECT:/run/ssh-access/{h["alias"]}.sock',
                  '  IdentityAgent /run/ssh-access/agent.sock','  IdentityFile none',
                  '  UserKnownHostsFile /etc/sandbox-ssh/known_hosts','  StrictHostKeyChecking yes',
                  '  PasswordAuthentication no','  KbdInteractiveAuthentication no','  BatchMode yes',
                  '  ForwardAgent no','  ClearAllForwardings yes']
    return '\n'.join(lines)+ '\n'


def up(config,state):
    if not enabled(config): return
    from . import sandbox as core
    hosts=validate(config['ssh'])['hosts'];check_ownership(config)
    worker,socket,keys=names(config);engine=config['engine'];target=state/'ssh';target.mkdir(exist_ok=True,mode=0o700)
    (target/'targets.json').write_text(json.dumps(hosts));(target/'targets.json').chmod(0o644)
    (target/'known_hosts').write_text(''.join(f'{h["alias"]} {key}\n' for h in hosts for key in h['host_keys']))
    (target/'known_hosts').chmod(0o644)
    (target/'config').write_text(client_config(hosts));(target/'config').chmod(0o644)
    for volume in (socket,keys):
        if core.run([engine,'volume','inspect',volume],capture=True,check=False,stderr=subprocess.DEVNULL).returncode:
            core.run([engine,'volume','create','--label',f'{core.LABEL}={config["name"]}',volume])
    existing=core.inspect(config,worker)
    if existing: core.resume_existing(config,worker,existing)
    else:
        args=[engine,'run','-d','--name',worker,'--label',f'{core.LABEL}={config["name"]}',
              '--cap-drop=ALL','--security-opt=no-new-privileges','--security-opt=label=disable','--pids-limit=-1',
              '--user',f'{config["uid"]}:{config["gid"]}','-v',f'{socket}:/run/ssh-access','-v',f'{keys}:/private',
              '-v',f'{target}:/etc/ssh-access:ro']
        if engine=='podman':args+=['--userns=keep-id:uid=1000,gid=1000']
        core.run(args+[image(config)])
    for _ in range(80):
        r=core.run([engine,'exec',worker,'test','-S','/run/ssh-access/'+hosts[-1]['alias']+'.sock'],capture=True,check=False,stderr=subprocess.DEVNULL)
        if r.returncode==0:return
        time.sleep(.25)
    raise ValueError('SSH service did not become ready; inspect its container logs')


def mounts(config,state):
    if not enabled(config):return []
    return ['-v',f'{names(config)[1]}:/run/ssh-access:ro','-v',f'{state / "ssh"}:/etc/sandbox-ssh:ro',
            '--env','SSH_AUTH_SOCK=/run/ssh-access/agent.sock']


def down(config,remove=False):
    from . import sandbox as core
    check_ownership(config);worker=names(config)[0]
    if core.inspect(config,worker):core.run([config['engine'],'rm','-f',worker] if remove else [config['engine'],'stop',worker])


def delete_volumes(config):
    from . import sandbox as core
    check_ownership(config)
    for volume in names(config)[1:]:
        if core.run([config['engine'],'volume','inspect',volume],capture=True,check=False,stderr=subprocess.DEVNULL).returncode==0:
            core.run([config['engine'],'volume','rm',volume])


def unlock(config, state, duration=DEFAULT_UNLOCK_DURATION):
    seconds = unlock_seconds(duration)
    if not enabled(config):raise ValueError('Configure SSH destinations first')
    from . import sandbox as core
    up(config,state)
    tty=['-it'] if sys.stdin.isatty() else ['-i']
    # Run the same helper as a new companion. Existing companions can use
    # the new duration without replacing their running agent.
    helper = (core.ROOT / 'containers/ssh-access/unlock.py').read_text()
    core.run([config['engine'], 'exec', *tty, names(config)[0], 'python3', '-c', helper, str(seconds)])


def key_source(config, path):
    source=Path(path).expanduser().resolve(strict=True)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_KEY_BYTES:
        raise ValueError('Select a nonempty SSH private key file up to 128 MiB.')
    shared = [config.get(field) for field in ('workspace', 'pi_config')]
    shared += [row['source'] for row in config.get('extra_folders', [])]
    for path in shared:
        if path:
            exposed = Path(path).resolve()
            if source == exposed or exposed in source.parents:
                raise ValueError('Choose a key outside all shared folders; those paths are visible to the agent')
    return source


def import_key(config, state, path, duration=DEFAULT_UNLOCK_DURATION):
    unlock_seconds(duration)
    if not enabled(config):raise ValueError('Configure SSH destinations first')
    source = key_source(config, path)
    from . import sandbox as core
    up(config,state)
    # No key bytes in argv, environment, logs, project state, or the development container.
    script = """import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

os.umask(0o077)
limit = int(sys.argv[1])
with TemporaryDirectory(prefix='.import-', dir='/private') as temp:
    key = Path(temp) / 'id_key'
    size = 0
    with key.open('xb') as target:
        while chunk := sys.stdin.buffer.read(65536):
            size += len(chunk)
            if size > limit:
                sys.exit('The SSH key file exceeds the OpenSSH file size limit.')
            target.write(chunk)
    if not size:
        sys.exit('The SSH key file is empty.')
    key.replace('/private/id_key')
"""
    with source.open('rb') as stream:
        core.run([config['engine'], 'exec', '-i', names(config)[0], 'python3', '-c', script,
                  str(MAX_KEY_BYTES)], stdin=stream)
    print('Key imported into the private SSH companion. Unlocking for the declared destinations.',flush=True)
    unlock(config, state, duration)


def repository_destination(url):
    """Turn a GitHub/GitLab project URL into a fixed SSH destination and setup links."""
    from urllib.parse import urlsplit
    import shlex
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError('Enter an HTTPS project URL without spaces')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise ValueError('Use an HTTPS project URL, such as https://github.com/owner/project')
    hostname = parsed.hostname or ''
    path = parsed.path.rstrip('/').removesuffix('.git').lstrip('/')
    parts = path.split('/')
    if len(parts) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', part) or part in ('.', '..') for part in parts):
        raise ValueError('Enter the project URL, including its owner or group and repository')
    if hostname == 'github.com' and len(parts) != 2:
        raise ValueError('Use https://github.com/owner/repository')
    host = dict(alias='repository', hostname=hostname, user='git', port=22, host_keys=['ssh-ed25519 AAAA'])
    validate({'hosts': [host]})
    base = 'https://' + hostname + '/' + path
    return {'line': f'repository git {hostname} 22',
            'settings_url': base + ('/settings/keys' if hostname == 'github.com' else '/-/settings/repository'),
            'clone': 'git clone ' + shlex.quote('git@repository:' + path + '.git')}


def repository_key(config, state, *, generate=False):
    """Create or inspect a key inside the existing companion; return only its public half."""
    if not enabled(config):
        raise ValueError('Apply SSH destinations before configuring a key')
    from . import sandbox as core
    if core.run([config['engine'], 'image', 'inspect', image(config)], capture=True,
                check=False, stderr=subprocess.DEVNULL).returncode:
        raise ValueError('Apply SSH destinations first to build the SSH companion')
    up(config, state)
    result = core.run([config['engine'], 'exec', names(config)[0], 'python3',
                       '/opt/ssh-access/key.py', 'generate' if generate else 'show'],
                      capture=True, check=False, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Could not read the public key. Unlock the key and retry.')
    public = result.stdout.strip()
    if not re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?', public):
        raise ValueError('The SSH companion did not return one public key')
    return public
