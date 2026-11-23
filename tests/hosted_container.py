"""Check hosted providers through installed Pi and FrostPi with synthetic credentials."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid


def check_editor(name, root, folder, output, call):
    import json
    import re
    import selectors
    import socket
    import subprocess
    import time
    from playwright.sync_api import sync_playwright

    def inside(*args):
        return subprocess.check_output(['podman','exec',name,*args],text=True)
    selected = json.loads(inside('cat','/home/node/.pi/agent/settings.json'))
    assert selected['defaultProvider'] == 'amazon-bedrock'
    bedrock_model = selected['defaultModel']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        address = f'127.0.0.1:{sock.getsockname()[1]}'
    assets = root/'ai_sandbox_factory/containers/editor'
    settings = {'binary':'/opt/test-code-server/bin/code-server','authority':address,'cwd':'/workspace',
                'password_hash':'','watch_fallback':(assets/'watch-fallback.cjs').read_text()}
    relay = None
    with (output/'editor.log').open('w') as log, sync_playwright() as playwright:
        proc = subprocess.Popen(['podman','exec','-i',name,'python3','-u','-c',(assets/'launch.py').read_text()],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
        try:
            proc.stdin.write(json.dumps(settings)+'\n')
            proc.stdin.flush()
            with selectors.DefaultSelector() as ready:
                ready.register(proc.stdout,selectors.EVENT_READ)
                assert ready.select(90) and proc.stdout.readline() == 'ready\n'
            relay = subprocess.Popen(['socat',f'TCP-LISTEN:{address.split(":")[1]},bind=127.0.0.1,reuseaddr,fork',
                                      'UNIX-CONNECT:'+str(folder/'editor/editor.sock')])
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width':1440,'height':960})
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.goto(f'http://{address}/?folder=/workspace')
            page.locator('.monaco-workbench').wait_for(timeout=60000)
            deadline = time.monotonic()+40
            while time.monotonic() < deadline:
                frame=next((f for f in page.frames if f.get_by_label('Message Pi',exact=True).count()),None)
                if frame: break
                page.wait_for_timeout(150)
            else: raise AssertionError('FrostPi startup did not open a session')
            trigger = frame.locator('.model-picker-trigger')
            trigger.and_(frame.locator('[title='+json.dumps('amazon-bedrock/'+bedrock_model)+']')).wait_for(timeout=15000)
            together = call(action='catalogue',provider='together')['models']
            together_model = together[0]['id']
            model_pairs = [('amazon-bedrock',bedrock_model), ('azure','gpt-4o'),
                           ('openrouter','anthropic/claude-haiku-4.5'), ('together',together_model)]
            for index,(provider,model) in enumerate(model_pairs):
                model_title=trigger.and_(frame.locator('[title='+json.dumps(provider+'/'+model)+']'))
                if not model_title.is_visible():
                    trigger.click()
                    frame.get_by_role('textbox',name=re.compile(r'^Search .* models$')).fill(model)
                    frame.locator(".model-option[title="+json.dumps(provider+"/"+model)+"]").click(timeout=15000)
                    model_title.wait_for(timeout=15000)
                frame.get_by_label('Message Pi',exact=True).fill(f'Hosted fixture for {provider}')
                frame.get_by_role('button',name='Send to Pi',exact=True).click()
                frame.get_by_text('OK',exact=True).nth(index).wait_for(timeout=30000)
                frame.get_by_role('button',name='Send to Pi',exact=True).wait_for(timeout=15000)
                print('PASS FrostPi hosted reply:',provider,flush=True)
            page.screenshot(path=str(output/'hosted.png'))
            assert not errors,errors
            browser.close()
        finally:
            if relay:
                relay.terminate()
                relay.wait(timeout=10)
            proc.stdin.close()
            proc.wait(timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True, help='A built Factory development image')
    parser.add_argument('--inference-image', required=True)
    parser.add_argument('--code-server', required=True, type=Path, help='Extracted pinned code-server archive')
    parser.add_argument('--output', required=True, type=Path, help='Directory for editor logs and screenshot')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    name = 'factory-hosted-check-' + uuid.uuid4().hex[:10]
    volume = name + '-home'
    root = Path(__file__).resolve().parents[1]
    script = (root / 'ai_sandbox_factory/providers/hosted-models.mjs').read_text()
    def call(**value):
        result = subprocess.run(['podman','exec','-i','--user','node',name,'node','--input-type=module','-e',script],
                                input=json.dumps(value),text=True,capture_output=True,timeout=75)
        if not result.stdout.strip(): raise RuntimeError(result.stderr)
        reply = json.loads(result.stdout)
        if result.returncode: raise RuntimeError(reply)
        return reply
    import sys, tempfile, shutil, base64, time
    sys.path.insert(0,str(root))
    from ai_sandbox_factory import inference
    broker=name+'-inference';socket=broker+'-socket';keys=broker+'-keys'
    folder=Path(tempfile.mkdtemp(prefix='factory-native-inference-'))
    config={'hosted_connections':{'openrouter':{},'together':{},'azure':{'endpoint':'https://fixture.openai.azure.com/openai/v1'},'amazon-bedrock':{'region':'eu-west-1','method':'iam'}}}
    code = args.code_server.resolve()
    (folder/'editor').mkdir()
    (folder/'workspace').mkdir()
    try:
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(folder/'tls.key'),'-out',str(folder/'tls.crt')],check=True,capture_output=True)
        shutil.copy(root/'tests/inference_fixture.mjs',folder/'server.mjs')
        routes=inference.routes(config)
        for route in routes.values():route['origin']='https://127.0.0.1:18444'
        (folder/'routes.json').write_text(json.dumps(routes))
        subprocess.run(['podman','run','-d','--name',name,'--network','none','--userns=keep-id:uid=1000,gid=1000',
                        '--user','1000:1000','--security-opt','label=disable',
                        '-v', str(code)+':/opt/test-code-server:ro', '-v', str(folder/'editor')+':/home/node/.local/share/pi-browser-editor:rw',
                        '-v', str(folder/'workspace')+':/workspace:rw',
                        '--volume',volume+':/home/node','--volume',socket+':/run/inference:ro',
                        '--entrypoint','sleep',args.image,'infinity'],check=True,capture_output=True)
        subprocess.run(['podman','exec',name,'python3','/usr/local/lib/sandbox-bootstrap.py'],check=True)
        frames = r"""
