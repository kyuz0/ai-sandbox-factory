"""Cancellation and rollback for one new sandbox, never an existing sandbox."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading

from . import host_platform, job_progress, operation_locks
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


def saved_reservation(state):
    marker = state / MARKER
    if state.is_symlink() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        with marker.open() as handle:
            token = handle.read(49)
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return None
    return token if re.fullmatch(r'[0-9a-f]{48}', token) else None


def reserved(state, token):
    if not token or not owns(state, token) or set(p.name for p in state.iterdir()) != {MARKER}:
        raise ValueError('Creation reservation changed; refusing to use this sandbox state.')


@dataclass(frozen=True)
class ResourceConflicts:
    engine: str
    connection: str | None
    containers: tuple[tuple[str, str], ...]
    volumes: tuple[tuple[str, str], ...]

    def describe(self):
        groups = [
            heading + '\n' + '\n'.join(names)
            for heading, names in (('Containers', [name for name, _ in self.containers]),
                                   ('Volumes', [name for name, _ in self.volumes])) if names
        ]
        return '\n\n'.join(groups)

    def require_available(self):
        if self.containers or self.volumes:
            raise ValueError('This name is already used by container resources. Choose another sandbox name.\n\n'
                             + self.describe())


def resource_conflicts(config):
    """Find exact names on the selected engine without changing those resources."""
    from . import sandbox, sandbox_advisors, sandbox_ssh, inference, aws_access, primary_workspace, connectors
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
    engine = config['engine']
    try:
        listed = sandbox.run([engine, 'ps', '-a', '--no-trunc', '--format', '{{.Names}}\t{{.ID}}'],
                             capture=True, stderr=subprocess.PIPE).stdout.splitlines()
        volume_names = sandbox.run([engine, 'volume', 'ls', '--format', '{{.Name}}'],
                                   capture=True, stderr=subprocess.PIPE).stdout.splitlines()
        matching = sorted(set(volume_names) & wanted['volume'])
        if matching:
            inspected = sandbox.run([engine, 'volume', 'inspect', *matching],
                                    capture=True, stderr=subprocess.PIPE).stdout
            volumes = tuple(sorted((volume['Name'], json.dumps({
                field: volume.get(field) for field in ('CreatedAt', 'Driver', 'Mountpoint', 'Labels', 'Options')
            }, sort_keys=True)) for volume in json.loads(inspected)))
        else:
            volumes = ()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or '').strip()
        raise ValueError(f'Could not check existing {engine} containers and volumes (exit {error.returncode}). '
                         f'Check that {engine} is available and try again.\n{detail}'.rstrip()) from error
    containers = (line.partition('\t')[::2] for line in listed)
    return ResourceConflicts(engine, config.get('podman_connection'),
                             tuple(sorted((name, identifier) for name, identifier in containers if name in wanted['container'])),
                             volumes)


def check_name(state):
    if os.path.lexists(state):
        raise ValueError('A sandbox with this name is already saved. Open it from the sandbox list or choose another name.')


def check_draft(state_root, draft, approved=None):
    """Check before scheduling creation, and remove only resources the user approved."""
    from . import sandbox
    name = draft['name']
    if not SANDBOX_NAME.fullmatch(name):
        raise ValueError(SANDBOX_NAME_ERROR)
    with operation_locks.sandbox(name, 'check name' if approved is None else 'remove conflicts'):
        check_name(state_root / name)
        engine = sandbox.choose_engine(draft['engine'], quiet=True)
        connection = host_platform.check_engine(engine, full=False)
        config = {**draft, 'engine': engine, 'podman_connection': connection}
        current = resource_conflicts(config)
        if approved is None:
            return current
        if current != approved or any(not re.fullmatch(r'[0-9a-f]{64}', identifier) for _, identifier in approved.containers):
            raise ValueError('The conflicting resources changed. Check the name again and review the current list.')
        try:
            if approved.containers:
                sandbox.run([engine, 'rm', '--force', *(identifier for _, identifier in approved.containers)],
                            capture=True, stderr=subprocess.PIPE)
            if approved.volumes:
                if resource_conflicts(config).volumes != approved.volumes:
                    raise ValueError('The conflicting volumes changed. Check the name again and review the current list.')
                sandbox.run([engine, 'volume', 'rm', *(volume for volume, _ in approved.volumes)],
                            capture=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or '').strip()
            raise ValueError(f'Could not remove all the listed {engine} resources (exit {error.returncode}). '
                             f'Check the remaining resources or go back to choose another name.\n{detail}'.rstrip()) from error
        return resource_conflicts(config)


def prepare_resources(state, token):
    """Recheck names before allowing this attempt to start any containers."""
    if not owns(state, token):
        raise ValueError('Creation reservation changed.')
    config = json.loads((state / 'config.json').read_text())
    host_platform.check_engine(config['engine'], config.get('podman_connection'))
    resource_conflicts(config).require_available()
    (state / READY).write_text(token)


class Creation:
    def __init__(self, progress=None):
        self.progress = progress
        self._progress_channel = None
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
        with operation_locks.sandbox(draft['name'], 'create'):
            return self._run_reserved(service, draft, key, log)

    def _run_reserved(self, service, draft, key, log):
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
            self._progress_channel = job_progress.Channel(self.progress)
            try:
                self._worker(service, draft, key, token, log)
                self._progress_channel.poll()
            finally:
                self._progress_channel.close()
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
                        reason = f' Original failure: {failure}' if failure is not None else ''
                        raise ValueError('Creation stopped, but cleanup failed. Saved state was retained. '
                                         'Choose Actions… → Delete sandbox… from the sandbox list. '
                                         + str(error) + reason) from error
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
                                   text=True, errors='replace', start_new_session=True,
                                   **operation_locks.factory_child(draft['name']))
        lines = queue.Queue()

        def write():
            try:
                json.dump({'state': str(service.state), 'draft': draft, 'key': key, 'token': token,
                           'progress_path': str(self._progress_channel.path)}, process.stdin)
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
            last_output = ''
            while True:
                self._progress_channel.poll()
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
                    if line:
                        last_output = line
                    log(line)
            self._progress_channel.poll()
            code = process.wait()
            if code and not self.requested.is_set():
                raise ValueError(f'Creation failed: worker exited with status {code}: {last_output or "No diagnostic output was returned; inspect the creation log."}')
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
    if value.get('progress_path'):
        import os
        os.environ[job_progress.ENVIRONMENT] = value['progress_path']
    try:
        service.create(draft, value['key'], lambda line: print(line, flush=True), creation_token=value['token'])
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Error: {error}', file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
