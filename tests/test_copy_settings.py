"""Creation reuses immutable packages without sharing private state."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_sandbox_factory import folders, operation_locks, profile_edit, sandbox, model_providers
from ai_sandbox_factory.sandbox_tui import copy_draft, copy_for_agent, Service, options, shares_editable_profile


class CopySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'locks').mkdir()
        self.enterContext(patch.object(operation_locks,'private_runtime_dir',return_value=self.root/'locks'))
        self.source = self.root / 'source'
        self.source.mkdir()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        skill = self.source / 'skills' / 'example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: example\n---\nExample skill\n')
        self.config = dict(name='source', engine='podman', network='allowlist', memory='12g',
            subagents=True, local_model_ports=None, workspace='/source/project',
            env_file='/private/key.env', pi_config='/private/pi', ssh={'enabled': True},
            advisors={'providers': ['claude'], 'claude_model': 'opus', 'claude_effort': 'high'},
            profile=sandbox.snapshot_profile(sandbox.load_profile('base'), self.source),
            model_providers=model_providers.prepare({'local': {}}, self.source),
            extra_skills=[dict(name='example', repo='https://example.com/skills.git', folder='example',
                               commit='abc', path=str(skill), digest=sandbox.tree_digest(skill))],
            options={'browser': True, 'web_access': {'enabled': True, 'providers': {'exa': {'has_key': True}}}})
        (self.source / 'config.json').write_text(json.dumps(self.config))
        (self.source / 'allowlist.txt').write_text('example.com:443\n')
        (self.source / 'private-auth.json').write_text('PRIVATE_TEST_SENTINEL')

    def create(self, omit=(), skills=(), agent='pi', share=False, **overrides):
        args = SimpleNamespace(name='target', profile=self.config['profile']['path'], reuse_from='source',
            workspace=str(self.workspace), pi_config=None, env_file=None, memory='12g',
            engine='docker', allowlist=None, allow_domain=['example.com:443'],
            primary_agent=agent, advisor=['claude'] if agent == 'pi' else [],
            omit_skill=list(omit), skill=list(skills),reuse_editable_profile=share)
        for key,value in overrides.items():
            setattr(args,key,value)
        state = self.root / 'target'
        resolve = model_providers.registry.resolve
        def saved_package(kind, name):
            if agent != 'pi' and (kind, name) == ('profile', 'base'):
                return resolve(kind, name)
            raise AssertionError('Must use saved packages')
        with patch.object(sandbox.host_platform, 'check_engine', return_value=None), \
             patch.object(sandbox, 'choose_engine', return_value='docker'), \
             patch.object(sandbox.os, 'getuid', return_value=1000), \
             patch.object(sandbox.os, 'getgid', return_value=1000), \
             patch.object(model_providers.registry, 'resolve', side_effect=saved_package):
            sandbox.create(args, state)
        return state, json.loads((state / 'config.json').read_text())

    def editable(self):
        path=Path(self.enterContext(tempfile.TemporaryDirectory()))/'editable-profile'
        shutil.copytree(self.config['profile']['path'],path)
        self.config[profile_edit.EDIT]={'path':str(path),'origin_name':'base','origin_digest':self.config['profile']['digest']}
        (self.source/'config.json').write_text(json.dumps(self.config))
        return path

    def test_explicit_shared_profile_keeps_one_source_and_independent_snapshots_for_all_agents(self):
        self.workspace=Path(self.enterContext(tempfile.TemporaryDirectory()))
        path=self.editable()
        (path/'profile.json').write_text('{invalid pending edit\n')
        for agent in ('pi','codex','claude','agy'):
            with self.subTest(agent=agent):
                state,config=self.create(agent=agent,share=True)
                self.assertEqual(config[profile_edit.EDIT],self.config[profile_edit.EDIT])
                self.assertEqual(folders.roots(config).count((profile_edit.PATH,False)),1)
                self.assertTrue(Path(config['profile']['path']).is_relative_to(state))
                self.assertEqual(config['profile']['digest'],self.config['profile']['digest'])
                self.assertNotEqual(config['profile']['path'],self.config['profile']['path'])
                self.assertIsNone(config['env_file'])
                self.assertFalse((state/'private-auth.json').exists())
                self.assertFalse(operation_locks.in_use('source'))
                shutil.rmtree(state)
        self.assertEqual((path/'profile.json').read_text(),'{invalid pending edit\n')

    def test_sharing_requires_an_editable_source_and_its_applied_profile(self):
        for overrides,reason in (({'reuse_from':None},'requires --reuse-from'),({},'no editable profile')):
            with self.subTest(overrides=overrides),self.assertRaisesRegex(ValueError,reason):
                self.create(share=True,**overrides)
            self.assertFalse((self.root/'target').exists())
        self.editable()
        with self.assertRaisesRegex(ValueError,'source sandbox’s applied profile'):
            self.create(share=True,profile='base')
        self.assertFalse((self.root/'target').exists())

    def test_shared_folder_overlap_fails_before_target_state_is_created(self):
        path=self.editable()
        self.workspace=path
        with self.assertRaisesRegex(ValueError,'workspace'):
            self.create(share=True)
        self.assertFalse((self.root/'target').exists())

    def test_shared_creation_can_select_its_source_profile_without_a_private_snapshot_path(self):
        self.workspace=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.editable()
        state,config=self.create(share=True,profile=None)
        self.assertEqual(config['profile']['digest'],self.config['profile']['digest'])
        self.assertEqual(config[profile_edit.EDIT],self.config[profile_edit.EDIT])

    def test_copy_refuses_a_source_lifecycle_operation_before_allocating_target_state(self):
        self.editable()
        with operation_locks.sandbox('source','change profile'),ThreadPoolExecutor() as pool:
            future=pool.submit(self.create,share=True)
            with self.assertRaisesRegex(ValueError,'Cannot copy settings from source.*another Factory operation'):
                future.result(10)
        self.assertFalse((self.root/'target').exists())

    def test_factory_child_can_reuse_its_handed_off_source_lock(self):
        code='''
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from ai_sandbox_factory import operation_locks, sandbox
with patch.object(operation_locks,'private_runtime_dir',return_value=Path(sys.argv[1])), patch.object(sandbox,'_create') as copied:
    sandbox.create(SimpleNamespace(reuse_from='source'),Path(sys.argv[2]))
    copied.assert_called_once()
'''
        with operation_locks.sandbox('source','copy settings from'):
            result=subprocess.run([sys.executable,'-c',code,str(self.root/'locks'),str(self.root/'target')],
                text=True,capture_output=True,**operation_locks.factory_child('source'))
            independent=subprocess.run([sys.executable,'-c',code,str(self.root/'locks'),str(self.root/'target')],
                text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertNotEqual(independent.returncode,0)
        self.assertIn('another Factory operation',independent.stdout+independent.stderr)
        self.assertFalse((self.root/'target').exists())

    def test_independent_copy_of_editable_source_has_its_own_snapshot_and_no_editable_attachment(self):
        self.editable()
        state,config=self.create(share=False)
        self.assertNotIn(profile_edit.EDIT,config)
        self.assertTrue(Path(config['profile']['path']).is_relative_to(state))
        self.assertNotEqual(config['profile']['path'],self.config['profile']['path'])

    def test_copy_recovers_a_pending_source_before_selecting_its_applied_snapshot(self):
        self.workspace=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.editable()
        (self.source/'profile-preparation').mkdir()
        previous=copy.deepcopy(self.config)
        stale=copy.deepcopy(previous)
        stale['profile']['path']='/uncommitted/profile'
        (self.source/'config.json').write_text(json.dumps(stale))
        def recover(config,state):
            (state/'config.json').write_text(json.dumps(previous))
        with patch('ai_sandbox_factory.profile_replacement.recover_preparation',side_effect=recover) as recovery:
            state,config=self.create(share=True)
        recovery.assert_called_once()
        self.assertEqual(config['profile']['digest'],previous['profile']['digest'])

    def test_copy_rejects_a_stale_ui_profile_after_source_recovery_for_both_copy_choices(self):
        self.editable()
        previous=copy.deepcopy(self.config)
        stale=copy.deepcopy(previous)
        stale['profile']['path']='/uncommitted/profile'
        for shared in (False,True):
            with self.subTest(shared=shared):
                (self.source/'profile-preparation').mkdir(exist_ok=True)
                (self.source/'config.json').write_text(json.dumps(stale))
                def recover(config,state):sandbox.write_json(state/'config.json',previous)
                with patch('ai_sandbox_factory.profile_replacement.recover_preparation',side_effect=recover):
                    with self.assertRaisesRegex(ValueError,'source.*changed during recovery.*Start Create from existing again'):
                        self.create(share=shared,profile=stale['profile']['path'])
                self.assertFalse((self.root/'target').exists())

    def test_invalid_source_name_reports_the_value_and_allowed_format(self):
        with self.assertRaises(ValueError) as raised:
            self.create(reuse_from='bad source')
        self.assertIn(repr('bad source'),str(raised.exception))
        self.assertIn(sandbox.SANDBOX_NAME_ERROR,str(raised.exception))

    def test_copy_rechecks_the_source_choices_accepted_by_the_caller(self):
        revision = sandbox.config_revision(self.config)
        self.config['memory'] = '8g'
        (self.source / 'config.json').write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, 'source.*changed.*Start the copy again'):
            self.create(reuse_config_revision=revision)
        self.assertFalse((self.root / 'target').exists())
        _, copied = self.create(reuse_config_revision=sandbox.config_revision(self.config))
        self.assertEqual(copied['memory'], '12g')  # Explicit target choices still win.

    def test_sharing_choice_only_applies_to_the_selected_saved_profile(self):
        self.editable()
        draft=copy_draft({'name':'source','config':self.config},self.root)
        self.assertFalse(shares_editable_profile(draft))
        draft['share_editable_profile']=True
        self.assertTrue(shares_editable_profile(draft))
        for agent in ('pi','codex','claude','agy'):
            self.assertTrue(shares_editable_profile(copy_for_agent(draft,agent)))
        draft['profile']='base'
        self.assertFalse(shares_editable_profile(draft))

    def test_service_emits_sharing_flag_only_for_the_selected_source_profile(self):
        self.editable()
        draft=copy_draft({'name':'source','config':self.config},self.root)
        draft.update(name='target',workspace=str(self.workspace),open_editor=False)
        service=Service(self.root)
        for index,(shared,profile,expected) in enumerate(((False,draft['profile'],False),(True,draft['profile'],True),(True,'base',False))):
            selected={**draft,'name':'target-'+str(index),'profile':profile,'share_editable_profile':shared}
            commands=[]
            with patch.object(service,'run',side_effect=lambda args,log:commands.append(args)),patch.object(service,'setup'):
                service.create(selected,None,lambda _:None)
            self.assertEqual('--reuse-editable-profile' in commands[0],expected)
            self.assertIn('--reuse-from',commands[0])

    def test_key_copy_failure_keeps_operation_exit_code_and_cause_without_exported_keys(self):
        service=Service(self.root)
        failure=subprocess.CompletedProcess([],23,'SYNTHETIC_EXPORTED_KEY','Cannot read key store: permission denied')
        with patch('ai_sandbox_factory.sandbox_tui.subprocess.run',return_value=failure):
            with self.assertRaises(ValueError) as raised:
                service.search_keys('source')
        message=str(raised.exception)
        for text in ('source','exit 23','permission denied','Include Web Search API keys unchecked'):
            self.assertIn(text,message)
        self.assertNotIn('SYNTHETIC_EXPORTED_KEY',message)

    def test_independent_profile_provider_and_skill_survive_source_deletion(self):
        state, config = self.create()
        self.assertEqual(config['advisors']['claude_model'], 'opus')
        self.assertIsNone(config['env_file'])
        self.assertIsNone(config['pi_config'])
        self.assertNotIn('ssh', config)
        self.assertFalse((state / 'private-auth.json').exists())
        shutil.rmtree(self.source)
        for row in [config['profile'], *config['model_providers'], *config['extra_skills']]:
            self.assertTrue(Path(row['path']).is_relative_to(state))
            self.assertEqual(sandbox.tree_digest(Path(row['path'])), row['digest'])

    def test_omitted_skill_and_modified_skill(self):
        state, config = self.create(omit=['example'])
        self.assertEqual(config['extra_skills'], [])
        shutil.rmtree(state)
        Path(self.config['extra_skills'][0]['path'], 'SKILL.md').write_text('changed')
        with self.assertRaises(ValueError) as raised:
            self.create()
        for value in ('source','example',self.config['extra_skills'][0]['path'],'omit that skill'):
            self.assertIn(value,str(raised.exception))
        self.assertFalse(state.exists())
        self.assertTrue(self.source.exists())

    def test_modified_profile_copy_names_the_source_path_revision_and_recovery(self):
        Path(self.config['profile']['path'],'profile.md').write_text('unexpected edit')
        with self.assertRaises(ValueError) as raised:
            self.create()
        for value in ('source',self.config['profile']['path'],self.config['profile']['digest'],'Restore the saved snapshot'):
            self.assertIn(value,str(raised.exception))
        self.assertFalse((self.root/'target').exists())

    def test_draft_excludes_private_paths_and_is_independent(self):
        self.config['profile_service_volumes'] = {'database': {'data': 'source-private-database'}}
        self.config['profile_service_retained_volumes'] = ['source-previous-database']
        self.config['profile_service_recovery'] = [{'id':'source-only','service':'database'}]
        original = copy.deepcopy(self.config)
        draft = copy_draft({'name': 'source', 'config': self.config}, self.root)
        for field in ('name', 'env_file', 'pi_config'):
            self.assertEqual(draft[field], '')
        self.assertEqual(draft['workspace'], self.config['workspace'])
        self.assertEqual(draft['workspace_parent'], self.config['workspace'])
        self.assertNotIn('ssh', draft)
        self.assertNotIn('profile_service_volumes', draft)
        self.assertNotIn('profile_service_retained_volumes', draft)
        self.assertNotIn('profile_service_recovery', draft)
        state, config = self.create()
        self.assertNotIn('profile_service_volumes', config)
        self.assertNotIn('profile_service_retained_volumes', config)
        self.assertNotIn('profile_service_recovery', config)
        self.assertFalse(draft['options']['web_access']['providers']['exa']['has_key'])
        draft['advisors']['providers'].clear()
        self.assertEqual(self.config, original)

    def test_creation_can_copy_packages_between_all_coding_agents(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for source_agent in ('pi', 'codex', 'claude', 'agy'):
            self.config['primary_agent'] = source_agent
            (self.source / 'config.json').write_text(json.dumps(self.config))
            before = (self.source / 'config.json').read_bytes()
            for target_agent in ('pi', 'codex', 'claude', 'agy'):
                with self.subTest(source=source_agent, target=target_agent):
                    state, config = self.create(agent=target_agent)
                    self.assertEqual(config['primary_agent'], target_agent)
                    self.assertEqual(config['profile']['name'], 'base')
                    self.assertEqual(config['extra_skills'][0]['name'], 'example')
                    self.assertTrue(Path(config['extra_skills'][0]['path']).is_relative_to(state))
                    self.assertEqual(config['network'], 'allowlist')
                    self.assertEqual(config['memory'], '12g')
                    if target_agent != 'pi':
                        self.assertNotIn('model_providers', config)
                        self.assertNotIn('advisors', config)
                    self.assertEqual((self.source / 'config.json').read_bytes(), before)
                    shutil.rmtree(state)

    def test_agent_changes_copy_shared_choices_and_reset_agent_specific_choices(self):
        draft = copy_draft({'name': 'source', 'config': self.config}, self.root)
        draft['options'] = options({
            'browser': False, 'git_identity': {'name': 'Example', 'email': 'author@example.test'},
            'website_groups': ['dev-python'], 'approval': 'manual', 'native_approval': 'manual',
            'codex_web_search': True, 'claude_web_search': True, 'agy_web_search': True,
            'goals': False, 'scheduling': False, 'compaction_recall': 'full',
        })
        for source in ('pi', 'codex', 'claude', 'agy'):
            draft['primary_agent'] = source
            before = copy.deepcopy(draft)
            for target in ('pi', 'codex', 'claude', 'agy'):
                with self.subTest(source=source, target=target):
                    copied = copy_for_agent(draft, target)
                    self.assertEqual(draft, before)
                    if source == target:
                        self.assertEqual(copied, draft)
                        continue
                    self.assertEqual(copied['primary_agent'], target)
                    self.assertEqual(copied['memory'], draft['memory'])
                    self.assertEqual(copied['extra_skills'], draft['extra_skills'])
                    self.assertEqual(copied['system_prompt'], draft['system_prompt'])
                    for field in ('browser', 'git_identity', 'website_groups'):
                        self.assertEqual(copied['options'][field], draft['options'][field])
                    self.assertEqual(copied['options']['approval'], 'automatic')
                    self.assertTrue(copied['options']['goals'])
                    self.assertEqual(copied['options']['compaction_recall'], 'lite')
                    for field in ('codex_web_search', 'claude_web_search', 'agy_web_search'):
                        self.assertFalse(copied['options'][field])
                    self.assertEqual(copied['model_providers'], {})
                    self.assertEqual(copied['advisors'], {})
                    self.assertFalse(copied['local_model_ports'])
                    self.assertEqual(copied['options']['native_approval'],
                                     'manual' if source != 'pi' and target != 'pi' else 'automatic')

    def test_service_passes_only_selected_resources_and_keys_separately(self):
        draft = copy_draft({'name': 'source', 'config': self.config}, self.root)
        self.assertEqual(draft['image_source'], 'local')
        draft.update(name='target', workspace=str(self.workspace), remove_skills=['example'], open_editor=False)
        service = Service(self.root)
        commands = []
        def run(args, log):
            commands.append(args)
            if args[0] == 'create':
                selected = json.loads(Path(args[args.index('--options-file') + 1]).read_text())
                self.assertNotIn('synthetic-key', json.dumps(selected))
        with patch.object(service, 'run', side_effect=run), patch.object(service, 'setup') as setup:
            service.create(draft, {'exa': 'synthetic-key'}, lambda _: None)
        self.assertIn('--reuse-from', commands[0])
        self.assertIn('--omit-skill', commands[0])
        self.assertEqual(commands[0][commands[0].index('--image-source') + 1], 'local')
        self.assertNotIn('--env-file', commands[0])
        self.assertNotIn('--pi-config', commands[0])
        self.assertNotIn('synthetic-key', str(commands))
        self.assertEqual(setup.call_args.args[2], {'exa': 'synthetic-key'})

    def test_copy_inherits_explicit_image_setup(self):
        config = {**self.config, 'image_source': 'prebuilt'}
        draft = copy_draft({'name': 'source', 'config': config}, self.root)
        self.assertEqual(draft['image_source'], 'prebuilt')
        self.assertEqual(copy_for_agent(draft, 'codex')['image_source'], 'prebuilt')

    def test_create_copies_multiple_library_skills_with_saved_selection(self):
        from ai_sandbox_factory import skill_library
        with patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'library')}):
            source=self.root/'new-skills'
            for name in ('first','second'):
                folder=source/name;folder.mkdir(parents=True)
                (folder/'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic {name}\n---\n')
            rows=skill_library.discover(str(source),self.root/'fetched')
            skill_library.install(rows)
            state, config=self.create(skills=[row['id'] for row in rows])
            self.assertEqual([row['name'] for row in config['extra_skills']], ['example','first','second'])
            for row in rows:skill_library.remove(row['id'])
            self.assertTrue(all(Path(row['path']).is_relative_to(state) for row in config['extra_skills']))
            self.assertTrue(all((Path(row['path'])/'SKILL.md').exists() for row in config['extra_skills']))

    def test_failed_multi_skill_update_preserves_config_and_running_container(self):
        from ai_sandbox_factory import skill_library
        self.config.update(workspace=str(self.workspace), env_file=None, extra_folders=[], ssh={'hosts':[]}, advisors={'providers':[]})
        (self.source/'config.json').write_text(json.dumps(self.config))
        original=(self.source/'config.json').read_bytes()
        with patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'library')}):
            source=self.root/'new-skills'
            for name in ('first','second'):
                folder=source/name;folder.mkdir(parents=True)
                (folder/'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic {name}\n---\n')
            rows=skill_library.discover(str(source),self.root/'fetched');skill_library.install(rows)
            with patch.object(sandbox,'build',side_effect=ValueError('build failed')), patch.object(sandbox,'down') as down, patch.object(sandbox,'up') as up:
                with self.assertRaisesRegex(ValueError,'build failed'):
                    sandbox.apply_settings(self.config,self.source,{'library_skills':[r['id'] for r in rows],'remove_skills':['example']})
                down.assert_not_called();up.assert_not_called()
            self.assertEqual((self.source/'config.json').read_bytes(),original)
