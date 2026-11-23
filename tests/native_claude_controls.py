"""Use Claude's official model, effort, approval and attachment controls."""
import base64
import json
import re
import secrets
import time
import subprocess

from playwright.sync_api import Error as BrowserError, expect

BLUE_PNG = 'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NQQkAAAgEsCtpODsZyhQ+hMH+S/WcikAgEAgEAoFAIPgSLCQx6Fu1gaoXAAAAAElFTkSuQmCC'


def composer_frame(page, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.parent_frame and frame.locator('fieldset[data-permission-mode]').count() and frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible').count():
                    return frame
            except BrowserError:
                continue
        page.wait_for_timeout(200)
    raise AssertionError('Native Claude composer did not appear')


def permissions(frame, label):
    frame.locator('button[title*="Click to change, or press Shift+Tab"]').click()
    frame.get_by_role('button', name=re.compile('^' + re.escape(label) + r'\s+Claude\b')).click()
    expect(frame.locator('fieldset[data-permission-mode]')).to_have_attribute(
        'data-permission-mode', {'Manual': 'default', 'Auto': 'auto', 'Plan': 'plan'}[label])


def model_picker(frame):
    return frame.locator('button[role=combobox][title^="Switch model"]')


def check(page, editor, output, timeout):
    from native_browser_editor import podman, verify_reply

    frame = composer_frame(page, timeout)
    expect(frame.locator('fieldset[data-permission-mode]')).to_have_attribute('data-permission-mode', 'auto')
    page.screenshot(path=str(output / 'claude-initial-automatic.png'))
    model_picker(frame).click()
    models = frame.get_by_role('listbox', name='Select a model')
    expect(models).to_be_visible()
    choices = models.locator('[role=option]:not([aria-disabled=true])')
    # Prefer an available Sonnet entry for this control check. Selecting one
    # supported model verifies the actual picker, without a model comparison.
    sonnet = choices.filter(has_text=re.compile(r'Sonnet', re.I))
    selected = sonnet.first if sonnet.count() else choices.first
    expect(selected).not_to_have_attribute('aria-disabled', 'true')
    chosen = selected.inner_text().splitlines()[0]
    selected.click()
    expect(model_picker(frame)).to_contain_text(chosen)
    model_picker(frame).click()
    effort = frame.get_by_title('Click or drag to set effort level', exact=True)
    expect(effort).to_be_visible()
    box = effort.bounding_box()
    assert box, 'Native effort control has no rendered bounds'
    effort.click(position={'x': 1, 'y': box['height'] / 2})
    page.screenshot(path=str(output / 'claude-model-effort.png'))
    model_picker(frame).click()
    expect(model_picker(frame)).to_contain_text('Low')
    receipts = []
    for mode, filename in [('Manual', 'manual'), ('Auto', 'automatic')]:
        permissions(frame, mode)
        receipt = 'claude-controls-' + secrets.token_hex(10)
        path = '/workspace/native-claude-controls-' + filename + '.txt'
        podman('exec', '-i', editor, 'tee', path, payload=receipt)
        verify_reply(page, f'Use your native Bash tool to run cat {path} and reply with the file contents.',
                     receipt, timeout, output)
        page.screenshot(path=str(output / ('claude-' + filename + '-tool.png')))
        receipts.append({'mode': mode, 'receipt': receipt})
    (output / 'claude-controls.json').write_text(json.dumps({'model': chosen, 'effort': 'Low',
                                                           'completed_tools': receipts}, indent=2))
    print('Native Claude model, effort and both approval modes completed workspace tools.', flush=True)


def check_attachments(page, output, timeout):
    from native_browser_editor import verify_reply

    frame = composer_frame(page, timeout)
    uploads = frame.locator('input[type=file]')
    expect(uploads).to_have_count(1)
    uploads.set_input_files({'name': 'native-color.png', 'mimeType': 'image/png',
                             'buffer': base64.b64decode(BLUE_PNG)})
    expect(frame.get_by_title('Remove attachment', exact=True)).to_be_visible()
    page.screenshot(path=str(output / 'claude-image-ready.png'))
    verify_reply(page, 'Describe the dominant color of the attached image in one lowercase word. Do not use tools.',
                 'blue', timeout, output)
    page.screenshot(path=str(output / 'claude-image-reply.png'))
    print('Native Claude identified an uploaded PNG through its official composer.', flush=True)


def check_browser(page, editor, output, timeout):
    from native_browser_editor import verify_reply
    from ai_sandbox_factory.browser_viewer import ensure_viewer

    identifier = subprocess.check_output(['podman', 'inspect', '--format', '{{.Id}}', editor], text=True).strip()
    # Match Factory's ordinary startup: the native browser tool returns the
    # authenticated viewer for its Chromium session after each successful call.
    ensure_viewer('podman', identifier)
    receipt = 'native-browser-' + secrets.token_hex(10)
    title = 'Factory native browser ' + secrets.token_hex(5)
    verify_reply(page,
        'Use the factory_workspace browser MCP tool to control Chromium. '
        f'Run await page.setContent({json.dumps("<title>" + title + "</title><main id=receipt>" + receipt + "</main>")}); '
        'then return {title: await page.title(), receipt: await page.locator("#receipt").innerText()}. '
        'Reply with the title and receipt returned by the browser. Do not use shell or file tools.',
        receipt, timeout, output)
    inspect = '''import json
from pathlib import Path
rows=[]
for p in (Path.home()/'.claude/projects').rglob('*.jsonl'):
 for line in p.read_text().splitlines():
  try: item=json.loads(line)
  except ValueError: continue
  message=item.get('message',{})
  if not isinstance(message,dict): continue
  content=message.get('content',[])
  if isinstance(content,list): rows.extend(b for b in content if isinstance(b,dict) and b.get('type') in ('tool_use','tool_result'))
print(json.dumps(rows))
'''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(['podman', 'exec', editor, 'python3', '-c', inspect],
                                capture_output=True, text=True, check=True)
        rows = json.loads(result.stdout)
        calls = [row for row in rows if row.get('type') == 'tool_use' and
                 row.get('name', '').endswith('__browser') and receipt in json.dumps(row.get('input'))]
        results = [row for row in rows if row.get('type') == 'tool_result' and
                   row.get('tool_use_id') in {call['id'] for call in calls}]
        observed = json.dumps(results)
        if calls and results and receipt in observed and title in observed:
            assert all(not row.get('is_error') for row in results), 'Native managed browser tool returned an error'
            code = json.dumps([call.get('input', {}) for call in calls])
            assert 'page.setContent' in code and 'page.title' in code, 'The browser tool did not exercise Chromium page methods'
            (output / 'claude-browser-tool.json').write_text(json.dumps({'calls': calls, 'results': results}, indent=2))
            page.screenshot(path=str(output / 'claude-browser-tool.png'))
            print('Native Claude used the managed Chromium browser and read rendered title and contents.', flush=True)
            return
        page.wait_for_timeout(200)
    raise AssertionError('Native Claude history contains no successful Chromium browser tool result for the fixture')


def reload_window(page, timeout, *, wait_for_composer=True):
    # Reload the extension host, rather than opening the same saved chat again.
    page.get_by_role('tab', name='Explorer (Ctrl+Shift+E)', exact=True).click()
    page.keyboard.press('Control+Shift+P')
    palette = page.get_by_role('textbox', name='Type the name of a command to run.', exact=True)
    palette.fill('>Developer: Reload Window')
    with page.expect_navigation(wait_until='domcontentloaded', timeout=timeout * 1000):
        palette.press('Enter')
    return composer_frame(page, timeout) if wait_for_composer else None


def fresh_conversation(frame, selected_model):
    composer = frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible').last
    composer.fill('/clear')
    # This is the native local command; it starts a fresh chat in the same panel
    # without sending a model request or reopening a concurrently held chat.
    frame.get_by_text('Clear conversation', exact=True).click()
    expect(composer).to_be_empty()
    picker = model_picker(frame)
    if selected_model not in picker.inner_text():
        picker.click()
        models = frame.get_by_role('listbox', name='Select a model')
        models.locator('[role=option]:not([aria-disabled=true])').filter(has_text=selected_model).first.click()
    expect(picker).to_contain_text(selected_model)


def search_records(editor, marker):
    source = '''import json,sys
from pathlib import Path
marker=sys.argv[1]
for path in (Path.home()/'.claude/projects').rglob('*.jsonl'):
 records=[]
 for line in path.read_text().splitlines():
  try: records.append(json.loads(line))
  except ValueError: continue
 start=next((i for i,row in enumerate(records) if row.get('type')=='user' and marker in json.dumps(row.get('message',{}))),None)
 if start is None: continue
 blocks=[]
 for row in records[start:]:
  message=row.get('message',{})
  if not isinstance(message,dict): continue
  content=message.get('content',[])
  if isinstance(content,list): blocks.extend(b for b in content if isinstance(b,dict))
 calls=[b for b in blocks if b.get('type')=='tool_use' and b.get('name')=='WebSearch']
 results=[b for b in blocks if b.get('type')=='tool_result' and b.get('tool_use_id') in {c['id'] for c in calls}]
 print(json.dumps({'session_record':path.name,'calls':calls,'results':results}));break
else: print('{}')
'''
    result = subprocess.check_output(['podman', 'exec', editor, 'python3', '-c', source, marker], text=True)
    return json.loads(result)


def check_web_search(page, editor, output, timeout):
    """Run last: fresh chats intentionally isolate the two search settings."""
    from native_browser_editor import podman, verify_reply

    frame = composer_frame(page, timeout)
    selected = model_picker(frame).get_attribute('title') or ''
    match = re.fullmatch(r'Switch model \((.+)\)', selected)
    selected_model = match.group(1) if match else re.sub(r'\s+(?:Low|Medium|High|Extra high|Max)$', '', model_picker(frame).inner_text())
    original = json.loads(subprocess.check_output(['podman', 'exec', editor, 'cat',
                                                  '/run/primary-runtime/settings.json'], text=True))
    evidence = []
    try:
        for enabled in (False, True):
            # Leave the completed chat before restarting its extension host.
            # Reloading an actively owned saved chat can invoke the official
            # ownership handoff; that is exercised separately by the harness.
            fresh_conversation(frame, selected_model)
            chosen = {**original, 'claude_web_search': enabled}
            podman('exec', '-i', editor, 'tee', '/run/primary-runtime/settings.json', payload=json.dumps(chosen))
            podman('exec', editor, 'python3', '/opt/primary/claude_client.py', '--factory-prepare')
            frame = reload_window(page, timeout)
            fresh_conversation(frame, selected_model)
            native = json.loads(subprocess.check_output(['podman', 'exec', editor, 'cat',
                                                        '/home/node/.claude/settings.json'], text=True))
            assert ('WebSearch' in native.get('permissions', {}).get('deny', [])) is not enabled
            marker = 'native-search-' + secrets.token_hex(10)
            prompt = ('Use the native WebSearch tool once to find the official Claude Code documentation. '
                      'Do not use WebFetch, shell, MCP or browser. Then reply ' + marker + '.') if enabled else (
                      'If the native WebSearch tool is available, call it once to find the official Claude Code documentation. '
                      'If that tool is unavailable or denied, do not use another tool. Reply ' + marker + '.')
            verify_reply(page, prompt, marker, timeout, output)
            deadline = time.monotonic() + timeout
            while True:
                records = search_records(editor, marker)
                if records and (not enabled or records.get('results')):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError('Native search transcript was not saved after the completed reply')
                page.wait_for_timeout(200)
            successes = [row for row in records.get('results', []) if not row.get('is_error')]
            if enabled:
                assert records.get('calls') and successes, 'Enabled native WebSearch completed no successful search'
            else:
                assert not successes, 'Disabled native WebSearch completed a successful search'
            evidence.append({'enabled': enabled, 'native_permissions': native.get('permissions'), **records})
            page.screenshot(path=str(output / ('claude-search-enabled.png' if enabled else 'claude-search-disabled.png')))
            print(f'Native Claude editor WebSearch {"enabled" if enabled else "disabled"} verified from native config and transcript.', flush=True)
    finally:
        podman('exec', '-i', editor, 'tee', '/run/primary-runtime/settings.json', payload=json.dumps(original))
        podman('exec', editor, 'python3', '/opt/primary/claude_client.py', '--factory-prepare')
        if not frame.is_detached() and frame.locator('textarea:visible, [contenteditable]:not([contenteditable=false]):visible').count():
            fresh_conversation(frame, selected_model)
        reload_window(page, timeout)
    (output / 'claude-search-settings.json').write_text(json.dumps(evidence, indent=2))
