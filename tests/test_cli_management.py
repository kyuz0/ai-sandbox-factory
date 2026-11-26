"""Public CLI management commands, run through the real ``sandbox.main`` parser.

Each test uses private state, registry, configuration and lock directories.
Only host boundaries are replaced: the engine inventory subprocess, the browser
editor process and the pipx updater.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_sandbox_factory import (browser_editor, cli, editor_settings, operation_locks, operations,
                                prompt_library, sandbox, skill_library, updates)

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'synthetic-editor-password-42'


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class CliCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='factory-cli-management-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.locks = self.root / 'locks'
        self.locks.mkdir(mode=0o700)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        for patcher in (patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                                'SANDBOX_REGISTRY_DIR': str(self.root / 'packages')}),
                        patch.object(operation_locks, 'private_runtime_dir', return_value=self.locks)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin=''):
        """Run the installed entry point; return (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        source = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
        code = 0
        with patch.object(sys, 'argv', ['ai-sandbox-factory', '--state-dir', str(self.state), *argv]), \
                patch('sys.stdin', source), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, stdin=''):
        code, out, err = self.cli(*argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        return out, err

    def json_ok(self, *argv, stdin=''):
        out, err = self.ok(*argv, '--json', stdin=stdin)
        return json.loads(out), err  # One JSON value on stdout.

    def fails(self, *argv, stdin='', code=1):
        actual, out, err = self.cli(*argv, stdin=stdin)
        self.assertEqual(actual, code, (out, err))
        self.assertEqual(out, '', 'A failed command must not print a partial result')
        return err

    def file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)

    def sandbox(self, name='demo', agent='pi'):
        state = self.state / name
        state.mkdir()
        profile = sandbox.load_profile('base')
        config = {'name': name, 'engine': 'podman', 'primary_agent': agent, 'workspace': str(self.workspace),
                  'network': 'allowlist', 'memory': '8g',
                  'profile': {'name': 'base', 'path': profile['path'], 'digest': 'rev-1', 'description': 'Base'},
                  'model_providers': [{'name': 'fixture', 'settings': {'model': 'm1'}}]}
        (state / 'config.json').write_text(json.dumps(config))
        (state / 'allowlist.txt').write_text('example.com:443\n')
        return state

    def tree(self, path):
        return {p.relative_to(path).as_posix(): p.read_bytes() if p.is_file() else None for p in path.rglob('*')}

    @contextmanager
    def held_by_another_operation(self, name):
        """Hold the sandbox lock from a separate thread, as another Factory job would."""
        acquired, release = threading.Event(), threading.Event()

        def hold():
            with operation_locks.sandbox(name, 'test'):
                acquired.set()
                release.wait()
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(acquired.wait(10), 'The competing operation did not acquire the lock')
            yield
        finally:
            release.set()
            thread.join()


class ImportBoundary(CliCase):
    def test_cli_commands_do_not_import_textual(self):
        self.sandbox()
        code = ('import json, sys\n'
                'from ai_sandbox_factory import cli\n'
                'results = []\n'
                'for argv in json.loads(sys.argv[1]):\n'
                '    sys.argv = ["ai-sandbox-factory", *argv]\n'
                '    try:\n'
                '        cli.main()\n'
                '    except SystemExit as exit:\n'
                '        results.append(exit.code)\n'
                '    else:\n'
                '        results.append(0)\n'
                'loaded = sorted(m for m in sys.modules if m == "textual" or m.startswith("textual."))\n'
                'print("RESULT " + json.dumps({"codes": results, "textual": loaded[:5]}), file=sys.stderr)\n')
        commands = [[*prefix, *argv] for prefix in (['--state-dir', str(self.state)],) for argv in
                    (['defaults', 'show', '--json'], ['editor', 'show', 'demo', '--json'],
                     ['prompt', 'list', '--json'], ['skill', 'list', '--json'], ['prompt', 'show', 'demo'])]
        result = subprocess.run([sys.executable, '-c', code, json.dumps(commands)], cwd=ROOT, text=True,
                                capture_output=True, env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=120)
        line = next((row for row in result.stderr.splitlines() if row.startswith('RESULT ')), None)
        self.assertIsNotNone(line, result.stderr)
        report = json.loads(line.removeprefix('RESULT '))
        self.assertEqual(report['codes'], [0] * len(commands), result.stderr)
        self.assertEqual(report['textual'], [], 'CLI management commands imported the Textual UI')


