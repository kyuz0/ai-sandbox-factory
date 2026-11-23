"""The application uses its canonical package, command and state paths."""
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_sandbox_factory import cli, sandbox, updates


class CommandTests(unittest.TestCase):
    def test_launcher_notice_is_consumed_once(self):
        with patch.dict(cli.os.environ, {'AI_FACTORY_STARTUP_NOTICE': 'Opened by a launcher.'}), \
                patch.object(sandbox, 'main') as run:
            cli.main()
            run.assert_called_once_with(startup_notice='Opened by a launcher.')
            self.assertNotIn('AI_FACTORY_STARTUP_NOTICE', cli.os.environ)

    def test_default_state_uses_the_new_product_name(self):
        from ai_sandbox_factory import sandbox_tui
        for notice in ('', 'Opened by a launcher.'):
            with self.subTest(notice=notice), \
                    patch.object(sandbox.sys, 'argv', ['factory']), \
                    patch.object(sandbox.Path, 'home', return_value=Path('/home/example')), \
                    patch.object(sandbox_tui, 'launch') as launch:
                sandbox.main(startup_notice=notice)
                launch.assert_called_once_with(
                    Path('/home/example/.local/state/ai-sandbox-factory'),
                    **({'startup_notice': notice} if notice else {}))

    def test_explicit_state_directory_is_preserved(self):
        from ai_sandbox_factory import sandbox_tui
        with patch.object(sandbox.sys, 'argv', ['factory', '--state-dir', '/tmp/example-state', 'tui']), \
                patch.object(sandbox_tui, 'launch') as launch:
            sandbox.main(startup_notice='Opened by a launcher.')
        launch.assert_called_once_with(Path('/tmp/example-state'), startup_notice='Opened by a launcher.')

    def test_interactive_cli_notice_does_not_change_command_output(self):
        for notice in ('', 'Opened by a launcher.'):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(notice=notice), \
                    patch.object(sandbox.sys, 'argv', ['factory', 'profile', 'list']), \
                    patch.object(sandbox.registry, 'choices', return_value={'base': 'Development'}), \
                    patch.object(sandbox.sys, 'stdout', output), \
                    patch.object(sandbox.sys, 'stderr', errors):
                sandbox.main(startup_notice=notice)
            self.assertEqual(output.getvalue(), 'base: Development\n')
            self.assertEqual(errors.getvalue(), notice + '\n' if notice else '')

    def test_help_uses_the_new_command_and_invalid_arguments_still_fail(self):
        for arguments, status in ((['--help'], 0), (['not-a-command'], 2)):
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(sandbox.sys, 'argv', ['factory', *arguments]), \
                    patch.object(sandbox.sys, 'stdout', output), \
                    patch.object(sandbox.sys, 'stderr', errors), \
                    self.assertRaises(SystemExit) as result:
                sandbox.main()
            self.assertEqual(result.exception.code, status)
            self.assertIn('ai-sandbox-factory', output.getvalue() + errors.getvalue())

    def test_updates_use_the_new_application_package(self):
        self.assertEqual(updates.PACKAGE, 'ai-sandbox-factory')
        self.assertEqual(updates.UPGRADE_COMMAND, ('pipx', 'upgrade', 'ai-sandbox-factory'))
        self.assertEqual(updates.TAGS_URL,
                         'https://api.github.com/repos/kyuz0/ai-sandbox-factory/tags?per_page=20')
