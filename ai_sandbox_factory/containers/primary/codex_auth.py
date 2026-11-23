"""Codex subscription storage. Only the credential container imports this module."""
import base64
import json
import os
from pathlib import Path
import secrets
import hashlib
from datetime import datetime, timezone


def claims(token):
    payload = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))


def placeholder(token, identifier):
    """Preserve the claims Codex reads, never the reusable signed token."""
    try:
        original = claims(token)
    except (ValueError, IndexError, TypeError):
        return 'factory-codex-' + identifier
    public = {k: original[k] for k in ('exp', 'iat', 'email') if k in original}
    for namespace, fields in {
        'https://api.openai.com/auth': ('chatgpt_plan_type', 'chatgpt_user_id', 'user_id',
            'chatgpt_account_id', 'chatgpt_account_user_id', 'chatgpt_account_is_fedramp'),
        'https://api.openai.com/profile': ('email',),
    }.items():
        source = original.get(namespace, {})
        if isinstance(source, dict):
            public[namespace] = {k: source[k] for k in fields if k in source}
    encode = lambda x: base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none', 'typ': 'JWT'}) + '.' + encode(public) + '.factory-' + identifier


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AuthStore:
    def __init__(self, home):
        self.path = Path(home) / '.codex/auth.json'
        self.mask_path = Path(home) / '.codex/factory-placeholders.json'
        self.saved = None
        self.data = {}
        self.masks = {}
        self.aliases = {}
        self.fingerprints = {}
        self.generation = ''
        self.account = None
        if self.mask_path.exists():
            saved = json.loads(self.mask_path.read_text())
            self.masks = saved['masks']
            self.generation = saved['generation']
            self.aliases = saved.get('aliases', {})
            self.fingerprints = saved.get('fingerprints', {})
            self.account = saved.get('account')

    def load(self):
        if not self.path.exists():
            self.data = {}
            return
        raw = self.path.read_bytes()
        if raw == self.saved:
            return
        self.data = json.loads(raw)
        if self.data.get('OPENAI_API_KEY'):
            raise ValueError('Codex requires a ChatGPT subscription sign-in for this credential service.')
        tokens = self.data.get('tokens') or {}
        account = tokens.get('account_id')
        if self.account is not None and self.account != account:
            self.generation = ''
            self.masks = {}
            self.aliases = {}
            self.fingerprints = {}
        self.account = account
        if not self.generation:
            self.generation = secrets.token_hex(16)
        for name in ('access_token', 'refresh_token', 'id_token'):
            value = tokens.get(name)
            if value:
                fingerprint = hashlib.sha256(value.encode()).hexdigest()
                if self.fingerprints.get(name) != fingerprint:
                    if name in self.masks:
                        self.aliases[self.masks[name]] = name
                    self.masks[name] = placeholder(value, secrets.token_hex(16))
                    self.fingerprints[name] = fingerprint
        self.saved = raw
        atomic_json(self.mask_path, {'masks': self.masks, 'generation': self.generation,
                                    'aliases': self.aliases, 'fingerprints': self.fingerprints,
                                    'account': self.account})

    def public(self):
        self.load()
        if not self.data.get('tokens'):
            return {'generation': self.generation, 'auth': None}
        tokens = self.data['tokens']
        return {'generation': self.generation, 'auth': {
            'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
            'tokens': {**{k: self.masks[k] for k in ('access_token', 'refresh_token', 'id_token') if k in self.masks},
                       'account_id': tokens.get('account_id')},
            'last_refresh': self.data.get('last_refresh'),
        }}

    def substitutions(self):
        self.load()
        tokens = self.data.get('tokens') or {}
        return {**{mask: tokens[name] for mask, name in self.aliases.items() if tokens.get(name)},
                **{mask: tokens[name] for name, mask in self.masks.items() if tokens.get(name)}}

    def capture(self, response, new_login=False):
        self.load()
        if not isinstance(response.get('access_token'), str):
            raise ValueError('Codex OAuth response omitted access_token.')
        if new_login:
            self.data = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {}}
            self.generation = secrets.token_hex(16)
            # Existing clients for this account can finish in-flight work.
            # load() discards aliases if the new sign-in changes the account.
        tokens = self.data.setdefault('tokens', {})
        for name in ('access_token', 'refresh_token', 'id_token'):
            if isinstance(response.get(name), str):
                tokens[name] = response[name]
        try:
            tokens['account_id'] = claims(tokens['id_token']).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
        except (KeyError, ValueError, IndexError):
            pass
        self.data['last_refresh'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        atomic_json(self.path, self.data)
        self.load()
        return {**response, **{name: self.masks[name] for name in self.masks if name in response}}


class Redactor:
    """Replace complete credentials even across streaming chunk boundaries."""
    def __init__(self, replacements):
        self.replacements = {secret.encode(): mask.encode() for mask, secret in replacements.items()}
        self.pending = b''

    def __call__(self, chunk):
        data = self.pending + chunk
        self.pending = b''
        for secret, mask in self.replacements.items():
            data = data.replace(secret, mask)
        if chunk:
            keep = 0
            for secret in self.replacements:
                for length in range(1, min(len(secret), len(data) + 1)):
                    if data.endswith(secret[:length]):
                        keep = max(keep, length)
            if keep:
                self.pending, data = data[-keep:], data[:-keep]
        return data
