from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_sandbox_factory import hosted_models, inference, provider_credentials as keys


class Vault:
    def __init__(self):
        self.items = {}

    def set_password(self, service, ref, value):
        self.items[ref] = value

    def get_password(self, service, ref):
        return self.items.get(ref)

    def delete_password(self, service, ref):
        del self.items[ref]


class SavedProviderKeys(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.index = self.root / 'provider-credentials.json'
        self.vault = Vault()
        for target, value in (('index_path', self.index), ('os_store', self.vault)):
            mock = patch.object(keys, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.config = {'hosted_connections': {'openrouter': {}}}
        self.binding = hosted_models.credential_scope({'provider': 'openrouter'})

    def test_scope_matches_only_selected_provider_and_full_endpoint(self):
        label = 'Work key for infrastructure, documentation, and development sandboxes'
        ref = keys.save(self.binding, label, 'synthetic-api-key')
        self.assertEqual(keys.entries(self.binding)[ref]['label'], label)
        self.assertNotIn('synthetic-api-key', self.index.read_text())
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)
        for field, value in (('provider', 'another'), ('endpoint', 'https://openrouter.ai/different'),
                             ('auth', 'api-key')):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                keys.read(ref, {**self.binding, field: value})

    def test_replace_and_forget_only_change_the_workstation_key(self):
        ref = keys.save(self.binding, 'Work', 'old-key')
        self.assertEqual(keys.save(self.binding, 'Work', 'old-key'), ref)
        with self.assertRaisesRegex(ValueError, 'already uses'):
            keys.save(self.binding, 'work', 'another-key')
        with patch.object(self.vault, 'set_password', side_effect=OSError):
            with self.assertRaisesRegex(ValueError, 'Could not save'):
                keys.save(self.binding, 'Work', 'new-key', replace=ref)
            self.assertEqual(keys.read(ref, self.binding), 'old-key')
        new = keys.save(self.binding, 'Work', 'new-key', replace=ref)
        self.assertEqual(keys.save(self.binding, 'Work', 'new-key', replace=ref), new)
        self.assertNotIn(ref, keys.entries())
        self.assertNotIn(ref, self.vault.items)
        self.assertEqual(keys.read(new, self.binding), 'new-key')
        keys.forget(new)
        self.assertEqual(keys.entries(), {})
        self.assertEqual(self.vault.items, {})

    def test_three_assignments_switch_one_and_preserve_active_key_on_failure(self):
        runtime = {}
        calls = []
        def credential(_config, state, request):
            name = state.name
            if request.get('status'):
                return {'configured': ['openrouter'] if name in runtime else [], 'assignments': {}}
            key = request['credentials']['key']
            if request.get('check'):
                calls.append(('check', name))
                self.assertEqual(request['basePath'], '/api/v1')
                return {'status': 'rejected' if key == 'rejected-key' else 'accepted'}
            calls.append(('assign', name))
            runtime[name] = (key, request['assignment'])
            return {'ok': True}

        work = keys.save(self.binding, 'Work', 'work-key')
        test = keys.save(self.binding, 'Test', 'test-key')
        with patch.object(inference, 'credentials', side_effect=credential), \
             patch.object(inference, 'up') as up:
            keys.assign(self.config, self.root/'one', 'openrouter', ref=work)
            keys.assign(self.config, self.root/'two', 'openrouter', ref=work)
            keys.assign(self.config, self.root/'three', 'openrouter', ref=test)
            keys.assign(self.config, self.root/'two', 'openrouter', ref=test)
            with self.assertRaisesRegex(ValueError, 'rejected'):
                keys.assign(self.config, self.root/'two', 'openrouter', key='rejected-key', label='Bad')
            up.assert_not_called()
        self.assertEqual([result[0] for result in runtime.values()], ['work-key', 'test-key', 'test-key'])
        self.assertEqual(runtime['two'][1]['label'], 'Test')
        self.assertEqual(calls[-1], ('check', 'two'))
        keys.forget(test)
        self.assertEqual(runtime['two'][0], 'test-key')
        self.assertIsNone(next((row for row in keys.entries().values() if row['label'] == 'Bad'), None))

    def test_existing_hosted_connection_switches_without_client_refresh(self):
        choice = {'ref': 'fixture-key-reference'}
        assignment = {'ref': 'fixture-key-reference', 'label': 'Test'}
        with patch.object(hosted_models, 'request') as request, \
             patch.object(inference, 'credentials', return_value={'configured': ['openrouter'], 'assignments': {}}), \
             patch.object(inference, 'up') as up, patch.object(keys, 'assign', return_value=assignment) as assign, \
             patch.object(hosted_models.activity, 'record'):
            result = hosted_models.save(self.config, self.root, {'provider': 'openrouter', 'credential': choice})
        assign.assert_called_once_with(self.config, self.root, 'openrouter', **choice)
        self.assertEqual(result['assignment'], assignment)
        request.assert_called_once_with(self.config, 'status')
        up.assert_not_called()

    def test_local_key_matches_its_port_and_approved_scan_range(self):
        binding = keys.scope('local', inference.local_route(8000))
        ref = keys.save(binding, 'Work', 'synthetic-local-key')
        config = {'local_model_ports': '8000-8001'}
        state = self.root / 'sandbox'
        with patch.object(inference, 'credentials', side_effect=[
            {'assignments': {}}, {'status': 'accepted'}, {'ok': True}, {'assignments': {}},
        ]) as credentials:
            assignment = keys.assign(config, state, inference.local_route_id(8000), ref=ref)
            self.assertEqual(assignment, {'ref': ref, 'label': 'Work'})
            self.assertEqual(credentials.call_args_list[1].args[-1]['origin'], 'http://local-model.internal:8000')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                keys.assign(config, state, inference.local_route_id(8001), ref=ref)
            with self.assertRaisesRegex(ValueError, 'different credential flow'):
                keys.assign(config, state, inference.local_route_id(8002), ref=ref)
            self.assertEqual(credentials.call_count, 4)
