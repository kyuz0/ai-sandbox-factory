"""Shared state and safety contracts exercised by the public CLI adapters."""
from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
import os
import shutil
import signal
from pathlib import Path
from unittest.mock import patch

from ai_sandbox_factory import (browser_editor, cli_lifecycle, editor_settings, hosted_models, inference,
                                operations, provider_credentials, runtime_context, sandbox, updates, web_access)
from test_cli_management import CliCase


class CliContracts(CliCase):
    def test_create_image_source_precedence(self):
        self.ok('defaults', 'set', '--settings-file', self.file('defaults.json', {'image_source': 'local'}))
        config = self.file('create.json', {'image_source': 'prebuilt'})
        with patch.object(sandbox, 'choose_engine', return_value='podman'), \
                patch.object(cli_lifecycle, 'start_creation') as start:
            for arguments, expected in (
                    ([], 'prebuilt'),
                    (['--use-defaults'], 'local'),
                    (['--use-defaults', '--config', config], 'prebuilt'),
                    (['--config', config, '--image-source', 'local'], 'local'),
                    (['--use-defaults', '--image-source', 'prebuilt'], 'prebuilt')):
                with self.subTest(arguments=arguments):
                    self.ok('create', 'demo', '--workspace', str(self.workspace), '--start', *arguments)
                    self.assertEqual(start.call_args.args[1]['image_source'], expected)
            start.reset_mock()
            message = self.fails('create', 'demo', '--workspace', str(self.workspace), '--start',
                                 '--config', self.file('bad.json', {'image_source': 'unknown-source'}))
            for term in ('unknown-source', 'prebuilt', 'local'):
                self.assertIn(term, message)
            start.assert_not_called()

    def test_recreate_passes_one_shot_image_source_and_show_reports_saved_preference(self):
        state = self.sandbox()
        with patch.object(operations.Service, 'run_update') as run:
            self.ok('recreate', 'demo', '--image-source', 'prebuilt')
            self.assertEqual(run.call_args.args[0], ['recreate', 'demo', '--image-source', 'prebuilt'])
            self.ok('recreate', 'demo')
            self.assertEqual(run.call_args.args[0], ['recreate', 'demo'])
        for saved, expected in ((None, 'local'), ('prebuilt', 'prebuilt')):
            if saved:
                config = json.loads((state / 'config.json').read_text())
                (state / 'config.json').write_text(json.dumps({**config, 'image_source': saved}))
            settings, _ = self.json_ok('show', 'demo', '--section', 'settings')
            self.assertEqual(settings['image_source'], expected)

    def test_public_lifecycle_commands_supervise_once_and_workers_execute_directly(self):
        commands = {
            'update': ['--settings-file', 'settings.json'],
            'update-containers': ['--revision', 'reviewed-revision'],
            'recreate': [],
            'install-system-packages': ['curl'],
            'profile-fork': ['--directory', str(self.root / 'profile')],
            'profile-apply': ['--revision', 'reviewed-revision'],
            'service-data-export': ['--saved', 'saved-data', '--volume', 'data',
                                    '--output', str(self.root / 'data.tar')],
            'service-data-restore': ['--saved', 'saved-data', '--service', 'database'],
        }
        self.assertEqual(set(commands), set(cli_lifecycle.GATED))
        for command, arguments in commands.items():
            with self.subTest(command=command), \
                    patch.object(cli_lifecycle, 'supervise') as supervise, \
                    patch.object(sandbox, 'sandbox_command') as execute:
                self.ok(command, 'demo', *arguments)
                supervise.assert_called_once()
                execute.assert_not_called()
                supervise.reset_mock()
                gate = self.root / (command + '-apply')
                self.ok(command, 'demo', *arguments, '--apply-gate', str(gate))
                supervise.assert_not_called()
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[0].apply_gate, gate)

    def test_search_default_keys_stay_separate_and_match_the_endpoint(self):
        key = 'synthetic-default-search-key'
        settings = self.file('defaults.json', {'options': {'web_access': {'providers': {'openai': {}}}}})
        value, _ = self.json_ok('defaults', 'set', '--settings-file', settings, '--secrets-stdin',
                               stdin=json.dumps({'web_keys': {'openai': key}}))
        self.assertTrue(value['options']['web_access']['providers']['openai']['has_key'])
        self.assertNotIn(key, operations.defaults_path().read_text())
        self.assertEqual(web_access.saved_keys(operations.search_keys_path(), value['options']['web_access']), {'openai': key})
        self.assertEqual(operations.search_keys_path().stat().st_mode & 0o777, 0o600)
        value['options']['web_access']['providers']['openai']['endpoint'] = 'https://other.example'
        self.assertEqual(web_access.saved_keys(operations.search_keys_path(), value['options']['web_access']), {})
        self.assertNotIn(key, self.ok('defaults', 'show')[0])

    def test_prompt_assignment_updates_the_reference_without_changing_config(self):
        state = self.sandbox()
        # This fixture uses a synthetic provider without an inference manifest.
        config = json.loads((state / 'config.json').read_text())
        config['model_providers'] = []
        (state / 'config.json').write_text(json.dumps(config))
        original = (state / 'config.json').read_bytes()
        runtime_context.publish_saved(state)
        before = (state / 'runtime-config/sandbox-state.json').read_text()
        prompt = self.file('prompt.json', {'name': 'Team guidance', 'text': 'Use the project conventions.'})
        self.json_ok('prompt', 'assign', 'demo', '--file', prompt)
        after = (state / 'runtime-config/sandbox-state.json').read_text()
        self.assertNotEqual(before, after)
        self.assertIn('Team guidance', after)
        self.assertIn('Use the project conventions.', (state / 'runtime-config/sandbox-context.md').read_text())
        self.assertEqual((state / 'config.json').read_bytes(), original)

    def test_update_preview_and_empty_apply_are_read_only(self):
        state = self.sandbox()
        before = self.tree(state)
        with patch.object(sandbox, 'run', side_effect=AssertionError('Preview executed an engine command')), \
                patch.object(sandbox, 'build', side_effect=AssertionError('Preview built images')):
            value, _ = self.json_ok('update', 'demo', '--settings-file', self.file('s.json', {'memory': '4g'}),
                                   '--preview')
            self.assertEqual(value['runtime_effect'], 'replace')
            self.assertEqual(self.tree(state), before)
            with redirect_stdout(io.StringIO()):
                sandbox.apply_settings(json.loads((state / 'config.json').read_text()), state, {})
            self.assertEqual(self.tree(state), before)

    def test_preview_rejects_wrong_agent_shortcuts_and_invalid_combinations(self):
        self.sandbox()
        service = operations.Service(self.state)
        for change in ({'codex_web_search': True}, {'native_approval': 'auto'},
                       {'approval_model': 'current', 'memory': '4g'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                service.preview_settings('demo', change)

    def test_headless_self_update_requires_explicit_confirmation(self):
        with patch.object(updates, 'pipx_install', return_value=True):
            with patch.object(updates, 'upgrade') as upgrade:
                self.assertIn('--yes', self.fails('self-update'))
                upgrade.assert_not_called()
                self.ok('self-update', '--yes')
                upgrade.assert_called_once()

    def test_native_agents_reject_pi_connections_without_running_commands(self):
        for agent in ('codex', 'claude', 'agy'):
            self.sandbox(name=agent, agent=agent)
            with patch.object(sandbox, 'run', side_effect=AssertionError('Native sandbox executed Pi setup')):
                self.assertIn('Pi', self.fails('models', 'connections', agent))

    def test_creation_secret_shapes_fail_before_starting_a_job(self):
        from types import SimpleNamespace
        for value in ({'provider_keys': []}, {'provider_keys': {'fixture': 42}},
                      {'provider_credentials': {'fixture': {'unknown': 'x'}}},
                      {'hosted': {'openrouter': {'key': None}}},
                      {'hosted': {'openrouter': {'credential': {'label': []}}}}):
            with self.subTest(value=value), patch('sys.stdin', io.StringIO(json.dumps(value))), self.assertRaises(ValueError):
                cli_lifecycle.secrets(SimpleNamespace(secrets_stdin=True))

    def test_invalid_creation_does_not_check_or_delete_conflicts(self):
        bad = [{'network': 'typo'}, {'subagents': 'false'},
               {'network': 'unrestricted', 'allowlist': 'example.test:443'},
               {'primary_agent': 'codex', 'model_providers': {'fixture': {}}},
               {'hosted_connections': {'openrouter': {'credential': {'key': 'private-fixture'}}}}]
        with patch.object(operations.Service, 'check_creation') as conflicts, \
                patch.object(operations.Service, 'create') as create:
            for index, value in enumerate(bad):
                path = self.file(f'bad-{index}.json', {'workspace': str(self.workspace), **value})
                error = self.fails('create', 'demo', '--config', path, '--start', '--replace-conflicts')
                self.assertNotIn('private-fixture', error)
                conflicts.assert_not_called()
                create.assert_not_called()
                self.assertFalse((self.state / 'demo').exists())

    def test_invalid_defaults_preserve_saved_preferences(self):
        settings = self.file('defaults.json', {'memory': '4g'})
        self.ok('defaults', 'set', '--settings-file', settings)
        before = operations.defaults_path().read_bytes()
        for value in ({'network': 'typo'}, {'engine': 'typo'}, {'subagents': 'false'},
                      {'model_providers': []}, {'library_skills': 123}):
            with self.subTest(value=value):
                self.fails('defaults', 'set', '--settings-file', self.file('bad.json', value))
                self.assertEqual(operations.defaults_path().read_bytes(), before)

    def test_native_creation_drops_pi_defaults_but_rejects_explicit_pi_credentials(self):
        settings = self.file('defaults.json', {'model_providers': {'fixture': {}}, 'local_model_ports': '8000',
                                               'subagents': True, 'memory': '4g'})
        self.ok('defaults', 'set', '--settings-file', settings)
        from ai_sandbox_factory import creation
        with patch.object(sandbox, 'choose_engine', return_value='podman'), \
                patch.object(operations.Service, 'check_creation', return_value=creation.ResourceConflicts('podman', None, (), ())), \
                patch.object(operations.Service, 'create') as create:
            self.ok('create', 'native', '--workspace', str(self.workspace), '--primary-agent', 'codex', '--use-defaults', '--start')
            draft = create.call_args.args[0]
            self.assertEqual(draft['model_providers'], {})
            self.assertIsNone(draft['local_model_ports'])
            self.assertFalse(draft['subagents'])
            self.assertIsNone(create.call_args.args[1])
            create.reset_mock()
            self.assertIn('Pi', self.fails('create', 'native', '--workspace', str(self.workspace), '--primary-agent',
                'codex', '--start', '--secrets-stdin', stdin='{"provider_keys":{"fixture":"private"}}'))
            create.assert_not_called()

    def test_creation_uses_windows_path_normalization(self):
        from ai_sandbox_factory import creation, host_platform
        windows = r'C:\Users\Example\project'
        with patch.object(host_platform, 'local_path', side_effect=lambda value: self.workspace if str(value) == windows else Path(value)) as convert, \
                patch.object(sandbox, 'choose_engine', return_value='podman'), \
                patch.object(operations.Service, 'check_creation', return_value=creation.ResourceConflicts('podman', None, (), ())), \
                patch.object(operations.Service, 'create') as create:
            self.ok('create', 'demo', '--workspace', windows, '--start')
            self.assertEqual(create.call_args.args[0]['workspace'], str(self.workspace))
            self.assertIn(windows, [call.args[0] for call in convert.call_args_list])

    def test_copy_sharing_requires_an_editable_source(self):
        self.sandbox()
        self.assertIn('no editable profile', self.fails('copy', 'demo', 'new', '--share-editable-profile'))
        self.assertIn('requires --reuse-from', self.fails('create', 'new', '--workspace', str(self.workspace),
            '--reuse-editable-profile', '--start'))
        self.assertIn('no editable profile', self.fails('create', 'new', '--workspace', str(self.workspace),
            '--reuse-from', 'demo', '--reuse-editable-profile', '--start'))

    def test_copy_can_choose_a_compatible_profile_when_switching_agents(self):
        self.sandbox()
        with patch.object(cli_lifecycle, 'start_creation') as start:
            self.ok('copy', 'demo', 'new', '--primary-agent', 'codex', '--profile', 'base')
        draft = start.call_args.args[1]
        self.assertEqual((draft['primary_agent'], draft['profile']), ('codex', 'base'))
        self.assertEqual(draft['model_providers'], {})
        self.assertFalse(draft['share_editable_profile'])

    def test_creation_file_enables_local_discovery_unless_explicitly_disabled(self):
        from types import SimpleNamespace
        request = {'workspace': str(self.workspace), 'local_model_ports': '8000',
                   'allowlist': 'local-model.internal:8000'}
        args = SimpleNamespace(name='demo', use_defaults=False, open=False)
        draft = cli_lifecycle.complete_draft(request, args)
        self.assertEqual(draft['model_providers'], {'local': {}})
        draft = cli_lifecycle.complete_draft({**request, 'model_providers': {}}, args)
        self.assertEqual(draft['model_providers'], {})

    def test_chat_target_is_validated_before_preparing_a_transfer(self):
        self.sandbox()
        from ai_sandbox_factory import session_transfer
        with patch.object(session_transfer, 'Transfer') as transfer:
            self.assertIn('Invalid sandbox to', self.fails('chat', 'copy', 'demo', '--to', '../other', '--chat', 'one', '--preview'))
            transfer.assert_not_called()

    def test_interrupted_creation_alerts_do_not_need_config_or_service_queries(self):
        record = {'name': 'partial', 'creation': 'interrupted', 'containers': []}
        with patch.object(operations.Service, 'inventory', return_value=[record]), \
                patch.object(operations.Service, 'config', side_effect=AssertionError('No config yet')):
            alerts, _ = self.json_ok('show', 'partial', '--section', 'alerts')
        self.assertEqual(alerts[0]['title'], 'Creation interrupted')

    def test_editor_recording_failure_restores_previous_settings(self):
        state = self.sandbox()
        original = editor_settings.validate()
        editor_settings.save(state, original)
        with patch.object(browser_editor, 'active', return_value=True), \
                patch.object(browser_editor, 'stop') as stop, \
                patch.object(editor_settings.activity, 'record', side_effect=OSError('disk full')):
            error = self.fails('editor', 'set', 'demo', '--settings-file', self.file('e.json', {'allow_uploads': True}))
        self.assertIn('stopped', error)
        self.assertIn('previous editor settings were kept', error)
        self.assertIn('disk full', error)
        self.assertEqual(editor_settings.load(state), original)
        stop.assert_called_once()

    def test_reassigning_a_hosted_key_does_not_dispatch_a_plugin(self):
        state = self.sandbox()
        config = json.loads((state / 'config.json').read_text())
        config['hosted_connections'] = {'openrouter': {}}
        config['model_providers'] = []
        (state / 'config.json').write_text(json.dumps(config))
        service = operations.Service(self.state)
        with patch.object(service, 'provider_key_status', return_value={'configured': []}), \
                patch.object(provider_credentials, 'assign', return_value={'ref': 'saved', 'label': 'Team'}) as assign, \
                patch.object(service, 'provider') as plugin:
            result = service.assign_provider_key('demo', 'openrouter', {'ref': 'saved'}, lambda _: None)
        assign.assert_called_once_with(config, state, 'openrouter', ref='saved')
        self.assertEqual(result['label'], 'Team')
        plugin.assert_not_called()

    def test_native_model_timeout_keeps_partial_byte_diagnostics(self):
        with patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(hosted_models.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    'node', 65, stderr=b'ECONNRESET from model endpoint')):
            with self.assertRaisesRegex(ValueError, 'status.*timed out.*65.*ECONNRESET'):
                hosted_models.request({'name': 'demo', 'engine': 'podman'}, 'status')

    def test_companion_errors_keep_exit_status_and_redact_submitted_keys(self):
        state = self.sandbox()
        (state / 'inference').mkdir()
        key = 'private-companion-fixture'
        result = subprocess.CompletedProcess([], 17, '', f'ECONNRESET Bearer {key}')
        with patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(inference.subprocess, 'run', return_value=result):
            with self.assertRaises(ValueError) as caught:
                inference.credentials({'name': 'demo', 'engine': 'podman'}, state,
                                      {'id': 'openrouter', 'credentials': {'key': key}})
        message = str(caught.exception)
        self.assertIn('exit', message)
        self.assertIn('17', message)
        self.assertIn('ECONNRESET', message)
        self.assertNotIn(key, message)

    def test_signals_inside_controller_locks_do_not_deadlock(self):
        script = """
import os, signal
from ai_sandbox_factory.cli_lifecycle import Foreground
from ai_sandbox_factory import creation, updating
for controller in (creation.Creation(), updating.Update()):
    lock = controller.lock if hasattr(controller, 'lock') else controller._lock
    with Foreground(controller).signals():
        with lock:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                os.kill(os.getpid(), number)
            print('handler returned while lock held', flush=True)
    assert controller.requested.is_set() if hasattr(controller, 'requested') else not controller.can_cancel
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('handler returned while lock held'), 2)

    def test_failed_worker_stop_keeps_its_apply_gate_and_returns_the_diagnostic(self):
        from ai_sandbox_factory import updating
        controller = updating.Update()
        processes = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        def cancel(line):
            if line == 'Running':
                controller.cancel()
        try:
            with patch.object(updating.subprocess, 'Popen', side_effect=launch), \
                    patch.object(updating.host_platform, 'stop_process_group', return_value=False):
                with self.assertRaisesRegex(updating.StopFailed, 'Could not stop the update worker'):
                    controller.run([sys.executable, '-c', "import signal; print('Running', flush=True); signal.pause()"], cancel)
            self.assertTrue(controller._gate.is_dir())
            self.assertFalse(updating.begin_apply(controller._gate))
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
            if controller._gate:
                shutil.rmtree(controller._gate.parent, ignore_errors=True)

    def test_callback_failure_stops_the_preparing_worker(self):
        from ai_sandbox_factory import updating
        controller = updating.Update()
        processes = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        def broken_log(line):
            if line == 'Running':
                raise RuntimeError('fixture progress callback failed')
        try:
            with patch.object(updating.subprocess, 'Popen', side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, 'progress callback failed'):
                    controller.run([sys.executable, '-c', "import signal; print('Running', flush=True); signal.pause()"], broken_log)
            self.assertIsNotNone(processes[0].poll(), 'A callback failure orphaned the worker')
            self.assertFalse(controller._gate.parent.exists())
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
