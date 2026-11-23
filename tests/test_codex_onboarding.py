"""Skip only the native Codex welcome pages already covered by Factory setup."""
import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/editor/launch.py'


class CodexOnboardingTests(unittest.TestCase):
    @staticmethod
    def launch_onboarding(user, agent):
        tree = ast.parse(SOURCE.read_text())
        node = next(node for node in tree.body if isinstance(node, ast.If)
                    and any(isinstance(child, ast.Constant) and child.value == 'viewed2025-09-15-nux'
                            for child in ast.walk(node)))
        code = compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec')
        exec(code, {'user': user, 'settings': {'agent': agent}, 'sqlite3': sqlite3, 'json': json})

    def test_fresh_codex_home_completes_only_the_two_native_welcome_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            user = Path(temporary)
            self.launch_onboarding(user, 'codex')
            with sqlite3.connect(user / 'globalStorage/state.vscdb') as database:
                rows = database.execute('SELECT key, value FROM ItemTable').fetchall()
            database.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 'openai.chatgpt')
            self.assertEqual(json.loads(rows[0][1]), {
                'viewed2025-09-15-nux': True,
                'viewed2025-09-15-full-chatgpt-auth-nux': True})

    def test_existing_native_preferences_and_other_extension_state_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            user = Path(temporary)
            storage = user / 'globalStorage/state.vscdb'
            storage.parent.mkdir()
            existing = {'saved-model': 'native-choice', 'viewed2025-09-15-apikey-auth-nux': False,
                        'viewed2025-09-15-nux': False, 'nested': {'keep': [1, 2]}}
            with sqlite3.connect(storage) as database:
                database.execute('CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)')
                database.executemany('INSERT INTO ItemTable VALUES (?, ?)', [
                    ('openai.chatgpt', json.dumps(existing)), ('other.extension', 'untouched')])
            database.close()
            self.launch_onboarding(user, 'codex')
            self.launch_onboarding(user, 'codex')
            with sqlite3.connect(storage) as database:
                rows = dict(database.execute('SELECT key, value FROM ItemTable'))
            database.close()
            self.assertEqual(rows['other.extension'], 'untouched')
            self.assertEqual(json.loads(rows['openai.chatgpt']), existing | {
                'viewed2025-09-15-nux': True, 'viewed2025-09-15-full-chatgpt-auth-nux': True})

    def test_other_agents_do_not_seed_codex_state(self):
        for agent in ('pi', 'claude', 'agy'):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as temporary:
                user = Path(temporary)
                self.launch_onboarding(user, agent)
                self.assertEqual(list(user.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
