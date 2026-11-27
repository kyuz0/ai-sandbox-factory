"""Explicit connector and package actions belong to app-owned jobs."""
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from ai_sandbox_factory import aws_ui, credentials_ui, repository_ui, skills_ui


class DedicatedJobTests(unittest.TestCase):
    def app(self):
        app = SimpleNamespace(service=Mock(), start_task=Mock(return_value=object()), push_screen=Mock())
        return app

    def test_discovery_retains_files_until_review_or_app_cleanup(self):
        app = self.app()
        screen = skills_ui.ImportSkills()
        with patch.object(type(screen), 'app', PropertyMock(return_value=app)):
            screen.discover('/synthetic/source', None)
        args, options = app.start_task.call_args
        self.assertEqual(options['followup_label'], 'Review validated skills')
        rows = [{'name': 'example'}]
        with patch.object(skills_ui.skill_library, 'discover', return_value=rows) as discover:
            result = args[2](lambda line: None)
        directory = Path(result.staging.name)
        self.assertTrue(directory.exists())
        self.assertEqual(result.rows, rows)
        discover.assert_called_once()
        result.close()
        self.assertFalse(directory.exists())

    def test_discovery_error_cleans_private_source_and_preserves_cause(self):
        app = self.app()
        screen = skills_ui.ImportSkills()
        with patch.object(type(screen), 'app', PropertyMock(return_value=app)):
            screen.discover('/synthetic/source', None)
        task = app.start_task.call_args.args[2]
        folders = []
        def broken(source, destination, ref):
            folders.append(destination.parent)
            raise OSError('Git exited 128: branch does not exist')
        with patch.object(skills_ui.skill_library, 'discover', side_effect=broken):
            with self.assertRaisesRegex(OSError, 'Git exited 128: branch does not exist'):
                task(lambda line: None)
        self.assertFalse(folders[0].exists())

    def test_install_is_deferred_and_owns_staging_until_worker_finishes(self):
        app = self.app()
        screen = skills_ui.ImportSkills()
        screen.staging = tempfile.TemporaryDirectory()
        directory = Path(screen.staging.name)
        rows = [{'name': 'example'}]
        with patch.object(type(screen), 'app', PropertyMock(return_value=app)), \
             patch.object(skills_ui.skill_library, 'install') as install:
            screen.install_selected(rows)
            install.assert_not_called()
            self.assertTrue(directory.exists())
            self.assertIsNone(screen.staging)
            app.start_task.call_args.args[2](lambda line: None)
            install.assert_called_once_with(rows)
        self.assertFalse(directory.exists())

    def test_aws_rollout_reserves_every_selected_sandbox(self):
        app = self.app()
        app.state_root = Path('/synthetic/state')
        widget = SimpleNamespace(app=app, notify=Mock(), is_mounted=False)
        selected = {'alpha': {'memory': '4g'}, 'beta': {'memory': '8g'}}
        profile = {'profile_name': 'Research'}
        with patch.object(aws_ui.aws_profiles, 'pending', return_value=selected):
            aws_ui.AWSProfileChoices.choose_targets(widget, profile)
        chosen = app.push_screen.call_args.args[1]
        chosen(selected)
        self.assertEqual(app.start_task.call_args.kwargs['targets'], {'alpha', 'beta'})
        with patch.object(aws_ui.aws_profiles, 'apply_selected') as apply:
            app.start_task.call_args.args[2](lambda line: None)
        self.assertEqual(apply.call_args.args[:3], (app.service, profile, selected))

    def test_connector_update_uses_cancellable_settings_job_with_reviewed_scope(self):
        from ai_sandbox_factory.connector_dialogs import ConnectorSettings
        app = self.app()
        app.start_settings_job = Mock()
        app.state_root = Path('/synthetic/state')
        connector = SimpleNamespace(external=True, id='release', title='Release status',
                                    routes=[('status', 'https://example.test/status')],
                                    reviewed_instructions={'service': 'Read release status', 'operations': {}})
        widget = SimpleNamespace(app=app, sandbox_name='alpha', config={})
        event = SimpleNamespace(connector=connector, stop=Mock())
        ConnectorSettings.configure(widget, event)
        app.push_screen.call_args.args[1](['status'])
        args, options = app.start_settings_job.call_args
        self.assertEqual(args[:2], ('alpha', 'Update Release status'))
        self.assertEqual(options['success_message'], 'Release status access saved.')
        control = object()
        args[2](lambda line: None, control)
        changes = app.service.update_settings.call_args.args[1]
        self.assertEqual(json.loads(options['cli_recipe'].steps[0].stdin), changes)
        self.assertEqual(changes['expected_connector_routes'], {'release': dict(connector.routes)})
        self.assertEqual(changes['expected_connector_instructions'], {'release': connector.reviewed_instructions})
        self.assertIs(app.service.update_settings.call_args.args[3], control)

    def test_forget_key_runs_only_in_job_and_never_logs_reference(self):
        app = self.app()
        widget = SimpleNamespace(app=app, saved={'synthetic-key-reference': {'label': 'Research'}}, is_mounted=False)
        with patch.object(credentials_ui.provider_credentials, 'forget') as forget:
            credentials_ui.CredentialFields.forget(widget, 'synthetic-key-reference')
            forget.assert_not_called()
            output = []
            app.start_task.call_args.args[2](output.append)
        forget.assert_called_once_with('synthetic-key-reference')
        self.assertNotIn('synthetic-key-reference', '\n'.join(output))

    def test_generate_key_runs_only_in_job_with_reopenable_public_output(self):
        app = self.app()
        widget = SimpleNamespace(app=app, sandbox_name='alpha', generate=True, is_mounted=False, query_one=Mock())
        repository_ui.PublicKey.load_key(widget)
        app.service.ssh_key.assert_not_called()
        app.service.ssh_key.return_value = 'ssh-ed25519 SYNTHETIC_PUBLIC_KEY'
        output = []
        public = app.start_task.call_args.args[2](output.append)
        self.assertEqual(public, output[0])
        self.assertEqual(app.start_task.call_args.kwargs['followup_label'], 'Show public key')
        app.service.ssh_key.assert_called_once_with('alpha', True)


    def test_model_test_uses_selected_provider_mapping_without_selecting_default(self):
        from ai_sandbox_factory.models_ui import ModelDialog
        app = self.app()
        fields = {'#model-results': SimpleNamespace(highlighted=0),
                  '#model-deployment': SimpleNamespace(value='deployment-name')}
        widget = SimpleNamespace(app=app, sandbox_name='alpha', provider='azure',
                                 plugin_label=None, filtered=[{'id': 'chat-model'}],
                                 query_one=lambda selector, *_: fields[selector], is_mounted=False)
        ModelDialog.apply(widget, True)
        app.service.hosted_test.assert_not_called()
        result = app.start_task.call_args.args[2](lambda line: None)
        self.assertEqual(result, 'chat-model')
        app.service.hosted_test.assert_called_once_with('alpha', 'azure', 'chat-model', deployment='deployment-name')
        app.service.hosted_select.assert_not_called()



