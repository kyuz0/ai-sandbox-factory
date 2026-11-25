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
                assert profile_edit.PATH in inside('cat','/home/node/.pi/agent/AGENTS.md').stdout
                assert inside('sh','-c','echo wrong >> '+profile_runtime.PATH+'/profile/profile.md',check=False).returncode
                assert (project/'AGENTS.md').read_text()=='USER_PROJECT_UNCHANGED\n'
                sandbox.delete(current,state)
                assert (destination/'agent-proof.txt').exists(),'sandbox deletion erased the user-owned profile folder'
                print('PASS cached-image editable mount: agent and host writes, shared validation/context, read-only applied record, unchanged project and retained host profile',flush=True)
            finally:
                if (state/'config.json').exists():
                    sandbox.delete(json.loads((state/'config.json').read_text()),state)
                if destination.exists():shutil.rmtree(destination)


if __name__=='__main__':main()
