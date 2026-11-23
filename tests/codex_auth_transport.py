#!/usr/bin/env python3
"""Synthetic TLS HTTP/2 checks for the native credential broker's transport.

Requires mitmproxy 12.3.1 (including h2 and cryptography). No account, external
network, or real credential is used. CONNECT exercises the generic relay behind the standard proxy diversion hook;
gRPC verifies data and trailing metadata through the standard proxy directly.
"""
import argparse
import asyncio
import io
import logging
import datetime
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import ConnectionTerminated, DataReceived, RequestReceived, StreamEnded, StreamReset, TrailersReceived

PLACEHOLDER = 'synthetic-placeholder-access-token'
SUBSCRIPTION = 'synthetic-private-access-token'
FRAME = b'\x00\x00\x00\x00\x04ping'


def certificate(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), False)
            .sign(key, hashes.SHA256()))
    (root / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    (root / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def forward_proxy(target_port, timeout, authority=None):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(timeout)
    port = listener.getsockname()[1]
    def serve():
        connections = []
        try:
            incoming, _ = listener.accept()
            connections.append(incoming)
            incoming.settimeout(timeout)
            request = b''
            while b'\r\n\r\n' not in request:
                request += incoming.recv(4096)
            expected = authority or f'localhost:{target_port}'
            assert request.startswith(f'CONNECT {expected} HTTP/1.1\r\n'.encode())
            outgoing = socket.create_connection(('127.0.0.1', target_port), timeout=timeout)
            connections.append(outgoing)
            incoming.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            def copy(source, destination):
                try:
                    while data := source.recv(65536):
                        destination.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
            worker = threading.Thread(target=copy, args=(incoming, outgoing), daemon=True)
            worker.start()
            copy(outgoing, incoming)
            worker.join(timeout=timeout)
        except OSError:
            pass
        finally:
            for connection in connections:
                connection.close()
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, port, thread


def exercise(kind, root, timeout, standard_proxy=False):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(timeout)
    upstream_port = listener.getsockname()[1]
    observed = {}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / 'cert.pem', root / 'key.pem')
    context.set_alpn_protocols(['h2'])

    def upstream():
        try:
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True) as sock:
                sock.settimeout(timeout)
                h2 = H2Connection(config=H2Configuration(client_side=False, header_encoding='utf-8'))
                h2.initiate_connection()
                sock.sendall(h2.data_to_send())
                response_pending = {}
                response_ends = set()
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    for event in h2.receive_data(data):
                        if isinstance(event, RequestReceived):
                            observed['headers'] = dict(event.headers)
                            observed.setdefault('stream_headers', {})[event.stream_id] = dict(event.headers)
                            expected = 'custom-fixture-ticket' if event.stream_id == 3 else SUBSCRIPTION
                            assert observed['headers'].get('authorization') == 'Bearer ' + expected
                            if event.stream_id == 7:
                                h2.reset_stream(7, error_code=2)
                            elif event.stream_id != 5:
                                h2.send_headers(event.stream_id, [(':status', '200'), ('content-type', 'application/grpc')])
                        elif isinstance(event, DataReceived):
                            observed['data'] = observed.get('data', b'') + event.data
                            h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                            if kind == 'connect':
                                response_pending[event.stream_id] = response_pending.get(event.stream_id, b'') + event.data
                        elif isinstance(event, StreamReset):
                            observed.setdefault('reset', []).append(event.stream_id)
                            response_pending.pop(event.stream_id, None)
                            response_ends.discard(event.stream_id)
                        elif isinstance(event, StreamEnded) and kind == 'connect':
                            response_ends.add(event.stream_id)
                        elif isinstance(event, StreamEnded) and kind == 'grpc':
                            h2.send_data(event.stream_id, FRAME)
                            h2.send_headers(event.stream_id, [('grpc-status', '0'), ('grpc-message', 'fixture-ok')], end_stream=True)
                    for stream, payload in list(response_pending.items()):
                        while payload:
                            count = min(len(payload), h2.local_flow_control_window(stream), h2.max_outbound_frame_size)
                            if count <= 0:
                                break
                            h2.send_data(stream, payload[:count])
                            payload = payload[count:]
                        response_pending[stream] = payload
                        if not payload and stream in response_ends:
                            h2.end_stream(stream)
                            response_ends.remove(stream)
                            del response_pending[stream]
                    sock.sendall(h2.data_to_send())
        except (OSError, AssertionError):
            # The client assertions report transport failure; shutdown can close TLS.
            pass
        finally:
            listener.close()

    worker = threading.Thread(target=upstream, daemon=True)
    worker.start()
    relay = None
    relay_port = None
    gateway_proxy = None
    client_host, client_port = 'localhost', upstream_port
    if kind == 'grpc':
        client_host, client_port = 'codex-cloud-backend.chatgpt.com', 443
        gateway_proxy = forward_proxy(upstream_port, timeout, f'{client_host}:{client_port}')
    if kind == 'connect' and not standard_proxy:
        reserve = socket.socket()
        reserve.bind(('127.0.0.1', 0))
        relay_port = reserve.getsockname()[1]
        reserve.close()
        gateway_proxy = forward_proxy(upstream_port, timeout)
        relay_script = root / 'relay.py'
        module = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
        relay_script.write_text(
            'import asyncio, ssl, sys\n'
            f'sys.path.insert(0, {str(module)!r})\n'
            'from codex_connect import ConnectRelay\n'
            'from codex_auth_proxy import connect_tls_context\n'
            'async def main():\n'
            f'    context=connect_tls_context({str(root / "ca-connect")!r}, "localhost")\n'
            f'    upstream=ssl.create_default_context(cafile={str(root / "cert.pem")!r})\n'
            f'    relay=ConnectRelay("localhost", lambda: {{{PLACEHOLDER!r}: {SUBSCRIPTION!r}}}, port={upstream_port}, proxy=("127.0.0.1", {gateway_proxy[1]}), upstream_context=upstream)\n'
            f'    server=await relay.start_proxy(context, port={relay_port})\n'
            '    async with server: await server.serve_forever()\n'
            'asyncio.run(main())\n')
        relay = subprocess.Popen([sys.executable, str(relay_script)], stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while True:
            try:
                with socket.create_connection(('127.0.0.1', relay_port), timeout=1):
                    break
            except OSError:
                if relay.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Synthetic CONNECT relay did not start')
                time.sleep(.05)
    addon = root / (kind + '-addon.py')
    addon.write_text('from mitmproxy import http\n'
        'def request(flow):\n'
        f'    if flow.request.headers.get("authorization") == "Bearer {PLACEHOLDER}":\n'
        f'        flow.request.headers["authorization"] = "Bearer {SUBSCRIPTION}"\n')
    if relay_port:
        module = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
        addon.write_text('import sys\n'
            f'sys.path.insert(0, {str(module)!r})\n'
            'from codex_auth_proxy import SubscriptionProxy\n'
            'class Store:\n'
            f'    def substitutions(self): return {{{PLACEHOLDER!r}: {SUBSCRIPTION!r}}}\n'
            f'addons=[SubscriptionProxy(Store(), connect_address=("127.0.0.1", {relay_port}), connect_target=("localhost", {upstream_port}))]\n')
    if kind == 'grpc':
        module = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
        addon.write_text('import sys\n'
            f'sys.path.insert(0, {str(module)!r})\n'
            'from codex_auth_proxy import SubscriptionProxy\n'
            'class Store:\n'
            f'    def substitutions(self): return {{{PLACEHOLDER!r}: {SUBSCRIPTION!r}}}\n'
            'class FixtureProxy(SubscriptionProxy):\n'
            '    def route(self, flow):\n'
            '        super().route(flow)\n'
            '        assert flow.server_conn.via == ("http", ("127.0.0.1", 3128))\n'
            f'        flow.server_conn.via = ("http", ("127.0.0.1", {gateway_proxy[1]}))\n'
            'addons=[FixtureProxy(Store())]\n')
    reserve = socket.socket()
    reserve.bind(('127.0.0.1', 0))
    proxy_port = reserve.getsockname()[1]
    reserve.close()
    log = (root / (kind + '-proxy.log')).open('w+')
    ignore = []
    if kind == 'grpc':
        ignore += ['--mode', 'upstream:http://127.0.0.1:' + str(gateway_proxy[1])]
    if relay_port:
        sys.path.insert(0, str(module))
        from codex_auth_proxy import connect_ignore_hosts
        for pattern in connect_ignore_hosts(('localhost', upstream_port), ('127.0.0.1', relay_port)):
            ignore += ['--ignore-hosts', pattern]
        ignore += ['--mode', 'upstream:http://127.0.0.1:' + str(gateway_proxy[1])]
    proxy = subprocess.Popen([str(Path(sys.executable).parent / 'mitmdump'), '-q', *ignore,
        '--listen-host', '127.0.0.1', '--listen-port', str(proxy_port),
        '--set', 'confdir=' + str(root / ('ca-' + kind)), '--ssl-insecure', '-s', str(addon)],
        stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                raw = socket.create_connection(('127.0.0.1', proxy_port), timeout=1)
                break
            except OSError:
                if proxy.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Synthetic proxy did not start')
                time.sleep(.05)
        raw.settimeout(timeout)
        raw.sendall(f'CONNECT {client_host}:{client_port} HTTP/1.1\r\nHost: {client_host}:{client_port}\r\n\r\n'.encode())
        response = b''
        while b'\r\n\r\n' not in response:
            response += raw.recv(4096)
        assert b' 200 ' in response, 'Outer proxy CONNECT rejected'
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if relay_port:
            client_context.load_verify_locations(root / 'ca-connect/mitmproxy-ca-cert.pem')
        else:
            client_context.check_hostname = False
            client_context.verify_mode = ssl.CERT_NONE
        client_context.set_alpn_protocols(['h2'])
        with client_context.wrap_socket(raw, server_hostname=client_host) as sock:
            h2 = H2Connection(config=H2Configuration(client_side=True, header_encoding='utf-8', validate_outbound_headers=False))
            h2.initiate_connection()
            headers = [(':method', 'CONNECT'), (':authority', f'localhost:{upstream_port}')]
            if kind == 'grpc':
                headers = [(':method', 'POST'), (':scheme', 'https'),
                           (':authority', f'{client_host}:{client_port}'), (':path', '/fixture.Native/Call'),
                           ('content-type', 'application/grpc'), ('te', 'trailers')]
            headers.append(('authorization', 'Bearer ' + PLACEHOLDER))
            bodies = {1: FRAME} if kind == 'grpc' else {
                1: b'tunnel-request' * 65536, 3: b'custom-ticket-stream'}
            request_bodies = bodies if kind == 'grpc' else {9: b'cancel-in-flight' * 65536, **bodies}
            for stream in bodies:
                stream_headers = list(headers)
                if stream == 3:
                    stream_headers[-1] = ('authorization', 'Bearer custom-fixture-ticket')
                h2.send_headers(stream, stream_headers)
            if kind == 'connect':
                h2.send_headers(5, headers)
                h2.reset_stream(5)
                h2.send_headers(7, headers)
                h2.send_headers(9, headers)
            pending = dict(request_bodies)
            received = {stream: b'' for stream in bodies}
            trailers = {}
            ended = set()
            resets = set()
            while len(ended) < len(bodies):
                for stream, payload in list(pending.items()):
                    window = min(h2.local_flow_control_window(stream), h2.max_outbound_frame_size)
                    if window > 0:
                        count = min(window, len(payload))
                        h2.send_data(stream, payload[:count], end_stream=count == len(payload))
                        if count == len(payload):
                            del pending[stream]
                        else:
                            pending[stream] = payload[count:]
                sock.sendall(h2.data_to_send())
                data = sock.recv(65536)
                if not data:
                    break
                for event in h2.receive_data(data):
                    if isinstance(event, DataReceived):
                        h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        if event.stream_id == 9:
                            if 9 in pending:
                                pending.pop(9)
                                h2.reset_stream(9)
                        else:
                            received[event.stream_id] += event.data
                    elif isinstance(event, StreamReset):
                        resets.add(event.stream_id)
                    elif isinstance(event, TrailersReceived):
                        trailers = dict(event.headers)
                    elif isinstance(event, StreamEnded):
                        ended.add(event.stream_id)
            assert observed.get('stream_headers', {}).get(1, {}).get('authorization') == 'Bearer ' + SUBSCRIPTION, 'Upstream did not receive substituted credential'
            assert received == bodies, 'Duplex stream bytes changed or stream did not complete'
            if kind == 'grpc':
                assert observed.get('data') == FRAME, 'gRPC request frame changed'
                assert trailers == {'grpc-status': '0', 'grpc-message': 'fixture-ok'}, 'gRPC trailers lost'
            else:
                assert observed['stream_headers'][3]['authorization'] == 'Bearer custom-fixture-ticket', 'Custom bearer was replaced'
                assert 5 in observed.get('reset', []), 'Stream cancellation was not propagated'
                assert 7 in resets, 'Upstream stream fault was not propagated'
                assert 9 in observed.get('reset', []), 'Active stream cancellation was not propagated'
            return {'check': kind, 'status': 'passed'}
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=10)
        if gateway_proxy is not None:
            gateway_proxy[0].close()
            gateway_proxy[2].join(timeout=timeout + 1)
        listener.close()
        worker.join(timeout=timeout + 1)
        log.close()



def fault_checks(root, timeout):
    """A refused gateway gives a correlated diagnostic and leaves no sessions."""
    module = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
    sys.path.insert(0, str(module))
    from codex_connect import ConnectRelay
    async def check():
        reserved = socket.socket()
        reserved.bind(('127.0.0.1', 0))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / 'cert.pem', root / 'key.pem')
        client_context = ssl.create_default_context(cafile=str(root / 'cert.pem'))
        client_context.set_alpn_protocols(['h2'])
        relay = ConnectRelay('localhost', lambda: {PLACEHOLDER: SUBSCRIPTION},
                             port=reserved.getsockname()[1], proxy=None)
        server = await relay.start(context, port=0)
        log = io.StringIO()
        logger = logging.getLogger()
        old = logger.handlers[:]
        logger.handlers = [logging.StreamHandler(log)]
        ids = set()
        try:
            for _ in range(2):
                reader, writer = await asyncio.open_connection('localhost', server.sockets[0].getsockname()[1],
                                                               ssl=client_context, server_hostname='localhost')
                h2 = H2Connection()
                h2.initiate_connection()
                writer.write(h2.data_to_send())
                await writer.drain()
                diagnostic = None
                while data := await reader.read(65536):
                    for event in h2.receive_data(data):
                        if isinstance(event, ConnectionTerminated):
                            diagnostic = event.additional_data.decode()
                    if diagnostic:
                        break
                assert diagnostic and ('ConnectionRefusedError' in diagnostic or 'Connect call failed' in diagnostic or 'Connection refused' in diagnostic), 'Missing underlying gateway failure'
                request_id = diagnostic.split(';', 1)[0].removeprefix('request ')
                assert request_id in log.getvalue(), 'Client/log request IDs differ'
                ids.add(request_id)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            assert len(ids) == 2, 'Listener did not recover for a subsequent connection'
            assert SUBSCRIPTION not in log.getvalue(), 'Credential leaked in transport diagnostics'
        finally:
            server.close()
            await server.wait_closed()
            await relay.close()
            reserved.close()
            logger.handlers = old
        assert not relay.sessions, 'Fault cleanup left active relay sessions'
    asyncio.run(asyncio.wait_for(check(), timeout))
    return {'check': 'gateway_failure_cleanup', 'status': 'passed'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['grpc', 'connect', 'fault', 'all'], default='all')
    parser.add_argument('--standard-proxy', action='store_true', help='Reproduce CONNECT without relay support')
    parser.add_argument('--timeout', type=float, default=15)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-codex-transport-') as directory:
        root = Path(directory)
        certificate(root)
        for kind in (['grpc', 'connect', 'fault'] if args.kind == 'all' else [args.kind]):
            try:
                result = fault_checks(root, args.timeout) if kind == 'fault' else exercise(kind, root, args.timeout, args.standard_proxy)
                print(json.dumps(result), flush=True)
            except Exception as error:
                print(json.dumps({'check': kind, 'status': 'failed', 'error': str(error)}), flush=True)
                raise SystemExit(1)


if __name__ == '__main__':
    main()