class DedicatedJobUITests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_save_preserves_selection_until_followup(self):
        from ai_sandbox_factory.reviewer_ui import ReviewerFields
        app = SimpleNamespace(service=Mock(), start_task=Mock(return_value=object()))
        widget = SimpleNamespace(app=app, sandbox_name='alpha', save_on_choice=True,
                                 selected='current', status='', is_mounted=True, update_controls=Mock())
        await ReviewerFields.select_model(widget, 'openrouter/model')
        self.assertEqual(widget.selected, 'current')
        app.service.save_approval_model.assert_not_called()
        task = app.start_task.call_args.args[2]
        result = task(lambda line: None)
        self.assertEqual(widget.selected, 'current')
        app.start_task.call_args.kwargs['on_success'](result)
        self.assertEqual(widget.selected, 'openrouter/model')
        app.start_task.call_args.kwargs['on_failure']('Provider rejected model: HTTP 404')
        self.assertEqual(widget.status, 'Provider rejected model: HTTP 404')

    async def test_hidden_skill_discovery_waits_for_review_and_keeps_source(self):
        import asyncio
        import copy
        import threading
        from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS
        from test_tui import FakeService
        gate, started = threading.Event(), threading.Event()
        def discover(source, destination, ref):
            started.set()
            if not gate.wait(10):
                raise RuntimeError('Test did not release skill discovery')
            destination.mkdir()
            (destination / 'SKILL.md').write_text('synthetic skill source')
            return [{'id': 'synthetic', 'name': 'example', 'description': 'Example', 'folder': '.'}]
        with tempfile.TemporaryDirectory() as tmp, \
             patch('ai_sandbox_factory.updates.available_update', return_value=None), \
             patch('ai_sandbox_factory.sandbox_tui.load_defaults', side_effect=lambda: copy.deepcopy(DEFAULT_SETTINGS)), \
             patch.object(skills_ui.skill_library, 'discover', side_effect=discover):
            app = Cockpit(Path(tmp), FakeService([]))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                origin = skills_ui.ImportSkills('/synthetic/source')
                await app.push_screen(origin)
                await pilot.pause()
                origin.discover('/synthetic/source', None)
                await pilot.pause()
                self.assertTrue(started.is_set())
                job = app.jobs[-1]
                await app.pop_screen()  # Leave job details while the task is running.
                await app.pop_screen()  # Close the originating form too.
                main = app.screen
                gate.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIs(app.screen, main)
                self.assertEqual(job.followup[0], 'Review validated skills')
                staging = Path(job.value.staging.name)
                self.assertTrue((staging / 'source' / 'SKILL.md').exists())
                app.show_job(job)
                await pilot.pause()
                app.resume_job(job)
                await pilot.pause()
                self.assertIsInstance(app.screen, skills_ui.ImportSkills)
                self.assertEqual(len(app.screen.rows), 1)
                self.assertIsNone(job.value)
                await app.pop_screen()
                self.assertFalse(staging.exists())
