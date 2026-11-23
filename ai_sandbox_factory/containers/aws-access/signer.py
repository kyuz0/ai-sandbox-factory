"""Sign SDK requests on a sandbox-specific Unix socket using only its STS session."""

from collections import Counter
import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import shutil
import socketserver
import tempfile
import threading
from xml.etree import ElementTree
from urllib.parse import parse_qsl, urlsplit

from receive import ADDRESS

from botocore.auth import S3SigV4Auth, SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import Session

SOCKET = Path('/run/aws-access/request.sock')
SCOPE = re.compile(r'AWS4-HMAC-SHA256 Credential=pi-sandbox/[0-9]{8}/([a-z0-9-]+)/([a-z0-9-]+)/aws4_request,')


def catalogue():
    sdk = Session()
    rows = {}
    for name in sdk.get_available_services():
        model = sdk.get_service_model(name)
        rows[name] = [model.endpoint_prefix, model.signing_name,
                      str(model.service_id).lower().replace(' ', '_')]
    return rows


def client_config(services, region):
    lines = ['[default]', 'region = ' + region, 'services = factory', 's3 =', '    addressing_style = path',
             '', '[services factory]']
    counts = Counter(row[2] for row in services.values())
    for name, (_, _, key) in sorted(services.items()):
        if counts[key] != 1:
            continue  # An ambiguous CLI identifier cannot select a unique endpoint.
        lines.extend([key + ' =', '    endpoint_url = http://aws-access.internal:19432/' + name])
    return '\n'.join(lines) + '\n'


