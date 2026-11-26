"""Exercise local model listing and selection through the CLI and its shipped Node helper."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from ai_sandbox_factory import hosted_models, model_providers, sandbox
from test_cli_management import CliCase


class LocalModelCommands(CliCase):
    def setUp(self):
        super().setUp()
        self.saved = self.sandbox()
        config = json.loads((self.saved / 'config.json').read_text())
        config.update(model_providers=[{'name': 'local', 'manifest': {}}], local_model_ports='80,8000')
        (self.saved / 'config.json').write_text(json.dumps(config))
        self.agent_dir = self.root / 'agent'
        self.agent_dir.mkdir()
        self.provider = 'owned-provider-from-manifest'
        self.model = {'id': 'example:model', 'name': 'Example model', 'reasoning': True, 'input': ['text', 'image'],
                      'maxTokens': 2048, 'contextWindow': 32768,
                      'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}
        definition = {'baseUrl': 'http://127.0.0.1:19431/r/local-80/v1', 'apiKey': 'fixture-placeholder',
                      'api': 'openai-completions', 'models': [self.model]}
        providers = {key: definition for key in (self.provider, 'edited', 'inactive-port', 'unowned')}
        (self.agent_dir / 'models.json').write_text(json.dumps({'providers': providers}))
        self.manifest = self.agent_dir / 'models.json.local-lab-state.json'
        self.manifest.write_text(json.dumps({'version': 1, 'entries': {
            self.provider: {'endpoint': 'http://local-model.internal', 'generated': definition},
            'edited': {'endpoint': 'http://local-model.internal:8000', 'generated': {**definition, 'models': []}},
            'inactive-port': {'endpoint': 'http://local-model.internal:9000', 'generated': definition}}}))
        self.enterContext(patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}))
        configured_runtime = os.environ.get('FACTORY_TEST_PI_RUNTIME')
        self.runtime = Path(configured_runtime) if configured_runtime else self.fixture_runtime()
        self.node_run = subprocess.run
        self.exec = self.enterContext(patch.object(hosted_models.subprocess, 'run', side_effect=self.run_in_fixture))

    def fixture_runtime(self, folder='pi'):
        runtime = self.root / folder
        core = runtime / 'dist/core'
        core.mkdir(parents=True)
        (runtime / 'dist/utils').mkdir()
        (runtime / 'package.json').write_text('{"type":"module"}')
        (core / 'auth-storage.js').write_text('export const AuthStorage={create:()=>({})}, readStoredCredential=()=>null;')
        (core / 'http-dispatcher.js').write_text('export function configureHttpDispatcher() {}')
        (runtime / 'dist/utils/json.js').write_text('export const stripJsonComments = value => value;')
        (core / 'model-runtime.js').write_text("""
            import fs from 'node:fs';
            export const ModelRuntime = { create({modelsPath}) {
                const {providers} = JSON.parse(fs.readFileSync(modelsPath));
                return {
                    getModels(provider) { return (providers[provider]?.models || []).map(model => (
                        {...model, provider, api: providers[provider].api})); },
                    getModel(provider, id) { return this.getModels(provider).find(model => model.id === id); }
                };
            }};
        """)
        (core / 'settings-manager.js').write_text("""
            import fs from 'node:fs';
            import path from 'node:path';
            export const SettingsManager = { create(cwd, dir) { return {
                setDefaultModelAndProvider(defaultProvider, defaultModel) {
                    fs.writeFileSync(path.join(dir, 'settings.json'), JSON.stringify({defaultProvider, defaultModel}));
                },
                flush() {}, drainErrors() { return []; }
            }; }};
        """)
        return runtime

    def run_in_fixture(self, command, **kwargs):
        self.assertEqual(command[:4], ['podman', 'exec', '-i', '--user'])
        lock = (sandbox.ROOT / 'providers/provider-lock.mjs').as_uri()
        script = command[-1].replace("'/opt/sandbox/provider-lock.mjs'", repr(lock))
        return self.node_run(['node', '--input-type=module', '-e', script], **kwargs,
                             env={**os.environ, 'PI_CODING_AGENT_DIR': str(self.agent_dir),
                                  'FACTORY_PI_RUNTIME': str(self.runtime)})

    def test_list_reports_runtime_metadata_and_selects_the_owned_id(self):
        result, _ = self.json_ok('models', 'list', 'demo', '--local')
        expected = {k: v for k, v in self.model.items() if k != 'cost'}
        self.assertEqual(result['models'], [{**expected, 'api': 'openai-completions', 'provider': self.provider}])
        answer, _ = self.json_ok('models', 'select', 'demo', '--local', '--provider', self.provider,
                                 '--model', self.model['id'])
        self.assertEqual(answer, {'provider': self.provider, 'model': self.model['id']})
        prefs = json.loads((self.agent_dir / 'settings.json').read_text())
        self.assertEqual((prefs['defaultProvider'], prefs['defaultModel']), (self.provider, self.model['id']))

    def test_unknown_provider_or_model_preserves_selection(self):
        for provider in ('unowned', 'edited', 'inactive-port', 'local-80'):
            with self.subTest(provider=provider):
                detail = self.fails('models', 'select', 'demo', '--local', '--provider', provider, '--model', self.model['id'])
                self.assertIn('models list --local', detail)
                self.assertIn(provider, detail)
        self.assertIn('Choose a model', self.fails('models', 'select', 'demo', '--local', '--provider',
                                                   self.provider, '--model', 'missing-model'))
        self.assertFalse((self.agent_dir / 'settings.json').exists())

    def test_missing_and_unreadable_ownership_manifest(self):
        self.manifest.unlink()
        self.assertEqual(self.json_ok('models', 'list', 'demo', '--local')[0], {'models': []})
        self.manifest.write_text('{ broken')
        detail = self.fails('models', 'list', 'demo', '--local')
        self.assertIn('invalid JSON', detail)
        self.assertIn(self.manifest.name, detail)

    def test_explicit_refresh_only_discovers_local_models(self):
        with patch.object(model_providers, 'configure') as discover:
            self.json_ok('models', 'list', 'demo', '--local', '--refresh')
            self.assertEqual(discover.call_args.args[1:], (self.saved, 'refresh'))
            self.assertEqual(discover.call_args.kwargs, {'provider': 'local'})

    def test_native_sandboxes_and_conflicting_provider_choices(self):
        self.sandbox('native', agent='codex')
        self.assertIn('Pi sandboxes', self.fails('models', 'list', 'native', '--local'))
        self.assertIn('Pi sandboxes', self.fails('models', 'select', 'native', '--local', '--provider', 'p', '--model', 'm'))
        self.fails('models', 'list', 'demo', '--local', '--provider', 'p', code=2)
        self.fails('models', 'select', 'demo', '--local', '--plugin', '--provider', 'p', '--model', 'm', code=2)
        self.exec.assert_not_called()

    def test_runtime_diagnostics_keep_exit_cause_and_redact_credential_variants(self):
        self.runtime = self.fixture_runtime('failing-pi')
        (self.runtime / 'dist/core/model-runtime.js').write_text("""
            export const ModelRuntime = { create() {
                const inner = Object.assign(new Error('read fixture token%2Fraw and trailing diagnostic'), {code: 'ECONNRESET'});
                throw Object.assign(new Error('catalogue fixture token/raw\\nretry', {cause: inner}), {code: 'EIO'});
            }};
        """)
        config = json.loads((self.saved / 'config.json').read_text())
        with self.assertRaises(ValueError) as caught:
            hosted_models.request(config, 'local-catalogue', localPorts=[80], credential={'key': 'token/raw'})
        message = str(caught.exception)
        self.assertIn('EIO: catalogue fixture [redacted] retry', message)
        self.assertIn('ECONNRESET: read fixture [redacted] and trailing diagnostic', message)
        self.assertNotIn('token/', message)
        self.assertNotIn('token%2F', message)

    def test_invalid_json_does_not_echo_a_partial_credential(self):
        script = (sandbox.ROOT / 'providers/hosted-models.mjs').read_text()
        result = self.run_in_fixture(['podman', 'exec', '-i', '--user', script],
                                     input='{"key": "synthetic-leak", invalid}', text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        detail = json.loads(result.stdout)['error']
        self.assertIn('Read model request', detail)
        self.assertIn('Invalid JSON', detail)
        self.assertNotIn('synthetic-leak', result.stdout + result.stderr)
