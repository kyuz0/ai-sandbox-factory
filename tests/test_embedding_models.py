import copy
import json
import unittest
from unittest.mock import patch

from ai_sandbox_factory import embedding_models, inference
from ai_sandbox_factory.sandbox_options import options


class EmbeddingModelTests(unittest.TestCase):
    def setUp(self):
        self.route = {
            'origin': 'https://company.example', 'basePath': '/api', 'auth': {'type': 'bearer'},
            'operations': [{'method': 'GET', 'path': '/api/v1/models', 'query': {'kind': ['text', 'vector']}}],
            'embeddingCatalogue': {'basePath': '/api/v1', 'path': '/api/v1/models', 'query': {'kind': 'vector'}},
        }
        self.row = {'name': 'company', 'manifest': {'description': 'Company', 'inference': self.route}}
        self.config = {'name': 'example', 'engine': 'podman', 'model_providers': [self.row]}
        self.selected = options({'knowledge_embedding': 'endpoint', 'knowledge_embedding_provider': 'company',
            'knowledge_embedding_url': 'https://company.example/api/v1', 'knowledge_embedding_model': 'sample'})
        self.reply = {'data': [{'id': 'sample'}]}

    def test_only_declared_and_enabled_sources(self):
        self.assertEqual(embedding_models.sources(self.config),
                         {'company': {'label': 'Company', 'url': 'https://company.example/api/v1'}})
        del self.route['embeddingCatalogue']
        self.config['_hosted_changes'] = {'openrouter': {}, 'together': {}}
        self.assertEqual(list(embedding_models.sources(self.config)), ['openrouter'])

    def test_draft_named_key_bound_to_exact_scope(self):
        with patch.object(embedding_models.provider_credentials, 'read', return_value='fixture') as read, \
             patch.object(embedding_models, '_direct', return_value=(200, json.dumps(self.reply))) as request:
            self.assertEqual(embedding_models.catalogue(self.config, 'company', choice={'ref': 'named'}), ['sample'])
        read.assert_called_once_with('named', {'provider': 'company', 'endpoint': 'https://company.example/api', 'auth': 'bearer'})
        request.assert_called_once_with(self.route, 'fixture')
        self.assertEqual(embedding_models._request_path(self.route), '/api/v1/models?kind=vector')

    def test_saved_companion_never_supplies_a_different_source(self):
        changed = copy.deepcopy(self.config)
        changed['model_providers'][0]['manifest']['inference']['origin'] = 'https://other.example'
        with patch.object(embedding_models, '_direct') as direct, \
             patch.object(embedding_models, '_managed', return_value=(200, json.dumps(self.reply))) as request:
            self.assertEqual(embedding_models.catalogue(self.config, 'company', saved_config=self.config), ['sample'])
            with self.assertRaisesRegex(ValueError, 'Choose a provider key'):
                embedding_models.catalogue(changed, 'company', saved_config=self.config)
            request.assert_called_once_with(self.config, 'company', self.route)
            direct.assert_not_called()

    def test_public_catalogue_does_not_send_account_key_or_list_chat_models(self):
        config = {'hosted_connections': {'openrouter': {}}}
        def model(name, output, inputs):
            return {'id': name, 'architecture': {'output_modalities': output, 'input_modalities': inputs}}
        data = {'data': [model('text-embed', ['embeddings'], ['text']), model('chat', ['text'], ['text']),
                         model('vision', ['embeddings'], ['image']), model('text-embed', ['embeddings'], ['text'])]}
        with patch.object(embedding_models, '_direct', return_value=(200, json.dumps(data))) as request:
            self.assertEqual(embedding_models.catalogue(config, 'openrouter', choice={'key': 'private'}), ['text-embed'])
        route, key = request.call_args.args
        self.assertEqual(embedding_models._request_path(route), '/api/v1/embeddings/models')
        self.assertIsNone(key)

    def test_fails_closed_on_provider_error_and_rejects_malformed_reply(self):
        for reply in ((401, b'private upstream detail'), (302, b''), (200, b'{"data":{}}')):
            with self.subTest(reply=reply[0]), patch.object(embedding_models, '_direct', return_value=reply):
                with self.assertRaises(ValueError) as error:
                    embedding_models.catalogue(self.config, 'company', choice={'key': 'fixture'})
                self.assertNotIn('private', str(error.exception))

    def test_catalogue_requires_a_declared_fixed_get_and_query(self):
        embedding_models.validate_catalogue(self.route)
        for changes in ({'basePath': '/other'}, {'basePath': '/api/v10'}, {'path': '//evil'},
                        {'path': '/api/v1/models/../other'}, {'query': {'kind': 'new'}}, {'query': []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                embedding_models.validate_catalogue({**self.route, 'embeddingCatalogue': {
                    **self.route['embeddingCatalogue'], **changes}})

    def test_explicit_provider_binding_prevents_fallback_and_resolves_duplicate_urls(self):
        self.config['model_providers'].append({**self.row, 'name': 'another'})
        self.config['options'] = self.selected
        self.assertEqual(inference.embedding_client_url(self.config), f'http://127.0.0.1:{inference.PORT}/r/company/api/v1')
        self.config['options']['knowledge_embedding_provider'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'selected embedding provider changed'):
            inference.embedding_client_url(self.config)
        self.config['options']['knowledge_embedding_provider'] = '@custom'
        self.assertEqual(inference.embedding_client_url(self.config), 'https://company.example/api/v1')
        self.config['options']['knowledge_embedding_provider'] = 'company'
        self.config['options']['knowledge_embedding_url'] = 'https://company.example/api'
        with self.assertRaisesRegex(ValueError, 'selected embedding provider changed'):
            inference.routes(self.config)
