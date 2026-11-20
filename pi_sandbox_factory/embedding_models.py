"""Read text embedding model IDs from an enabled provider's fixed catalogue."""
import http.client
import json
import re
import subprocess
from urllib.parse import urlencode, urlsplit

from . import inference, provider_credentials, registry
from .sandbox_options import embedding_model


def validate_catalogue(route):
    catalogue = route['embeddingCatalogue']
    if not isinstance(catalogue, dict) or set(catalogue) - {'basePath', 'path', 'query', 'label'}:
        raise ValueError('Declare a fixed embedding catalogue path and API base path.')
    label = catalogue.get('label')
    if label is not None and (not isinstance(label, str) or not label.isprintable() or not label.strip()):
        raise ValueError('Use a single-line embedding provider label.')
    base = catalogue.get('basePath')
    path = catalogue.get('path')
    scope = route.get('basePath', '').rstrip('/')
    if (not isinstance(base, str) or not re.fullmatch(r'(/[a-zA-Z0-9_-]+)*', base)
            or not (base == scope or base.startswith(scope + '/'))
            or not isinstance(path, str) or not re.fullmatch(r'(/[a-zA-Z0-9_-]+)+', path)
            or not path.startswith(base + '/')):
        raise ValueError('The embedding catalogue must be a fixed path under the provider API base.')
    query = catalogue.get('query', {})
    if not isinstance(query, dict) or any(
        not re.fullmatch(r'[a-z][a-z0-9_-]*', key) or not isinstance(value, str)
        or not re.fullmatch(r'[A-Za-z0-9_-]+', value) for key, value in query.items()
    ):
        raise ValueError('Embedding catalogue filters must use fixed query values.')
    if not any(op.get('method') == 'GET' and op.get('path') == path and not op.get('upstreamPath')
               and all(value in op.get('query', {}).get(key, []) for key, value in query.items())
               for op in route.get('operations', [])):
        raise ValueError('Declare the embedding catalogue GET operation and its allowed query values.')


def _connections(config):
    rows = config.get('model_providers', [])
    if isinstance(rows, dict):
        rows = [{'name': name, 'manifest': registry.manifest('provider', registry.resolve('provider', name))}
                for name in rows if name != 'local']
    hosted = {**config.get('hosted_connections', {}), **config.get('_hosted_changes', {})}
    routes = inference._provider_routes({'model_providers': rows,
                                        'hosted_connections': {'openrouter': {}} if 'openrouter' in hosted else {}})
    labels = {row['name']: row['manifest'].get('description') or row['name'] for row in rows}
    if 'openrouter' in hosted:
        labels['openrouter'] = 'OpenRouter'
    return {name: (route, route['embeddingCatalogue'].get('label', labels[name]))
            for name, route in routes.items() if route.get('embeddingCatalogue')}


def sources(config):
    return {name: {'label': label, 'url': route['origin'].rstrip('/') + route['embeddingCatalogue']['basePath']}
            for name, (route, label) in _connections(config).items()}


def _request_path(route):
    spec = route['embeddingCatalogue']
    query = urlencode(spec.get('query', {}))
    return spec['path'] + ('?' + query if query else '')


def _direct(route, key):
    origin = urlsplit(route['origin'])
    auth = route['auth']
    headers = {}
    if key:
        key = provider_credentials.validate_key(key)
        headers = {'Authorization': 'Bearer ' + key} if auth['type'] == 'bearer' else {auth['header']: key}
    # HTTPSConnection performs one request. A redirect can never forward a credential.
    connection = http.client.HTTPSConnection(origin.hostname, origin.port, timeout=20)
    try:
        connection.request('GET', _request_path(route), headers=headers)
        response = connection.getresponse()
        return response.status, response.read() if response.status == 200 else b''
    except (OSError, http.client.HTTPException):
        raise ValueError('Could not connect to the embedding model catalogue.') from None
    finally:
        connection.close()


def _managed(config, name, route):
    from . import sandbox
    worker = inference.names(config)[0]
    info = sandbox.inspect(config, worker)
    if not info or not info.get('State', {}).get('Running'):
        raise ValueError('Start the sandbox to load embedding models with its saved key.')
    script = """
const http=require('node:http');
const req=http.get({socketPath:'/run/inference/proxy.sock',path:process.argv[1]},res=>{
  process.stdout.write(res.statusCode+'\\n');
  if(res.statusCode===200)res.pipe(process.stdout);else res.resume();
});
req.on('error',()=>process.exit(1));
"""
    path = '/r/' + name + _request_path(route)
    try:
        reply = subprocess.run([config['engine'], 'exec', worker, 'node', '-e', script, path],
                               capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise ValueError('The embedding model catalogue timed out. Try again.') from None
    status, _, data = reply.stdout.partition(b'\n')
    if reply.returncode or not status.isdigit():
        raise ValueError('Could not reach the sandbox model connection.')
    return int(status), data


def catalogue(config, provider, *, choice=None, saved_config=None):
    selected = _connections(config).get(provider)
    if not selected:
        raise ValueError('Choose a configured embedding provider.')
    route, _ = selected
    if provider == 'openrouter' and 'openrouter' in {**config.get('hosted_connections', {}), **config.get('_hosted_changes', {})}:
        # OpenRouter publishes the same embedding catalogue without account credentials.
        status, body = _direct(route, None)
    elif choice:
        key = choice.get('key')
        if choice.get('ref'):
            key = provider_credentials.read(choice['ref'], provider_credentials.scope(provider, route))
        if not key:
            raise ValueError('Choose a provider key before loading its embedding models.')
        status, body = _direct(route, key)
    elif saved_config and _connections(saved_config).get(provider, (None,))[0] == route:
        status, body = _managed(saved_config, provider, route)
    else:
        raise ValueError('Choose a provider key before loading its embedding models.')
    if status in (401, 403):
        raise ValueError('The provider rejected access to its embedding models. Check the configured key.')
    if status != 200:
        raise ValueError(f'The embedding catalogue returned HTTP {status}. Check the provider connection.')
    try:
        data = json.loads(body)['data']
        if not isinstance(data, list):
            raise TypeError
        models = set()
        for row in data:
            architecture = row.get('architecture') or {}
            if 'input_modalities' in architecture and 'text' not in architecture['input_modalities']:
                continue
            if 'output_modalities' in architecture and 'embeddings' not in architecture['output_modalities']:
                continue
            models.add(embedding_model(row['id']))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError('The provider returned an invalid embedding model catalogue.') from None
    return sorted(models)
