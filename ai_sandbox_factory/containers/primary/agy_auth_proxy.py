"""Authenticate unmodified Antigravity clients through the credential companion."""
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from mitmproxy import http, options
from mitmproxy.tools.dump import DumpMaster

from agy_auth import AuthStore
from codex_auth import Redactor
from codex_auth_proxy import SubscriptionProxy, replace_headers

NETWORK = json.loads(Path(__file__).with_name('agy_network.json').read_text())
AUTH_HOST = 'oauth2.googleapis.com'
PUBLIC_HOST = 'factory-agy.internal'
RESOURCE_HOSTS = frozenset(NETWORK['credential_hosts'])


def credential_route(request):
    if request.scheme != 'https' or request.port != 443:
        return False
    path = request.path.split('?', 1)[0]
    if request.host == AUTH_HOST:
        return path == '/token'
    if request.host == 'www.googleapis.com':
        return path in ('/oauth2/v2/userinfo', '/oauth2/v3/userinfo')
    if request.host == 'play.googleapis.com':
        return path == '/log'
    if request.host == 'aicode.googleapis.com':
        return path.startswith(('/v1alpha:', '/v1alpha/'))
    return request.host in ('daily-cloudcode-pa.googleapis.com', 'cloudcode-pa.googleapis.com') and path.startswith('/v1internal:')


class AntigravityProxy(SubscriptionProxy):
    def route(self, flow):
        if flow.request.host.lower() in NETWORK['transport_hosts']:
            flow.server_conn.via = ('http', ('127.0.0.1', 3128))
        else:
            flow.server_conn.via = ('http', ('127.0.0.1', 3129)) if self.restricted else None

    def requestheaders(self, flow):
        self.route(flow)
        if flow.request.host in RESOURCE_HOSTS:
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
            else:
                flow.response = http.Response.make(404, 'No such Antigravity credential-service operation.')
            return
        if not credential_route(request):
            return
        try:
            oauth = request.host == AUTH_HOST and request.path.split('?', 1)[0] == '/token'
            flow.metadata['factory_oauth'] = oauth
            if oauth:
                encoded_json = 'application/json' in request.headers.get('content-type', '')
                payload = json.loads(request.content) if encoded_json else dict(parse_qsl(request.get_text(), keep_blank_values=True))
                if not isinstance(payload, dict):
                    raise ValueError('Antigravity OAuth token request must be a JSON object or form fields.')
                grant = payload.get('grant_type')
                if grant not in ('authorization_code', 'refresh_token'):
                    flow.response = http.Response.make(400, json.dumps({'error': 'unsupported_grant_type',
                        'error_description': f'Antigravity subscription sign-in cannot process OAuth grant {grant!r}; use authorization_code or refresh_token.'}),
                        {'Content-Type': 'application/json'})
                    return
                flow.metadata['factory_new_login'] = grant == 'authorization_code'
                if grant == 'refresh_token':
                    if not isinstance(payload.get('refresh_token'), str) or not payload['refresh_token']:
                        raise ValueError('Antigravity OAuth refresh_token must be a nonempty string; sign in again if the native profile is damaged.')
                    await self.refresh_lock.acquire()
                    flow.metadata['factory_refresh_lock'] = True
                    replacements = self.store.substitutions()
                    payload['refresh_token'] = replacements.get(payload.get('refresh_token'), payload.get('refresh_token'))
                request.content = json.dumps(payload).encode() if encoded_json else urlencode(payload).encode()
            replacements = self.store.substitutions()
            flow.metadata['factory_redactions'] = replacements
            authorization = request.headers.get('authorization', '')
            if authorization.startswith('Bearer '):
                token = authorization[len('Bearer '):]
                if token in replacements:
                    request.headers['authorization'] = 'Bearer ' + replacements[token]
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.release(flow)
            self.fail(flow, error)

    def response(self, flow):
        if flow.metadata.get('factory_oauth'):
            try:
                if 200 <= flow.response.status_code < 300:
                    payload = self.store.capture(json.loads(flow.response.content),
                        new_login=flow.metadata.get('factory_new_login', False))
                    content = json.dumps(payload).encode()
                    # OAuth diagnostics may echo issued tokens in nested fields,
                    # beyond the standard top-level credential field names.
                    for replacements in (flow.metadata.get('factory_redactions', {}), self.store.substitutions()):
                        redact = Redactor(replacements)
                        content = redact(content) + redact(b'')
                    flow.response.content = content
                    replace_headers(flow.response.headers,
                        {real: mask for mask, real in self.store.substitutions().items()})
                else:
                    redact = Redactor(flow.metadata.get('factory_redactions', {}))
                    flow.response.content = redact(flow.response.content) + redact(b'')
            except (OSError, ValueError, KeyError) as error:
                self.fail(flow, error)
            finally:
                self.release(flow)
        elif flow.response.content is not None:
            redact = Redactor(flow.metadata.get('factory_redactions', {}))
            flow.response.content = redact(flow.response.content) + redact(b'')
        if flow.response.status_code >= 400:
            logging.warning('Antigravity upstream HTTP %s: %s %s%s; request %s', flow.response.status_code,
                flow.request.method, flow.request.host, flow.request.path.split('?', 1)[0], flow.id)

    def error(self, flow):
        self.release(flow)
        logging.error('Antigravity transport failure: %s %s%s; request %s: %s',
            flow.request.method, flow.request.host, flow.request.path.split('?', 1)[0], flow.id, flow.error)

    @staticmethod
    def fail(flow, error):
        detail = f'Antigravity credential service: {type(error).__name__}: {error}'
        logging.error('%s; request %s', detail, flow.id)
        flow.response = http.Response.make(502, json.dumps({'error': detail, 'request_id': flow.id}),
            {'Content-Type': 'application/json'})


async def main():
    home = Path.home()
    settings = json.loads(Path('/run/primary-input/settings.json').read_text())
    opts = options.Options(listen_host='127.0.0.1', listen_port=3130,
        confdir=str(home / '.agy-proxy'), mode=['upstream:http://127.0.0.1:3128'])
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    master.options.update(connection_strategy='lazy', upstream_cert=False)
    master.addons.add(AntigravityProxy(AuthStore(home), settings.get('network_mode', 'allowlist') == 'allowlist'))
    async def publish():
        source = home / '.agy-proxy/mitmproxy-ca-cert.pem'
        while not source.exists():
            await asyncio.sleep(.05)
        Path('/run/primary-editor/agy-ca.pem').write_bytes(source.read_bytes())
    task = asyncio.create_task(publish())
    try:
        await master.run()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    os.umask(0o077)
    asyncio.run(main())
