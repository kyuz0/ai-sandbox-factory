"""Real browser controls with an owned disposable Factory sandbox.

Creates, updates and deletes its own sandbox. No model requests or host sign-ins.
Run: PYTHONPATH=.:tests python tests/browser_toolbar.py --output /tmp/toolbar-check
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect
from ai_sandbox_factory import browser_editor, browser_shell, editor_settings, sandbox
from browser_editor_lifecycle import request


def exercise(state, output, *, headed=False):
    config = json.loads((state / 'config.json').read_text())
    editor = browser_editor.runtime(state)['url']
    origin = browser_shell.origin(state)
    password = 'synthetic-toolbar-test-password'
    status, headers, _ = request(editor, '/login', method='POST', body=urlencode({'password':password}),
        headers={'Content-Type':'application/x-www-form-urlencoded'})
    assert status == 302, (status, headers)
    cookie = headers['Set-Cookie'].split(';')[0]
    results = []
    def passed(value):
        results.append(value)
        print('PASS ' + value, flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={'width':1440,'height':900})
        page = context.new_page()
        errors = []
        replacing = False
        restart_errors = []
        def page_error(error):
            message = str(error)
            # The old editor can lose a pending webview fetch while its server
            # is replaced. Recovery below must prove a fresh webview works.
            if replacing and message.startswith('Could not register service worker:') and editor in message:
                restart_errors.append(message)
            else:
                errors.append(message)
        page.on('pageerror', page_error)
        frame = page.frame_locator('#editor')
        def chat():
            frame.locator('.monaco-workbench').wait_for(timeout=120000)
            frame.locator('.monaco-workbench').click(position={'x':600,'y':400})
            page.keyboard.press('Control+Shift+p')
            frame.locator('.quick-input-widget input').fill('>FrostPi: Focus Chat')
            frame.locator('.quick-input-widget').get_by_text('FrostPi: Focus Chat',exact=True).click()
            deadline = time.monotonic()+90
            last_error = None
            while time.monotonic()<deadline:
                for child in page.frames[1:]:
                    try:
                        field = child.get_by_label('Message Pi',exact=True)
                        visible = field.count() and field.is_visible()
                        if not visible:
                            new_session = child.locator('.onboarding-actions button').filter(has_text='New session')
                            if new_session.count() and new_session.is_visible(): new_session.click()
                    except Exception as error:
                        last_error = str(error)
                        continue
                    if visible:
                        field.fill('Toolbar compatibility draft — not sent')
                        actual = field.evaluate('(node) => node.value ?? node.textContent')
                        assert actual.strip() == 'Toolbar compatibility draft — not sent',repr(actual)
                        return
                page.wait_for_timeout(100)
            raise AssertionError('Shipped FrostPi composer did not appear: '+str(last_error))
        def section(name):
            if page.locator('#drawer').is_hidden():
                if page.locator('#toolbar').is_hidden(): page.locator('#handle').click()
                page.locator('#settings').click()
            if page.locator('#categories').is_hidden(): page.locator('#section-back').click()
            page.locator(f'[data-section="{name}"]').click()
        def apply(completion):
            page.locator('#apply').click()
            expect(page.locator('#review')).to_be_visible()
            page.locator('#apply').click()
            expect(page.locator('#status')).to_have_text(completion,timeout=300000)
        try:
            page.goto(browser_shell.control(state,'pair'))
            expect(page.locator('#editor')).to_have_attribute('src', editor)
            assert not urlsplit(page.url).fragment
            frame.locator('input[name="password"]').fill(password)
            frame.get_by_role('button',name='Submit').click()
            frame.locator('.monaco-workbench').wait_for(timeout=120000)
            frame.locator('.monaco-workbench').click(position={'x':600,'y':400})
            frame.get_by_role('treeitem',name='README.md',exact=True).dblclick()
            frame.locator('.monaco-editor').first.wait_for()
            page.screenshot(path=str(output/'editor-closed.png'))
            centre = frame.locator('.command-center-center')
            grip = page.locator('#handle').bounding_box()
            assert grip['y'] + grip['height'] <= centre.bounding_box()['y']
            centre.click(position={'x':12,'y':14})
            frame.locator('.quick-input-widget input').wait_for()
            page.keyboard.press('Escape')
            # Focus starts on the host grip on a fresh document; Tab does not
            # require a synthetic keyboard handler inside the untrusted editor.
            page.locator('#handle').focus()
            page.keyboard.press('Enter')
            expect(page.locator('#toolbar')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#toolbar')).to_be_hidden()
            passed('real editor login and file opening; command centre unobstructed; keyboard disclosure and Escape')
            chat()
            page.screenshot(path=str(output/'frostpi.png'))
            passed('shipped FrostPi nested webview accepts a draft without a model request')

            token = page.evaluate("sessionStorage.getItem('factory-control')")
            untrusted = page.frames[1]
            assert untrusted.evaluate("() => {try{return parent.sessionStorage.getItem('factory-control')}catch{return 'blocked'}}") == 'blocked'
            denied = untrusted.evaluate("async origin => {try {return await fetch(origin+'/state',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.status)}catch{return 'blocked'}}", origin)
            assert denied == 'blocked', denied
            with page.expect_popup() as pending:
                untrusted.evaluate('(url) => window.open(url)', origin)
            popup = pending.value
            popup.wait_for_load_state()
            expect(popup.locator('body')).to_contain_text(re.compile('not connected|Cross-site'))
            popup.goto(origin)
            expect(popup.locator('#status')).to_contain_text('not connected')
            assert popup.evaluate("sessionStorage.getItem('factory-control')") is None
            popup.close()
            assert untrusted.evaluate("async () => {try{await document.documentElement.requestFullscreen();return 'allowed'}catch{return 'blocked'}}") == 'blocked'
            passed('editor cannot read authority, call controls, inherit an authorized popup or enter fullscreen')

            page.locator('#handle').click()
            page.locator('#fullscreen').click()
            expect(page.locator('#fullscreen')).to_have_text('Exit full screen')
            page.locator('#fullscreen').click()
            expect(page.locator('#fullscreen')).to_have_text('Full screen')
            passed('host fullscreen enters and exits without changing the editor')
            page.locator('#settings').click()
            category = page.locator('[data-section=approvals]')
            category.focus()
            page.wait_for_timeout(1800)
            expect(category).to_be_focused()
            passed('settings polling preserves keyboard focus')
            if page.locator('#categories').is_visible(): page.locator('[data-section=network]').click()
            for width,height in ((1440,900),(900,650),(820,650),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                expect(page.locator('#apply')).to_be_in_viewport()
                expect(page.locator('#close')).to_be_in_viewport()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output/f'settings-{width}.png'))
            page.set_viewport_size({'width':1440,'height':900})
            page.locator('#network').select_option('unrestricted')
            page.locator('#apply').click()
            expect(page.locator('#review')).to_be_visible()
            page.screenshot(path=str(output/'review.png'))
            page.locator('#cancel').click()
            page.locator('#handle').click()
            page.locator('#settings').click()
            if page.locator('#categories').is_visible(): page.locator('[data-section=network]').click()
            expect(page.locator('#network')).to_have_value('allowlist')
            expect(page.locator('#review')).to_be_hidden()
            passed('responsive panel, review cancellation and draft reset')

            for mode in ('unrestricted','allowlist'):
                section('network')
                page.locator('#network').select_option(mode)
                replacing = True
                page.locator('#apply').click()
                expect(page.locator('#review')).to_be_visible()
                old_container = browser_editor.runtime(state)['container']
                page.locator('#apply').click()
                expect(page.locator('#status')).to_contain_text('applying')
                page.reload()
                expect(page.locator('#editor')).to_have_attribute('src',editor,timeout=120000)
                page.locator('#handle').click()
                page.locator('#settings').click()
                if page.locator('#categories').is_visible(): page.locator('[data-section=network]').click()
                expect(page.locator('#status')).to_have_text('Network mode updated.',timeout=300000)
                frame.locator('.monaco-workbench').wait_for(timeout=120000)
                current = browser_editor.runtime(state)
                assert current['container'] != old_container
                assert current['url'] == editor
                assert browser_shell.origin(state) == origin
                assert request(editor,headers={'Cookie':cookie})[0] == 200
                assert json.loads((state/'config.json').read_text())['network'] == mode
                page.locator('#cancel').click()
                chat()
                replacing = False
                passed(f'{mode}: real Apply, reload during job, original addresses and authenticated editor preserved')
                if mode == 'unrestricted':
                    # A real package-manager error remains visible, and retry
                    # uses the same running container without reloading chat.
                    section('packages')
                    package_container = browser_editor.runtime(state)['container']
                    package_document = page.frames[1].evaluate('performance.timeOrigin')
                    page.locator('#packages').fill('factory-package-that-does-not-exist')
                    page.locator('#apply').click()
                    expect(page.locator('#review')).to_be_visible()
                    expect(page.locator('#effect')).to_contain_text('root inside this sandbox')
                    page.locator('#apply').click()
                    expect(page.locator('#status')).to_contain_text('Install system packages failed',timeout=300000)
                    page.locator('#job-output summary').click()
                    expect(page.locator('#job-output')).to_contain_text('unavailable in the configured APT repositories')
                    page.screenshot(path=str(output/'package-error.png'))
                    page.locator('#packages').fill('hello')
                    apply('Packages installed.')
                    current = json.loads((state/'config.json').read_text())
                    installed = sandbox.run(['podman','exec',sandbox.names(current)[0],'hello'],capture=True).stdout
                    assert 'Hello, world!' in installed, installed
                    assert browser_editor.runtime(state)['container'] == package_container
                    assert page.frames[1].evaluate('performance.timeOrigin') == package_document
                    passed('package failure diagnostics, corrected retry and real installed executable')

            section('destinations')
            container = browser_editor.runtime(state)['container']
            document = page.frames[1].evaluate('performance.timeOrigin')
            page.locator('#allowlist').fill('example.com:443')
            apply('Allowed destinations updated.')
            assert (state/'allowlist.txt').read_text().strip() == 'example.com:443'
            assert browser_editor.runtime(state)['container'] == container
            assert page.frames[1].evaluate('performance.timeOrigin') == document
            passed('allowed destinations update the gateway without replacing or reloading the editor')

            section('resources')
            memory = page.locator('#memory').input_value()
            page.locator('#memory').fill('not-a-memory-size')
            page.locator('#apply').click()
            expect(page.locator('#status')).to_contain_text('memory',ignore_case=True)
            expect(page.locator('#review')).to_be_hidden()
            page.locator('#memory').fill(memory)
            for source in ('local','prebuilt'):
                page.locator('#image_source').select_option(source)
                apply('Image setup saved for future builds.')
                assert browser_editor.runtime(state)['container'] == container
                assert page.frames[1].evaluate('performance.timeOrigin') == document
            passed('invalid resources are explained; image-source changes save without interrupting the editor')

            section('approvals')
            page.locator('#approval').select_option('manual')
            replacing = True
            apply('Approvals & sandbox updated.')
            assert json.loads((state/'config.json').read_text())['options']['approval'] == 'manual'
            assert browser_editor.runtime(state)['url'] == editor
            page.locator('#cancel').click()
            chat()
            replacing = False
            passed('approval mode applies through real replacement and FrostPi remains usable')

            section('features')
            for width,height in ((1440,900),(820,650),(390,500)):
                page.set_viewport_size({'width':width,'height':height})
                expect(page.locator('#apply')).to_be_in_viewport()
                expect(page.locator('#close')).to_be_in_viewport()
                page.screenshot(path=str(output/f'features-{width}.png'))
            page.set_viewport_size({'width':1440,'height':900})
            page.locator('#goals').uncheck()
            replacing = True
            apply('Pi features updated.')
            assert json.loads((state/'config.json').read_text())['options']['goals'] is False
            assert browser_editor.runtime(state)['url'] == editor
            page.locator('#cancel').click()
            chat()
            replacing = False
            passed('Pi feature change applies, long forms keep actions visible, and FrostPi recovers')

            # Real CLI refusal: another owner changes the saved settings after review.
            section('network')
            page.locator('#network').select_option('unrestricted')
            page.locator('#apply').click()
            expect(page.locator('#review')).to_be_visible()
            saved = (state/'config.json').read_text()
            changed = json.loads(saved)
            changed['memory'] = '4g'
            sandbox.write_json(state/'config.json',changed)
            page.locator('#apply').click()
            expect(page.locator('#status')).to_contain_text('changed after review')
            (state/'config.json').write_text(saved)
            page.locator('#apply').click()
            expect(page.locator('#review')).to_be_visible()
            passed('stale review refuses Apply; correcting state permits a fresh review')
            page.screenshot(path=str(output/'complete.png'))
            assert not errors, errors
            assert token not in (state/'browser-controls/job.log').read_text()
            passed('no unexpected page errors or control token in lifecycle output; fresh FrostPi checked after every replacement')
            (output/'restart-fetch-errors.json').write_text(json.dumps(restart_errors,indent=2)+'\n')
        except BaseException:
            page.screenshot(path=str(output/'failure.png'))
            (output/'page-errors.json').write_text(json.dumps({'errors':errors,'restart_errors':restart_errors},indent=2))
            for index,child in enumerate(page.frames):
                try:
                    (output/f'frame-{index}.txt').write_text(child.url+'\n'+child.locator('body').inner_text(timeout=5000))
                except Exception:
                    pass
            raise
        finally:
            browser.close()
    return results


def exercise_native(state,output,*,headed=False):
    config=json.loads((state/'config.json').read_text())
    agent=config['primary_agent']
    runtime=browser_editor.runtime(state)
    editor=runtime['url']
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=not headed)
        page=browser.new_page(viewport={'width':1440,'height':900})
        try:
            page.goto(browser_shell.control(state,'pair'))
            frame=page.frame_locator('#editor')
            frame.locator('input[name=password]').fill('synthetic-toolbar-test-password')
            frame.get_by_role('button',name='Submit').click()
            frame.locator('.monaco-workbench').wait_for(timeout=120000)
            extension={'codex':'openai.chatgpt','claude':'anthropic.claude-code','agy':'google.google-antigravity'}[agent]
            deadline=time.monotonic()+120
            while not any(f'extensionid={extension}' in child.url.lower() for child in page.frames) and time.monotonic()<deadline:
                page.wait_for_timeout(100)
            assert any(f'extensionid={extension}' in child.url.lower() for child in page.frames),[child.url for child in page.frames]
            page.locator('#handle').click();page.locator('#settings').click()
            page.locator('[data-section=approvals]').click()
            page.locator('#native_approval').select_option('manual')
            page.locator('#apply').click()
            expect(page.locator('#effect')).to_contain_text('restart this sandbox')
            page.locator('#apply').click()
            expect(page.locator('#status')).to_have_text('Settings saved. Restart the sandbox to use them.',timeout=60000)
            assert json.loads((state/'config.json').read_text())['options']['native_approval']=='manual'
            assert browser_editor.runtime(state)['container']==runtime['container']
            page.locator('#section-back').click();page.locator('[data-section=web-search]').click()
            page.locator('#'+agent+'_web_search').check()
            page.locator('#apply').click();expect(page.locator('#review')).to_be_visible();page.locator('#apply').click()
            expect(page.locator('#status')).to_have_text('Web search saved. Restart the selected agent to use it.',timeout=60000)
            assert json.loads((state/'config.json').read_text())['options'][agent+'_web_search'] is True
            assert browser_editor.runtime(state)['container']==runtime['container']
            assert browser_editor.runtime(state)['url']==editor
            page.screenshot(path=str(output/(agent+'-settings.png')))
            return [f'{agent}: real extension opens inside toolbar; approvals and web search save with required restart notices; editor stays running']
        except BaseException:
            import traceback
            traceback.print_exc()
            page.screenshot(path=str(output/'failure.png'))
            (output/'frames.json').write_text(json.dumps([child.url for child in page.frames],indent=2))
            raise
        finally:
            browser.close()


def run(output, headed=False,agent='pi'):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='factory-toolbar-test-') as temporary:
        root=Path(temporary)
        project=root/'project'; project.mkdir()
        (project/'README.md').write_text('# Toolbar test\nDisposable workspace.\n')
        name='toolbar-test-'+uuid4().hex[:12]
        state=root/'state'/name
        request_file=root/'create.json'
        sandbox.write_json(request_file,{'profile':'base','workspace':str(project),'primary_agent':agent,'primary_auth':'login','engine':'podman',
            'network':'allowlist','image_source':'prebuilt','subagents':False,
            'options':{'browser':False,'website_groups':[]},'editor':{'mode':'browser'}})
        def cli(*args, **kwargs):
            return subprocess.run([sys.executable,'-m','ai_sandbox_factory','--state-dir',str(state.parent),*args],check=True,**kwargs)
        launcher = None
        try:
            cli('create',name,'--config',str(request_file),'--start')
            cli('editor','set',name,'--password-stdin',input='synthetic-toolbar-test-password\n',text=True)
            with (output/'launcher.log').open('w') as log:
                launcher=subprocess.Popen([sys.executable,'-m','ai_sandbox_factory','--state-dir',str(state.parent),
                    'browser-editor',name,'--no-open'],stdout=log,stderr=log)
            deadline=time.monotonic()+120
            while not browser_editor.runtime(state) and launcher.poll() is None and time.monotonic()<deadline:
                time.sleep(.1)
            assert browser_editor.runtime(state), (output/'launcher.log').read_text()
            results=(exercise if agent=='pi' else exercise_native)(state,output,headed=headed)
            assert launcher.poll() is None,'Foreground launcher exited during a settings replacement'
            (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            if agent!='pi': print('PASS '+results[0],flush=True)
        finally:
            if (state/'config.json').exists():
                browser_shell.stop(state)
                browser_editor.stop(state)
                sandbox.delete(json.loads((state/'config.json').read_text()),state)
            if launcher:
                launcher.wait(timeout=15)
                assert launcher.returncode == 0, (output/'launcher.log').read_text()
            for resource in (['ps','-aq'],['volume','ls','-q'],['network','ls','-q']):
                remaining=subprocess.check_output(['podman',*resource,'--filter',f'label={sandbox.LABEL}={name}'],text=True)
                assert not remaining.strip(),remaining
            print('PASS disposable containers, volumes and networks removed',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--headed',action='store_true')
    parser.add_argument('--agent',choices=('pi','codex','claude','agy'),default='pi')
    args=parser.parse_args()
    run(args.output,args.headed,args.agent)
