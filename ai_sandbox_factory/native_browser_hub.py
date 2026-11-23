"""A fixed Antigravity browser origin with the browser editor's authentication."""
import asyncio
import gzip
import json
import re
import secrets
import zlib
from urllib.parse import unquote, urlsplit

from .browser_editor import Relay, validate_head


class AntigravityRelay(Relay):
    def __init__(self, container, password_hash, editor_origin):
        super().__init__(container)
        self.password_hash = password_hash
        self.editor_origin = editor_origin
        self.worker_port = None

    async def connector(self):
        if not self.worker_port:
            raise ValueError('The Antigravity workspace hub has not opened its port. Reopen the browser editor.')
        return await asyncio.create_subprocess_exec(
            'podman', 'exec', '-i', '--user', 'node', self.container,
            'socat', 'STDIO', 'TCP:127.0.0.1:' + str(self.worker_port),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

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
        # The incoming authority and browser Origin were validated above. The
        # native hub itself accepts localhost authorities only, so present the
        # fixed workspace destination rather than the browser relay's address.
        local = '127.0.0.1:' + str(self.worker_port)
        lines = []
        script = head.split(b' ', 2)[1].split(b'?', 1)[0] == b'/main.js'
        for line in head.split(b'\r\n'):
            if not line or script and line.lower().startswith(b'accept-encoding:'):
                continue
            if line.lower().startswith(b'host:'):
                line = ('Host: ' + local).encode()
            elif line.lower().startswith(b'origin:'):
                line = ('Origin: http://' + local).encode()
            lines.append(line)
        if script:
            lines.append(b'Accept-Encoding: identity')
        return b'\r\n'.join(lines + [b'', b''])

    def response_body_transform(self, head, status, fields):
        method, target, _ = head.split(b' ', 2)
        if status != 200 or method != b'GET' or target.split(b'?', 1)[0] != b'/main.js':
            return None
        def patch(body):
            encoding = fields.get('content-encoding', 'identity')
            try:
                if encoding == 'gzip':
                    body = gzip.decompress(body)
                elif encoding == 'deflate':
                    body = zlib.decompress(body)
                elif encoding != 'identity':
                    raise ValueError(f'Cannot adapt the Antigravity browser frontend: unsupported content encoding {encoding!r}. Reopen the browser editor.')
            except (OSError, zlib.error) as error:
                raise ValueError(f'Cannot decode the Antigravity frontend using {encoding}: {error}. Reopen the browser editor.') from None
            source = b'if(a===b||a.startsWith("vscode-webview://")'
            if body.count(source) != 1:
                raise ValueError('The Antigravity frontend changed its parent-origin check. Update the browser integration before opening it.')
            replacement = ('if(a===' + json.dumps(self.editor_origin) + '||a===b||a.startsWith("vscode-webview://")').encode()
            body = body.replace(source, replacement)
            return gzip.compress(body) if encoding == 'gzip' else zlib.compress(body) if encoding == 'deflate' else body
        return patch

    def redirect(self, head, headers):
        method, target, _ = head.split(b'\r\n', 1)[0].decode('ascii').split(' ')
        parsed = urlsplit(target)
        match = re.match(r'^/proxy/([0-9]+)/', parsed.path)
        if not match:
            return None
        if method != 'GET':
            raise ValueError('Invalid panel navigation')
        self.authorize(headers)
        port = int(match[1])
        if not 1 <= port <= 65535:
            raise ValueError('The Antigravity workspace hub port must be between 1 and 65535.')
        self.worker_port = port
        path = parsed.path[match.end() - 1:]
        return self.scheme + '://' + self.host + path + ('?' + parsed.query if parsed.query else '')

    def response_headers(self, lines):
        # Only this sandbox's editor may embed the native panel. Other local
        # applications and other sandboxes do not inherit its browser origin.
        result = []
        for line in lines:
            if line.lower().startswith('content-security-policy:'):
                parts = [part.strip() for part in line.split(':', 1)[1].split(';')
                         if part.strip() and not part.strip().lower().startswith('frame-ancestors ')]
                line = 'Content-Security-Policy: ' + '; '.join(parts + ["frame-ancestors 'self' " + self.editor_origin])
            result.append(line)
        return result
