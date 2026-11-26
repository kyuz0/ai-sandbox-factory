"""Real VS Code file-menu references and their model-visible prompt, using synthetic files."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect

REFERENCE_PATH = 'reference docs/Quarterly Observatory Instrument Calibration Notes.tex'
BODY_CANARY = 'REFERENCE_BODY_MUST_NOT_BE_SENT_624'


def check(page, frame, inside, output, cwd):
    source = Path(cwd) / REFERENCE_PATH
    content = 'First line\n' + BODY_CANARY + '\nThird line\n'
    inside('python3', '-c', 'from pathlib import Path; p=Path(' + repr(str(source))
           + '); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(' + repr(content) + ')')
    composer = frame.get_by_label('Message Pi', exact=True)
    url = urlsplit(page.url)
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=f'{url.scheme}://{url.netloc}')

    def open_file():
        page.keyboard.press('Control+p')
        quick = page.locator('.quick-input-widget input')
        expect(quick).to_be_visible()
        quick.fill(str(source))
        option = page.get_by_role('option', name=re.compile(re.escape(Path(REFERENCE_PATH).name))).first
        expect(option).to_be_visible(timeout=15000)
        option.click()
        expect(quick).not_to_be_visible()
        expect(page.locator('.monaco-editor .view-line').filter(has_text=BODY_CANARY)).to_be_visible()

    def mention(menu):
        line = page.locator('.monaco-editor .view-line').filter(has_text=BODY_CANARY)
        line.click(button='right', position={'x':8, 'y':8})
        item = page.get_by_role('menuitem', name=re.compile(re.escape(menu)))
        expect(item).to_be_visible()
        # VS Code enables menu mouse-up handlers 100 ms after rendering to
        # avoid treating the opening right-click as an item activation.
        item.click(delay=150)
        expect(item).not_to_be_visible()

    def submit(kind, path):
        chip = frame.get_by_role('button', name='Edit file reference ' + path, exact=True)
        expect(chip).to_be_visible()
        composer.focus()
        page.keyboard.press('Control+Home')
        page.keyboard.insert_text('Factory file reference fixture ' + kind + ' ')
        frame.get_by_role('button', name='Send to Pi', exact=True).click()
        expect(frame.get_by_text('FACTORY FILE REFERENCE ' + kind + ' VERIFIED', exact=True)).to_be_visible(timeout=30000)
        bubble = frame.locator('.user-bubble').filter(has_text='Factory file reference fixture ' + kind).last
        tag = bubble.locator('.message-file-reference')
        expect(tag).to_have_attribute('title', path)
        assert 'ref:' not in bubble.inner_text(), bubble.inner_text()
        bubble.screenshot(path=str(output / f'sent-{kind}-reference.png'))
        expect(frame.get_by_role('button', name='Send to Pi', exact=True)).to_be_disabled()

    # Exercise the actual editor selection and contextual commands, not direct bridge calls.
    open_file()
    page.locator('.monaco-editor .view-line').filter(has_text=BODY_CANARY).click()
    page.keyboard.press('Home')
    page.keyboard.press('Shift+ArrowDown')
    mention('FrostPi: Mention Selection Path in Prompt')
    selected = REFERENCE_PATH + ':2-3'
    chip = frame.get_by_role('button', name='Edit file reference ' + selected, exact=True)
    expect(chip).to_be_visible()
    expect(chip).to_have_text('Quarterly Observatory Instrument Calibration Notes.tex:2-3')
    expect(chip.locator('.file-reference-lines')).to_have_text(':2-3')
    page.screenshot(path=str(output / 'selection-reference.png'))
    submit('selection', selected)
    user = frame.locator('.message-user').filter(has_text='Factory file reference fixture selection').last
    user.hover()
    user.get_by_role('button', name='Copy user message', exact=True).click()
    expected_raw = 'Factory file reference fixture selection ref:`' + selected + '`'
    page.wait_for_function('expected => navigator.clipboard.readText().then(text => text.trim() === expected)', arg=expected_raw)

    # Copy a selected answer through VS Code's webview context menu, then read
    # the browser clipboard used when pasting into another host application.
    answer = frame.get_by_text('FACTORY FILE REFERENCE selection VERIFIED', exact=True)
    answer.scroll_into_view_if_needed()
    composer.focus()
    lines = answer.evaluate('''element => {
      const range=document.createRange(); range.selectNodeContents(element);
      return [...range.getClientRects()].map(r => ({x:r.x,y:r.y,right:r.right,height:r.height}));
    }''')
    frame_bounds = frame.frame_element().bounding_box()
    first, last = lines[0], lines[-1]
    page.mouse.move(frame_bounds['x'] + first['x'], frame_bounds['y'] + first['y'] + first['height']/2)
    page.mouse.down()
    page.mouse.move(frame_bounds['x'] + last['right'], frame_bounds['y'] + last['y'] + last['height']/2, steps=15)
    page.mouse.up()
    selected_answer = frame.evaluate('window.getSelection().toString()')
    assert selected_answer.strip() == 'FACTORY FILE REFERENCE selection VERIFIED', selected_answer
    assert frame.evaluate('document.activeElement.classList.contains("conversation")'), 'Selecting an answer left focus on the Composer'
    page.evaluate('navigator.clipboard.writeText("FACTORY CLIPBOARD SENTINEL")')
    answer.click(button='right')
    copy = page.get_by_role('menuitem', name=re.compile(r'^Copy(?:\s|$)')).first
    expect(copy).to_be_visible()
    copy.click(delay=150)
    expect(copy).not_to_be_visible()
    page.wait_for_function('expected => navigator.clipboard.readText().then(text => text === expected)', arg=selected_answer)
    page.evaluate('navigator.clipboard.writeText("FACTORY CLIPBOARD SENTINEL")')
    page.keyboard.press('Control+c')
    page.wait_for_function('expected => navigator.clipboard.readText().then(text => text === expected)', arg=selected_answer)

    open_file()
    mention('FrostPi: Mention Current File in Prompt')
    submit('file', REFERENCE_PATH)

    # The @ special uses the same text formatter and chip presentation.
    composer.fill('Factory file reference fixture completion @')
    option = frame.locator('.cm-completionLabel').filter(has_text=re.compile(r'^CurrentFile$'))
    expect(option).to_be_visible()
    option.click()
    chip = frame.get_by_role('button', name='Edit file reference ' + REFERENCE_PATH, exact=True)
    expect(chip).to_be_visible()
    frame.get_by_role('button', name='Send to Pi', exact=True).click()
    expect(frame.get_by_text('FACTORY FILE REFERENCE completion VERIFIED', exact=True)).to_be_visible(timeout=30000)
    bubble = frame.locator('.user-bubble').filter(has_text='Factory file reference fixture completion').last
    expect(bubble.locator('.message-file-reference')).to_have_attribute('title', REFERENCE_PATH)
    assert 'ref:' not in bubble.inner_text(), bubble.inner_text()

    # Browser editing: duplicate references, atomic deletion, undo, editing and literal clipboard.
    text = 'before ref:`' + selected + '` between ref:`other file.md` after'
    composer.fill(text)
    expect(frame.locator('.cm-file-reference')).to_have_count(2)
    remove = frame.get_by_role('button', name='Remove file reference other file.md', exact=True)
    remove.click()
    expect(frame.locator('.cm-file-reference')).to_have_count(1)
    composer.focus()
    page.keyboard.press('Control+z')
    expect(frame.locator('.cm-file-reference')).to_have_count(2)
    for key in ('Backspace', 'Delete'):
        composer.focus()
        page.keyboard.press('Control+End')
        for _ in ' after':
            page.keyboard.press('ArrowLeft')
        if key == 'Delete':
            page.keyboard.press('ArrowLeft')
        page.keyboard.press(key)
        expect(frame.locator('.cm-file-reference')).to_have_count(1)
        page.keyboard.press('Control+z')
        expect(frame.locator('.cm-file-reference')).to_have_count(2)
    composer.focus()
    page.keyboard.press('Control+a')
    page.keyboard.press('Control+c')
    copied = page.evaluate('navigator.clipboard.readText()')
    assert copied == text, (copied, text)
    composer.fill('')
    page.keyboard.press('Control+v')
    expect(frame.locator('.cm-file-reference')).to_have_count(2)
    page.keyboard.press('Control+End')
    expect(frame.locator('.cm-file-reference')).to_have_count(2)
    edit = frame.get_by_role('button', name='Edit file reference other file.md', exact=True)
    edit.click()
    page.keyboard.insert_text('renamed file.md')
    page.keyboard.press('End')
    expect(frame.get_by_role('button', name='Edit file reference renamed file.md', exact=True)).to_be_visible()

    dimensions = []
    for width in (1440, 900):
        if width == 900:
            page.get_by_role('button', name=re.compile(r'^Restore Secondary Side Bar')).click()
        page.set_viewport_size({'width':width, 'height':960})
        if width == 1440:
            page.get_by_role('button', name='Maximize Secondary Side Bar', exact=True).click()
            frame.wait_for_function('window.innerWidth > 1100')
        composer.focus()
        page.keyboard.press('Control+End')
        bounds = frame.locator('.composer-box').evaluate('''box => {
          const reference=box.querySelector('.cm-file-reference');
          return {width:box.clientWidth, scrollWidth:box.scrollWidth,
                  referenceWidth:reference.getBoundingClientRect().width,
                  referencePath:reference.title,
                  rangeVisible:reference.querySelector('.file-reference-lines').getBoundingClientRect().right
                    <= reference.getBoundingClientRect().right};
        }''')
        assert bounds['scrollWidth'] <= bounds['width'], bounds
        assert bounds['referencePath'] == selected, bounds
        assert bounds['referenceWidth'] <= bounds['width'], bounds
        assert bounds['rangeVisible'], bounds
        sent = frame.locator('.user-bubble .message-file-reference').evaluate_all('''tags => tags.map(tag => {
          const box=tag.getBoundingClientRect(), bubble=tag.closest('.user-bubble').getBoundingClientRect();
          const lines=tag.querySelector('.file-reference-lines')?.getBoundingClientRect();
          return {fits:box.left >= bubble.left && box.right <= bubble.right,
                  rangeVisible:!lines || lines.right <= box.right};
        })''')
        assert len(sent) == 3 and all(tag['fits'] and tag['rangeVisible'] for tag in sent), sent
        dimensions.append({'viewport':width, **bounds})
        page.screenshot(path=str(output / f'references-{width}.png'))
        frame.locator('.composer-shell').screenshot(path=str(output / f'composer-{width}.png'))
    assert dimensions[0]['width'] > dimensions[1]['width'] * 2, dimensions
    composer.fill('')
    received = json.loads(inside('cat', '/tmp/factory-reference-receipts.json'))
    assert received == ['selection', 'file', 'completion'], received
    assert inside('sha256sum', str(source)).split()[0] == hashlib.sha256(content.encode()).hexdigest(), 'Mentioning a file changed it'
    (output / 'file-references.json').write_text(json.dumps({'received':received,'layouts':dimensions,
        'clipboard':['selected answer context menu','selected answer Ctrl+C','Composer copy/paste']},indent=2))
    print('PASS actual VS Code selection/file context menus and @ completion: tagged references, exact Pi prompts without file contents, edit/remove/undo, selected-answer host clipboard and wide/narrow layouts.',flush=True)
