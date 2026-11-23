"""Exercise production gateway uploads against a slow local receiver; no inference."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from ai_sandbox_factory import host_platform, sandbox


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=120)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            remaining = int(self.headers['Content-Length'])
            size = 0
            digest = hashlib.sha256()
            while remaining:
                chunk = self.rfile.read(min(16384, remaining))
                if not chunk:
                    return
                digest.update(chunk)
                remaining -= len(chunk)
                size += len(chunk)
                # Apply backpressure instead of draining every socket immediately.
                time.sleep(.001)
            received.append(self.path)
            body = json.dumps({'bytes': size, 'sha256': digest.hexdigest()}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    name = 'factory-gateway-upload-' + uuid.uuid4().hex[:8]
    port = receiver.server_port
    try:
        with tempfile.TemporaryDirectory(prefix=name + '-') as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            egress = root / 'egress'
            egress.mkdir()
            config = root / 'squid.conf'
            config.write_text(sandbox.squid_config([('local-model.internal', port)]))
            subprocess.run(['podman', 'run', '-d', '--name', name,
                '--security-opt=no-new-privileges', '--pids-limit=-1', '--cap-drop=ALL',
                '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=DAC_OVERRIDE',
                *host_platform.gateway_labels(), *host_platform.model_gateway_args(),
                '-v', f'{config}:/etc/squid/sandbox.conf:{host_platform.bind_mode()}',
                '-v', f'{egress}:/run/egress:z', args.image], check=True, capture_output=True)
            sock = egress / 'proxy.sock'
            deadline = time.monotonic() + 30
            while True:
                ready = UnixHTTPConnection(sock)
                try:
                    ready.request('GET', 'http://not-allowed.invalid/')
                    response = ready.getresponse()
                    response.read()
                    if response.status == 403:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    ready.close()
                if time.monotonic() >= deadline:
                    raise RuntimeError('Gateway did not begin enforcing its destination rules')
                time.sleep(.05)

            def send(size, label):
                body = b'{"padding":"' + b'x' * (size - 14) + b'"}'
                assert len(body) == size
                connection = UnixHTTPConnection(sock)
                try:
                    connection.request('POST', f'http://local-model.internal:{port}/{label}', body,
                                       {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    value = response.read()
                    assert response.status == 200, (response.status, value[:200])
                    assert json.loads(value) == {'bytes': size, 'sha256': hashlib.sha256(body).hexdigest()}
                finally:
                    connection.close()

            # Incident-sized upload and the broker's maximum supported body size.
            send(4_549_370, 'incident-size')
            send(32 * 1024 * 1024, 'maximum-size')
            print('PASS complete 4.55 MB and 32 MiB uploads with matching hashes', flush=True)
            with ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(lambda n: send(4_549_370, f'concurrent-{n}'), range(10)))
            print('PASS ten concurrent incident-sized uploads under backpressure', flush=True)

            cancelled = UnixHTTPConnection(sock)
            cancelled.putrequest('POST', f'http://local-model.internal:{port}/cancelled')
            cancelled.putheader('Content-Length', str(8 * 1024 * 1024))
            cancelled.endheaders()
            cancelled.send(b'x' * 1024 * 1024)
            cancelled.close()
            send(4_549_370, 'after-cancellation')
            denied = UnixHTTPConnection(sock)
            denied.request('GET', 'http://not-allowed.invalid/')
            response = denied.getresponse()
            assert response.status == 403
            response.read()
            denied.close()
            send(1024, 'after-denial')
            assert '/cancelled' not in received
            print('PASS cancellation recovery and unchanged destination restrictions', flush=True)
    finally:
        subprocess.run(['podman', 'logs', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['podman', 'rm', '-f', name], capture_output=True)
        receiver.shutdown()
        receiver.server_close()
        thread.join()


if __name__ == '__main__':
    main()
