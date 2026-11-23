"""Pinned native editor installation must preserve existing files on failure."""
from contextlib import ExitStack
import ast
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ai_sandbox_factory import native_editor
from native_editor_environment import editor_environment


class NativeEditorInstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = self.root / 'native-editors'
        self.cache.mkdir()
        self.stack.enter_context(patch('ai_sandbox_factory.sandbox_extensions.cache_root',
                                      return_value=self.root / 'extensions'))
        self.calls = []
        self.installed = {}
        self.fail_extract = False

        def run(command, **kwargs):
            self.calls.append((command, kwargs.get('input')))
            if 'cat' in command:
                return subprocess.CompletedProcess(command, 1, '')
            if 'tar' in command:
                if self.fail_extract:
                    raise subprocess.CalledProcessError(1, command)
                with tarfile.open(fileobj=kwargs['stdin']) as archive:
                    self.installed = {item.name: archive.extractfile(item).read()
                                      for item in archive if item.isfile()}
            return subprocess.CompletedProcess(command, 0, '')

        self.runner = self.stack.enter_context(patch('ai_sandbox_factory.sandbox.run', side_effect=run))
        self.download = self.stack.enter_context(patch('urllib.request.urlopen'))
        self.config = {'engine': 'podman', 'primary_agent': 'claude'}

    def package(self, entry='extension/package.json', *, symlink=False, cached=True):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            item = zipfile.ZipInfo(entry)
            item.external_attr = (0o120777 if symlink else 0o100644) << 16
            archive.writestr(item, b'{}')
            archive.writestr('extension/extension.js', b'official native extension bytes')
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        self.stack.enter_context(patch.object(native_editor, 'specification', return_value={
            'publisher': 'Anthropic', 'name': 'claude-code', 'version': '1.2.3',
            'platforms': {'linux-x64': {'sha256': digest, 'url': 'https://example.invalid/editor.vsix'}}}))
        path = self.cache / (digest + '.vsix')
        if cached:
            path.write_bytes(payload)
        return path, payload

    def test_cached_install_checks_archive_and_retires_only_after_success(self):
        self.package()
        native_editor.install(self.config, 'fixture', 'x86_64')
        self.download.assert_not_called()
        self.assertIn('Anthropic.claude-code-1.2.3/package.json', self.installed)
        helper = self.installed['pi-sandbox-factory.native-editor-1.0.0/index.cjs'].decode()
        self.assertIn('claude-vscode.sidebar.open', helper)
        self.assertEqual(self.installed['Anthropic.claude-code-1.2.3/extension.js'],
                         b'official native extension bytes')
        wrapper = next(value for command, value in self.calls
                       if 'tee' in command and command[-1] == '/opt/primary/editor-cli')
        self.assertEqual(wrapper, '#!/bin/sh\nexec /usr/local/bin/claude "$@"\n')
        marker = next(i for i, (cmd, _) in enumerate(self.calls) if 'tee' in cmd and cmd[-1].endswith('.factory-sha256'))
        retire = next(i for i, (cmd, _) in enumerate(self.calls) if 'python3' in cmd)
        self.assertLess(marker, retire)
        self.assertEqual(self.calls[retire][0][-2:], ['Anthropic.claude-code-', 'Anthropic.claude-code-1.2.3'])

    def test_claude_uses_official_bundled_cli_and_retires_only_factory_wrapper(self):
        self.assertEqual(native_editor.settings('claude'), {})
        path = Path(native_editor.__file__).parent / 'containers/editor/launch.py'
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'retire_claude_wrapper')
        namespace = {'settings': {'agent': 'claude'}}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        retire = namespace['retire_claude_wrapper']
        self.assertEqual(retire({'claudeCode.claudeProcessWrapper': '/opt/primary/editor-cli',
                                 'claudeCode.disableLoginPrompt': True, 'editor.fontSize': 16}), {'editor.fontSize': 16})
        custom = {'claudeCode.claudeProcessWrapper': '/workspace/custom-wrapper',
                  'claudeCode.disableLoginPrompt': True}
        self.assertEqual(retire(custom.copy()), custom)

    def test_bundled_claude_inherits_managed_config_and_authentication_transport(self):
        env = editor_environment('claude', {})
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], '/home/node/.claude')
        for key in ('HTTP_PROXY', 'HTTPS_PROXY'):
            self.assertEqual(env[key], 'http://127.0.0.1:3130')
        for key in ('NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE'):
            self.assertEqual(env[key], '/home/node/.claude/factory-ca.pem')

    def test_bad_checksum_never_mutates_container(self):
        path, _ = self.package()
        path.write_bytes(b'not the pinned archive')
        with self.assertRaises(ValueError):
            native_editor.install(self.config, 'fixture', 'x86_64')
        self.assertEqual(len(self.calls), 1)  # Read the existing installation marker only.

    def test_archive_cannot_escape_or_install_symlinks(self):
        for entry, symlink in (('extension/../../outside', False), ('extension/linked', True)):
            with self.subTest(entry=entry):
                self.calls.clear()
                self.package(entry, symlink=symlink)
                with self.assertRaisesRegex(ValueError, 'archive path'):
                    native_editor.install(self.config, 'fixture', 'x86_64')
                self.assertEqual(len(self.calls), 1)
                self.assertFalse((self.root / 'outside').exists())

    def test_failed_extract_never_marks_complete_or_removes_old_version(self):
        self.package()
        self.fail_extract = True
        with self.assertRaises(subprocess.CalledProcessError):
            native_editor.install(self.config, 'fixture', 'x86_64')
        self.assertFalse(any('tee' in cmd or 'python3' in cmd for cmd, _ in self.calls))

    def test_failed_download_is_removed_and_retry_succeeds(self):
        path, payload = self.package(cached=False)
        self.download.side_effect = [OSError('connection lost'), io.BytesIO(payload)]
        with self.assertRaisesRegex(OSError, 'connection lost'):
            native_editor.install(self.config, 'fixture', 'x86_64')
        self.assertEqual(list(self.cache.iterdir()), [])
        native_editor.install(self.config, 'fixture', 'x86_64')
        self.assertEqual(path.read_bytes(), payload)
        self.assertIn('Anthropic.claude-code-1.2.3/package.json', self.installed)


if __name__ == '__main__':
    unittest.main()
