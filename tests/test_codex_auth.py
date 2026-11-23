"""Subscription credentials stay outside native Codex and its editor process."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from codex_auth import AuthStore, Redactor, atomic_json, claims


def token(secret, expiry=2000000000):
    payload = {'email': 'test@example.invalid', 'exp': expiry,
        'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account', 'chatgpt_plan_type': 'pro'}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
    return 'eyJhbGciOiJSUzI1NiJ9.' + encoded + '.' + secret


def login():
    return {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {
        'id_token': token('private-id-signature'), 'access_token': token('private-access-signature'),
        'refresh_token': 'private-refresh-secret', 'account_id': 'fixture-account'},
        'last_refresh': '2026-11-23T10:00:00Z'}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AuthStore(self.temp.name)
        atomic_json(self.store.path, login())

    def test_host_copy_exposes_only_subscription_metadata(self):
        public = self.store.public()
        self.assertEqual(public['auth']['auth_mode'], 'chatgpt')
        self.assertEqual(claims(public['auth']['tokens']['id_token'])['https://api.openai.com/auth']['chatgpt_plan_type'], 'pro')
        for secret in login()['tokens'].values():
            if secret != 'fixture-account':
                self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(AuthStore(self.temp.name).public(), public)

    def test_refresh_rotates_real_tokens_without_returning_them(self):
        before = self.store.public()
        original = before['auth']['tokens']['access_token']
        response = {'access_token': token('rotated-secret', 2100000000), 'refresh_token': 'rotated-refresh'}
        masked = self.store.capture(response)
        self.assertEqual(self.store.substitutions()[original], response['access_token'])
        self.assertNotEqual(masked['access_token'], response['access_token'])
        self.assertEqual(claims(masked['access_token'])['exp'], 2100000000)
        self.assertEqual(json.loads(self.store.path.read_text())['tokens']['refresh_token'], 'rotated-refresh')
        self.assertEqual(self.store.public()['generation'], before['generation'])

    def test_new_sign_in_is_stored_privately_and_returns_placeholders(self):
        before = self.store.public()
        response = login()['tokens'] | {'access_token': token('new-sign-in')}
        result = self.store.capture(response, new_login=True)
        self.assertNotEqual(before['generation'], self.store.public()['generation'])
        self.assertNotIn('new-sign-in', json.dumps(result))
        self.assertEqual(json.loads(self.store.path.read_text())['tokens']['access_token'], response['access_token'])
        self.assertEqual(self.store.substitutions()[before['auth']['tokens']['access_token']], response['access_token'])

    def test_different_account_invalidates_previous_account_placeholders(self):
        before = self.store.public()
        changed = login()
        changed['tokens']['account_id'] = 'other-account'
        atomic_json(self.store.path, changed)
        after = self.store.public()
        self.assertNotEqual(before['generation'], after['generation'])
        self.assertNotIn(before['auth']['tokens']['access_token'], self.store.substitutions())

    def test_response_stream_redacts_every_possible_split(self):
        secret = b'private-token-abc123'
        for split in range(len(secret) + 1):
            redact = Redactor({'placeholder': secret.decode()})
            result = redact(b'prefix ' + secret[:split]) + redact(secret[split:] + b' suffix') + redact(b'')
            self.assertEqual(result, b'prefix placeholder suffix')
        redact = Redactor({'placeholder': secret.decode()})
        self.assertEqual(redact(b'data: ordinary token\n\n'), b'data: ordinary token\n\n')


if __name__ == '__main__':
    unittest.main()
