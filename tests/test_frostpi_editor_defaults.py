"""Factory's browser-editor preference is independent of FrostPi's default."""
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/editor/launch.py'
SEND_ON_ENTER = 'frostpi.composer.sendOnEnter'


class FrostPiEditorDefaultsTests(unittest.TestCase):
    def launch_settings(self, root, agent='pi'):
        # Execute the shipped settings publication, including the actual file
        # reads and writes, without starting an editor or touching a user home.
        tree = ast.parse(SOURCE.read_text())
        start = next(index for index, node in enumerate(tree.body)
                     if isinstance(node, ast.Assign) and any(
                         isinstance(target, ast.Name) and target.id == 'machine'
                         for target in node.targets))
        end = next(index for index in range(start, len(tree.body))
                   if isinstance(tree.body[index], ast.If) and any(
                       isinstance(child, ast.Constant) and child.value == 'viewed2025-09-15-nux'
                       for child in ast.walk(tree.body[index])))
        code = compile(ast.Module(body=tree.body[start:end], type_ignores=[]), str(SOURCE), 'exec')
        configuration = {} if agent is None else {'agent': agent}
        exec(code, {'root': root, 'settings': configuration, 'Path': Path,
                    'json': json, 'os': os, 'retire_claude_wrapper': lambda values: values})
        return json.loads((root / 'data/User/settings.json').read_text())

    def test_fresh_and_existing_pi_editors_explicitly_enable_enter_to_send(self):
        for agent in ('pi', None):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'machine-defaults.json').write_text('{}')
                first = self.launch_settings(root, agent)
                self.assertIs(first[SEND_ON_ENTER], True)
                self.assertEqual(self.launch_settings(root, agent), first)

    def test_existing_user_choice_survives_reopen_and_settings_refresh(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                machine = root / 'machine-defaults.json'
                machine.write_text('{"http.proxySupport": "override"}')
                user = root / 'data/User/settings.json'
                user.parent.mkdir(parents=True)
                user.write_text(json.dumps({SEND_ON_ENTER: enabled, 'editor.fontSize': 17}))
                for proxy in ('override', 'off'):
                    machine.write_text(json.dumps({'http.proxySupport': proxy}))
                    actual = self.launch_settings(root)
                    self.assertIs(actual[SEND_ON_ENTER], enabled)
                    self.assertEqual(actual['editor.fontSize'], 17)
                    self.assertEqual(actual['http.proxySupport'], proxy)

    def test_other_coding_agents_do_not_receive_a_frostpi_preference(self):
        for agent in ('codex', 'claude', 'agy'):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'machine-defaults.json').write_text('{}')
                self.assertNotIn(SEND_ON_ENTER, self.launch_settings(root, agent))


if __name__ == '__main__':
    unittest.main()
