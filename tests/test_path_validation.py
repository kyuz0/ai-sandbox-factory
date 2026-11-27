"""Malformed paths stay editable and never escape as UI or CLI tracebacks."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Select

from ai_sandbox_factory import editor_settings, host_platform, operations, registry, sandbox_ssh
from ai_sandbox_factory.file_browser import FolderPicker, PathField
from ai_sandbox_factory.sandbox_tui import Cockpit, InstallPackage, NewSandbox, SettingsForm, preset
from test_cli_lifecycle import CliCase
from test_tui import FakeService


class PathParsing(unittest.TestCase):
    def test_valid_home_named_home_relative_and_absolute_paths(self):
        self.assertEqual(host_platform.local_path('~'), Path.home())
        self.assertEqual(host_platform.local_path('~/project'), Path.home() / 'project')
        self.assertEqual(host_platform.local_path('relative/project'), Path('relative/project'))
        self.assertEqual(host_platform.local_path('/synthetic/project'), Path('/synthetic/project'))
        with patch('pwd.getpwnam', return_value=SimpleNamespace(pw_dir='/synthetic/user')):
            self.assertEqual(host_platform.local_path('~known/project'), Path('/synthetic/user/project'))

    def test_unresolved_named_home_identifies_the_input_and_correction(self):
        with patch('pwd.getpwnam', side_effect=KeyError('test')):
            for value in ('~test', '~test/folder with spaces'):
                with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                    host_platform.local_path(value)
                self.assertIn(repr(value), str(caught.exception))
                self.assertIn('~/' + value[1:], str(caught.exception))
                self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_unresolved_current_home_has_an_absolute_path_alternative(self):
        with patch('os.path.expanduser', return_value='~'):
            with self.assertRaisesRegex(ValueError, 'absolute path'):
                host_platform.local_path('~')

    def test_windows_conversion_failure_preserves_status_and_diagnostic(self):
        value = r'Z:\project'
        failure = subprocess.CalledProcessError(1, ['wslpath', '-u', value], output='wslpath: Invalid argument\n')
        with patch.object(host_platform, 'is_wsl', return_value=True), \
                patch.object(host_platform.subprocess, 'check_output', side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                host_platform.local_path(value)
        self.assertIn(repr(value), str(caught.exception))
        self.assertIn('status 1', str(caught.exception))
        self.assertIn('wslpath: Invalid argument', str(caught.exception))
        with patch.object(host_platform, 'is_wsl', return_value=True), \
                patch.object(host_platform.subprocess, 'check_output', side_effect=FileNotFoundError('wslpath missing')):
            with self.assertRaisesRegex(ValueError, 'wslpath missing'):
                host_platform.local_path(value)

    def test_registry_path_inputs_use_the_same_validation(self):
        with patch('pwd.getpwnam', side_effect=KeyError('test')):
            for action in (lambda: registry.source_url('~test'), lambda: registry.resolve('profile', '~test')):
                with self.assertRaisesRegex(ValueError, '~/test'):
                    action()
        with self.assertRaises(ValueError):
            registry.source_url('')

    def test_resolve_runtime_error_has_a_path_and_recovery_step(self):
        with patch.object(Path, 'resolve', side_effect=RuntimeError('Symlink loop')):
            with self.assertRaisesRegex(ValueError, 'symbolic links') as caught:
                host_platform.resolve_path('/synthetic/loop')
        self.assertIn('/synthetic/loop', str(caught.exception))
        self.assertIn('Symlink loop', str(caught.exception))

    def test_ssh_key_and_editor_https_paths_use_the_same_validation(self):
        with patch('pwd.getpwnam', side_effect=KeyError('test')):
            with self.assertRaisesRegex(ValueError, '~/test'):
                sandbox_ssh.key_source({}, '~test')
            for field in ('certificate', 'private_key'):
                value = {'certificate': '/synthetic/cert.pem', 'private_key': '/synthetic/key.pem'}
                value[field] = '~test'
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, '~/test'):
                    editor_settings.tls_context(value)


class PathFormTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.enterContext(patch('ai_sandbox_factory.sandbox_tui.load_defaults',
                               side_effect=lambda: copy.deepcopy(operations.DEFAULT_SETTINGS)))
        self.enterContext(patch('ai_sandbox_factory.updates.available_update', return_value=None))
        self.enterContext(patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'registry'),
                                                  'XDG_CONFIG_HOME': str(self.root / 'config')}))

    async def ready(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_create_form_reports_typo_preserves_input_and_can_retry(self):
        for mono in (False, True):
            for size in ((80, 24), (120, 40), (220, 50)):
                with self.subTest(size=size, monochrome=mono), \
                        patch.dict(os.environ, {'NO_COLOR': '1'} if mono else {}), \
                        patch('pwd.getpwnam', side_effect=KeyError('test')):
                    if not mono:
                        os.environ.pop('NO_COLOR', None)
                    app = Cockpit(self.state, FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await self.ready(app, pilot)
                        form = NewSandbox()
                        await app.push_screen(form)
                        form.query_one('#workspace', Input).value = '~test'
                        form.query_one('#name', Input).value = 'test'
                        form.query_one('#continue', Button).press()
                        await self.ready(app, pilot)
                        self.assertIs(app.screen, form)
                        self.assertEqual(form.query_one('#workspace', Input).value, '~test')
                        self.assertIs(app.focused, form.query_one('#workspace'))
                        error = form.query_one('#workspace-error')
                        self.assertTrue(error.display)
                        self.assertIn("'~test'", str(error.render()))
                        self.assertIn("'~/test'", str(error.render()))
                        # Deliver a queued notification after validation, before any real edit.
                        form.post_message(Input.Changed(form.query_one('#workspace', Input), '~test'))
                        await pilot.pause()
                        self.assertTrue(error.display)
                        self.assertTrue(form.query_one('.form').content_region.contains_region(error.region))
                        self.assertTrue(form.region.contains_region(form.query_one('#continue').region))
                        self.assertFalse(app.jobs)
                        form.query_one('#workspace', Input).value = str(self.workspace)
                        await pilot.pause()
                        self.assertFalse(error.display)
                        form.query_one('#continue', Button).press()
                        await self.ready(app, pilot)
                        self.assertIsInstance(app.screen, SettingsForm)
                        self.assertEqual(app.screen.draft['workspace'], str(self.workspace))
                        self.assertFalse(app.jobs)

    async def test_picker_bad_path_keeps_location_then_accepts_correction(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            picker = FolderPicker(self.root)
            await app.push_screen(picker)
            await self.ready(app, pilot)
            field = picker.query_one('#folder-path', Input)
            field.focus()
            field.value = '~test'
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                await pilot.press('enter')
                await pilot.pause()
            self.assertIs(app.screen, picker)
            self.assertEqual(field.value, '~test')
            self.assertEqual(picker.navigation.location, self.root)
            self.assertIn('~/test', str(picker.query_one('#folder-browse-error').render()))
            field.value = str(self.workspace)
            await pilot.press('enter')
            await self.ready(app, pilot)
            self.assertEqual(picker.navigation.location, self.workspace)
            self.assertFalse(picker.query_one('#folder-browse-error').display)

    async def test_picker_initial_error_survives_listing_then_clears_on_navigation(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                picker = FolderPicker('~test', root=self.root)
            await app.push_screen(picker)
            await self.ready(app, pilot)
            self.assertIn('~/test', str(picker.query_one('#folder-browse-error').render()))
            picker.go_to(self.workspace)
            await self.ready(app, pilot)
            self.assertFalse(picker.query_one('#folder-browse-error').display)

    async def test_rooted_subfolder_with_tilde_is_a_literal_name(self):
        folder = self.root / '~test'
        folder.mkdir()
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            field = PathField('literal-subfolder', '~test', title='Choose subfolder', root=self.root)
            await app.screen.mount(field)
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                field.query_one(Button).press()
                await self.ready(app, pilot)
            self.assertIsInstance(app.screen, FolderPicker)
            self.assertEqual(app.screen.navigation.location, folder)
            app.screen.go_to(self.root)
            await self.ready(app, pilot)
            path = app.screen.query_one('#folder-path', Input)
            path.focus()
            path.value = '~test'
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                await pilot.press('enter')
                await self.ready(app, pilot)
            self.assertEqual(app.screen.navigation.location, folder)

    async def test_custom_profile_bad_path_is_reported_without_leaving_the_form(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(app, pilot)
            form = NewSandbox()
            await app.push_screen(form)
            form.query_one('#profile', Select).value = 'custom'
            await pilot.pause()
            form.query_one('#custom-profile', Input).value = '~test'
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                form.query_one('#continue', Button).press()
                await self.ready(app, pilot)
            self.assertIs(app.screen, form)
            self.assertIs(app.focused, form.query_one('#custom-profile'))
            self.assertIn('~/test', str(form.query_one('#new-error').render()))
            self.assertFalse(app.jobs)

    async def test_network_file_typo_preserves_rules_and_can_be_corrected(self):
        from textual.widgets import TextArea
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            draft = preset('base', copy.deepcopy(operations.DEFAULT_SETTINGS))
            draft.update(name='test', workspace=str(self.workspace), allowlist='example.com:443\n')
            form = SettingsForm(draft)
            await app.push_screen(form)
            await self.ready(app, pilot)
            fields = form.query_one('#network-rules-fields')
            fields.query_one('#network-file', Input).value = '~test'
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                fields.query_one('#network-import', Button).press()
                await pilot.pause()
            self.assertIn('~/test', str(fields.query_one('#network-file-error').render()))
            self.assertEqual(fields.query_one('#allowlist', TextArea).text, 'example.com:443\n')
            rules = self.root / 'rules.txt'
            rules.write_text('example.org:443\n')
            fields.query_one('#network-file', Input).value = str(rules)
            fields.query_one('#network-import', Button).press()
            await pilot.pause()
            self.assertFalse(fields.query_one('#network-file-error').display)
            self.assertEqual(fields.query_one('#allowlist', TextArea).text, 'example.org:443\n')

    async def test_browse_and_package_source_do_not_crash_on_the_same_typo(self):
        app = Cockpit(self.state, FakeService([]))
        async with app.run_test() as pilot:
            await self.ready(app, pilot)
            form = NewSandbox()
            await app.push_screen(form)
            form.query_one('#workspace-field').initial = self.root
            form.query_one('#workspace', Input).value = '~test'
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                form.query_one('#browse', Button).press()
                await self.ready(app, pilot)
            self.assertIsInstance(app.screen, FolderPicker)
            self.assertEqual(app.screen.navigation.location, self.root)
            self.assertIn('~/test', str(app.screen.query_one('#folder-browse-error').render()))
            await pilot.press('escape')
            self.assertEqual(form.query_one('#workspace', Input).value, '~test')
            await app.pop_screen()
            package = InstallPackage('profile')
            await app.push_screen(package)
            with patch('pwd.getpwnam', side_effect=KeyError('test')):
                package.query_one('#package-source', Input).value = '~test'
                package.query_one('#package-name', Input).value = 'example'
                await pilot.pause()
                package.query_one('#package-add', Button).press()
                await pilot.pause()
            self.assertIs(app.screen, package)
            self.assertIn('~/test', str(package.query_one('#package-error').render()))
            self.assertEqual(package.query_one('#package-source', Input).value, '~test')


class PathCLI(CliCase):
    def test_create_bad_path_has_a_validation_error_and_no_traceback_or_allocation(self):
        with patch('pwd.getpwnam', side_effect=KeyError('test')):
            code, out, err = self.cli('create', 'demo', '--workspace', '~test', '--start')
        self.assertEqual(code, 1)
        self.assertIn('~/test', err)
        self.assertNotIn('Traceback', out + err)
        self.assertFalse((self.state / 'demo').exists())

    def test_file_and_state_arguments_have_actionable_errors_without_allocation(self):
        commands = (
            ('create', 'demo', '--config', '~test', '--start'),
            ('create', 'demo', '--workspace', str(self.workspace), '--allowlist', '~test', '--start'),
            ('create', 'demo', '--workspace', str(self.workspace), '--env-file', '~test', '--start'),
            ('--state-dir', '~test', 'list'),
        )
        with patch('pwd.getpwnam', side_effect=KeyError('test')):
            for command in commands:
                with self.subTest(command=command):
                    code, out, err = self.cli(*command)
                    self.assertEqual(code, 1)
                    self.assertIn('~/test', err)
                    self.assertNotIn('Traceback', out + err)
                    self.assertFalse((self.state / 'demo').exists())
