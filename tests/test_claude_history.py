"""Preserve native Claude sessions without exporting private diagnostics."""
import io
import json
import os
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tarfile
import tempfile
import threading
import unittest
import uuid

from ai_sandbox_factory import codex_history as history


class ClaudeHistoryTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('FACTORY_CLAUDE_HISTORY_BINARY'),
                         'Set FACTORY_CLAUDE_HISTORY_BINARY to the pinned native Claude executable.')
    def test_native_resume_reads_preserved_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, restored = root / 'private/.claude', root / 'worker/.claude'
            workspace = root / 'project'
            workspace.mkdir()
            identifier = str(uuid.uuid4())
            project_key = re.sub(r'[^a-zA-Z0-9]', '-', str(workspace))
            project = private / 'projects' / project_key
            project.mkdir(parents=True)
            previous = str(uuid.uuid4())
            records = [
                {'type': 'user', 'uuid': previous, 'parentUuid': None,
                 'message': {'role': 'user', 'content': 'Synthetic preserved earlier question'}},
                {'type': 'assistant', 'uuid': str(uuid.uuid4()), 'parentUuid': previous,
                 'message': {'id': 'synthetic-message', 'type': 'message', 'role': 'assistant',
                             'model': 'claude-sonnet-4-6', 'content': [
                                 {'type': 'text', 'text': 'Synthetic preserved earlier answer'}],
                             'stop_reason': 'end_turn', 'stop_sequence': None,
                             'usage': {'input_tokens': 1, 'output_tokens': 1}}},
            ]
            for record in records:
                record.update(sessionId=identifier, cwd=str(workspace), timestamp='2026-11-23T12:00:00Z',
                              version='2.1.330', isSidechain=False, userType='external')
            (project / (identifier + '.jsonl')).write_text(''.join(json.dumps(row) + '\n' for row in records))
            transfer = io.BytesIO()
            history.export_history(private, transfer, 'claude')
            transfer.seek(0)
            history.restore_history(restored, transfer, 'claude')
            (restored / '.claude.json').write_text(json.dumps({
                'hasCompletedOnboarding': True, 'projects': {str(workspace): {'hasTrustDialogAccepted': True}}}))
            requests = []

            class API(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    response = json.dumps({'id': 'synthetic-result', 'type': 'message', 'role': 'assistant',
                        'model': 'claude-sonnet-4-6', 'content': [{'type': 'text', 'text': 'Fixture completed'}],
                        'stop_reason': 'end_turn', 'stop_sequence': None,
                        'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

            with ThreadingHTTPServer(('127.0.0.1', 0), API) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    env = {**os.environ, 'HOME': str(restored.parent), 'CLAUDE_CONFIG_DIR': str(restored),
                           'ANTHROPIC_API_KEY': 'synthetic-fixture-key',
                           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:' + str(server.server_port)}
                    result = subprocess.run([os.environ['FACTORY_CLAUDE_HISTORY_BINARY'], '--resume', identifier,
                        '--print', '--model', 'claude-sonnet-4-6', '--max-turns', '1', 'Continue the synthetic task.'],
                        cwd=workspace, env=env, capture_output=True, text=True, timeout=120)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            messages = json.dumps([request.get('messages') for request in requests])
            self.assertIn('Synthetic preserved earlier question', messages)
            self.assertIn('Synthetic preserved earlier answer', messages)

    def test_transfer_preserves_native_records_links_and_index_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / 'private/.claude', root / 'worker/.claude'
            project = source / 'projects/-workspace'
            project.mkdir(parents=True)
            records = [
                {'type': 'user', 'uuid': 'user', 'parentUuid': None, 'sessionId': 'session',
                 'cwd': '/workspace', 'message': {'role': 'user', 'content': 'Keep this task.'}},
                {'type': 'progress', 'uuid': 'progress', 'parentUuid': 'user', 'headers': 'old-private-diagnostic'},
                {'type': 'assistant', 'uuid': 'error', 'parentUuid': 'progress', 'isApiErrorMessage': True,
                 'message': {'role': 'assistant', 'content': 'old-private-diagnostic'}},
                {'type': 'assistant', 'uuid': 'answer', 'parentUuid': 'error',
                 'message': {'role': 'assistant', 'content': 'Preserve answer and redact current-private-token'}},
            ]
            transcript = project / 'session.jsonl'
            transcript.write_text(''.join(json.dumps(record) + '\n' for record in records))
            (project / 'sessions-index.json').write_text(json.dumps({'version': 1, 'entries': [{'sessionId': 'session'}]}))
            (source / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'current-private-token'}}))
            (source / 'settings.json').write_text('private settings')
            (project / 'linked.jsonl').symlink_to(source / '.credentials.json')
            original = transcript.read_bytes()
            transfer = io.BytesIO()
            self.assertEqual(history.export_history(source, transfer, 'claude'), 2)
            transfer.seek(0)
            with tarfile.open(fileobj=transfer) as archive:
                self.assertEqual(set(archive.getnames()), {'projects/-workspace/session.jsonl',
                                                         'projects/-workspace/sessions-index.json'})
            transfer.seek(0)
            self.assertEqual(history.restore_history(target, transfer, 'claude'), 2)
            restored = (target / 'projects/-workspace/session.jsonl').read_text()
            self.assertNotIn('old-private-diagnostic', restored)
            self.assertNotIn('current-private-token', restored)
            parsed = [json.loads(line) for line in restored.splitlines()]
            self.assertEqual([row['uuid'] for row in parsed], ['user', 'error', 'answer'])
            self.assertEqual(parsed[1]['parentUuid'], 'user')
            self.assertEqual(parsed[2]['parentUuid'], 'error')
            self.assertIn('Preserve answer', restored)
            self.assertEqual(transcript.read_bytes(), original)
            transfer.seek(0)
            self.assertEqual(history.restore_history(target, transfer, 'claude'), 0)

    def test_conflict_keeps_both_homes_and_no_completion_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / 'source', root / 'target'
            for home, message in ((source, 'original'), (target, 'existing workspace chat')):
                project = home / 'projects/-workspace'
                project.mkdir(parents=True)
                (project / 'session.jsonl').write_text(json.dumps({'type': 'user', 'message': message}) + '\n')
            transfer = io.BytesIO()
            history.export_history(source, transfer, 'claude')
            transfer.seek(0)
            with self.assertRaisesRegex(ValueError, 'different data.*kept unchanged'):
                history.restore_history(target, transfer, 'claude')
            self.assertIn('existing workspace chat', (target / 'projects/-workspace/session.jsonl').read_text())
            self.assertFalse((target / history.MARKER).exists())
