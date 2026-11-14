"""Run in a disposable Factory container; exercise the production editor supervisor."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

fake = Path('/tmp/fake-editor')
fake.write_text('''#!/usr/bin/env python3
import os,socket,sys,time
from pathlib import Path
s=socket.socket(socket.AF_UNIX)
s.bind(sys.argv[sys.argv.index('--socket')+1]);s.listen()
for wave in range(10):
 while not Path('/tmp/start-'+str(wave)).exists():time.sleep(.01)
 for _ in range(100):
  child=os.fork()
  if child==0:
   if os.fork()==0:
    time.sleep(.02);os._exit(0)
   os._exit(0)
  os.waitpid(child,0)
 Path('/tmp/done-'+str(wave)).touch()
while not Path('/tmp/editor-exit').exists():time.sleep(.01)
''')
fake.chmod(0o755)
supervisor = subprocess.Popen(['python3','-u',sys.argv[1]],stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,text=True)
try:
    supervisor.stdin.write(json.dumps({'binary':str(fake),'cwd':'/workspace',
        'authority':'127.0.0.1:12345','password_hash':'','watch_fallback':''})+'\n')
    supervisor.stdin.flush()
    with selectors.DefaultSelector() as ready:
        ready.register(supervisor.stdout,selectors.EVENT_READ)
        assert ready.select(60), 'Supervisor did not start'
    assert supervisor.stdout.readline() == 'ready\n'
    def zombies():
        result=[]
        for path in Path('/proc').glob('[0-9]*/status'):
            try:
                values=dict(line.split(':',1) for line in path.read_text().splitlines() if ':' in line)
                if int(values['PPid'])==supervisor.pid and values['State'].strip().startswith('Z'):
                    result.append(path.parent.name)
            except (OSError,KeyError):pass
        return result
    baseline='--expect-leak' in sys.argv
    for wave in range(1 if baseline else 10):
        Path('/tmp/start-'+str(wave)).touch()
        deadline=time.monotonic()+30
        while not Path('/tmp/done-'+str(wave)).exists():
            assert time.monotonic()<deadline,'Orphan workload stalled'
            time.sleep(.05)
        time.sleep(.5)
        leaked=zombies()
        if baseline:
            assert len(leaked)==100,leaked
            print('PASS reproduced 100 unreaped orphan helpers under old supervisor',flush=True)
        else:
            deadline=time.monotonic()+5
            while leaked and time.monotonic()<deadline:
                time.sleep(.05);leaked=zombies()
            assert not leaked,leaked
            assert supervisor.poll() is None
    if not baseline:
        Path('/tmp/editor-exit').touch()
        supervisor.wait(timeout=5)
        print('PASS 1,000 orphan helpers reaped across 10 waves; supervisor follows editor exit',flush=True)
finally:
    supervisor.stdin.close()
    supervisor.wait(timeout=15)
