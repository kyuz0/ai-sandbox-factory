"""Host-owned browser controls, independent of the replaceable editor process.

Controls come from settings_controls. Preview and Apply use the public CLI;
credentials for this service never enter the editor's origin or container.
"""
import asyncio
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import browser_editor, editor_settings, host_platform, operation_locks, settings_controls
from .sandbox_options import write_json

ASSETS = Path(__file__).with_name('browser_assets')


def directory(state):
    path = Path(state) / 'browser-controls'
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def origin(state):
    try:
        return json.loads((Path(state) / 'editor-address.json').read_text()).get('toolbar_origin', '')
    except (OSError, ValueError):
        return ''


def control(state, action):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(host_platform.control_path(Path(state) / 'browser-controls')))
        client.sendall((action + '\n').encode())
        return client.recv(4096).decode().strip()


def locked(path):
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def startup_error(path, message):
    try:
        with (path / 'server.log').open('rb') as log:
            size = log.seek(0, 2)
            log.seek(max(0, size - 8192))
            detail = log.read().decode(errors='replace').strip()
    except OSError as error:
        detail = f'Could not read the startup log: {error}'
    return ValueError(f'{message} {detail or "No startup diagnostic was written."} '
                      f'Check {path / "server.log"}. If its address is in use, close the conflicting listener '
                      'or change the Browser connection address, then Open again.')


def stop(state):
    path = Path(state) / 'browser-controls'
    if not path.exists() or not locked(path / 'server.lock'):
        return
    try:
        control(state, 'stop')
    except OSError as error:
        raise ValueError(f'Cannot stop the browser toolbar for {Path(state).name}: {error}. {recovery_hint(path)}') from error
    deadline = time.monotonic() + 15  # Same supervisor shutdown allowance as the editor.
    while locked(path / 'server.lock') and time.monotonic() < deadline:
        time.sleep(.05)
    if locked(path / 'server.lock'):
        raise ValueError(f'The browser toolbar for {Path(state).name} is still stopping. {recovery_hint(path)}')


def recovery_hint(path):
    try:
        pid = int((path / 'server.pid').read_text())
        return (f'Check `ps -p {pid} -o pid=,command=`. If it is the Factory browser_shell serve process '
                f'for {path.parent}, end it with `kill -TERM {pid}`, then retry. See {path / "server.log"}.')
    except (OSError, ValueError):
        return (f'Find the Factory browser_shell serve process for {path.parent} with `ps -ef`, '
                f'end that process with SIGTERM, then retry. See {path / "server.log"}.')


def start(state):
    """Start once, preserving its saved address. Pair only through host IPC."""
    path = directory(state)
    with (path / 'server.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return control(state, 'pair'), False
            except OSError as error:
                raise ValueError(f'Cannot contact the browser toolbar for {Path(state).name}: {error}. {recovery_hint(path)}') from error
        read_fd, write_fd = os.pipe()
        try:
            with (path / 'server.log').open('w') as log:
                process = subprocess.Popen(host_platform.python_command('ai_sandbox_factory.browser_shell',
                    'serve', str(state), str(write_fd), str(lock.fileno())),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    pass_fds=(write_fd, lock.fileno()))
            os.close(write_fd)
            write_fd = None
            if not select.select([read_fd], [], [], 15)[0]:
                process.terminate()
                process.wait()
                raise startup_error(path, 'The browser toolbar did not report readiness.')
            result = os.read(read_fd, 4096).decode().strip()
            if not result.startswith(('http://', 'https://')):
                process.wait()
                raise startup_error(path, f'The browser toolbar failed to start (exit {process.returncode}).')
            return result, True
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)


