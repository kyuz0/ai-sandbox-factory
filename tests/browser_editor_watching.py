"""Real Explorer regression: healthy native watches, exhausted watches, automatic fallback.

Uses a cached Factory image and extracted pinned code-server. Only its disposable
container is removed. Seccomp injects EMFILE inside that container; host quotas stay unchanged.
"""
import argparse
import json
import selectors
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def check(image, code_server, output):
    output.mkdir(parents=True, exist_ok=True)
    fallback = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/editor/watch-fallback.cjs'
    with tempfile.TemporaryDirectory(prefix='factory-editor-watch-') as temp, sync_playwright() as playwright:
        root = Path(temp); (root/'workspace').mkdir()
        policy = root/'seccomp.json'
        policy.write_text(json.dumps({'defaultAction':'SCMP_ACT_ALLOW','syscalls':[
            {'names':['inotify_init','inotify_init1'],'action':'SCMP_ACT_ERRNO','errnoRet':24}]}))
        for scenario in ('native', 'exhausted', 'fallback', 'launcher'):
            name = 'factory-watch-' + uuid.uuid4().hex[:10]
            args = ['podman','run','-d','--name',name,'--label','io.pi-sandbox-factory.test=editor-watching',
                    '--userns','keep-id:uid=1000,gid=1000','--user','1000:1000','--security-opt','label=disable',
                    '-p','127.0.0.1::19989','-v',f'{root / "workspace"}:/workspace:rw',
                    '-v',f'{code_server}:/opt/code:ro','-v',f'{fallback}:/tmp/watch-fallback.cjs:ro']
            if scenario != 'native': args += ['--security-opt',f'seccomp={policy}']
            if scenario == 'fallback': args += ['-e','NODE_OPTIONS=--require=/tmp/watch-fallback.cjs']
            args += (['--entrypoint','sleep',image,'infinity'] if scenario == 'launcher' else
                     ['--entrypoint','/opt/code/bin/code-server',image,'--bind-addr','0.0.0.0:19989',
                     '--auth','none','--user-data-dir','/tmp/editor-data','--extensions-dir','/tmp/extensions',
                     '--disable-telemetry','--disable-update-check','--disable-workspace-trust','/workspace'])
            run(*args)
            browser = supervisor = None
            try:
                address = run('podman','port',name,'19989/tcp')
                if scenario == 'launcher':
                    helper = fallback.with_name('launch.py').read_text()
                    with (output/'launcher.log').open('w') as log:
                        supervisor = subprocess.Popen(['podman','exec','-i',name,'python3','-u','-c',helper],
                            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
                    supervisor.stdin.write(json.dumps({'binary':'/opt/code/bin/code-server','cwd':'/workspace',
                        'authority':address,'password_hash':'','watch_fallback':fallback.read_text()})+'\n')
                    supervisor.stdin.flush()
                    with selectors.DefaultSelector() as ready:
                        ready.register(supervisor.stdout,selectors.EVENT_READ)
                        assert ready.select(60), 'Production editor launcher did not become ready'
                    assert supervisor.stdout.readline() == 'ready\n'
                    run('podman','exec','-d',name,'socat','TCP-LISTEN:19989,reuseaddr,fork',
                        'UNIX-CONNECT:/home/node/.local/share/pi-browser-editor/editor.sock')
                browser = playwright.chromium.launch(headless=True,args=['--no-proxy-server'])
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
                for attempt in range(30):
                    try: page.goto(f'http://{address}/?folder=/workspace'); break
                    except Exception:
                        if attempt == 29: raise
                        time.sleep(1)
                page.locator('.monaco-workbench').wait_for(timeout=60000)
                page.keyboard.press('Control+Shift+e')
                page.locator('.explorer-viewlet').wait_for(timeout=30000)
                page.wait_for_timeout(3000)
                prefix = scenario + '-' + uuid.uuid4().hex[:6]
                created, renamed = prefix+'-created.txt', prefix+'-renamed.txt'
                operations = [('create', f'echo fixture > /workspace/{created}', created, None),
                              ('rename', f'mv /workspace/{created} /workspace/{renamed}', renamed, created),
                              ('delete', f'rm /workspace/{renamed}', None, renamed)]
                for action, command, new, old in operations:
                    run('podman','exec',name,'sh','-c',command)
                    started=time.monotonic()
                    try:
                        if new: page.locator('.explorer-viewlet').get_by_text(new,exact=True).wait_for(timeout=12000)
                        if old: page.locator('.explorer-viewlet').get_by_text(old,exact=True).wait_for(state='hidden',timeout=12000)
                    except BrowserTimeout:
                        if scenario != 'exhausted': raise
                        print('PASS exhausted: reproduced stale Explorer without refresh',flush=True)
                        break
                    assert scenario != 'exhausted', 'The fault did not reproduce stale Explorer'
                    print(f'PASS {scenario}: {action} visible in {time.monotonic()-started:.2f}s',flush=True)
                assert not errors, errors
                page.screenshot(path=str(output/f'{scenario}.png'))
                (output/f'{scenario}.log').write_text(run('podman','logs',name))
            finally:
                try:
                    if browser: browser.close()
                    if supervisor:
                        supervisor.stdin.close()
                        supervisor.wait(timeout=15)
                finally:
                    run('podman','rm','-f',name)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--code-server',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.image,args.code_server.resolve(),args.output)
