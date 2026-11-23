"""Authenticate native Codex traffic without exposing subscription tokens.

mitmproxy owns TLS, HTTP/2, WebSockets and forwarding. This addon owns only
subscription token substitution; it does not interpret Codex's editor protocol.
"""
import asyncio
import json
import logging
import os
import re
import ssl
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy import certs, http, options
from mitmproxy.tools.dump import DumpMaster

from codex_auth import AuthStore, Redactor
from codex_connect import ConnectRelay

AUTH_HOST = 'auth.openai.com'
NETWORK = json.loads(Path(__file__).with_name('codex_network.json').read_text())
RESOURCE_HOSTS = frozenset(NETWORK['credential_hosts']) - {AUTH_HOST}
TRANSPORT_HOSTS = frozenset(NETWORK['transport_hosts'])
TRANSPORT_DOMAINS = tuple(NETWORK['transport_domains'])
PUBLIC_HOST = 'factory-codex.internal'
CONNECT_TARGET = ('codex-portals.api.openai.com', 443)
CONNECT_ADDRESS = ('127.0.0.1', 3132)


def native_transport(host):
    return host in TRANSPORT_HOSTS or any(host == domain or host.endswith('.' + domain)
                                        for domain in TRANSPORT_DOMAINS)


def replace_headers(headers, replacements):
    # Preserve repeated fields (notably Set-Cookie) and their original order.
    headers.fields = tuple((name, replace_bytes(value, replacements))
                           for name, value in headers.fields)


def replace_bytes(value, replacements):
    for source, target in replacements.items():
        value = value.replace(source.encode(), target.encode())
    return value


class SubscriptionProxy:
    def __init__(self, store, restricted=True, *, connect_address=None, connect_target=CONNECT_TARGET):
        self.store = store
        self.restricted = restricted
        self.refresh_lock = asyncio.Lock()
        self.connect_address = connect_address
        self.connect_target = connect_target

    def route(self, flow):
        host = flow.request.host.lower()
        if native_transport(host):
            flow.server_conn.via = ('http', ('127.0.0.1', 3128))
        else:
            flow.server_conn.via = ('http', ('127.0.0.1', 3129)) if self.restricted else None

    def http_connect(self, flow):
        if self.connect_address and (flow.request.host, flow.request.port) == self.connect_target:
            flow.server_conn.via = ('http', self.connect_address)
        else:
            self.route(flow)

    def requestheaders(self, flow):
        self.route(flow)
        if flow.request.host in RESOURCE_HOSTS | {AUTH_HOST}:
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
                flow.response = http.Response.make(404, 'No such Codex credential-service operation.')
            return
        # Match the actual TLS destination, not a caller-supplied header. Never
        # inject on HTTP, alternate ports, redirects to other hosts, or lookalikes.
        trusted = request.scheme == 'https' and request.port == 443 and request.host in RESOURCE_HOSTS | {AUTH_HOST}
        if not trusted:
            return
        try:
            oauth = request.host == AUTH_HOST and request.path.split('?', 1)[0] == '/oauth/token'
            flow.metadata['factory_oauth'] = oauth
            if oauth:
                if 'application/json' in request.headers.get('content-type', ''):
                    payload = json.loads(request.content)
                    encode = lambda value: json.dumps(value).encode()
                else:
                    payload = dict(parse_qsl(request.get_text(), keep_blank_values=True))
                    encode = lambda value: urlencode(value).encode()
                grant = payload.get('grant_type')
                if grant not in ('authorization_code', 'refresh_token'):
                    # Native browser sign-in optionally obtains an API-key style
                    # token, then continues subscription login if that
                    # exchange fails. Never create or store that second credential.
                    if grant == 'urn:ietf:params:oauth:grant-type:token-exchange' and payload.get('requested_token') == 'openai-api-key':
                        detail = ('The optional OpenAI API-key exchange is disabled for this ChatGPT subscription sign-in. '
                                  'Continue signing in with ChatGPT; no API key is required.')
                    else:
                        detail = (f'ChatGPT subscription sign-in cannot process OAuth grant {grant!r}. '
                                  'Use an authorization_code or refresh_token grant.')
                    flow.response = http.Response.make(400, json.dumps({'error': 'unsupported_grant_type',
                        'error_description': detail}), {'Content-Type': 'application/json'})
                    return
                flow.metadata['factory_subscription_grant'] = grant
                flow.metadata['factory_new_login'] = grant == 'authorization_code'
                if payload.get('grant_type') == 'refresh_token':
                    await self.refresh_lock.acquire()
                    flow.metadata['factory_refresh_lock'] = True
                replacements = self.store.substitutions()
                # Native OAuth grants put credentials in these named token
                # fields. Other values may be ordinary caller-supplied text.
                field = 'refresh_token' if grant == 'refresh_token' else None
                if field and isinstance(payload.get(field), str):
                    payload[field] = replacements.get(payload[field], payload[field])
                request.content = encode(payload)
            replacements = self.store.substitutions()
            flow.metadata['factory_redactions'] = replacements
            authorization = request.headers.get('authorization', '')
            if authorization.startswith('Bearer '):
                token = authorization[len('Bearer '):]
                if token in replacements:
                    request.headers['authorization'] = 'Bearer ' + replacements[token]
        except (OSError, ValueError, KeyError) as error:
            self.release(flow)
            self.fail(flow, error)

    def responseheaders(self, flow):
        replacements = flow.metadata.get('factory_redactions', {})
        replace_headers(flow.response.headers, {real: mask for mask, real in replacements.items()})
        if flow.metadata.get('factory_oauth'):
            return  # OAuth responses must be masked before any byte is delivered.
        if flow.response.headers.get('content-encoding', 'identity') != 'identity':
            return
        if replacements:
            # Replacement can change body length. Streaming retains only a
            # possible token prefix, so first-token delivery is not buffered.
            flow.response.headers.pop('content-length', None)
            flow.response.stream = Redactor(replacements)
        else:
            flow.response.stream = True

    def response(self, flow):
        if flow.metadata.get('factory_oauth'):
            try:
                if 200 <= flow.response.status_code < 300:
                    if flow.metadata.get('factory_subscription_grant') not in ('authorization_code', 'refresh_token'):
                        raise ValueError('OAuth response is not an authorized ChatGPT subscription token grant.')
                    payload = self.store.capture(json.loads(flow.response.content),
                        new_login=flow.metadata.get('factory_new_login', False))
                    flow.response.content = json.dumps(payload).encode()
                    replace_headers(flow.response.headers,
                        {real: mask for mask, real in self.store.substitutions().items()})
                else:
                    redactor = Redactor(flow.metadata.get('factory_redactions', {}))
                    flow.response.content = redactor(flow.response.content) + redactor(b'')
            except (OSError, ValueError, KeyError) as error:
                self.fail(flow, error)
            finally:
                self.release(flow)
        elif flow.response.content is not None:
            redact = Redactor(flow.metadata.get('factory_redactions', {}))
            flow.response.content = redact(flow.response.content) + redact(b'')
        if flow.response.status_code >= 400:
            logging.warning('Codex upstream HTTP %s: %s %s%s; request %s',
                flow.response.status_code, flow.request.method, flow.request.host,
                flow.request.path.split('?', 1)[0], flow.id)

    def websocket_message(self, flow):
        message = flow.websocket.messages[-1]
        if not message.from_client:
            redact = Redactor(flow.metadata.get('factory_redactions', {}))
            message.content = redact(message.content) + redact(b'')

    def error(self, flow):
        self.release(flow)
        logging.error('Codex transport failure: %s %s%s; request %s: %s',
            flow.request.method, flow.request.host, flow.request.path.split('?', 1)[0], flow.id, flow.error)

    def release(self, flow):
        if flow.metadata.pop('factory_refresh_lock', False):
            self.refresh_lock.release()

    @staticmethod
    def fail(flow, error):
        # Storage/parser errors describe the operation, never dump token payloads.
        detail = f'Codex credential service: {type(error).__name__}: {error}'
        logging.error('%s; request %s', detail, flow.id)
        flow.response = http.Response.make(502, json.dumps({'error': detail, 'request_id': flow.id}),
            {'Content-Type': 'application/json'})


