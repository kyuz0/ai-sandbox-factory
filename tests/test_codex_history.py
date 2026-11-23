"""Native Codex chat preservation with synthetic private and workspace homes."""
from contextlib import closing
import io
import json
import os
import select
import subprocess
import time
import uuid
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import codex_history as history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'private/.codex'
        self.source.mkdir(parents=True)
        self.target = self.root / 'workspace/.codex'

    def write(self, name, value=b'chat\n', home=None):
        path = (home or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def export(self):
        stream = io.BytesIO()
        history.export_history(self.source, stream)
        stream.seek(0)
        return stream

    @unittest.skipUnless(os.environ.get('FACTORY_CODEX_HISTORY_BINARY'),
                         'Set FACTORY_CODEX_HISTORY_BINARY to the pinned native Codex executable.')
    def test_native_list_read_and_paginated_database_history_survive_restore(self):
        binary = os.environ['FACTORY_CODEX_HISTORY_BINARY']
        identifier, turn = str(uuid.uuid4()), str(uuid.uuid4())
        timestamp = '2026-11-23T12:00:00Z'
        meta = {'id': identifier, 'session_id': identifier, 'timestamp': timestamp,
                'cwd': '/workspace', 'originator': 'cli', 'cli_version': '0.171.0',
                'source': 'cli', 'model_provider': 'openai', 'history_mode': 'paginated'}
        rows = [{'type': 'session_meta', 'payload': meta},
                {'type': 'event_msg', 'payload': {'type': 'user_message',
                 'message': 'Synthetic offline preservation check', 'images': [],
                 'local_images': [], 'text_elements': []}}]
        name = f'sessions/2026/11/23/rollout-2026-11-23T12-00-00-{identifier}.jsonl'
        self.write(name, ''.join(json.dumps({'timestamp': timestamp, 'ordinal': n, **row}) + '\n'
                                 for n, row in enumerate(rows)).encode())

        def server():
            log = tempfile.TemporaryFile()
            process = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=log, text=True,
                                       env={**os.environ, 'CODEX_HOME': str(self.source)})
            self.addCleanup(log.close)
            def close():
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                process.stdin.close()
                process.stdout.close()
            self.addCleanup(close)
            sequence = 0
            def call(method, params):
                nonlocal sequence
                sequence += 1
                process.stdin.write(json.dumps({'id': sequence, 'method': method, 'params': params}) + '\n')
                process.stdin.flush()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if not select.select([process.stdout], [], [], 1)[0]:
                        continue
                    line = process.stdout.readline()
                    self.assertTrue(line, 'The offline native app-server exited before replying.')
                    reply = json.loads(line)
                    if reply.get('id') == sequence:
                        self.assertNotIn('error', reply)
                        return reply['result']
                self.fail(f'The offline native app-server did not complete {method}.')
            call('initialize', {'clientInfo': {'name': 'factory-history-test', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
            process.stdin.write(json.dumps({'method': 'initialized'}) + '\n')
            process.stdin.flush()
            return call, close

        call, close = server()
        self.assertEqual(call('thread/read', {'threadId': identifier, 'includeTurns': False})['thread']['id'], identifier)
        call('thread/turns/list', {'threadId': identifier, 'limit': 20})
        close()
        # Use the pinned native-created schema. This answer exists only in SQLite,
        # proving that JSONL-only preservation would lose paginated chat content.
        with closing(sqlite3.connect(self.source / 'thread_history_1.sqlite')) as database, database:
            database.execute('insert into thread_turns(thread_id,turn_id,rollout_ordinal,status,final_agent_item_id) '
                             'values (?,?,?,?,?)', (identifier, turn, 1, 'completed', 'synthetic-agent'))
            item = {'type': 'agentMessage', 'id': 'synthetic-agent',
                    'text': 'Synthetic database-only earlier answer', 'phase': 'final_answer'}
            database.execute('insert into thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type) '
                             'values (?,?,?,?,?,?,?)',
                             (identifier, turn, 'synthetic-agent', 2, 1795435200000, json.dumps(item), 'agentMessage'))
        transfer = self.export()
        # Production uses the same /home/node/.codex path in distinct volumes.
        # Keep that path stable here too, without changing the private original.
        self.source.rename(self.root / 'private-original')
        history.restore_history(self.source, transfer)
        call, close = server()
        listed = call('thread/list', {'limit': 20, 'sourceKinds': [], 'modelProviders': [], 'archived': False})
        self.assertIn(identifier, [row['id'] for row in listed['data']])
        self.assertEqual(call('thread/read', {'threadId': identifier, 'includeTurns': False})['thread']['id'], identifier)
        turns = call('thread/turns/list', {'threadId': identifier, 'limit': 20})
        self.assertIn('Synthetic database-only earlier answer', json.dumps(turns))
        close()

    def test_migrate_skips_export_after_marker_and_reports_engine_errors(self):
        from subprocess import CompletedProcess
        with patch.object(history.subprocess, 'run', return_value=CompletedProcess([], 0, b'', b'')) as run:
            self.assertEqual(history.migrate({'engine': 'podman', 'name': 'synthetic'}, 'workspace'),
                             'Codex chats were already preserved.')
            self.assertEqual(run.call_count, 1)
        with patch.object(history.subprocess, 'run', return_value=CompletedProcess([], 125, b'', b'no such container')) as run:
            with self.assertRaisesRegex(ValueError, r'workspace.*exit 125.*no such container'):
                history.migrate({'engine': 'podman', 'name': 'synthetic'}, 'workspace')
            self.assertEqual(run.call_count, 1)

    def test_selective_export_omits_credentials_configuration_and_links(self):
        self.write('sessions/2026/11/23/rollout-session.jsonl')
        self.write('archived_sessions/rollout-archive.jsonl.zst')
        for name in ('auth.json', 'config.toml', 'factory-placeholders.json', 'factory-ca.pem',
                     'sessions/unrelated.txt', '.codex-proxy/mitmproxy-ca.pem'):
            self.write(name, b'synthetic-private-value')
        (self.source / 'sessions/link.jsonl').symlink_to(self.source / 'auth.json')
        (self.source / 'sessions/linked-directory').symlink_to(self.source / '.codex-proxy', target_is_directory=True)
        with tarfile.open(fileobj=self.export()) as archive:
            self.assertEqual(archive.getnames(), ['sessions/2026/11/23/rollout-session.jsonl',
                                                 'archived_sessions/rollout-archive.jsonl.zst'])

    def test_wal_snapshot_preserves_committed_database_history(self):
        path = self.source / 'thread_history_1.sqlite'
        database = sqlite3.connect(path)
        self.addCleanup(database.close)
        database.execute('pragma journal_mode=wal')
        database.execute('create table thread_items (thread_id text, item_json text)')
        database.execute('insert into thread_items values (?, ?)', ('synthetic-thread', 'earlier turn'))
        database.commit()
        self.assertTrue(path.with_name(path.name + '-wal').exists())
        history.restore_history(self.target, self.export())
        with closing(sqlite3.connect(self.target / path.name)) as restored:
            self.assertEqual(restored.execute('select * from thread_items').fetchall(),
                             [('synthetic-thread', 'earlier turn')])
        self.assertEqual(database.execute('select count(*) from thread_items').fetchone(), (1,))

    def test_restore_keeps_native_listing_names_and_history_metadata(self):
        names = ['sessions/rollout-session.jsonl', 'archived_sessions/rollout-old.jsonl',
                 'session_index.jsonl', 'history.jsonl']
        for name in names:
            self.write(name, name.encode())
        with closing(sqlite3.connect(self.source / 'state_5.sqlite')) as database, database:
            database.execute('create table threads (id text, title text)')
            database.execute('insert into threads values (?, ?)', ('synthetic-thread', 'Saved title'))
        self.assertEqual(history.restore_history(self.target, self.export()), 5)
        for name in names:
            self.assertEqual((self.target / name).read_bytes(), name.encode())
        with closing(sqlite3.connect(self.target / 'state_5.sqlite')) as database:
            self.assertEqual(database.execute('select title from threads').fetchone(), ('Saved title',))
        self.assertTrue((self.target / history.MARKER).is_file())
        self.assertTrue((self.source / 'state_5.sqlite').exists())

    def test_conflict_preflight_preserves_both_homes_and_all_existing_chats(self):
        self.write('sessions/rollout-one.jsonl')
        self.write('history.jsonl', b'old history')
        self.write('history.jsonl', b'new history', self.target)
        with self.assertRaisesRegex(ValueError, 'already contains different data'):
            history.restore_history(self.target, self.export())
        self.assertEqual((self.target / 'history.jsonl').read_bytes(), b'new history')
        self.assertFalse((self.target / 'sessions').exists())
        self.assertFalse((self.target / history.MARKER).exists())
        self.assertEqual((self.source / 'history.jsonl').read_bytes(), b'old history')

    def test_install_failure_rolls_back_and_retry_completes(self):
        self.write('sessions/rollout-one.jsonl')
        self.write('sessions/rollout-two.jsonl')
        original = history.os.link
        calls = []
        def fail_second(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('synthetic disk failure')
            return original(*args)
        with patch.object(history.os, 'link', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'synthetic disk failure'):
                history.restore_history(self.target, self.export())
        self.assertFalse(self.target.exists())
        self.assertEqual(history.restore_history(self.target, self.export()), 2)
        self.write('sessions/rollout-new.jsonl', b'new native chat', self.target)
        self.assertEqual(history.restore_history(self.target, self.export()), 0)
        self.assertEqual((self.target / 'sessions/rollout-new.jsonl').read_bytes(), b'new native chat')

    def test_destination_symlink_and_untrusted_archive_entries_are_rejected(self):
        self.write('sessions/rollout-one.jsonl')
        outside = self.root / 'outside'
        outside.mkdir()
        self.target.mkdir(parents=True)
        (self.target / 'sessions').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            history.restore_history(self.target, self.export())
        for name in ('../auth.json', '/auth.json', 'auth.json', 'sessions/../auth.json'):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                entry = tarfile.TarInfo(name)
                archive.addfile(entry, io.BytesIO())
            stream.seek(0)
            with self.assertRaisesRegex(ValueError, 'rejected archive entry'):
                history.restore_history(self.target, stream)
        self.assertEqual(list(outside.iterdir()), [])

    def test_fresh_home_is_unchanged_and_existing_identical_files_are_retained(self):
        self.assertEqual(history.restore_history(self.target, self.export()), 0)
        self.assertFalse(self.target.exists())
        self.write('history.jsonl', b'same')
        target = self.write('history.jsonl', b'same', self.target)
        inode = target.stat().st_ino
        self.assertEqual(history.restore_history(self.target, self.export()), 0)
        self.assertEqual(target.stat().st_ino, inode)
        self.assertTrue((self.target / history.MARKER).exists())


if __name__ == '__main__':
    unittest.main()
