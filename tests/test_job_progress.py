"""Stage events use a private channel and report the work actually executed."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import job_progress, updating


class ProgressTests(unittest.TestCase):
    def test_only_entered_stages_are_reported_and_failure_keeps_cause(self):
        events = []
        channel = job_progress.Channel(lambda *value: events.append(value))
        try:
            progress = job_progress.Progress(['Build images', 'Start containers'], channel.path)
            with progress.stage('Build images'):
                channel.poll()
            with self.assertRaisesRegex(ValueError, r'Step 2 of 2 \(Start containers\) failed: socket reset'):
                with progress.stage('Start containers'):
                    channel.poll()
                    raise OSError('socket reset')
            channel.poll()
            self.assertEqual(events, [(1, 2, 'Build images'), (2, 2, 'Start containers')])
        finally:
            channel.close()
        self.assertFalse(channel.path.parent.exists())

    def test_no_channel_needed_for_regular_cli(self):
        with patch.dict(os.environ, {}, clear=True):
            with job_progress.Progress(['Prepare']).stage('Prepare'):
                pass

    def test_engine_stdout_cannot_forge_stages(self):
        events, output = [], []
        script = '''import time
from ai_sandbox_factory.job_progress import Progress
print('Step 99 of 100: invented engine output', flush=True)
with Progress(['Build selected images']).stage('Build selected images'):
    time.sleep(0.25)
'''
        updating.Update(progress=lambda *value: events.append(value)).run(
            [sys.executable, '-c', script], output.append)
        self.assertEqual(events, [(1, 1, 'Build selected images')])
        self.assertIn('Step 99 of 100: invented engine output', output)

    def test_worker_failure_reports_status_and_actual_diagnostic(self):
        with self.assertRaisesRegex(ValueError, 'status 7: build transport reset'):
            updating.Update().run([sys.executable, '-c',
                                   'import sys; print("build transport reset"); sys.exit(7)'], lambda line: None)

    def test_creation_plan_adds_only_selected_work(self):
        from ai_sandbox_factory import sandbox_tui
        from ai_sandbox_factory.sandbox_tui import Service, DEFAULT_SETTINGS, preset
        for reserved in (False, True):
            with self.subTest(reserved=reserved), tempfile.TemporaryDirectory() as tmp:
                draft = preset('base', DEFAULT_SETTINGS)
                draft.update(name='demo', workspace=tmp, engine='podman')
                draft['_advisor_imports'] = ['codex', 'claude']
                service = Service(Path(tmp) / 'state')
                events = []
                channel = job_progress.Channel(lambda *event: events.append(event))
                def executed(*args, **kwargs):
                    channel.poll()
                try:
                    with patch.dict(os.environ, {job_progress.ENVIRONMENT: str(channel.path)}), \
                         patch.object(service, 'run', side_effect=executed), \
                         patch.object(service, 'setup', side_effect=executed), \
                         patch.object(sandbox_tui.editor_settings, 'save', side_effect=executed), \
                         patch('ai_sandbox_factory.creation.prepare_resources', side_effect=executed):
                        service.create(draft, None, lambda line: None, creation_token='synthetic' if reserved else None)
                    channel.poll()
                    expected = ['Save sandbox configuration', 'Save connection and editor settings']
                    if reserved:
                        expected += ['Check sandbox resource ownership']
                    expected += ['Build sandbox images', 'Start sandbox containers', 'Configure sandbox services',
                                 'Import codex advisor sign-in', 'Import claude advisor sign-in']
                    self.assertEqual(events, [(i, len(expected), label) for i, label in enumerate(expected, 1)])
                finally:
                    channel.close()

    def test_service_run_redacts_reservation_and_reports_status(self):
        from ai_sandbox_factory.sandbox_tui import Service
        import io
        from unittest.mock import MagicMock
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO('engine rejected image digest\n')
        process.wait.return_value = 23
        output = []
        with patch('ai_sandbox_factory.sandbox_tui.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(ValueError, 'status 23: engine rejected image digest'):
                Service(Path('/tmp')).run(['create', 'demo', '--creation-token', 'synthetic-private-token'], output.append)
        self.assertNotIn('synthetic-private-token', '\n'.join(output))

    def test_creation_lock_covers_worker_failure_and_rollback(self):
        import subprocess
        from ai_sandbox_factory import creation
        from ai_sandbox_factory.sandbox_tui import Service
        name = 'progress-lock-fixture'
        script = ('from ai_sandbox_factory.operation_locks import sandbox\n'
                  'with sandbox("' + name + '", "replace"):\n    pass\n')
        def blocked():
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('another Factory operation', result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(Path(tmp))
            job = creation.Creation()
            cleanup = job.cleanup
            def worker(*args):
                blocked()
                raise ValueError('synthetic build rejection')
            def rollback(*args):
                blocked()
                return cleanup(*args)
            with patch.object(job, '_worker', side_effect=worker), patch.object(job, 'cleanup', side_effect=rollback):
                with self.assertRaisesRegex(ValueError, 'synthetic build rejection'):
                    job.run(service, {'name': name}, None, lambda line: None)
            self.assertFalse((Path(tmp) / name).exists())
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_recreate_plan_omits_build_when_reusing_images(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from ai_sandbox_factory import sandbox
        for building in (False, True):
            with self.subTest(building=building), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / 'config.json').write_text(json.dumps({'engine': 'podman', 'profile': {'path': tmp}}))
                args = SimpleNamespace(command='recreate', profile=None, subagents=True if building else None,
                                       browser=None, apply_gate=None)
                events = []
                channel = job_progress.Channel(lambda *event: events.append(event))
                def executed(*args, **kwargs):
                    channel.poll()
                try:
                    with patch.dict(os.environ, {job_progress.ENVIRONMENT: str(channel.path)}), \
                         patch.object(sandbox, 'image_graph', return_value=nullcontext()), \
                         patch.object(sandbox, 'snapshot_profile', side_effect=lambda *a, **k: executed() or {}), \
                         patch.object(sandbox, 'build', side_effect=executed), \
                         patch.object(sandbox.activity, 'save_config', side_effect=executed), \
                         patch.object(sandbox, 'down', side_effect=executed), \
                         patch.object(sandbox, 'up', side_effect=executed):
                        sandbox.sandbox_command(args, state, None)
                    expected = (['Prepare sandbox sources', 'Build sandbox images'] if building else []) + [
                        'Save sandbox configuration', 'Stop sandbox containers', 'Start sandbox containers']
                    self.assertEqual(events, [(i, len(expected), label) for i, label in enumerate(expected, 1)])
                finally:
                    channel.close()
