"""Exercise standalone FrostPi using the real Factory listener, Pi and a local test model."""
import base64
import fcntl
import os
import socket
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from pi_sandbox_factory import browser_editor, editor_settings


def check_files(page, composer, state, url, output):
    workspace = state / 'workspace'
    reports = workspace / 'reports'
    reports.mkdir()
    (reports / 'README.md').write_text(
        '# Field notes\n\nObservations from the site visit.\n\n'
        '![Elevation chart](<elevation chart.png>)\n\n[Survey data](survey.csv)\n\n'
        '![External](https://example.invalid/tracking.png)\n\n'
        '[Source website](https://example.invalid/source)\n\n'
        '<img src="https://example.invalid/raw-html.png">\n')
    (reports / 'survey.csv').write_text('altitude,temperature\n245,18\n')
    (reports / 'elevation chart.png').write_bytes(base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nO3NQQkAAAgEsCtpODsZyhQ+hMH+'
        'S/WcikAgEAgEAoFAIPgSLCQx6Fu1gaoXAAAAAElFTkSuQmCC'))
    (workspace / 'large.txt').write_text('z' * 65535 + '🌱' + 'z' * 65536 + 'last line')
    (workspace / 'empty').mkdir()
    (workspace / 'outside').symlink_to('/etc')
    (workspace / 'notes-link').symlink_to('reports/README.md')
    os.mkfifo(workspace / 'pipe')
    api = url + 'files'
    for name in ('/etc/passwd', '../etc/passwd', 'outside/passwd'):
        assert page.request.get(api, params={'path': name}).status == 403
    first_page = page.request.get(api, params={'path': 'large.txt'}).json()
    with (workspace / 'large.txt').open('a') as file:
        file.write(' updated')
    assert page.request.get(api, params={'path': 'large.txt', 'offset': first_page['next'],
                                        'version': first_page['version']}).status == 409
    origin = urlsplit(url)
    headers = {'Origin': f'{origin.scheme}://{origin.netloc}', 'Content-Type': 'application/octet-stream'}
    assert page.request.post(api, params={'name': 'disabled.txt'}, headers=headers, data=b'off').status == 403
    assert not (workspace / 'disabled.txt').exists()
    assert page.request.get(api, params={'path': 'reports/README.md', 'download': '1'}).status == 403
    assert page.request.get(api, params={'path': 'notes-link'}).json()['kind'] == 'text'
    assert page.request.get(api, params={'path': 'pipe'}).json()['kind'] == 'unsupported'
    assert page.request.get(api, params={'path': 'reports/survey.csv', 'image': '1'}).status == 415

    image_response = page.request.get(api, params={'path': 'reports/elevation chart.png', 'image': '1'})
    assert image_response.headers['content-type'] == 'image/png'
    assert "default-src 'none'" in image_response.headers['content-security-policy']
    assert image_response.headers['x-content-type-options'] == 'nosniff'
    assert image_response.headers['cross-origin-resource-policy'] == 'same-origin'

    visits = []
    page.route('https://example.invalid/**', lambda route: (visits.append(route.request.url), route.abort()))
    # The page policy also blocks a remote image independently of the Markdown renderer.
    directive = page.evaluate("""() => new Promise(resolve => {
        document.addEventListener('securitypolicyviolation', e => resolve(e.effectiveDirective), {once: true});
        new Image().src = 'https://example.invalid/csp-check.png';
    })""")
    assert directive == 'img-src' and not visits
    composer.fill('Factory chat [Read field notes](./reports/README.md)')
    page.get_by_role('button', name='Send to Pi', exact=True).click()
    link = page.get_by_role('link', name='Read field notes', exact=True).last
    expect(link).to_be_visible(timeout=30000)
    composer.fill('Keep this draft')
    link.click()
    dialog = page.get_by_role('dialog', name='Workspace files')
    expect(dialog.get_by_role('heading', name='Field notes')).to_be_visible()
    expect(dialog.get_by_role('link', name='Download', exact=True)).to_have_count(0)
    image = dialog.get_by_role('img', name='Elevation chart')
    expect(image).to_be_visible()
    page.wait_for_function("document.querySelector('dialog[open] img')?.naturalWidth === 32")
    expect(dialog.get_by_text('Remote image blocked · example.invalid')).to_be_visible()
    expect(dialog.locator('img[src^="https:"]')).to_have_count(0)
    source = dialog.get_by_role('link', name='Source website')
    source.click()
    confirmation = page.get_by_role('dialog').filter(has_text='opening the link may bypass')
    expect(confirmation).to_contain_text('https://example.invalid/source')
    confirmation.get_by_role('button', name='Cancel').click()
    expect(confirmation).not_to_be_visible()
    expect(dialog).to_be_visible()
    assert not visits
    dialog.get_by_role('link', name='Survey data').click()
    expect(dialog.locator('pre')).to_contain_text('245,18')
    dialog.get_by_role('button', name='reports', exact=True).click()
    expect(dialog.get_by_role('button', name='survey.csv', exact=True)).to_be_focused()
    page.screenshot(path=str(output / 'workspace-files.png'))
    dialog.get_by_role('button', name='elevation chart.png', exact=True).click()
    expect(dialog.get_by_role('img', name='elevation chart.png')).to_be_visible()
    page.wait_for_function("document.querySelector('dialog[open] img')?.naturalWidth === 32")
    page.keyboard.press('Escape')
    expect(dialog).not_to_be_visible()
    expect(composer).to_have_text('Keep this draft')

    files = page.get_by_role('button', name='Files', exact=True)
    files.click()
    expect(dialog.get_by_role('button', name='Upload file…')).to_have_count(0)
    dialog.get_by_role('button', name='empty', exact=True).click()
    expect(dialog.get_by_text('This folder is empty.')).to_be_visible()
    dialog.get_by_role('button', name='Workspace', exact=True).click()
    dialog.get_by_role('button', name='large.txt', exact=True).click()
    expect(dialog.get_by_role('button', name='Show more')).to_be_visible()
    expect(dialog.get_by_role('region', name='large.txt')).to_be_focused()
    page.keyboard.press('PageDown')
    expect(dialog.get_by_role('region', name='large.txt')).not_to_have_js_property('scrollTop', 0)
    dialog.get_by_role('button', name='Show more').click()
    expect(dialog.locator('pre')).to_contain_text('🌱')
    expect(dialog.get_by_role('region', name='large.txt')).to_be_focused()
    dialog.get_by_role('button', name='Show more').click()
    expect(dialog.locator('pre')).to_contain_text('last line')
    dialog.get_by_role('button', name='Workspace', exact=True).click()
    (workspace / 'new file.txt').touch()
    dialog.get_by_role('button', name='Refresh', exact=True).click()
    dialog.get_by_role('button', name='new file.txt', exact=True).click()
    expect(dialog.get_by_text('This file is empty.')).to_be_visible()
    dialog.get_by_role('button', name='Workspace', exact=True).click()
    dialog.get_by_role('button', name='outside', exact=True).click()
    expect(dialog.get_by_role('alert')).to_contain_text('outside the workspace')
    dialog.get_by_role('button', name='Close file viewer').click()
    expect(files).to_be_focused()

    page.set_viewport_size({'width': 390, 'height': 844})
    files.click()
    dialog.get_by_role('button', name='reports', exact=True).click()
    dialog.get_by_role('button', name='README.md', exact=True).click()
    expect(dialog.get_by_role('heading', name='Field notes')).to_be_visible()
    page.screenshot(path=str(output / 'workspace-files-mobile.png'))
    assert dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth')
    page.keyboard.press('Escape')
    expect(composer).to_have_text('Keep this draft')
    assert not visits, 'Markdown preview requested a remote image'
    link.click()
    source.click()
    confirmation.get_by_role('button', name='Open in browser').click()
    final_link = page.get_by_role('link', name='Open link', exact=True)
    expect(final_link).to_have_attribute('href', 'https://example.invalid/source')
    assert not visits
    page.get_by_role('button', name='Close', exact=True).click()