def open_editor(config, state, archive=None, background=False, open_tab=True):
    """User-facing Open; replacement code continues using the editor directly."""
    with operation_locks.sandbox(Path(state).name, 'open the browser editor for'):
        previous = browser_editor.runtime(state)
        url, started = start(state)
        try:
            browser_editor.open_browser_editor(config, state, archive, background=True, open_tab=False)
        except BaseException:
            if started:
                try:
                    stop(state)
                except ValueError as cleanup_error:
                    print(str(cleanup_error), file=sys.stderr)
            raise
    print('Sandbox toolbar: ' + url, flush=True)
    if open_tab:
        host_platform.open_url(url)
    if not background and previous is None:
        print('Ctrl+C stops the browser editor.', flush=True)
        try:
            while locked(directory(state) / 'server.lock'):
                # A replacement temporarily stops the editor, while its
                # lifecycle lock remains held. Keep the foreground owner alive.
                if not browser_editor.active(state) and not operation_locks.in_use(Path(state).name):
                    raise ValueError(f'The browser editor for {Path(state).name} stopped unexpectedly. '
                                     f'Check {Path(state) / "browser-editor.log"}, then Open the sandbox again.')
                time.sleep(.25)
        except KeyboardInterrupt:
            if operation_locks.in_use(Path(state).name):
                raise ValueError('The sandbox is being updated. Wait for its result in the browser toolbar, '
                                 'then stop the browser editor from Factory.') from None
            with operation_locks.sandbox(Path(state).name, 'stop the browser editor for'):
                stop(state)
                browser_editor.stop(state)


def command(state, *args):
    return host_platform.python_command('ai_sandbox_factory', '--state-dir', str(Path(state).parent), *args)


def job_status(state):
    path = directory(state)
    try:
        job = json.loads((path / 'job.json').read_text())
    except FileNotFoundError:
        return None
    running = locked(path / 'job.lock')
    if running:
        job['status'] = 'running'
    elif job.get('status') == 'running':
        job.update(status='interrupted', error='The update worker stopped without reporting a result. '
                   + ('Factory is still finishing the operation. ' if operation_locks.in_use(Path(state).name) else '')
                   + f'Check {path / "job.log"}, then review the sandbox settings before retrying.')
    if job.get('status') == 'failed' and operation_locks.in_use(Path(state).name):
        job['error'] += ' Factory is still finishing an operation for this sandbox. Wait for it before retrying.'
    if job['status'] in ('failed','interrupted'):
        try:
            with (path/'job.log').open('rb') as log:
                size = log.seek(0,2)
                log.seek(max(0,size-65536))
                job['output'] = ('[Earlier output omitted]\n' if size>65536 else '') + log.read().decode(errors='replace')
        except OSError:
            pass
    return job


