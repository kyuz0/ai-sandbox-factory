"""Stop update preparation without interrupting changes to an existing sandbox."""
import os
import queue
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from . import host_platform, job_progress


class UpdateCancelled(Exception):
    pass


class StopFailed(ValueError):
    pass


def begin_apply(gate):
    """An atomic claim decides whether to cancel or start applying changes."""
    if gate is None:
        return True
    try:
        gate.mkdir()
    except FileExistsError:
        return False
    return True


class Update:
    def __init__(self, progress=None, *, cooperative=False):
        self.progress = progress
        self.cooperative = cooperative
        self._progress_channel = None
        self._lock = threading.Lock()
        self._requested = threading.Event()
        self._gate = None
        self._applied = False
        self._finished = False

    @property
    def can_cancel(self):
        with self._lock:
            return (not self._finished and not self._requested.is_set()
                    and (self._gate is None or not self._gate.exists()))

    @property
    def applying(self):
        with self._lock:
            return self._applied or (not self._requested.is_set()
                                     and self._gate is not None and self._gate.exists())

    def cancel(self):
        with self._lock:
            if self._finished or self._requested.is_set() or not begin_apply(self._gate):
                return False
            self._requested.set()
            return True

    def run(self, command, log, *, env=None, pass_fds=()):
        with self._lock:
            if self._requested.is_set():
                raise UpdateCancelled()
            if self._gate is not None:
                raise ValueError('This update worker has already started.')
            directory = Path(tempfile.mkdtemp(prefix='pi-sandbox-update-'))
            self._gate = directory / 'apply'
        retain_gate = False
        self._progress_channel = job_progress.Channel(self.progress)
        try:
            self._run(command + ['--apply-gate', str(self._gate)], log, env, pass_fds)
        except StopFailed:
            # Even if the worker keeps running, it must never enter the apply phase.
            retain_gate = True
            raise
        finally:
            self._progress_channel.poll()
            self._progress_channel.close()
            with self._lock:
                self._applied = not self._requested.is_set() and self._gate.exists()
                self._finished = True
            if not retain_gate:
                shutil.rmtree(directory)

    def _run(self, command, log, env, pass_fds):
        environment = dict(os.environ if env is None else env,
                           **{job_progress.ENVIRONMENT: str(self._progress_channel.path)})
        process = subprocess.Popen(command, env=environment, pass_fds=pass_fds, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors='replace',
                                   start_new_session=True)
        lines = queue.Queue()

        def read():
            try:
                with process.stdout:
                    for line in process.stdout:
                        lines.put(line.rstrip())
            finally:
                lines.put(None)

        def stop():
            if not host_platform.stop_process_group(process):
                raise StopFailed('Could not stop the update worker. Check process '
                                 f'{process.pid} before starting another update.')

        threading.Thread(target=read, daemon=True).start()
        stopped = False
        output_closed = False
        last_output = ''
        while not output_closed or process.poll() is None:
            self._progress_channel.poll()
            if self._requested.is_set() and not stopped:
                if not self.cooperative:
                    stop()
                stopped = True
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
        with self._lock:
            if not self._requested.is_set():
                self._finished = True
        # Also stop descendants if the worker exited just as cancellation won.
        if self._requested.is_set():
            if not stopped and not self.cooperative:
                stop()
            if self.cooperative and process.wait():
                raise ValueError(f'Profile cancellation could not finish cleanly (worker exit {process.returncode}): '
                                 + (last_output or 'No recovery diagnostic was returned; inspect the job log before retrying.'))
            raise UpdateCancelled()
        code = process.wait()
        if code:
            raise ValueError(f'Update command exited with status {code}: {last_output or "No diagnostic output was returned; inspect the update log."}')
