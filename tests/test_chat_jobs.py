"""Chat jobs survive navigation and retain explicit copy/delete confirmation."""
import asyncio
import copy
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Select

from ai_sandbox_factory.chat_history_ui import ChatTransfer, DeleteSandbox
from ai_sandbox_factory.sandbox_jobs import Job
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS
from ai_sandbox_factory.session_transfer_ui import ContinueChat
from test_tui import FakeService


class Snapshot:
    instances = []
    preview_started = None
    preview_release = None
    fail_preview = None
    fail_apply = None

    def __init__(self, config, state):
        self.config = config
        self.agent = 'claude'
        self.rows = [{'title': 'Synthetic conversation', 'path': 'sessions/chat.jsonl'}]
        self.closed = 0
        self.applied = 0
        self.instances.append(self)

    def close(self):
        self.closed += 1

    def preview(self, path, target, state):
        if self.preview_started:
            self.preview_started.set()
            if not self.preview_release.wait(30):
                raise RuntimeError('Synthetic preview was not released')
        if self.fail_preview:
            raise self.fail_preview
        return {'source': {'events': {'message': 3}}, 'dropped_events': {'thinking:unsupported': 1}}

    def apply(self):
        if self.fail_apply:
            raise self.fail_apply
        self.applied += 1


class ChatJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.configs = [dict(name=name, engine='podman', primary_agent=agent, workspace=str(self.root / name))
                        for name, agent in [('source', 'claude'), ('target', 'pi')]]
        self.records = [dict(name=c['name'], dev='exited', config=c, workspace=c['workspace'],
                             profile='base', engine='podman', proxy='exited') for c in self.configs]
        self.service = FakeService(self.records)
        self.service.state = self.root
        self.service.chat_config = lambda name: next(c for c in self.configs if c['name'] == name)
        for target, replacement in (
            ('ai_sandbox_factory.updates.available_update', None),
            ('ai_sandbox_factory.sandbox_tui.load_defaults', copy.deepcopy(DEFAULT_SETTINGS)),
        ):
            mocked = patch(target, return_value=replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        Snapshot.instances = []
        Snapshot.preview_started = Snapshot.preview_release = None
        Snapshot.fail_preview = Snapshot.fail_apply = None
        mocked = patch('ai_sandbox_factory.session_transfer.Transfer', Snapshot)
        mocked.start()
        self.addCleanup(mocked.stop)

    async def ready(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.poll_jobs()
        await pilot.pause()

    async def choose_copy(self, app, pilot):
        await app.push_screen(ContinueChat('source'))
        await self.ready(app, pilot)
        self.assertIsInstance(app.screen, ContinueChat)
        app.screen.query_one('#copy-chat', Select).value = 'sessions/chat.jsonl'
        app.screen.query_one('#copy-target', Select).value = 'target'
        await pilot.pause()

    async def test_hidden_backup_returns_to_explicit_delete_confirmation(self):
        started, release = Event(), Event()
        backup = self.root / 'history.zip'

        def export(name, path):
            self.service.calls.append(['export-chats', name, path])
            started.set()
            if not release.wait(30):
                raise RuntimeError('Synthetic export was not released')
            return Path(path), 3

        self.service.export_chats = export
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await self.ready(app, pilot)
                app.perform('delete', record=self.records[0])
                await pilot.pause()
                await pilot.click('#delete-export-chats')
                app.screen.query_one('#chat-transfer-path', Input).value = str(backup)
                await pilot.click('#chat-transfer-apply')
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                self.assertIsInstance(app.screen, JobDetails)
                job = app.jobs[-1]
                await pilot.click('#job-hide')
                await app.push_screen(ChatTransfer('target', importing=True))
                release.set()
                await self.ready(app, pilot)
                self.assertIsInstance(app.screen, ChatTransfer)
                self.assertEqual(app.screen.sandbox_name, 'target')
                self.assertEqual(job.result, 'Complete')
                self.assertIsNotNone(job.followup)
                await pilot.press('escape')
                app.show_job(job)
                await pilot.pause()
                await pilot.click('#job-continue')
                await pilot.pause()
                self.assertIsInstance(app.screen, DeleteSandbox)
                self.assertIn('history.zip', str(app.screen.query_one('#chat-backup-status').render()))
                self.assertEqual(app.focused.id, 'cancel')
                await pilot.press('enter')
                self.assertEqual([call[0] for call in self.service.calls], ['export-chats'])
            finally:
                release.set()

    async def test_engine_error_keeps_status_output_and_retry_path(self):
        backup = self.root / 'restore.zip'

        def restore(name, path):
            raise subprocess.CalledProcessError(42, ['podman', 'exec'], output='archive read rejected', stderr='connection reset')

        self.service.import_chats = restore
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await app.push_screen(ChatTransfer('source', importing=True, agent='claude', path=str(backup)))
            await pilot.pause()
            await pilot.click('#chat-transfer-apply')
            await self.ready(app, pilot)
            self.assertEqual(app.jobs[-1].result, 'Failed')
            self.assertIsInstance(app.screen, ChatTransfer)
            self.assertEqual(app.screen.query_one('#chat-transfer-path', Input).value, str(backup))
            error = str(app.screen.query_one('#chat-transfer-error').render())
            for detail in ('42', 'archive read rejected', 'connection reset'):
                self.assertIn(detail, error)
                self.assertIn(detail, '\n'.join(app.jobs[-1].read(0)[0]))

    async def test_long_engine_error_keeps_retry_actions_visible(self):
        message = 'Export exited with status 42.\n' + 'Synthetic archive diagnostic.\n' * 40
        for size in ((80, 24), (120, 40), (220, 50)):
            with self.subTest(size=size):
                app = Cockpit(self.root, self.service)
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    await app.push_screen(ChatTransfer('source', path=str(self.root / 'history.zip'), error=message))
                    await pilot.pause()
                    for key in ('#chat-transfer-cancel', '#chat-transfer-apply'):
                        button = app.screen.query_one(key, Button)
                        self.assertEqual(button.region.height, 1)
                        self.assertTrue(app.screen.region.contains_region(button.region))
                    self.assertEqual(app.focused.id, 'chat-transfer-path')
                    self.assertIn(message, str(app.screen.query_one('#chat-transfer-error').render()))

    async def test_hidden_preview_retains_snapshot_and_requires_explicit_apply(self):
        Snapshot.preview_started, Snapshot.preview_release = Event(), Event()
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await self.ready(app, pilot)
                await self.choose_copy(app, pilot)
                snapshot = Snapshot.instances[-1]
                await pilot.click('#copy-next')
                self.assertTrue(await asyncio.to_thread(Snapshot.preview_started.wait, 5))
                job = app.jobs[-1]
                self.assertEqual(job.targets, ('source', 'target'))
                await pilot.click('#job-hide')
                Snapshot.preview_release.set()
                await self.ready(app, pilot)
                self.assertIs(app.screen, app.screen_stack[0])
                self.assertEqual(snapshot.applied, 0)
                self.assertEqual(snapshot.closed, 0)
                self.assertIsNotNone(job.value)
                app.show_job(job)
                await pilot.pause()
                await pilot.click('#job-continue')
                await pilot.pause()
                self.assertIsInstance(app.screen, ContinueChat)
                self.assertTrue(app.screen.previewed)
                self.assertEqual(str(app.screen.query_one('#copy-next', Button).label), 'Create copy')
                self.assertEqual(snapshot.applied, 0)
                self.assertIsNone(job.value)
                await pilot.click('#copy-next')
                await self.ready(app, pilot)
                self.assertEqual(snapshot.applied, 1)
                self.assertEqual(snapshot.closed, 1)
                self.assertIn('Created a new chat', str(app.screen.query_one('#copy-preview').render()))
            finally:
                Snapshot.preview_release.set()

    async def test_pending_preview_cleanup_on_app_close_and_dialog_cancel(self):
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await self.choose_copy(app, pilot)
            first = Snapshot.instances[-1]
            await pilot.press('escape')
            await pilot.pause()
            self.assertEqual(first.closed, 1)
            await self.choose_copy(app, pilot)
            second = Snapshot.instances[-1]
            Snapshot.preview_started, Snapshot.preview_release = Event(), Event()
            try:
                await pilot.click('#copy-next')
                self.assertTrue(await asyncio.to_thread(Snapshot.preview_started.wait, 5))
                await pilot.click('#job-hide')
                Snapshot.preview_release.set()
                await self.ready(app, pilot)
            finally:
                Snapshot.preview_release.set()
            self.assertEqual(second.closed, 0)
        self.assertEqual(second.closed, 1)

    async def test_preview_failure_closes_snapshot_and_preserves_retry_choices(self):
        Snapshot.fail_preview = ValueError('Destination target is running; stop it before copying this chat.')
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await self.choose_copy(app, pilot)
            snapshot = Snapshot.instances[-1]
            await pilot.click('#copy-next')
            await self.ready(app, pilot)
            self.assertEqual(snapshot.closed, 1)
            self.assertEqual(len(Snapshot.instances), 1)
            self.assertIsInstance(app.screen, ContinueChat)
            self.assertEqual(app.screen.query_one('#copy-chat', Select).value, 'sessions/chat.jsonl')
            self.assertEqual(app.screen.query_one('#copy-target', Select).value, 'target')
            self.assertIn('Destination target is running', str(app.screen.query_one('#copy-error').render()))
            self.assertEqual(str(app.screen.query_one('#copy-next', Button).label), 'Retry loading chats')
            await pilot.click('#copy-next')
            await self.ready(app, pilot)
            self.assertEqual(len(Snapshot.instances), 2)
            self.assertEqual(app.screen.query_one('#copy-chat', Select).value, 'sessions/chat.jsonl')
            self.assertEqual(app.screen.query_one('#copy-target', Select).value, 'target')

    async def test_apply_failure_closes_snapshot_without_repeating_apply(self):
        Snapshot.fail_apply = ValueError('Destination target settings changed. Preview the copy again.')
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await self.choose_copy(app, pilot)
            snapshot = Snapshot.instances[-1]
            await pilot.click('#copy-next')
            await self.ready(app, pilot)
            await pilot.click('#copy-next')
            await self.ready(app, pilot)
            self.assertEqual(snapshot.closed, 1)
            self.assertEqual(len(Snapshot.instances), 1)
            self.assertEqual(app.jobs[-1].result, 'Failed')
            self.assertIn('Destination target settings changed', str(app.screen.query_one('#copy-error').render()))

    async def test_destination_conflict_preserves_selected_snapshot(self):
        app = Cockpit(self.root, self.service)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            await self.choose_copy(app, pilot)
            snapshot = Snapshot.instances[-1]
            conflict = Job('target', 'Import chat history · target', 'Importing chats')
            app.jobs.append(conflict)
            await pilot.click('#copy-next')
            self.assertIsInstance(app.screen, ContinueChat)
            self.assertEqual(len(app.jobs), 2)  # Source listing and the competing target job.
            self.assertEqual(snapshot.closed, 0)
            self.assertIn('Import chat history · target is already running',
                          str(app.screen.query_one('#copy-error').render()))
            self.assertEqual(app.screen.query_one('#copy-chat', Select).value, 'sessions/chat.jsonl')
            conflict.result = 'Complete'
