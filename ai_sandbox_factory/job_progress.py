"""Private stage notifications, independent of command and container output."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

ENVIRONMENT = 'AI_SANDBOX_FACTORY_JOB_PROGRESS'


class Progress:
    def __init__(self, labels, path=None):
        self.labels = tuple(labels)
        self.index = 0
        self.path = Path(path or os.environ[ENVIRONMENT]) if path or os.environ.get(ENVIRONMENT) else None

    @contextmanager
    def stage(self, label):
        self.index += 1
        if self.index > len(self.labels) or self.labels[self.index - 1] != label:
            raise ValueError(f'Job stage does not match its execution plan: {label}')
        if self.path:
            # Both files live inside the controller's private temporary directory.
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps([self.index, len(self.labels), label]))
            temporary.replace(self.path)
        try:
            yield
        except Exception as error:
            raise ValueError(f'Step {self.index} of {len(self.labels)} ({label}) failed: {error}') from error


class Channel:
    def __init__(self, callback):
        self.callback = callback
        self.directory = tempfile.TemporaryDirectory(prefix='ai-sandbox-progress-')
        self.path = Path(self.directory.name) / 'stage.json'
        self.previous = None

    def poll(self):
        if self.callback is None or not self.path.exists():
            return
        value = json.loads(self.path.read_text())
        if value != self.previous:
            self.callback(*value)
            self.previous = value

    def close(self):
        self.directory.cleanup()
