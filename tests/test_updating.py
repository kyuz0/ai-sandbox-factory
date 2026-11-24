from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_sandbox_factory import updating


class UpdateTests(unittest.TestCase):
    def test_cancel_before_launch(self):
        update = updating.Update()
        self.assertTrue(update.cancel())
        with self.assertRaises(updating.UpdateCancelled):
            update.run(['unused'], self.fail)
        self.assertFalse(update.cancel())

    def test_cancel_stops_worker_before_replacing_saved_sandbox(self):
        self.exercise('update', cancel=True)
        self.exercise('recreate', cancel=True)

    def test_apply_finishes_after_cancellation_becomes_unavailable(self):
        self.exercise('update', cancel=False)
        self.exercise('recreate', cancel=False)

    def test_failed_stop_leaves_the_apply_gate_closed(self):
        update = updating.Update()
        gate = None

        def cannot_stop(command, *args):
            nonlocal gate
            gate = Path(command[-1])
            self.addCleanup(shutil.rmtree, gate.parent, ignore_errors=True)
            self.assertTrue(update.cancel())
            raise updating.StopFailed('Could not stop fixture worker')

        with patch.object(update, '_run', side_effect=cannot_stop):
            with self.assertRaisesRegex(updating.StopFailed, 'Could not stop'):
                update.run(['unused'], self.fail)
        self.assertTrue(gate.is_dir())
        self.assertFalse(updating.begin_apply(gate))
        self.assertFalse(update.can_cancel)
        self.assertFalse(update.applying)

    def exercise(self, action, *, cancel):
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor() as pool:
            root = Path(tmp)
            state = root / 'fixture'
            state.mkdir()
            config = state / 'config.json'
            original = {'workspace': tmp, 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                        'memory': '4g', 'subagents': False}
            config.write_text(json.dumps(original))
            (state / 'allowlist.txt').write_text('')
            (root / 'settings.json').write_text('{"memory":"8g"}')
            ready = threading.Event()
            control = updating.Update()
            fixture = """
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from ai_sandbox_factory import sandbox

root = Path(sys.argv[1])
cancel = sys.argv[2] == 'True'
sys.argv = sys.argv[2:]

def build(config):
    if cancel:
        subprocess.run([sys.executable, '-c',
                        "import time; print('Ready', flush=True); time.sleep(60)"], check=True)

def down(config, remove):
    (root / 'stopped').touch()
    print('Ready', flush=True)
    while not (root / 'finish').exists():
        time.sleep(0.01)

with patch.object(sandbox, 'build', build), patch.object(sandbox, 'down', down), \
     patch.object(sandbox, 'up'), patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=root):
    try:
        sandbox.main()
    except KeyboardInterrupt:
        sys.exit(130)
"""
            args = ['--state-dir', tmp, action, 'fixture']
            args += ['--settings-file', str(root / 'settings.json')] if action == 'update' else ['--subagents']
            output = []

            def log(line):
                output.append(line)
                if line == 'Ready':
                    ready.set()

            result = pool.submit(control.run, [sys.executable, '-c', fixture, tmp, str(cancel), *args], log)
            try:
                self.assertTrue(ready.wait(10), output)
                self.assertEqual(control.can_cancel, cancel)
                self.assertEqual(control.applying, not cancel)
                self.assertEqual(control.cancel(), cancel)
                if cancel:
                    with self.assertRaises(updating.UpdateCancelled):
                        result.result(20)
                    self.assertEqual(json.loads(config.read_text()), original)
                    self.assertFalse((root / 'stopped').exists())
                else:
                    (root / 'finish').touch()
                    result.result(10)
                    saved = json.loads(config.read_text())
                    self.assertEqual(saved['memory'], '8g' if action == 'update' else '4g')
                    self.assertEqual(saved['subagents'], action == 'recreate')
            finally:
                (root / 'finish').touch()
                control.cancel()
