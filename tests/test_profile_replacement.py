"""Workspace cutover preserves applications and restores all Factory helpers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ai_sandbox_factory import browser_editor, profile_replacement as replacement, profile_services, runtime_context, sandbox


class ReplacementTests(unittest.TestCase):
    def test_stopped_runtime_helpers_stay_stopped_and_db_is_never_removed(self):
        self.exercise(False, False)

    def test_startup_failure_restores_exact_workspace_and_helper_ids(self):
        self.exercise(True, True)

    def test_open_editor_resumes_on_replacement_before_old_containers_are_removed(self):
        self.exercise(True, False, browser=True)

    def test_failed_editor_reconnection_restores_previous_runtime_and_editor(self):
        self.exercise(True, False, browser=True, reconnect_failure=True)

    def test_startup_failure_restores_open_editor_at_previous_address(self):
        self.exercise(True, True, browser=True)

    def exercise(self, running, failure, browser=False, reconnect_failure=False):
        config = {'name':'fixture','engine':'podman','primary_agent':'codex',
                  'profile':sandbox.load_profile('developer-postgres')}
        workspace = sandbox.names(config)[0]
        helper = workspace + '-agent'
        db = profile_services.container(config,'database')
        containers = {name: {'Id':'old-' + str(index),'State':{'Running':running}}
                      for index,name in enumerate((workspace,helper,db))}
        original = copy.deepcopy(containers)
        calls=[]
        connection={'url':'http://127.0.0.1:12345','preferences':{'mode':'browser'}} if browser else None
        resumed=[]
        def resume(config,state,snapshot):
            self.assertEqual(snapshot,connection)
            self.assertEqual(snapshot_file.read_bytes(), previous_context)
            resumed.append(containers[workspace]['Id'])
            if resumed[-1].startswith('new-'):
                self.assertTrue(any(value['Id']==original[workspace]['Id'] for value in containers.values()))
                if reconnect_failure:
                    raise ValueError('Cannot reconnect editor: address already in use')
            else:
                self.assertEqual(resumed[-1],original[workspace]['Id'])
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
            runtime_context.publish_saved(state)
            self.assertEqual(snapshot_file.read_bytes(), previous_context)
            if starts==1:
                for name in (workspace,helper): containers[name]={'Id':'new-' + name,'State':{'Running':True}}
                containers[db]['State']['Running']=True
                if failure: raise ValueError('startup failed with diagnostic')
            else:
                for name in (workspace,helper,db): containers[name]['State']['Running']=True
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            config['workspace'] = str(state)
            (state/'allowlist.txt').write_text('')
            (state/'squid.conf').write_text('old policy')
            sandbox.activity.save_config(state, config)
            runtime_context.publish_saved(state)
            snapshot_file = state / 'primary-runtime/sandbox-state.json'
            previous_context = snapshot_file.read_bytes()
            updated = {**config, 'memory': '2g'}
            with patch.object(sandbox,'inspect',side_effect=lambda c,n: copy.deepcopy(containers.get(n))), \
                 patch.object(sandbox,'run',side_effect=run), patch.object(sandbox,'up',side_effect=up), \
                 patch.object(sandbox,'network_policy',return_value='new policy'), \
                 patch.object(profile_services,'update_policy'), \
                 patch.object(browser_editor,'replacement_connection',return_value=connection), \
                 patch.object(browser_editor,'stop') as stop_editor, \
                 patch.object(browser_editor,'resume_connection',side_effect=resume) as resume_editor:
                if failure or reconnect_failure:
                    with self.assertRaisesRegex(ValueError,'address already in use' if reconnect_failure else 'startup failed with diagnostic'):
                        replacement.apply(config,updated,state)
                    self.assertEqual(containers,original)
                    self.assertEqual((state/'squid.conf').read_text(),'old policy')
                    self.assertEqual(snapshot_file.read_bytes(), previous_context)
                else:
                    replacement.apply(config,updated,state)
                    self.assertEqual(json.loads(snapshot_file.read_text())['values']['Memory limit'], '2g')
                    self.assertEqual(containers[db],original[db])
                    self.assertTrue(all(info['State']['Running'] == running for info in containers.values()))
                if browser:
                    stop_editor.assert_called_once_with(state)
                    self.assertEqual(resumed[-1],original[workspace]['Id'] if failure or reconnect_failure else 'new-'+workspace)
                else:
                    stop_editor.assert_not_called()
                    resume_editor.assert_not_called()
        self.assertFalse(any(argv[1] == 'rm' and any(value in (db, original[db]['Id']) for value in argv[2:]) for argv in calls))
        if running:
            self.assertFalse(any(argv[1] == 'stop' and argv[2] in (db, original[db]['Id']) for argv in calls))

    def test_ordinary_rebuild_directs_service_changes_to_reviewed_apply(self):
        original={'profile':sandbox.load_profile('base')}
        updated={'profile':sandbox.load_profile('developer-postgres')}
        with self.assertRaisesRegex(ValueError,'Apply profile changes'):
            profile_services.validate_update(original,updated)
