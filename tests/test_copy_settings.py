"""Creation reuses immutable packages without sharing private state."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_sandbox_factory import sandbox, model_providers
from ai_sandbox_factory.sandbox_tui import copy_draft, copy_for_agent, Service, options


class CopySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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

    def create(self, omit=(), skills=(), agent='pi'):
        args = SimpleNamespace(name='target', profile=self.config['profile']['path'], reuse_from='source',
            workspace=str(self.workspace), pi_config=None, env_file=None, memory='12g',
            engine='docker', allowlist=None, allow_domain=['example.com:443'],
            primary_agent=agent, advisor=['claude'] if agent == 'pi' else [],
            omit_skill=list(omit), skill=list(skills))
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
        with self.assertRaisesRegex(ValueError, 'Source skill snapshot was modified'):
            self.create()
        self.assertFalse(state.exists())
        self.assertTrue(self.source.exists())

    def test_draft_excludes_private_paths_and_is_independent(self):
        original = copy.deepcopy(self.config)
        draft = copy_draft({'name': 'source', 'config': self.config}, self.root)
        for field in ('name', 'env_file', 'pi_config'):
            self.assertEqual(draft[field], '')
        self.assertEqual(draft['workspace'], self.config['workspace'])
        self.assertEqual(draft['workspace_parent'], self.config['workspace'])
        self.assertNotIn('ssh', draft)
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
        self.assertNotIn('--env-file', commands[0])
        self.assertNotIn('--pi-config', commands[0])
        self.assertNotIn('synthetic-key', str(commands))
        self.assertEqual(setup.call_args.args[2], {'exa': 'synthetic-key'})

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
