"""Cross-agent copies require an explicit preview and a stopped destination."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import session_transfer


class SessionTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / 'project'
        self.project.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        self.config = dict(name='target', primary_agent='pi', engine='podman',
                           workspace=str(self.project), uid=1000, gid=1000)
        (self.state / 'config.json').write_text(json.dumps(self.config))
        self.transfer = session_transfer.Transfer.__new__(session_transfer.Transfer)
        self.transfer.plan = None
        self.transfer.rows = [{'path': 'sessions/chat.jsonl', 'title': 'Earlier conversation'}]
        self.transfer.config = {**self.config, 'name': 'source', 'primary_agent': 'claude'}

    def test_apply_requires_review_and_detects_changed_target(self):
        with self.assertRaisesRegex(ValueError, 'Preview'):
            self.transfer.apply()
        with patch.object(session_transfer, 'build'), patch.object(self.transfer, 'run', return_value={'warnings': []}) as run:
            self.transfer.preview('sessions/chat.jsonl', self.config, self.state)
            request = run.call_args.args[0]
            self.assertEqual(request['target_agent'], 'pi')
            self.assertEqual(request['cwd'], '/workspace')
            self.assertEqual(request['operation'], 'preview')
            session_id = request['session_id']
            (self.state / 'config.json').write_text(json.dumps({**self.config, 'primary_agent': 'codex'}))
            with self.assertRaisesRegex(ValueError, 'settings changed'):
                self.transfer.apply()
            self.assertEqual(run.call_count, 1)
            (self.state / 'config.json').write_text(json.dumps(self.config))
            self.transfer.apply()
            self.assertEqual(run.call_args.args[0]['session_id'], session_id)
            self.assertEqual(run.call_args.args[0]['operation'], 'apply')
            self.assertIsNone(self.transfer.plan)

    def test_target_antigravity_and_unlisted_chat_report_error(self):
        with self.assertRaisesRegex(ValueError, 'Antigravity'):
            self.transfer.preview('sessions/chat.jsonl', {**self.config, 'primary_agent': 'agy'}, self.state)
        with self.assertRaisesRegex(ValueError, 'Choose a readable'):
            self.transfer.preview('../chat.jsonl', self.config, self.state)

    def test_running_pi_destination_is_rejected_before_conversion(self):
        self.transfer.source = self.project
        self.transfer.agent = 'claude'
        with patch.object(session_transfer.chat_history, 'archive_command'), \
             patch.object(session_transfer.sandbox, 'inspect', return_value={'State': {'Status': 'running'}}), \
             patch.object(session_transfer.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Stop the destination'):
                self.transfer.run({'operation': 'apply'}, self.config)
            run.assert_not_called()


class SessionTransferUITests(unittest.IsolatedAsyncioTestCase):
    async def test_choose_preview_error_and_copy_fit_a_narrow_terminal(self):
        from render_session_transfer import check
        await check(None, (80, 24), False)
