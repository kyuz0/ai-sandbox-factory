"""Keep Antigravity subscription tokens in the credential companion's keyring."""
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
from datetime import datetime, timedelta, timezone

from codex_auth import atomic_json


KEYRING = ['service', 'gemini', 'username', 'antigravity']


def diagnostic(result, profile=None):
    detail = result.stderr.strip()
    for name in ('access_token', 'refresh_token', 'id_token'):
        value = (profile or {}).get('token', {}).get(name)
        if isinstance(value, str) and value:
            detail = detail.replace(value, '[redacted]')
    return f'exit {result.returncode}' + (': ' + detail if detail else ' (no diagnostic output)')


def read_profile():
    result = subprocess.run(['secret-tool', 'lookup', *KEYRING], capture_output=True, text=True)
    if result.returncode == 1 and not result.stdout and not result.stderr.strip():
        return None
    if result.returncode:
        raise OSError(f'Antigravity keyring lookup failed ({diagnostic(result)}). Check the credential companion Secret Service and retry.')
    return json.loads(result.stdout)


def write_profile(profile):
    result = subprocess.run(['secret-tool', 'store', '--label=Antigravity CLI', *KEYRING],
                            input=json.dumps(profile), capture_output=True, text=True)
    if result.returncode:
        raise OSError(f'Antigravity keyring storage failed ({diagnostic(result, profile)}). Unlock the credential companion keyring and retry.')


class AuthStore:
    def __init__(self, home, *, read=read_profile, write=write_profile):
        self.path = Path(home) / '.factory-agy/placeholders.json'
        self.read, self.write = read, write
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'generation': '', 'account': None, 'masks': {}, 'aliases': {}, 'fingerprints': {}}
        self.profile = None

    def load(self):
        self.profile = self.read()
        if not self.profile:
            return
        if not isinstance(self.profile, dict) or not isinstance(self.profile.get('token'), dict):
            raise ValueError('Antigravity keyring profile must contain a token object; import the sign-in again.')
        if self.profile.get('auth_method') != 'consumer':
            raise ValueError('Antigravity credential isolation requires a personal Google subscription sign-in; choose Google OAuth when signing in.')
        account = self.profile.get('id_token_email')
        if self.state['account'] != account or not self.state['generation']:
            self.state = {'generation': secrets.token_hex(16), 'account': account,
                          'masks': {}, 'aliases': {}, 'fingerprints': {}}
        tokens = self.profile.get('token', {})
        for name in ('access_token', 'refresh_token', 'id_token'):
            value = tokens.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f'Antigravity keyring token.{name} must be a string; import the sign-in again.')
            if not value:
                continue
            fingerprint = hashlib.sha256(value.encode()).hexdigest()
            if self.state['fingerprints'].get(name) != fingerprint:
                if name in self.state['masks']:
                    self.state['aliases'][self.state['masks'][name]] = name
                # Google access/refresh tokens are opaque. ID-token claims are
                # read by the native client; retain its JSON payload but replace
                # the reusable signature with a deliberately invalid one.
                parts = value.split('.')
                self.state['masks'][name] = ('.'.join(parts[:2]) + '.factory-' + secrets.token_hex(16)
                    if name == 'id_token' and len(parts) == 3 else 'factory-agy-' + secrets.token_hex(16))
                self.state['fingerprints'][name] = fingerprint
        atomic_json(self.path, self.state)

    def public(self):
        self.load()
        if not self.profile:
            return {'generation': self.state['generation'], 'auth': None}
        return {'generation': self.state['generation'], 'auth': {
            'token': {**{key: self.profile['token'][key] for key in ('expiry', 'expires_in', 'token_type')
                         if key in self.profile['token']}, **self.state['masks']},
            'auth_method': 'consumer', 'id_token_email': self.profile.get('id_token_email', '')}}

    def substitutions(self):
        self.load()
        tokens = self.profile.get('token', {}) if self.profile else {}
        return {mask: tokens[name] for mask, name in
                {**self.state['aliases'], **{mask: name for name, mask in self.state['masks'].items()}}.items()
                if tokens.get(name)}

    def capture(self, response, new_login=False):
        self.load()
        if not isinstance(response.get('access_token'), str) or not response['access_token']:
            raise ValueError('Antigravity OAuth response omitted access_token.')
        profile = {'auth_method': 'consumer', 'token': {}} if new_login or not self.profile else self.profile
        if new_login:
            self.state['generation'] = ''
        tokens = profile['token']
        for name in ('access_token', 'refresh_token', 'id_token', 'token_type', 'expires_in'):
            if name in response:
                tokens[name] = response[name]
        if isinstance(response.get('expires_in'), (int, float)):
            tokens['expiry'] = (datetime.now(timezone.utc) + timedelta(seconds=response['expires_in'])).isoformat()
        self.write(profile)
        self.load()
        replacements = {real: mask for mask, real in self.substitutions().items()}
        return {key: replacements.get(value, value) if isinstance(value, str) else value
                for key, value in response.items()}
