"""Serve the filtered native Antigravity hub on the private editor socket."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from agy_editor_protocol import AntigravityProtocol
from agy_editor_assets import CONFIG, render
from editor_protocol import Rejected, object_value, text
from launch import AGENT, SOURCE, command, workspace_directory


SOCKET = Path('/run/primary-editor/antigravity.sock')
SERVICE = '/exa.language_server_pb.LanguageServerService/'
# This port exists only inside the controller's private network namespace.
UPSTREAM = 'http://127.0.0.1:18783'
ASSETS = frozenset(('/', '/jetbox.css', '/compiled_tailwind.css', '/prism_bundle.js', '/main.js'))


class Hub:
    def __init__(self, settings, context):
        self.settings = settings
        self.context = context
        self.protocol = AntigravityProtocol(Path.home() / '.factory-editor',
            cwd=workspace_directory(settings), browser=settings.get('browser', False))
        self.public_token = secrets.token_urlsafe(32)
        self.private_token = None
        self.session = None
        self.process = None
        self.start_lock = asyncio.Lock()
        self.subscribers = {}

    async def start(self):
        async with self.start_lock:
            if self.process is not None and self.process.returncode is None and self.private_token:
                return
            self.private_token = None
            args = command('agy', 'open', self.settings, self.context)
            cwd = Path(workspace_directory(self.settings))
            cwd.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, 'AGY_ENABLE_HUB': '1', 'ANTIGRAVITY_VSCODE_HOST': '1'}
            self.process = await asyncio.create_subprocess_exec(
                AGENT, *args, '--hub', '--hub-port=18783', '--app_data_dir=antigravity',
                cwd=cwd, env=env, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            while self.process.returncode is None:
                try:
                    async with self.session.get(UPSTREAM + '/', allow_redirects=False) as reply:
                        html = await reply.text()
                    match = CONFIG.search(html)
                    if match:
                        self.private_token = text(json.loads(match.group(1))['csrfToken'])
                        return
                except (OSError, ValueError, KeyError):
                    pass
                await asyncio.sleep(.2)
            raise Rejected('Antigravity could not start. Check its sign-in in Factory.')

    async def close(self):
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            await self.process.wait()
        if self.session is not None:
            await self.session.close()

    def authenticated(self, headers, *, websocket=False):
        value = headers.get('x-codeium-csrf-token')
        if websocket:
            if not isinstance(value, list) or len(value) != 1:
                raise Rejected('Invalid editor connection')
            value = value[0]
        if not isinstance(value, str) or not secrets.compare_digest(value, self.public_token):
            raise Rejected('Invalid editor connection')

    async def socket(self, request):
        downstream = web.WebSocketResponse(max_msg_size=0)
        await downstream.prepare(request)
        async with self.session.ws_connect(UPSTREAM + '/connect-websocket',
                headers={'x-codeium-csrf-token': self.private_token}, max_msg_size=0) as upstream:
            streams = {}
            local_streams = set()

            async def incoming():
                async for message in downstream:
                    if message.type != WSMsgType.TEXT:
                        continue
                    sid = None
                    try:
                        data = object_value(json.loads(message.data))
                        sid = text(data.get('streamId'))
                        kind = data.get('type')
                        if kind == 'ping':
                            await downstream.send_json({'streamId': sid, 'type': 'pong'})
                            continue
                        if kind == 'cancel':
                            if sid in streams:
                                await upstream.send_json({'streamId': sid, 'type': 'cancel'})
                                streams.pop(sid, None)
                            local_streams.discard(sid)
                            self.subscribers.pop((id(downstream), sid), None)
                            continue
                        if kind != 'start' or sid in streams or sid in local_streams:
                            raise Rejected('Invalid editor request')
                        self.authenticated(object_value(data.get('headers', {})), websocket=True)
                        procedure = text(data.get('procedure'))
                        if not procedure.startswith(SERVICE):
                            raise Rejected('This operation is unavailable in the protected sandbox')
                        method = procedure[len(SERVICE):]
                        route, payload = self.protocol.request(method, data.get('payload', {}))
                        if route == 'local':
                            await downstream.send_json({'streamId': sid, 'type': 'data', 'payload': payload})
                            if data.get('stream'):
                                local_streams.add(sid)
                            else:
                                await downstream.send_json({'streamId': sid, 'type': 'end', 'statusCode': 200})
                            if method == 'JetboxSubscribeToState':
                                self.subscribers[(id(downstream), sid)] = downstream
                            if method == 'JetboxWriteState':
                                await self.publish_state()
                        else:
                            streams[sid] = (method, payload)
                            await upstream.send_json({
                                'streamId': sid, 'type': 'start', 'procedure': procedure,
                                'stream': data.get('stream') is True,
                                'headers': {'x-codeium-csrf-token': [self.private_token],
                                    'Content-Type': ['application/connect+json' if data.get('stream') else 'application/json']},
                                'payload': payload,
                            })
                    except (ValueError, KeyError, TypeError):
                        await downstream.send_json({'streamId': sid, 'type': 'end', 'statusCode': 403,
                            'error': {'code': 7, 'message': 'This operation is unavailable in the protected sandbox.'}})

            async def outgoing():
                async for message in upstream:
                    if message.type != WSMsgType.TEXT:
                        continue
                    data = object_value(json.loads(message.data))
                    sid = data.get('streamId')
                    entry = streams.get(sid)
                    if entry is None:
                        continue
                    method, payload = entry
                    try:
                        if data.get('type') == 'data':
                            result = self.protocol.response(method, data.get('payload', {}), payload)
                            if self.settings.get('native_approval', 'automatic') == 'automatic':
                                # Only permissions validated as enabled Factory
                                # workspace tools enter this set. Never approve
                                # a native controller operation automatically.
                                for chat, trajectory, index in list(self.protocol.pending):
                                    _, approval = self.protocol.request('HandleCascadeUserInteraction', {
                                        'cascadeId': self.protocol.public_id(chat), 'interaction': {
                                            'trajectoryId': trajectory, 'stepIndex': index,
                                            'permission': {'allow': True, 'scope': 'PERMISSION_SCOPE_ONCE'}}})
                                    await upstream.send_json({'streamId': 'approval-' + secrets.token_hex(12),
                                        'type': 'start', 'procedure': SERVICE + 'HandleCascadeUserInteraction',
                                        'stream': False, 'headers': {'x-codeium-csrf-token': [self.private_token],
                                            'Content-Type': ['application/json']}, 'payload': approval})
                            await downstream.send_json({'streamId': sid, 'type': 'data', 'payload': result})
                        elif data.get('type') == 'end':
                            failed = bool(data.get('error')) or data.get('statusCode', 200) >= 400
                            result = {'streamId': sid, 'type': 'end', 'statusCode': 502 if failed else 200}
                            if failed:
                                result['error'] = {'code': 13, 'message': 'Antigravity could not complete this request.'}
                            await downstream.send_json(result)
                            streams.pop(sid, None)
                    except (ValueError, KeyError, TypeError):
                        await upstream.send_json({'streamId': sid, 'type': 'cancel'})
                        streams.pop(sid, None)
                        await downstream.send_json({'streamId': sid, 'type': 'end', 'statusCode': 502,
                            'error': {'code': 13, 'message': 'Antigravity returned an unsupported conversation update.'}})

            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for key in list(self.subscribers):
                    if key[0] == id(downstream):
                        self.subscribers.pop(key, None)
        return downstream

    async def publish_state(self):
        for (_, sid), subscriber in list(self.subscribers.items()):
            if not subscriber.closed:
                with contextlib.suppress(ConnectionError):
                    await subscriber.send_json({'streamId': sid, 'type': 'data',
                                                'payload': {'appState': self.protocol.app.copy()}})

    async def handle(self, request):
        try:
            path = request.path
            if request.method == 'GET' and re.fullmatch(r'/c/[0-9a-f-]{36}', path):
                self.protocol.owned(path[3:])
                path = '/'
            if not (request.method == 'GET' and path in ASSETS or path == '/connect-websocket' or
                    request.method == 'POST' and path.startswith(SERVICE)):
                raise Rejected('This operation is unavailable in the protected sandbox')
            await self.start()
            if path == '/connect-websocket':
                return await self.socket(request)
            if request.method == 'POST':
                self.authenticated(request.headers)
                method = path[len(SERVICE):]
                route, payload = self.protocol.request(method, await request.json())
                if route == 'remote':
                    async with self.session.post(UPSTREAM + path, json=payload,
                            headers={'x-codeium-csrf-token': self.private_token, 'Connect-Protocol-Version': '1'},
                            allow_redirects=False) as reply:
                        if reply.status != 200:
                            raise Rejected('Antigravity could not complete this request')
                        payload = self.protocol.response(method, await reply.json(), payload)
                elif method == 'JetboxWriteState':
                    await self.publish_state()
                return web.json_response(payload, headers={'Cache-Control': 'no-store'})
            async with self.session.get(UPSTREAM + path, allow_redirects=False) as reply:
                if reply.status != 200:
                    raise Rejected('Antigravity could not load the panel')
                body = await reply.read()
                content_type = reply.headers.get('Content-Type', 'application/octet-stream').split(';')[0]
            body = render(path, body, self.public_token, request.headers.get('X-Factory-Editor-Origin', ''))
            return web.Response(body=body, content_type=content_type, headers={
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                'Content-Security-Policy': (
                    "default-src 'none'; base-uri 'none'; object-src 'none'; form-action 'none'; "
                    "script-src 'self' 'nonce-" + self.public_token + "'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
                    "worker-src 'self' blob:; media-src 'self' blob:; frame-src 'none'"),
            })
        except (ValueError, KeyError, TypeError):
            return web.json_response({'code': 'permission_denied',
                'message': 'This operation is unavailable in the protected sandbox.'}, status=403)


async def main():
    settings = json.loads((SOURCE / 'settings.json').read_text())
    hub = Hub(settings, (SOURCE / 'context.md').read_text())
    hub.session = ClientSession(trust_env=False, timeout=ClientTimeout(total=None))
    app = web.Application(client_max_size=0)
    app.router.add_route('*', '/{path:.*}', hub.handle)
    runner = web.AppRunner(app)
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    try:
        await runner.setup()
        SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        SOCKET.unlink(missing_ok=True)
        await web.UnixSite(runner, str(SOCKET)).start()
        SOCKET.chmod(0o600)
        await stop.wait()
    finally:
        await runner.cleanup()
        await hub.close()
        SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    asyncio.run(main())
