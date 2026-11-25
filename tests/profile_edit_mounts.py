"""Check editable host mounts with a cached image, including Windows-backed WSL2 paths."""
import argparse
import json
import shutil
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import activity, profile_edit, profile_runtime, sandbox


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--proxy-image',required=True)
    parser.add_argument('--profile-root',type=Path,required=True,help='Writable parent folder on the host filesystem being checked')
    parser.add_argument('--git-maintenance',action='store_true',help='Also check Git commits/local-origin pushes and restart persistence; use a filesystem supporting Git lock-file metadata')
    args=parser.parse_args()
    args.profile_root.mkdir(parents=True,exist_ok=True)
    destination=args.profile_root/('factory-profile-edit-'+uuid4().hex[:8])
    with tempfile.TemporaryDirectory(prefix='factory-profile-mount-',dir=Path.home()) as temporary:
        root=Path(temporary)
        name='profile-mount-'+uuid4().hex[:8]
        state=root/'state'/name;state.mkdir(parents=True)
        project=root/'project';project.mkdir()
        (project/'AGENTS.md').write_text('USER_PROJECT_UNCHANGED\n')
        config={'name':name,'engine':'podman','uid':1000,'gid':1000,
                'workspace':str(project),'primary_agent':'pi','network':'allowlist','memory':None,
                'options':{'browser':False},'subagents':False,'extra_skills':[],'extra_folders':[],
                'profile':sandbox.snapshot_profile(sandbox.load_profile('base'),state),
                'common':sandbox.snapshot_profile(sandbox.common_profile(),state)}
        (state/'allowlist.txt').write_text('')
        (state/'squid.conf').write_text(sandbox.network_policy(config,[]))
        activity.save_config(state,config)
        with patch.object(sandbox,'image_names',return_value=(args.image,args.proxy_image)):
            try:
                with profile_runtime.recording(config,state):pass
                profile_runtime.select(config,state)
                sandbox.up(config,state,prepare_sign_in=False)
                profile_edit.fork(config,state,destination)
                current=json.loads((state/'config.json').read_text())
                dev=sandbox.names(current)[0]
                def inside(*command,check=True):
                    return sandbox.run(['podman','exec',dev,*command],capture=True,check=check)
                inside('python3','-c',"from pathlib import Path; Path('/opt/sandbox/profile/agent-proof.txt').write_text('agent wrote this')")
                assert (destination/'agent-proof.txt').read_text()=='agent wrote this'
                (destination/'agent-proof.txt').write_text('host can edit the same file')
                assert inside('cat','/opt/sandbox/profile/agent-proof.txt').stdout=='host can edit the same file'
                assert 'Static checks passed' in inside('python3',profile_runtime.PATH+'/authoring/validate.py',profile_edit.PATH,'--agent','pi').stdout
                initial=inside('cat','/home/node/.pi/agent/AGENTS.md').stdout
                assert 'The writable profile source is '+profile_edit.PATH in initial, 'The image did not load the editable profile instructions; use a current Pi image with the matching Factory bootstrap'
                assert current[profile_edit.EDIT]['origin_digest'] in initial, ('Missing original profile revision',initial)
                assert inside('sh','-c','echo wrong >> '+profile_runtime.PATH+'/profile/profile.md',check=False).returncode
                assert (project/'AGENTS.md').read_text()=='USER_PROJECT_UNCHANGED\n'
                if args.git_maintenance:
                    # A local bare origin exercises real Git writes/pushes without
                    # external accounts or granting the fixture another host mount.
                    def git(*args):
                        return inside('git','-C',profile_edit.PATH,*args).stdout.strip()
                    git('init','-b','main')
                    git('config','user.name','Example Profile Maintainer')
                    git('config','user.email','maintainer@example.test')
                    remote=profile_edit.PATH+'/.git/fixture-origin'
                    inside('git','init','--bare',remote)
                    git('remote','add','origin',remote)
                    git('add','.')
                    git('commit','-m','Track this editable profile')
                    git('push','-u','origin','main')
                    first=git('rev-parse','HEAD')
                    assert inside('git','--git-dir',remote,'rev-parse','refs/heads/main').stdout.strip()==first
                    with (destination/'profile.md').open('a') as stream:
                        stream.write('\nMaintain this profile in its approved Git origin.\n')
                    git('add','profile.md')
                    git('commit','-m','Document profile maintenance')
                    git('push')
                    latest=git('rev-parse','HEAD')
                    assert latest!=first
                    (destination/'uncommitted.txt').write_text('keep this edit across restart')
                    reviewed=profile_edit.review(current,state)
                    assert not (Path(reviewed['profile']['path'])/'.git').exists()
                    sandbox.down(current)
                    sandbox.up(current,state,prepare_sign_in=False)
                    context=inside('cat','/home/node/.pi/agent/AGENTS.md').stdout
                    assert current[profile_edit.EDIT]['origin_digest'] in context, ('Missing original profile revision after restart',context)
                    assert 'forked from '+repr(current[profile_edit.EDIT]['origin_name']) in context
                    assert 'ask whether to track this folder with Git' in context, context
                    assert 'whether that is one-time or ongoing' in context, context
                    assert 'Follow standing maintenance instructions without asking again' in context, context
                    assert 'inspect this folder’s Git status, log and remotes' in context
                    assert git('rev-parse','HEAD')==latest
                    assert git('remote','get-url','origin')==remote
                    assert 'uncommitted.txt' in git('status','--short')
                    assert inside('git','--git-dir',remote,'rev-parse','refs/heads/main').stdout.strip()==latest
                    print('PASS Git profile maintenance: commits, local-origin pushes, history/origin and uncommitted edits survive restart; original/applied/editable context remains available',flush=True)
                sandbox.delete(current,state)
                assert (destination/'agent-proof.txt').exists(),'sandbox deletion erased the user-owned profile folder'
                print('PASS cached-image editable mount: agent and host writes, shared validation/context, read-only applied record, unchanged project and retained host profile',flush=True)
            finally:
                if (state/'config.json').exists():
                    sandbox.delete(json.loads((state/'config.json').read_text()),state)
                if destination.exists():shutil.rmtree(destination)


if __name__=='__main__':main()
