import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from ai_sandbox_factory import chat_history, codex_history, session_archive


def transcript(session_id, cwd='/workspace', text='Existing chat'):
    header = dict(type='session', version=3, id=session_id, cwd=cwd, timestamp='2026-11-20T10:00:00Z')
    rows = [header, dict(type='message', id='first', parentId=None, message={'role': 'user', 'content': text}),
            dict(type='message', id='branch-1', parentId='first', message={'role': 'user', 'content': 'branch 1'}),
            dict(type='message', id='branch-2', parentId='first', message={'role': 'user', 'content': 'branch 2'})]
    return ''.join(json.dumps(row) + '\n' for row in rows).encode()


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source'
        self.destination = self.root / 'destination'
        self.workspace = self.root / 'project'
        (self.workspace / 'app').mkdir(parents=True)
        for home in (self.source, self.destination):
            self.group(home).mkdir(parents=True)
        self.config = {'workspace': str(self.workspace), 'working_directory': 'app'}
        self.archive = self.root / 'chats.zip'

    def group(self, home):
        return home / '.pi/agent/sessions/--workspace--'

    def command(self, home):
        script = Path(session_archive.__file__).read_text()
        return lambda config, operation, *args: [sys.executable, '-I', '-c', script, operation, str(home), *args]

    def native_command(self, home):
        script = Path(codex_history.__file__).read_text()
        return lambda config, operation: [sys.executable, '-I', '-c', script,
                                          'backup' if operation == 'export' else 'import',
                                          config['primary_agent'], str(home)]

    def test_export_errors_keep_the_operation_path_and_original_diagnostic(self):
        import errno
        for error in (FileNotFoundError(errno.ENOENT, 'engine executable missing', 'fixture-engine'),
                      OSError(errno.ENOSPC, 'filesystem metadata exhausted'),
                      PermissionError(errno.EACCES, 'access denied by fixture policy')):
            with self.subTest(error=error), patch.object(chat_history, 'archive_command', side_effect=error):
                with self.assertRaises(ValueError) as raised:
                    chat_history.export_chats({}, self.source, self.archive)
                self.assertIn(str(self.archive), str(raised.exception))
                self.assertIn(str(error), str(raised.exception))
                self.assertFalse(self.archive.exists())

    def test_engine_failure_preserves_exit_status_and_diagnostic(self):
        import subprocess
        with patch.object(chat_history.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['fixture-engine'], 125, stderr=b'cannot mount history: filesystem metadata exhausted')):
            with self.assertRaises(ValueError) as raised:
                chat_history.transfer(['fixture-engine'])
            self.assertIn('fixture-engine exited with status 125', str(raised.exception))
            self.assertIn('cannot mount history: filesystem metadata exhausted', str(raised.exception))

    def test_native_roundtrip_requires_new_same_agent_and_keeps_sign_in_out(self):
        files = {
            'codex': 'sessions/2026/11/23/rollout.jsonl',
            'claude': 'projects/-workspace-app/chat.jsonl',
            'agy': 'antigravity/conversations/chat.pb',
        }
        for agent, relative in files.items():
            with self.subTest(agent=agent):
                config = {**self.config, 'primary_agent': agent}
                backup = self.root / (agent + '.zip')
                folder = self.source / codex_history.HOMES[agent]
                native_file = folder / relative
                native_file.parent.mkdir(parents=True)
                record = b'{"type":"user","message":{"role":"user","content":"Synthetic saved question"}}\n'
                native_file.write_bytes(record)
                (folder / 'auth.json').write_text('fixture private sign-in')
                target_folder = self.destination / codex_history.HOMES[agent]
                target_folder.mkdir()
                (target_folder / codex_history.MARKER).touch()
                with patch.object(chat_history, 'archive_command', side_effect=self.native_command(self.source)):
                    self.assertEqual(chat_history.export_chats(config, self.source, backup), (backup, None))
                    self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                with patch.object(chat_history, 'archive_command', side_effect=self.native_command(self.destination)) as command:
                    wrong_agent = {**config, 'primary_agent': 'claude' if agent == 'codex' else 'codex'}
                    with self.assertRaisesRegex(ValueError, 'This backup is for'):
                        chat_history.import_chats(wrong_agent, backup, state=self.destination)
                    with self.assertRaisesRegex(ValueError, 'different starting folder'):
                        chat_history.import_chats({**config, 'working_directory': ''}, backup, state=self.destination)
                    command.assert_not_called()
                    self.assertEqual(chat_history.import_chats(config, backup, state=self.destination), (1, 0))
                    with self.assertRaisesRegex(ValueError, 'already has chat storage'):
                        chat_history.import_chats(config, backup, state=self.destination)
                self.assertEqual((target_folder / relative).read_bytes(), record)
                self.assertFalse((target_folder / 'auth.json').exists())

    def test_native_transfer_refuses_running_workspace(self):
        config = {**self.config, 'primary_agent': 'codex', 'engine': 'podman', 'name': 'fixture'}
        with patch.object(chat_history.sandbox, 'inspect', return_value={'State': {'Status': 'running'}}):
            with self.assertRaisesRegex(ValueError, 'Stop sandbox'):
                chat_history.archive_command(config, 'export')

    def test_roundtrip_keeps_branches_and_does_not_replace_an_existing_chat(self):
        raw = transcript('shared', text='Source version')
        new = transcript('new')
        (self.group(self.source) / 'existing.jsonl').write_bytes(raw)
        (self.group(self.source) / 'new.jsonl').write_bytes(new)
        (self.source / '.pi/agent/auth.json').write_text('private fixture')
        (self.group(self.source) / 'sidecar.json').write_text('sidecar fixture')
        with patch.object(chat_history, 'archive_command', side_effect=self.command(self.source)):
            self.assertEqual(chat_history.export_chats({}, self.source, self.archive), (self.archive, 2))
            self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                chat_history.export_chats({}, self.source, self.archive)
        with ZipFile(self.archive) as backup:
            self.assertEqual(backup.namelist(), ['sessions/--workspace--/existing.jsonl', 'sessions/--workspace--/new.jsonl'])
            self.assertEqual(backup.read('sessions/--workspace--/new.jsonl'), new)
        existing = self.group(self.destination) / 'different-name.jsonl'
        local = transcript('shared', text='Destination version')
        existing.write_bytes(local)
        with patch.object(chat_history, 'archive_command', side_effect=self.command(self.destination)):
            self.assertEqual(chat_history.import_chats(self.config, self.archive), (1, 1))
            self.assertEqual(chat_history.import_chats(self.config, self.archive), (0, 2))
        self.assertEqual(existing.read_bytes(), local)
        restored, = (self.group(self.destination).parent / '--workspace-app--').glob('*.jsonl')
        original_header, *original_entries = map(json.loads, new.splitlines())
        new_header, *new_entries = map(json.loads, restored.read_bytes().splitlines())
        self.assertEqual(new_header, {**original_header, 'cwd': '/workspace/app'})
        self.assertEqual(new_entries, original_entries)

    def test_import_single_pi_chat(self):
        path = self.root / 'chat.jsonl'
        path.write_bytes(transcript('single'))
        with patch.object(chat_history, 'archive_command', side_effect=self.command(self.destination)):
            self.assertEqual(chat_history.import_chats(self.config, path), (1, 0))

    def test_empty_and_failed_backup_do_not_publish_files(self):
        with patch.object(chat_history, 'archive_command', side_effect=self.command(self.source)):
            with self.assertRaisesRegex(ValueError, 'no saved chats'):
                chat_history.export_chats({}, self.source, self.archive)
            (self.group(self.source) / 'valid.jsonl').write_bytes(transcript('valid'))
            (self.group(self.source) / 'zz-invalid.jsonl').write_text('invalid')
            with self.assertRaisesRegex(ValueError, 'invalid Pi session header'):
                chat_history.export_chats({}, self.source, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob('.chat-backup-*')), [])

    def test_bad_entry_does_not_leave_a_partially_imported_backup(self):
        with ZipFile(self.archive, 'w') as backup:
            backup.comment = session_archive.FORMAT
            backup.writestr('sessions/--workspace--/valid.jsonl', transcript('valid'))
            backup.writestr('sessions/--workspace--/bad.jsonl', b'bad json')
        with patch.object(chat_history, 'archive_command', side_effect=self.command(self.destination)):
            with self.assertRaisesRegex(ValueError, 'invalid Pi session header'):
                chat_history.import_chats(self.config, self.archive)
        self.assertEqual(list(self.group(self.destination).parent.glob('*/*')), [])

    def test_refuses_symlinks_and_traversal(self):
        outside = self.root / 'outside.jsonl'
        outside.write_bytes(transcript('outside'))
        (self.group(self.source) / 'link.jsonl').symlink_to(outside)
        with self.assertRaises(OSError):
            session_archive.export_archive(self.source, io.BytesIO())
        with ZipFile(self.archive, 'w') as backup:
            backup.comment = session_archive.FORMAT
            link = ZipInfo('sessions/--workspace--/link.jsonl')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            backup.writestr(link, str(outside))
        for member in (link, 'sessions/../outside.jsonl', '/sessions/--workspace--/outside.jsonl'):
            with ZipFile(self.archive, 'w') as backup:
                backup.comment = session_archive.FORMAT
                backup.writestr(member, transcript('outside'))
            with self.assertRaisesRegex(ValueError, 'invalid file entry'):
                chat_history.import_chats(self.config, self.archive)
