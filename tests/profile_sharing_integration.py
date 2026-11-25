"""Shared editable files with separate images, homes and application data."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import profile_edit, profile_services, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, copy_draft, copy_for_agent, preset


def main():
    with tempfile.TemporaryDirectory(prefix='factory-sharing-') as temporary:
        root=Path(temporary)
        service=Service(root/'state')
        prefix='profile-sharing-'+uuid4().hex[:8]
        names=[prefix+'-source',prefix+'-peer',prefix+'-copy']
        editable=root/'editable-profile'
        configurations={}
        def log(line):print(line,flush=True)
        def inside(name,*args,check=True):
            config=service.config(name)
            return sandbox.run(['podman','exec',sandbox.names(config)[0],*args],capture=True,
                               check=check,stderr=subprocess.PIPE)
        def sql(name,query):
            return inside(name,'ssh','-F','/run/profile-services/config','database.profile.internal',
                          'psql -U app -d app -Atc '+shlex.quote(query)).stdout.strip()
        def workspace(name):
            path=root/name
            path.mkdir()
            (path/'AGENTS.md').write_text('USER_PROJECT_INSTRUCTIONS\n')
            return str(path)
        def shared_copy(source,name,agent):
            config=service.config(source)
            draft=copy_for_agent(copy_draft({'name':source,'config':config},service.state),agent)
            draft.update(name=name,workspace=workspace(name),open_editor=False,primary_auth='login',
                         model_providers={},share_editable_profile=True)
            service.create(draft,None,log,cancellation=Creation())
            configurations[name]=service.config(name)
        with patch.dict(os.environ,SANDBOX_REGISTRY_DIR=str(root/'library')):
            try:
                source,peer,descendant=names
                draft=preset('developer-postgres',copy.deepcopy(DEFAULT_SETTINGS))
                draft.update(name=source,workspace=workspace(source),engine='podman',network='allowlist',
                             model_providers={},local_model_ports=None,allowlist='',open_editor=False,primary_auth='login')
                draft['options']['browser']=False
                service.create(draft,None,log,cancellation=Creation())
                service.run(['profile-fork',source,'--directory',str(editable)],log)
                configurations[source]=service.config(source)
                applied=configurations[source]['profile']['digest']
                manifest=(editable/'profile.json').read_text()
                (editable/'profile.json').write_text('{invalid pending edit\n')
                shared_copy(source,peer,'codex')
                assert inside(peer,'cat',profile_edit.PATH+'/profile.json').stdout=='{invalid pending edit\n'
                assert service.config(peer)['profile']['digest']==applied
                assert 'Other sandboxes can share this editable folder' in inside(peer,'cat','/run/primary-runtime/context.md').stdout
                (editable/'profile.json').write_text(manifest)
                shared_copy(peer,descendant,'pi')
                records={record['name']:record for record in service.inventory()}
                assert set(records[source]['shared_profiles'])=={peer,descendant}
                for name in names:
                    config=service.config(name)
                    assert config[profile_edit.EDIT]['path']==str(editable)
                    assert config[profile_edit.EDIT]['origin_digest']==configurations[source][profile_edit.EDIT]['origin_digest']
                    assert Path(config['profile']['path']).is_relative_to(service.state/name)
                    inside(name,'sh','-c','printf '+shlex.quote(name)+' > /home/node/sharing-private-marker')
                    sql(name,"CREATE TABLE sharing_marker (value text); INSERT INTO sharing_marker VALUES ('"+name+"')")
                passwords=[json.loads((service.state/name/'profile-services/client/connections.json').read_text())['database']['password'] for name in names]
                assert len(set(passwords))==len(names)
                assert len({profile_services.data_volume(service.config(name),'database','data') for name in names})==len(names)
                log('PASS shared creation: Pi/Codex and copy-of-copy share one editable folder; invalid pending edits stay unapplied; homes, database volumes and credentials are separate')
                def git(name,*args):return inside(name,'git','-C',profile_edit.PATH,*args).stdout.strip()
                git(source,'init','-b','main')
                git(source,'config','user.name','Example Profile Maintainer')
                git(source,'config','user.email','maintainer@example.test')
                git(source,'add','.')
                git(source,'commit','-m','Track the shared profile')
                head=git(source,'rev-parse','HEAD')
                assert all(git(name,'rev-parse','HEAD')==head for name in names)
                inside(peer,'python3','-c','from pathlib import Path; f=Path('+repr(profile_edit.PATH+'/Dockerfile')+'); '
                       'f.write_text(f.read_text()+"\\nUSER root\\nRUN printf SHARED_PROFILE_APPLIED > /opt/shared-profile-marker\\nUSER node\\n")')
                assert all('Dockerfile' in git(name,'status','--short') for name in names)
                before={name:sandbox.inspect(service.config(name),sandbox.names(service.config(name))[0])['Id'] for name in names}
                review=service.review_profile(peer)
                assert set(review['shared_with'])=={source,descendant}
                service.run(['profile-apply',peer,'--revision',review['revision']],log)
                configurations[peer]=service.config(peer)
                assert inside(peer,'cat','/opt/shared-profile-marker').stdout=='SHARED_PROFILE_APPLIED'
                for name in (source,descendant):
                    assert inside(name,'test','-e','/opt/shared-profile-marker',check=False).returncode==1
                    assert sandbox.inspect(service.config(name),sandbox.names(service.config(name))[0])['Id']==before[name]
                for name in names:
                    assert sql(name,'SELECT value FROM sharing_marker')==name
                    assert inside(name,'cat','/home/node/sharing-private-marker').stdout==name
                log('PASS shared edit/apply: same Git history and pending files; applying to one sandbox changes only its image and preserves every sandbox’s private home and database')
                service.run(['delete',source],log)
                assert editable.is_dir() and git(peer,'rev-parse','HEAD')==head
                service.run(['down',peer],log)
                service.run(['up',peer],log)
                assert inside(peer,'cat','/opt/shared-profile-marker').stdout=='SHARED_PROFILE_APPLIED'
                review=service.review_profile(descendant)
                assert review['shared_with']==[peer]
                assert review['changes'] and 'Dockerfile' in '\n'.join(review['changes'])
                service.run(['profile-apply',descendant,'--revision',review['revision']],log)
                configurations[descendant]=service.config(descendant)
                assert inside(descendant,'cat','/opt/shared-profile-marker').stdout=='SHARED_PROFILE_APPLIED'
                assert git(descendant,'rev-parse','HEAD')==head
                assert sql(peer,'SELECT value FROM sharing_marker')==peer
                assert sql(descendant,'SELECT value FROM sharing_marker')==descendant
                log('PASS source deletion and later Apply: shared files/Git survive; peers restart independently and each applies its own diff against its own image')
            finally:
                for name in reversed(names):
                    if (service.state/name/'config.json').exists():
                        configurations[name]=service.config(name)
                        service.run(['delete',name],log)
                    label='label='+sandbox.LABEL+'='+name
                    for command in (['ps','-aq'],['volume','ls','-q'],['network','ls','-q']):
                        result=sandbox.run(['podman',*command,'--filter',label],capture=True,stderr=subprocess.PIPE)
                        assert not result.stdout.strip(),(name,command,result.stdout)
                    project=root/name/'AGENTS.md'
                    if project.exists():assert project.read_text()=='USER_PROJECT_INSTRUCTIONS\n'
                if editable.exists():assert (editable/'Dockerfile').is_file()
                log('PASS disposable sharing containers, volumes and networks removed; project instructions and the host profile folder were preserved')


if __name__=='__main__':main()
