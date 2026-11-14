"""Cancellation and rollback for one new sandbox, never an existing sandbox."""
import json
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading

from . import host_platform

MARKER = '.creation-token'
READY = '.creation-resources'


class Cancelled(Exception):
    pass


class StopFailed(Exception):
    pass


def owns(state, token):
    marker = state / MARKER
    return (not state.is_symlink() and marker.is_file() and not marker.is_symlink()
            and marker.read_text() == token)


def reserved(state, token):
    if not token or not owns(state, token) or set(p.name for p in state.iterdir()) != {MARKER}:
        raise ValueError('Creation reservation changed; refusing to use this sandbox state.')


def prepare_resources(state, token):
    """Refuse orphaned resources before this attempt can start any containers."""
    from . import sandbox, sandbox_advisors, sandbox_ssh, inference
    if not owns(state, token):
        raise ValueError('Creation reservation changed.')
    config = json.loads((state / 'config.json').read_text())
    host_platform.check_engine(config['engine'], config.get('podman_connection'))
    dev, proxy, *volumes = sandbox.names(config)
    advisor, advisor_proxy, *advisor_volumes = sandbox_advisors.names(config)
    ssh, *ssh_volumes = sandbox_ssh.names(config)
    broker, *broker_volumes = inference.names(config)
    wanted = {'container': {dev, proxy, advisor, advisor_proxy, ssh, broker},
              'volume': set(volumes + advisor_volumes + ssh_volumes + broker_volumes)}
    for kind, names in wanted.items():
        command = [config['engine'], 'ps', '-a', '--format', '{{.Names}}'] if kind == 'container' else [config['engine'], 'volume', 'ls', '--format', '{{.Name}}']
        existing = set(subprocess.check_output(command, text=True).splitlines()) & names
        if existing:
            raise ValueError('Resources already exist for this name; choose another sandbox name: ' + ', '.join(sorted(existing)))
    (state / READY).write_text(token)


class Creation:
    def __init__(self):
        self.requested = threading.Event()
        self.lock = threading.Lock()
        self.finished = False

    def cancel(self):
        with self.lock:
            if self.finished:
                return False
            self.requested.set()
            return True

    def run(self, service, draft, key, log):
        name = draft['name']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', name):
            raise ValueError('Invalid sandbox name')
        state = service.state / name
        token = secrets.token_hex(24)
        if self.requested.is_set():
            raise Cancelled('Creation cancelled before starting.')
        state.mkdir(parents=True, mode=0o700)  # Atomic reservation; never adopt existing state.
        stop_failed = False
        try:
            (state / MARKER).write_text(token)
            try:
                self._worker(service, draft, key, token, log)
            except StopFailed:
                stop_failed = True
                raise
            except Exception:
                if not self.requested.is_set():
                    raise
        finally:
            with self.lock:
                cancelled = self.requested.is_set()
                self.finished = True  # Accepted cancellation rolls back; later clicks do nothing.
            if stop_failed:
                pass  # The worker may still be running: never race it with cleanup.
            elif cancelled:
                log('Creation stopped. Removing its containers and private state…')
                try:
                    self.cleanup(service, state, token, log)
                except Exception as error:
                    raise ValueError('Creation stopped, but cleanup failed. Saved state was retained; '
                                     'use Delete on this sandbox to retry. ' + str(error)) from error
            elif owns(state, token):
                if not (state / READY).exists():
                    shutil.rmtree(state)  # Validation/preflight failed before any container launch.
                else:
                    (state / MARKER).unlink()
                    (state / READY).unlink()
        if cancelled:
            raise Cancelled('Creation cancelled. Runtime containers and private state removed; project files and build cache preserved.')

    @staticmethod
    def cleanup(service, state, token, log):
        if not state.exists():
            return
        if not owns(state, token):
            raise ValueError('Creation ownership changed; no resources were removed.')
        ready = state / READY
        if ready.exists() and ready.read_text() == token:
            service.run(['delete', state.name], log)
        else:
            # No container launch was permitted before the resource checkpoint.
            shutil.rmtree(state)

    def _worker(self, service, draft, key, token, log):
        # Keys travel only over stdin, never temporary files, argv or environment.
        if self.requested.is_set():
            return
        process = subprocess.Popen(host_platform.python_command('pi_sandbox_factory.creation'),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', start_new_session=True)
        lines = queue.Queue()

        def write():
            try:
                json.dump({'state': str(service.state), 'draft': draft, 'key': key, 'token': token}, process.stdin)
            except (BrokenPipeError, OSError):
                pass  # The worker exited or was cancelled before accepting input.
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass

        def read():
            try:
                for line in process.stdout:
                    lines.put(line.rstrip())
            finally:
                lines.put(None)

        writer = threading.Thread(target=write, daemon=True)
        reader = threading.Thread(target=read, daemon=True)
        writer.start()
        reader.start()
        stopped = False
        unsafe_stop = False
        output_closed = False
        try:
            while True:
                if self.requested.is_set() and not stopped:
                    log('Stopping creation…')
                    if not host_platform.stop_process_group(process):
                        raise StopFailed('Creation could not be stopped. Cleanup was not attempted; '
                                         f'saved state is retained. Check creation process {process.pid} before deleting this sandbox.')
                    stopped = True
                if output_closed and process.poll() is not None:
                    break
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    output_closed = True
                else:
                    log(line)
            code = process.wait()
            if code and not self.requested.is_set():
                raise ValueError('Creation failed. See the log above; any created sandbox is retained for retry.')
        except StopFailed:
            unsafe_stop = True
            raise
        except BaseException:
            if process.poll() is None and not host_platform.stop_process_group(process):
                unsafe_stop = True
                raise StopFailed('Creation worker could not be stopped; cleanup was not attempted. Saved state is retained.')
            raise
        finally:
            if not unsafe_stop and process.poll() is not None:
                writer.join()
                reader.join()
                process.stdout.close()


def main():
    from .sandbox_tui import Service
    value = json.load(sys.stdin)
    service = Service(Path(value['state']))
    draft = value['draft']
    try:
        service.create(draft, value['key'], lambda line: print(line, flush=True), creation_token=value['token'])
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == '__main__':
    main()
