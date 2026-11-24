import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import creation, host_platform, operation_locks, sandbox
from ai_sandbox_factory.sandbox_tui import Service


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        locks = patch('ai_sandbox_factory.operation_locks.private_runtime_dir', return_value=self.root)
        locks.start()
        self.addCleanup(locks.stop)
        self.service = Service(self.root / 'state')
        self.state = self.service.state / 'demo'
        self.draft = {'name': 'demo'}
        self.logs = []

    def test_cancel_before_start_and_existing_state_are_preserved(self):
        job = creation.Creation()
        job.cancel()
        with self.assertRaises(creation.Cancelled):
            job.run(self.service, self.draft, None, self.logs.append)
        self.assertFalse(self.state.exists())
        self.state.mkdir(parents=True)
        (self.state / 'keep').write_text('unchanged')
        with self.assertRaises(FileExistsError):
            creation.Creation().run(self.service, self.draft, None, self.logs.append)
        self.assertEqual((self.state / 'keep').read_text(), 'unchanged')

    def test_cancel_in_snapshot_stage_removes_owned_state_only(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'receipt.txt').write_text('keep')
        job = creation.Creation()
        def worker(*args):
            (self.state / 'providers').mkdir()
            (self.state / 'providers' / 'partial').write_text('partial')
            job.cancel()
        with patch.object(job, '_worker', side_effect=worker), patch.object(self.service, 'run') as run:
            with self.assertRaises(creation.Cancelled):
                job.run(self.service, self.draft, None, self.logs.append)
        run.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual((workspace / 'receipt.txt').read_text(), 'keep')

    def test_cancel_after_start_uses_checked_delete_and_suppresses_worker_error(self):
        job = creation.Creation()
        def worker(service, draft, key, token, log):
            (self.state / creation.READY).write_text(token)
            job.cancel()
            raise ValueError('child terminated')
        def delete(args, log):
            self.assertEqual(args, ['delete', 'demo'])
            self.assertTrue(job.finished)
            import shutil
            shutil.rmtree(self.state)
        with patch.object(job, '_worker', side_effect=worker), patch.object(self.service, 'run', side_effect=delete):
            with self.assertRaises(creation.Cancelled):
                job.run(self.service, self.draft, None, self.logs.append)
        self.assertFalse(job.cancel())
        self.assertFalse(self.state.exists())

    def test_cleanup_failure_retains_state_and_reports_failure(self):
        def worker(service, draft, key, token, log):
            (self.state / creation.READY).write_text(token)
            raise ValueError('engine unavailable during creation')

        job = creation.Creation()
        with patch.object(job, '_worker', side_effect=worker), patch.object(self.service, 'run', side_effect=ValueError('engine unavailable')):
            with self.assertRaisesRegex(ValueError, 'cleanup failed.*engine unavailable'):
                job.run(self.service, self.draft, None, self.logs.append)
        self.assertTrue(job.retry_blocked)
        self.assertTrue((self.state / creation.MARKER).is_file())
        self.assertTrue((self.state / creation.READY).is_file())

    def test_ownership_change_refuses_cleanup(self):
        job = creation.Creation()
        def worker(*args):
            (self.state / creation.MARKER).write_text('other-attempt')
            job.cancel()
        with patch.object(job, '_worker', side_effect=worker), patch.object(self.service, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'ownership changed'):
                job.run(self.service, self.draft, None, self.logs.append)
        run.assert_not_called()
        self.assertTrue(self.state.exists())

    def test_build_failure_cleans_up_then_same_name_can_be_created(self):
        def worker(service, draft, key, token, log):
            (self.state / creation.READY).write_text(token)

        def fail(*args):
            worker(*args)
            raise ValueError('build failed')

        def delete(args, log):
            self.assertEqual(args, ['delete', 'demo'])
            import shutil
            shutil.rmtree(self.state)

        job = creation.Creation()
        with patch.object(job, '_worker', side_effect=fail), patch.object(self.service, 'run', side_effect=delete) as run:
            with self.assertRaisesRegex(ValueError, 'build failed'):
                job.run(self.service, self.draft, None, self.logs.append)
        run.assert_called_once()
        self.assertFalse(job.retry_blocked)
        self.assertFalse(job.cancel())
        self.assertFalse(self.state.exists())

        retry = creation.Creation()
        with patch.object(retry, '_worker', side_effect=worker):
            retry.run(self.service, self.draft, None, self.logs.append)
        self.assertTrue(self.state.is_dir())
        self.assertFalse((self.state / creation.MARKER).exists())
        self.assertFalse((self.state / creation.READY).exists())
        self.assertFalse(retry.cancel())

    def test_preflight_checks_all_companions_and_volumes_before_start(self):
        self.state.mkdir(parents=True)
        (self.state / creation.MARKER).write_text('ours')
        (self.state / 'config.json').write_text(json.dumps({'name': 'demo', 'engine': 'podman'}))
        for collision in ('pi-sandbox-factory-demo', 'pi-sandbox-factory-demo-advisor', 'pi-sandbox-factory-demo-inference-keys'):
            with self.subTest(collision=collision), patch.object(host_platform, 'check_engine'), patch('subprocess.check_output', return_value=collision + '\n'):
                with self.assertRaisesRegex(ValueError, 'Resources already exist'):
                    creation.prepare_resources(self.state, 'ours')
                self.assertFalse((self.state / creation.READY).exists())
        with patch.object(host_platform, 'check_engine'), patch('subprocess.check_output', return_value='unrelated\n'):
            creation.prepare_resources(self.state, 'ours')
        self.assertEqual((self.state / creation.READY).read_text(), 'ours')

    def test_reservation_requires_exact_token_and_empty_owned_directory(self):
        self.state.mkdir(parents=True)
        (self.state / creation.MARKER).write_text('ours')
        creation.reserved(self.state, 'ours')
        with self.assertRaises(ValueError): creation.reserved(self.state, 'other')
        (self.state / 'foreign').touch()
        with self.assertRaises(ValueError): creation.reserved(self.state, 'ours')

    def test_real_process_and_descendant_stop_before_cleanup(self):
        script = self.root / 'worker.py'
        script.write_text('''import subprocess, sys, time
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
print('ready ' + str(p.pid), flush=True)
try:
    p.wait()
except KeyboardInterrupt:
    p.wait()
''')
        job = creation.Creation()
        def log(line):
            self.logs.append(line)
            if line.startswith('ready '):
                self.child_pid = int(line.split()[1])
                job.cancel()
        with patch.object(host_platform, 'python_command', return_value=[sys.executable, str(script)]):
            with self.assertRaises(creation.Cancelled):
                job.run(self.service, self.draft, {'synthetic': 'test-secret-0b328'}, log)
        self.assertFalse(self.state.exists())
        self.assertNotIn('test-secret-0b328', '\n'.join(self.logs))
        with self.assertRaises(ProcessLookupError): os.kill(self.child_pid, 0)

    def test_forced_stop_handles_process_ignoring_interrupts(self):
        code = 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(120)'
        with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, start_new_session=True) as process:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            host_platform.stop_process_group(process, grace=0.1)
            self.assertEqual(process.returncode, -9)

    def test_validation_failure_removes_reservation_and_allows_retry(self):
        for _ in range(2):
            job = creation.Creation()
            with patch.object(job, '_worker', side_effect=ValueError('invalid workspace')):
                with self.assertRaisesRegex(ValueError, 'invalid workspace'):
                    job.run(self.service, self.draft, None, self.logs.append)
            self.assertFalse(self.state.exists())

    def test_missing_podman_reports_install_hint_and_releases_name(self):
        from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, preset
        workspace = self.root / 'project'
        workspace.mkdir()
        draft = preset('base', DEFAULT_SETTINGS)
        name = 'no-podman-' + uuid4().hex[:12]
        draft.update(name=name, workspace=str(workspace), engine='podman')
        # The parent and real CLI child must resolve the same lock path.
        with patch.object(operation_locks, 'private_runtime_dir', host_platform.private_runtime_dir), \
                patch.dict(os.environ, {'PATH': str(self.root)}):
            with self.assertRaisesRegex(ValueError, 'Creation failed'):
                self.service.create(draft, None, self.logs.append, cancellation=creation.Creation())
        self.assertIn('Podman was not found', '\n'.join(self.logs))
        self.assertFalse((self.service.state / name).exists())

    def test_unconfirmed_stop_preserves_state_without_cleanup(self):
        job = creation.Creation()
        def worker(*args):
            job.cancel()
            raise creation.StopFailed('could not stop')
        with patch.object(job, '_worker', side_effect=worker), patch.object(self.service, 'run') as run:
            with self.assertRaises(creation.StopFailed):
                job.run(self.service, self.draft, None, self.logs.append)
        self.assertTrue(self.state.exists())
        run.assert_not_called()

    def test_stop_requires_group_disappearance_and_handles_permission_error(self):
        from unittest.mock import Mock
        process = Mock(pid=123)
        with patch.object(os, 'killpg', return_value=None):
            self.assertFalse(host_platform.stop_process_group(process, grace=0))
        process.wait.assert_not_called()
        with patch.object(os, 'killpg', side_effect=[PermissionError(), ProcessLookupError()]):
            self.assertTrue(host_platform.stop_process_group(process, grace=0.1))
        process.wait.assert_called_once()

    def test_worker_handoff_preserves_options_and_credentials_in_stdin(self):
        import io
        value = {'state': str(self.root), 'draft': {'name': 'demo', '_hosted_changes': {'azure': {'key': 'synthetic'}},
                 '_communication_keys': {'slack': 'synthetic'}, 'extra_folders': [{'name': 'reference'}],
                 'advisors': {'providers': ['codex']}}, 'key': {'exa': 'synthetic'}, 'token': 'ours'}
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(value))), patch.object(Service, 'create') as create:
            creation.main()
        self.assertEqual(create.call_args.args[:2], (value['draft'], value['key']))
        self.assertEqual(create.call_args.kwargs, {'creation_token': 'ours'})
