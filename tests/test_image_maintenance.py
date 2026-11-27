"""Weekly updates may be promoted only from the exact verified candidate commit."""
from datetime import datetime, timezone, timedelta
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REAL_RUN=subprocess.run
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import image_maintenance as maintenance
import build_factory_images as publisher

SHA='a'*40
OTHER='b'*40
OLD='c'*40
STAGING=maintenance.BRANCH+'-staging-123-1'
RUN='https://github.com/kyuz0/ai-sandbox-factory/actions/runs/123'


class Promotion(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(maintenance,'ROOT',self.root))
        report=self.root/maintenance.REPORT;report.parent.mkdir()
        report.write_text(json.dumps({'base_commit':OTHER,'review_head':OLD,'components':{'pi':{'current':'1.0.0','available':'1.1.0','changed':True,'status':'updated'}}}))
        self.api=self.enterContext(patch.object(maintenance,'api',return_value=[]))
        self.command=self.enterContext(patch.object(maintenance,'command',side_effect=lambda *args,**kwargs:
            SHA if args[1]=='rev-parse' else (OTHER if args[-1]=='refs/heads/main' else SHA)+'\trefs/heads/'+STAGING))
        self.diff=self.enterContext(patch.object(maintenance.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')))
        self.enterContext(patch.dict(os.environ,{'FACTORY_AUTOMATED_PRS':''}))
        self.enterContext(patch.dict(os.environ,{},clear=False))

    def test_a_new_commit_after_verification_prevents_status_and_pr_creation(self):
        self.command.side_effect=[SHA,OTHER+'\trefs/heads/'+maintenance.BRANCH]
        with self.assertRaisesRegex(ValueError,'changed during testing'):
            maintenance.pull_request(SHA,STAGING,RUN)
        self.api.assert_not_called()

    def test_a_wrong_checkout_cannot_claim_the_tested_status(self):
        self.command.return_value=OTHER;self.command.side_effect=None
        with self.assertRaisesRegex(ValueError,'does not match'):
            maintenance.pull_request(SHA,STAGING,RUN)
        self.api.assert_not_called()

    def test_default_mode_provides_verified_branch_without_requesting_broader_permissions(self):
        maintenance.pull_request(SHA,STAGING,RUN)
        self.assertEqual(len(self.api.call_args_list),1)
        method,path,data=self.api.call_args.args
        self.assertEqual((method,path),('POST',f'repos/{maintenance.REPOSITORY}/statuses/{SHA}'))
        self.assertEqual(data['context'],'maintenance/verified')

    def test_approved_pr_mode_sets_status_on_exact_commit_and_opens_one_pr(self):
        with patch.dict(os.environ,{'FACTORY_AUTOMATED_PRS':'true'}):
            maintenance.pull_request(SHA,STAGING,RUN)
        self.assertEqual(self.api.call_args.args[:2],('POST',f'repos/{maintenance.REPOSITORY}/pulls'))
        self.assertEqual(self.api.call_args.args[2]['head'],maintenance.BRANCH)
        self.assertEqual(self.api.call_args.args[2]['base'],'main')
        self.assertIn('1.1.0',self.api.call_args.args[2]['body'])
        self.assertNotIn('/merge',str(self.api.call_args_list))

    def test_partial_update_remains_incomplete_after_verified_safe_subset(self):
        report=self.root/maintenance.REPORT
        report.write_text(json.dumps({'base_commit':OTHER,'review_head':OLD,'components':{'agy':{'status':'failed','error':'Upstream HTTP 503','changed':False}}}))
        with self.assertRaisesRegex(ValueError,'failed components'):
            maintenance.pull_request(SHA,STAGING,RUN)
        self.assertEqual(self.api.call_args.args[2]['state'],'success')

    def test_existing_verified_branch_moves_only_with_its_original_lease(self):
        maintenance.pull_request(SHA,STAGING,RUN)
        pushes=[call.args for call in self.command.call_args_list if call.args[:2]==('git','push')]
        self.assertEqual(len(pushes),1)
        self.assertIn('--force-with-lease=refs/heads/'+maintenance.BRANCH+':'+OLD,pushes[0])
        self.assertEqual(pushes[0][-1],SHA+':refs/heads/'+maintenance.BRANCH)

    def test_source_drift_on_main_preserves_the_previous_verified_branch(self):
        self.diff.return_value=subprocess.CompletedProcess([],1,'','')
        with self.assertRaisesRegex(ValueError,'changed on main'):
            maintenance.pull_request(SHA,STAGING,RUN)
        self.assertFalse(any(call.args[:2]==('git','push') for call in self.command.call_args_list))
        self.api.assert_not_called()

    def test_failed_component_is_not_hidden_by_an_identical_inventory_or_test_mode(self):
        report=self.root/maintenance.REPORT
        report.write_text(json.dumps({'components':{'agy':{'status':'failed','error':'HTTP 503','changed':False}}}))
        with patch.dict(os.environ,{'FACTORY_PROMOTION_INVENTORIES':str(self.root)}),patch.object(maintenance,'meaningful_update',return_value=False):
            with self.assertRaisesRegex(ValueError,'check failed'):
                maintenance.pull_request(SHA,STAGING,RUN)
        with self.assertRaisesRegex(ValueError,'failed components'):
            maintenance.pull_request(SHA,maintenance.BRANCH+'-test-123-1',RUN)
        self.api.assert_not_called()

    def test_identical_package_inventory_does_not_create_a_needless_update(self):
        directory=self.root/'inventories'
        old=directory/'image-scan-fixture';new=directory/'image-gateway'
        old.mkdir(parents=True);new.mkdir()
        (old/'factory-image-review.json').write_text(json.dumps({'current':True,'image':'docker.io/kyuz0/ai-sandbox-factory-gateway@sha256:'+'a'*64}))
        value={'components':[{'name':'curl','version':'1.0','purl':'pkg:deb/curl@1.0'}]}
        (old/'factory-image.cdx.json').write_text(json.dumps(value));(new/'gateway.cdx.json').write_text(json.dumps(value))
        report={'base_commit':OTHER,'components':{'pi':{'changed':False}}}
        self.assertFalse(maintenance.meaningful_update(report,directory))
        value['components'][0]['version']='1.1'
        (new/'gateway.cdx.json').write_text(json.dumps(value))
        self.assertTrue(maintenance.meaningful_update(report,directory))

    def test_pending_identical_proposal_keeps_its_verified_commit(self):
        directory=self.root/'inventories'
        prior=directory/'image-scan-pending';candidate=directory/'image-gateway'
        prior.mkdir(parents=True);candidate.mkdir()
        value={'components':[{'name':'tool','version':'1.1','purl':None},{'name':'tool','version':'1.1','purl':'pkg:test'}]}
        (prior/'factory-image-review.json').write_text(json.dumps({'current':True,'kind':'pending','image':'docker.io/kyuz0/ai-sandbox-factory-gateway@sha256:'+'b'*64}))
        (prior/'factory-image.cdx.json').write_text(json.dumps(value))
        (candidate/'gateway.cdx.json').write_text(json.dumps(value))
        index={'images':{'same-recipe':{'linux/amd64':'new-digest'}}}
        (self.root/maintenance.INDEX).parent.mkdir(parents=True)
        (self.root/maintenance.INDEX).write_text(json.dumps(index))
        self.api.return_value={'content':base64.b64encode(json.dumps(index).encode()).decode()}
        report={'review_head':OLD,'components':{'pi':{'changed':True}}}
        self.assertFalse(maintenance.meaningful_update(report,directory))
        value['components'][0]['version']='1.2'
        (candidate/'gateway.cdx.json').write_text(json.dumps(value))
        self.assertTrue(maintenance.meaningful_update(report,directory))

    def test_rerun_preparation_uses_an_independent_attempt_branch(self):
        report=self.root/'updates.json';report.write_text('{}')
        self.command.side_effect=None;self.command.return_value=''
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}):
            maintenance.prepare(report,test=True)
        branches=[call.args[-1] for call in self.command.call_args_list if call.args[:3]==('git','checkout','-B')]
        self.assertEqual(branches,[maintenance.BRANCH+'-test-123-2'])

    def test_promotion_has_git_authentication_and_runs_only_after_verification(self):
        import yaml
        workflow=yaml.safe_load((ROOT/'.github/workflows/maintenance.yml').read_text())
        jobs=workflow['jobs']
        self.assertIn('verify',jobs['review']['needs'])
        # Existing image scans deliberately fail on actionable findings. Every
        # replacement stage must proceed from its own successful prerequisite,
        # rather than inheriting a default success() from that failed ancestor.
        for name in ('candidates','index','verify','review'):
            self.assertIn('always()',jobs[name]['if'])
            self.assertIn("== 'success'",jobs[name]['if'])
        self.assertEqual(jobs['review']['permissions']['contents'],'write')
        self.assertEqual(jobs['review']['permissions']['statuses'],'write')
        self.assertEqual(jobs['plan']['permissions']['statuses'],'read')
        maintenance.pull_request(SHA,STAGING,RUN)
        calls=[call.args for call in self.command.call_args_list]
        auth=calls.index(('gh','auth','setup-git'))
        push=next(i for i,call in enumerate(calls) if call[:2]==('git','push'))
        self.assertLess(auth,push)

    def test_real_git_comparison_ignores_only_the_apt_cache_marker(self):
        def git(*args):
            return REAL_RUN(['git',*args],cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()
        git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        recipe=self.root/'ai_sandbox_factory/containers/inference/Dockerfile';recipe.parent.mkdir(parents=True)
        recipe.write_text('FROM debian:trixie\nARG FACTORY_APT_REFRESH=2026-11-20\nRUN true\n')
        index=self.root/maintenance.INDEX;index.write_text('{"images":{}}')
        git('add','.');git('commit','-qm','Initial fixture');prior=git('rev-parse','HEAD')
        recipe.write_text(recipe.read_text().replace('2026-11-20','2026-11-27'))
        index.write_text('{"images":{"different-digest":{}}}')
        git('add','.');git('commit','-qm','Refresh cache')
        directory=self.root/'inventories';old=directory/'image-scan-pending';new=directory/'image-gateway'
        old.mkdir(parents=True);new.mkdir()
        (old/'factory-image-review.json').write_text(json.dumps({'current':True,'kind':'pending','image':'docker.io/kyuz0/ai-sandbox-factory-gateway@sha256:'+'a'*64}))
        inventory=json.dumps({'components':[{'name':'fixture','version':'1'}]})
        (old/'factory-image.cdx.json').write_text(inventory);(new/'gateway.cdx.json').write_text(inventory)
        self.diff.side_effect=REAL_RUN
        report={'review_head':prior,'components':{'bases':{'changed':False}}}
        self.assertFalse(maintenance.meaningful_update(report,directory))
        host=self.root/'ai_sandbox_factory/updates.py';host.write_text('HOST_ONLY_CHANGE = True\n')
        git('add',str(host));git('commit','-qm','Host-only change')
        self.assertFalse(maintenance.meaningful_update(report,directory))
        recipe.write_text(recipe.read_text().replace('RUN true','RUN false'))
        git('add',str(recipe));git('commit','-qm','Change runtime')
        self.assertTrue(maintenance.meaningful_update(report,directory))

    def test_shared_stable_digest_is_also_in_the_pending_proposal(self):
        directory=self.root/'inventories';old=directory/'image-scan-shared';new=directory/'image-gateway'
        old.mkdir(parents=True);new.mkdir()
        reference='docker.io/kyuz0/ai-sandbox-factory-gateway@sha256:'+'a'*64
        (old/'factory-image-review.json').write_text(json.dumps({'current':True,'kind':'stable','image':reference}))
        inventory=json.dumps({'components':[{'name':'fixture','version':'1'}]})
        (old/'factory-image.cdx.json').write_text(inventory);(new/'gateway.cdx.json').write_text(inventory)
        index={'images':{'recipe':{'linux/amd64':reference}}}
        self.api.return_value={'content':base64.b64encode(json.dumps(index).encode()).decode()}
        report={'review_head':OLD,'components':{'pi':{'changed':True}}}
        self.assertFalse(maintenance.meaningful_update(report,directory))

    def test_committed_failure_report_keeps_cause_and_links_full_artifact_without_stack_frames(self):
        report=self.root/'version-report.json'
        original={'python':{'status':'failed','changed':False,'error':'pip-compile exited 1\nTraceback (most recent call last):\n  PRIVATE_STACK_FRAME\nImportError: missing compiler API'}}
        report.write_text(json.dumps(original))
        public=maintenance.published_components(report,'123')
        self.assertNotIn('PRIVATE_STACK_FRAME',json.dumps(public))
        self.assertIn('pip-compile exited 1',public['python']['error'])
        self.assertIn('ImportError: missing compiler API',public['python']['error'])
        self.assertEqual(public['python']['diagnostic_url'],RUN)
        self.assertEqual(json.loads(report.read_text()),original)


class CandidateIndex(unittest.TestCase):
    def test_source_drift_missing_role_and_wrong_platform_fail_before_pulling(self):
        config=publisher.public_config()
        images={publisher.image_sources.recipe(config,role):{'linux/amd64':publisher.REPOSITORY+role+'@sha256:'+'a'*64} for role in publisher.SMOKE}
        with tempfile.TemporaryDirectory() as temporary:
            file=Path(temporary)/'images.json'
            file.write_text(json.dumps({'version':1,'images':images}))
            self.assertEqual(set(publisher.validate_index(file)),set(publisher.SMOKE))
            key=publisher.image_sources.recipe(config,'gateway')
            original=images[key]
            for wrong in ({'linux/arm64':original['linux/amd64']},{'linux/amd64':'docker.io/kyuz0/ai-sandbox-factory-aws@sha256:'+'a'*64},{}):
                images[key]=wrong;file.write_text(json.dumps({'version':1,'images':images}))
                with self.subTest(wrong=wrong),self.assertRaisesRegex(ValueError,'gateway'):
                    publisher.validate_index(file)
            del images[key];file.write_text(json.dumps({'version':1,'images':images}))
            with self.assertRaisesRegex(ValueError,'Rebuild'):
                publisher.validate_index(file)

    def test_wrong_installed_version_fails_even_when_the_process_exits_zero(self):
        result=subprocess.CompletedProcess([],0,'Claude Code 0.0.1','')
        with patch.object(publisher.sandbox,'run',return_value=result),self.assertRaisesRegex(ValueError,'expected'):
            publisher.smoke('controller-claude','sha256:'+'a'*64)


class CadenceAndRetention(unittest.TestCase):
    def test_daily_success_does_not_masquerade_as_a_successful_weekly_cycle(self):
        now=datetime.now(timezone.utc)
        run={'id':123,'status':'completed','conclusion':'success','created_at':(now-timedelta(days=10)).isoformat(), 'updated_at':now.isoformat()}
        def api(method,path,*args):
            if '/pulls?' in path:return []
            if path.endswith('/jobs?per_page=100'):return {'jobs':[{'name':'Prepare verified weekly update','conclusion':'skipped'}]}
            return {'workflow_runs':[run]}
        with patch.object(maintenance,'api',side_effect=api),self.assertRaisesRegex(ValueError,'No successful weekly'):
            maintenance.overdue()

    def test_initial_week_is_allowed_before_first_scheduled_weekly_cycle(self):
        now=datetime.now(timezone.utc)
        run={'id':123,'status':'completed','conclusion':'success','created_at':now.isoformat(),'updated_at':now.isoformat()}
        def api(method,path,*args):
            if '/pulls?' in path:return []
            if '/jobs?' in path:return {'jobs':[]}
            return {'workflow_runs':[run]}
        with patch.object(maintenance,'api',side_effect=api):maintenance.overdue()

    def test_retention_keeps_images_from_every_release_and_never_deletes(self):
        refs=['docker.io/kyuz0/ai-sandbox-factory-gateway@sha256:'+v*64 for v in ('a','b')]
        results=[subprocess.CompletedProcess([],0,json.dumps({'version':1,'images':{'old':{'linux/amd64':ref}}}),'') for ref in refs]
        with patch.object(maintenance,'command',return_value='v2026.1.1.1\nv2026.2.1.1'),patch.object(maintenance.subprocess,'run',side_effect=results) as run,patch('builtins.print') as output:
            maintenance.retention()
        report=json.loads(output.call_args.args[0])
        self.assertEqual(set(report['released_images_to_keep']),set(refs))
        self.assertFalse(report['registry_deletion'])
        self.assertTrue(all(call.args[0][:2]==['git','show'] for call in run.call_args_list))

    def test_corrupt_release_inventory_prevents_retention_decisions(self):
        with patch.object(maintenance,'command',return_value='v2026.1.1.1'),patch.object(maintenance.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"version":0,"images":{}}','')),self.assertRaisesRegex(ValueError,'must not delete'):
            maintenance.retention()


class ScanSummary(unittest.TestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output=self.enterContext(patch.object(maintenance,'output'))

    def review(self,name,role,kind,blocked):
        folder=self.folder/name;folder.mkdir()
        (folder/'factory-image-review.json').write_text(json.dumps({'image':'docker.io/kyuz0/ai-sandbox-factory-'+role+'@sha256:'+'a'*64,'kind':kind,'current':kind!='previous','requires_review':[{'id':'CVE-fixture'}] if blocked else []}))

    def test_verified_pending_fix_prevents_rebuilding_the_same_proposal_each_day(self):
        self.review('stable','inference','stable',True)
        self.review('pending','inference','pending',False)
        self.review('previous','ssh','previous',True)
        maintenance.scan_summary(self.folder)
        self.output.assert_called_once_with(urgent='false')

    def test_new_finding_in_pending_or_another_current_role_starts_an_update(self):
        for role,kind in [('inference','pending'),('ssh','stable')]:
            with self.subTest(role=role,kind=kind):
                for path in self.folder.glob('*/*'):path.unlink()
                for path in self.folder.iterdir():path.rmdir()
                self.review('stable','inference','stable',True)
                self.review('pending','inference','pending',False)
                self.review('new',role,kind,True)
                self.output.reset_mock()
                maintenance.scan_summary(self.folder)
                self.output.assert_called_once_with(urgent='true')

    def test_missing_scan_output_is_an_error_not_a_clean_report(self):
        with self.assertRaisesRegex(ValueError,'No completed image scan'):
            maintenance.scan_summary(self.folder)
        self.output.assert_not_called()


if __name__=='__main__':unittest.main()
