"""Manage the credential companion; the development container receives only its socket."""
import hashlib
import fcntl
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

PORT = 19431
SENTINEL = 'factory-inference'


def local_route_id(port):
    return f'local-{port}'


def local_route(port):
    return {
        'origin': f'http://local-model.internal:{port}', 'local': True,
        'auth': {'type': 'bearer'}, 'basePath': '/v1',
        'operations': [{'method': 'GET', 'path': path} for path in ('/v1/models', '/props', '/server_info')]
        + [{'method': 'POST', 'path': '/v1/chat/completions'}],
    }


def names(config):
    name = 'pi-sandbox-factory-' + config['name'] + '-inference'
    return name, name + '-socket', name + '-keys'


def _provider_routes(config):
    """Declare the exact upstream operations available to this sandbox."""
    from . import sandbox
    result = {}
    if config.get('local_model_ports'):
        result.update({local_route_id(port): local_route(port)
                       for port in sandbox.local_model_ports(config['local_model_ports'])})
    def op(method, path, query=None):
        return {'method': method, 'path': path, **({'query': query} if query else {})}
    for provider, value in config.get('hosted_connections', {}).items():
        auth = {'type': 'bearer'}
        credential_check = None
        if provider == 'openrouter':
            origin = 'https://openrouter.ai'
            prefix = '/api/v1'
            credential_check = op('GET', '/api/v1/key')
            operations = [op('POST', '/api/v1/chat/completions'), op('POST', '/api/v1/responses'),
                          op('POST', '/api/v1/messages', {'beta': ['true']}),
                          {**op('POST', '/api/v1/v1/messages', {'beta': ['true']}), 'upstreamPath': '/api/v1/messages'},
                          op('GET', '/api/v1/models'), op('GET', '/api/v1/models/user')]
        elif provider == 'together':
            origin = 'https://api.together.xyz'
            prefix = '/v1'
            operations = [op('POST', '/v1/chat/completions'), op('GET', '/v1/models')]
        elif provider == 'azure':
            endpoint = urlsplit(value['endpoint'])
            origin = f'{endpoint.scheme}://{endpoint.netloc}'
            prefix = endpoint.path.rstrip('/') or '/openai/v1'
            auth = {'type': 'header', 'header': 'api-key'}
            operations = [op('POST', prefix + '/responses', {'api-version': ['v1']}),
                          op('POST', prefix + '/chat/completions', {'api-version': ['v1']}),
                          op('GET', prefix + '/models', {'api-version': ['v1']})]
        elif provider == 'amazon-bedrock':
            region = value['region']
            suffix = 'amazonaws.com.cn' if region.startswith('cn-') else 'amazonaws.com'
            origin = f'https://bedrock-runtime.{region}.{suffix}'
            prefix = ''
            if value.get('method') == 'iam':
                auth = {'type': 'sigv4', 'region': region}
            operations = [op('POST', '/model/{model}/converse-stream')]
        else:
            raise ValueError('Unknown hosted provider.')
        result[provider] = {'origin': origin, 'auth': auth, 'operations': operations, 'basePath': prefix}
        if provider == 'openrouter':
            result[provider]['embeddingCatalogue'] = {'basePath': prefix, 'path': prefix + '/embeddings/models'}
        if credential_check:
            result[provider]['credentialCheck'] = credential_check
    for row in config.get('model_providers', []):
        route = row['manifest'].get('inference')
        if route:
            if row['name'] in result:
                raise ValueError('Duplicate inference connection: ' + row['name'])
            result[row['name']] = route
    for provider, route in result.items():
        if route.get('credentialCheck') and route['auth']['type'] in ('bearer', 'header'):
            identity = {key: route.get(key) for key in ('origin', 'auth', 'basePath', 'credentialCheck')}
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            result[provider] = {**route, 'credentialCheckId': fingerprint}
    return result