def start_editor(container, cwd, state, **options):
    preferences = editor_settings.validate({'mode': 'frostpi', **options})
    with (state / 'browser-editor.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            browser_editor.start_background(container, cwd, preferences, state, False, lock.fileno())
        except ValueError:
            print((state / 'browser-editor.log').read_text())
            raise
    return browser_editor.runtime(state)['url']


def check_transfers(page, state, url, output):
    origin = urlsplit(url)
    headers = {'Origin': f'{origin.scheme}://{origin.netloc}', 'Content-Type': 'application/octet-stream'}
    api = url + 'files'
    page.goto(url)
    page.get_by_role('button', name='Files', exact=True).click()
    dialog = page.get_by_role('dialog', name='Workspace files')
    dialog.get_by_role('button', name='reports', exact=True).click()
    upload = dialog.get_by_role('button', name='Upload file…')
    # Leave a transfer in flight long enough to cancel using the actual keyboard control.
    session = page.context.new_cdp_session(page)
    session.send('Network.enable')
    session.send('Network.emulateNetworkConditions', {'offline': False, 'latency': 0,
                 'downloadThroughput': -1, 'uploadThroughput': 65536})
    try:
        upload.focus()
        with page.expect_file_chooser() as chosen:
            page.keyboard.press('Enter')
        chosen.value.set_files({'name': 'cancelled.bin', 'mimeType': 'application/octet-stream',
                                'buffer': bytes(2 * 1024 * 1024)})
        expect(dialog.get_by_role('button', name='Cancel upload')).to_be_focused()
        page.keyboard.press('Enter')
        expect(dialog.get_by_role('status')).to_have_text('Upload cancelled.')
        expect(upload).to_be_focused()
        folder = state / 'workspace' / 'reports'
        expect(dialog.get_by_role('button', name='cancelled.bin')).to_have_count(0)
        assert not (folder / 'cancelled.bin').exists()
    finally:
        session.send('Network.emulateNetworkConditions', {'offline': False, 'latency': 0,
                     'downloadThroughput': -1, 'uploadThroughput': -1})
        session.detach()
    name = "November’s observations.bin"
    body = bytes(range(256)) * 1024
    with page.expect_file_chooser() as chosen:
        upload.click()
    chosen.value.set_files({'name': name, 'mimeType': 'application/octet-stream', 'buffer': body})
    added = dialog.get_by_role('button', name=name)
    expect(added).to_be_focused()
    page.screenshot(path=str(output / 'files-transfer-browser.png'))
    dest = state / 'workspace' / 'reports' / name
    assert dest.read_bytes() == body
    with page.expect_file_chooser() as chosen:
        upload.click()
    chosen.value.set_files({'name': name, 'mimeType': 'text/plain', 'buffer': b'different'})
    expect(dialog.get_by_role('alert')).to_contain_text(f'“{name}” already exists')
    expect(upload).to_be_focused()
    assert dest.read_bytes() == body
    assert page.request.get(api, params={"path": "reports/" + name}).json().get("download")
    added.click()
    expect(dialog.get_by_text("This file type has no preview.")).to_be_visible()
    with page.expect_download() as event:
        dialog.get_by_role('link', name='Download', exact=True).click()
    download = event.value
    assert download.suggested_filename == name and download.path().read_bytes() == body
    response = page.request.get(api, params={'path': 'reports/' + name, 'download': '1'})
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert page.request.post(api, params={'name': '../escape'}, headers=headers, data=b'x').status == 400
    assert page.request.post(api, params={'name': 'unused', 'path': 'outside'}, headers=headers, data=b'x').status == 403
    assert page.request.post(api, params={'name': 'unused'}, data=b'x').status == 403
    assert page.request.get(api, params={'path': 'outside/passwd', 'download': '1'}).status == 403

    # A closed tab may interrupt a transfer. Only complete files should appear under their final names.
    folder = dest.parent
    with socket.create_connection((origin.hostname, origin.port)) as sock:
        head = (f'POST {origin.path}files?path=reports&name=interrupted HTTP/1.1\r\nHost: {origin.netloc}\r\n'
                f'Origin: {headers["Origin"]}\r\nContent-Type: application/octet-stream\r\nContent-Length: 524288\r\n\r\n')
        sock.sendall(head.encode() + b'x' * 65536)
        deadline = time.monotonic() + 10
        while not list(folder.glob('.frostpi-*.part')) and time.monotonic() < deadline:
            time.sleep(.05)
        assert list(folder.glob('.frostpi-*.part'))
    deadline = time.monotonic() + 10
    while list(folder.glob('.frostpi-*.part')) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not list(folder.glob('.frostpi-*.part')) and not (folder / 'interrupted').exists()


def check(container, cwd, state, chromium, output):
    url = start_editor(container, cwd, state)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox', '--no-proxy-server'])
            page = browser.new_page(viewport={'width': 1280, 'height': 800})
            origin = urlsplit(url)
            assert page.request.get(f'{origin.scheme}://{origin.netloc}/', headers={'Connection': 'close'}).status == 403
            assert page.request.post(url + 'request', headers={'Origin': 'https://example.invalid'}, data={}).status == 403
            assert page.request.get(url + 'files', headers={'Sec-Fetch-Site': 'cross-site'}).status == 403
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            response = page.goto(url)
            assert response and response.ok
            policy = dict((parts[0], parts[1:]) for part in response.headers['content-security-policy'].split(';')
                          if (parts := part.split()))
            assert policy['default-src'] == ["'none'"] and policy['connect-src'] == ["'self'"]
            assert policy['frame-ancestors'] == ["'none'"] and policy['form-action'] == ["'none'"]
            assert set(policy['img-src']) == {"'self'", 'blob:', 'data:'}
            assert policy['script-src'] == ["'self'"] and response.headers['referrer-policy'] == 'no-referrer'
            page.get_by_role('button', name='New session', exact=False).click()
            composer = page.get_by_label('Message Pi', exact=True)
            expect(composer).to_be_visible(timeout=30000)
            composer.fill('Factory chat standalone')
            page.get_by_role('button', name='Send to Pi', exact=True).click()
            reply = page.get_by_text('CHAT REPLY standalone', exact=True)
            expect(reply).to_be_visible(timeout=60000)
            page.reload()
            expect(reply).to_be_visible(timeout=30000)
            page.get_by_role('button', name='New chat', exact=True).click()
            expect(reply).to_have_count(0)
            page.get_by_role('button', name='Resume…', exact=True).click()
            picker = page.get_by_role('dialog')
            expect(picker).to_be_visible(timeout=30000)
            picker.get_by_role('button').filter(has_text='standalone').first.click()
            expect(reply).to_be_visible(timeout=30000)
            assert page.evaluate("document.fonts.load('16px codicon').then(fonts => fonts.length > 0)")
            page.screenshot(path=str(output / 'frostpi-browser.png'))
            check_files(page, composer, state, url, output)
            expect(composer).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            page.close()
            browser_editor.stop(state)
            url = start_editor(container, cwd, state, frostpi_uploads=True, frostpi_downloads=True)
            page = browser.new_page()
            check_transfers(page, state, url, output)
            page.close()
            for uploads in (True, False):
                browser_editor.stop(state)
                url = start_editor(container, cwd, state, frostpi_uploads=uploads, frostpi_downloads=not uploads)
                page = browser.new_page()
                page.goto(url)
                page.get_by_role('button', name='Files', exact=True).click()
                dialog = page.get_by_role('dialog', name='Workspace files')
                dialog.get_by_role('button', name='reports', exact=True).click()
                expect(dialog.get_by_role('button', name='README.md')).to_be_visible()
                expect(dialog.get_by_role('button', name='Upload file…')).to_have_count(int(uploads))
                dialog.get_by_role('button', name='README.md').click()
                expect(dialog.get_by_role('heading', name='Field notes')).to_be_visible()
                expect(dialog.get_by_role('link', name='Download', exact=True)).to_have_count(int(not uploads))
                page.close()
            browser.close()
        print('PASS Standalone FrostPi rejected unauthorized requests, sent a message, restored the reply and browsed workspace files on desktop and mobile. Verified uploads, downloads, collision handling and cancellation.')
    finally:
        browser_editor.stop(state)
