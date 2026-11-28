"""Disposable browser-control isolation probe; never updates a live sandbox.

Run with PYTHONPATH=.:tests python tests/browser_settings_probe.py --output /tmp/probe.
Optional --image supplies a cached Factory image containing the pinned code-server.
The synthetic Apply handler changes private fixture state only.
"""
import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from playwright.sync_api import sync_playwright
from ai_sandbox_factory import browser_editor, operations, sandbox, settings_catalog

ASSETS = Path(__file__).parent / 'fixtures/browser_settings'


@contextmanager
def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, content='application/json', extra=()):
        body = json.dumps(value).encode() if content == 'application/json' else value.encode()
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class Host(Handler):
    def do_GET(self):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            return self.reply(403, {'error': 'Wrong host'})
        if self.path == '/negative-control':
            return self.reply(200, '<script>sessionStorage.setItem("unsafe-authority","synthetic")</script><iframe src="/same-origin-editor"></iframe>', 'text/html')
        if self.path == '/same-origin-editor':
            return Editor.do_GET(self)
        assets = {'/': ('panel.html','text/html'), '/panel.js': ('panel.js','text/javascript'), '/panel.css': ('panel.css','text/css'), '/brand.css': ('brand.css','text/css')}
        route = self.path.split('?', 1)[0]
        if route not in assets:
            return self.reply(404, {'error': 'Unknown route'})
        file, content = assets[route]
        self.reply(200, (ASSETS / file).read_text(), content, [
            ('Content-Security-Policy', "default-src 'self'; frame-src " + self.server.editor + "; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
            ('X-Frame-Options', 'DENY'), ('Cross-Origin-Opener-Policy', 'same-origin')])

    def do_POST(self):
        host = self.server
        if (self.headers.get('Host') != host.origin.removeprefix('http://')
                or self.headers.get('Origin') != host.origin
                or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + host.token)):
            return self.reply(403, {'error': 'Open this experiment using its host-issued link, then retry.'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.reply(415, {'error': 'Use application/json'})
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with host.lock:
                if self.path == '/state':
                    return self.reply(200, self.state())
                if self.path == '/preview':
                    if set(data) != {'network'}:
                        raise ValueError('This experiment accepts only Network mode.')
                    # Exercise the existing shared operation, not a parallel validator.
                    preview = host.service.preview_settings('example', data)
                    plan = secrets.token_hex(16)
                    host.plans[plan] = (host.revision, data)
                    return self.reply(200, {'id': plan, 'notes': preview['notes'], 'runtime_effect': preview['runtime_effect']})
                if self.path == '/apply':
                    planned = host.plans.pop(data.get('plan'), None)
                    if not planned or planned[0] != host.revision:
                        return self.reply(409, {'error': 'Settings changed after review. Review the change again.'})
                    if host.fail_next:
                        host.fail_next = False
                        return self.reply(503, {'error': 'Demo update failed before changing settings. Retry the review.'})
                    host.config.update(planned[1])
                    host.revision += 1
                    (host.service.state / 'example/config.json').write_text(json.dumps(host.config))
                    host.applies += 1
                    return self.reply(200, self.state())
                return self.reply(404, {'error': 'Unknown route'})
        except (ValueError, TypeError, KeyError) as error:
            self.reply(400, {'error': str(error)})

    def state(self):
        return {'network': self.server.config['network'], 'editor': self.server.editor,
                'label': settings_catalog.NETWORK_MODE_LABEL, 'revision': self.server.revision}


class Editor(Handler):
    def do_GET(self):
        if hasattr(self.server, 'requests'):
            self.server.requests.append(dict(self.headers))
        self.reply(200, '''<!doctype html><title>Untrusted editor fixture</title>
        <meta charset="utf-8"><body style="background:#181b1e;color:#bbb;font:15px monospace;padding:28px">
        <p>EXPLORER &nbsp; README.md &nbsp; · &nbsp; FrostPi</p><h2>Project workspace</h2>
        <textarea aria-label="Editor text" style="width:85%;height:300px;background:#202429;color:#eee">Your project stays open while you review sandbox settings.</textarea>
        <button id="navigate">Try top navigation</button><button id="popup" onclick="window.open('/')">Open popup</button><button id="frame-fullscreen" onclick="document.documentElement.requestFullscreen().then(()=>document.body.dataset.fullscreen='allowed').catch(()=>document.body.dataset.fullscreen='blocked')">Try frame fullscreen</button>
        <script>document.querySelector('#navigate').onclick=()=>{try{top.location='about:blank'}catch(e){document.body.dataset.nav='blocked'}};</script>
        </body>''', 'text/html')


def api(origin, token, path, value, **headers):
    request = Request(origin + path, data=json.dumps(value).encode(), headers={
        'Authorization': 'Bearer ' + token, 'Origin': origin, 'Content-Type': 'application/json', **headers})
    try:
        with urlopen(request) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


@contextmanager
def actual_editor(image, output, shell_origin):
    """Use an owned, offline container and existing relay; no host folders mounted."""
    name = 'factory-toolbar-probe-' + secrets.token_hex(6)
    def podman(*args):
        return subprocess.check_output(['podman', *args], text=True).strip()
    container = podman('run', '-d', '--name', name, '--network', 'none', '--entrypoint', 'sleep', image, 'infinity')
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    relay = server = None
    try:
        podman('exec', container, 'sh', '-c', 'mkdir -p /tmp/probe-workspace /tmp/probe-data/User /tmp/probe-extensions; echo "Toolbar experiment" > /tmp/probe-workspace/README.md; ln -s /opt/editor-extensions/* /tmp/probe-extensions/')
        settings = {'workbench.colorTheme':'Default Dark Modern', 'security.workspace.trust.enabled':False, 'workbench.startupEditor':'none', 'chat.disableAIFeatures':True}
        podman('exec', container, 'node', '-e', "require('fs').writeFileSync('/tmp/probe-data/User/settings.json',process.argv[1])", json.dumps(settings))
        editor_command = ['exec', '-d', '-e', 'PASSWORD=synthetic-editor-password', container, browser_editor.INSTALL + '/bin/code-server', '--auth', 'password',
               '--socket', '/tmp/toolbar-editor.sock', '--user-data-dir', '/tmp/probe-data',
               '--extensions-dir', '/tmp/probe-extensions', '--disable-telemetry', '--disable-update-check', '/tmp/probe-workspace']
        podman(*editor_command)
        deadline = time.monotonic() + 90
        while subprocess.run(['podman','exec',container,'test','-S','/tmp/toolbar-editor.sock'], capture_output=True).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('Disposable code-server did not open its socket within 90s')
            time.sleep(.25)
        class Embedded(browser_editor.Relay):
            def response_headers(self, lines):
                return [line for line in lines if line] + [
                    "Content-Security-Policy: frame-ancestors 'self' " + shell_origin, '', '']

            def validate(self, head):
                # The protected iframe must also follow code-server's login redirect.
                request, rest = head.split(b'\r\n', 1)
                if request.startswith(b'GET /login'):
                    head = b'GET / HTTP/1.1\r\n' + rest
                return browser_editor.validate_head(head, self.host, embedded_navigation=True)
        relay = Embedded(container)
        relay.socket = '/tmp/toolbar-editor.sock'
        async def start():
            server = await asyncio.start_server(relay.handle, '127.0.0.1', 0)
            relay.host = '127.0.0.1:' + str(server.sockets[0].getsockname()[1])
            return server
        server = asyncio.run_coroutine_threadsafe(start(), loop).result()
        yield 'http://' + relay.host, lambda: podman('kill', container), lambda: (podman('start', container), podman('exec', container, 'rm', '-f', '/tmp/toolbar-editor.sock'), podman(*editor_command))
    finally:
        # Stop our offline fixture before reaping its active WebSocket relays.
        subprocess.run(['podman','rm','-f',container], check=True, capture_output=True)
        async def close():
            if server:
                server.close()
            if relay:
                tasks = list(relay.tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if server:
                await server.wait_closed()
        asyncio.run_coroutine_threadsafe(close(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


def run(output, image=None):
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix='factory-toolbar-state-') as directory, serve(Editor) as (editor_server, editor), serve(Host) as (host, origin), sync_playwright() as p:
        editor_server.requests = []
        host.origin, host.editor, host.token = origin, editor, secrets.token_urlsafe(32)
        host.config = {'name':'example','network':'allowlist','engine':'podman','options':{},'model_providers':[]}
        host.service = operations.Service(Path(directory))
        state = Path(directory) / 'example'
        state.mkdir()
        (state / 'config.json').write_text(json.dumps(host.config))
        (state / 'allowlist.txt').write_text('')
        host.lock = threading.Lock()
        host.revision, host.applies, host.fail_next, host.plans = 0, 0, False, {}
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440,'height':900})
            errors, console_messages = [], []
            page.on('console', lambda m: console_messages.append({'level':m.type,'text':m.text}) if m.type in ('error','warning') else None)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(origin + '/#' + host.token)
            page.frame_locator('#editor').get_by_label('Editor text').wait_for()
            assert page.url == origin + '/'
            assert page.title() == 'Factory toolbar experiment'
            assert page.locator('#drawer').is_hidden()
            assert page.locator('#toolbar').is_hidden()
            page.screenshot(path=str(output / 'toolbar-closed.png'))
            if page.locator('#toolbar').is_hidden():
                page.get_by_role('button',name='Show sandbox toolbar').click()
            page.get_by_role('button',name='Settings',exact=True).click()
            page.select_option('#network','unrestricted')
            page.get_by_role('button',name='Review change',exact=True).click()
            page.get_by_role('button',name='Apply',exact=True).wait_for()
            assert 'replaces' in page.locator('#effect').inner_text().lower()
            page.locator('#handle').click()
            page.get_by_role('button',name='Settings',exact=True).click()
            assert page.locator('#network').input_value() == 'unrestricted'
            assert page.get_by_role('button',name='Apply',exact=True).is_visible()
            page.screenshot(path=str(output / 'toolbar-review.png'))
            page.get_by_role('button',name='Apply',exact=True).click()
            page.get_by_text('Demo setting updated. No live sandbox was changed.').wait_for()
            assert host.applies == 1
            checks.append('Toolbar opens; shared preview shows replacement effect; synthetic Apply succeeds')
            page.reload()
            page.frame_locator('#editor').get_by_label('Editor text').wait_for()
            checks.append('Host authorization survives reload without fragment or editor token')
            assert all(host.token not in json.dumps(row) for row in editor_server.requests)
            frame = page.frames[1]
            isolated = frame.evaluate('''() => {const out={}; for(const [key, fn] of Object.entries({dom:()=>parent.document.body.innerHTML,storage:()=>parent.sessionStorage.getItem('factory-control')})){try{fn();out[key]=false}catch(e){out[key]=true}} parent.postMessage({type:'apply',network:'allowlist'},'*');return out}''')
            assert isolated == {'dom':True,'storage':True}
            assert host.applies == 1
            cross = frame.evaluate('''async origin=>{try{await fetch(origin+'/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return false}catch(e){return true}}''', origin)
            assert cross
            assert host.applies == 1
            frame.get_by_role('button',name='Try top navigation').click()
            assert page.url == origin + '/'
            checks.append('Hostile editor cannot read parent/token, apply via fetch/postMessage, or navigate top after click')
            with page.expect_popup() as opened:
                frame.get_by_role('button',name='Open popup').click()
            child = opened.value
            child.wait_for_load_state()
            assert child.evaluate('window.opener === null')
            child.close()
            checks.append('Escaped editor popup loses opener with host COOP; cannot navigate toolbar through opener')
            frame.get_by_role('button',name='Try frame fullscreen').click()
            from playwright.sync_api import expect
            expect(frame.locator('body')).to_have_attribute('data-fullscreen','blocked')
            assert page.evaluate('document.fullscreenElement === null')
            checks.append('Editor fullscreen is denied; only host toolbar can hide browser chrome')
            with page.expect_console_message(predicate=lambda message: 'frame-ancestors' in message.text):
                frame.evaluate('''origin=>{const f=document.createElement('iframe');f.src=origin;document.body.append(f);}''', origin)
            checks.append('Host controls refuse framing by untrusted editor')
            # A same-origin embedding is a failing control: the editor can read the authority.
            unsafe = browser.new_page()
            unsafe.goto(origin + '/negative-control')
            unsafe.frame_locator('iframe').get_by_label('Editor text').wait_for()
            assert unsafe.frames[1].evaluate("parent.sessionStorage.getItem('unsafe-authority')") == 'synthetic'
            unsafe.close()
            checks.append('Negative control: same-origin editor can steal authorization; reject this design')
            # A separate host-owned window needs no editor framing, but separates the workflow.
            popup = browser.new_page(viewport={'width':480,'height':650})
            popup.goto(origin + '/?settings-only#' + host.token)
            popup.get_by_label('Network mode',exact=True).wait_for()
            assert popup.evaluate('window.opener===null')
            popup.screenshot(path=str(output / 'separate-window.png'))
            popup.close()
            checks.append('Separate settings window works without opener or embedded editor')
            # Browsers scope cookies to a host, not a port. Demonstrate why they are not our authority.
            page.context.add_cookies([{'name':'probe-cookie','value':'synthetic','url':origin}])
            frame.goto(editor)
            assert 'probe-cookie=synthetic' in editor_server.requests[-1].get('Cookie','')
            checks.append('Confirmed cookie leakage across ports; bearer remains absent from editor requests')
            for path in ('/state','/preview','/apply'):
                assert api(origin,'wrong',path,{})[0] == 403
                assert api(origin,host.token,path,{},Origin=editor)[0] == 403
            assert api(origin,host.token,'/state',{}, Host='wrong.invalid')[0] == 403
            assert api(origin,host.token,'/state',{}, Origin='null')[0] == 403
            assert api(origin,host.token,'/state',{}, **{'Content-Type':'text/plain'})[0] == 415
            assert api(origin,host.token,'/preview',{'network':'invalid'})[0] == 400
            assert api(origin,host.token,'/preview',{'network':'allowlist','command':'anything'})[0] == 400
            checks.append('Spoofed origin/no token and unsupported settings rejected')
            status, plan = api(origin,host.token,'/preview',{'network':'allowlist'})
            assert status == 200
            host.revision += 1
            assert api(origin,host.token,'/apply',{'plan':plan['id']})[0] == 409
            assert api(origin,host.token,'/apply',{'plan':plan['id']})[0] == 409
            checks.append('Stale review and consumed plan rejected')
            if page.locator('#toolbar').is_hidden():
                page.get_by_role('button',name='Show sandbox toolbar').click()
            page.get_by_role('button',name='Settings',exact=True).click()
            page.select_option('#network','allowlist')
            page.get_by_role('button',name='Review change',exact=True).click()
            page.get_by_role('button',name='Apply',exact=True).wait_for()
            host.fail_next = True
            page.get_by_role('button',name='Apply',exact=True).click()
            page.get_by_text('Demo update failed before changing settings. Retry the review.').wait_for()
            assert host.config['network'] == 'unrestricted'
            page.get_by_role('button',name='Review change',exact=True).click()
            page.get_by_role('button',name='Apply',exact=True).click()
            page.get_by_text('Demo setting updated. No live sandbox was changed.').wait_for()
            checks.append('Synthetic failure preserves settings and retry succeeds')
            for width,height in ((1440,900),(900,650),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                assert page.get_by_role('button',name='Cancel',exact=True).is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / f'panel-{width}.png'))
            page.keyboard.press('Escape')
            assert page.locator('#status').inner_text() == ''
            assert page.locator('#drawer').is_hidden()
            assert page.locator('#handle').evaluate('(e)=>e===document.activeElement')
            checks.append('Wide/narrow layouts; Escape returns focus; no horizontal overflow')
            page.locator('#handle').click()
            page.get_by_role('button',name='Full screen',exact=True).click()
            page.wait_for_function('() => document.fullscreenElement === document.documentElement')
            assert page.locator('#handle').is_visible()
            page.get_by_role('button',name='Exit full screen',exact=True).click()
            page.wait_for_function('() => !document.fullscreenElement')
            checks.append('Fullscreen enters/exits from host toolbar and keeps handle available')
            assert not errors, errors
            if image:
                with actual_editor(image, output, origin) as (actual, stop_editor, restart_editor):
                    host.editor = actual
                    page.set_viewport_size({'width':1440,'height':900})
                    page.reload()
                    editor_frame = page.frame_locator('#editor')
                    editor_frame.locator('input[name=password]').fill('synthetic-editor-password')
                    editor_frame.locator('input[type=submit]').click()
                    editor_frame.locator('.monaco-workbench').wait_for(timeout=90000)
                    expect(editor_frame.locator('.monaco-workbench')).to_have_class(re.compile(r'vs-dark'))
                    editor_frame.get_by_role('treeitem', name='README.md').dblclick()
                    editor_frame.locator('.monaco-editor').first.wait_for()
                    page.screenshot(path=str(output / 'actual-editor-closed.png'))
                    page.screenshot(path=str(output / 'closed-detail.png'), clip={'x':0,'y':0,'width':1440,'height':240})
                    # The small tab must leave the title-bar command centre usable.
                    command = editor_frame.locator('.command-center-center')
                    command_box = command.bounding_box()
                    tab_box = page.locator('#handle').bounding_box()
                    assert tab_box['y'] + tab_box['height'] <= command_box['y']
                    command.click(position={'x':12,'y':14})
                    editor_frame.locator('.quick-input-widget input').wait_for()
                    page.keyboard.press('Escape')
                    checks.append('Editor command centre remains clickable beside the collapsed tab')
                    if page.locator('#toolbar').is_hidden():
                        page.get_by_role('button',name='Show sandbox toolbar').click()
                    for width,height in ((1440,900),(900,650),(820,650),(390,844)):
                        page.set_viewport_size({'width':width,'height':height})
                        bar = page.locator('#toolbar').bounding_box()
                        tab = page.locator('#handle').bounding_box()
                        assert bar['y'] == 0
                        assert abs(tab['y'] - (bar['height'] - 1)) < 1
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(output / f'actual-toolbar-{width}.png'))
                        if width == 1440:
                            page.screenshot(path=str(output / 'toolbar-detail.png'), clip={'x':0,'y':0,'width':1440,'height':240})
                    page.set_viewport_size({'width':1440,'height':900})
                    page.get_by_role('button',name='Settings',exact=True).click()
                    assert page.locator('#toolbar').is_hidden()
                    page.screenshot(path=str(output / 'actual-editor-settings.png'))
                    checks.append('Pinned code-server password login and file opening work in framed editor')
                    page.locator('#close').click()
                    # Wait for extension activation before testing editor focus/clipboard.
                    editor_frame.locator('.monaco-editor').first.click()
                    page.keyboard.press('Control+Shift+p')
                    editor_frame.locator('.quick-input-widget input').fill('>FrostPi: Focus Chat')
                    page.keyboard.press('Enter')
                    deadline = time.monotonic() + 60
                    composer = None
                    while time.monotonic() < deadline:
                        for child in page.frames[1:]:
                            try:
                                field = child.get_by_label('Message Pi',exact=True)
                                if field.count():
                                    composer = field
                                    break
                            except Exception:
                                continue
                        if composer:
                            break
                        page.wait_for_timeout(100)
                    assert composer, 'Shipped FrostPi composer did not appear inside the framed editor'
                    composer.fill('Toolbar compatibility draft — not sent')
                    checks.append('Shipped FrostPi nested webview renders and accepts draft typing; no model request')
                    page.screenshot(path=str(output / 'actual-frostpi.png'))
                    page.locator('#handle').click()
                    editor_frame.locator('.monaco-editor').first.click()
                    expect(page.locator('#toolbar')).to_be_hidden()
                    checks.append('Toolbar touches top edge; one attached tab; editor focus collapses it; settings has no overlapping toolbar')
                    page.context.grant_permissions(['clipboard-read','clipboard-write'],origin=actual)
                    page.context.grant_permissions(['clipboard-read','clipboard-write'],origin=origin)
                    page.keyboard.press('Control+a')
                    page.keyboard.press('Control+c')
                    assert 'Toolbar experiment' in page.evaluate('navigator.clipboard.readText()')
                    page.evaluate("navigator.clipboard.writeText('Clipboard probe')")
                    page.keyboard.press('ArrowRight')
                    page.keyboard.press('Control+v')
                    expect(editor_frame.locator('.view-lines').first).to_contain_text('Clipboard probe')
                    checks.append('Real editor selection copies to browser clipboard and paste reaches Monaco')
                    # Restart only the owned editor; the host control process stays up.
                    stop_editor()
                    assert api(origin,host.token,'/state',{})[0] == 200
                    if page.locator('#toolbar').is_hidden():
                        page.locator('#handle').click()
                    page.get_by_role('button',name='Settings',exact=True).click()
                    assert page.get_by_label('Network mode',exact=True).is_visible()
                    restart_editor()
                    page.locator('#editor').evaluate('(e)=>{ e.src=e.src; }')
                    editor_frame.locator('.monaco-workbench').wait_for(timeout=90000)
                    assert api(origin,host.token,'/state',{})[0] == 200
                    checks.append('Host controls/auth stay available during editor restart; original iframe URL and login reconnect')
            assert not errors, errors
            (output / 'results.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'console_messages':console_messages},indent=2))
            print(json.dumps(checks,indent=2))
        except BaseException:
            page.screenshot(path=str(output / 'failure.png'))
            (output / 'failure-dom.html').write_text(page.content())
            for index, frame in enumerate(page.frames):
                try:
                    (output / f'failure-frame-{index}.html').write_text(frame.content())
                except Exception:
                    pass
            raise
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',help='Cached image ID for disposable real code-server embedding check')
    args = parser.parse_args()
    run(args.output,args.image)