def _embedding_target(config, providers):
    selected = config.get('options', {})
    if selected.get('knowledge_embedding') != 'endpoint':
        return None, None
    base = selected['knowledge_embedding_url'].rstrip('/')
    provider = selected.get('knowledge_embedding_provider')
    if provider == '@custom':
        return base, None
    if provider:
        route = providers.get(provider, {})
        catalogue = route.get('embeddingCatalogue')
        if catalogue and base == route['origin'].rstrip('/') + catalogue['basePath']:
            return base, provider
        raise ValueError('The selected embedding provider changed. Choose the provider and model again in Document search.')
    matches = []
    for name, route in providers.items():
        if route['auth']['type'] not in ('bearer', 'header'):
            continue
        origin = route['origin'].rstrip('/')
        prefix = route.get('basePath', '').rstrip('/')
        bases = {origin + prefix}
        # Some adapters use /api for credentials and declare their v1 API
        # explicitly through its model catalogue, e.g. /api/v1/models.
        if any(op['method'] == 'GET' and op['path'] == prefix + '/v1/models'
               for op in route['operations']):
            bases.add(origin + prefix + '/v1')
        if base in bases:
            matches.append(name)
    if len(matches) > 1:
        raise ValueError('Several model connections use the same embedding URL.')
    return base, matches[0] if matches else None


def routes(config):
    providers = _provider_routes(config)
    base, name = _embedding_target(config, providers)
    if name:
        route = providers[name]
        operation = {'method': 'POST', 'path': urlsplit(base).path + '/embeddings'}
        if operation not in route['operations']:
            providers[name] = {**route, 'operations': [*route['operations'], operation]}
    return providers


def embedding_client_url(config):
    """Base URL for pi-knowledge. Only a matching companion injects credentials."""
    base, name = _embedding_target(config, _provider_routes(config))
    if name:
        return f'http://127.0.0.1:{PORT}/r/{name}' + urlsplit(base).path
    return base


def embedding_provider(config):
    return _embedding_target(config, _provider_routes(config))[1]


def embedding_batch_size(config):
    providers = _provider_routes(config)
    _, name = _embedding_target(config, providers)
    if name:
        model = config['options']['knowledge_embedding_model']
        return providers[name].get('embeddingBatchSizes', {}).get(model)
    return None


def connection_checks(config):
    return {provider: {'id': route['credentialCheckId'],
                       'target': route['origin'].rstrip('/') + route['credentialCheck']['path']}
            for provider, route in routes(config).items() if 'credentialCheckId' in route}


def write_connection_checks(config, state):
    from .sandbox_options import write_json
    write_json(Path(state) / 'runtime-config/provider-connection-checks.json', connection_checks(config))


def enabled(config):
    return bool(routes(config))


def image():
    from . import sandbox
    source = sandbox.ROOT / 'containers/inference'
    digest = hashlib.sha256(b''.join((source / f).read_bytes() for f in
        ('Dockerfile', 'package.json', 'package-lock.json', 'broker.mjs', 'credentials.mjs'))).hexdigest()[:16]
    return 'localhost/pi-sandbox-factory-inference:' + digest