class EditorCommands(CliCase):
    def setUp(self):
        super().setUp()
        self.state_dir = self.sandbox()
        self.running = False
        self.stops = []
        active = patch.object(browser_editor, 'active', side_effect=lambda state: self.running)
        stop = patch.object(browser_editor, 'stop', side_effect=self.stop)
        for patcher in (active, stop):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stop(self, state):
        # Record what was saved when the editor was stopped: it must still be the old settings.
        self.stops.append(editor_settings.load(state))
        self.running = False

    def saved(self):
        return editor_settings.load(self.state_dir)

    def test_show_masks_the_password_hash(self):
        editor_settings.save(self.state_dir, {'mode': 'browser', 'password_hash': editor_settings.password_hash(PASSWORD)})
        digest = self.saved()['password_hash']
        value, _ = self.json_ok('editor', 'show', 'demo')
        self.assertIs(value['password_enabled'], True)
        self.assertNotIn('password_hash', value)
        out, _ = self.ok('editor', 'show', 'demo')
        self.assertNotIn(digest, out)
        self.assertNotIn('$argon2', out)

    def test_set_is_partial_and_keeps_the_saved_password(self):
        out, _ = self.ok('editor', 'set', 'demo', '--password-stdin', '--json', stdin=PASSWORD + '\n')
        self.assertEqual(json.loads(out), {'changed': True, 'editor_stopped': False})
        self.assertNotIn(PASSWORD, out)
        digest = self.saved()['password_hash']
        self.assertTrue(digest.startswith('$argon2id$'))

        value, _ = self.json_ok('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'allow_uploads': True}))
        self.assertEqual(value['changed'], True)
        saved = self.saved()
        self.assertEqual((saved['allow_uploads'], saved['password_hash'], saved['mode']), (True, digest, 'browser'))

        value, _ = self.json_ok('editor', 'set', 'demo', '--clear-password')
        self.assertEqual(self.saved()['password_hash'], '')
        self.assertTrue(self.saved()['allow_uploads'])

    def test_unchanged_save_reports_no_change_and_leaves_the_editor_running(self):
        self.running = True
        path = self.file('e.json', {'mode': 'browser', 'allow_uploads': False})
        before = self.tree(self.state_dir)
        value, err = self.json_ok('editor', 'set', 'demo', '--settings-file', path)
        self.assertEqual(value, {'changed': False, 'editor_stopped': False})
        self.assertEqual(self.stops, [])
        self.assertEqual(err, '')
        self.assertEqual(self.tree(self.state_dir), before, 'A no-op save changed saved state')

    def test_changed_save_stops_the_open_editor_first_and_names_the_reopen_command(self):
        self.running = True
        value, err = self.json_ok('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'allow_uploads': True}))
        self.assertEqual(len(self.stops), 1)
        self.assertFalse(self.stops[0]['allow_uploads'], 'The editor must stop before its new settings are saved')
        self.assertTrue(self.saved()['allow_uploads'])
        self.assertEqual(value['editor_stopped'], True)
        self.assertIn('ai-sandbox-factory open demo', value['next_step'])
        self.assertIn('Saved chats are kept', err)

    def test_native_agent_accepts_the_same_clients_as_its_editor_menu(self):
        self.sandbox('native', agent='codex')
        for mode in editor_settings.modes('codex'):
            with self.subTest(mode=mode):
                self.json_ok('editor', 'set', 'native', '--settings-file', self.file('e.json', {'mode': mode}))
                self.assertEqual(json.loads(self.ok('editor', 'show', 'native', '--json')[0])['mode'], mode)

    def test_credentials_cannot_be_supplied_in_the_settings_file(self):
        digest = editor_settings.password_hash(PASSWORD)
        err = self.fails('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'password_hash': digest}))
        self.assertIn('--password-stdin', err)
        self.assertNotIn(digest, err)
        err = self.fails('editor', 'set', 'demo', '--settings-file', '-', '--password-stdin', stdin='{}')
        self.assertIn('stdin cannot contain both', err)
        err = self.fails('editor', 'set', 'demo', '--password-stdin', stdin='\n')
        self.assertIn('--clear-password', err)
        self.fails('editor', 'set', 'demo', '--password-stdin', '--clear-password', code=2)
        self.assertEqual(self.saved(), editor_settings.validate())

    def test_short_password_is_rejected_without_echoing_it(self):
        err = self.fails('editor', 'set', 'demo', '--password-stdin', stdin='short-pw\n')
        self.assertIn('12 characters', err)
        self.assertNotIn('short-pw', err)

    def test_invalid_settings_name_the_rejected_value_and_the_choices(self):
        # Actionable errors identify the rejected field or value and the accepted choices.
        err = self.fails('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'colour': 'blue'}))
        self.assertIn('colour', err)
        err = self.fails('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'mode': 'desktop-app'}))
        self.assertIn('desktop-app', err)
        self.assertIn('browser', err)
        self.assertIn('pi', err)
        self.assertEqual(self.saved(), editor_settings.validate())

    def test_set_is_refused_while_another_operation_holds_the_sandbox(self):
        with self.held_by_another_operation('demo'):
            err = self.fails('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'allow_uploads': True}))
        self.assertIn('another Factory operation is using this sandbox', err)
        self.assertFalse(self.saved()['allow_uploads'])


class PromptCommands(CliCase):
    def test_save_assign_show_and_clear(self):
        state = self.sandbox()
        saved, _ = self.json_ok('prompt', 'save', '--file', self.file('p.json', {'name': ' Review ', 'text': 'Be careful.\n'}))
        (identifier, entry), = saved.items()
        self.assertEqual(entry, {'name': 'Review', 'text': 'Be careful.'})
        self.assertEqual(self.json_ok('prompt', 'list')[0], saved)

        self.assertEqual(self.json_ok('prompt', 'assign', 'demo', '--id', identifier)[0], {'changed': True})
        self.assertEqual(self.json_ok('prompt', 'show', 'demo')[0], entry)
        self.assertTrue((state / 'activity/edited.json').exists())
        self.assertEqual(self.json_ok('prompt', 'assign', 'demo', '--id', identifier)[0], {'changed': False})

        # Editing the library leaves the sandbox's own copy unchanged.
        self.json_ok('prompt', 'save', '--id', identifier, '--file', self.file('p.json', {'name': 'Review', 'text': 'Changed.'}))
        self.assertEqual(self.json_ok('prompt', 'show', 'demo')[0]['text'], 'Be careful.')

        self.assertEqual(self.json_ok('prompt', 'assign', 'demo', '--file',
                                      self.file('custom.json', {'name': 'Custom', 'text': 'Only here.'}))[0], {'changed': True})
        self.assertEqual(self.json_ok('prompt', 'show', 'demo')[0], {'name': 'Custom', 'text': 'Only here.'})
        self.assertEqual(self.json_ok('prompt', 'assign', 'demo', '--clear')[0], {'changed': True})
        self.assertIsNone(self.json_ok('prompt', 'show', 'demo')[0])
        self.assertEqual(self.json_ok('prompt', 'remove', identifier)[0], {'removed': identifier})
        self.assertEqual(self.json_ok('prompt', 'list')[0], {})

    def test_assign_requires_exactly_one_choice(self):
        self.sandbox()
        self.fails('prompt', 'assign', 'demo', code=2)
        self.fails('prompt', 'assign', 'demo', '--clear', '--id', 'x', code=2)

    def test_unknown_prompt_names_the_id_and_the_list_command(self):
        state = self.sandbox()
        err = self.fails('prompt', 'assign', 'demo', '--id', 'not-installed')
        self.assertIn("'not-installed'", err)
        self.assertIn('prompt list', err)
        self.assertIsNone(prompt_library.selection(state))

    def test_assign_is_refused_while_another_operation_holds_the_sandbox(self):
        state = self.sandbox()
        with self.held_by_another_operation('demo'):
            err = self.fails('prompt', 'assign', 'demo', '--file', self.file('p.json', {'name': 'A', 'text': 'B'}))
        self.assertIn('another Factory operation is using this sandbox', err)
        self.assertIsNone(prompt_library.selection(state))


class SkillCommands(CliCase):
    def source(self):
        source = self.root / 'skills-source'
        for name in ('alpha-skill', 'beta-skill'):
            folder = source / name
            folder.mkdir(parents=True)
            (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic {name}\n---\nUse it.\n')
        return str(source)

    def test_discover_install_and_remove_a_local_source(self):
        source = self.source()
        rows, _ = self.json_ok('skill', 'discover', source)
        self.assertEqual([row['folder'] for row in rows], ['alpha-skill', 'beta-skill'])
        self.assertTrue(all('path' not in row for row in rows), 'Discovery exposed a host staging path')

        err = self.fails('skill', 'install', source)
        self.assertIn('alpha-skill, beta-skill', err)
        err = self.fails('skill', 'install', source, '--folder', 'gamma-skill')
        self.assertIn('Missing: gamma-skill', err)
        self.assertEqual(skill_library.entries(), {})

        installed, _ = self.json_ok('skill', 'install', source, '--folder', 'beta-skill')
        self.assertEqual([row['name'] for row in installed], ['beta-skill'])
        self.assertEqual(set(skill_library.entries()), {installed[0]['id']})
        self.assertEqual(self.json_ok('skill', 'remove', installed[0]['id'])[0], {'removed': installed[0]['id']})
        self.assertEqual(skill_library.entries(), {})


class DefaultsCommands(CliCase):
    def test_show_returns_built_in_defaults_before_any_save(self):
        value, _ = self.json_ok('defaults', 'show')
        self.assertEqual(value, operations.load_defaults())
        self.assertFalse(operations.defaults_path().exists(), 'defaults show must not write a file')

    def test_set_merges_top_level_fields_and_persists(self):
        self.json_ok('defaults', 'set', '--settings-file', self.file('d.json', {'memory': '12g', 'network': 'unrestricted'}))
        value, _ = self.json_ok('defaults', 'set', '--settings-file', self.file('d.json', {'subagents': True}))
        self.assertEqual((value['memory'], value['network'], value['subagents']), ('12g', 'unrestricted', True))
        self.assertEqual(self.json_ok('defaults', 'show')[0], value)
        self.assertEqual(operations.load_defaults()['memory'], '12g')

    def test_set_keeps_only_reusable_editor_choices(self):
        editor = {'mode': 'pi', 'allow_uploads': True, 'address': '127.0.0.1', 'port': 9999,
                  'password_hash': editor_settings.password_hash(PASSWORD)}
        value, _ = self.json_ok('defaults', 'set', '--settings-file', self.file('d.json', {'editor': editor}))
        self.assertEqual(value['editor'], {**editor_settings.DEFAULTS, 'mode': 'pi', 'allow_uploads': True})
        self.assertNotIn(editor['password_hash'], operations.defaults_path().read_text())

    def test_invalid_values_leave_saved_defaults_unchanged(self):
        self.json_ok('defaults', 'set', '--settings-file', self.file('d.json', {'memory': '12g'}))
        before = operations.defaults_path().read_text()
        for change in ({'memory': 'lots'}, {'local_model_ports': 'not-a-port'}, ['memory']):
            with self.subTest(change=change):
                self.fails('defaults', 'set', '--settings-file', self.file('d.json', change))
                self.assertEqual(operations.defaults_path().read_text(), before)

    def test_unknown_field_is_named_in_the_error(self):
        err = self.fails('defaults', 'set', '--settings-file', self.file('d.json', {'memmory': '12g'}))
        self.assertIn('memmory', err)
        self.assertFalse(operations.defaults_path().exists())


class ShowCommand(CliCase):
    def setUp(self):
        super().setUp()
        self.state_dir = self.sandbox()
        record = [{'name': 'demo', 'containers': {'dev': 'exited'}, 'ssh_key_state': 'missing', 'config': {}}]
        inventory = patch.object(operations.Service, 'inventory', return_value=record)
        self.inventory = inventory.start()
        self.addCleanup(inventory.stop)
        for target, attribute in ((operations.Service, 'start'), (sandbox, 'up'), (sandbox, 'build'),
                                  (sandbox, 'open_selected')):
            guard = patch.object(target, attribute, side_effect=AssertionError(f'show called {attribute}'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_show_is_read_only_and_does_not_start_the_sandbox(self):
        before = self.tree(self.state_dir)
        value, _ = self.json_ok('show', 'demo')
        self.assertEqual(self.tree(self.state_dir), before)
        self.assertEqual((value['name'], value['agent'], value['containers']), ('demo', 'pi', {'dev': 'exited'}))
        self.assertEqual(value['profile']['revision'], 'rev-1')
        self.assertEqual(value['settings']['model_providers'], {'fixture': {'model': 'm1'}})
        self.assertEqual(value['settings']['allowlist'], 'example.com:443\n')
        self.assertEqual([alert['title'] for alert in value['alerts']], ['SSH key missing'])

    def test_sections_return_only_the_requested_part(self):
        settings, _ = self.json_ok('show', 'demo', '--section', 'settings')
        self.assertEqual(settings['memory'], '8g')
        self.inventory.assert_not_called()
        containers, _ = self.json_ok('show', 'demo', '--section', 'containers')
        self.assertEqual(containers, {'dev': 'exited'})

    def test_missing_sandbox_error_names_it_and_the_list_command(self):
        err = self.fails('show', 'absent')
        self.assertIn('absent', err)
        self.assertIn('list', err)
        self.assertNotIn('Errno', err, 'The missing sandbox was reported as a raw file error')

    def test_invalid_names_are_argument_errors_like_other_sandbox_commands(self):
        # The legacy sandbox commands report an invalid name as a usage error (exit 2).
        legacy = self.fails('down', 'Bad Name', code=2)
        for argv in (('show', 'Bad Name'), ('editor', 'show', 'Bad Name'), ('chat', 'list', 'Bad Name')):
            with self.subTest(argv=argv):
                self.assertIn('lowercase letters', self.fails(*argv, code=2))
        self.assertIn('lowercase letters', legacy)


class ChatCopyConfirmation(CliCase):
    def test_copy_without_a_terminal_needs_preview_or_yes(self):
        self.sandbox()
        self.sandbox('other')
        with patch('ai_sandbox_factory.session_transfer.Transfer') as transfer:
            err = self.fails('chat', 'copy', 'demo', '--to', 'other', '--chat', 'c1')
            self.assertIn('--preview', err)
            self.assertIn('--yes', err)
            err = self.fails('chat', 'copy', 'demo', '--to', 'demo', '--chat', 'c1', '--yes')
            self.assertIn('different destination', err)
        transfer.assert_not_called()

    def test_declined_terminal_confirmation_copies_nothing(self):
        self.sandbox()
        self.sandbox('other')
        with patch('ai_sandbox_factory.session_transfer.Transfer') as transfer:
            transfer.return_value.preview.return_value = {'messages': 3}
            stdin = TerminalInput('n\n')
            out, err = self.ok('chat', 'copy', 'demo', '--to', 'other', '--chat', 'c1', '--json', stdin=stdin)
        self.assertEqual(json.loads(out), {'preview': {'messages': 3}, 'copied': False})
        self.assertIn('[y/N]', err)
        transfer.return_value.apply.assert_not_called()
        transfer.return_value.close.assert_called_once()


class LogsAndSelfUpdate(CliCase):
    def test_logs_pass_options_to_the_engine_and_keep_its_exit_status(self):
        self.sandbox()
        dev = sandbox.names({'name': 'demo'})[0]
        with patch.object(sandbox, 'inspect', return_value={'State': {}}), \
                patch('ai_sandbox_factory.cli_management.subprocess.run',
                      return_value=subprocess.CompletedProcess([], 125)) as run:
            err = self.fails('logs', 'demo', '--tail', '5', '--follow')
        run.assert_called_once_with(['podman', 'logs', '--tail', '5', '--follow', dev])
        self.assertIn('exited with status 125', err)
        with patch.object(sandbox, 'inspect', return_value=None):
            err = self.fails('logs', 'demo', '--container', 'nope')
        self.assertIn("'nope'", err)
        self.assertIn('list --json', err)

    def test_self_update_check_and_non_pipx_installation(self):
        with patch.object(updates, 'installed_version', return_value='2026.11.26'), \
                patch.object(updates, 'available_update', return_value=None):
            self.assertEqual(self.json_ok('self-update', '--check')[0], {'installed': '2026.11.26', 'available': None})
        with patch.object(updates, 'pipx_install', return_value=False), patch.object(updates, 'upgrade') as upgrade:
            err = self.fails('self-update')
        self.assertIn('not managed by pipx', err)
        upgrade.assert_not_called()


if __name__ == '__main__':
    unittest.main()
