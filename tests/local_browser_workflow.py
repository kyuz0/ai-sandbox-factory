#!/usr/bin/env python3
"""Run localhost regression through shipped Pi in disposable containers."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_sandbox_factory import sandbox
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--image',required=True)
p.add_argument('--packages',type=Path,help='Optional dependency checkout to test before pinning')
p.add_argument('--editor-env',action='store_true',help='Exercise inherited browser editor Node settings and scheduling')
p.add_argument('--goals',action='store_true',help='Also exercise native Pi goals without review')
a=p.parse_args()
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='factory-local-browser-') as directory:
 state=Path(directory)
 (state/'allowlist.txt').write_text('fixture.test:80\n')
 config={'common':{'path':str(root/'ai_sandbox_factory/containers/dev')},'options':{'browser':True,'goals':a.goals,'scheduling':a.editor_env,'compaction_recall':'off'}}
 sandbox.prepare_options(config,state)
 # Mounts are read-only; UID mapping preserves the current user's private fixtures.
 for client in ('pi',):
  name='factory-local-check-'+uuid.uuid4().hex[:10]
  mounts=[(root/'tests','/fixture'),(root/'ai_sandbox_factory/containers/editor/watch-fallback.cjs','/fixture-watch.cjs'),(state/'runtime-config','/opt/pi-security/config'),(state/'options.json','/opt/sandbox/options.json'),(root/'ai_sandbox_factory/containers/dev/bootstrap.py','/usr/local/lib/sandbox-bootstrap.py')]
  if a.packages:
   mounts.append((a.packages/'packages','/opt/pi-security/packages'))
   # Browser imports are relative to the installed Betterwright package tree.
   for filename in ('browser.mjs','network.mjs'):
    mounts.append((a.packages/'packages/pi-sandbox/src/browser'/filename,'/opt/betterwright/'+filename))
  command=['podman','run','--rm','--name',name,'--network','none','--userns=keep-id','--security-opt','label=disable','--security-opt','seccomp=unconfined','--entrypoint','node','-e','TMPDIR=/var/tmp/pi','-e','SANDBOX_NETWORK_MODE=allowlist']
  for source,target in mounts:command+=['-v',f'{source}:{target}:ro']
  try:subprocess.run(command+[a.image,'/fixture/local_browser_workflow.mjs',client]+(['--goals'] if a.goals else [])+(['--editor-env'] if a.editor_env else []),check=True)
  finally:subprocess.run(['podman','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
