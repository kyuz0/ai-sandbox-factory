import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_sandbox_factory import prompt_library, runtime_context, sandbox
from ai_sandbox_factory.sandbox_options import write_json


class SandboxContextTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.config = {'name': 'settings-test', 'engine': 'podman', 'network': 'allowlist'}
        write_json(self.state / 'config.json', self.config)
        (self.state / 'allowlist.txt').write_text('initial.example:443\n')
        (self.state / 'squid.conf').write_text(sandbox.network_policy(self.config, [('initial.example', 443)]))
        self.path = self.state / 'runtime-config/sandbox-state.json'

    def read(self):
        return json.loads(self.path.read_text())

    def test_publication_uses_applied_network_and_ignores_comments_and_rule_order(self):
        config = {**self.config, 'primary_agent': 'codex'}
        write_json(self.state / 'config.json', config)
        self.path = self.state / 'primary-runtime/sandbox-state.json'
        runtime_context.publish_saved(self.state)
        old_revision = self.read()['revision']
        with patch.object(sandbox, 'inspect', return_value=None):
            saved = sandbox.update_network(config, self.state, 'one.example:80\ntwo.example:443\n')
            doc = self.read()
            self.assertNotEqual(doc['revision'], old_revision)
            self.assertEqual(doc['values']['Additional allowed destinations'], 'one.example:80, two.example:443')
            previous = self.path.stat()
            sandbox.update_network(saved, self.state, 'two.example:443 # comment\none.example:80\n')
            self.assertEqual(self.read(), doc)
            self.assertEqual(self.path.stat().st_ino, previous.st_ino)
        write_json(self.state / 'config.json', {**saved, 'network': 'unrestricted'})
        runtime_context.publish_saved(self.state)
        self.assertEqual(self.read()['network'], 'unrestricted')
        self.assertEqual(self.read()['values']['Network'], 'Unrestricted')
        self.assertNotIn('Additional allowed destinations', self.read()['values'])

    def test_gateway_rollback_keeps_previous_published_revision(self):
        runtime_context.publish_saved(self.state)
        original = self.path.read_bytes()
        with (patch.object(sandbox, 'inspect', return_value={'State': {'Running': True}}),
              patch.object(sandbox, 'restart_existing'),
              patch.object(sandbox, 'ensure_gateway', side_effect=[ValueError('fixture gateway offline'), None])):
            with self.assertRaisesRegex(ValueError, 'fixture gateway offline'):
                sandbox.update_network(self.config, self.state, 'changed.example:80')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.state / 'allowlist.txt').read_text(), 'initial.example:443\n')

    def test_cancelled_settings_do_not_publish_a_previous_unapplied_candidate(self):
        config = {**self.config, 'primary_agent': 'codex'}
        write_json(self.state / 'config.json', config)
        runtime_context.publish_saved(self.state)
        public = self.state / 'primary-runtime/sandbox-state.json'
        before = public.read_bytes()
        candidate = {**config, 'network': 'unrestricted'}
        write_json(self.state / 'config.json', candidate)
        gate = self.state / 'cancelled'
        gate.mkdir()
        sandbox.apply_settings(candidate, self.state, {'codex_web_search': True}, apply_gate=gate)
        self.assertEqual(public.read_bytes(), before)

    def test_nested_readiness_and_profile_preparation_do_not_publish_candidate(self):
        runtime_context.publish_saved(self.state)
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'editor failed'):
            with runtime_context.applied(self.state):
                write_json(self.state / 'config.json', {**self.config, 'network': 'unrestricted'})
                with runtime_context.applied(self.state):
                    runtime_context.publish_saved(self.state)
                self.assertEqual(self.path.read_bytes(), original)
                raise ValueError('editor failed')
        write_json(self.state / 'profile-preparation/record.json', {})
        runtime_context.publish_saved(self.state)
        self.assertEqual(self.path.read_bytes(), original)

    def test_snapshot_uses_only_workspace_paths_and_public_grants(self):
        config = {**self.config, 'workspace': '/host/private/project', 'working_directory': 'src',
                  'extra_folders': [{'source': '/host/private/data', 'name': 'reports', 'read_only': True}],
                  'env_file': '/host/private/credentials', 'pi_config': '/host/private/pi',
                  'profile': {'name': 'backend', 'digest': 'abc123', 'path': '/host/private/snapshot',
                              'services': {'database': {'environment': {'PASSWORD': 'fixture-password'}}}},
                  'ssh': {'hosts': [{'alias': 'server', 'hostname': 'server.example', 'port': 22,
                                     'user': 'developer', 'host_keys': ['fixture-private-material']}]},
                  'connector_ids': ['tasks'], 'connector_operations': {'tasks': ['status']},
                  'aws_access': {'region': 'eu-west-1', 'role': 'arn:aws:iam::000000000000:role/Test',
                                 'profile': '/host/private/aws',
                                 'grants': [{'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::fixture/*'}]},
                  'model_providers': [{'name': 'local', 'settings': {'token': 'fixture-secret'}}]}
        write_json(self.state / 'runtime-config/sandbox-system-prompt.json',
                   {'name': 'Review', 'text': 'fixture-private-addendum'})
        write_json(self.state / 'config.json', config)
        runtime_context.publish_saved(self.state)
        document = self.read()
        self.assertEqual(document['values']['Starting folder for new sessions'], '/workspace/src')
        self.assertIn('/mnt/reports (read-only)', document['values']['Folders'])
        self.assertIn('tasks (status)', document['values']['HTTPS connector grants'])
        self.assertIn('s3:getobject on arn:aws:s3:::fixture/*', document['values']['AWS permissions'])
        self.assertIn('database', document['values']['Profile services'])
        self.assertEqual(document['values']['Network'], 'Restricted')
        for private in ('fixture-password', 'fixture-private', 'fixture-secret', '/host/private'):
            self.assertNotIn(private, self.path.read_text())

    def test_large_destination_lists_preserve_every_host_and_detect_changes_after_preview(self):
        rules = [f'host-{number:03d}.example:443' for number in range(100)]
        allowlist = self.state / 'allowlist.txt'
        allowlist.write_text('\n'.join(rules))
        first = runtime_context.snapshot(self.config, self.state)
        label = 'Additional allowed destinations'
        self.assertIn('/opt/pi-security/config/sandbox-state.json', first['values'][label])
        self.assertNotIn(rules[-1], first['values'][label])
        self.assertEqual(first['details'][label].split(', '), rules)
        allowlist.write_text('\n'.join(rules[:-1] + ['replaced.example:443']))
        second = runtime_context.snapshot(self.config, self.state)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertNotEqual(first['values'][label], second['values'][label])

    def test_native_client_preferences_state_when_they_take_effect(self):
        config = {**self.config, 'primary_agent': 'claude'}
        doc = runtime_context.snapshot(config, self.state)
        self.assertNotIn('securityGuidance', doc)
        self.assertNotIn('Inner command sandbox', doc['values'])
        self.assertEqual(doc['values']['Web Search for newly opened clients'], 'Off')
        updated = runtime_context.snapshot({**config, 'options': {'claude_web_search': True}}, self.state)
        changed = {label for label in doc['values'] if doc['values'][label] != updated['values'][label]}
        self.assertEqual(changed, {'Web Search for newly opened clients'})
        self.assertNotEqual(doc['revision'], updated['revision'])

    def test_live_pi_security_instructions_come_from_committed_choices(self):
        config = {**self.config, 'profile': sandbox.load_profile('base'), 'workspace': str(self.state),
                  'common': {'path': str(sandbox.ROOT / 'containers/dev')}}
        write_json(self.state / 'config.json', config)
        sandbox.prepare_options(config, self.state)
        runtime_context.publish_saved(self.state)
        security = self.state / 'runtime-config/security.md'
        self.assertEqual(self.read()['securityGuidance'], security.read_text())
        previous = self.read()
        candidate = {**config, 'network': 'unrestricted'}
        with self.assertRaisesRegex(ValueError, 'fixture rejection'):
            with runtime_context.applied(self.state):
                sandbox.prepare_options(candidate, self.state)
                self.assertIn('Network is Unrestricted', security.read_text())
                self.assertEqual(self.read(), previous)
                raise ValueError('fixture rejection')
        self.assertEqual(self.read(), previous)
        write_json(self.state / 'config.json', candidate)
        runtime_context.publish_saved(self.state)
        self.assertIn('Network is Unrestricted', self.read()['securityGuidance'])
        self.assertNotEqual(self.read()['revision'], previous['revision'])
        with patch.object(runtime_context, 'pi_security', return_value='Revised Factory instructions'):
            self.assertNotEqual(runtime_context.snapshot(candidate, self.state)['revision'], self.read()['revision'])

    def test_provider_mappings_and_prepared_snapshots_publish_only_provider_names(self):
        configured = {'local': {'endpoint': '/host/private/local', 'token': 'fixture-secret'}}
        prepared = [{'name': 'local', 'path': '/host/private/package', 'settings': configured['local']}]
        original = runtime_context.snapshot({**self.config, 'model_providers': prepared}, self.state)
        draft = runtime_context.snapshot({**self.config, 'model_providers': configured}, self.state)
        self.assertEqual(original, draft)
        self.assertEqual(draft['values']['Model providers'], 'local')
        self.assertNotIn('/host/private', json.dumps(draft))
        self.assertNotIn('fixture-secret', json.dumps(draft))

    def test_native_addon_refreshes_reference_before_publishing_changed_revision(self):
        from ai_sandbox_factory import primary_agents, registry
        config = {**self.config, 'primary_agent': 'claude', 'workspace': str(self.state)}
        write_json(self.state / 'config.json', config)
        primary_agents.write_context(config, self.state)
        runtime_context.publish_saved(self.state)
        public_file = self.state / 'primary-runtime/sandbox-state.json'
        context = self.state / 'primary-runtime/context.md'
        with patch.object(registry, 'root', return_value=self.state / 'library'):
            prompt_library.assign(self.state, {'name': 'Review', 'text': 'Read the fixture instructions.'})
            self.assertIn('Read the fixture instructions.', context.read_text())
            self.assertNotIn('Read the fixture instructions.', public_file.read_text())
            published = public_file.read_bytes()
            for module, function in [(primary_agents, 'write_context'), (runtime_context, 'publish_saved')]:
                with (self.subTest(function=function), patch.object(module, function, side_effect=OSError('fixture disk full')),
                      self.assertRaisesRegex(OSError, 'fixture disk full')):
                    prompt_library.assign(self.state, {'name': 'Changed', 'text': 'Pending fixture'})
                self.assertEqual(prompt_library.selection(self.state)['name'], 'Review')
                self.assertIn('Read the fixture instructions.', context.read_text())
                self.assertEqual(public_file.read_bytes(), published)
