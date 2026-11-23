"""Authenticate the official Claude clients without filtering their native protocol."""
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from mitmproxy import http, options
from mitmproxy.tools.dump import DumpMaster

from claude_auth import AuthStore
from codex_auth import Redactor
from codex_auth_proxy import replace_headers

PUBLIC_HOST = 'factory-claude.internal'
RESOURCE_HOSTS = frozenset(('api.anthropic.com', 'claude.ai'))
OAUTH_ENDPOINTS = frozenset((('platform.claude.com', '/v1/oauth/token'),
                            ('console.anthropic.com', '/v1/oauth/token')))
NATIVE_HOSTS = RESOURCE_HOSTS | {host for host, _ in OAUTH_ENDPOINTS} | {
    'claude.com', 'code.claude.com', 'downloads.claude.ai', 'storage.googleapis.com',
    'statsig.anthropic.com', 'sentry.io',
}


class SubscriptionProxy:
    def __init__(self, store, restricted=True):
        self.store = store
        self.restricted = restricted
        self.refresh_lock = asyncio.Lock()

    def route(self, flow):
        native = flow.request.host in NATIVE_HOSTS
        flow.server_conn.via = ('http', ('127.0.0.1', 3128 if native else 3129)) if native or self.restricted else None

    def http_connect(self, flow):
        self.route(flow)

    def requestheaders(self, flow):
        self.route(flow)
        if flow.request.host in RESOURCE_HOSTS | {host for host, _ in OAUTH_ENDPOINTS}:
            flow.request.headers['accept-encoding'] = 'identity'

    async def request(self, flow):
        request = flow.request
        if request.host == PUBLIC_HOST:
            if request.method == 'GET' and request.path == '/auth':
                try:
                    flow.response = http.Response.make(200, json.dumps(self.store.public()),
                        {'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
                except (OSError, ValueError) as error:
                    self.fail(flow, error)
            elif request.method == 'POST' and request.path == '/logout':
                try:
                    self.store.clear()
                    flow.response = http.Response.make(200, json.dumps({'signed_out': True}),
                        {'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
                except OSError as error:
                    self.fail(flow, error)
            else:
                flow.response = http.Response.make(404, 'No such Claude credential-service operation.')
            return
        trusted = request.scheme == 'https' and request.port == 443
        oauth = trusted and (request.host, request.path.split('?', 1)[0]) in OAUTH_ENDPOINTS
        if not trusted or request.host not in RESOURCE_HOSTS and not oauth:
            return
        try:
            if request.path.split('?', 1)[0] == '/api/oauth/claude_cli/create_api_key':
                flow.response = http.Response.make(403, json.dumps({'error': {
                    'type': 'permission_error', 'message': 'This sandbox uses a Claude subscription. Creating an Anthropic Console API key is unavailable; sign in with your Claude subscription instead.'}}),
                    {'Content-Type': 'application/json'})
                return
            flow.metadata['factory_oauth'] = oauth
            if oauth:
                if 'application/json' in request.headers.get('content-type', ''):
                    payload = json.loads(request.content)
                    encode = lambda value: json.dumps(value).encode()
                else:
                    payload = dict(parse_qsl(request.get_text(), keep_blank_values=True))
                    encode = lambda value: urlencode(value).encode()
                if not isinstance(payload, dict):
                    raise ValueError('Claude OAuth token request must contain a JSON object.')
                grant = payload.get('grant_type')
                if grant not in ('authorization_code', 'refresh_token'):
                    flow.response = http.Response.make(400, json.dumps({'error': 'unsupported_grant_type',
                        'error_description': f'Claude subscription sign-in requires authorization_code or refresh_token, received {grant!r}.'}),
                        {'Content-Type': 'application/json'})
                    return
                flow.metadata['factory_new_login'] = grant == 'authorization_code'
                flow.metadata['factory_auth_generation'] = self.store.public()['generation']
                if grant == 'refresh_token':
                    await self.refresh_lock.acquire()
                    flow.metadata['factory_refresh_lock'] = True
                    replacements = self.store.substitutions()
                    value = payload.get('refresh_token')
                    if isinstance(value, str):
                        payload['refresh_token'] = replacements.get(value, value)
                request.content = encode(payload)
            replacements = self.store.substitutions()
            flow.metadata['factory_redactions'] = replacements
            authorization = request.headers.get('authorization', '')
            if authorization.startswith('Bearer '):
                value = authorization[len('Bearer '):]
                if value in replacements:
                    request.headers['authorization'] = 'Bearer ' + replacements[value]
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.release(flow)
            self.fail(flow, error)

    def responseheaders(self, flow):
        replacements = flow.metadata.get('factory_redactions', {})
        replace_headers(flow.response.headers, {real: mask for mask, real in replacements.items()})
        if flow.metadata.get('factory_oauth'):
            return
        if flow.response.headers.get('content-encoding', 'identity') != 'identity':
            return
        if replacements:
            flow.response.headers.pop('content-length', None)
            flow.response.stream = Redactor(replacements)
        else:
            flow.response.stream = True

    def response(self, flow):
        if flow.metadata.get('factory_oauth'):
            try:
                if 200 <= flow.response.status_code < 300:
                    if flow.metadata.get('factory_auth_generation') != self.store.public()['generation']:
                        raise ValueError('Claude sign-in changed while this token request was running. Sign in again.')
                    payload = self.store.capture(json.loads(flow.response.content),
                        new_login=flow.metadata.get('factory_new_login', False))
                    redact = Redactor(self.store.substitutions())
                    flow.response.content = redact(json.dumps(payload).encode()) + redact(b'')
                    replace_headers(flow.response.headers,
                        {real: mask for mask, real in self.store.substitutions().items()})
                else:
                    redact = Redactor(flow.metadata.get('factory_redactions', {}))
                    flow.response.content = redact(flow.response.content) + redact(b'')
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.fail(flow, error)
            finally:
                self.release(flow)
        elif flow.response.raw_content is not None:
            # A server may ignore accept-encoding: identity. Buffer and decode
            # that exceptional response before removing credentials; ordinary
            # event streams retain incremental delivery.
            flow.response.decode(strict=False)
            redact = Redactor(flow.metadata.get('factory_redactions', {}))
            flow.response.content = redact(flow.response.content) + redact(b'')
        if flow.response.status_code >= 400:
            logging.warning('Claude upstream HTTP %s: %s %s%s; request %s',
                flow.response.status_code, flow.request.method, flow.request.host,
                flow.request.path.split('?', 1)[0], flow.id)

    def websocket_message(self, flow):
        message = flow.websocket.messages[-1]
        if not message.from_client:
            redact = Redactor(flow.metadata.get('factory_redactions', {}))
            message.content = redact(message.content) + redact(b'')

    def release(self, flow):
        if flow.metadata.pop('factory_refresh_lock', False):
            self.refresh_lock.release()

    def error(self, flow):
        self.release(flow)
        logging.error('Claude transport failure: %s %s%s; request %s: %s',
            flow.request.method, flow.request.host, flow.request.path.split('?', 1)[0], flow.id, flow.error)

    @staticmethod
    def fail(flow, error):
        # JSON parse exceptions do not include payloads; never log tokens.
        detail = f'Claude credential service: {type(error).__name__}: {error}'
        logging.error('%s; request %s', detail, flow.id)
        flow.response = http.Response.make(502, json.dumps({'error': {'type': 'api_error',
            'message': detail}, 'request_id': flow.id}), {'Content-Type': 'application/json'})


async def main():
    home = Path.home()
    settings = json.loads(Path('/run/primary-input/settings.json').read_text())
    opts = options.Options(listen_host='127.0.0.1', listen_port=3130,
        confdir=str(home / '.claude-proxy'), mode=['upstream:http://127.0.0.1:3128'])
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    master.options.update(connection_strategy='lazy', upstream_cert=False)
    master.addons.add(SubscriptionProxy(AuthStore(home), settings.get('network_mode', 'allowlist') == 'allowlist'))

    async def publish():
        source = home / '.claude-proxy/mitmproxy-ca-cert.pem'
        while not source.exists():
            await asyncio.sleep(.05)
        Path('/run/primary-editor/claude-ca.pem').write_bytes(source.read_bytes())
    task = asyncio.create_task(publish())
    try:
        await master.run()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    os.umask(0o077)
    asyncio.run(main())
