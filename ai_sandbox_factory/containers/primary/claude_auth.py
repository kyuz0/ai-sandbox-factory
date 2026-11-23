"""Claude subscription credentials, imported only in the private companion."""
import hashlib
import json
from pathlib import Path
import secrets
import time

from codex_auth import atomic_json


class AuthStore:
    def __init__(self, home):
        self.path = Path(home) / '.claude/.credentials.json'
        self.mask_path = Path(home) / '.claude/factory-placeholders.json'
        self.data = {}
        self.saved = None
        self.state = json.loads(self.mask_path.read_text()) if self.mask_path.exists() else {}

    def load(self):
        if not self.path.exists():
            self.data = {}
            return
        raw = self.path.read_bytes()
        if raw == self.saved:
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Claude subscription credential file must contain a JSON object. Sign in to Claude again.')
        oauth = data.get('claudeAiOauth', {})
        if not isinstance(oauth, dict) or not oauth.get('accessToken'):
            raise ValueError('Claude subscription credential file has no claudeAiOauth.accessToken. Sign in to Claude again.')
        account = oauth.get('accountUuid')
        if self.state.get('account') != account:
            self.state = {}
        self.state.setdefault('generation', secrets.token_hex(16))
        self.state['account'] = account
        masks = self.state.setdefault('masks', {})
        fingerprints = self.state.setdefault('fingerprints', {})
        aliases = self.state.setdefault('aliases', {})
        for name in ('accessToken', 'refreshToken', 'idToken'):
            value = oauth.get(name)
            if value:
                if not isinstance(value, str):
                    raise ValueError(f'Claude subscription credential field {name} must be text. Sign in to Claude again.')
                fingerprint = hashlib.sha256(value.encode()).hexdigest()
                if fingerprints.get(name) != fingerprint:
                    if name in masks:
                        aliases[masks[name]] = name
                    # Native OAuth tokens are opaque; these strings are never
                    # valid upstream credentials, even if copied out of a tool.
                    masks[name] = 'factory-claude-' + secrets.token_hex(32)
                    fingerprints[name] = fingerprint
        self.data = data
        self.saved = raw
        atomic_json(self.mask_path, self.state)

    def public(self):
        self.load()
        oauth = self.data.get('claudeAiOauth')
        if not oauth:
            return {'generation': self.state.get('generation', ''), 'auth': None}
        # Do not forward unknown future credential fields to the workspace.
        metadata = ('expiresAt', 'refreshTokenExpiresAt', 'scopes', 'subscriptionType',
                    'subscriptionTypeSource', 'rateLimitTier', 'accountUuid', 'organizationUuid')
        return {'generation': self.state['generation'], 'auth': {'claudeAiOauth': {
            **{key: oauth[key] for key in metadata if key in oauth},
            **{key: value for key, value in self.state['masks'].items() if key in ('accessToken', 'refreshToken')},
        }}}

    def substitutions(self):
        self.load()
        oauth = self.data.get('claudeAiOauth', {})
        return {mask: oauth[name] for mask, name in {
            **self.state.get('aliases', {}),
            **{mask: name for name, mask in self.state.get('masks', {}).items()},
        }.items() if oauth.get(name)}

    def clear(self):
        """Forget only this sandbox's copy; never revoke the host's session."""
        self.path.unlink(missing_ok=True)
        self.saved = None
        self.data = {}
        self.state = {'generation': secrets.token_hex(16)}
        atomic_json(self.mask_path, self.state)

    def capture(self, response, new_login=False):
        self.load()
        if not isinstance(response, dict):
            raise ValueError('Claude OAuth token endpoint returned a JSON value instead of a token object.')
        if not isinstance(response.get('access_token'), str) or not response['access_token']:
            raise ValueError('Claude OAuth response omitted access_token.')
        if new_login:
            self.data = {'claudeAiOauth': {}}
            self.state = {'generation': secrets.token_hex(16)}
        oauth = self.data.setdefault('claudeAiOauth', {})
        for source, target in (('access_token', 'accessToken'), ('refresh_token', 'refreshToken'), ('id_token', 'idToken')):
            if isinstance(response.get(source), str):
                oauth[target] = response[source]
        if isinstance(response.get('expires_in'), (int, float)):
            oauth['expiresAt'] = int((time.time() + response['expires_in']) * 1000)
        if isinstance(response.get('refresh_token_expires_in'), (int, float)):
            oauth['refreshTokenExpiresAt'] = int((time.time() + response['refresh_token_expires_in']) * 1000)
        if isinstance(response.get('scope'), str):
            oauth['scopes'] = response['scope'].split()
        account = response.get('account', {})
        organization = response.get('organization', {})
        if isinstance(account, dict) and isinstance(account.get('uuid'), str):
            oauth['accountUuid'] = account['uuid']
        if isinstance(organization, dict) and isinstance(organization.get('uuid'), str):
            oauth['organizationUuid'] = organization['uuid']
        atomic_json(self.path, self.data)
        self.saved = None
        self.load()
        masked = {**response}
        for source, target in (('access_token', 'accessToken'), ('refresh_token', 'refreshToken'), ('id_token', 'idToken')):
            if source in masked:
                masked[source] = self.state['masks'][target]
        return masked
