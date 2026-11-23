"""Send the advisor fixture through FrostPi's browser chat or VS Code extension."""
import fcntl
import time
import subprocess

from playwright.sync_api import Error, expect, sync_playwright

from ai_sandbox_factory import browser_editor, editor_settings


def check_image(container, root, mode, chromium):
    state = root / ('editor-' + mode)
    state.mkdir()
    container = subprocess.check_output(['podman','inspect','--format','{{.Id}}',container],text=True).strip()
    settings = editor_settings.validate({'mode': mode})
    with (state / 'browser-editor.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            browser_editor.start_background(container, '/workspace', settings, state, False, lock.fileno())
        except ValueError:
            print((state / 'browser-editor.log').read_text(),flush=True)
            raise
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium)
            page = browser.new_page()
            page.goto(browser_editor.runtime(state)['url'])
            if mode == 'frostpi':
                page.get_by_role('button', name='New session').click()
                frame = page
            else:
                page.locator('.monaco-workbench').wait_for(timeout=60000)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        frame = next((f for f in page.frames if f.get_by_label('Message Pi',exact=True).count()), None)
                    except Error as error:
                        if 'Frame was detached' not in str(error):
                            raise
                        frame = None
                    if frame:
                        break
                    page.wait_for_timeout(150)
                else:
                    raise AssertionError('FrostPi chat did not open')
            frame.get_by_label('Message Pi', exact=True).fill('Attach advisor-image.png and describe it.')
            frame.get_by_role('button',name='Send to Pi',exact=True).click()
            expect(frame.get_by_text('ADVISOR_IMAGES_DONE',exact=True)).to_be_visible(timeout=60000)
            browser.close()
            print('PASS FrostPi advisor image and model-choice tools in', mode, flush=True)
    finally:
        browser_editor.stop(state)
