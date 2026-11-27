"""Replay generated requests through the public parser; exported text contains no keys."""
import copy
import getpass
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from ai_sandbox_factory import (cli_recipes, cli_support, connector_settings, connectors, credential_inputs, creation, operation_locks,
                                editor_settings, operations, sandbox)
from ai_sandbox_factory.sandbox_options import options
from test_cli_lifecycle import CliCase

SECRET = 'synthetic-private-key-never-export-this'
REF = 'a' * 32


class Recipes(CliCase):
    def setUp(self):
        super().setUp()
        self.jobs = []
        self.enterContext(patch.object(sandbox, 'choose_engine', return_value='podman'))
        self.enterContext(patch.object(operations.Service, 'check_creation',
            return_value=creation.ResourceConflicts('podman', None, (), ())))
        self.enterContext(patch.object(operations.Service, 'create', autospec=True,
            side_effect=lambda service, draft, keys, log, **kw: self.jobs.append((draft, keys))))

    def draft(self, **changes):
        draft = operations.preset('base', copy.deepcopy(operations.DEFAULT_SETTINGS))
        draft.update(name='demo', workspace=str(self.workspace), engine='podman', memory='8g',
                     model_providers={}, local_model_ports=None, subagents=False, open_editor=False,
                     editor={'mode': 'pi'}, allowlist='', options=options())
        draft.update(changes)
        return draft

    def replay(self, recipe):
        for step in recipe.steps:
            self.assertTrue(step.factory)
            code, out, err = self.cli(*step.argv, stdin=step.stdin or '')
            self.assertEqual(code, 0, err)
        return self.jobs[-1] if self.jobs else None

    def test_simple_creation_and_all_agents(self):
        for agent in ('pi', 'codex', 'claude', 'agy'):
            with self.subTest(agent=agent):
                draft = self.draft(primary_agent=agent, network='unrestricted')
                recipe = cli_recipes.creation(draft, self.state)
                self.assertIsNone(recipe.steps[0].stdin)
                actual, keys = self.replay(recipe)
                for field in ('workspace', 'primary_agent', 'memory', 'network', 'subagents', 'engine'):
                    self.assertEqual(actual[field], draft[field], field)
                self.assertEqual(actual['options'], draft['options'])
                self.assertEqual(actual['editor']['mode'], 'pi')

    def test_complex_creation_keeps_choices_and_prompts_before_job(self):
        draft = self.draft(allowlist='example.com:443\n',
            options=options({'goals': False, 'web_access': {'enabled': True, 'providers': {'brave': {}}}}),
            _provider_credential_choices={'fixture': {'key': SECRET, 'label': 'Work'}},
            _communication_keys={'botToken': SECRET},
            _hosted_changes={'azure': {'endpoint': 'https://example.openai.azure.com', 'key': SECRET}},
            editor={**editor_settings.DEFAULTS, 'password_hash': editor_settings.password_hash(SECRET)})
        recipe = cli_recipes.creation(draft, self.state, web_keys={'brave': SECRET})
        text = recipe.render(launcher=['ai-sandbox-factory'])
        self.assertNotIn(SECRET, text)
        self.assertNotIn('$argon2', text)
        supplied = []
        def prompt(label):
            self.assertFalse(self.jobs, 'A credential prompt happened after creation started')
            supplied.append(label)
            return SECRET
        with patch.object(credential_inputs, 'prompt_secret', side_effect=prompt):
            actual, keys = self.replay(recipe)
        self.assertEqual(len(supplied), 5)
        self.assertEqual(keys, {'brave': SECRET})
        self.assertEqual(actual['_provider_credential_choices']['fixture'], {'key': SECRET, 'label': 'Work'})
        self.assertEqual(actual['_hosted_changes']['azure']['key'], SECRET)
        self.assertEqual(actual['_communication_keys'], {'botToken': SECRET})
        self.assertTrue(actual['options']['web_access']['providers']['brave']['has_key'])
        self.assertFalse(actual['options']['goals'])
        self.assertEqual(actual['allowlist'], 'example.com:443\n')
        self.assertTrue(actual['editor']['password_hash'].startswith('$argon2'))
        self.assertNotIn('password_hash', json.loads(recipe.steps[0].stdin)['editor'])

    def test_saved_hosted_and_local_refs_are_routed_before_creation(self):
        draft = self.draft(local_model_ports='8000', model_providers={'local': {}}, allowlist='local-model.internal:8000',
            _local_model_keys={'8000': {'ref': REF}},
            _hosted_changes={'openrouter': {'credential': {'ref': REF}}},
            _provider_credential_choices={'fixture': {'ref': REF}})
        with patch.object(credential_inputs, 'prompt_secret', side_effect=AssertionError('Must reuse references')):
            actual, _ = self.replay(cli_recipes.creation(draft, self.state))
        self.assertEqual(actual['_local_model_keys'], {'8000': {'ref': REF}})
        self.assertEqual(actual['_hosted_changes']['openrouter']['credential'], {'ref': REF})
        self.assertEqual(actual['_provider_credential_choices'], {'fixture': {'ref': REF}})

    def test_new_native_editor_password_is_masked_and_accepted(self):
        draft = self.draft(primary_agent='codex', editor={**editor_settings.DEFAULTS,
            'address': '127.0.0.1', 'password_hash': editor_settings.password_hash(SECRET)})
        with patch.object(credential_inputs, 'prompt_secret', return_value=SECRET):
            actual, _ = self.replay(cli_recipes.creation(draft, self.state))
        self.assertTrue(actual['editor']['password_hash'])
        self.assertNotIn(SECRET, cli_recipes.creation(draft, self.state).render())

    def test_credential_prompt_failure_stops_before_creation_without_echo(self):
        recipe = cli_recipes.creation(self.draft(_provider_keys={'fixture': SECRET}), self.state)
        with patch.object(getpass, 'getpass', side_effect=getpass.GetPassWarning('No terminal')):
            code, out, err = self.cli(*recipe.steps[0].argv, stdin=recipe.steps[0].stdin)
        self.assertEqual(code, 1)
        self.assertIn('terminal', err)
        self.assertFalse(self.jobs)
        self.assertNotIn(SECRET, out + err)

    def test_public_stale_revision_is_not_dropped(self):
        _, source = self.source()
        revision = sandbox.config_revision(source)
        draft = self.draft(reuse_from='source', _reuse_config_revision=revision)
        actual, _ = self.replay(cli_recipes.creation(draft, self.state))
        self.assertEqual(actual['reuse_from'], 'source')
        self.assertEqual(actual['_reuse_config_revision'], revision)
        code, _, err = self.cli('create', 'demo', '--workspace', str(self.workspace),
                                '--reuse-config-revision', 'revision', '--start')
        self.assertEqual(code, 1)
        self.assertIn('--reuse-from', err)

    def test_network_replays_live_operation_and_never_rebuilds(self):
        state = self.sandbox()
        config = json.loads((state / 'config.json').read_text())
        config.update(network='allowlist')
        (state / 'config.json').write_text(json.dumps(config))
        recipe = cli_recipes.network('demo', 'example.com:443\n', [], self.state)
        with patch.object(sandbox, 'update_network') as live, \
                patch.object(operations.Service, 'run_update', side_effect=AssertionError('Must stay live')):
            self.replay(recipe)
        self.assertEqual(live.call_args.args[2:], ('example.com:443\n', []))

    def test_editor_password_replays_without_hash_and_preserves_other_settings(self):
        self.sandbox()
        recipe = cli_recipes.editor('demo', {'allow_uploads': True,
            'password_hash': editor_settings.password_hash(SECRET)}, self.state)
        with patch('ai_sandbox_factory.cli_management.prompt_secret', return_value=SECRET), \
                patch.object(operations.Service, 'save_editor', return_value={'changed': True, 'editor_stopped': False}) as save:
            self.replay(recipe)
        changes = save.call_args.args[1]
        self.assertTrue(changes['allow_uploads'])
        self.assertTrue(changes['password_hash'].startswith('$argon2'))
        self.assertNotIn('$argon2', recipe.render())
        self.assertNotIn(SECRET, recipe.render())

    def test_full_update_and_setup_have_same_order_and_preserve_selected_credentials(self):
        draft = self.draft(_local_model_keys={'8000': {'ref': REF}},
            _provider_keys={'fixture': SECRET}, _provider_credential_choices={'other': {'ref': REF}},
            _communication_keys={'botToken': SECRET, 'userToken': ''},
            model_providers={'fixture': {}}, open_editor=False)
        recipe = cli_recipes.full_update(draft, {'brave': SECRET, 'tavily': ''}, self.state)
        self.assertEqual([step.argv[0] for step in recipe.steps],
                         ['update', 'credentials', 'configure', 'credentials', 'communication-key', 'web-key', 'configure'])
        self.assertIn('--no-refresh', recipe.steps[1].argv)
        self.assertIn('--clear-token', recipe.steps[4].argv)
        self.assertIn('--clear-provider', recipe.steps[5].argv)
        self.assertEqual(json.loads(recipe.steps[0].stdin), operations.update_changes(draft))
        self.assertNotIn(SECRET, recipe.render())

    def source(self, **changes):
        state = self.state / 'source'
        state.mkdir()
        (state / 'allowlist.txt').write_text('')
        config = self.draft(name='source', profile=sandbox.snapshot_profile(sandbox.load_profile('base'), state))
        config['model_providers'] = []
        config.update(changes)
        (state / 'config.json').write_text(json.dumps(config))
        return state, config

    def test_saved_setup_has_fresh_editor_connection_and_no_private_mounts(self):
        state, config = self.source(env_file='/private/auth.env', pi_config='/private/pi')
        editor_settings.save(state, {**editor_settings.DEFAULTS, 'port': 12345,
            'password_hash': editor_settings.password_hash(SECRET)})
        recipe = cli_recipes.saved(config, self.state)
        actual, _ = self.replay(recipe)
        self.assertEqual(actual['editor']['port'], 0)
        self.assertFalse(actual['editor']['password_hash'])
        self.assertFalse(actual['env_file'])
        self.assertFalse(actual['pi_config'])
        self.assertNotIn('/private', recipe.render())
        self.assertNotIn('12345', recipe.render())

    def test_saved_setup_keeps_managed_refs_and_handles_optional_offline_local_keys(self):
        state, config = self.source(local_model_ports='8000', hosted_connections={'openrouter': {}},
            model_providers=[{'name': 'fixture', 'settings': {}, 'manifest': {
                'inference': {'auth': {'type': 'bearer'}}}}])
        (state / 'allowlist.txt').write_text('local-model.internal:8000')
        status = {'configured': ['openrouter', 'fixture', 'local-8000'],
                  'assignments': {key: {'ref': REF} for key in ('openrouter', 'fixture', 'local-8000')}}
        recipe = cli_recipes.saved(config, self.state, credential_status=status)
        with patch.object(credential_inputs, 'prompt_secret', side_effect=AssertionError('Must reuse key references')):
            actual, _ = self.replay(recipe)
        self.assertEqual(actual['_local_model_keys']['8000'], {'ref': REF})
        self.assertEqual(actual['_provider_credential_choices']['fixture'], {'ref': REF})
        self.assertEqual(actual['_hosted_changes']['openrouter']['credential'], {'ref': REF})
        config.update(hosted_connections={}, model_providers=[])
        (state / 'config.json').write_text(json.dumps(config))
        recipe = cli_recipes.saved(config, self.state)
        with patch.object(credential_inputs, 'prompt_secret', return_value=''):
            actual, _ = self.replay(recipe)
        self.assertFalse(actual.get('_local_model_keys'))
        self.assertIn('press Enter to skip', recipe.render())

    def test_empty_required_key_fails_before_starting_job(self):
        recipe = cli_recipes.creation(self.draft(_provider_keys={'fixture': SECRET}), self.state)
        with patch.object(credential_inputs, 'prompt_secret', return_value=''):
            code, _, err = self.cli(*recipe.steps[0].argv, stdin=recipe.steps[0].stdin)
        self.assertEqual(code, 1)
        self.assertIn('is empty', err)
        self.assertFalse(self.jobs)

    def test_optional_empty_stdin_credentials_do_not_create_empty_assignments(self):
        prompts = [{'path': ['local_model_keys', '8000', 'key'], 'label': 'Local key', 'required': False}]
        with patch.object(credential_inputs, 'prompt_secret', side_effect=AssertionError('Supplied input must win')):
            self.assertEqual(credential_inputs.collect(prompts, {'local_model_keys': {'8000': {'key': ''}}}), {})

    def test_saved_recipe_reads_references_only_from_an_already_running_companion(self):
        _, config = self.source(hosted_connections={'openrouter': {}})
        status = {'configured': ['openrouter'], 'assignments': {'openrouter': {'ref': REF}}}
        manager = operations.Service(self.state)
        with patch('ai_sandbox_factory.inference.enabled', return_value=True), \
                patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}), \
                patch('ai_sandbox_factory.inference.credentials', side_effect=lambda *args:
                    self.assertFalse(operation_locks.in_use('source')) or status) as read:
            recipe = manager.cli_recipe('source')
        self.assertIn(REF, recipe.render())
        self.assertEqual(read.call_args.args[2], {'status': True})
        with patch('ai_sandbox_factory.inference.enabled', return_value=True), \
                patch.object(sandbox, 'inspect', return_value={'State': {'Running': False}}), \
                patch('ai_sandbox_factory.inference.credentials', side_effect=AssertionError('Must not start or query stopped service')):
            recipe = manager.cli_recipe('source')
        self.assertIn('credential_prompts', recipe.render())

    def test_show_commands_has_no_inventory_or_start_side_effect(self):
        self.source(primary_agent='codex')
        with patch.object(operations.Service, 'inventory', side_effect=AssertionError('No engine inventory needed')):
            code, out, err = self.cli('show', 'source', '--section', 'commands')
        self.assertEqual(code, 0, err)
        self.assertIn('source-copy', out)
        self.assertIn('--reuse-config-revision', out)

    def test_bad_workspace_or_stale_source_fails_before_any_credential_prompt(self):
        _, config = self.source()
        for changes, message in (({'workspace': str(self.root / 'missing')}, 'Workspace'),
                                 ({'reuse_from': 'source', '_reuse_config_revision': 'stale'}, 'changed after'),
                                 ({'editor': {'port': -1}}, 'Port')):
            draft = self.draft(_provider_keys={'fixture': SECRET}, **changes)
            recipe = cli_recipes.creation(draft, self.state)
            with patch.object(credential_inputs, 'prompt_secret', side_effect=AssertionError('Check settings first')):
                code, _, err = self.cli(*recipe.steps[0].argv, stdin=recipe.steps[0].stdin)
            self.assertEqual(code, 1, err)
            self.assertIn(message, err)
        self.assertFalse(self.jobs)

    def test_saved_iam_credentials_include_an_optional_session_token(self):
        _, config = self.source(hosted_connections={'amazon-bedrock': {'region': 'eu-west-1', 'method': 'iam'}})
        recipe = cli_recipes.saved(config, self.state)
        prompts = {tuple(row['path']): row for row in json.loads(recipe.steps[0].stdin)['credential_prompts']}
        self.assertFalse(prompts[('hosted', 'amazon-bedrock', 'session_token')]['required'])
        self.assertIn(('hosted', 'amazon-bedrock', 'access_key'), prompts)
        self.assertIn(('hosted', 'amazon-bedrock', 'secret_key'), prompts)

    def test_reviewed_connector_permissions_are_declared_once_even_when_empty(self):
        for grants in ([], ['summary']):
            recipe = cli_recipes.creation(self.draft(connector_ids=['status'], connector_operations={'status': grants},
                _connector_routes={'status': {}}, _connector_instructions={'status': 'Use status.'}), self.state)
            value = json.loads(recipe.steps[0].stdin)
            self.assertEqual(value['connectors'], ['status'])
            actual, _ = self.replay(recipe)
            self.assertEqual(actual['connector_operations'], {'status': grants})

    def test_connector_copy_uses_saved_manifest_after_registry_update(self):
        from test_connectors import manifest
        installed = self.root / 'installed'
        path = installed / 'status' / 'connector.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest()))
        previous = self.root / 'previous'
        previous.mkdir()
        target = self.root / 'new'
        target.mkdir()
        with patch.object(connectors, 'directory', return_value=installed):
            connectors.snapshot(['status'], previous)
            original = connectors.saved({'connector_ids': ['status']}, previous)[0]
            path.write_text(json.dumps(manifest('https://changed.example.test/status')))
            connector_settings.capture(['status'], target, {'status': dict(original.routes)},
                {'status': original.reviewed_instructions}, source=({'connector_ids': ['status']}, previous))
        self.assertEqual((previous / 'connector-manifests/status.json').read_bytes(),
                         (target / 'connector-manifests/status.json').read_bytes())

    def test_default_state_directory_is_omitted_including_a_symlinked_home(self):
        home = self.root / 'home'
        home.mkdir()
        alias = self.root / 'home-alias'
        alias.symlink_to(home, target_is_directory=True)
        default = home / '.local/state/ai-sandbox-factory'
        with patch.object(Path, 'home', return_value=alias):
            recipe = cli_recipes.Recipe('Start', (cli_recipes.Step(('up', 'demo')),), str(default))
            self.assertNotIn('--state-dir', recipe.render(launcher=['ai-sandbox-factory']))
            custom = cli_recipes.Recipe('Start', recipe.steps, str(self.state))
            self.assertIn('--state-dir', custom.render(launcher=['ai-sandbox-factory']))

    def test_launcher_uses_this_version_outside_the_checkout(self):
        result = subprocess.run([*cli_recipes.command(), 'create', '--help'], cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--reuse-config-revision', result.stdout)

    def test_shell_replay_preserves_literals_and_stops_after_failure(self):
        recorder = self.root / 'record.py'
        result = self.root / 'record.json'
        recorder.write_text('import json,sys\nfrom pathlib import Path\n'
                            'Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:],sys.stdin.read()]))\n')
        payload = 'literal $HOME `touch bad`\nFACTORY_CONFIG\nlast line\n'
        recipe = cli_recipes.Recipe('Literal', (cli_recipes.Step(('value with spaces', "'quoted'", '$(touch bad)'), payload),), str(self.state))
        for width in (20, 80, 220):
            text = recipe.render(width=width, launcher=[sys.executable, str(recorder), str(result)])
            process = subprocess.run(['sh'], input=text, text=True, capture_output=True, cwd=self.root)
            self.assertEqual(process.returncode, 0, process.stderr)
            argv, stdin = json.loads(result.read_text())
            self.assertEqual(argv, ['--state-dir', str(self.state), 'value with spaces', "'quoted'", '$(touch bad)'])
            self.assertEqual(stdin, payload)
            self.assertFalse((self.root / 'bad').exists())
        marker = self.root / 'must-not-exist'
        failure = cli_recipes.Recipe('Failure', (cli_recipes.Step(('false',), factory=False),
            cli_recipes.Step(('touch', str(marker)), factory=False)), str(self.state))
        result = subprocess.run(['sh'], input=failure.render(), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