def connect_ignore_hosts(target=CONNECT_TARGET, address=CONNECT_ADDRESS):
    # mitmproxy checks the rewritten destination too. Keep both entries exact;
    # this must never turn an arbitrary user destination into a raw TLS bypass.
    return [f'^{re.escape(host)}:{port}$' for host, port in (target, address)]


def connect_tls_context(directory, host=CONNECT_TARGET[0]):
    store = certs.CertStore.from_store(directory, 'mitmproxy', 2048)
    entry = store.get_cert(host, [x509.DNSName(host)])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # SSLContext retains the key after loading it. Transient key files stay in
    # the private companion directory and are removed before accepting clients.
    with tempfile.TemporaryDirectory(prefix='connect-', dir=directory) as temporary:
        certificate = Path(temporary) / 'certificate.pem'
        key = Path(temporary) / 'key.pem'
        certificate.write_bytes(entry.cert.to_pem() + store.default_ca.to_pem())
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(entry.privatekey.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        context.load_cert_chain(certificate, key)
    return context


async def main():
    home = Path.home()
    settings = json.loads(Path('/run/primary-input/settings.json').read_text())
    opts = options.Options(listen_host='127.0.0.1', listen_port=3130,
        confdir=str(home / '.codex-proxy'), mode=['upstream:http://127.0.0.1:3128'])
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    master.options.update(connection_strategy='lazy', upstream_cert=False)
    store = AuthStore(home)
    relay = ConnectRelay(CONNECT_TARGET[0], store.substitutions)
    context = connect_tls_context(home / '.codex-proxy')
    server = await relay.start_proxy(context, *CONNECT_ADDRESS)
    master.options.update(ignore_hosts=connect_ignore_hosts())
    master.addons.add(SubscriptionProxy(store, settings.get('network_mode', 'allowlist') == 'allowlist',
                                      connect_address=CONNECT_ADDRESS))
    # The private CA key stays in this container. Only its public certificate
    # and the proxy socket are shared with the workspace.
    async def publish():
        source = home / '.codex-proxy/mitmproxy-ca-cert.pem'
        while not source.exists():
            await asyncio.sleep(.05)
        Path('/run/primary-editor/codex-ca.pem').write_bytes(source.read_bytes())
    task = asyncio.create_task(publish())
    try:
        await master.run()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await relay.close()


if __name__ == '__main__':
    os.umask(0o077)
    asyncio.run(main())
