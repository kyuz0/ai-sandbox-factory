"""Editor preferences stay separate from container configuration and copied secrets."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from argon2 import PasswordHasher
from ai_sandbox_factory import editor_settings as settings


class Preferences(unittest.TestCase):
    def test_browser_default_and_host_editor_boundary(self):
        self.assertEqual(settings.validate()['mode'], 'browser')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            self.assertEqual(settings.load(state), settings.DEFAULTS)
            (state / 'browser-editor.enabled').touch()
            self.assertEqual(settings.load(state)['mode'], 'browser')
            with self.assertRaisesRegex(ValueError, 'separate sandbox'):
                settings.save(state, {'mode': 'desktop'})

    def test_password_hash_private_file_and_no_container_change(self):
        password = 'synthetic-editor-password'
        digest = settings.password_hash(password)
        self.assertTrue(PasswordHasher().verify(digest, password))
        self.assertNotEqual(digest, settings.password_hash(password))
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'config.json').write_text('unchanged')
            value = settings.save(state, {'password_hash': digest})
            self.assertEqual(settings.load(state), value)
            self.assertEqual((state / 'editor.json').stat().st_mode & 0o777, 0o600)
            self.assertNotIn(password, (state / 'editor.json').read_text())
            self.assertEqual((state / 'config.json').read_text(), 'unchanged')
            self.assertEqual(settings.copy_choice(value), settings.DEFAULTS)

    def test_uploads_default_off_for_existing_sandboxes(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'editor.json').write_text('{"mode":"browser"}')
            self.assertFalse(settings.load(state)['allow_uploads'])
            settings.save(state, {'allow_uploads': True})
            self.assertTrue(settings.copy_choice(settings.load(state))['allow_uploads'])
            settings.save(state, {'allow_uploads': False})
            self.assertFalse(settings.load(state)['allow_uploads'])
        with self.assertRaisesRegex(ValueError, 'file uploads'):
            settings.validate({'allow_uploads': 'false'})

    def test_retired_standalone_choice_keeps_chats_and_connection_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            chat = state / 'saved-chat.jsonl'
            chat.write_text('synthetic saved conversation')
            previous = {'mode': 'frostpi', 'frostpi_uploads': True,
                        'frostpi_downloads': True, 'allow_uploads': False, 'port': 43210}
            (state / 'editor.json').write_text(json.dumps(previous))
            current = settings.load(state)
            self.assertEqual(settings.copy_choice(previous), settings.DEFAULTS)
            self.assertEqual(settings.saved_choice({**previous, 'mode': 'browser'}), current)
            self.assertEqual(current, {**settings.DEFAULTS, 'port': 43210})
            self.assertEqual(chat.read_text(), 'synthetic saved conversation')
            settings.save(state, current)
            self.assertEqual(json.loads((state / 'editor.json').read_text()), current)
            self.assertEqual(set(settings.modes()), {'browser', 'pi', 'desktop'})
            with self.assertRaisesRegex(ValueError, 'Editor menu'):
                settings.validate({'mode': 'frostpi'})

    def test_address_port_and_remote_authentication(self):
        for value in ({'address': '0.0.0.0'}, {'address': '::'}, {'address': 'example.com'},
                      {'port': -1}, {'port': 65536}, {'port': True}, {'mode': 'other'},
                      {'address': '192.0.2.1'}, {'password_hash': 'raw-password'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.validate(value)
        digest = settings.password_hash('synthetic-remote-password')
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            settings.validate({'address': '192.0.2.1', 'password_hash': digest})
        value = settings.validate({'address': '192.0.2.1', 'password_hash': digest,
                                   'certificate': '/cert.pem', 'private_key': '/key.pem'})
        self.assertEqual(settings.copy_choice(value), settings.DEFAULTS)
        self.assertEqual(settings.authority('::1', 8000), '[::1]:8000')

    @patch("platform.release", return_value="6.12")
    @patch("platform.system", return_value="Linux")
    def test_private_loopback_address_and_port_reused(self, _system, _release):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            address, port = settings.connection(state, settings.DEFAULTS)
            self.assertTrue(address.startswith('127.'))
            self.assertNotEqual(address, '127.0.0.1')
            self.assertEqual(port, 0)
            (state / 'editor-address.json').write_text(json.dumps({'automatic_address': address, 'address': address, 'port': 43210}))
            self.assertEqual(settings.connection(state, settings.DEFAULTS), (address, 43210))
            self.assertEqual(settings.connection(state, {**settings.DEFAULTS, 'port': 4444}), (address, 4444))
            settings.remember_connection(state, {'address': '127.0.0.1'}, '127.0.0.1', 5555)
            self.assertEqual(settings.connection(state, settings.DEFAULTS), (address, 0))

    def test_supported_architectures_and_engine_choice(self):
        for arch in ('x86_64', 'aarch64', 'arm64'):
            with patch('platform.machine', return_value=arch):
                self.assertEqual(settings.default_mode(), 'browser')
        with patch('platform.machine', return_value='unsupported'):
            self.assertEqual(settings.default_mode(), 'desktop')
        self.assertEqual(settings.default_mode('docker'), 'desktop')
