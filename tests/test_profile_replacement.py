"""Workspace cutover preserves applications and restores all Factory helpers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ai_sandbox_factory import profile_replacement as replacement, profile_services, sandbox


class ReplacementTests(unittest.TestCase):
    def test_stopped_runtime_helpers_stay_stopped_and_db_is_never_removed(self):
        self.exercise(False, False)

    def test_startup_failure_restores_exact_workspace_and_helper_ids(self):
        self.exercise(True, True)

    def exercise(self, running, failure):
        config = {'name':'fixture','engine':'podman','primary_agent':'codex',
                  'profile':sandbox.load_profile('developer-postgres')}
        workspace = sandbox.names(config)[0]
        helper = workspace + '-agent'
        db = profile_services.container(config,'database')
        containers = {name: {'Id':'old-' + str(index),'State':{'Running':running}}
                      for index,name in enumerate((workspace,helper,db))}
        original = copy.deepcopy(containers)
        calls=[]
        def run(argv, **kwargs):
            calls.append(argv)
            operation, value = argv[1:3]
            if operation == 'rm' and value == '-f': value = argv[3]
            key = next((name for name,info in containers.items() if name == value or info['Id'] == value),None)
            if operation == 'stop': containers[key]['State']['Running']=False
            elif operation == 'start': containers[key]['State']['Running']=True
            elif operation == 'rename': containers[argv[3]]=containers.pop(key)
            elif operation == 'rm': containers.pop(key)
        starts=0
        def up(candidate,*args,**kwargs):
            nonlocal starts
            starts+=1
            if starts==1:
                for name in (workspace,helper): containers[name]={'Id':'new-' + name,'State':{'Running':True}}
                containers[db]['State']['Running']=True
                if failure: raise ValueError('startup failed with diagnostic')
            else:
                for name in (workspace,helper,db): containers[name]['State']['Running']=True
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            (state/'allowlist.txt').write_text('')
            (state/'squid.conf').write_text('old policy')
            with patch.object(sandbox,'inspect',side_effect=lambda c,n: copy.deepcopy(containers.get(n))), \
                 patch.object(sandbox,'run',side_effect=run), patch.object(sandbox,'up',side_effect=up), \
                 patch.object(sandbox,'network_policy',return_value='new policy'), \
                 patch.object(profile_services,'update_policy'), \
                 patch('ai_sandbox_factory.activity.save_config'):
                if failure:
                    with self.assertRaisesRegex(ValueError,'startup failed with diagnostic'):
                        replacement.apply(config,copy.deepcopy(config),state)
                    self.assertEqual(containers,original)
                    self.assertEqual((state/'squid.conf').read_text(),'old policy')
                else:
                    replacement.apply(config,copy.deepcopy(config),state)
                    self.assertEqual(containers[db],original[db])
                    self.assertFalse(any(info['State']['Running'] for info in containers.values()))
        self.assertFalse(any(argv[1] == 'rm' and any(value in (db, original[db]['Id']) for value in argv[2:]) for argv in calls))
        if running:
            self.assertFalse(any(argv[1] == 'stop' and argv[2] in (db, original[db]['Id']) for argv in calls))

    def test_ordinary_rebuild_directs_service_changes_to_reviewed_apply(self):
        original={'profile':sandbox.load_profile('base')}
        updated={'profile':sandbox.load_profile('developer-postgres')}
        with self.assertRaisesRegex(ValueError,'Apply profile changes'):
            profile_services.validate_update(original,updated)