def start_job(state, plan):
    path = directory(state)
    with (path / 'job.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('An update is already running for this sandbox. Wait for its result.') from None
        identifier = secrets.token_hex(16)
        write_json(path / 'changes.json', plan.get('changes', {}))
        write_json(path / 'request.json', {key:plan[key] for key in ('operation','packages') if key in plan})
        write_json(path / 'job.json', {'id': identifier, 'status': 'running',
            'network_before': json.loads((Path(state) / 'config.json').read_text()).get('network', 'allowlist'),
            'controls_before': settings_controls.describe(json.loads((Path(state) / 'config.json').read_text()),state),
            'title': plan.get('title','Settings'), 'effect': plan['runtime_effect'],
            'completion': plan.get('completion','Settings updated.')})
        try:
            with (path / 'job.log').open('w') as log:
                subprocess.Popen(host_platform.python_command('ai_sandbox_factory.browser_shell',
                    'apply', str(state), plan['revision'], str(lock.fileno())),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    pass_fds=(lock.fileno(),))
        except OSError as error:
            message = f'Could not start {plan.get("title", "the settings change")}: {error}. Retry after correcting the reported cause.'
            write_json(path / 'job.json', {'id': identifier, 'status': 'failed', 'error': message})
            raise ValueError(message) from error
        return json.loads((path/'job.json').read_text())


def run_job(state, revision, lock_fd):
    path = directory(state)
    with os.fdopen(lock_fd, 'a+'):
        job = json.loads((path / 'job.json').read_text())
        try:
            # The CLI also inherits the receipt lock: killing this runner cannot
            # falsely report completion while its lifecycle child still runs.
            request = json.loads((path / 'request.json').read_text())
            if request.get('operation', 'update') == 'update':
                argv = ['update', Path(state).name, '--settings-file', str(path / 'changes.json'), '--revision', revision]
            elif request['operation'] == 'install-system-packages':
                from . import system_packages
                argv = ['install-system-packages', Path(state).name, '--revision', revision, *system_packages.package_names(request['packages'])]
            else:
                raise ValueError('Unknown browser operation. Review the requested action again.')
            result = subprocess.run(command(state, *argv), pass_fds=(lock_fd,))
            job.update(status='succeeded' if result.returncode == 0 else 'failed', exit_code=result.returncode)
            if result.returncode:
                lines = (path / 'job.log').read_text(errors='replace').splitlines()
                detail = lines[-1] if lines else 'No diagnostic output.'
                detail = re.sub(r'^(?:Error: |Update command exited with status -?\d+: )+', '', detail)
                job['error'] = f'{job.get("title", "Settings change")} failed (exit {result.returncode}). ' + detail
        except Exception as error:
            job.update(status='failed', error=f'Could not run {job.get("title", "the settings change")}: {error}. See {path / "job.log"}.')
        write_json(path / 'job.json', job)


class Shell:
    def __init__(self, state):
        self.state = Path(state)
        self.url = ''
        self.bearer = secrets.token_urlsafe(32)
        self.pair_code = None
        self.plan = None
        self.tasks = set()
        # Keep a running server's protocol and assets together across upgrades.
        self.assets = {name: (ASSETS / name).read_bytes() for name in ('panel.html','panel.js','panel.css','brand.css')}

    def pair(self):
        self.pair_code = secrets.token_urlsafe(32)
        return self.url + '/#pair=' + self.pair_code

    async def snapshot(self):
        config = json.loads((self.state / 'config.json').read_text())
        editor = await asyncio.to_thread(browser_editor.runtime, self.state)
        job = job_status(self.state)
        network = job.get('network_before', config.get('network', 'allowlist')) if job and job['status'] == 'running' else config.get('network', 'allowlist')
        controls = job.get('controls_before') if job and job['status'] == 'running' else None
        return {'name': self.state.name, 'network': network,
                'controls': controls or settings_controls.describe(config,self.state),
                'editor': editor['url'] if editor else None, 'job': job}

    async def preview(self, body):
        if set(body) != {'section','values'} or not isinstance(body['section'],str):
            raise ValueError('Choose a settings section and its values, then review the change.')
        return await asyncio.to_thread(self.prepare_preview, body)

    def prepare_preview(self, body):
        import tempfile
        # Translating a partial form and reviewing its merged options is one
        # operation. The CLI inherits this lock; unrelated callers cannot edit
        # the saved options between translation and revision capture.
        with operation_locks.sandbox(self.state.name, 'review browser settings for'):
            config = json.loads((self.state/'config.json').read_text())
            prepared = settings_controls.prepare(config,self.state,body['section'],body['values'])
            with tempfile.TemporaryDirectory(prefix='preview-', dir=directory(self.state)) as temporary:
                path = Path(temporary) / 'settings.json'
                if prepared['operation'] == 'update':
                    write_json(path,prepared['changes'])
                    argv = ['update',self.state.name,'--settings-file',str(path),'--preview']
                else:
                    argv = ['install-system-packages',self.state.name,'--preview',*prepared['packages']]
                result = subprocess.run(command(self.state,*argv),capture_output=True,text=True,
                                        **operation_locks.factory_child(self.state.name))
            if result.returncode:
                raise ValueError(f'Cannot review {prepared["title"]} (exit {result.returncode}): '
                                 + (result.stderr or result.stdout).strip())
            plan = {**prepared, **json.loads(result.stdout), 'id': secrets.token_hex(16)}
            plan.setdefault('completion', prepared['title'] + ' updated.')
            if plan['runtime_effect'] == 'none' and not prepared.get('changes'):
                plan['notes'] = ['No ' + prepared['title'].lower() + ' settings changed.']
            return plan

    async def route(self, target, body):
        if target == '/pair':
            if set(body) != {'code'} or not isinstance(body['code'], str) or not self.pair_code or not secrets.compare_digest(body['code'], self.pair_code):
                raise PermissionError('This browser link has expired or was already used. Open the sandbox again from Factory.')
            self.pair_code = None
            return {'token': self.bearer}
        if target == '/state' and body == {}:
            return await self.snapshot()
        if target == '/preview':
            # A superseded request cannot publish a plan after a later request.
            owner = object()
            self.plan = owner
            plan = await self.preview(body)
            if self.plan is not owner:
                raise ValueError('A newer review replaced this one. Review the selected settings again.')
            self.plan = plan
            return {key:plan[key] for key in ('id','title','notes','runtime_effect','summary','packages') if key in plan}
        if target == '/apply':
            if not isinstance(self.plan, dict) or body != {'plan': self.plan['id']}:
                raise ValueError('This review is no longer current. Review the change again before applying.')
            from . import settings_plan
            settings_plan.check(self.state, self.plan['revision'])
            if self.plan['runtime_effect'] == 'none':
                notes = self.plan['notes']
                self.plan = None
                return {'job': None, 'notes': notes}
            job = start_job(self.state, self.plan)
            self.plan = None
            return {'job': job}
        raise ValueError(f'Unknown browser control request {target!r}. Reload the Factory toolbar.')

    def response_headers(self):
        # Reading the runtime record is sufficient for CSP. Readiness remains
        # checked independently by snapshot(); these origins carry no authority.
        origins = []
        try:
            runtime = json.loads((self.state / 'browser-editor-runtime.json').read_text())
            origins = [runtime[key] for key in ('url', 'hub_url') if runtime.get(key)]
        except (OSError, ValueError, KeyError):
            try:
                saved = json.loads((self.state / 'editor-address.json').read_text())
                if saved.get('port'):
                    origins = [('https' if self.url.startswith('https:') else 'http') + '://' +
                               editor_settings.authority(saved['address'], saved['port'])]
            except (OSError, ValueError, KeyError):
                pass
        frame_sources = ' '.join(origins) or "'none'"
        return {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                'Referrer-Policy': 'no-referrer', 'Cross-Origin-Opener-Policy': 'same-origin',
                'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                    "frame-src " + frame_sources + "; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await self.reply(reader, writer)
        except asyncio.CancelledError:
            # Shutdown must not wait for a TLS peer's close-notify response.
            writer.transport.abort()
            raise
        finally:
            self.tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def reply(self, reader, writer):
        data = b'{"error":"Browser control request was interrupted. Retry the request."}'
        status, content_type = 200, 'application/json'
        try:
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 60)
            parsed = urlsplit(self.url)
            try:
                headers = browser_editor.validate_head(head, parsed.netloc, parsed.scheme)
            except ValueError as error:
                raise ValueError(f'Browser control request rejected: {error}. Open the sandbox again from Factory.') from error
            method, target, _ = head.decode('ascii').split('\r\n', 1)[0].split(' ')
            if method == 'GET' and target in ('/', '/panel.js', '/panel.css', '/brand.css'):
                name = 'panel.html' if target == '/' else target[1:]
                content_type = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}[name.rsplit('.', 1)[1]]
                data = self.assets[name]
            elif method == 'POST':
                if headers.get('origin') != self.url or headers.get('content-type') != 'application/json':
                    raise PermissionError('Factory controls require a request from this toolbar with JSON content.')
                if target != '/pair' and not secrets.compare_digest(headers.get('authorization',''), 'Bearer ' + self.bearer):
                    raise PermissionError('This page is not connected to Factory. Open the sandbox again from Factory.')
                length = int(headers.get('content-length', '0'))
                # Pair is the sole unauthenticated POST and carries one fixed
                # 43-character code. Authenticated settings have no length cap.
                if target == '/pair' and length > 256:
                    raise ValueError('Pairing requires a JSON object containing only the 43-character code from the Factory link.')
                body = json.loads(await asyncio.wait_for(reader.readexactly(length), 60))
                if not isinstance(body, dict):
                    raise ValueError('Browser control request must be a JSON object.')
                data = json.dumps(await self.route(target, body)).encode()
            else:
                status, data = 404, b'{"error":"No such browser control. Reload the Factory toolbar."}'
        except (PermissionError, ValueError, OSError, KeyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as error:
            status = 403 if isinstance(error, PermissionError) else 400
            data = json.dumps({'error': str(error) or 'Browser control request timed out while reading HTTP headers or body. Retry the request.'}).encode()
        except Exception as error:
            status, data = 500, json.dumps({'error': f'Factory browser control failed: {error}. Check browser-controls/server.log.'}).encode()
        headers = self.response_headers()
        headers.update({'Content-Type': content_type, 'Content-Length': str(len(data)), 'Connection': 'close'})
        writer.write((f'HTTP/1.1 {status} Response\r\n' + ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n').encode() + data)
        await writer.drain()



async def serve(state, ready):
    shell = Shell(state)
    preferences = editor_settings.load(state)
    address, editor_port = editor_settings.connection(state, preferences)
    # Reserve through the editor's own address policy so both origins use the
    # same host, without treating an explicit address as its automatic choice.
    editor_settings.remember_connection(state, preferences, address, editor_port)
    saved_path = state / 'editor-address.json'
    saved = json.loads(saved_path.read_text()) if saved_path.exists() else {}
    previous = urlsplit(saved.get('toolbar_origin', ''))
    port = previous.port if previous.hostname == address else 0
    context = editor_settings.tls_context(preferences)
    # Do not silently change a saved toolbar address: a collision is actionable.
    server = await asyncio.start_server(shell.handle, host_platform.listen_address(address), port or 0, ssl=context, limit=16384)
    shell.url = ('https' if context else 'http') + '://' + editor_settings.authority(address, server.sockets[0].getsockname()[1])
    write_json(saved_path, {**saved, 'toolbar_origin': shell.url})
    task = asyncio.current_task()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)
    socket_path = host_platform.control_path(state / 'browser-controls')
    socket_path.unlink(missing_ok=True)

    async def ipc(reader, writer):
        request = asyncio.current_task()
        shell.tasks.add(request)
        try:
            action = (await reader.readline()).decode().strip()
            if action == 'pair':
                writer.write((shell.pair() + '\n').encode())
            elif action == 'stop':
                writer.write(b'stopping\n')
                task.cancel()
            await writer.drain()
        finally:
            writer.close()
            shell.tasks.discard(request)

    ipc_server = await asyncio.start_unix_server(ipc, str(socket_path))
    socket_path.chmod(0o600)
    identity = state.stat().st_ino
    try:
        ready(shell.pair())
        while state.exists() and state.stat().st_ino == identity:
            os.utime(socket_path, None)
            os.utime(socket_path.parent, None)
            await asyncio.sleep(1)
    finally:
        server.close()
        ipc_server.close()
        requests = list(shell.tasks)
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        if hasattr(server, 'abort_clients'):
            server.abort_clients()
        # Unfinished TLS handshakes have no handler to cancel. Leave time within
        # stop()'s existing 15-second allowance for the server process to exit;
        # closing its remaining sockets cannot discard an accepted job.
        try:
            await asyncio.wait_for(asyncio.gather(server.wait_closed(), ipc_server.wait_closed()), 5)
        except asyncio.TimeoutError:
            pass
        socket_path.unlink(missing_ok=True)


def main():
    action, state, value, lock_fd = sys.argv[1:]
    state = Path(state)
    if action == 'apply':
        run_job(state, value, int(lock_fd))
        return
    with os.fdopen(int(lock_fd), 'a+'):
        (directory(state) / 'server.pid').write_text(str(os.getpid()))
        def ready(url):
            os.write(int(value), (url + '\n').encode())
            os.close(int(value))
        try:
            asyncio.run(serve(state, ready))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


if __name__ == '__main__':
    main()
