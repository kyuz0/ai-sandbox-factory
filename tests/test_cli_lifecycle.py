"""Public CLI creation and supervised updates, run through the real ``sandbox.main`` parser.

Creation tests replace only the engine checks and the creation job at the
``operations.Service`` boundary. Supervision tests run the real CLI in a child
process and replace the Factory worker command with a synthetic worker from this
file. The worker is driven by FIFOs, so every step waits for an observed event.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_sandbox_factory import (cli, cli_lifecycle, creation, editor_settings, operation_locks,
                                operations, sandbox, web_access)
from ai_sandbox_factory.sandbox_options import options

ROOT = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()
KEY = 'sk-synthetic-provider-key-0123456789'
HOSTED_KEY = 'sk-synthetic-hosted-key-9876543210'
NESTED_KEY = 'sk-synthetic-nested-key-5555555555'
WEB_KEY = 'synthetic-web-search-key-4242'
SLACK_TOKEN = 'xoxb-synthetic-slack-token-1111'
REF = 'a' * 32
TOKEN = 'b' * 48
WAIT = 60  # Failure guard only; every step waits for a specific event.
APPLYING = 'The operation is applying or finishing; waiting for a consistent result.'
CANCELLING = 'Cancellation requested; waiting for cleanup.'


class CliCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='factory-cli-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.locks = self.root / 'locks'
        self.locks.mkdir(mode=0o700)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.environment = {'XDG_CONFIG_HOME': str(self.root / 'config'),
                            'SANDBOX_REGISTRY_DIR': str(self.root / 'packages')}
        for patcher in (patch.dict(os.environ, self.environment),
                        patch.object(operation_locks, 'private_runtime_dir', return_value=self.locks)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['ai-sandbox-factory', '--state-dir', str(self.state), *argv]), \
                patch('sys.stdin', io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return str(path)

    def sandbox(self, name='demo'):
        state = self.state / name
        state.mkdir()
        (state / 'config.json').write_text(json.dumps({'name': name, 'engine': 'podman', 'primary_agent': 'pi',
                                                       'workspace': str(self.workspace)}))
        return state


class PublicCreation(CliCase):
    """``create --start`` builds one draft and hands it, with separate keys, to the shared job."""

    def setUp(self):
        super().setUp()
        self.jobs = []
        self.on_create = None
        engine = patch.object(sandbox, 'choose_engine',
                              side_effect=lambda requested, quiet=False: 'podman' if requested in ('auto', 'podman') else requested)
        check = patch.object(operations.Service, 'check_creation', autospec=True,
                             side_effect=lambda service, draft, approved=None: creation.ResourceConflicts('podman', None, (), ()))
        create = patch.object(operations.Service, 'create', autospec=True, side_effect=self.create)
        for patcher in (engine, check, create):
            result = patcher.start()
            if patcher is check:
                self.check_creation = result
            self.addCleanup(patcher.stop)

    def create(self, service, draft, key, log, cancellation=None, **kwargs):
        self.assertIsInstance(cancellation, creation.Creation)
        self.assertEqual(service.state, self.state.resolve())
        self.jobs.append({'draft': draft, 'key': key})
        if self.on_create:
            self.on_create(log)

    def start(self, *argv, stdin='', config=None):
        if config is not None:
            argv = ('--config', self.file('creation.json', {'workspace': str(self.workspace), **config}), *argv)
        code, out, err = self.cli('create', 'demo', *argv, '--start', stdin=stdin)
        return code, out, err

    def started(self, *argv, **kwargs):
        code, out, err = self.start(*argv, **kwargs)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.jobs), 1)
        return self.jobs[0]['draft'], self.jobs[0]['key'], out, err

    def refused(self, *argv, code=1, **kwargs):
        actual, out, err = self.start(*argv, **kwargs)
        self.assertEqual(actual, code, err)
        self.assertEqual(self.jobs, [], 'Invalid creation input reached the creation job')
        self.assertEqual(out, '')
        return err

    # Configuration mapping

    def test_config_file_maps_to_the_creation_draft(self):
        draft, key, out, _ = self.started('--json', config={
            'profile': 'base', 'network': 'unrestricted', 'memory': '2g', 'editor': 'pi',
            'options': {'browser': False}, 'model_providers': {'fixture': {'model': 'm1'}},
            'working_directory': '.', 'subagents': True})
        self.assertEqual(json.loads(out), {'name': 'demo', 'status': 'ready'})
        self.assertEqual((draft['name'], draft['workspace'], draft['engine']), ('demo', str(self.workspace.resolve()), 'podman'))
        self.assertEqual((draft['network'], draft['memory'], draft['subagents']), ('unrestricted', '2g', True))
        self.assertEqual(draft['editor'], {**editor_settings.DEFAULTS, 'mode': 'pi'})
        self.assertEqual(draft['options'], options({'browser': False}))
        self.assertEqual(draft['model_providers'], {'fixture': {'model': 'm1'}})
        self.assertIs(draft['open_editor'], False)
        self.assertIsNone(key)

    def test_explicit_flags_override_the_config_file(self):
        draft, *_ = self.started('--memory', '4g', '--network=allowlist', '--editor', 'browser',
                                 config={'memory': '2g', 'network': 'unrestricted', 'editor': 'pi'})
        self.assertEqual((draft['memory'], draft['network'], draft['editor']['mode']), ('4g', 'allowlist', 'browser'))

    def test_abbreviated_flags_also_override_the_config_file(self):
        # argparse accepts unambiguous prefixes, so --mem is the --memory flag.
        draft, *_ = self.started('--mem', '4g', config={'memory': '2g'})
        self.assertEqual(draft['memory'], '4g')

    def test_flag_only_creation_needs_no_config_file(self):
        draft, *_ = self.started('--workspace', str(self.workspace), '--memory', '3g', '--allow-domain', 'example.com:443')
        self.assertEqual((draft['memory'], draft['allowlist']), ('3g', 'example.com:443'))

    # Workstation defaults

    def save_defaults(self, value):
        code, _, err = self.cli('defaults', 'set', '--settings-file', self.file('defaults.json', value))
        self.assertEqual(code, 0, err)
        return operations.defaults_path().read_text()

    def test_built_in_defaults_ignore_saved_workstation_defaults(self):
        saved = self.save_defaults({'memory': '16g', 'network': 'unrestricted', 'subagents': True, 'engine': 'docker'})
        draft, *_ = self.started(config={})
        self.assertNotEqual(draft['memory'], '16g')
        self.assertEqual((draft['network'], draft['subagents'], draft['engine']), ('allowlist', False, 'podman'))
        self.assertEqual(operations.defaults_path().read_text(), saved, 'Creation changed the saved defaults')

    def test_use_defaults_starts_from_saved_choices_and_flags_still_win(self):
        saved = self.save_defaults({'memory': '16g', 'network': 'unrestricted', 'engine': 'docker',
                                    'editor': {'mode': 'pi', 'allow_uploads': True}})
        draft, *_ = self.started('--use-defaults', '--workspace', str(self.workspace))
        self.assertEqual((draft['memory'], draft['network'], draft['engine']), ('16g', 'unrestricted', 'docker'))
        self.assertEqual((draft['editor']['mode'], draft['editor']['allow_uploads']), ('pi', True))
        self.jobs.clear()
        draft, *_ = self.started('--use-defaults', '--workspace', str(self.workspace), '--memory', '4g', '--engine', 'podman')
        self.assertEqual((draft['memory'], draft['network'], draft['engine']), ('4g', 'unrestricted', 'podman'))
        self.assertEqual(operations.defaults_path().read_text(), saved)

    def test_only_use_defaults_reuses_saved_web_search_keys(self):
        web = options()['web_access']
        web['enabled'] = True
        operations.save_web_defaults(web, {'exa': WEB_KEY})
        draft, key, out, err = self.started('--use-defaults', '--workspace', str(self.workspace))
        self.assertEqual(key, {'exa': WEB_KEY})
        self.assertIs(draft['options']['web_access']['providers']['exa']['has_key'], True)
        self.assertNotIn(WEB_KEY, out + err)
        self.jobs.clear()
        draft, key, *_ = self.started(config={'options': {'web_access': web}})
        self.assertFalse(key)
        self.assertIs(draft['options']['web_access']['providers']['exa']['has_key'], False)

    # Credentials

    def test_secrets_arrive_separately_from_the_public_draft(self):
        secrets = {'provider_keys': {'fixture': KEY}, 'web_keys': {'exa': WEB_KEY},
                   'communication_keys': {'botToken': SLACK_TOKEN},
                   'hosted': {'azure': {'key': HOSTED_KEY}}}
        draft, key, out, err = self.started('--secrets-stdin', '--json', stdin=json.dumps(secrets), config={
            'hosted_connections': {'azure': {'endpoint': 'https://example.openai.azure.com'}},
            'credential_refs': {'openrouter': REF}})
        self.assertEqual(key, {'exa': WEB_KEY})
        self.assertEqual(draft['_provider_keys'], {'fixture': KEY})
        self.assertEqual(draft['_communication_keys'], {'botToken': SLACK_TOKEN})
        self.assertEqual(draft['_hosted_changes'], {'azure': {'endpoint': 'https://example.openai.azure.com',
                                                              'key': HOSTED_KEY, 'provider': 'azure'}})
        self.assertEqual(draft['_provider_credential_choices'], {'openrouter': {'ref': REF}})
        public = json.dumps({field: value for field, value in draft.items() if not field.startswith('_')})
        for secret in (KEY, WEB_KEY, SLACK_TOKEN, HOSTED_KEY):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret, out + err)
        self.assertNotIn('hosted_connections', draft)
        self.assertNotIn('credential_refs', draft)

    def test_saved_key_references_and_entered_keys_are_both_kept(self):
        secrets = {'provider_credentials': {'acme': {'key': KEY, 'label': 'Team'}}}
        draft, *_ = self.started('--secrets-stdin', stdin=json.dumps(secrets), config={'credential_refs': {'openrouter': REF}})
        self.assertEqual(draft['_provider_credential_choices'],
                         {'openrouter': {'ref': REF}, 'acme': {'key': KEY, 'label': 'Team'}})

    def test_job_output_and_failures_redact_supplied_secrets(self):
        def leak(log):
            log(f'adapter said Bearer {KEY} and {SLACK_TOKEN}')
            raise ValueError(f'Upstream 401 for {HOSTED_KEY}: invalid_api_key')
        self.on_create = leak
        secrets = {'provider_keys': {'fixture': KEY}, 'communication_keys': {'botToken': SLACK_TOKEN},
                   'hosted': {'azure': {'key': HOSTED_KEY}}}
        code, out, err = self.start('--secrets-stdin', stdin=json.dumps(secrets),
                                    config={'hosted_connections': {'azure': {'endpoint': 'https://example.openai.azure.com'}}})
        self.assertEqual(code, 1)
        for secret in (KEY, SLACK_TOKEN, HOSTED_KEY):
            self.assertNotIn(secret, out + err)
        self.assertIn('adapter said Bearer', out)
        self.assertIn('Upstream 401 for [redacted]: invalid_api_key', err)

    def test_nested_credentials_are_redacted_at_any_depth(self):
        # Hosted connections accept a saved-key object (see models connect --remember).
        def leak(log):
            log(f'nested credential {NESTED_KEY}')
        self.on_create = leak
        secrets = {'hosted': {'openrouter': {'credential': {'key': NESTED_KEY, 'label': 'Team'}}},
                   'local_model_keys': {'8080': {'key': KEY}}}
        draft, _, out, err = self.started('--secrets-stdin', stdin=json.dumps(secrets), config={})
        self.assertEqual(draft['_local_model_keys'], {'8080': {'key': KEY}})
        self.assertNotIn(NESTED_KEY, out + err)

    def test_credentials_in_the_public_file_are_refused_without_echoing_them(self):
        err = self.refused(config={'provider_keys': {'fixture': KEY}})
        self.assertIn('provider_keys', err)
        self.assertNotIn(KEY, err)
        err = self.refused(config={'hosted_connections': {'azure': {'key': HOSTED_KEY}}})
        self.assertIn('--secrets-stdin', err)
        self.assertNotIn(HOSTED_KEY, err)

    def test_secret_input_errors_are_actionable(self):
        err = self.refused('--secrets-stdin', stdin=json.dumps({'passwords': {'x': KEY}}), config={})
        self.assertIn('provider_keys', err)
        self.assertNotIn(KEY, err)
        err = self.refused('--config', '-', '--secrets-stdin', stdin='{}')
        self.assertIn('stdin cannot contain both', err)
        code, out, err = self.cli('create', 'demo', '--workspace', str(self.workspace), '--secrets-stdin', stdin='{}')
        self.assertEqual(code, 2)
        self.assertIn('--secrets-stdin requires --start', err)

    # Invalid input and conflicts

    def test_invalid_creation_input_names_the_problem(self):
        err = self.refused(config={'workspace_dir': '/x'})
        self.assertIn('workspace_dir', err)
        err = self.refused('--config', self.file('list.json', ['workspace']))
        self.assertIn('JSON object', err)
        err = self.refused('--config', str(self.root / 'absent.json'))
        self.assertIn('Cannot read JSON from', err)
        err = self.refused('--use-defaults')
        self.assertIn('--workspace', err)
        err = self.refused('--editor', 'pi', code=2)
        self.assertIn('--workspace is required', err)

    def test_missing_workspace_is_named_as_the_workspace(self):
        missing = self.root / 'no-such-project'
        err = self.refused('--config', self.file('c.json', {'workspace': str(missing)}))
        self.assertIn(str(missing), err)
        self.assertIn('workspace', err.lower())
        self.assertNotIn('Errno', err, 'The missing workspace was reported as a raw file error')

    def test_conflicting_resources_need_explicit_approval(self):
        listed = creation.ResourceConflicts('podman', None, (('factory-demo', 'c' * 64),), (('factory-demo-home', '{}'),))
        approvals = []

        def check(service, draft, approved=None):
            approvals.append(approved)
            return listed if approved is None else creation.ResourceConflicts('podman', None, (), ())
        with patch.object(self.check_creation, 'side_effect', check):
            err = self.refused(config={})
            self.assertIn('factory-demo-home', err)
            self.assertIn('--replace-conflicts', err)
            self.assertEqual(approvals, [None])
            self.started('--replace-conflicts', config={})
        self.assertEqual(approvals, [None, None, listed])

    def test_cancelled_creation_exits_130_with_its_cleanup_result(self):
        def cancelled(log):
            raise creation.Cancelled('Creation cancelled. Runtime containers and private state removed; project files and build cache preserved.')
        self.on_create = cancelled
        code, out, err = self.start('--json', config={})
        self.assertEqual(code, 130)
        self.assertEqual(out, '')
        self.assertIn('project files and build cache preserved', err)

    def test_invalid_internal_combinations_are_usage_errors(self):
        code, _, err = self.cli('create', 'demo', '--workspace', str(self.workspace), '--creation-token', TOKEN, '--start')
        self.assertEqual(code, 2)
        self.assertIn('internal worker input', err)
        code, _, err = self.cli('create', 'demo', '--workspace', str(self.workspace), '--open')
        self.assertEqual(code, 2)
        self.assertIn('--open requires --start', err)
        self.assertEqual(self.jobs, [])


class WorkerInvocation(CliCase):
    """The saved-configuration step re-enters the CLI; it must reach the worker path, not the public adapter."""

    def setUp(self):
        super().setUp()
        for patcher in (patch.object(sandbox, 'choose_engine', return_value='podman'),
                        patch.dict(os.environ, {'PYTHONPATH': str(ROOT), 'FACTORY_TEST_LOCKS': str(self.locks),
                                                'FACTORY_TEST_MODE': 'legacy-create'}),
                        patch.object(operations.Service, 'command', autospec=True,
                                     side_effect=lambda service, *args: [sys.executable, str(THIS), '--cli-launcher',
                                                                         '--state-dir', str(service.state), *args])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def invocations(self, lines):
        return [json.loads(line.split(' ', 1)[1]) for line in lines if line.startswith('LEGACY-CREATE ')]

    def test_public_create_without_start_saves_through_the_worker_path(self):
        code, out, err = self.cli('create', 'demo', '--config',
                                  self.file('c.json', {'workspace': str(self.workspace), 'editor': 'pi'}), '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'name': 'demo', 'status': 'configured'})
        self.assertEqual(self.invocations(err.splitlines()), [{'name': 'demo', 'token': False, 'start': False}])
        self.assertEqual(editor_settings.load(self.state / 'demo')['mode'], 'pi')
        self.assertFalse(operation_locks.in_use('demo'))

    def test_creation_token_reaches_the_worker_path_and_is_hidden_from_the_log(self):
        draft = {**operations.DEFAULT_SETTINGS, 'name': 'demo', 'workspace': str(self.workspace), 'engine': 'podman',
                 'options': options(), 'allowlist': '', 'primary_agent': 'pi', 'subagents': False}
        lines = []
        operations.Service(self.state).save_configuration(draft, lines.append, creation_token=TOKEN)
        self.assertEqual(self.invocations(lines), [{'name': 'demo', 'token': True, 'start': False}])
        self.assertIn('--creation-token [redacted]', lines[0])
        self.assertNotIn(TOKEN, '\n'.join(lines))


# Supervised updates through a real child CLI process ------------------------------------------


class Supervised:
    """A child CLI process, its synthetic worker and the FIFO protocol that drives the worker."""

    def __init__(self, case, argv, *, stdin=None, stdout='pipe'):
        self.case = case
        self.control = Path(tempfile.mkdtemp(prefix='control-', dir=case.root))
        os.mkfifo(self.control / 'events')
        os.mkfifo(self.control / 'release')
        self.events, self.errors, self.output = queue.Queue(), queue.Queue(), queue.Queue()
        self.seen = {'events': [], 'stderr': [], 'stdout': []}
        self.pending = {'event': [], 'stderr': [], 'stdout': []}
        self.worker_pid = None
        environment = {**os.environ, **case.environment, 'PYTHONPATH': str(ROOT), 'FACTORY_TEST_MODE': 'update',
                       'FACTORY_TEST_LOCKS': str(case.locks), 'FACTORY_TEST_CONTROL': str(self.control)}
        self.master = None
        if stdout == 'pty':
            self.master, slave = os.openpty()
            target = slave
        else:
            target = subprocess.PIPE
        self.process = subprocess.Popen([sys.executable, str(THIS), '--cli-launcher', '--state-dir', str(case.state), *argv],
                                        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                        stdout=target, stderr=subprocess.PIPE, text=True, env=environment,
                                        start_new_session=True)
        if stdout == 'pty':
            os.close(slave)
        if stdin is not None:
            self.process.stdin.write(stdin)
            self.process.stdin.close()
        self._read(self.process.stderr, self.errors, 'stderr')
        if stdout == 'pipe-read':
            self._read(self.process.stdout, self.output, 'stdout')
        threading.Thread(target=self._events, daemon=True).start()
        case.addCleanup(self.close)

    def _read(self, stream, target, name):
        def read():
            for line in stream:
                self.seen[name].append(line.rstrip('\n'))
                target.put(line.rstrip('\n'))
            target.put(None)
        threading.Thread(target=read, daemon=True).start()

    def _events(self):
        with open(self.control / 'events') as stream:
            for line in stream:
                self.seen['events'].append(line.rstrip('\n'))
                self.events.put(line.rstrip('\n'))
        self.events.put(None)

    def diagnostics(self):
        return (f'CLI status {self.process.poll()}; recent events {self.seen["events"][-10:]}; '
                f'stderr {self.seen["stderr"][-10:]}; stdout {self.seen["stdout"][-10:]}')

    def expect(self, source, text):
        stream = {'event': self.events, 'stderr': self.errors, 'stdout': self.output}[source]
        for index, line in enumerate(self.pending[source]):
            if line.startswith(text):
                return self.pending[source].pop(index)
        while True:
            try:
                line = stream.get(timeout=WAIT)
            except queue.Empty:
                self.case.fail(f'Timed out waiting for {source} {text!r}. ' + self.diagnostics())
            if line is None:
                self.case.fail(f'{source} closed before {text!r}. ' + self.diagnostics())
            if line.startswith(text):
                return line
            self.pending[source].append(line)

    def started(self):
        self.worker_pid = int(self.expect('event', 'started').split()[1])

    def send(self, command):
        """Wait until the worker is blocked on its next command, then send one."""
        self.expect('event', 'waiting')
        done = threading.Event()

        def write():
            with open(self.control / 'release', 'w') as stream:
                stream.write(command)
            done.set()
        threading.Thread(target=write, daemon=True).start()
        if not done.wait(WAIT):
            self.case.fail(f'The worker did not accept {command!r}. ' + self.diagnostics())

    def signal(self, number):
        self.process.send_signal(number)

    def finish(self):
        try:
            code = self.process.wait(WAIT)
        except subprocess.TimeoutExpired:
            self.case.fail('The CLI did not exit. ' + self.diagnostics())
        return code

    def stderr_after_exit(self):
        while self.errors.get(timeout=WAIT) is not None:
            pass
        return self.seen['stderr']

    def close_output(self):
        if self.master is not None:
            os.close(self.master)
            self.master = None
        else:
            self.process.stdout.close()

    def close(self):
        for pid, kill in ((self.worker_pid, os.killpg), (self.process.pid, os.killpg)):
            if pid is not None:
                try:
                    kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.process.wait()
        if self.master is not None:
            os.close(self.master)
        try:  # Release an events reader still waiting for a worker that never started.
            os.close(os.open(self.control / 'events', os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class SupervisedUpdates(CliCase):
    def setUp(self):
        super().setUp()
        self.sandbox()

    def worker_signals(self, run):
        return [event for event in run.seen['events'] if event.startswith('signal ')]

    def test_signal_before_apply_stops_the_worker_and_refuses_its_apply_claim(self):
        run = Supervised(self, ['update', 'demo', '--settings-file', '-'], stdin=json.dumps({'memory': '4g'}))
        self.assertEqual(json.loads(run.expect('event', 'settings').split(' ', 1)[1]), {'memory': '4g'})
        run.started()
        run.expect('event', 'waiting')
        run.events.put('waiting')  # Keep the observed milestone for the next send().
        run.signal(signal.SIGINT)
        run.expect('stderr', CANCELLING)
        run.expect('event', 'signal SIGINT')  # The non-cooperative worker is stopped.
        run.signal(signal.SIGTERM)
        repeated = run.expect('stderr', '')  # Any next diagnostic line.
        run.signal(signal.SIGHUP)
        run.expect('stderr', '')
        run.send('claim')
        self.assertEqual(run.expect('event', 'claim'), 'claim refused')
        run.send('exit 0')
        self.assertEqual(run.finish(), 130, run.diagnostics())
        stderr = run.stderr_after_exit()
        self.assertFalse(operation_locks.in_use('demo'), 'The sandbox lock outlived the cancelled update')
        self.assertNotIn('signal SIGKILL', run.seen['events'])
        # Feedback for a repeated signal describes the cancellation already in progress.
        self.assertNotEqual(repeated, APPLYING, 'A repeated signal during cancellation was reported as applying')
        self.assertTrue(any('cancel' in line.lower() for line in stderr[stderr.index(CANCELLING) + 1:]),
                        f'No completion message after cancellation: {stderr}')

    def test_signals_after_apply_wait_for_the_worker_without_interrupting_it(self):
        run = Supervised(self, ['update', 'demo', '--settings-file', self.file('s.json', {'memory': '4g'}), '--json'],
                         stdout='pipe-read')
        run.started()
        run.send('claim')
        self.assertEqual(run.expect('event', 'claim'), 'claim won')
        # The worker reused the CLI's lock; an independent command is refused meanwhile.
        with self.assertRaisesRegex(ValueError, 'another Factory operation is using this sandbox'):
            with operation_locks.sandbox('demo', 'test'):
                pass
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            run.signal(number)
            run.expect('stderr', APPLYING)
        run.send('say applied the settings')
        run.expect('stderr', 'applied the settings')  # Structured mode logs progress to stderr.
        run.send('exit 0')
        self.assertEqual(run.finish(), 0, run.diagnostics())
        self.assertEqual(json.loads(run.expect('stdout', '{"name"')),
                         {'name': 'demo', 'operation': 'update', 'status': 'complete'})
        self.assertEqual(self.worker_signals(run), [], 'A signal after the apply gate reached the worker')
        self.assertFalse(operation_locks.in_use('demo'))

    def test_failed_apply_keeps_the_worker_diagnostic(self):
        run = Supervised(self, ['update', 'demo', '--settings-file', self.file('s.json', {'memory': '4g'})])
        run.started()
        run.send('claim')
        run.send('say Error: podman build exited with status 125: no space left on device')
        run.send('exit 1')
        self.assertEqual(run.finish(), 1)
        stderr = '\n'.join(run.stderr_after_exit())
        self.assertIn('Update command exited with status 1', stderr)
        self.assertIn('no space left on device', stderr)

    def test_cooperative_worker_finishes_its_own_cancellation(self):
        for exit_code, expected in ((0, 130), (3, 1)):
            with self.subTest(worker_exit=exit_code):
                run = Supervised(self, ['profile-apply', 'demo', '--revision', 'rev-2'])
                run.started()
                run.expect('event', 'waiting')
                run.events.put('waiting')
                run.signal(signal.SIGINT)
                run.expect('stderr', CANCELLING)
                run.send('claim')
                self.assertEqual(run.expect('event', 'claim'), 'claim refused')
                run.send('say rolled back profile preparation')
                run.send(f'exit {exit_code}')
                self.assertEqual(run.finish(), expected, run.diagnostics())
                stderr = '\n'.join(run.stderr_after_exit())
                self.assertEqual(self.worker_signals(run), [], 'A cooperative worker was signalled')
                if exit_code:
                    self.assertIn('Profile cancellation could not finish cleanly (worker exit 3)', stderr)
                    self.assertIn('rolled back profile preparation', stderr)

    def closed_output(self, kind, *, applying):
        run = Supervised(self, ['update', 'demo', '--settings-file', self.file('s.json', {'memory': '4g'})], stdout=kind)
        run.started()
        if applying:
            run.send('claim')
            self.assertEqual(run.expect('event', 'claim'), 'claim won')
        run.close_output()
        run.send('say output after the terminal closed')
        return run

    def test_closed_output_before_apply_cancels_and_cleans_up(self):
        for kind in ('pipe', 'pty'):  # EPIPE and EIO
            with self.subTest(output=kind):
                run = self.closed_output(kind, applying=False)
                run.expect('event', 'signal SIGINT')
                run.send('claim')
                self.assertEqual(run.expect('event', 'claim'), 'claim refused')
                run.send('exit 0')
                code = run.finish()
                stderr = run.stderr_after_exit()
                self.assertEqual(code, 130, stderr)
                self.assertFalse(any('Traceback' in line for line in stderr), stderr)
                self.assertFalse(operation_locks.in_use('demo'))

    def test_closed_output_after_apply_lets_the_worker_finish(self):
        for kind in ('pipe', 'pty'):  # EPIPE and EIO
            with self.subTest(output=kind):
                run = self.closed_output(kind, applying=True)
                run.send('say more output')
                run.send('exit 0')
                code = run.finish()
                stderr = run.stderr_after_exit()
                self.assertEqual(self.worker_signals(run), [], 'Closing the terminal interrupted an applying update')
                self.assertFalse(any('Traceback' in line for line in stderr), stderr)
                self.assertEqual(code, 0, stderr)


# Child-process entry points ---------------------------------------------------------------------


def launcher(argv):
    """Run the real CLI with private locks and a synthetic worker or saved-configuration step."""
    from ai_sandbox_factory import cli, operation_locks, operations
    locks = Path(os.environ['FACTORY_TEST_LOCKS'])
    operation_locks.private_runtime_dir = lambda: locks
    if os.environ['FACTORY_TEST_MODE'] == 'update':
        operations.Service.command = lambda service, *args: [sys.executable, str(THIS), '--synthetic-worker',
                                                              '--state-dir', str(service.state), *args]
    else:
        from ai_sandbox_factory import cli_lifecycle, sandbox

        def refuse(*args, **kwargs):
            raise AssertionError('The worker invocation re-entered the public creation adapter')

        def create(args, state):
            state.mkdir(mode=0o700)
            (state / 'config.json').write_text(json.dumps({'name': args.name, 'engine': 'podman'}))
            print('LEGACY-CREATE ' + json.dumps({'name': args.name, 'token': bool(args.creation_token),
                                                 'start': args.start}), flush=True)
        cli_lifecycle.from_arguments = cli_lifecycle.start_creation = refuse
        sandbox.create = create
    sys.argv = ['ai-sandbox-factory', *argv]
    cli.main()


def worker(argv):
    """Stand in for a gated Factory command and follow commands from the test."""
    from ai_sandbox_factory import operation_locks, updating
    locks = Path(os.environ['FACTORY_TEST_LOCKS'])
    operation_locks.private_runtime_dir = lambda: locks
    control = Path(os.environ['FACTORY_TEST_CONTROL'])
    gate = Path(argv[argv.index('--apply-gate') + 1])
    name = argv[argv.index('--state-dir') + 3]
    events = open(control / 'events', 'w', buffering=1)

    def event(text):
        events.write(text + '\n')
        events.flush()

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, lambda number, frame: event('signal ' + signal.Signals(number).name))
    if '--settings-file' in argv:
        event('settings ' + json.dumps(json.loads(Path(argv[argv.index('--settings-file') + 1]).read_text())))
    with operation_locks.sandbox(name, 'run the test worker for'):  # Requires the inherited lock.
        event(f'started {os.getpid()}')
        while True:
            event('waiting')
            with open(control / 'release') as stream:
                command = stream.read()
            if command == 'claim':
                event('claim ' + ('won' if updating.begin_apply(gate) else 'refused'))
            elif command.startswith('say '):
                print(command[4:], flush=True)
            elif command.startswith('exit '):
                return int(command[5:])


if __name__ == '__main__':
    if sys.argv[1:2] == ['--cli-launcher']:
        launcher(sys.argv[2:])
    elif sys.argv[1:2] == ['--synthetic-worker']:
        raise SystemExit(worker(sys.argv[2:]))
    else:
        unittest.main()
