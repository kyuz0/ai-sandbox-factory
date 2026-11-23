"""Cancellation and rollback for one new sandbox, never an existing sandbox."""
import json
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading

from . import host_platform
from .sandbox_options import SANDBOX_NAME, SANDBOX_NAME_ERROR

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
    from . import sandbox, sandbox_advisors, sandbox_ssh, inference, aws_access, primary_workspace, connectors
    if not owns(state, token):
        raise ValueError('Creation reservation changed.')
    config = json.loads((state / 'config.json').read_text())
    host_platform.check_engine(config['engine'], config.get('podman_connection'))
    dev, proxy, *volumes = sandbox.names(config)
    advisor, advisor_proxy, *advisor_volumes = sandbox_advisors.names(config)
    ssh, *ssh_volumes = sandbox_ssh.names(config)
    broker, *broker_volumes = inference.names(config)
    wanted = {'container': {dev, proxy, advisor, advisor_proxy, ssh, broker, aws_access.worker_name(config)},
              'volume': set(volumes + advisor_volumes + ssh_volumes + broker_volumes)}
    wanted['container'].update(connectors.worker_name(config, i) for i in config.get('connector_ids', []))
    if primary_workspace.agent(config) != 'pi':
        from . import primary_agents
        for kind, names in primary_agents.resources(config).items():
            wanted[kind].update(names)
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
        self.retry_blocked = False

    def cancel(self):
        with self.lock:
            if self.finished:
                return False
            self.requested.set()
            return True

    def run(self, service, draft, key, log):
        name = draft['name']
        if not SANDBOX_NAME.fullmatch(name):
            raise ValueError(SANDBOX_NAME_ERROR)
        state = service.state / name
        token = secrets.token_hex(24)
        if self.requested.is_set():
            raise Cancelled('Creation cancelled before starting.')
        state.mkdir(parents=True, mode=0o700)  # Atomic reservation; never adopt existing state.
        failure = None
        try:
            (state / MARKER).write_text(token)
            self._worker(service, draft, key, token, log)
        except StopFailed:
            self.retry_blocked = True  # The worker may still be running.
            raise
        except Exception as error:
            failure = error
        finally:
            with self.lock:
                cancelled = self.requested.is_set()
                self.finished = True  # Accepted cancellation rolls back; later clicks do nothing.
            if not self.retry_blocked:
                if cancelled or failure is not None:
                    try:
                        if cancelled or (state / READY).exists():
                            log('Removing containers and private state from this creation attempt…')
                        self.cleanup(service, state, token, log)
                    except Exception as error:
                        self.retry_blocked = True
                        raise ValueError('Creation stopped, but cleanup failed. Saved state was retained. '
                                         'Choose Actions… → Delete sandbox… from the sandbox list. ' + str(error)) from error
                elif owns(state, token):
                    (state / MARKER).unlink()
                    (state / READY).unlink()
        if cancelled:
            raise Cancelled('Creation cancelled. Runtime containers and private state removed; project files and build cache preserved.')
        if failure is not None:
            raise failure

    @staticmethod
    def cleanup(service, state, token, log):
        if not state.exists():
            return
        if not owns(state, token):
            raise ValueError('Creation ownership changed; no resources were removed.')
        ready = state / READY
        if ready.exists() or ready.is_symlink():
            if not ready.is_file() or ready.is_symlink() or ready.read_text() != token:
                raise ValueError('Creation resource ownership changed; no resources were removed.')
            service.run(['delete', state.name], log)
        else:
            # No container launch was permitted before the resource checkpoint.
            shutil.rmtree(state)

    def _worker(self, service, draft, key, token, log):
        # Keys travel only over stdin, never temporary files, argv or environment.
        if self.requested.is_set():
            return
        process = subprocess.Popen(host_platform.python_command('ai_sandbox_factory.creation'),
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
                raise ValueError('Creation failed. See the output above.')
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
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Error: {error}', file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