const {EventStreamCodec}=require('/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@smithy/core/event-streams');
const codec=new EventStreamCodec(x=>Buffer.from(x).toString(),x=>Buffer.from(x));
const events=[['messageStart',{role:'assistant'}],['contentBlockDelta',{contentBlockIndex:0,delta:{text:'OK'}}],['contentBlockStop',{contentBlockIndex:0}],['messageStop',{stopReason:'end_turn'}],['metadata',{usage:{inputTokens:1,outputTokens:1,totalTokens:2},metrics:{latencyMs:1}}]];
console.log(Buffer.concat(events.map(([type,body])=>Buffer.from(codec.encode({headers:{':message-type':{type:'string',value:'event'},':event-type':{type:'string',value:type},':content-type':{type:'string',value:'application/json'}},body:Buffer.from(JSON.stringify(body))})))).toString('base64'));
"""
        encoded=subprocess.run(['podman','exec','-i',name,'node'],input=frames,text=True,check=True,capture_output=True).stdout
        (folder/'bedrock.bin').write_bytes(base64.b64decode(encoded))
        subprocess.run(['podman','run','-d','--name',broker,'--network','none','--userns=keep-id:uid=1000,gid=1000','--user','1000:1000','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                        '-v',keys+':/private:z','-v',socket+':/run/inference:z','-v',str(folder)+':/fixture:ro,Z','-e','NODE_EXTRA_CA_CERTS=/fixture/tls.crt',args.inference_image,'node','/fixture/server.mjs'],check=True,capture_output=True)
        for provider in config['hosted_connections']:
            credential={'accessKeyId':'fixture-access','secretAccessKey':'fixture-secret','sessionToken':'fixture-session'} if provider=='amazon-bedrock' else {'key':'private-fixture-'+provider}
            subprocess.run(['podman','exec','-i',broker,'node','/app/credentials.mjs','/private/credentials.json'],input=json.dumps({'id':provider,'auth':routes[provider]['auth']['type'],'origin':routes[provider]['origin'],'credentials':credential}),text=True,check=True,capture_output=True)
        for _ in range(80):
            if subprocess.run(['podman','exec',name,'test','-S','/run/inference/proxy.sock'],capture_output=True).returncode==0:break
            time.sleep(.25)
        subprocess.run(['podman','exec','-d',name,'socat','TCP-LISTEN:19431,bind=127.0.0.1,reuseaddr,fork','UNIX-CONNECT:/run/inference/proxy.sock'],check=True,capture_output=True)
        for provider,connection in config['hosted_connections'].items():
            reply=call(action='configure',provider=provider,endpoint=inference.client_url(provider,connection),region=connection.get('region',''))
            assert reply['configured']
            assert call(action='catalogue',provider=provider)['models']
            print('PASS configuration/catalogue:',provider,flush=True)
        call(action='select',provider='azure',model='gpt-4o',deployment='team-gpt')
        call(action='test',provider='openrouter',model='anthropic/claude-haiku-4.5')
        try:
            call(action='test',provider='azure',model='gpt-4o',deployment='missing')
        except RuntimeError as error:
            assert 'DeploymentNotFound' in str(error),str(error)
        else:
            raise AssertionError('Missing deployment was accepted')
        print('PASS bounded connection test and actionable provider errors',flush=True)
        bedrock=call(action='catalogue',provider='amazon-bedrock')['models']
        base=next(m for m in bedrock if 'claude' in m['id'])['id']
        call(action='select',provider='amazon-bedrock',model=base,profile_id='arn:aws:bedrock:eu-west-1:123456789012:application-inference-profile/fixture')
        subprocess.run(['podman', 'exec', '-i', '--user', 'node', name, 'node'],
                       input=(root/'tests/check_hosted_runtime.cjs').read_text(), text=True, check=True, timeout=150)
        check_editor(name, root, folder, args.output, call)
        for provider in ['openrouter','together','azure','amazon-bedrock']:call(action='remove',provider=provider)
        assert not any(p['configured'] for p in call(action='status')['providers'])
        print('PASS connection removal')
    finally:
        subprocess.run(['podman','rm','-f',name,broker],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['podman','volume','rm',volume,socket,keys],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.rmtree(folder)


if __name__ == '__main__': main()
