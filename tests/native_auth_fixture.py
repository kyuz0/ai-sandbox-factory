"""Account-free credential-service source shared by disposable native checks.

The source runs inside the workspace image, where its public system CA bundle
exists. It never reads host sign-ins or creates subscription credentials.
"""

AUTH_SERVICE = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

def start_auth_fixture(agent, port=3130):
    expected = 'factory-' + agent + '.internal'
    class Auth(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            target = urlsplit(self.path)
            host = target.hostname or urlsplit('//' + self.headers.get('Host', '')).hostname
            if host != expected or target.path != '/auth' or target.query or target.fragment:
                body = ('No synthetic ' + agent + ' credential-service route for ' + self.path).encode()
                self.send_response(404)
            else:
                self.server.auth_requests += 1
                body = json.dumps({'generation': 'synthetic', 'auth': None}).encode()
                self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', port), Auth)
    server.auth_requests = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
'''


def public_ca_source(agent):
    """Seed the same public-only CA path expected by the shipped client."""
    return ('Path(' + repr('/run/primary-editor/' + agent + '-ca.pem') + ').write_bytes('
            "Path('/etc/ssl/certs/ca-certificates.crt').read_bytes())\n")


def auth_fixture_source(agent):
    """Keep the shared fixture alive until its caller closes standard input."""
    return AUTH_SERVICE + public_ca_source(agent) + f'''
import sys
server, thread = start_auth_fixture({agent!r})
print('ready', flush=True)
sys.stdin.read()
server.shutdown()
server.server_close()
thread.join()
'''
