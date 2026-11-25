"""Opt-in real Opus profile-authoring check using a disposable Claude sandbox."""
import argparse
import copy
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from uuid import uuid4

from ai_sandbox_factory import profile_edit, profile_runtime, profile_services, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, preset


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True,help='Use the host Claude subscription through its isolated companion')
    parser.add_argument('--output',type=Path,required=True,help='Private directory for real model output; never commit it')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='factory-profile-opus-') as temporary:
        root=Path(temporary)
        service=Service(root/'state')
        project=root/'project';project.mkdir()
        (project/'AGENTS.md').write_text('USER_PROJECT_RULES_UNCHANGED\n')
        name='profile-opus-'+uuid4().hex[:8]
        state=service.state/name
        source=root/'editable-profile'
        draft=preset('base',copy.deepcopy(DEFAULT_SETTINGS))
        draft.update(name=name,primary_agent='claude',primary_auth='copy',engine='podman',network='allowlist',
                     workspace=str(project),open_editor=False,model_providers={},local_model_ports=None,allowlist='')
        draft['options']['browser']=False
        draft['options']['native_approval']='automatic'
        def log(line):print(line,flush=True)
        def inside(*args,check=True):
            return sandbox.run(['podman','exec',sandbox.names(service.config(name))[0],*args],capture=True,
                               check=check,stderr=subprocess.PIPE)
        try:
            service.create(draft,None,log,cancellation=Creation())
            service.run(['profile-fork',name,'--directory',str(source)],log)
            # The agent must diagnose a real validator error, not just be told
            # that its own valid edits passed.
            (source/'profile.json').write_text('{"name":"base","version":1,}\n')
            origin=profile_edit.PATH+'/.git/fixture-origin'
            inside('git','init','--bare',origin)
            prompt=("This is an authorized profile-editing test in a disposable AI Sandbox Factory sandbox. "
                "Only edit /opt/sandbox/profile, never /workspace or the read-only applied record. "
                "Read the bundled authoring guide in your Factory instructions. First run its validator; "
                "the editable profile.json has an accidental JSON error. Repair it using the applied profile "
                "as a reference, preserving coding-agent compatibility. Then add figlet and postgresql-client "
                "to the coding-agent Dockerfile, and add one PostgreSQL service named reporting using the "
                "bundled two-host example's reporting service and its SSH-capable Dockerfile. "
                "Explain this host in profile.md. Validate the finished profile for Claude. "
                "I also authorize Git tracking for this profile. Use Example Maintainer <maintainer@example.test> "
                "as this repository’s Git author. Initialize its repository on branch main, "
                "commit these changes and push to the supplied disposable local origin at " + origin + ". "
                "Record my ongoing instruction in profile.md: maintain this profile in its approved Git origin, "
                "preserving user edits. No other remote or publication is authorized. "
                "Do not apply it, install packages in the current container or change permissions/network settings. "
                "Finish by telling the user what changed, the exact "
                "Factory UI path to review and apply it, and what application will interrupt and preserve.")
            def ask_opus(message,stem):
                output=args.output/(stem+'.jsonl')
                errors=args.output/(stem+'.stderr')
                dev=sandbox.names(service.config(name))[0]
                with output.open('w') as stdout,errors.open('w') as stderr:
                    process=subprocess.Popen(['podman','exec','-i','-w','/workspace',dev,'/usr/local/bin/claude',
                        '--print','--verbose','--output-format','stream-json','--model','opus','--effort','medium',message],
                        stdout=stdout,stderr=stderr,stdin=subprocess.DEVNULL)
                    code=process.wait()
                events=[json.loads(line) for line in output.read_text().splitlines() if line.startswith('{')]
                terminal=next((event for event in reversed(events) if event.get('type')=='result'),{})
                assert code==0 and terminal and not terminal.get('is_error'), f'Native Opus failed (exit {code}); inspect private output in {args.output}'
                result=terminal.get('result','')
                (args.output/(stem+'-next-steps.txt')).write_text(result)
                return events,result
            events,result=ask_opus(prompt,'opus-profile-edit')
            calls=[part for event in events for part in event.get('message',{}).get('content',[]) if isinstance(part,dict) and part.get('type')=='tool_use']
            serialized=json.dumps(calls)
            assert 'README.md' in serialized and 'validate.py' in serialized,'Opus did not read the guide and invoke validation'
            assert 'Review changes' in result and 'Apply profile changes' in result,result
            assert any(word in result.lower() for word in ('restart','replac','interrupt','stop')),result
            assert 'figlet' in (source/'Dockerfile').read_text()
            validated=inside('python3',profile_runtime.PATH+'/authoring/validate.py',profile_edit.PATH,'--agent','claude')
            assert 'Static checks passed' in validated.stdout
            reviewed=service.review_profile(name)
            assert reviewed['added_services']==['reporting'],reviewed['added_services']
            assert any('Dockerfile' in change for change in reviewed['changes'])
            assert (project/'AGENTS.md').read_text()=='USER_PROJECT_RULES_UNCHANGED\n'
            def git(*args):
                return inside('git','-C',profile_edit.PATH,*args).stdout.strip()
            commit=git('rev-parse','HEAD')
            assert inside('git','--git-dir',origin,'rev-parse','refs/heads/main').stdout.strip()==commit
            assert git('remote','get-url','origin')==origin
            assert not git('status','--porcelain'),'Opus left the approved edits uncommitted'
            original=service.config(name)[profile_edit.EDIT].copy()
            expected_instructions=(source/'profile.md').read_text().rstrip()
            service.run(['profile-apply',name,'--revision',reviewed['revision']],log)
            assert inside('sh','-c','command -v figlet').returncode==0
            connections=json.loads(inside('cat',profile_services.CLIENT+'/connections.json').stdout)['reporting']
            command='psql -U '+shlex.quote(connections['user'])+' -d '+shlex.quote(connections['database'])+' -Atc '+shlex.quote('SELECT 42')
            assert inside('ssh','-F',profile_services.CLIENT+'/config',connections['host'],command).stdout.strip()=='42'
            service.run(['down',name],log)
            service.run(['up',name],log)
            context=inside('cat','/run/primary-runtime/context.md').stdout
            assert original['origin_digest'] in context
            assert service.config(name)['profile']['digest'] in context
            assert expected_instructions in context,'The saved profile instructions were missing from the restarted agent context'
            assert git('rev-parse','HEAD')==commit
            restarted,answer=ask_opus('This is a new session after the profile was applied and the sandbox restarted. '
                'Read your Factory profile context and inspect the editable profile Git status and origin. '
                'Explain the original fork, applied record, editable source, existing maintenance instruction and '
                'the exact UI steps for any future changes to be applied. Do not change files or push.',
                'opus-profile-restart')
            assert profile_edit.PATH in answer and profile_runtime.PATH in answer,answer
            assert 'Review changes' in answer and 'Apply profile changes' in answer,answer
            assert git('rev-parse','HEAD')==commit
            print('PASS live native Opus: repaired invalid JSON, edited package/service recipes, validated, committed/pushed to the authorized local origin, explained Apply, and understood original/applied/editable profile maintenance in a fresh session after restart; package and SQL checks passed',flush=True)

        finally:
            if (state/'config.json').exists():
                current=service.config(name)
                wanted=profile_services.resources(current)
                service.run(['delete',name],log)
                for kind,names in wanted.items():
                    for resource in names:
                        assert sandbox.run(['podman',kind,'exists',resource],capture=True,check=False).returncode==1,resource


if __name__=='__main__':main()
