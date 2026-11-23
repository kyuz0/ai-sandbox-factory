import copy
import json
from pathlib import Path
import tempfile
import unittest

from ai_sandbox_factory import inference, registry, sandbox


class EmbeddingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.route = {
            'origin': 'https://models.example', 'basePath': '/api',
            'auth': {'type': 'bearer'},
            'operations': [{'method': 'GET', 'path': '/api/v1/models'}],
        }
        self.config = {'model_providers': [{'name': 'company', 'manifest': {'inference': self.route}}]}

    def select(self, base):
        self.config['options'] = {'knowledge_embedding': 'endpoint', 'knowledge_embedding_url': base, 'knowledge_embedding_model': 'fixture-embedding'}

    def test_endpoint_only_adds_selected_post_and_preserves_manifest(self):
        original = copy.deepcopy(self.route)
        for prefix in ('/api', '/api/v1'):
            self.select('https://models.example' + prefix)
            self.assertEqual(inference.embedding_client_url(self.config),
                             f'http://127.0.0.1:{inference.PORT}/r/company' + prefix)
            self.assertEqual(inference.routes(self.config)['company']['operations'],
                             [*original['operations'], {'method': 'POST', 'path': prefix + '/embeddings'}])
            self.assertEqual(self.route, original)
        self.config['options'] = {'knowledge_embedding': 'off'}
        self.assertEqual(inference.routes(self.config)['company'], original)
        self.assertIsNone(inference.embedding_client_url(self.config))

    def test_unmatched_endpoint_keeps_existing_egress_and_cannot_use_companion(self):
        for base in ('https://models.example:444/api', 'https://other.example/api',
                     'https://models.example/api/other', 'https://models.example/api/v10'):
            self.select(base)
            self.assertEqual(inference.embedding_client_url(self.config), base)
            self.assertEqual(inference.routes(self.config)['company'], self.route)

    def test_exact_selected_local_port(self):
        self.config['local_model_ports'] = '8080-8081'
        self.select('http://local-model.internal:8080/v1')
        self.assertEqual(inference.embedding_client_url(self.config),
                         f'http://127.0.0.1:{inference.PORT}/r/local-8080/v1')
        routes = inference.routes(self.config)
        self.assertIn({'method': 'POST', 'path': '/v1/embeddings'}, routes['local-8080']['operations'])
        self.assertEqual(routes['local-8081'], inference.local_route(8081))

    def test_ambiguous_connections_cannot_select_a_credential(self):
        self.select('https://models.example/api')
        self.config['model_providers'].append({'name': 'other', 'manifest': {'inference': self.route}})
        with self.assertRaisesRegex(ValueError, 'Several model connections'):
            inference.routes(self.config)

    def test_batch_size_matches_both_provider_and_model(self):
        self.route['embeddingBatchSizes'] = {'single-text': 1}
        self.select('https://models.example/api/v1')
        self.config['options']['knowledge_embedding_model'] = 'single-text'
        self.assertEqual(inference.embedding_batch_size(self.config), 1)
        self.config['options']['knowledge_embedding_model'] = 'batch-model'
        self.assertIsNone(inference.embedding_batch_size(self.config))
        self.select('https://other.example/api/v1')
        self.config['options']['knowledge_embedding_model'] = 'single-text'
        self.assertIsNone(inference.embedding_batch_size(self.config))

    def test_reviewer_receives_selected_service_and_broker_bases_without_credentials(self):
        self.select('https://models.example/api/v1')
        self.assertEqual(inference.trusted_model_endpoints(self.config), sorted([
            'https://models.example/api', 'https://models.example/api/v1',
            f'http://127.0.0.1:{inference.PORT}/r/company/api',
            f'http://127.0.0.1:{inference.PORT}/r/company/api/v1',
        ]))
        self.select('https://embeddings.example/v1')
        self.assertIn('https://embeddings.example/v1', inference.trusted_model_endpoints(self.config))
        self.config['options']['knowledge_embedding'] = 'off'
        self.assertNotIn('https://embeddings.example/v1', inference.trusted_model_endpoints(self.config))

    def test_runtime_trust_comes_from_configuration_and_preserves_old_snapshot_format(self):
        self.select('https://models.example/api/v1')
        for supported in (True, False):
            with self.subTest(supported=supported), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                common = sandbox.common_profile()
                # Isolate the versioned reviewer configuration from the checkout.
                import shutil
                shutil.copytree(common['path'], root / 'common')
                path = root / 'common/security/config/auto-review.json'
                config = json.loads(path.read_text())
                if not supported:
                    config.pop('trustedModelEndpoints', None)
                path.write_text(json.dumps(config))
                runtime = {**self.config, 'common': {**common, 'path': str(root / 'common')}}
                sandbox.prepare_options(runtime, root)
                actual = json.loads((root / 'runtime-config/auto-review.json').read_text())
                if supported:
                    self.assertEqual(actual['trustedModelEndpoints'], inference.trusted_model_endpoints(runtime))
                else:
                    self.assertNotIn('trustedModelEndpoints', actual)

    def test_guarded_indexing_tools_are_allowed_only_with_embeddings(self):
        import copy
        policy = {'permission': {}}
        selected = sandbox.options({'knowledge_embedding': 'endpoint',
            'knowledge_embedding_url': 'https://embeddings.example/v1',
            'knowledge_embedding_model': 'fixture'})
        actual = sandbox.permissions(copy.deepcopy(policy), selected)['permission']
        for name in ('knowledge_add', 'knowledge_update', 'knowledge_search'):
            self.assertEqual(actual[name], 'allow')
        off = sandbox.permissions(copy.deepcopy(policy), sandbox.options())['permission']
        for name in ('knowledge_add', 'knowledge_update', 'knowledge_search'):
            self.assertNotIn(name, off)

    def test_model_ids_keep_valid_spaces_and_errors_explain_the_failing_field(self):
        from ai_sandbox_factory.sandbox_options import embedding_model, embedding_url
        self.assertEqual(embedding_model('local embedding model'), 'local embedding model')
        for invalid in ('', ' ', 'model\nname', 'model\x7fname'):
            with self.assertRaises(ValueError):
                embedding_model(invalid)
        for url, message in [('models.example/v1', 'Start'),
                             ('https://user:secret@models.example/v1', 'username'),
                             ('https://models.example/v1?token=secret', 'query')]:
            with self.assertRaisesRegex(ValueError, message):
                embedding_url(url)

    def test_manifest_validates_batch_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'adapter.mjs').touch()
            manifest = {'name': 'sample', 'entry': 'adapter.mjs', 'inference': self.route}
            for value in ([], {'': 1}, {'a': True}, {'a': -1}, {'a': 1.5}, {'a': '64'}):
                self.route['embeddingBatchSizes'] = value
                (folder / 'provider.json').write_text(json.dumps(manifest))
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'positive integers'):
                    registry.manifest('provider', folder)
            self.route['embeddingBatchSizes'] = {'a': 1}
            (folder / 'provider.json').write_text(json.dumps(manifest))
            self.assertEqual(registry.manifest('provider', folder), manifest)
