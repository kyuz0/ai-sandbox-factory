"""Antigravity history snapshots retain native files and exclude private state."""
import io
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from ai_sandbox_factory import codex_history as history


class AntigravityHistoryTests(unittest.TestCase):
    def test_cli_and_hub_histories_keep_committed_sqlite_pages_and_binary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / 'private', Path(temporary) / 'workspace'
            connections = []
            try:
                for app in history.AGY_APPS:
                    folder = source / app / 'conversations'
                    folder.mkdir(parents=True)
                    (folder / 'synthetic.pb').write_bytes(b'\x0a\x04chat')
                    (source / app / 'implicit').mkdir()
                    (source / app / 'implicit/synthetic.pb').write_bytes(b'\x0a\x07context')
                    artifact = source / app / 'brain/synthetic/artifacts/example.py'
                    artifact.parent.mkdir(parents=True)
                    artifact.write_bytes(b'print("preserved native artifact")\n')
                    connection = sqlite3.connect(folder / 'synthetic.db')
                    connections.append(connection)
                    connection.execute('pragma journal_mode=wal')
                    connection.execute('create table turns(message text)')
                    connection.execute("insert into turns values ('Earlier native answer')")
                    connection.commit()
                    for name in ('log/private.log', 'cache/private', 'user_settings.pb', 'installation_id'):
                        path = source / app / name
                        path.parent.mkdir(exist_ok=True)
                        path.write_bytes(b'private fixture')
                stream = io.BytesIO()
                self.assertEqual(history.export_history(source, stream, 'agy'), 8)
                stream.seek(0)
                with tarfile.open(fileobj=stream) as archive:
                    self.assertFalse(any(name.endswith(('-wal', '-shm')) or '/log/' in name
                                         for name in archive.getnames()))
                stream.seek(0)
                self.assertEqual(history.restore_history(target, stream, 'agy'), 8)
                for app in history.AGY_APPS:
                    self.assertEqual((target / app / 'conversations/synthetic.pb').read_bytes(), b'\x0a\x04chat')
                    self.assertEqual((target / app / 'brain/synthetic/artifacts/example.py').read_bytes(),
                                     b'print("preserved native artifact")\n')
                    with sqlite3.connect(target / app / 'conversations/synthetic.db') as restored:
                        self.assertEqual(restored.execute('pragma integrity_check').fetchone(), ('ok',))
                        self.assertEqual(restored.execute('select message from turns').fetchone(), ('Earlier native answer',))
            finally:
                for connection in connections:
                    connection.close()

    def test_private_token_in_binary_history_aborts_before_export_without_printing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            file = source / 'antigravity-cli/conversations/synthetic.pb'
            file.parent.mkdir(parents=True)
            credential = 'synthetic-private-token'
            original = b'x' * (io.DEFAULT_BUFFER_SIZE - 3) + credential.encode() + b'conversation'
            file.write_bytes(original)
            stream = io.BytesIO()
            with self.assertRaisesRegex(ValueError, 'synthetic.pb.*private sign-in token.*source home was retained') as raised:
                history.export_history(source, stream, 'agy', [credential])
            self.assertNotIn(credential, str(raised.exception))
            self.assertEqual(file.read_bytes(), original)
            self.assertEqual(stream.getvalue(), b'')
