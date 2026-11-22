"""Exercise uploaded bytes through the actual Antigravity chat composer."""
import base64
import json
import secrets
import time
from playwright.sync_api import expect
from native_browser_editor import verify_reply


def check(page, output, timeout):
    # No copy of this receipt is mounted into either container.
    receipt = 'attached-' + secrets.token_hex(10)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in page.frames:
            uploads = frame.locator('input[type=file]')
            if not uploads.count():
                continue
            uploads.first.set_input_files({'name': 'field-notes.txt', 'mimeType': 'text/plain',
                                          'buffer': ('Receipt: ' + receipt).encode()})
            expect(frame.get_by_role('button', name='Remove field-notes.txt')).to_be_visible(timeout=15000)
            page.screenshot(path=str(output/'attachment-ready.png'))
            verify_reply(page, 'Read the attached field notes and reply with its receipt. Do not use tools.',
                         receipt, timeout, output)
            page.screenshot(path=str(output/'attachment-reply.png'))
            blue = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NQQkAAAgEsCtpODsZyhQ+hMH+S/WcikAgEAgEAoFAIPgSLCQx6Fu1gaoXAAAAAElFTkSuQmCC')
            uploads.first.set_input_files({'name':'color.png', 'mimeType':'image/png', 'buffer':blue})
            expect(frame.get_by_role('button', name='Remove color.png')).to_be_visible(timeout=15000)
            verify_reply(page, 'Describe the dominant color of the attached image in one lowercase word. Do not use tools.',
                         'blue', timeout, output)
            page.screenshot(path=str(output/'image-reply.png'))
            print('Antigravity read an uploaded file and identified an uploaded image through its native composer.', flush=True)
            return
        page.screenshot(path=str(output/'attachment-waiting.png'))
        (output/'attachment-controls.json').write_text(json.dumps([{'url': f.url.split('?')[0], 'buttons': f.get_by_role('button').all_text_contents(), 'inputs': f.locator('input').evaluate_all('(nodes) => nodes.map(n => ({type:n.type,accept:n.accept}))')} for f in page.frames]))
        page.wait_for_timeout(250)
    raise AssertionError('Native attachment input was not found')
