"""Antigravity subscription profiles expose placeholders to native clients."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from agy_auth import AuthStore, read_profile, write_profile


def profile():
    return {'token': {'access_token': 'private-google-access', 'refresh_token': 'private-google-refresh',
                     'token_type': 'Bearer', 'expiry': '2099-01-01T00:00:00Z', 'expires_in': 3600},
            'auth_method': 'consumer', 'id_token_email': 'synthetic@example.invalid'}


class AntigravityAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = profile()
        self.store = AuthStore(self.temp.name, read=lambda: copy.deepcopy(self.profile), write=self.write)

    def write(self, value):
        self.profile = copy.deepcopy(value)

    def test_host_copy_exposes_only_placeholders_and_native_metadata(self):
        public = self.store.public()
        self.assertEqual(public['auth']['auth_method'], 'consumer')
        self.assertEqual(public['auth']['token']['expiry'], self.profile['token']['expiry'])
        self.assertEqual(public['auth']['id_token_email'], self.profile['id_token_email'])
        for name in ('access_token', 'refresh_token'):
            self.assertNotIn(self.profile['token'][name], json.dumps(public))
        other = AuthStore(self.temp.name, read=lambda: copy.deepcopy(self.profile), write=self.write)
        self.assertEqual(public, other.public())

    def test_refresh_rotates_masks_and_resolves_inflight_old_masks(self):
        before = self.store.public()
        old = before['auth']['token']['access_token']
        result = self.store.capture({'access_token': 'private-refreshed-access', 'expires_in': 3600,
                                     'token_type': 'Bearer'})
        self.assertNotEqual(old, result['access_token'])
        self.assertEqual(self.store.substitutions()[old], 'private-refreshed-access')
        self.assertEqual(self.profile['token']['refresh_token'], 'private-google-refresh')
        self.assertNotIn('private-refreshed-access', json.dumps(result))
        self.assertEqual(before['generation'], self.store.public()['generation'])

    def test_fresh_login_updates_private_profile_and_public_generation(self):
        old = self.store.public()
        result = self.store.capture({'access_token': 'private-new-access', 'refresh_token': 'private-new-refresh',
                                     'expires_in': 3600, 'token_type': 'Bearer'}, new_login=True)
        self.assertNotEqual(old['generation'], self.store.public()['generation'])
        self.assertNotIn('private-new-access', json.dumps(result))
        self.assertEqual(self.profile['token']['refresh_token'], 'private-new-refresh')

    def test_account_switch_invalidates_old_masks(self):
        old = self.store.public()['auth']['token']['access_token']
        self.profile['id_token_email'] = 'second@example.invalid'
        self.assertNotIn(old, self.store.substitutions())

    def test_missing_signin_and_unsupported_auth_are_actionable(self):
        self.profile = None
        self.assertIsNone(self.store.public()['auth'])
        self.profile = profile()
        self.profile['auth_method'] = 'api_key'
        with self.assertRaisesRegex(ValueError, 'personal Google subscription sign-in'):
            self.store.public()

    def test_failed_exchange_keeps_previous_credentials(self):
        before = copy.deepcopy(self.profile)
        with self.assertRaisesRegex(ValueError, 'omitted access_token'):
            self.store.capture({'refresh_token': 'private-unsaved'})
        self.assertEqual(self.profile, before)

    def test_keyring_failures_keep_diagnostics_and_redact_known_tokens(self):
        with patch('agy_auth.subprocess.run', return_value=CompletedProcess([], 1, '', 'Cannot connect to bus: connection refused')):
            with self.assertRaisesRegex(OSError, 'exit 1: Cannot connect to bus: connection refused'):
                read_profile()
        with patch('agy_auth.subprocess.run', return_value=CompletedProcess([], 1, '', 'Rejected private-google-refresh: collection is locked')):
            with self.assertRaises(OSError) as failed:
                write_profile(profile())
            self.assertIn('collection is locked', str(failed.exception))
            self.assertNotIn('private-google-refresh', str(failed.exception))


if __name__ == '__main__':
    unittest.main()
