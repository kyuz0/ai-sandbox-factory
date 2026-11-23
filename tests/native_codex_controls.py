"""Exercise native Codex controls and clipboard images in a disposable editor."""
import re
import secrets
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError, expect

BLUE_PNG = 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NQQkAAAgEsCtpODsZyhQ+hMH+S/WcikAgEAgEAoFAIPgSLCQx6Fu1gaoXAAAAAElFTkSuQmCC'


def composer_frame(page, timeout):
    origin = urlsplit(page.url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.is_detached():
                    continue
                location = urlsplit(frame.url)
                # Native VS Code content is in fake.html. Its remote DIL runner
                # is a separate document and can still be loading independently.
                if ((location.scheme, location.netloc) != (origin.scheme, origin.netloc)
                        or not location.path.endswith('/webview/browser/pre/fake.html')):
                    continue
                if frame.locator('[contenteditable=true]:visible').count():
                    return frame
                if frame.parent_frame:
                    for label in ('Next', 'Continue', 'Skip', 'Get started', 'Start coding'):
                        button = frame.get_by_role('button', name=label, exact=True)
                        if button.count() == 1 and button.is_visible():
                            button.click(timeout=1000)
                            break
            except BrowserError:
                # The native extension replaces its onboarding webview.
                continue
        page.wait_for_timeout(200)
    raise AssertionError('Native Codex composer did not appear')


def permissions(frame, label):
    frame.get_by_role('button', name='Change permissions', exact=True).click()
    frame.get_by_role('menuitem', name=re.compile('^' + re.escape(label) + r'(?:\s|$)')).click()


def model_picker(frame):
    # The native responsive layout replaces the icon's accessible label with
    # the selected model name when the panel is wide enough.
    return frame.get_by_role('button', name=re.compile(r'^(?:Select (?:model|effort)$|(?:GPT-)?[56]\.)'))


def check(page, editor, output, timeout):
    from native_browser_editor import podman, verify_reply

    frame = composer_frame(page, timeout)
    frame.get_by_role('button', name='Change permissions', exact=True).click()
    automatic = frame.get_by_role('menuitem', name=re.compile('^Approve for me'))
    expect(automatic).to_be_visible()
    expect(automatic.locator('svg[aria-hidden=false]')).to_be_visible()
    page.screenshot(path=str(output / 'codex-initial-automatic.png'))
    frame.get_by_role('menu', name='Change permissions', exact=True).press('Escape')
    model_picker(frame).click()
    frame.get_by_role('menuitem', name='Select model', exact=True).click()
    frame.get_by_role('menuitemradio', name=re.compile(r'^(?:GPT-)?6\.1 Sol$')).click()
    # Selecting a model returns to the native model/power menu.
    expect(frame.get_by_role('menuitem', name='Select model', exact=True)).to_contain_text('6.1 Sol')
    power = frame.get_by_role('menuitem', name='Power', exact=True)
    before = frame.get_by_role('status').last.inner_text()
    power.focus()
    power.press('ArrowRight' if '4 of 4' not in before else 'ArrowLeft')
    expect(frame.get_by_role('status').last).not_to_have_text(before)
    page.screenshot(path=str(output / 'codex-model-power.png'))
    power.press('Escape')
    for mode, filename in [('Ask for approval', 'manual'), ('Approve for me', 'automatic')]:
        permissions(frame, mode)
        receipt = 'controls-' + secrets.token_hex(10)
        path = '/workspace/native-controls-' + filename + '.txt'
        podman('exec', '-i', editor, 'tee', path, payload=receipt)
        verify_reply(page, f'Run cat {path} and reply with its contents.', receipt, timeout, output)
        page.screenshot(path=str(output / ('codex-' + filename + '-tool.png')))
    print('Native Codex model, reasoning power and both approval modes completed workspace tools.', flush=True)
    check_full_access(page, output, timeout)


def check_attachments(page, output, timeout):
    from native_browser_editor import verify_reply

    frame = composer_frame(page, timeout)
    frame.locator('[contenteditable=true]:visible').last.evaluate('''(node, encoded) => {
        const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], 'clipboard-color.png', {type: 'image/png'}));
        node.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: transfer, bubbles: true, cancelable: true}));
    }''', BLUE_PNG)
    expect(frame.get_by_role('button', name=re.compile('Remove.*clipboard-color', re.I))).to_be_visible()
    page.screenshot(path=str(output / 'codex-clipboard-ready.png'))
    verify_reply(page, 'Describe the dominant color of the attached image in one lowercase word. Do not use tools.',
                 'blue', timeout, output)
    page.screenshot(path=str(output / 'codex-clipboard-reply.png'))
    print('Native Codex identified a clipboard PNG through its composer.', flush=True)


def check_full_access(page, output, timeout):
    from native_browser_editor import verify_reply

    frame = composer_frame(page, timeout)
    permissions(frame, 'Full access')
    frame.get_by_role('dialog', name='Turn on Full Access?').get_by_role('button', name='Confirm', exact=True).click()
    receipt = 'full-access-' + secrets.token_hex(10)
    verify_reply(page, f'Run printf {receipt} and reply with its output.', receipt, timeout, output)
    page.screenshot(path=str(output / 'codex-full-access.png'))
    permissions(frame, 'Approve for me')
    print('Native Codex Full access completed a command; restored Approve for me.', flush=True)
