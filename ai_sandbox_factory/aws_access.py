"""Host-side settings and role assumption for a sandbox AWS connection."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class InvalidSetting(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def permission_values(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
        raise ValueError('Each permission needs at least one AWS action and resource.')
    return values


def session_policy(grants):
    """Make the grant a ceiling even if a resource policy grants the session access."""
    if not isinstance(grants, list) or not grants:
        raise ValueError('Add at least one AWS permission.')
    actions = {}
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != {'Action', 'Resource'}:
            raise ValueError('Each AWS permission needs Action and Resource.')
        resources = permission_values(grant['Resource'])
        for action in permission_values(grant['Action']):
            if not re.fullmatch(r'[a-zA-Z0-9-]+:[a-zA-Z0-9]+', action):
                raise ValueError('Choose individual AWS actions, such as ec2:DescribeInstances.')
            if action.lower().startswith('sts:'):
                raise ValueError('The host manages role sessions. Choose actions from the AWS service Pi will use.')
            actions.setdefault(action.lower(), set()).update(resources)
    groups = {}
    for action, resources in sorted(actions.items()):
        scope = ('*',) if '*' in resources else tuple(sorted(resources))
        groups.setdefault(scope, []).append(action)
    statements = [{'Effect': 'Deny', 'NotAction': sorted(actions), 'Resource': '*'}]
    for scope, names in groups.items():
        statements.append({'Effect': 'Allow', 'Action': names, 'Resource': list(scope)})
        if scope != ('*',):
            statements.append({'Effect': 'Deny', 'Action': names, 'NotResource': list(scope)})
    return {'Version': '2012-10-17', 'Statement': statements}


def settings(value):
    if value is None or value == {}:
        return {}
    required = {'profile', 'role', 'region', 'grants'}
    if not isinstance(value, dict) or set(value) not in (required, required | {'profile_id', 'profile_name'}):
        raise ValueError('Choose an AWS profile, role, region and permissions.')
    if 'profile_id' in value and (
        not isinstance(value['profile_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['profile_id'])
        or not isinstance(value['profile_name'], str) or not value['profile_name'].strip()
        or not value['profile_name'].isprintable()
    ):
        raise InvalidSetting('name', 'Choose a valid saved AWS access profile.')
    for key in ('profile', 'role', 'region'):
        if not isinstance(value[key], str) or not value[key].strip() or not value[key].isprintable():
            raise InvalidSetting(key, 'Enter the AWS ' + key + '.')
    if not re.fullmatch(r'arn:[a-z0-9-]+:iam::[0-9]{12}:role/[\w+=,.@/-]+', value['role'], re.ASCII):
        raise InvalidSetting('role', 'Enter the ARN of the IAM role this sandbox can assume.')
    if not re.fullmatch(r'[a-z0-9-]+', value['region']):
        raise InvalidSetting('region', 'Enter an AWS region, such as eu-west-1.')
    policy = json.dumps(session_policy(value['grants']), separators=(',', ':'), ensure_ascii=True)
    if len(policy) > 2048:
        raise ValueError('These permissions exceed the AWS STS limit of 2,048 characters for an inline session policy.')
    return dict(value)


def assume(name, connection):
    """Only the restricted role credentials cross from the host into the signer."""
    value = settings(connection)
    # STS role session names accept at most 64 characters.
    label = name if name.startswith('aws-sandbox-') else 'aws-sandbox-' + name
    if len(label) > 64 or not re.fullmatch(r'[a-zA-Z0-9_+=,.@-]+', label):
        label = 'aws-sandbox-' + hashlib.sha256(name.encode()).hexdigest()[:32]
    policy = json.dumps(session_policy(value['grants']), separators=(',', ':'))
    env = {k: v for k, v in os.environ.items() if k not in
           ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN')}
    env.update(AWS_IGNORE_CONFIGURED_ENDPOINT_URLS='true', AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    try:
        result = subprocess.run(['aws', '--profile', value['profile'], '--region', value['region'],
                                 'sts', 'assume-role', '--role-arn', value['role'], '--role-session-name', label,
                                 '--policy', policy, '--query', 'Credentials', '--output', 'json'],
                                env=env, text=True, capture_output=True)
    except FileNotFoundError:
        raise ValueError('Install AWS CLI v2 on this computer to connect the sandbox.') from None
    if result.returncode:
        raise ValueError('AWS could not start the sandbox session. Check your host sign-in and the role’s trust policy.')
    creds = json.loads(result.stdout)
    if not all(isinstance(creds.get(key), str) and creds[key] for key in
               ('AccessKeyId', 'SecretAccessKey', 'SessionToken', 'Expiration')):
        raise ValueError('AWS returned an incomplete role session.')
    return creds


CLIENT_ENV = {'AWS_CONFIG_FILE': '/run/aws-access/client.conf', 'AWS_ACCESS_KEY_ID': 'pi-sandbox',
              'AWS_SECRET_ACCESS_KEY': 'placeholder', 'AWS_EC2_METADATA_DISABLED': 'true'}


def socket_mount(state, mode):
    from . import host_platform
    options = mode if host_platform.is_mac() else mode + ',z'
    return f'{state / "aws-access/socket"}:/run/aws-access:{options}'


def client_mount(state):
    return ['--add-host=aws-access.internal:127.0.0.1',
            '-v', socket_mount(state, 'ro')]


def has_client_mount(container):
    return bool(container and any(mount.get('Type') == 'bind' and
                                  mount.get('Destination') == '/run/aws-access' and not mount.get('RW', True)
                                  for mount in container.get('Mounts', [])))


def worker_name(config):
    from .sandbox_options import RESOURCE_PREFIX
    return RESOURCE_PREFIX + config['name'] + '-aws'


def image():
    from . import sandbox
    return 'localhost/pi-sandbox-factory-aws:' + sandbox.tree_digest(sandbox.ROOT / 'containers/aws-access')


def down(config):
    from . import sandbox
    name = worker_name(config)
    current = sandbox.inspect(config, name)
    if current:
        subprocess.run([config['engine'], 'rm', '-f', current['Id']], check=True, capture_output=True)


def _deliver(engine, worker_id, region, creds):
    result = subprocess.run([engine, 'exec', '-i', worker_id, 'python3', '/app/receive.py'],
                            input=json.dumps({'region': region, 'credentials': creds}), text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Could not deliver the AWS session to its signing container.')


def ensure_renewal(engine, worker_id, filename, name, expiration):
    # An inherited lock lets a later "up" recover a lost renewal process.
    with (filename.parent / (worker_id + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), engine, worker_id,
                          str(filename), name, expiration, str(lock.fileno())], pass_fds=(lock.fileno(),),
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def up(config, state):
    from . import sandbox
    from .sandbox_options import write_json
    value = settings(config.get('aws_access'))
    if not value:
        down(config)
        return
    value = {key: value[key] for key in ('profile', 'role', 'region', 'grants')}
    engine = config['engine']
    name = worker_name(config)
    tag = image()
    identity = hashlib.sha256((tag + json.dumps(value, sort_keys=True)).encode()).hexdigest()
    directory = Path(state) / 'aws-access'
    filename = directory / (identity + '.json')
    old = sandbox.inspect(config, name)
    if old and old['State']['Running'] and old['Config']['Labels'].get('io.pi-sandbox-factory.aws') == identity:
        ensure_renewal(engine, old['Id'], filename, config['name'], datetime.now(timezone.utc).isoformat())
        return
    creds = assume(config['name'], value)
    if subprocess.run([engine, 'image', 'inspect', tag], capture_output=True).returncode:
        sandbox.run([engine, 'build', '-t', tag, sandbox.ROOT / 'containers/aws-access'])
    directory.mkdir(exist_ok=True, mode=0o700)
    socket_dir = directory / 'socket'
    socket_dir.mkdir(exist_ok=True, mode=0o755)
    write_json(filename, value)
    down(config)
    args = [engine, 'run', '-d', '--name', name, '--label', sandbox.LABEL + '=' + config['name'],
            '--label', 'io.pi-sandbox-factory.aws=' + identity, '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user', f'{config["uid"]}:{config["gid"]}',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev', '-v', socket_mount(state, 'rw')]
    if engine == 'podman':
        args += ['--userns=keep-id:uid=1000,gid=1000']
    worker_id = subprocess.check_output(args + [tag, value['region']], text=True).strip()
    try:
        _deliver(engine, worker_id, value['region'], creds)
        ensure_renewal(engine, worker_id, filename, config['name'], creds['Expiration'])
    except BaseException:
        subprocess.run([engine, 'rm', '-f', worker_id], capture_output=True)
        raise


def renew(engine, worker_id, filename, sandbox_name, expiration):
    value = settings(json.loads(Path(filename).read_text()))
    while True:
        expires = datetime.fromisoformat(expiration.replace('Z', '+00:00'))
        delay = max(0, (expires - datetime.now(timezone.utc)).total_seconds() - 300)
        try:
            subprocess.run([engine, 'wait', worker_id], timeout=delay, capture_output=True)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            creds = assume(sandbox_name, value)
            _deliver(engine, worker_id, value['region'], creds)
            expiration = creds['Expiration']
        except (ValueError, OSError, subprocess.SubprocessError):
            try:
                subprocess.run([engine, 'wait', worker_id], timeout=60, capture_output=True)
                return
            except subprocess.TimeoutExpired:
                pass


if __name__ == '__main__':
    with os.fdopen(int(sys.argv[-1]), 'a'):
        renew(*sys.argv[1:-1])
