"""Keep native page configuration private and recognize its exact owning editor."""
import json
import re
from urllib.parse import urlsplit

from editor_protocol import Rejected, object_value, text

CONFIG = re.compile(r'window\.__APP_CONFIG__\s*=\s*(\{.*?\});')
# The pinned native client recognizes localhost but not Factory's per-sandbox
# loopback addresses. Add only the editor origin supplied by the host relay.
# Its CSP also restricts all frame ancestors to that same editor origin.
ORIGIN_CHECK = 'if(a===b||a.startsWith("vscode-webview://")'
ORIGIN_REPLACEMENT = 'if(a===window.__APP_CONFIG__?.factoryEditorOrigin||a===b||a.startsWith("vscode-webview://")'


def render(path, body, public_token, editor_origin=''):
    if path == '/main.js':
        source = body.decode('utf-8')
        if source.count(ORIGIN_CHECK) != 1:
            raise Rejected('Antigravity changed its browser connection. Update the integration before opening it.')
        return source.replace(ORIGIN_CHECK, ORIGIN_REPLACEMENT).encode('utf-8')
    if path != '/':
        return body
    html = body.decode('utf-8')
    match = CONFIG.search(html)
    if not match:
        raise Rejected('Antigravity changed its panel configuration')
    original = object_value(json.loads(match.group(1)))
    visible = {'productName': 'antigravity', 'csrfToken': public_token,
               'appVersion': text(original.get('appVersion', '')), 'devMode': False}
    if editor_origin:
        origin = urlsplit(editor_origin)
        if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password or
                origin.path or origin.query or origin.fragment):
            raise Rejected('Invalid editor origin')
        visible['factoryEditorOrigin'] = editor_origin
    html = html[:match.start()] + 'window.__APP_CONFIG__=' + json.dumps(visible) + ';' + html[match.end():]
    html = html.replace('<script', '<script nonce="' + public_token + '"')
    html = re.sub(r'<link\b[^>]*\brel="preconnect"[^>]*>', '', html)
    return html.encode('utf-8')
