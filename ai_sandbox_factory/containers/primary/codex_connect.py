"""HTTP/2 CONNECT credential relay; opaque tunnel data is never interpreted.

The TLS listener belongs inside the credential companion. Its caller supplies a
private CA-signed server context and an exact upstream gateway. HTTP/2 receive
windows bound pending data; credit is returned only after forwarding succeeds.
"""
import asyncio
import logging
import ssl
import uuid
from collections import deque
from datetime import datetime, timezone

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.exceptions import StreamClosedError
from h2.events import (ConnectionTerminated, DataReceived, RequestReceived,
                       ResponseReceived, StreamEnded, StreamReset,
                       TrailersReceived)


class Peer:
    def __init__(self, reader, writer, client):
        self.reader, self.writer = reader, writer
        self.h2 = H2Connection(config=H2Configuration(
            client_side=client, header_encoding=None, validate_outbound_headers=False))
        self.pending = {}
        self.ends = set()
        self.other = None
        self.rejected = set()

    async def write(self):
        data = self.h2.data_to_send()
        if data:
            self.writer.write(data)
            await self.writer.drain()

    def flush(self):
        # Round-robin frames across streams so a large tunnel cannot monopolize
        # each connection-window update while another tunnel is ready to send.
        while self.pending:
            progressed = False
            for stream, chunks in list(self.pending.items()):
                data, credit = chunks[0]
                window = min(self.h2.local_flow_control_window(stream), self.h2.max_outbound_frame_size)
                if data and window <= 0:
                    continue
                count = min(window, len(data)) if data else 0
                if count:
                    self.h2.send_data(stream, data[:count])
                if count == len(data):
                    chunks.popleft()
                    self.other.h2.acknowledge_received_data(credit, stream)
                else:
                    chunks[0] = (data[count:], credit)
                progressed = True
                if not chunks:
                    del self.pending[stream]
                    if stream in self.ends:
                        self.ends.remove(stream)
                        self.h2.end_stream(stream)
            if not progressed:
                break

    def end(self, stream):
        if self.pending.get(stream):
            self.ends.add(stream)
        else:
            self.h2.end_stream(stream)


