import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pi_sandbox_factory import connectors
from pi_sandbox_factory.connectors import context


class ConnectorContextTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        self.hosts = [{'alias': f'build-{i}', 'hostname': f'build-{i}.example', 'user': 'builder', 'port': 2202,
                       'host_keys': ['sentinel-host-key']} for i in range(7)]
        self.aws = {'role': 'arn:aws:iam::123456789012:role/Reader', 'region': 'eu-west-1',
                    'grants': [{'Action': 's3:GetObject', 'Resource': ['arn:aws:s3:::example/public/*']}],
                    'profile': '/private/host-profile', 'AccessKeyId': 'sentinel-key', 'SessionToken': 'sentinel-token'}

    def install(self, instructions='Use the build connector to inspect release jobs.'):
        directory = self.state / 'installed'
        path = directory / 'build/connector.json'
        path.parent.mkdir(parents=True)
        manifest = dict(id='build', version=1, title='Builds', description='Read results.',
                        documentation='Host setup only.', agent_instructions=instructions,
                        operations={'latest': 'https://builds.example/latest?q=sentinel-query',
                                    'tests': 'https://test-reports.example/tests'})
        path.write_text(json.dumps(manifest))
        with patch.object(connectors, 'directory', return_value=directory):
            connectors.snapshot(['build'], self.state)
        return {'connector_ids': ['build'], 'connector_operations': {'build': ['latest']}}

    def test_all_boundaries_and_only_saved_permissions(self):
        config = self.install()
        config.update(aws_access=self.aws, ssh={'hosts': self.hosts}, _ssh_key_file='/private/key-file',
                      options={'git_identity': {'email': 'private@example'}})
        summary, details = context.render(config, self.state)
        for text in (summary, details):
            for expected in ('build-0.example', 'builder', '2202', self.aws['role'], 'eu-west-1',
                             's3:GetObject', 'arn:aws:s3:::example/public/*', 'builds.example',
                             'http://localhost/latest', 'Use the build connector', 'bash_escalate'):
                self.assertIn(expected, text)
            for excluded in ('test-reports.example', '/tests', 'sentinel', '/private/', 'private@example', 'Host setup only'):
                self.assertNotIn(excluded, text)
        self.assertNotIn('build-6.example', summary)
        self.assertIn('build-6.example', details)
        self.assertIn('4 more', summary)
        self.assertIn('/opt/pi-security/config/connectors.md', summary)

    def test_native_files_and_revocation(self):
        config = self.install()
        config['primary_agent'] = 'codex'
        context.write(config, self.state)
        root = self.state / 'primary-runtime'
        prompt = root / 'connectors-prompt.md'
        guide = root / 'connectors.md'
        self.assertIn('/run/primary-runtime/connectors.md', prompt.read_text())
        self.assertNotIn('bash_escalate', prompt.read_text())
        self.assertIn('/run/primary-runtime/connectors/build/api.sock', guide.read_text())
        context.write({'primary_agent': 'codex', 'options': {'git_identity': {'name': 'Foo'}}}, self.state)
        self.assertFalse(prompt.exists())
        self.assertFalse(guide.exists())

    def test_abbreviating_text_does_not_change_access(self):
        long_text = 'Follow the published build runbook.\n' * 45
        config = self.install(long_text)
        config['options'] = {'approval': 'off'}
        summary, details = context.render(config, self.state)
        self.assertIn('enable approvals', summary)
        self.assertIn('full connector reference', summary)
        self.assertNotIn(long_text, summary)
        self.assertIn(long_text, details)
        self.assertEqual(len(connectors.permitted_routes(connectors.saved(config, self.state)[0], config)), 1)

    def test_instructions_are_optional_plain_text_and_immutable(self):
        config = self.install()
        path = self.state / 'installed/build/connector.json'
        raw = json.loads(path.read_text())
        raw['agent_instructions'] = 'Changed instruction.'
        path.write_text(json.dumps(raw))
        self.assertNotIn('Changed instruction', context.render(config, self.state)[0])
        for value in (False, 'Hello\x1b[31m'):
            with self.assertRaises(ValueError):
                connectors._parse(dict(raw, agent_instructions=value), 'build')
        del raw['agent_instructions']
        self.assertEqual(connectors._parse(raw, 'build').agent_instructions, '')

    def test_only_granted_operations_contribute_their_instructions(self):
        config = self.install()
        path = self.state / 'installed/build/connector.json'
        raw = json.loads(path.read_text())
        raw['operations'] = {name: {'url': url, 'instructions': f'Instructions for {name} only.'}
                             for name, url in raw['operations'].items()}
        path.write_text(json.dumps(raw))
        with patch.object(connectors, 'directory', return_value=path.parent.parent):
            connectors.snapshot(['build'], self.state, refresh=True)
        for selected, excluded in [('latest', 'tests'), ('tests', 'latest')]:
            config['connector_operations']['build'] = [selected]
            for text in context.render(config, self.state):
                self.assertIn(f'Instructions for {selected} only.', text)
                self.assertNotIn(f'Instructions for {excluded} only.', text)
        original = connectors.saved(config, self.state)[0]
        raw['operations']['latest']['instructions'] = 'Changed.'
        path.write_text(json.dumps(raw))
        self.assertEqual(connectors.saved(config, self.state)[0], original)
        self.assertNotEqual(connectors._parse(raw, 'build').reviewed_instructions, original.reviewed_instructions)

    def test_identity_only_and_unselected_plugin_emit_no_context(self):
        self.install()
        config = {'options': {'git_identity': {'name': 'Foo', 'email': 'private@example'}}, 'ssh': {'hosts': []}}
        self.assertEqual(context.render(config, self.state), ('', ''))