class Requests(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Paths, headers and bodies may contain user data.

    def handle_aws(self):
        try:
            self.forward()
        except (ValueError, KeyError, FileNotFoundError):
            self.send_error(403, 'AWS access is unavailable for this request')
        except (OSError, http.client.HTTPException):
            if not self.wfile.closed:
                self.close_connection = True

    def aws_error(self, reply):
        # A signing diagnostic may echo a secret header. Preserve only known
        # service error codes and never relay the diagnostic text or request.
        payload = reply.read()
        is_json = 'json' in (reply.getheader('Content-Type') or '').lower()
        code = reply.getheader('x-amzn-errortype', '').split(':')[0]
        if not code:
            try:
                if is_json:
                    values = json.loads(payload)
                    code = str(values.get('__type') or values.get('code', '')).rsplit('#', 1)[-1]
                else:
                    code = ElementTree.fromstring(payload).findtext('.//{*}Code', '')
            except (ValueError, AttributeError, ElementTree.ParseError):
                pass
        if code not in {'AccessDeniedException', 'UnauthorizedOperation', 'AccessDenied',
                        'ExpiredToken', 'ExpiredTokenException', 'ThrottlingException',
                        'ResourceNotFoundException', 'ValidationException', 'InvalidClientTokenId'}:
            code = 'AccessDenied' if reply.status == 403 else 'AWSRequestFailed'
        if is_json:
            message = json.dumps({'__type': code, 'message': code}).encode()
        else:
            message = f'<Response><Errors><Error><Code>{code}</Code><Message>{code}</Message></Error></Errors></Response>'.encode()
        self.send_response(reply.status)
        self.send_header('Content-Type', 'application/json' if is_json else 'application/xml')
        self.send_header('x-amzn-errortype', code)
        self.send_header('Content-Length', str(len(message)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(message)

    def forward(self):
        scope = SCOPE.match(self.headers.get('Authorization', ''))
        match = re.fullmatch(r'/([a-z0-9-]+)(/[^#]*)?', self.path)
        if not scope or not match or match[1] == 'sts' or self.headers.get('Transfer-Encoding'):
            raise ValueError('Unsupported request')
        region, signing_name = scope.groups()
        session = self.server.session
        if not session:
            raise ValueError('AWS session unavailable')
        if region != session['region']:
            raise ValueError('Unexpected region')
        endpoint_prefix, _, _ = self.server.services[match[1]]
        endpoint = self.server.resolver.construct_endpoint(endpoint_prefix, region)
        if not endpoint or endpoint.get('hostname') is None:
            raise ValueError('No standard regional endpoint')
        host = endpoint['hostname']
        service = endpoint.get('credentialScope', {}).get('service', self.server.services[match[1]][1])
        if service != signing_name or not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
            raise ValueError('Unexpected signing scope')
        # Only the SDK catalogue selects the upstream host. Never accept a client URL or a redirect.
        path = match[2] or '/'
        query = parse_qsl(urlsplit(path).query, keep_blank_values=True)
        if any(key.lower() in ('awsaccesskeyid', 'signature') or key.lower().startswith('x-amz-') for key, _ in query):
            raise ValueError('Use the AWS CLI with a regular signed request.')
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or len(self.headers.get_all('Content-Length', [])) > 1:
            raise ValueError('Invalid body length')
        headers = {k:v for k,v in self.headers.items() if k.lower() in
                   ('content-type','content-encoding','accept','range','if-match','if-none-match') or
                   k.lower().startswith('x-amz-')}
        for key in list(headers):
            if key.lower() in ('x-amz-security-token', 'x-amz-date','x-amz-content-sha256'):
                del headers[key]
        if 'aws-chunked' in self.headers.get('Content-Encoding', '').lower():
            self.send_error(501, 'Chunk-signed AWS uploads are unsupported')
            return
        headers['Host'] = host
        keys = session['credentials']
        creds = Credentials(keys['AccessKeyId'], keys['SecretAccessKey'], keys['SessionToken'])
        auth = S3SigV4Auth if service == 's3' else SigV4Auth
        with tempfile.TemporaryFile() as body:
            remaining = length
            while remaining:
                part = self.rfile.read(min(remaining, 65536))
                if not part:
                    raise ValueError('Incomplete request')
                body.write(part)
                remaining -= len(part)
            body.seek(0)
            req = AWSRequest(method=self.command, url='https://' + host + path, headers=headers, data=body)
            auth(creds, service, endpoint.get('credentialScope', {}).get('region', region)).add_auth(req)
            upstream = http.client.HTTPSConnection(host)
            try:
                body.seek(0)
                outgoing = dict(req.headers)
                outgoing['Content-Length'] = str(length)
                upstream.request(self.command, path, body=body, headers=outgoing)
                reply = upstream.getresponse()
                if reply.status >= 300:
                    # Upstream signature diagnostics can include signed headers.
                    self.aws_error(reply)
                    return
                self.send_response(reply.status)
                for k,v in reply.getheaders():
                    if k.lower() in ('content-type','content-length','content-encoding','etag','last-modified',
                                      'x-amzn-errortype','x-amz-request-id','x-amzn-requestid','content-range'):
                        self.send_header(k,v)
                self.end_headers()
                if self.command != 'HEAD':
                    shutil.copyfileobj(reply,self.wfile)
            finally:
                upstream.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = handle_aws


if __name__ == '__main__':
    import sys
    if '--build-catalogue' in sys.argv:
        Path('/app/services.json').write_text(json.dumps(catalogue()))
    else:
        services = json.loads(Path('/app/services.json').read_text())
        region = sys.argv[1]
        (SOCKET.parent / 'client.conf').write_text(client_config(services, region))
        SOCKET.unlink(missing_ok=True)
        with Requests(str(SOCKET), Handler) as server:
            # The abstract socket exists only in this container's network namespace.
            # The development container can reach only the separate request socket.
            class SessionUpdate(socketserver.StreamRequestHandler):
                def handle(self):
                    value = json.load(self.rfile)
                    if value['region'] == region:
                        server.session = value
                        self.wfile.write(b'1')

            server.session = None
            server.services = services
            server.resolver = Session().get_component('endpoint_resolver')
            SOCKET.chmod(0o666)
            with socketserver.UnixStreamServer(ADDRESS, SessionUpdate) as updates:
                threading.Thread(target=updates.serve_forever, daemon=True).start()
                server.serve_forever()