class ConnectRelay:
    def __init__(self, host, substitutions, *, port=443,
                 proxy=('127.0.0.1', 3128), upstream_context=None):
        self.host, self.port = host, port
        self.substitutions = substitutions
        self.proxy = proxy
        self.upstream_context = upstream_context or ssl.create_default_context()
        self.upstream_context.set_alpn_protocols(['h2'])
        self.sessions = set()

    async def upstream(self):
        if self.proxy is None:
            reader, writer = await asyncio.open_connection(
                self.host, self.port, ssl=self.upstream_context, server_hostname=self.host)
        else:
            reader, writer = await asyncio.open_connection(*self.proxy)
            authority = f'{self.host}:{self.port}'
            writer.write(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'))
            await writer.drain()
            response = await reader.readuntil(b'\r\n\r\n')
            status = response.split(b'\r\n', 1)[0].split(b' ', 2)
            if len(status) < 2 or status[1] != b'200':
                writer.close()
                await writer.wait_closed()
                code = status[1].decode('ascii', 'replace') if len(status) > 1 else 'missing'
                raise OSError(f'Gateway proxy CONNECT rejected with HTTP status {code}')
            try:
                await writer.start_tls(self.upstream_context, server_hostname=self.host)
            except BaseException:
                writer.close()
                await asyncio.gather(writer.wait_closed(), return_exceptions=True)
                raise
        if writer.get_extra_info('ssl_object').selected_alpn_protocol() != 'h2':
            writer.close()
            await writer.wait_closed()
            raise OSError('Gateway TLS negotiation did not select HTTP/2')
        return reader, writer

    def headers(self, headers, replace):
        # Only complete recognized placeholders are substituted. User-supplied
        # bearer tickets and all unrelated headers remain unchanged.
        substitutions = self.substitutions()
        pairs = {b'Bearer ' + mask.encode(): b'Bearer ' + real.encode()
                 for mask, real in substitutions.items()}
        if not replace:
            pairs = {real: mask for mask, real in pairs.items()}
        return [(name, pairs.get(value, value) if name.lower() == b'authorization' else value)
                for name, value in headers]

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.sessions.add(task)
        request_id = uuid.uuid4().hex
        peers = []
        stage = 'upstream TLS connection'
        downstream = None
        initialized = False
        response_started = False
        try:
            downstream = Peer(reader, writer, False)
            upstream = Peer(*(await self.upstream()), True)
            peers = [downstream, upstream]
            stage = 'HTTP/2 tunnel forwarding'
            downstream.other, upstream.other = upstream, downstream
            lock = asyncio.Lock()
            for peer in peers:
                peer.h2.initiate_connection()
                await peer.write()
            initialized = True

            async def pump(peer):
                nonlocal response_started
                while data := await peer.reader.read(65536):
                    async with lock:
                        for event in peer.h2.receive_data(data):
                            stream = getattr(event, 'stream_id', None)
                            other = peer.other
                            if isinstance(event, RequestReceived):
                                headers = dict(event.headers)
                                # This listener is a gateway relay, never an open
                                # forward proxy. The configured upstream is fixed.
                                if headers.get(b':method') != b'CONNECT':
                                    peer.rejected.add(stream)
                                    peer.h2.send_headers(stream, [(b':status', b'405')], end_stream=True)
                                    continue
                                other.h2.send_headers(stream, self.headers(event.headers, True))
                            elif isinstance(event, (ResponseReceived, TrailersReceived)):
                                response_started = True
                                other.h2.send_headers(stream, self.headers(event.headers, False))
                            elif isinstance(event, DataReceived):
                                if stream in peer.rejected:
                                    peer.h2.acknowledge_received_data(event.flow_controlled_length, stream)
                                    continue
                                other.pending.setdefault(stream, deque()).append((event.data, event.flow_controlled_length))
                            elif isinstance(event, StreamEnded):
                                if stream not in peer.rejected:
                                    other.end(stream)
                            elif isinstance(event, StreamReset):
                                if stream in peer.rejected:
                                    continue
                                # Discard buffered bytes and return connection
                                # credit, so a cancelled stream cannot starve others.
                                for target in (peer, other):
                                    credit = sum(size for _, size in target.pending.pop(stream, ()))
                                    if credit:
                                        target.other.h2.increment_flow_control_window(credit)
                                    target.ends.discard(stream)
                                try:
                                    other.h2.reset_stream(stream, event.error_code)
                                except StreamClosedError:
                                    pass
                            elif isinstance(event, ConnectionTerminated):
                                if event.error_code:
                                    raise OSError(f'HTTP/2 peer sent GOAWAY error {event.error_code}; last stream {event.last_stream_id}')
                                return
                        downstream.flush()
                        upstream.flush()
                        await downstream.write()
                        await upstream.write()
            tasks = [asyncio.create_task(pump(peer)) for peer in peers]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for completed in done:
                    completed.result()
            finally:
                for pending in tasks:
                    pending.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Preserve the cause while removing known credentials from peer text.
            detail = str(error)
            try:
                secrets = self.substitutions()
            except (OSError, ValueError, KeyError):
                secrets = {}
                detail = 'Credential state could not be read while handling transport failure'
            for mask, real in secrets.items():
                detail = detail.replace(real, '[credential]').replace(mask, '[placeholder]')
            logging.error('CONNECT relay failure; time %s; request %s; gateway %s:%s; stage %s; cause %s: %s; errno %s',
                          datetime.now(timezone.utc).isoformat(), request_id, self.host, self.port,
                          stage, type(error).__name__, detail, getattr(error, 'errno', None))
            if downstream is not None and not response_started:
                try:
                    if not initialized:
                        downstream.h2.initiate_connection()
                    diagnostic = f'request {request_id}; {stage}; {type(error).__name__}: {detail}'.encode()
                    limit = downstream.h2.max_outbound_frame_size - 8  # RFC GOAWAY fixed fields
                    if len(diagnostic) > limit:
                        suffix = b' [truncated]'
                        diagnostic = diagnostic[:limit - len(suffix)] + suffix
                    downstream.h2.close_connection(error_code=2, additional_data=diagnostic)
                    await downstream.write()
                except Exception:
                    pass  # The original failure is already recorded; peer may be gone.
        finally:
            writers = [peer.writer for peer in peers] or [writer]
            for connection in writers:
                connection.close()
            await asyncio.gather(*(connection.wait_closed() for connection in writers), return_exceptions=True)
            self.sessions.discard(task)

    async def start_proxy(self, context, host='127.0.0.1', port=3132):
        """Expose a private HTTP CONNECT hop, with TLS/H2 terminated inside it."""
        context.set_alpn_protocols(['h2'])
        async def accept(reader, writer):
            task = asyncio.current_task()
            self.sessions.add(task)
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                line = request.split(b'\r\n', 1)[0].split(b' ')
                authority = f'{self.host}:{self.port}'.encode('ascii')
                if len(line) != 3 or line[:2] != [b'CONNECT', authority] or line[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                    await writer.drain()
                    return
                writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                await writer.drain()
                await writer.start_tls(context)
                await self.handle(reader, writer)
            except asyncio.IncompleteReadError as error:
                if error.partial:
                    logging.error('Private CONNECT relay handshake ended before complete headers')
            except (OSError, asyncio.LimitOverrunError) as error:
                logging.error('Private CONNECT relay handshake failed: %s: %s', type(error).__name__, error)
            finally:
                writer.close()
                await asyncio.gather(writer.wait_closed(), return_exceptions=True)
                self.sessions.discard(task)
        return await asyncio.start_server(accept, host, port)

    async def start(self, context, host='127.0.0.1', port=3132):
        context.set_alpn_protocols(['h2'])
        return await asyncio.start_server(self.handle, host, port, ssl=context)

    async def close(self):
        tasks = list(self.sessions)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.difference_update(tasks)
