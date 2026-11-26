"""A read-only loopback OCI registry for real engine pull tests."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
from urllib.parse import unquote


class Registry(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.images = {}
        self.requests = []
        self.authenticated_requests = []
        self.blocked = Event()
        self.release = Event()
        self.block_repository = None
        self.failed_repository = None

    def add(self, role, directory):
        directory = Path(directory)
        manifest = (directory / 'manifest.json').read_bytes()
        digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        self.images[role] = (directory, manifest, digest)
        return f'localhost:{self.server_port}/{role}@{digest}'

    def alias(self, role, existing):
        self.images[role] = self.images[existing]
        return f'localhost:{self.server_port}/{role}@{self.images[role][2]}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.respond(head=True)

    def do_GET(self):
        self.respond()

    def respond(self, head=False):
        parts = unquote(self.path).split('?')[0].strip('/').split('/')
        self.server.requests.append((self.command, self.path))
        if 'Authorization' in self.headers:
            self.server.authenticated_requests.append((self.command, self.path))
        if parts == ['v2']:
            return self.send(b'{}', 'application/json', head=head)
        if len(parts) != 4 or parts[0] != 'v2' or parts[1] not in self.server.images:
            return self.send(b'{}', 'application/json', status=404, head=head)
        _, role, kind, digest = parts
        directory, manifest, expected = self.server.images[role]
        if kind == 'manifests':
            if role == self.server.failed_repository:
                return self.send(b'{"errors":[{"code":"UNAVAILABLE","message":"synthetic registry maintenance"}]}',
                                 'application/json', status=503, head=head)
            if role == self.server.block_repository:
                self.server.blocked.set()
                self.server.release.wait()
            if digest not in ('latest', expected):
                return self.send(b'{}', 'application/json', status=404, head=head)
            return self.send(manifest, json.loads(manifest)['mediaType'], digest=expected, head=head)
        if kind != 'blobs' or not digest.startswith('sha256:'):
            return self.send(b'{}', 'application/json', status=404, head=head)
        # Only serve blobs explicitly named by this manifest.
        parsed = json.loads(manifest)
        permitted = {entry['digest'] for entry in [parsed['config'], *parsed['layers']]}
        if digest not in permitted:
            return self.send(b'{}', 'application/json', status=404, head=head)
        path = directory / digest.split(':')[1]
        self.send_response(200)
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('Content-Type', 'application/octet-stream')
        self.end_headers()
        if not head:
            try:
                with path.open('rb') as file:
                    shutil.copyfileobj(file, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send(self, body, content_type, *, status=200, digest=None, head=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Docker-Distribution-Api-Version', 'registry/2.0')
        if digest:
            self.send_header('Docker-Content-Digest', digest)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def export(image, destination):
    destination = Path(destination)
    if (destination / 'manifest.json').exists():
        manifest = json.loads((destination / 'manifest.json').read_text())
        current = json.loads(subprocess.check_output(['podman', 'image', 'inspect', image], text=True))[0]
        if manifest['config']['digest'].removeprefix('sha256:') == current['Id'].removeprefix('sha256:'):
            return
        raise ValueError(f'Test export {destination} belongs to a different image. Use a new output folder.')
    subprocess.run(['podman', 'save', '--format', 'docker-dir', '--compress',
                    '-o', str(destination), image], check=True)


@contextmanager
def serve():
    server = Registry()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join()
