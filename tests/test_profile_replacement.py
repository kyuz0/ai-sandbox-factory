"""Workspace cutover preserves applications and restores all Factory helpers."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from ai_sandbox_factory import browser_editor, profile_replacement as replacement, profile_services, runtime_context, sandbox


class ReplacementTests(unittest.TestCase):
    def test_cleanup_failure_keeps_successful_update_and_restored_editor(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.exercise(True, False, browser=True, services=False, action='update', cleanup_failure=True)
        self.assertIn('Update applied, but the previous stopped container', output.getvalue())
        self.assertIn('fixture cleanup refused', output.getvalue())
        self.assertIn('Retry cleanup with: podman rm old-', output.getvalue())

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

    def test_plain_settings_update_restores_open_editor_and_rolls_back_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.exercise(True, failure, browser=True, services=False, action='update')

    def test_plain_rebuild_restores_open_editor_and_rolls_back_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.exercise(True, failure, browser=True, services=False, action='recreate')

    def test_network_policy_switch_restores_open_editor_and_rolls_back_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.exercise(True, failure, browser=True, services=False, action='policy')

    def test_plain_update_and_rebuild_preserve_stopped_and_no_editor_states(self):
        for action in ('update', 'recreate'):
            for running in (False, True):
                with self.subTest(action=action, running=running):
                    self.exercise(running, False, services=False, action=action)

    def exercise(self, running, failure, browser=False, reconnect_failure=False, services=True, action=None, cleanup_failure=False):
        config = {'name':'fixture','engine':'podman','primary_agent':'codex',
                  'uid':os.getuid(),'gid':os.getgid(),
                  'profile':sandbox.load_profile('developer-postgres' if services else 'base')}
        workspace = sandbox.names(config)[0]
        helper = workspace + '-agent'
        db = profile_services.container(config,'database')
        containers = {name: {'Id':'old-' + str(index),'State':{'Running':running}}
                      for index,name in enumerate((workspace,helper,db) if services else (workspace,helper))}
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
            elif operation == 'rm':
                if cleanup_failure and containers[key]['Id'].startswith('old-'):
                    raise subprocess.CalledProcessError(125,argv,stderr='fixture cleanup refused')
                containers.pop(key)
        starts=0
        def up(candidate,*args,**kwargs):
            nonlocal starts
            starts+=1
            runtime_context.publish_saved(state)
            self.assertEqual(snapshot_file.read_bytes(), previous_context)
            if starts==1:
                for name in (workspace,helper): containers[name]={'Id':'new-' + name,'State':{'Running':True}}
                if services:
                    containers[db]['State']['Running']=True
                if failure: raise ValueError('startup failed with diagnostic')
            else:
                for info in containers.values(): info['State']['Running']=True
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state'/'fixture'
            state.mkdir(parents=True)
            project=Path(tmp)/'project'
            project.mkdir()
            (Path(tmp)/'locks').mkdir(mode=0o700)
            config['workspace'] = str(project)
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
                 patch.object(sandbox,'build'), \
                 patch.object(sandbox.operation_locks,'private_runtime_dir',return_value=Path(tmp)/'locks'), \
                 patch.object(browser_editor,'replacement_connection',return_value=connection), \
                 patch.object(browser_editor,'stop') as stop_editor, \
                 patch.object(browser_editor,'resume_connection',side_effect=resume) as resume_editor:
                def apply():
                    if action == 'update':
                        sandbox.apply_settings(config,state,{'memory':'2g'})
                    elif action == 'recreate':
                        sandbox.sandbox_command(SimpleNamespace(command='recreate',profile=None,
                    subagents=None,browser=None,apply_gate=None,image_source=None),state,None)
                    elif action == 'policy':
                        sandbox.sandbox_command(SimpleNamespace(command='policy',network='unrestricted',
                            allowlist=None),state,None)
                    else:
                        replacement.apply(config,updated,state)
                if failure or reconnect_failure:
                    with self.assertRaisesRegex(ValueError,'address already in use' if reconnect_failure else 'startup failed with diagnostic'):
                        apply()
                    self.assertEqual(containers,original)
                    self.assertEqual((state/'squid.conf').read_text(),'old policy')
                    self.assertEqual(snapshot_file.read_bytes(), previous_context)
                else:
                    apply()
                    if action not in ('recreate', 'policy'):
                        self.assertEqual(json.loads(snapshot_file.read_text())['values']['Memory limit'], '2g')
                    if services:
                        self.assertEqual(containers[db],original[db])
                    active = [info for name,info in containers.items() if '-previous-' not in name]
                    self.assertTrue(all(info['State']['Running'] == running for info in active))
                    if cleanup_failure:
                        retained = [info for name,info in containers.items() if '-previous-' in name]
                        self.assertTrue(retained)
                        self.assertTrue(all(not info['State']['Running'] for info in retained))
                if browser:
                    stop_editor.assert_called_once_with(state)
                    self.assertEqual(resumed[-1],original[workspace]['Id'] if failure or reconnect_failure else 'new-'+workspace)
                else:
                    stop_editor.assert_not_called()
                    resume_editor.assert_not_called()
        if services:
            self.assertFalse(any(argv[1] == 'rm' and any(value in (db, original[db]['Id']) for value in argv[2:]) for argv in calls))
        if running and services:
            self.assertFalse(any(argv[1] == 'stop' and argv[2] in (db, original[db]['Id']) for argv in calls))

    def test_ordinary_rebuild_directs_service_changes_to_reviewed_apply(self):
        original={'profile':sandbox.load_profile('base')}
        updated={'profile':sandbox.load_profile('developer-postgres')}
        with self.assertRaisesRegex(ValueError,'Apply profile changes'):
            profile_services.validate_update(original,updated)
