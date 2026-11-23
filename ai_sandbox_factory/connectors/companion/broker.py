"""Execute named HTTPS reads. Client headers and request data never reach the service."""
from http.client import HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import threading
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = self.server.routes.get(self.path[1:]) if self.path.startswith('/') else None
        if not url or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
            self.send_error(404)
            return
        parsed = urlsplit(url)
        path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        upstream = HTTPSConnection(parsed.hostname, parsed.port)
        started = False
        try:
            upstream.request('GET', path, headers={'Authorization': 'Bearer ' + self.server.token})
            response = upstream.getresponse()
            # Deliver only successful bodies. Never forward redirects, auth challenges or headers.
            if not 200 <= response.status < 300:
                self.send_error(502, f'The service returned HTTP {response.status}.')
                return
            self.protocol_version = 'HTTP/1.1'
            self.send_response(response.status)
            self.send_header('Content-Type', 'application/octet-stream')
            # Chunk framing makes a broken upstream detectable by the caller.
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers()
            started = True
            while chunk := response.read(64 * 1024):
                self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
            if response.length:
                return  # Upstream sent less than its advertised length.
            self.wfile.write(b'0\r\n\r\n')
        except (OSError, HTTPException):
            self.close_connection = True
            if not started:
                self.send_error(502, 'The connector could not reach the service.')
        finally:
            upstream.close()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path='/socket/api.sock', address='\0pi-factory-connector-control'):
    from receive import readiness_fifo
    # The engine can reach this listener by executing the helper inside this
    # companion. The workspace shares the filesystem socket only.
    with socket.socket(socket.AF_UNIX) as control:
        control.bind(address)
        control.listen()
        # If PID 1 exits, the engine terminates the waiting exec helper with
        # this namespace. If the helper exits, the host removes the failed worker.
        with open(readiness_fifo(), 'wb', buffering=0) as ready:
            ready.write(b'1')
        with control.accept()[0] as sender:
            with sender.makefile('rb') as stream:
                private = json.load(stream)
            Path(path).unlink(missing_ok=True)
            server = Server(path, Handler)
            server.routes = private['routes']
            server.token = private['token']
            os.chmod(path, 0o666)
            sender.sendall(b'1')
        with server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            while True:
                with control.accept()[0] as sender:
                    try:
                        with sender.makefile('rb') as stream:
                            update = json.load(stream)
                        if set(update) != {'routes'} or not isinstance(update['routes'], dict):
                            continue
                        # A request resolves a route from one complete grant.
                        server.routes = update['routes']
                        sender.sendall(b'1')
                    except (OSError, ValueError, TypeError):
                        continue


if __name__ == '__main__':
    # Container PID 1 otherwise ignores SIGTERM, delaying revocation until SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    serve()
