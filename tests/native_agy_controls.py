"""Exercise the official Antigravity model picker in the browser editor."""
import json
import re
import secrets
import time

from playwright.sync_api import expect


def check(page, editor, output, timeout):
    from native_browser_editor import verify_reply
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = next((f for f in page.frames if f.get_by_role('button', name=re.compile('^Select model, current:')).count()), None)
        if frame:
            break
        page.wait_for_timeout(200)
    else:
        raise AssertionError('Native Antigravity model picker was not visible')
    picker = frame.get_by_role('button', name=re.compile('^Select model, current:'))
    original = picker.inner_text()
    notices = page.locator('.notification-list-item-message')
    if notices.count():
        (output / 'editor-notifications.json').write_text(json.dumps(notices.all_text_contents()))
        page.keyboard.press('Control+Shift+P')
        command = page.locator('.quick-input-widget input').first
        command.fill('>Notifications: Clear All Notifications')
        command.press('Enter')
    picker.click()
    (output / 'agy-model-picker.txt').write_text(frame.locator('body').inner_text())
    (output / 'agy-model-picker.html').write_text(frame.locator('body').inner_html())
    page.screenshot(path=str(output / 'agy-model-picker.png'))
    choices = frame.get_by_role('menu').get_by_role('menuitem')
    expect(choices.first).to_be_visible()
    available = [choices.nth(i) for i in range(choices.count()) if choices.nth(i).is_enabled()
                 and choices.nth(i).get_attribute('aria-disabled') != 'true'
                 and not original.strip().startswith(choices.nth(i).inner_text().splitlines()[0].strip())
                 and 'Leaving Soon' not in choices.nth(i).inner_text()
                 and choices.nth(i).inner_text().strip() != 'View Usage']
    assert available, 'No second model offered by the native picker'
    selected = available[0]
    chosen = selected.inner_text().splitlines()[0].strip()
    selected.click()
    expect(picker).not_to_have_text(original)
    receipt = 'agy-model-' + secrets.token_hex(8)
    verify_reply(page, f'Reply with exactly {receipt}. Do not use tools.', receipt, timeout, output)
    page.screenshot(path=str(output / 'agy-model-selected.png'))
    (output / 'agy-controls.json').write_text(json.dumps({'before': original, 'selected': chosen, 'receipt': receipt}))
    print('Native Antigravity model selection completed a real reply.', flush=True)
