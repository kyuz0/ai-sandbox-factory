"""A dashboard rebuild and its private output for the lifetime of the TUI."""
import tempfile
from threading import Lock

from .updating import Update, UpdateCancelled


class RebuildJob:
    def __init__(self, name):
        self.name = name
        self.update = Update()
        self.result = None
        self.log_error = None
        self._cancelled = False
        self._lock = Lock()
        self._log = tempfile.TemporaryFile()

    @property
    def phase(self):
        if self.result:
            return self.result
        if self._cancelled:
            return 'Cancelling'
        return 'Applying changes' if self.update.applying else 'Preparing update'

    def cancel(self):
        if self.update.cancel():
            self._cancelled = True

    def write(self, message):
        safe = ''.join(c for c in str(message) if c.isprintable() or c in '\n\t')
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
            task(self.write, self.update)
        except UpdateCancelled:
            self.write('Update cancelled. The previous sandbox was preserved.')
            self.result = 'Cancelled'
        except Exception as error:
            self.write(f'Error: {error}')
            self.result = 'Failed'
        else:
            self.write('Update complete.')
            self.result = 'Complete'

    def close(self):
        self._log.close()
