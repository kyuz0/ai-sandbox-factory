"""A fixed Antigravity browser origin with the browser editor's authentication."""
import secrets
from urllib.parse import unquote, urlsplit

from .browser_editor import Relay, validate_head


class AntigravityRelay(Relay):
    def __init__(self, container, password_hash, editor_origin):
        super().__init__(container)
        self.socket = '/run/primary-editor/antigravity.sock'
        self.password_hash = password_hash
        self.editor_origin = editor_origin
        self.worker_port = None

    def authorize(self, headers):
        if not self.password_hash:
            return
        # Factory configures only code-server's Argon2 hashed-password mode.
        # The pinned code-server validates this same URI-encoded session cookie.
        values = []
        for item in headers.get('cookie', '').split(';'):
            name, separator, value = item.strip().partition('=')
            if separator and name == 'code-server-session':
                values.append(unquote(value).strip())
        if len(values) != 1 or not values[0].isascii() or not secrets.compare_digest(values[0], self.password_hash):
            raise ValueError('Sign in to the browser editor')

    def validate(self, head):
        headers = validate_head(head, self.host, self.scheme, embedded_navigation=True)
        if 'x-factory-editor-origin' in headers:
            raise ValueError('Invalid editor connection')
        self.authorize(headers)
        return headers

    def request_head(self, head):
        return head[:-2] + ('X-Factory-Editor-Origin: ' + self.editor_origin + '\r\n\r\n').encode('ascii')

    def redirect(self, head, headers):
        method, target, _ = head.split(b'\r\n', 1)[0].decode('ascii').split(' ')
        prefix = '/proxy/' + str(self.worker_port) + '/'
        parsed = urlsplit(target)
        if not parsed.path.startswith(prefix):
            return None
        if method != 'GET':
            raise ValueError('Invalid panel navigation')
        self.authorize(headers)
        path = parsed.path[len(prefix) - 1:]
        return self.scheme + '://' + self.host + path + ('?' + parsed.query if parsed.query else '')

    def response_headers(self, lines):
        # Only this sandbox's editor may embed the native panel. Other local
        # applications and other sandboxes do not inherit its browser origin.
        result = []
        for line in lines:
            if line.lower().startswith('content-security-policy:'):
                line += "; frame-ancestors 'self' " + self.editor_origin
            result.append(line)
        return result