def build(config):
    if not enabled(config):
        return
    from . import sandbox
    tag = image()
    if subprocess.run([config['engine'], 'image', 'inspect', tag], capture_output=True).returncode:
        result = subprocess.run([config['engine'], 'build', '-t', tag,
            str(sandbox.ROOT / 'containers/inference')], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('Could not build the inference service. Check registry access and retry.')


def check_ownership(config):
    from . import sandbox
    worker, *volumes = names(config)
    sandbox.inspect(config, worker)
    for volume in volumes:
        result = subprocess.run([config['engine'], 'volume', 'inspect', volume], capture_output=True, text=True)
        if not result.returncode and json.loads(result.stdout)[0].get('Labels', {}).get(sandbox.LABEL) != config['name']:
            raise ValueError('Refusing to manage unrelated inference volume: ' + volume)


def up(config, state):
    from . import sandbox
    from .sandbox_options import write_json
    worker, socket, keys = names(config)
    check_ownership(config)
    def volume(name):
        if subprocess.run([config['engine'], 'volume', 'inspect', name], capture_output=True).returncode:
            subprocess.run([config['engine'], 'volume', 'create', '--label', f'{sandbox.LABEL}={config["name"]}', name], check=True, capture_output=True)
    volume(socket)
    if not enabled(config):
        return
    build(config)
    # Validate with the same parser used by the service before replacing a working route.
    validation = subprocess.run([config['engine'], 'run', '--rm', '-i', '--network=none',
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', image(),
        'node', '--input-type=module', '-e',
        "import {compileRoutes} from '/app/broker.mjs';let text='';for await(const chunk of process.stdin)text+=chunk;try{compileRoutes(JSON.parse(text));}catch{process.exitCode=1;}"],
        input=json.dumps(routes(config)), text=True, capture_output=True)
    if validation.returncode:
        raise ValueError('Invalid inference routes. Check the provider endpoint and its declared operations.')
    volume(keys)
    target = Path(state) / 'inference'
    target.mkdir(exist_ok=True, mode=0o700)
    write_json(target / 'routes.json', routes(config))
    # Routes contain no keys and are readable by the mapped container user.
    (target / 'routes.json').chmod(0o644)
    identity = hashlib.sha256((json.dumps(routes(config), sort_keys=True) + image()).encode()).hexdigest()
    existing = sandbox.inspect(config, worker)
    if existing and existing.get('Config',{}).get('Labels',{}).get('io.pi-sandbox-factory.inference') != identity:
        subprocess.run([config['engine'], 'rm', '-f', worker], check=True, capture_output=True)
        existing = None
    if existing:
        sandbox.resume_existing(config, worker, existing)
    else:
        args = [config['engine'], 'run', '-d', '--name', worker, '--label', f'{sandbox.LABEL}={config["name"]}',
                '--label', 'io.pi-sandbox-factory.inference=' + identity,
                '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--read-only', '--pids-limit=-1', '--user', f'{config["uid"]}:{config["gid"]}',
                '-v', socket + ':/run/inference:z', '-v', keys + ':/private:z', '-v', str(target) + ':/config:ro,Z']
        if config['engine'] == 'podman':
            args += ['--userns=keep-id:uid=1000,gid=1000']
        if config.get('local_model_ports'):
            args += ['-v', sandbox.names(config)[2] + ':/run/egress:ro,z']
        subprocess.run(args + [image()], check=True, capture_output=True)
    for _ in range(80):
        result = subprocess.run([config['engine'], 'exec', worker, 'node', '-e',
            "const http=require('http');const r=http.get({socketPath:'/run/inference/proxy.sock',path:'/'},s=>{s.resume();s.on('end',()=>process.exit(s.statusCode===404?0:1));});r.on('error',()=>process.exit(1));r.setTimeout(1000,()=>process.exit(1));"], capture_output=True)
        if not result.returncode:
            return
        time.sleep(.25)
    raise ValueError('Inference service did not start. Check its container logs.')


def credentials(config, state, request):
    from . import sandbox
    worker = names(config)[0]
    info = sandbox.inspect(config, worker)
    if not info or not info.get('State', {}).get('Running'):
        raise ValueError('Start the inference service before configuring credentials.')
    # Kernel locks release on process exit, including interrupted imports.
    with (Path(state) / 'inference' / 'credentials.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = subprocess.run([config['engine'], 'exec', '-i', worker, 'node', '/app/credentials.mjs', '/private/credentials.json'],
                                input=json.dumps(request), text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Could not update inference credentials.')
    return json.loads(result.stdout)


def mounts(config):
    return ['-v', names(config)[1] + ':/run/inference:ro']


def down(config, remove=False):
    from . import sandbox
    check_ownership(config)
    worker = names(config)[0]
    if sandbox.inspect(config, worker):
        subprocess.run([config['engine'], 'rm', '-f', worker] if remove else [config['engine'], 'stop', worker], check=True, capture_output=True)


def delete_volumes(config):
    check_ownership(config)
    for volume in names(config)[1:]:
        if not subprocess.run([config['engine'], 'volume', 'inspect', volume], capture_output=True).returncode:
            subprocess.run([config['engine'], 'volume', 'rm', volume], check=True, capture_output=True)


def client_url(provider, connection):
    prefix = {'openrouter': '/api/v1', 'together': '/v1', 'amazon-bedrock': ''}.get(provider)
    if provider == 'azure':
        prefix = urlsplit(connection['endpoint']).path.rstrip('/') or '/openai/v1'
    return f'http://127.0.0.1:{PORT}/r/{provider}' + prefix
