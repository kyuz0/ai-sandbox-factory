"""Editable sources never silently change a running sandbox or reviewed build."""
import copy
import io
from contextlib import closing, nullcontext, redirect_stdout
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_sandbox_factory import folders, profile_edit as edit, profile_replacement, profile_runtime, profile_services, profile_spec, registry, sandbox, updating


class EditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state' / 'example'
        self.state.mkdir(parents=True)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.source = self.root / 'original'
        shutil.copytree(sandbox.ROOT / 'profiles/base', self.source)
        self.profile = sandbox.load_profile(str(self.source))
        self.saved = sandbox.snapshot_profile(self.profile, self.state)
        self.config = {'name':'example','primary_agent':'pi','engine':'podman','workspace':str(self.workspace),
                       'network':'allowlist','profile':self.saved}
        self.destination = self.root / 'editable'

    def attach(self):
        with patch.object(profile_runtime, 'ensure_cached'), patch('ai_sandbox_factory.profile_replacement.apply') as apply, \
                patch('ai_sandbox_factory.profile_replacement.prepared_services',return_value=nullcontext()), \
                patch.object(sandbox, 'image_graph', return_value=nullcontext()):
            edit.fork(self.config, self.state, self.destination)
            self.config = apply.call_args.args[1]
        return self.config

    def review(self, change='RUN printf test\n'):
        if not self.config.get(edit.EDIT):
            self.attach()
        with (self.destination / 'Dockerfile').open('a') as stream:
            stream.write(change)
        return edit.review(self.config, self.state)

    def test_fork_uses_applied_snapshot_offline_without_touching_original_or_git(self):
        applied = self.state / 'applied-profiles/fixture'
        applied.mkdir(parents=True)
        shutil.copytree(self.saved['path'], applied / 'profile')
        sandbox.write_json(self.state / 'profile-runtime/applied.json', {'path':str(applied)})
        (self.source / 'profile.md').write_text('UPSTREAM MOVED')
        self.attach()
        self.assertEqual(sandbox.tree_digest(self.destination), self.saved['digest'])
        self.assertEqual((self.source / 'profile.md').read_text(), 'UPSTREAM MOVED')
        self.assertFalse((self.destination / '.git').exists())
        self.assertEqual(self.config[edit.EDIT]['origin_digest'], self.saved['digest'])
        self.assertIn(edit.PATH, edit.mounts(self.config)[1])

    def test_nonempty_destination_and_project_overlap_are_not_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / 'my-file'
        marker.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'not an empty folder'):
            edit.fork(self.config, self.state, self.destination)
        self.assertEqual(marker.read_text(), 'keep')
        for path in (self.workspace / 'profile', self.state / 'profile-source'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'workspace|private sandbox state'):
                edit.fork(self.config, self.state, path)
            self.assertFalse(path.exists())

    def test_review_drops_library_identity_and_preserves_customization_origin(self):
        self.config['profile']['registry_name'] = 'base'
        first = self.review()
        self.assertNotIn('registry_name',first['profile'])
        self.assertEqual(first['profile']['customized_from'],
                         {'name':self.profile['name'],'digest':self.saved['digest']})
        self.config['profile'] = first['profile']
        second = self.review('RUN printf later\n')
        self.assertEqual(second['profile']['customized_from'],first['profile']['customized_from'])

    def test_renamed_editable_base_can_be_reviewed_for_every_agent(self):
        self.attach()
        manifest=json.loads((self.destination/'profile.json').read_text())
        manifest['name']='team-developer'
        (self.destination/'profile.json').write_text(json.dumps(manifest))
        for agent in profile_spec.AGENTS:
            with self.subTest(agent=agent):
                self.config['primary_agent']=agent
                reviewed=edit.review(self.config,self.state)
                self.assertEqual(reviewed['profile']['name'],'team-developer')
                self.assertTrue(profile_spec.compatible(reviewed['profile'],agent))

    def test_review_identifies_current_shared_folder_users_without_linking_their_state(self):
        self.attach()
        for name,path in (('peer',self.destination),('unrelated',self.root/'other-profile')):
            state=self.state.parent/name
            state.mkdir()
            sandbox.write_json(state/'config.json',{'name':name,edit.EDIT:{'path':str(path)}})
        broken=self.state.parent/'broken'
        broken.mkdir()
        (broken/'config.json').write_text('{invalid')
        reviewed=self.review()
        self.assertEqual(reviewed['shared_with'],['peer'])
        shutil.rmtree(self.state.parent/'peer')
        self.assertEqual(edit.shared_with(self.config,self.state),[])

    def test_inventory_reports_shared_users_without_failing_on_an_invalid_peer_path(self):
        self.attach()
        for name,path in (('example',self.destination),('peer',self.destination),('broken','invalid\0path')):
            state=self.state.parent/name
            state.mkdir(exist_ok=True)
            config={**self.config,'name':name,edit.EDIT:{**self.config[edit.EDIT],'path':str(path)}}
            sandbox.write_json(state/'config.json',config)
        output=io.StringIO()
        with patch.object(sandbox,'run',return_value=subprocess.CompletedProcess([],0,'','')),redirect_stdout(output):
            sandbox.list_sandboxes(self.state.parent,structured=True)
        records={row['name']:row for row in json.loads(output.getvalue())}
        self.assertEqual(records['example']['shared_profiles'],['peer'])
        self.assertEqual(records['peer']['shared_profiles'],['example'])
        self.assertNotIn('shared_profiles',records['broken'])

    def test_missing_editable_folder_reports_saved_path_without_mutating_state(self):
        self.attach()
        renamed = self.destination.with_name('moved-profile')
        self.destination.rename(renamed)
        with self.assertRaisesRegex(ValueError,'Editable profile folder .* is missing.*Restore it'):
            edit.freeze(self.config,self.state)
        self.assertTrue(Path(self.saved['path']).is_dir())
        renamed.rename(self.destination)
        self.assertEqual(edit.freeze(self.config,self.state)['digest'],self.saved['digest'])

    def test_lifecycle_actions_recover_interrupted_profile_before_using_saved_config(self):
        (self.state/'profile-preparation').mkdir()
        for action in ('up','down','delete','build'):
            with self.subTest(action=action):
                sandbox.write_json(self.state/'config.json',self.config)
                def recover(config,state):
                    sandbox.write_json(state/'config.json',{**config,'recovered':'previous-runtime'})
                with patch.object(profile_replacement,'recover_preparation',side_effect=recover) as recovery, \
                        patch.object(sandbox,action) as operation:
                    sandbox.sandbox_command(SimpleNamespace(command=action),self.state,None)
                recovery.assert_called_once()
                self.assertEqual(operation.call_args.args[0]['recovered'],'previous-runtime')

    def test_open_pi_and_browser_resolve_pending_recovery_before_using_a_runtime(self):
        from ai_sandbox_factory import browser_editor
        (self.state/'profile-preparation').mkdir()
        sandbox.write_json(self.state/'config.json',self.config)
        def recover(config,state):
            sandbox.write_json(state/'config.json',{**config,'recovered':'previous-runtime'})
        with patch.object(profile_replacement,'recover_preparation',side_effect=recover), \
                patch.object(sandbox.operation_locks,'private_runtime_dir',return_value=self.root), \
                patch.object(sandbox,'up') as start, patch.object(sandbox,'run'), \
                patch.object(sandbox,'names',wraps=sandbox.names) as names:
            sandbox.open_selected(self.config,self.state,'pi')
            self.assertEqual(start.call_args.args[0]['recovered'],'previous-runtime')
            for command in ('pi','exec','shell','check'):
                sandbox.sandbox_command(SimpleNamespace(command=command,name='example',args=['--','true'],
                                                       allowed_url='https://example.test'),self.state,None)
                self.assertEqual(names.call_args.args[0]['recovered'],'previous-runtime')
        with patch.object(profile_replacement,'recover_preparation',side_effect=recover), \
                patch.object(sandbox.operation_locks,'private_runtime_dir',return_value=self.root), \
                patch('ai_sandbox_factory.host_platform.check_engine'), patch.object(sandbox,'inspect',return_value=None) as inspect:
            with self.assertRaisesRegex(ValueError,'Start the sandbox'):
                browser_editor.open_browser_editor(self.config,self.state,background=False)
            self.assertEqual(inspect.call_args.args[0]['recovered'],'previous-runtime')

    def test_failed_attachment_keeps_copy_and_allows_same_folder_retry(self):
        with patch.object(profile_runtime, 'ensure_cached'), \
                patch('ai_sandbox_factory.profile_replacement.prepared_services',return_value=nullcontext()), \
                patch('ai_sandbox_factory.profile_replacement.apply', side_effect=ValueError('startup diagnostic')), \
                patch.object(sandbox, 'image_graph', return_value=nullcontext()):
            with self.assertRaisesRegex(ValueError, 'startup diagnostic'):
                edit.fork(self.config, self.state, self.destination)
        self.assertEqual(sandbox.tree_digest(self.destination), self.saved['digest'])
        self.attach()
        self.assertFalse((self.state / 'editable-profile-pending.json').exists())

    def test_fork_resolves_a_windows_path_before_creating_the_destination(self):
        from ai_sandbox_factory import host_platform
        original=host_platform.local_path
        windows=r'C:\Users\Example\profiles\editable'
        with patch.object(host_platform,'local_path',side_effect=lambda value:self.destination if value==windows else original(value)), \
                patch.object(profile_runtime,'ensure_cached'), \
                patch.object(sandbox,'image_graph',return_value=nullcontext()), \
                patch.object(profile_replacement,'prepared_services',return_value=nullcontext()), \
                patch.object(profile_replacement,'apply'):
            edit.fork(self.config,self.state,windows)
        profile_spec.validate(self.destination)

    def test_windows_backed_fork_copies_contents_without_requiring_unix_metadata(self):
        with patch('ai_sandbox_factory.host_platform.is_wsl', return_value=True), \
                patch('ai_sandbox_factory.host_platform.windows_drive', return_value=True), \
                patch.object(shutil, 'copystat', side_effect=PermissionError('DrvFS has no metadata')):
            edit._copy_source(self.source, self.destination)
        self.assertEqual(sandbox.tree_digest(self.source), sandbox.tree_digest(self.destination))
        profile_spec.validate(self.destination)

    def test_review_freezes_uncommitted_files_and_diff_includes_new_dockerfile(self):
        review = self.review()
        self.assertIn('RUN printf test', review['diff'])
        self.assertNotEqual(review['profile']['path'], str(self.destination))
        self.assertEqual(Path(review['profile']['path']).parent, self.state / 'profiles')
        (self.destination / 'Dockerfile').write_text('LATER EDIT')
        self.assertIn('RUN printf test', (Path(review['profile']['path']) / 'Dockerfile').read_text())
        with patch.object(sandbox, 'build') as build:
            with self.assertRaisesRegex(ValueError, 'changed after review'):
                edit.apply(self.config, self.state, review['revision'])
            build.assert_not_called()

    def test_review_cannot_apply_to_another_base_or_folder(self):
        review = self.review()
        stale = copy.deepcopy(self.config)
        stale['profile']['digest'] = 'changed-base'
        with self.assertRaisesRegex(ValueError, 'applied profile or editable folder changed'):
            edit.apply(stale, self.state, review['revision'])
        with self.assertRaisesRegex(ValueError, 'Unknown profile review'):
            edit.apply(self.config, self.state, '../escape')

    def test_review_output_is_not_confused_by_recovery_diagnostics(self):
        from ai_sandbox_factory.sandbox_tui import Service
        service=Service(self.state.parent)
        def capture(command,name,*args):
            output=Path(args[1])
            output.write_text('{"revision":"reviewed"}')
            return subprocess.CompletedProcess([],0,'Restored the previous runtime.\nengine output\n','')
        with patch.object(service,'capture',side_effect=capture):
            self.assertEqual(service.review_profile('example'),{'revision':'reviewed'})

    def test_git_metadata_does_not_change_a_review_and_fetched_sources_register_offline(self):
        review=self.review()
        (self.destination/'.git').mkdir()
        (self.destination/'.git/HEAD').write_text('ref: refs/heads/main\n')
        self.assertEqual(edit.review(self.config,self.state)['revision'],review['revision'])
        # A fork can contain the actual dependency files saved at creation,
        # even if the original profile fetched them from another repository.
        (self.destination/'source').mkdir()
        (self.destination/'source/tool.py').write_text('print("bundled source")\n')
        (self.destination/'source-revisions.json').write_text('{"commit":"saved-origin"}')
        manifest=json.loads((self.destination/'profile.json').read_text())
        manifest['git_source']='https://example.test/unreachable-source.git'
        (self.destination/'profile.json').write_text(json.dumps(manifest))
        self.assertNotIn('git_source',sandbox.load_profile(str(self.destination)))
        with patch.dict('os.environ',SANDBOX_REGISTRY_DIR=str(self.root/'library')), \
                patch.object(registry,'export',wraps=registry.export) as export:
            # Remove the synthetic .git fixture so registration treats this
            # as an ordinary local folder, not an initialized repository.
            shutil.rmtree(self.destination/'.git')
            row=registry.add('profile','independent',str(self.destination))
            self.assertEqual(export.call_count,1,'registration unexpectedly fetched an upstream source')
        installed=Path(row['path'])
        self.assertEqual((installed/'source/tool.py').read_text(),'print("bundled source")\n')
        self.assertNotIn('git_source',json.loads((installed/'profile.json').read_text()))
        self.assertIn('git_source',json.loads((self.destination/'profile.json').read_text()))

    def test_bad_build_preserves_configuration_and_never_starts_services_or_cutover(self):
        review = self.review('RUN missing-package-command\n')
        with patch.object(sandbox, 'image_graph', return_value=nullcontext()), \
                patch.object(sandbox, 'build', side_effect=ValueError('apt: Unable to locate package imaginary')), \
                patch.object(profile_services, 'up') as services, patch('ai_sandbox_factory.profile_replacement.apply') as apply:
            with self.assertRaisesRegex(ValueError, 'Unable to locate package imaginary'):
                edit.apply(self.config, self.state, review['revision'])
            services.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(self.config['profile'], self.saved)

    def test_each_agent_has_one_writable_profile_root_and_clear_apply_instructions(self):
        self.attach()
        for agent in profile_spec.AGENTS:
            with self.subTest(agent=agent):
                config = {**self.config, 'primary_agent':agent}
                self.assertEqual(folders.roots(config).count((edit.PATH, False)), 1)
                context = edit.guidance(config)
                self.assertIn('--agent ' + agent, context)
                self.assertIn('Editing the source does not change the running sandbox', context)
                self.assertIn('Review changes', context)
                self.assertIn('Dockerfile', context)
                self.assertIn('services entry in profile.json', context)
                self.assertIn('forked from '+repr(self.saved['name']),context)
                self.assertIn(self.saved['digest'],context)
                self.assertIn('If undecided, ask whether to track this folder with Git',context)
                self.assertIn('push to the supplied origin',context)
                self.assertIn('Follow standing maintenance instructions without asking again',context)
                self.assertIn('Apply updates only this sandbox',context)
                self.assertIn('Other sandboxes can share this editable folder',context)
                self.assertIn('preserve uncommitted edits',context)
                self.assertIn('future sessions receive them after Apply',context)
                self.assertNotIn(str(self.destination), context)
        self.assertNotIn((edit.PATH, False), folders.roots({}))
        self.assertIn('create an editable copy', edit.guidance({}))

    def test_profile_customization_is_need_based_in_every_agent_context(self):
        self.attach()
        for agent in profile_spec.AGENTS:
            for attached in (False,True):
                with self.subTest(agent=agent,attached=attached):
                    config={**self.config,'primary_agent':agent}
                    if not attached:config.pop(edit.EDIT)
                    context=edit.guidance(config)
                    self.assertIn('Customize the profile only for needed',context)
                    self.assertIn('persistent system software',context)
                    self.assertIn('new services (such as a database)',context)
                    self.assertIn('Once the environment works, leave it unchanged',context)
                    self.assertIn('a new task, session or day needs no profile change',context)
                    self.assertIn('ordinary project dependencies in the project',context)
                    self.assertIn('Reuse an editable profile unless the user requests another fork',context)
                    if attached:
                        self.assertIn('When resuming profile maintenance, inspect',context)
                        self.assertIn('read editable profile.md for choices saved since the last Apply',context)
                        self.assertIn('whether that is one-time or ongoing',context)
                        self.assertNotIn('After a restart, inspect',context)

    def test_applied_edits_keep_the_original_fork_identity_in_future_agent_context(self):
        reviewed=self.review()
        changed={**self.config,'profile':reviewed['profile']}
        self.assertNotEqual(changed['profile']['digest'],self.saved['digest'])
        with patch.object(profile_runtime,'applied',return_value={
                'name':changed['profile']['name'],'profile_digest':changed['profile']['digest']}):
            for agent in profile_spec.AGENTS:
                from ai_sandbox_factory import primary_agents
                config={**changed,'primary_agent':agent}
                context=profile_runtime.guidance(self.state,config) if agent=='pi' else primary_agents.guidance(config,self.state)
                self.assertIn(changed['profile']['digest'],context)
                self.assertIn(self.saved['digest'],context)
                self.assertIn('forked from '+repr(self.saved['name']),context)
                self.assertIn('Customize the profile only for needed',context)
                self.assertIn(edit.PATH,context)
                self.assertIn('If undecided, ask whether to track this folder with Git',context)
                self.assertIn('inspect this folder’s Git status, log and remotes',context)

    def test_additions_and_metadata_are_allowed_but_existing_service_runtime_changes_are_refused(self):
        original = {**self.config, 'profile':sandbox.load_profile('developer-postgres')}
        updated = copy.deepcopy(original)
        updated['profile']['services']['reporting'] = copy.deepcopy(updated['profile']['services']['database'])
        profile_services.validate_update(original, updated, allow_additions=True)
        updated['profile']['services']['database']['description'] = 'New description'
        updated['profile']['services']['database']['connections']['purpose'] = 'project'
        profile_services.validate_update(original, updated, allow_additions=True)
        for field, value in [('environment', {}), ('volumes', {}), ('command', ['false']), ('ssh', {})]:
            broken = copy.deepcopy(updated)
            broken['profile']['services']['database'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "existing profile service 'database'.*" + field):
                profile_services.validate_update(original, broken, allow_additions=True)
        del updated['profile']['services']['database']
        with self.assertRaisesRegex(ValueError, "remove existing profile service 'database'"):
            profile_services.validate_update(original, updated, allow_additions=True)

    def test_validation_error_names_file_line_agent_and_service_field(self):
        file = self.source / 'profile.json'
        file.write_text('{\n"name": bad\n}')
        with self.assertRaisesRegex(ValueError, r'profile.json: line 2, column'):
            profile_spec.validate(self.source)
        file.write_text(json.dumps({'name':'custom','agents':['pi']}))
        with self.assertRaisesRegex(ValueError, "coding agent 'codex'"):
            profile_spec.validate(self.source, 'codex')
        file.write_text(json.dumps({'name':'custom','services':{'database':{'description':'db','image':'postgres',
                                 'connections':{'password':{'environment':[]}}}}}))
        with self.assertRaisesRegex(ValueError, "database.*connection password.*not declared"):
            profile_spec.validate(self.source)

    def test_standalone_validator_works_without_factory_installed(self):
        script = self.root / 'validate.py'
        shutil.copyfile(profile_spec.__file__, script)
        result = subprocess.run([sys.executable, '-I', str(script), str(self.source), '--agent','codex'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Static checks passed', result.stdout)
        (self.source / 'Dockerfile').unlink()
        result = subprocess.run([sys.executable, '-I', str(script), str(self.source)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('required profile file', result.stderr)
        self.assertIn('Dockerfile', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_interrupted_preparation_and_cutover_restore_old_runtime_and_exact_settings(self):
        self.journal_case(committed=False, cutover=False)
        self.journal_case(committed=False, cutover=True)

    def test_committed_recovery_only_retires_the_old_container(self):
        self.journal_case(committed=True, cutover=True)

    def test_recovery_keeps_a_stopped_workspace_and_restarts_its_previously_running_helper(self):
        self.journal_case(committed=False, cutover=True, workspace_running=False)

    def test_failure_logs_keep_exit_cause_and_redact_generated_service_passwords(self):
        directory=self.state/'profile-services'
        directory.mkdir()
        (directory/'values.json').write_text('{"database-password":"synthetic-private-password"}')
        output=io.StringIO()
        with patch.object(sandbox,'inspect',return_value={'State':{'ExitCode':31,'Error':'bootstrap failed'}}), \
                patch.object(sandbox,'run',return_value=subprocess.CompletedProcess([],0,'STARTUP_CAUSE synthetic-private-password','')), \
                redirect_stdout(output):
            profile_replacement.failure_logs(self.config,self.state,{'candidate'})
        self.assertIn('Container exit 31: bootstrap failed',output.getvalue())
        self.assertIn('STARTUP_CAUSE [redacted]',output.getvalue())
        self.assertNotIn('synthetic-private-password',output.getvalue())

    def journal_case(self, *, committed, cutover, workspace_running=True):
        config=copy.deepcopy(self.config)
        updated={**config,'profile':sandbox.load_profile('developer-postgres')}
        dev=sandbox.names(config)[0]
        def info(name,identifier):
            return {'Name':name,'Id':identifier,'State':{'Running':True},
                    'Config':{'Labels':{sandbox.LABEL:'example',profile_services.LABEL_ROLE:name}}}
        containers={dev:info(dev,'old-workspace')}
        containers[dev]['State']['Running']=workspace_running
        helper=sandbox.names(config)[1]
        if not workspace_running:containers[helper]=info(helper,'old-helper')
        resources={'volume':set(),'network':set()}
        wanted=profile_services.resources(updated)
        def inspect(candidate,name):
            return copy.deepcopy(next((value for key,value in containers.items() if key==name or value['Id']==name),None))
        def run(argv,**kwargs):
            operation=argv[1]
            if operation in ('container','volume','network') and argv[2]=='exists':
                present=argv[3] in (containers if operation=='container' else resources[operation])
                return subprocess.CompletedProcess(argv,0 if present else 1,'','')
            if operation in ('volume','network') and argv[2]=='inspect':
                return subprocess.CompletedProcess(argv,0,json.dumps([{'Labels':{sandbox.LABEL:'example'}}]),'')
            if operation in ('volume','network') and argv[2]=='rm':
                resources[operation].discard(argv[3])
            elif operation=='rm':
                value=argv[-1]
                key=next(key for key,entry in containers.items() if key==value or entry['Id']==value)
                del containers[key]
            elif operation=='rename':
                key=next(key for key,entry in containers.items() if key==argv[2] or entry['Id']==argv[2])
                entry=containers.pop(key);entry['Name']=argv[3];containers[argv[3]]=entry
            elif operation in ('start','stop'):
                next(value for key,value in containers.items() if key==argv[2] or value['Id']==argv[2])['State']['Running']=operation=='start'
            return subprocess.CompletedProcess(argv,0,'','')
        directory=self.state/'profile-services'
        directory.mkdir(exist_ok=True)
        (directory/'values.json').write_text('{"existing":"preserved"}')
        (self.state/'allowlist.txt').write_text('old.example:443\n')
        (self.state/'squid.conf').write_text('original policy')
        with patch.object(sandbox,'inspect',side_effect=inspect),patch.object(sandbox,'run',side_effect=run), \
                patch.object(sandbox,'up'):
            owner=profile_replacement.prepared_services(config,updated,self.state)
            with closing(owner.gen):
                owner.__enter__()
                for name in wanted['container']:containers[name]=info(name,'new-'+name)
                for kind in resources:resources[kind].update(wanted[kind])
                (directory/'values.json').write_text('{"existing":"wrong","new":"temporary"}')
                if cutover:
                    run(['podman','rename','old-workspace',dev+'-previous'])
                    containers[dev]=info(dev,'new-workspace')
                if committed:
                    file=self.state/'profile-preparation/record.json'
                    saved=json.loads(file.read_text());saved['committed']=True;file.write_text(json.dumps(saved))
                profile_replacement.recover_preparation(config,self.state)
        self.assertFalse((self.state/'profile-preparation').exists())
        if committed:
            self.assertEqual(containers[dev]['Id'],'new-workspace')
            self.assertFalse(any(value['Id']=='old-workspace' for value in containers.values()))
            self.assertTrue(wanted['container'].issubset(containers))
        else:
            self.assertEqual(set(containers),{dev} if workspace_running else {dev,helper})
            self.assertEqual(containers[dev]['Id'],'old-workspace')
            self.assertEqual(containers[dev]['State']['Running'],workspace_running)
            if not workspace_running:self.assertTrue(containers[helper]['State']['Running'])
            self.assertFalse(resources['volume']|resources['network'])
            self.assertEqual((directory/'values.json').read_text(),'{"existing":"preserved"}')
            self.assertEqual((self.state/'squid.conf').read_text(),'original policy')


class CooperativeUpdateTests(unittest.TestCase):
    def test_cleanup_failure_after_cancel_is_failed_not_cancelled(self):
        from concurrent.futures import ThreadPoolExecutor
        ready=threading.Event()
        control=updating.Update(cooperative=True)
        script="""from pathlib import Path
import sys,time
gate=Path(sys.argv[-1]);print('Ready',flush=True)
while not gate.exists():time.sleep(.01)
print('Cannot remove new service volume fixture-data: engine refused deletion',flush=True)
sys.exit(17)
"""
        with ThreadPoolExecutor() as pool:
            future=pool.submit(control.run,[sys.executable,'-c',script],lambda line:ready.set() if line=='Ready' else None)
            self.assertTrue(ready.wait(10))
            self.assertTrue(control.cancel())
            with self.assertRaisesRegex(ValueError,'worker exit 17.*fixture-data'):
                future.result(timeout=10)

    def test_build_cancellation_stops_its_child_and_restores_the_normal_runner(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor() as pool:
            root=Path(temporary)
            ready,cancel=root/'ready',root/'cancel'
            def build():
                with sandbox.cancellable_build(cancel.exists):
                    sandbox.run([sys.executable,'-c',
                        'from pathlib import Path; import time; Path('+repr(str(ready))+').touch(); time.sleep(600)'],stderr=subprocess.DEVNULL)
            future=pool.submit(build)
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(),'build command did not reach its milestone')
            cancel.touch()
            with self.assertRaises(updating.UpdateCancelled):
                future.result(timeout=20)
            self.assertEqual(sandbox.run([sys.executable,'-c','print("normal")'],capture=True).stdout.strip(),'normal')

    def test_cancel_waits_for_worker_cleanup_without_killing_it(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as temporary, ThreadPoolExecutor() as pool:
            root = Path(temporary)
            ready = threading.Event()
            control = updating.Update(cooperative=True)
            script = """import pathlib,sys,time
gate=pathlib.Path(sys.argv[-1]); root=pathlib.Path(sys.argv[1])
print('Ready',flush=True)
while not gate.exists(): time.sleep(.01)
(root/'cleaned').write_text('cleanup complete')
"""
            with patch('ai_sandbox_factory.host_platform.stop_process_group') as stop:
                future = pool.submit(control.run, [sys.executable,'-c',script,str(root)], lambda line: ready.set() if line == 'Ready' else None)
                self.assertTrue(ready.wait(10), 'worker did not reach its milestone')
                self.assertTrue(control.cancel())
                with self.assertRaises(updating.UpdateCancelled):
                    future.result(timeout=10)
                stop.assert_not_called()
                self.assertEqual((root/'cleaned').read_text(), 'cleanup complete')


if __name__ == '__main__':
    unittest.main()
