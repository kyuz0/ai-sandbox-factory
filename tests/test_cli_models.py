"""Command-line adapters for models and saved keys, with the shared operations mocked."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_sandbox_factory import cli_models

KEY = 'sk-synthetic-test-key-0123456789'
BINDING = {'provider': 'openrouter', 'endpoint': 'https://openrouter.ai/api/v1', 'auth': 'bearer'}
REF = 'a' * 32


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class CliModelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Mock()
        self.service.config.return_value = {'name': 'demo', 'hosted_connections': {}}
        patcher = patch.object(cli_models.operations, 'Service', return_value=self.service)
        self.service_class = patcher.start()
        self.addCleanup(patcher.stop)

    def parse(self, *argv):
        parser = argparse.ArgumentParser(prog='ai-sandbox-factory')
        parser.add_argument('--state-dir', default=self.temp.name)
        cli_models.register(parser.add_subparsers(dest='command', required=True))
        return parser.parse_args(['--state-dir', self.temp.name, *argv])

    def run_cli(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        source = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
        with patch('sys.stdin', source), redirect_stdout(out), redirect_stderr(err):
            args = self.parse(*argv)
            args.cli_handler(args)
        return out.getvalue(), err.getvalue()

    def fails(self, *argv, stdin=''):
        with self.assertRaises(ValueError) as caught:
            self.run_cli(*argv, stdin=stdin)
        return str(caught.exception)

    def usage_error(self, *argv):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            self.parse(*argv)
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def settings(self, value):
        path = Path(self.temp.name) / 'connection.json'
        path.write_text(json.dumps(value))
        return str(path)

    # models connections / disconnect

    def test_connections_uses_shared_status_and_prints_one_json_value(self):
        self.service.hosted_request.return_value = {'providers': [
            {'provider': 'azure', 'configured': True, 'has_key': True,
             'assignment': {'ref': REF, 'label': 'Team'}, 'endpoint': 'https://example.openai.azure.com'}]}
        out, _ = self.run_cli('models', 'connections', 'demo', '--json')
        self.service_class.assert_called_once_with(Path(self.temp.name))
        self.service.hosted_request.assert_called_once_with('demo', 'status')
        self.assertEqual(json.loads(out)['providers'][0]['assignment']['label'], 'Team')
        out, _ = self.run_cli('models', 'connections', 'demo')
        self.assertIn('azure · key assigned · saved key “Team” · endpoint: https://example.openai.azure.com', out)

    def test_connections_without_rows_names_the_command_that_adds_one(self):
        self.service.hosted_request.return_value = {'providers': []}
        out, _ = self.run_cli('models', 'connections', 'demo')
        self.assertIn('models connect demo --settings-file FILE', out)

    def test_disconnect_calls_shared_removal(self):
        out, _ = self.run_cli('models', 'disconnect', 'demo', '--provider', 'together')
        self.service.hosted_remove.assert_called_once_with('demo', 'together')
        self.assertIn('Removed the Together connection from demo.', out)

    def test_hosted_commands_reject_unknown_providers_with_the_supported_list(self):
        error = self.fails('models', 'disconnect', 'demo', '--provider', 'openai')
        self.assertIn('"openai" is not a hosted model connection', error)
        self.assertIn('openrouter, together, azure, amazon-bedrock', error)
        self.assertIn('--plugin', error)
        self.service.hosted_remove.assert_not_called()

    # models connect

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_connect_reads_key_from_stdin_and_remembers_it(self, scope):
        self.service.hosted_save.return_value = {'provider': 'openrouter', 'configured': True,
                                                 'assignment': {'ref': REF, 'label': 'Team'}}
        path = self.settings({'provider': 'openrouter'})
        out, _ = self.run_cli('models', 'connect', 'demo', '--settings-file', path, '--key-stdin',
                              '--remember', 'Team', '--json', stdin=KEY + '\n')
        name, value = self.service.hosted_save.call_args.args
        self.assertEqual(name, 'demo')
        self.assertEqual(value['key'], KEY)
        self.assertEqual(value['credential'], {'key': KEY, 'label': 'Team'})
        self.assertEqual(value['provider'], 'openrouter')
        self.assertEqual(json.loads(out)['assignment']['label'], 'Team')
        self.assertNotIn(KEY, out)

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_connect_with_saved_key_sends_only_the_reference(self, scope):
        self.service.hosted_save.return_value = {'provider': 'openrouter'}
        path = self.settings({'provider': 'openrouter'})
        self.run_cli('models', 'connect', 'demo', '--settings-file', path, '--saved-key', REF)
        value = self.service.hosted_save.call_args.args[1]
        self.assertEqual(value['credential'], {'ref': REF})
        self.assertEqual(value['key'], '')

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_connect_keeps_existing_credentials_when_none_are_supplied(self, scope):
        self.service.config.return_value = {'hosted_connections': {'azure': {'endpoint': 'https://example.openai.azure.com'}}}
        self.service.hosted_save.return_value = {'provider': 'azure'}
        path = self.settings({'provider': 'azure', 'endpoint': 'https://example.openai.azure.com'})
        self.run_cli('models', 'connect', 'demo', '--settings-file', path)
        value = self.service.hosted_save.call_args.args[1]
        self.assertNotIn('credential', value)
        self.assertEqual([value[f] for f in cli_models.SECRET_FIELDS], ['', '', '', ''])

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_new_connection_prompts_masked_in_a_terminal(self, scope):
        self.service.hosted_save.return_value = {'provider': 'openrouter'}
        path = self.settings({'provider': 'openrouter'})
        with patch('ai_sandbox_factory.cli_support.getpass.getpass', return_value=KEY) as prompt:
            self.run_cli('models', 'connect', 'demo', '--settings-file', path, stdin=TerminalInput())
        prompt.assert_called_once_with('OpenRouter API key: ')
        self.assertEqual(self.service.hosted_save.call_args.args[1]['credential'], {'key': KEY})

    def test_new_connection_without_terminal_or_key_explains_key_stdin(self):
        path = self.settings({'provider': 'openrouter'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', path)
        self.assertIn('--key-stdin', error)
        self.service.hosted_save.assert_not_called()

    def test_connect_rejects_credentials_in_the_settings_file_without_echoing_them(self):
        path = self.settings({'provider': 'openrouter', 'key': KEY, 'session_token': 'synthetic-token'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', path)
        self.assertIn('credential fields (key, session_token)', error)
        self.assertIn('--key-stdin', error)
        self.assertNotIn(KEY, error)
        self.assertNotIn('synthetic-token', error)
        self.service.hosted_save.assert_not_called()

    def test_connect_rejects_a_settings_file_that_is_not_an_object(self):
        error = self.fails('models', 'connect', 'demo', '--settings-file', self.settings(['openrouter']))
        self.assertIn('must contain a JSON object', error)
        error = self.fails('models', 'connect', 'demo', '--settings-file', str(Path(self.temp.name) / 'missing.json'))
        self.assertIn('Cannot read JSON from', error)

    def test_connect_names_an_unsupported_provider(self):
        error = self.fails('models', 'connect', 'demo', '--settings-file', self.settings({'provider': 'openai'}))
        self.assertIn('"openai" is not a hosted model connection', error)

    def test_connect_reads_iam_credentials_as_json_on_stdin(self):
        self.service.hosted_save.return_value = {'provider': 'amazon-bedrock', 'region': 'eu-west-1', 'method': 'iam'}
        path = self.settings({'provider': 'amazon-bedrock', 'region': 'eu-west-1', 'method': 'iam'})
        secrets = {'access_key': 'AKIASYNTHETIC', 'secret_key': 'synthetic-secret'}
        self.run_cli('models', 'connect', 'demo', '--settings-file', path, '--secrets-stdin', stdin=json.dumps(secrets))
        value = self.service.hosted_save.call_args.args[1]
        self.assertEqual((value['access_key'], value['secret_key'], value['session_token'], value['key']),
                         ('AKIASYNTHETIC', 'synthetic-secret', '', ''))
        self.assertNotIn('credential', value)

    def test_iam_and_api_key_options_are_checked_against_the_method(self):
        iam = self.settings({'provider': 'amazon-bedrock', 'region': 'eu-west-1', 'method': 'iam'})
        self.assertIn('--secrets-stdin', self.fails('models', 'connect', 'demo', '--settings-file', iam, '--key-stdin'))
        self.assertIn('--secrets-stdin', self.fails('models', 'connect', 'demo', '--settings-file', iam))
        bearer = self.settings({'provider': 'openrouter'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', bearer, '--secrets-stdin', stdin='{}')
        self.assertIn('applies only to "provider": "amazon-bedrock"', error)

    def test_iam_secrets_reject_unknown_and_non_string_fields(self):
        path = self.settings({'provider': 'amazon-bedrock', 'region': 'eu-west-1', 'method': 'iam'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', path, '--secrets-stdin',
                           stdin=json.dumps({'access_key': 'AKIA', 'secret_key': 's', 'key': KEY}))
        self.assertIn('unsupported fields (key)', error)
        self.assertNotIn(KEY, error)
        error = self.fails('models', 'connect', 'demo', '--settings-file', path, '--secrets-stdin',
                           stdin=json.dumps({'access_key': 1, 'secret_key': 's'}))
        self.assertIn('must be strings: access_key', error)

    def test_credential_sources_are_mutually_exclusive(self):
        path = self.settings({'provider': 'openrouter'})
        error = self.usage_error('models', 'connect', 'demo', '--settings-file', path, '--key-stdin', '--secrets-stdin')
        self.assertIn('not allowed with argument', error)
        self.usage_error('models', 'connect', 'demo', '--settings-file', path, '--key-stdin', '--saved-key', REF)

    def test_settings_and_credentials_cannot_share_stdin(self):
        error = self.fails('models', 'connect', 'demo', '--settings-file', '-', '--key-stdin')
        self.assertIn('either the settings file or the credentials', error)

    def test_remember_requires_an_entered_key(self):
        path = self.settings({'provider': 'openrouter'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', path, '--saved-key', REF, '--remember', 'Team')
        self.assertIn('Add --key-stdin', error)

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_upstream_errors_keep_their_cause_without_the_supplied_key(self, scope):
        self.service.hosted_save.side_effect = ValueError(f'Upstream 401 for Bearer {KEY}: invalid_api_key')
        path = self.settings({'provider': 'openrouter'})
        error = self.fails('models', 'connect', 'demo', '--settings-file', path, '--key-stdin', stdin=KEY)
        self.assertIn('Upstream 401 for Bearer [redacted]: invalid_api_key', error)
        self.assertNotIn(KEY, error)

    @patch.object(cli_models.hosted_models, 'credential_scope', return_value=BINDING)
    def test_upstream_errors_without_secrets_are_raised_unchanged(self, scope):
        failure = ValueError('Start the sandbox before configuring models.')
        self.service.hosted_save.side_effect = failure
        path = self.settings({'provider': 'openrouter'})
        with self.assertRaises(ValueError) as caught:
            self.run_cli('models', 'connect', 'demo', '--settings-file', path, '--key-stdin', stdin=KEY)
        self.assertIs(caught.exception, failure)

    # models list / select / test

    def test_list_reads_saved_models_unless_refresh_is_explicit(self):
        self.service.hosted_request.return_value = {'models': [{'id': 'm1', 'name': 'Model one'}], 'warning': ''}
        out, _ = self.run_cli('models', 'list', 'demo', '--provider', 'openrouter')
        self.service.hosted_request.assert_called_once_with('demo', 'catalogue', provider='openrouter')
        self.assertEqual(out.strip(), 'm1  Model one')
        self.service.hosted_request.reset_mock()
        self.service.hosted_request.return_value = {'models': [], 'warning': 'Catalogue refresh failed; showing saved models.'}
        out, err = self.run_cli('models', 'list', 'demo', '--provider', 'openrouter', '--refresh')
        self.service.hosted_request.assert_called_once_with('demo', 'catalogue', provider='openrouter', refresh=True)
        self.assertIn('Catalogue refresh failed', err)
        self.assertIn('No models are saved for openrouter in demo.', out)

    def test_list_plugin_models_through_shared_plugin_operation(self):
        self.service.plugin_model.return_value = {'models': [{'id': 'p1', 'name': 'P'}]}
        out, _ = self.run_cli('models', 'list', 'demo', '--provider', 'acme', '--plugin', '--json')
        self.service.plugin_model.assert_called_once_with('demo', 'catalogue', provider='acme', refresh=False)
        self.assertEqual(json.loads(out)['models'][0]['id'], 'p1')
        self.service.hosted_request.assert_not_called()

    def test_select_passes_azure_deployment_and_bedrock_profile(self):
        self.service.hosted_select.return_value = {'provider': 'azure', 'model': 'gpt-x'}
        out, _ = self.run_cli('models', 'select', 'demo', '--provider', 'azure', '--model', 'gpt-x', '--deployment', 'prod')
        self.service.hosted_select.assert_called_once_with('demo', 'azure', 'gpt-x', deployment='prod', profile_id='')
        self.assertIn('Default chat model for demo: azure/gpt-x.', out)

    def test_select_plugin_model_and_reject_hosted_only_options(self):
        self.service.plugin_model.return_value = {'provider': 'acme', 'model': 'p1'}
        self.run_cli('models', 'select', 'demo', '--provider', 'acme', '--model', 'p1', '--plugin')
        self.service.plugin_model.assert_called_once_with('demo', 'select', provider='acme', model='p1')
        error = self.fails('models', 'select', 'demo', '--provider', 'acme', '--model', 'p1', '--plugin', '--deployment', 'x')
        self.assertIn('--deployment applies only to hosted Azure and AWS Bedrock', error)

    def test_test_uses_shared_hosted_test_and_explains_plugin_limit(self):
        self.service.hosted_test.return_value = {'ok': True}
        out, _ = self.run_cli('models', 'test', 'demo', '--provider', 'amazon-bedrock', '--model', 'm',
                              '--profile-id', 'eu.m', '--json')
        self.service.hosted_test.assert_called_once_with('demo', 'amazon-bedrock', 'm', deployment='', profile_id='eu.m')
        self.assertEqual(json.loads(out), {'ok': True})
        error = self.fails('models', 'test', 'demo', '--provider', 'acme', '--model', 'p1', '--plugin')
        self.assertIn('Testing a model of provider plugin acme is not supported', error)
        self.service.hosted_test.assert_called_once()

    def test_model_request_failures_are_preserved(self):
        failure = ValueError('The model request failed: 403 model access denied')
        self.service.hosted_test.side_effect = failure
        with self.assertRaises(ValueError) as caught:
            self.run_cli('models', 'test', 'demo', '--provider', 'together', '--model', 'm')
        self.assertIs(caught.exception, failure)

    # approval models

    def test_approval_models_list_and_select(self):
        self.service.approval_models.return_value = [{'provider': 'openrouter', 'id': 'm1', 'name': 'One'}]
        out, _ = self.run_cli('models', 'approval-models', 'demo')
        self.assertEqual(out.strip(), 'openrouter/m1  One')

        def save(name, model, log):
            log('Updating demo…')
            print('stray output from a shared operation')
        self.service.save_approval_model.side_effect = save
        out, err = self.run_cli('models', 'select-approval-model', 'demo', '--model', 'openrouter/m1', '--json')
        self.assertEqual(json.loads(out), {'name': 'demo', 'approval_model': 'openrouter/m1'})
        self.assertIn('Updating demo…', err)
        self.assertIn('stray output', err)
        out, _ = self.run_cli('models', 'select-approval-model', 'demo', '--model', 'current')
        self.assertIn('Approvals in demo use the chat model.', out)

    # credentials

    def test_credentials_list_prints_metadata_only(self):
        records = {REF: {'label': 'Team', 'scope': BINDING}}
        with patch.object(cli_models.provider_credentials, 'entries', return_value=records):
            out, _ = self.run_cli('credentials', 'list', '--json')
        self.assertEqual(json.loads(out), [{'ref': REF, 'label': 'Team', **BINDING}])
        with patch.object(cli_models.provider_credentials, 'entries', return_value={}):
            out, _ = self.run_cli('credentials', 'list')
        self.assertIn('No provider keys are saved', out)

    def test_credentials_status_passes_local_flag(self):
        self.service.provider_key_status.return_value = {'configured': ['local-8080', 'acme'],
                                                         'assignments': {'acme': {'ref': REF, 'label': 'Team'}}}
        out, _ = self.run_cli('credentials', 'status', 'demo', '--local')
        self.service.provider_key_status.assert_called_once_with('demo', local=True)
        self.assertIn('local-8080 · key entered', out)
        self.assertIn('acme · saved key “Team”', out)

    def test_credentials_save_binds_local_routes_to_local_scope(self):
        route = {'origin': 'http://local-model.internal:8080', 'local': True, 'auth': {'type': 'bearer'}}
        binding = {'provider': 'local', 'endpoint': 'http://local-model.internal:8080/v1', 'auth': 'bearer'}
        with patch.object(cli_models.inference, 'routes', return_value={'local-8080': route}), \
                patch.object(cli_models.provider_credentials, 'scope', return_value=binding) as scope, \
                patch.object(cli_models.provider_credentials, 'save', return_value=REF) as save:
            out, _ = self.run_cli('credentials', 'save', 'demo', '--provider', 'local-8080', '--label', ' Lab ',
                                  '--key-stdin', '--json', stdin=KEY + '\n')
        scope.assert_called_once_with('local', route)
        save.assert_called_once_with(binding, ' Lab ', KEY, replace=None)
        self.assertEqual(json.loads(out), {'ref': REF, 'label': 'Lab', **binding})
        self.assertNotIn(KEY, out)
        self.service.assign_provider_key.assert_not_called()

    def test_credentials_save_names_the_available_providers(self):
        with patch.object(cli_models.inference, 'routes', return_value={'openrouter': {}, 'local-8080': {}}):
            error = self.fails('credentials', 'save', 'demo', '--provider', 'acme', '--label', 'x', '--key-stdin', stdin=KEY)
        self.assertIn('demo has no model provider "acme"', error)
        self.assertIn('local-8080, openrouter', error)

    def test_credentials_save_redacts_the_key_from_store_errors(self):
        with patch.object(cli_models.inference, 'routes', return_value={'openrouter': {'auth': {'type': 'bearer'}}}), \
                patch.object(cli_models.provider_credentials, 'scope', return_value=BINDING), \
                patch.object(cli_models.provider_credentials, 'save', side_effect=ValueError(f'store rejected {KEY}')):
            error = self.fails('credentials', 'save', 'demo', '--provider', 'openrouter', '--label', 'x',
                               '--key-stdin', stdin=KEY)
        self.assertIn('store rejected [redacted]', error)
        self.assertNotIn(KEY, error)

    def test_assign_saved_reference(self):
        self.service.assign_provider_key.return_value = {'ref': REF, 'label': 'Team'}
        out, _ = self.run_cli('credentials', 'assign', 'demo', '--provider', 'acme', '--ref', REF)
        self.service.assign_provider_key.assert_called_once()
        name, provider, choice, log = self.service.assign_provider_key.call_args.args
        self.assertEqual((name, provider, choice), ('demo', 'acme', {'ref': REF}))
        self.assertIn('acme in demo now uses saved key “Team”.', out)

    def test_assign_entered_key_with_remember_and_replace(self):
        self.service.assign_provider_key.return_value = {'ref': 'b' * 32, 'label': 'New'}
        out, _ = self.run_cli('credentials', 'assign', 'demo', '--provider', 'acme', '--key-stdin',
                              '--remember', 'New', '--replace', REF, '--json', stdin=KEY)
        choice = self.service.assign_provider_key.call_args.args[2]
        self.assertEqual(choice, {'key': KEY, 'label': 'New', 'replace': REF})
        self.assertNotIn(KEY, out)
        self.assertEqual(json.loads(out)['assignment']['label'], 'New')

    def test_assign_prompts_masked_and_reports_unsaved_key(self):
        self.service.assign_provider_key.return_value = None
        with patch('ai_sandbox_factory.cli_support.getpass.getpass', return_value=KEY):
            out, _ = self.run_cli('credentials', 'assign', 'demo', '--provider', 'acme', stdin=TerminalInput())
        self.assertEqual(self.service.assign_provider_key.call_args.args[2], {'key': KEY})
        self.assertIn('now uses the entered key', out)
        self.assertNotIn(KEY, out)

    def test_assign_rejects_inconsistent_key_options(self):
        self.usage_error('credentials', 'assign', 'demo', '--provider', 'acme', '--ref', REF, '--key-stdin')
        self.assertIn('apply to an entered key',
                      self.fails('credentials', 'assign', 'demo', '--provider', 'acme', '--ref', REF, '--remember', 'x'))
        self.assertIn('--replace needs --remember',
                      self.fails('credentials', 'assign', 'demo', '--provider', 'acme', '--key-stdin', '--replace', REF, stdin=KEY))
        self.assertIn('No acme API key was read from stdin',
                      self.fails('credentials', 'assign', 'demo', '--provider', 'acme', '--key-stdin', stdin='\n'))
        self.service.assign_provider_key.assert_not_called()

    def test_assign_provider_rejection_is_preserved(self):
        failure = ValueError('The provider rejected the API key.')
        self.service.assign_provider_key.side_effect = failure
        with self.assertRaises(ValueError) as caught:
            self.run_cli('credentials', 'assign', 'demo', '--provider', 'acme', '--key-stdin', stdin=KEY)
        self.assertIs(caught.exception, failure)

    def test_disconnect_and_forget(self):
        self.service.disconnect_provider_key.return_value = {'configured': []}
        out, _ = self.run_cli('credentials', 'disconnect', 'demo', '--provider', 'acme')
        self.service.disconnect_provider_key.assert_called_once_with('demo', 'acme')
        self.assertIn('Removed the acme key from demo.', out)
        with patch.object(cli_models.provider_credentials, 'entries', return_value={REF: {}}), \
                patch.object(cli_models.provider_credentials, 'forget') as forget:
            out, _ = self.run_cli('credentials', 'forget', REF, '--json')
        forget.assert_called_once_with(REF)
        self.assertEqual(json.loads(out), {'ref': REF, 'forgotten': True})
        with patch.object(cli_models.provider_credentials, 'entries', return_value={}), \
                patch.object(cli_models.provider_credentials, 'forget') as forget:
            error = self.fails('credentials', 'forget', 'b' * 32)
        self.assertIn('credentials list', error)
        forget.assert_not_called()

    def test_every_leaf_accepts_json_and_has_a_handler(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--state-dir')
        actions = parser.add_subparsers(dest='command')
        cli_models.register(actions)
        for group in ('models', 'credentials'):
            group_parser = actions.choices[group]
            leaves = next(a for a in group_parser._actions if isinstance(a, argparse._SubParsersAction)).choices
            for name, leaf in leaves.items():
                with self.subTest(command=f'{group} {name}'):
                    self.assertIn('--json', leaf._option_string_actions)
                    self.assertTrue(callable(leaf.get_default('cli_handler')))


if __name__ == '__main__':
    unittest.main()
