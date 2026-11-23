import argparse,os,sys,tempfile,subprocess,uuid
from pathlib import Path
from types import SimpleNamespace
project = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(project))
parser=argparse.ArgumentParser(description='Build two synthetic library skills and check shipped Pi and Pendant registration.')
parser.add_argument('image', help='Existing Factory development image')
parser.add_argument('--local-model-manager', help='Optional dependency checkout to mount for pre-build integration')
args=parser.parse_args()
from ai_sandbox_factory import skill_library,sandbox_skills,sandbox
with tempfile.TemporaryDirectory(prefix='factory-library-runtime-') as temp:
 root=Path(temp);os.environ['SANDBOX_REGISTRY_DIR']=str(root/'library')
 source=root/'source';source.mkdir()
 for name in ('library-first','library-second'):
  d=source/name;d.mkdir();(d/'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic integration skill {name}.\n---\nUse this synthetic fixture only.\n')
 rows=skill_library.discover(str(source),root/'fetched');skill_library.install(rows)
 skills=skill_library.snapshot([r['id'] for r in rows],root/'state')
 for r in rows:skill_library.remove(r['id'])
 def run(command):
  command=list(map(str,command));command[2:2]=['--network=none']
  return subprocess.run(command,check=True)
 image='localhost/factory-skill-library-check:'+uuid.uuid4().hex[:12]
 try:
  sandbox_skills.build({'extra_skills':skills,'engine':'podman'},image,SimpleNamespace(run=run,tree_digest=sandbox.tree_digest),base_image=args.image)
  command=['podman','run','--rm','--network','none','--security-opt','label=disable','--entrypoint','node','-v',str(project/'tests/check_radiance_discovery.mjs')+':/tmp/check.mjs:ro']
  if args.local_model_manager: command += ['-v',str(Path(args.local_model_manager).resolve())+':/opt/pi-local-model-manager:ro']
  subprocess.run(command+[image,'/tmp/check.mjs','--library-skills'],check=True)
 finally:
  subprocess.run(['podman','image','rm',image],check=False)
