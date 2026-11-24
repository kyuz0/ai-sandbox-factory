"""App-owned jobs and their private output for the lifetime of the TUI."""
import tempfile
from threading import Lock

from .creation import Creation, Cancelled
from .updating import Update, UpdateCancelled


class Job:
    cancel_label = None
    cancel_question = None

    def __init__(self, name, title, working):
        self.name = name
        self.targets = (name,) if name else ()
        self.title = title
        self.working = working
        self.result = None
        self.value = None
        self.error = ''
        self.followup = None
        self.retry = None
        self.edit = None
        self.notice = ''
        self.model_disconnected = False
        self.log_error = None
        self._cancelled = False
        self._step = None
        self._lock = Lock()
        self._log = tempfile.TemporaryFile()

    @property
    def phase(self):
        if self._cancelled:
            return self.result or 'Cancelling'
        return self.result or self.working

    @property
    def status(self):
        phase, step = self.phase, self._step
        if step and (not self.result and not self._cancelled or self.result == 'Failed'):
            number, total, label = step
            stage = f'Step {number} of {total} · {label}'
            return f'Failed · {stage}' if self.result else stage
        return 'Complete · Model not connected' if self.result == 'Complete' and self.model_disconnected else phase

    @property
    def can_cancel(self):
        return False

    @property
    def applying(self):
        return False

    def cancel(self):
        return False

    def set_progress(self, number, total, label):
        self._step = (number, total, label)

    def write(self, message):
        safe = ''.join(c for c in str(message) if c.isprintable() or c in '\n\t')
        if 'Model status: Not connected.' in safe:
            self.model_disconnected = True
        with self._lock:
            if self.log_error is None:
                try:
                    self._log.seek(0, 2)
                    self._log.write((safe + '\n').encode('utf-8', errors='replace'))
                    self._log.flush()
                except OSError as error:
                    self.log_error = f'Could not save the complete job output: {error}'

    def read(self, position):
        # Bound work per refresh; the full output stays in the private file.
        with self._lock:
            try:
                self._log.seek(position)
                lines = self._log.readlines(64 * 1024)
                return [line.decode('utf-8', errors='replace').rstrip('\n') for line in lines], self._log.tell()
            except OSError as error:
                self.log_error = f'Could not read the job output: {error}'
                return [], position

    def run(self, task):
        try:
            self.value = task(self.write)
        except (Cancelled, UpdateCancelled) as error:
            self.write(str(error) or 'Update cancelled. The previous sandbox was preserved.')
            self.result = 'Cancelled'
        except Exception as error:
            self.error = str(error)
            self.write(f'Error: {self.error}')
            self.result = 'Failed'
        else:
            self.write(self.completion_message)
            self.result = 'Complete'

    @property
    def completion_message(self):
        return 'Finished.'

    def close(self):
        self._log.close()
        self.discard_followup()

    def discard_followup(self):
        close_value = getattr(self.value, 'close', None)
        if close_value:
            close_value()
        self.value = self.followup = self.retry = self.edit = None


class RebuildJob(Job):
    cancel_label = 'Cancel update'

    def __init__(self, name, title=None):
        super().__init__(name, title or 'Update · ' + name, 'Preparing update')
        self.update = Update(progress=self.set_progress)
        self.cancel_question = (f'Cancel the update for {name}? '
                                'The sandbox will keep its current settings and containers.')

    @property
    def phase(self):
        if self.applying:
            return 'Applying changes'
        return super().phase

    @property
    def applying(self):
        return not self.result and self.update.applying

    @property
    def can_cancel(self):
        return self.result is None and not self.applying and self.update.can_cancel

    def cancel(self):
        if self.can_cancel and self.update.cancel():
            self._cancelled = True
            return True
        return False

    def run(self, task):
        super().run(lambda log: task(log, self.update))

    @property
    def completion_message(self):
        return 'Update complete.'


class CreationJob(Job):
    cancel_label = 'Cancel creation'

    def __init__(self, name, *, preview=None):
        super().__init__(name, 'Create sandbox · ' + name, 'Creating sandbox')
        self.preview = preview
        self.creation = Creation(progress=self.set_progress)
        self.cancel_question = (f'Stop creating {name} and remove the containers and private state from this attempt? '
                                'Your project files and build cache will be kept.')

    @property
    def phase(self):
        return 'Sandbox ready' if self.result == 'Complete' else super().phase

    @property
    def can_cancel(self):
        return self.result is None and not self.creation.finished and not self.creation.requested.is_set()

    def cancel(self):
        if self.can_cancel and self.creation.cancel():
            self._cancelled = True
            return True
        return False

    @property
    def completion_message(self):
        return f'{self.name} is ready.'
