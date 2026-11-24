import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import operation_locks


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        runtime_patch = patch.object(operation_locks, 'private_runtime_dir', return_value=self.runtime)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    def child(self, code, **kwargs):
        setup = ('from pathlib import Path\n'
                 'from ai_sandbox_factory import operation_locks as locks\n'
                 f'locks.private_runtime_dir = lambda: Path({str(self.runtime)!r})\n')
        return subprocess.run([sys.executable, '-c', setup + code], capture_output=True,
                              text=True, timeout=30, **kwargs)

    def assert_busy(self):
        result = self.child("with locks.sandbox('demo', 'delete'):\n    print('entered')\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another Factory operation is using this sandbox', result.stderr)
        self.assertNotIn('entered', result.stdout)

    def test_independent_process_blocked_until_outer_context_closes(self):
        with operation_locks.sandbox('demo', 'create'):
            self.assert_busy()
            with operation_locks.sandbox('demo', 'configure'):
                self.assert_busy()
            self.assert_busy()
        result = self.child("with locks.sandbox('demo', 'delete'):\n    print('entered')\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'entered')

    def test_trusted_child_reuses_lock_without_unlocking_parent(self):
        with operation_locks.sandbox('demo', 'create'):
            kwargs = operation_locks.factory_child('demo')
            result = self.child("with locks.sandbox('demo', 'build'):\n    print('entered')\n", **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'entered')
            self.assert_busy()
        self.assertEqual(operation_locks.factory_child('demo'), {})

    def test_worker_can_forward_to_cli_but_ordinary_children_do_not_receive_fd(self):
        cli = ('from pathlib import Path\n'
               'from ai_sandbox_factory import operation_locks as locks\n'
               f'locks.private_runtime_dir = lambda: Path({str(self.runtime)!r})\n'
               "with locks.sandbox('demo', 'up'):\n    print('entered')\n")
        code = f'''import os, subprocess, sys
name, fd = locks._inherited
assert locks._HANDOFF not in os.environ
assert not os.get_inheritable(fd)
ordinary = subprocess.run([sys.executable, '-c',
    'import os,sys; os.fstat(int(sys.argv[1]))', str(fd)], capture_output=True, text=True)
assert ordinary.returncode != 0, ordinary.stdout
assert locks.factory_child('other') == {{}}
result = subprocess.run([sys.executable, '-c', {cli!r}], capture_output=True, text=True,
                        **locks.factory_child('demo'))
assert result.returncode == 0, result.stderr
print(result.stdout.strip())
'''
        # Use a separate worker and CLI process, as the creation workflow does.
        with operation_locks.sandbox('demo', 'create'):
            result = self.child(code, **operation_locks.factory_child('demo'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'entered')
            self.assert_busy()

    def test_wrong_inode_cannot_bypass_lifecycle_lock(self):
        with operation_locks.sandbox('demo', 'create'):
            with tempfile.TemporaryFile() as unrelated:
                env = {**os.environ, operation_locks._HANDOFF: json.dumps(['demo', unrelated.fileno()])}
                result = self.child("with locks.sandbox('demo', 'delete'):\n    print('entered')\n",
                                    env=env, pass_fds=(unrelated.fileno(),))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('does not refer to the sandbox lock file', result.stderr)
            self.assert_busy()

    def test_independent_descriptor_to_same_inode_cannot_bypass_held_lock(self):
        with operation_locks.sandbox('demo', 'create'):
            with operation_locks._path('sandbox:demo').open('rb') as independent:
                env = {**os.environ, operation_locks._HANDOFF: json.dumps(['demo', independent.fileno()])}
                result = self.child("with locks.sandbox('demo', 'delete'):\n    print('entered')\n",
                                    env=env, pass_fds=(independent.fileno(),))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot inherit the Factory operation lock for demo', result.stderr)
            self.assert_busy()

    def test_replaced_lock_inode_rejects_inherited_descriptor(self):
        with operation_locks.sandbox('demo', 'create'):
            kwargs = operation_locks.factory_child('demo')
            path = operation_locks._path('sandbox:demo')
            path.unlink()
            path.touch(mode=0o600)
            result = self.child("with locks.sandbox('demo', 'build'):\n    print('entered')\n", **kwargs)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('does not refer to the sandbox lock file', result.stderr)
