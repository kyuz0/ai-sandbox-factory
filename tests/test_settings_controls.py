"""Each browser field uses existing validation and preserves unrelated settings."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import (curlie_catalog, operations, sandbox, settings_controls as controls,
                                settings_plan, system_packages)
from ai_sandbox_factory.sandbox_options import DEFAULTS, options, write_json


class Controls(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='factory-controls-')
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.state=self.root/'demo'; self.state.mkdir()
        self.config={'name':'demo','engine':'podman','network':'allowlist','model_providers':[],
                     'options':{},'workspace':str(self.root),'subagents':False,'memory':'8g'}
        self.save()
        (self.state/'allowlist.txt').write_text('example.test:443\n')
        self.service=operations.Service(self.root)
        self.locks=self.root/'locks';self.locks.mkdir()
        lock=patch('ai_sandbox_factory.operation_locks.private_runtime_dir',return_value=self.locks)
        lock.start();self.addCleanup(lock.stop)

    def save(self):
        write_json(self.state/'config.json',self.config)

    def test_every_setting_is_exposed_or_has_an_explicit_dedicated_flow(self):
        fields=[field for agent in ('pi','codex','claude','agy')
                for _,group in controls.groups({**self.config,'primary_agent':agent}).values() for field in group]
        option_keys={field.key for field in fields if field.option}
        config_keys={field.key for field in fields if not field.option} - {'packages'}
        self.assertEqual(set(DEFAULTS),option_keys|controls.DEDICATED_OPTIONS.keys())
        self.assertEqual(sandbox.EDITABLE_SETTINGS,config_keys|{'options'}|controls.DEDICATED_SETTINGS.keys())
        self.assertFalse(option_keys & controls.DEDICATED_OPTIONS.keys())

    def test_agent_visibility_and_native_shortcuts(self):
        for agent in ('pi','codex','claude','agy'):
            config={**self.config,'primary_agent':agent}
            groups=controls.groups(config)
            if agent=='pi':
                self.assertIn('features',groups)
                self.assertNotIn('web-search',groups)
            else:
                self.assertNotIn('features',groups)
                self.assertEqual([field.key for field in groups['web-search'][1]],[agent+'_web_search'])
                prepared=controls.prepare(config,self.state,'approvals',{'native_approval':'manual'})
                self.assertEqual(prepared['changes'],{'native_approval':'manual'})
                prepared=controls.prepare(config,self.state,'web-search',{agent+'_web_search':True})
                self.assertEqual(prepared['changes'],{agent+'_web_search':True})
                self.config=config; self.save()
                self.assertEqual(self.service.preview_settings('demo',{'native_approval':'manual'})['runtime_effect'],'restart')
                self.assertEqual(self.service.preview_settings('demo',{agent+'_web_search':True})['runtime_effect'],'next-launch')
        self.assertNotIn('destinations',controls.groups({**self.config,'network':'unrestricted'}))

    def test_descriptors_never_expose_dedicated_credentials_or_paths(self):
        config={**self.config,'env_file':'/host/private/credentials.env',
                'ssh':{'hosts':[],'identity_file':'/host/private/key'},
                'primary_auth':{'path':'/host/private/sign-in','token':'synthetic-secret'},
                'options':{'web_access':{'providers':{'brave':{'has_key':True}}}}}
        value=json.dumps(controls.describe(config,self.state))
        for excluded in ('synthetic-secret','/host/private','identity_file','env_file'):
            self.assertNotIn(excluded,value)

    def test_partial_options_merge_preserves_other_configuration(self):
        self.config['options']=options({'git_identity':{'name':'Example','email':'example@example.test'},'goals':False})
        original=copy.deepcopy(self.config)
        changes=controls.prepare(self.config,self.state,'approvals',{'approval':'manual'})['changes']
        expected=copy.deepcopy(self.config['options']);expected['approval']='manual'
        self.assertEqual(changes,{'options':expected})
        self.assertEqual(self.config,original)

    def test_closed_schema_and_types(self):
        cases=[('approvals',{'network':'unrestricted'}),('features',{'goals':'false'}),
               ('resources',{'memory':[]}),('packages',{'packages':'jq; curl evil'}),
               ('network',{'network':'garbage'}),('destinations',{'website_groups':'dev'}),
               ('unknown',{}),('resources',{'env_file':'/host/secret'})]
        for section,values in cases:
            with self.subTest(section=section,values=values),self.assertRaises(ValueError):
                controls.prepare(self.config,self.state,section,values)

    def test_field_changes_use_shared_preview_effects(self):
        for section,values,effect in (
            ('network',{'network':'unrestricted'},'replace'),
            ('destinations',{'allowlist':'new.example:443'},'live-network'),
            ('approvals',{'approval':'manual'},'replace'),
            ('features',{'goals':False},'replace'),
            ('resources',{'image_source':'prebuilt'},'next-build'),
            ('resources',{'memory':'4g'},'replace'),
            ('directory',{'working_directory':'.'},'none'),
        ):
            with self.subTest(section=section):
                prepared=controls.prepare(self.config,self.state,section,values)
                result=self.service.preview_settings('demo',prepared['changes'])
                self.assertEqual(result['runtime_effect'],effect)

    def test_rule_only_apply_never_builds_or_replaces_workspace(self):
        revision=settings_plan.revision(self.state)
        with patch.object(sandbox,'update_network') as update,patch.object(sandbox,'build') as build:
            sandbox.apply_settings(self.config,self.state,{'allowlist':'new.example:443'},revision=revision)
            update.assert_called_once()
            build.assert_not_called()
        self.config['memory']='4g';self.save()
        with patch.object(sandbox,'update_network') as update,self.assertRaisesRegex(ValueError,'changed after review'):
            sandbox.apply_settings(self.config,self.state,{'allowlist':'new.example:443'},revision=revision)
        update.assert_not_called()

    def test_starting_directory_checks_path_and_describes_next_launch(self):
        (self.root/'project').mkdir()
        prepared=controls.prepare(self.config,self.state,'directory',{'working_directory':'project'})
        result=self.service.preview_settings('demo',prepared['changes'])
        self.assertEqual(result['runtime_effect'],'next-launch')
        self.assertEqual(result['completion'],'Starting directory saved for the next client or terminal launch.')
        with self.assertRaises(ValueError):
            self.service.preview_settings('demo',{'working_directory':'missing-project'})

    def test_local_models_refusal_and_required_provider_destinations(self):
        with self.assertRaisesRegex(ValueError,'Turn off Local models'):
            settings_plan.normalize({**self.config,'local_model_ports':'8000'},self.state,{'network':'unrestricted'})
        with patch.object(sandbox.model_providers,'required_rules',return_value=['provider.example:443']):
            result=settings_plan.normalize({**self.config,'network':'unrestricted'},self.state,{'network':'allowlist'})
        self.assertIn('provider.example:443',result['allowlist'])

    def test_curlie_download_note_and_cached_revision(self):
        from ai_sandbox_factory import network_groups
        key=next(key for key,row in network_groups.catalog().items() if row.get('curlie'))
        changes={'options':options({'website_groups':[key]})}
        with patch.object(curlie_catalog,'metadata',return_value=None):
            result=self.service.preview_settings('demo',changes)
        self.assertTrue(any('180 MB' in note for note in result['notes']))
        with patch.object(curlie_catalog,'metadata',return_value={'revision':'a'*32}):
            result=self.service.preview_settings('demo',changes)
        self.assertEqual(result['changes']['curlie_list'],'a'*32)
        self.assertFalse(any('180 MB' in note for note in result['notes']))

    def test_package_preview_reuses_validator_and_warns_before_install(self):
        request=controls.prepare(self.config,self.state,'packages',{'packages':'jq, ripgrep jq'})
        self.assertEqual(request['packages'],['jq','ripgrep'])
        result=system_packages.preview(request['packages'])
        self.assertEqual(result['runtime_effect'],'install')
        self.assertTrue(any('root inside this sandbox' in note for note in result['notes']))
        with self.assertRaises(ValueError):system_packages.preview(['--allow-unauthenticated'])
