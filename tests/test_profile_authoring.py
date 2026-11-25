"""The shipped authoring example and its read-only agent documentation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import primary_agents, profile_runtime, profile_services, profile_spec, registry, sandbox

GUIDE = sandbox.ROOT / 'containers/dev/profile-authoring'
EXAMPLE = GUIDE / 'example'


class ProfileAuthoringTests(unittest.TestCase):
    def test_renaming_the_base_recipe_retains_declared_agent_support(self):
        metadata=json.loads((sandbox.ROOT/'profiles/base/profile.json').read_text())
        metadata['name']='team-developer'
        for agent in profile_spec.AGENTS:
            self.assertTrue(profile_spec.compatible(metadata,agent))

    def test_complete_example_declares_independent_hosts_and_shared_build_context(self):
        profile = sandbox.load_profile(str(EXAMPLE))
        self.assertEqual(set(profile['services']), {'database', 'reporting'})
        self.assertEqual(registry.manifest('profile', EXAMPLE)['version'], 1)
        for agent in profile_spec.AGENTS:
            self.assertTrue(profile_spec.compatible(profile, agent))
        database, reporting = (profile['services'][name] for name in ('database', 'reporting'))
        self.assertEqual(database['build'], reporting['build'])
        self.assertNotEqual(database['connections']['database'], reporting['connections']['database'])
        self.assertEqual(database['environment']['POSTGRES_PASSWORD'], reporting['environment']['POSTGRES_PASSWORD'])
        config = {'name':'example', 'engine':'podman', 'profile':profile}
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            profile_services.prepare(config, state)
            credentials = json.loads((state / 'profile-services/client/connections.json').read_text())
            self.assertNotEqual(credentials['database']['password'], credentials['reporting']['password'])
            self.assertNotEqual(profile_services.data_volume(config, 'database', 'data'),
                                profile_services.data_volume(config, 'reporting', 'data'))

    def test_authoring_guide_and_complete_example_reach_every_agent_record(self):
        profile = sandbox.load_profile(str(EXAMPLE))
        common = sandbox.common_profile()
        with tempfile.TemporaryDirectory() as temporary:
            for agent in profile_spec.AGENTS:
                with self.subTest(agent=agent):
                    state = Path(temporary) / agent
                    config = {'name':'example', 'engine':'podman', 'primary_agent':agent,
                              'profile':profile, 'common':common, 'workspace':temporary, 'options':{'browser':False}}
                    with patch.object(sandbox, 'image_names', return_value=('fixture','proxy')), \
                            patch.object(sandbox, 'inspect', return_value=None), \
                            patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess(
                                [], 0, '[{"Id":"fixture-image"}]', '')):
                        with profile_runtime.recording(config, state):
                            pass
                        profile_runtime.select(config, state)
                    applied = Path(profile_runtime.applied(state)['path'])
                    guide = applied / 'authoring/README.md'
                    self.assertEqual(guide.read_bytes(), (GUIDE / 'README.md').read_bytes())
                    copied = sandbox.load_profile(str(applied / 'authoring/example'))
                    self.assertEqual(set(copied['services']), {'database', 'reporting'})
                    self.assertIn(profile_runtime.AUTHORING, profile_runtime.guidance(state))
                    self.assertIn('Your Main agent sandbox was built from profile', profile_runtime.guidance(state))
                    self.assertIn(profile_runtime.AUTHORING, primary_agents.guidance(config, state))
                    self.assertIn('Actions… → Profile and services… → Make profile editable…', profile_runtime.guidance(state, config))
                    self.assertEqual((applied / 'authoring/validate.py').read_bytes(), Path(profile_spec.__file__).read_bytes())
                    self.assertNotIn('Author a profile with multiple hosts', primary_agents.guidance(config, state))

    def test_old_record_does_not_advertise_a_guide_it_does_not_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / 'profile-runtime').mkdir()
            (state / 'profile-runtime/applied.json').write_text(json.dumps({
                'name':'example', 'profile_digest':'fixture', 'path':str(state/'old-record')}))
            self.assertNotIn(profile_runtime.AUTHORING, profile_runtime.guidance(state))

    def test_example_can_be_versioned_independently_and_updated_without_mutating_old_copy(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            import shutil
            root = Path(temporary)
            profile = root / 'team-dev'
            shutil.copytree(EXAMPLE, profile)
            os.environ['SANDBOX_REGISTRY_DIR'] = str(root / 'library')
            def git(*args):
                return subprocess.run(['git','-c','commit.gpgsign=false','-C',str(profile),*args],check=True,
                                      capture_output=True, text=True)
            git('init')
            git('-c','user.name=Example Author','-c','user.email=author@example.test','add','.')
            git('-c','user.name=Example Author','-c','user.email=author@example.test','commit','-m','Initial profile')
            old = registry.add('profile','team-dev',str(profile))
            git('tag','v1')
            pinned = registry.add('profile','team-dev-v1',str(profile),ref='v1')
            manifest = json.loads((profile/'profile.json').read_text())
            manifest['services']['analysis'] = manifest['services']['reporting'].copy()
            (profile/'profile.json').write_text(json.dumps(manifest))
            git('add','.')
            git('-c','user.name=Example Author','-c','user.email=author@example.test','commit','-m','Add analysis host')
            git('tag','v2')
            new = registry.add('profile','team-dev-v2',str(profile),ref='v2')
            self.assertEqual(set(registry.manifest('profile', old['path'])['services']), {'database','reporting'})
            self.assertEqual(set(registry.manifest('profile', new['path'])['services']), {'database','reporting','analysis'})
            self.assertEqual(registry.manifest('profile',new['path'])['version'], 1)
            retained = registry.update('profile','team-dev-v1')
            self.assertEqual(retained['commit'], pinned['commit'])
            self.assertEqual(set(registry.manifest('profile',retained['path'])['services']), {'database','reporting'})
            registry.update('profile','team-dev')
            self.assertEqual(set(registry.manifest('profile', old['path'])['services']), {'database','reporting'})


if __name__ == '__main__':
    unittest.main()
