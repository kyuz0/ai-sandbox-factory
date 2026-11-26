import json
from unittest.mock import patch

from ai_sandbox_factory import connector_settings, connectors, sandbox
from test_cli_management import CliCase


class ConnectorPreview(CliCase):
    def setUp(self):
        super().setUp()
        self.saved = self.sandbox()
        self.config_file = self.saved / 'config.json'
        self.config = json.loads(self.config_file.read_text())
        example = sandbox.ROOT.parent / 'examples/connectors/build-status/connector.json'
        self.manifest = json.loads(example.read_text())
        self.identifier = self.manifest['id']
        self.plugin_file = connectors.directory() / self.identifier / 'connector.json'
        self.plugin_file.parent.mkdir(parents=True)
        self.plugin_file.write_text(json.dumps(self.manifest))
        self.enterContext(patch.object(sandbox, 'inspect', side_effect=AssertionError('preview accessed the engine')))

    def preview(self, changes):
        result, _ = self.json_ok('update', 'demo', '--preview', '--settings-file', '-', stdin=json.dumps(changes))
        return result['runtime_effect']

    def test_adding_a_connector_validates_installed_operations_without_snapshotting(self):
        before = self.tree(self.saved)
        changes = {'connector_ids': [self.identifier], 'connector_operations': {self.identifier: ['latest']}}
        self.assertEqual(self.preview(changes), 'connectors')
        for operations, reason in ((['made-up'], 'declared'), ([], 'at least one')):
            changes['connector_operations'][self.identifier] = operations
            error = self.fails('update', 'demo', '--preview', '--settings-file', '-', stdin=json.dumps(changes))
            self.assertIn(reason, error)
        self.assertEqual(before, self.tree(self.saved))

    def test_existing_grants_use_the_snapshot_and_removal_works_if_it_is_damaged(self):
        retained = {'connector_ids': [self.identifier], 'connector_operations': {self.identifier: ['latest']}}
        self.config_file.write_text(json.dumps({**self.config, **retained}))
        self.assertEqual(self.preview({'connector_ids': [self.identifier]}), 'none')
        snapshot = self.saved / 'connector-manifests' / (self.identifier + '.json')
        snapshot.parent.mkdir()
        snapshot.write_text(json.dumps(self.manifest))
        self.plugin_file.unlink()
        self.assertEqual(self.preview({**retained, 'connector_operations': {self.identifier: ['test-results']}}), 'connectors')
        snapshot.write_text('invalid')
        before = self.tree(self.saved)
        self.assertEqual(self.preview({'connector_ids': []}), 'connectors')
        self.assertEqual(before, self.tree(self.saved))

    def test_preview_and_apply_refuse_the_same_incomplete_or_mixed_grants(self):
        for changes, reason in (
            ({'connector_operations': {}}, 'include connector_ids'),
            ({'connector_ids': [], 'memory': '2g'}, 'memory'),
            ({'connector_ids': [], 'connector_operations': {'other': ['latest']}}, 'other'),
            ({'connector_ids': [self.identifier], 'connector_operations': {}}, self.identifier),
        ):
            with self.subTest(changes=changes):
                error = self.fails('update', 'demo', '--preview', '--settings-file', '-', stdin=json.dumps(changes))
                self.assertIn(reason, error)
                with self.assertRaisesRegex(ValueError, reason):
                    connector_settings.apply(self.config, self.saved, changes, None)

    def test_unchanged_ssh_is_a_no_op_but_a_changed_option_still_replaces(self):
        self.assertEqual(self.preview({'ssh': {'hosts': []}}), 'none')
        self.assertEqual(self.preview({'ssh': {'hosts': []}, 'options': {'browser': False}}), 'replace')

    def test_unchanged_aws_grants_do_not_inspect_the_runtime(self):
        self.assertEqual(self.preview({'aws_access': {}}), 'none')
        access = {'profile': 'fixture', 'role': 'arn:aws:iam::123456789012:role/Fixture',
                  'region': 'eu-west-1', 'grants': [{'Action': 'ec2:DescribeRegions', 'Resource': '*'}]}
        self.config_file.write_text(json.dumps({**self.config, 'aws_access': access}))
        self.assertEqual(self.preview({'aws_access': access}), 'none')

    def test_equivalent_folder_choices_use_the_normalized_mounts(self):
        data = self.root / 'data'
        data.mkdir()
        original = {'source': str(data), 'name': 'data', 'read_only': True}
        self.config_file.write_text(json.dumps({**self.config, 'extra_folders': [original]}))
        self.assertEqual(self.preview({'extra_folders': [{'source': str(data)}]}), 'next-launch')
        self.assertEqual(self.preview({'extra_folders': [{**original, 'read_only': False}]}), 'replace')

    def test_network_changes_check_existing_allowlist_ports_and_provider_requirements(self):
        config = {**self.config, 'model_providers': [{'name': 'fixture', 'settings': {},
                                                      'manifest': {'network': ['example.com:443']}}]}
        self.config_file.write_text(json.dumps(config))
        for changes, reason in (({'allowlist': ''}, 'example.com:443'),
                                ({'local_model_ports': '8000'}, 'local-model.internal:8000'),
                                ({'network': 'unrestricted'}, 'Unrestricted networking')):
            with self.subTest(changes=changes):
                error = self.fails('update', 'demo', '--preview', '--settings-file', '-', stdin=json.dumps(changes))
                self.assertIn(reason, error)
