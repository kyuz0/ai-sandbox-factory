"""Update discovery must change complete, explicit pins or leave them intact."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_dependency_updates', ROOT / 'scripts/update_image_dependencies.py')
updates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updates)


class DependencyUpdates(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in (updates.AGENTS, 'ai_sandbox_factory/containers/dev/Dockerfile',
                     'ai_sandbox_factory/sandbox.py', 'ai_sandbox_factory/browser_editor.py',
                     'ai_sandbox_factory/containers/primary/Dockerfile',
                     'ai_sandbox_factory/containers/aws-access/client/Dockerfile',
                     'ai_sandbox_factory/containers/dev/extensions.lock.json'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.pins = json.loads((self.root / updates.AGENTS).read_text())

    def test_available_update_does_not_claim_to_have_applied_or_write_files(self):
        path = self.root / updates.AGENTS
        before = path.read_bytes()
        with patch.object(updates, 'request', return_value={'version': '99.1.0'}):
            result = updates.update(self.root, components=['codex'])
        self.assertEqual(result['codex']['status'], 'available')
        self.assertEqual(path.read_bytes(), before)

    def test_codex_controller_and_advisor_share_the_updated_pin(self):
        with patch.object(updates, 'request', return_value={'version': '99.1.0'}):
            result = updates.update(self.root, apply=True, components=['codex'])
        self.assertEqual(result['codex']['status'], 'updated')
        self.assertEqual(json.loads((self.root / updates.AGENTS).read_text())['codex'], {'version': '99.1.0'})
        self.assertEqual(json.loads((self.root / updates.AGENTS).read_text())['claude'], self.pins['claude'])
        for name in ('primary', 'advisor'):
            source = (ROOT / f'ai_sandbox_factory/containers/{name}/Dockerfile').read_text()
            self.assertIn('agent-versions.json', source)
            self.assertIn('install-agent.py', source)
            self.assertNotIn('npm install -g @openai/codex\n', source)

    def test_same_version_and_older_upstream_do_not_downgrade(self):
        for version in (self.pins['codex']['version'], '0.1.0'):
            with self.subTest(version=version), patch.object(updates, 'request', return_value={'version': version}):
                result = updates.update(self.root, apply=True, components=['codex'])
                self.assertFalse(result['codex']['changed'])
                self.assertEqual(json.loads((self.root / updates.AGENTS).read_text()), self.pins)

    def test_missing_empty_prerelease_and_transport_error_never_count_as_current(self):
        path = self.root / updates.AGENTS
        original = path.read_bytes()
        for value in ({}, {'version': ''}, {'version': '99.1.0-beta.1'}, ValueError('HTTP 503 from registry')):
            with self.subTest(value=value):
                with patch.object(updates, 'request', **({'side_effect': value} if isinstance(value, Exception) else {'return_value': value})):
                    result = updates.update(self.root, apply=True, components=['codex'])
                self.assertEqual(result['codex']['status'], 'failed')
                self.assertTrue(result['codex']['error'])
                self.assertEqual(path.read_bytes(), original)

    def test_claude_update_includes_both_publisher_checksums(self):
        manifest = {'platforms': {'linux-x64': {'checksum': 'a' * 64}, 'linux-arm64': {'checksum': 'b' * 64}}}
        with patch.object(updates, 'request', side_effect=['99.1.0', manifest]):
            updates.update(self.root, apply=True, components=['claude'])
        pin = json.loads((self.root / updates.AGENTS).read_text())['claude']
        self.assertEqual(pin['version'], '99.1.0')
        self.assertEqual(pin['platforms']['amd64']['sha256'], 'a' * 64)
        self.assertEqual(pin['platforms']['arm64']['sha256'], 'b' * 64)
        self.assertIn('/99.1.0/', pin['platforms']['arm64']['url'])

    def test_incomplete_claude_manifest_keeps_old_pin_but_allows_codex_update(self):
        values = [{'version': '99.0.0'}, '99.1.0', {'platforms': {'linux-x64': {'checksum': 'a' * 64}}}]
        with patch.object(updates, 'request', side_effect=values):
            result = updates.update(self.root, apply=True, components=['codex', 'claude'])
        actual = json.loads((self.root / updates.AGENTS).read_text())
        self.assertEqual(actual['codex']['version'], '99.0.0')
        self.assertEqual(actual['claude'], self.pins['claude'])
        self.assertEqual(result['claude']['status'], 'failed')

    def test_invalid_checksum_and_architecture_release_skew_leave_agy_intact(self):
        first = {'version': '99.1.0', 'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/99/linux-x64/cli.tar.gz', 'sha512': 'a' * 128}
        cases = [dict(first, sha512='not a hash'), dict(first, url='http://example.invalid/tool'), dict(first, version='99.2.0')]
        for second in cases:
            with self.subTest(second=second), patch.object(updates, 'request', side_effect=[first, second]):
                result = updates.update(self.root, apply=True, components=['agy'])
                self.assertEqual(result['agy']['status'], 'failed')
                self.assertEqual(json.loads((self.root / updates.AGENTS).read_text()), self.pins)

    def test_pi_changes_both_pins_and_rejects_existing_inconsistency(self):
        with patch.object(updates, 'request', return_value={'version': '99.0.0'}):
            result = updates.update(self.root, apply=True, components=['pi'])
        self.assertTrue(result['pi']['changed'])
        docker = self.root / 'ai_sandbox_factory/containers/dev/Dockerfile'
        python = self.root / 'ai_sandbox_factory/sandbox.py'
        self.assertIn('ARG PI_VERSION=99.0.0', docker.read_text())
        self.assertIn("PI_VERSION = '99.0.0'", python.read_text())
        docker.write_text(docker.read_text().replace('PI_VERSION=99.0.0', 'PI_VERSION=98.0.0'))
        result = updates.update(self.root, apply=True, components=['pi'])
        self.assertEqual(result['pi']['status'], 'failed')
        self.assertIn('differs', result['pi']['error'])
        self.assertIn("PI_VERSION = '99.0.0'", python.read_text())

    def test_editor_missing_checksum_does_not_change_version(self):
        file = self.root / 'ai_sandbox_factory/browser_editor.py'
        original = file.read_bytes()
        release = {'tag_name': 'v99.0.0', 'assets': [{'name': 'code-server-99.0.0-linux-amd64.tar.gz', 'digest': ''}]}
        with patch.object(updates, 'request', return_value=release):
            result = updates.update(self.root, apply=True, components=['editor'])
        self.assertEqual(result['editor']['status'], 'failed')
        self.assertEqual(file.read_bytes(), original)

    def test_base_refresh_ignores_stage_names_and_resolves_repeated_tag_once(self):
        directory = self.root / 'ai_sandbox_factory/containers'
        shutil.rmtree(directory)
        directory.mkdir(parents=True)
        file = directory / 'Dockerfile'
        file.write_text('FROM docker.io/library/node:24 AS tools\nFROM tools\nFROM ${BASE_IMAGE}\nFROM docker.io/library/node:24 AS second\n')
        with patch.object(updates.subprocess, 'check_output', return_value=b'{"mediaType":"application/vnd.oci.image.index.v1+json"}') as inspect:
            result = updates.update(self.root, apply=True, components=['bases'])
        inspect.assert_called_once()
        self.assertTrue(result['bases']['changed'])
        self.assertIn('FROM tools\nFROM ${BASE_IMAGE}\n', file.read_text())
        self.assertEqual(file.read_text().count('@sha256:'), 2)

    def test_owned_frostpi_source_is_never_advanced_by_agent_updates(self):
        source = self.root / 'ai_sandbox_factory/containers/dev/extensions.lock.json'
        original = source.read_bytes()
        with patch.object(updates, 'request', return_value={'version': '99.1.0'}):
            updates.update(self.root, apply=True, components=['pi', 'codex'])
        self.assertEqual(source.read_bytes(), original)


    def test_aws_tag_and_digest_advance_together_if_later_base_refresh_fails(self):
        name = 'ai_sandbox_factory/containers/aws-access/client/Dockerfile'
        raw = b'{"schemaVersion":2,"test":"new aws manifest"}'
        def request(url, **kwargs):
            if 'mitmproxy' in url:
                return {'info': {'version': '12.3.1'}}
            if '/aws/aws-cli/' in url:
                return [{'name': '2.99.0'}]
            return {'tag_name': 'v10.5.2'}
        with patch.object(updates, 'bundled_installers',return_value=[]), patch.object(updates, 'request', side_effect=request), patch.object(
                updates.subprocess, 'check_output', side_effect=[raw, subprocess.CalledProcessError(1, ['skopeo'], stderr=b'registry unavailable')]):
            result = updates.update(self.root, apply=True, components=['tools', 'bases'])
        self.assertEqual(result['tools']['status'], 'updated')
        self.assertEqual(result['bases']['status'], 'failed')
        self.assertIn('aws-cli:2.99.0@sha256:' + hashlib.sha256(raw).hexdigest(), (self.root / name).read_text())

    def test_failed_aws_digest_resolution_keeps_the_old_tag_and_digest(self):
        path = self.root / 'ai_sandbox_factory/containers/aws-access/client/Dockerfile'
        original = path.read_bytes()
        with patch.object(updates, 'request', side_effect=[{'info': {'version':'12.3.1'}}, [{'name':'2.99.0'}]]), patch.object(
                updates.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(1, ['skopeo'], stderr=b'HTTP 503')):
            result = updates.update(self.root, apply=True, components=['tools'])
        self.assertEqual(result['tools']['status'], 'failed')
        self.assertIn('HTTP 503', result['tools']['error'])
        self.assertEqual(path.read_bytes(), original)

    def test_weekly_npm_refresh_updates_transitive_lock_with_unchanged_direct_version(self):
        folder = self.root / 'ai_sandbox_factory/containers/test-tool'
        folder.mkdir()
        package = {'dependencies': {'tool':'1.0.0','owned':'github:example/owned#' + 'a'*40}}
        (folder / 'package.json').write_text(json.dumps(package))
        before = {'packages': {'':package,'node_modules/tool':{'version':'1.0.0'},'node_modules/child':{'version':'1.0.0'}}}
        (folder / 'package-lock.json').write_text(json.dumps(before))
        def compile(args, **kwargs):
            self.assertEqual(args[:4], ['npm','update','--package-lock-only','--ignore-scripts'])
            output = Path(kwargs['cwd']) / 'package-lock.json'
            value = json.loads(output.read_text())
            value['packages']['node_modules/child']['version'] = '1.0.1'
            output.write_text(json.dumps(value))
            return subprocess.CompletedProcess(args,0)
        with patch.object(updates, 'request', return_value={'version':'1.0.0'}), patch.object(updates.subprocess,'run',side_effect=compile):
            result = updates.update(self.root, apply=True, components=['npm'])
        self.assertTrue(result['npm']['changed'])
        self.assertEqual(json.loads((folder / 'package.json').read_text()), package)
        self.assertEqual(json.loads((folder / 'package-lock.json').read_text())['packages']['node_modules/child']['version'], '1.0.1')

    def test_python_compiler_refreshes_document_and_aws_sdk_locks(self):
        names = ['ai_sandbox_factory/containers/dev/document-requirements',
                 'ai_sandbox_factory/containers/aws-access/client/requirements']
        for name in names:
            path = self.root / (name + '.txt')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('fixture==1.0.0\n')
            path.with_suffix('.in').write_text('fixture\n')
        compiled = []
        def compile(args, **kwargs):
            compiled.append(args[-1])
            self.assertEqual(kwargs['env']['PIP_INDEX_URL'],'https://pypi.org/simple')
            Path(args[args.index('--output-file')+1]).write_text('--index-url https://private.invalid\nfixture==1.0.1\n')
            return subprocess.CompletedProcess(args,0)
        with patch('sys.version_info',(3,13,0)), patch.object(updates.subprocess,'run',side_effect=compile):
            result = updates.update(self.root,apply=True,components=['python'])
        self.assertEqual(result['python']['status'],'updated')
        self.assertEqual(len(compiled),2)
        for name in names:
            text = (self.root / (name+'.txt')).read_text()
            self.assertIn('fixture==1.0.1',text)
            self.assertNotIn('private.invalid',text)
    def test_bundled_npm_and_pip_versions_update_every_managed_stage_together(self):
        first=self.root/'ai_sandbox_factory/containers/Dockerfile-one'
        second=self.root/'ai_sandbox_factory/containers/Dockerfile-two'
        first.write_text('FROM node:24\nARG NPM_VERSION=12.0.0\nARG PIP_VERSION=26.0\n')
        second.write_text('FROM node:22\nARG NPM_VERSION=12.0.0\nFROM node:24\nARG NPM_VERSION=12.0.0\n')
        # Isolate the two synthetic recipes from the normal shared installer pins.
        with patch.object(updates,'npm_version',return_value='99.0.0'), patch.object(updates,'request',return_value={'info':{'version':'99.1'}}):
            # Test a private recipe tree, so this assertion is independent of
            # future additions to the actual public image set.
            fixture=Path(self.enterContext(tempfile.TemporaryDirectory()))
            (fixture/'ai_sandbox_factory').mkdir()
            a=fixture/'ai_sandbox_factory/Dockerfile-a';b=fixture/'ai_sandbox_factory/Dockerfile-b'
            a.write_bytes(first.read_bytes());b.write_bytes(second.read_bytes())
            edits=updates.Edits(fixture)
            changed=updates.bundled_installers(edits);edits.save()
        self.assertEqual(len(changed),2)
        self.assertEqual(b.read_text().count('ARG NPM_VERSION=99.0.0'),2)
        self.assertIn('ARG PIP_VERSION=99.1',a.read_text())

    def test_inconsistent_installer_pins_fail_without_writing_any_stage(self):
        fixture=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (fixture/'ai_sandbox_factory').mkdir()
        for suffix,version in [('a','12.0.0'),('b','12.1.0')]:
            (fixture/f'ai_sandbox_factory/Dockerfile-{suffix}').write_text(f'FROM node:24\nARG NPM_VERSION={version}\n')
        edits=updates.Edits(fixture)
        with self.assertRaisesRegex(ValueError,'NPM_VERSION differs'):
            updates.bundled_installers(edits)
        self.assertEqual(edits.files,{})

    def test_npm_security_updates_preserve_the_supported_install_script_behavior(self):
        versions={'versions':{'11.21.2':{},'11.21.3':{},'12.2.3':{},'11.22.0-beta.1':{}}}
        with patch.object(updates,'request',return_value=versions):
            self.assertEqual(updates.npm_version('npm',major='11'),'11.21.3')

    def test_apt_cache_refresh_alone_is_not_reported_as_a_software_update(self):
        fixture=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (fixture/'ai_sandbox_factory').mkdir()
        raw=b'{"schemaVersion":2}'
        digest=hashlib.sha256(raw).hexdigest()
        docker=fixture/'ai_sandbox_factory/Dockerfile'
        docker.write_text(f'FROM debian:trixie@sha256:{digest}\nARG FACTORY_APT_REFRESH=2000-01-01\nRUN apt-get update && apt-get upgrade -y\n')
        with patch.object(updates.subprocess,'check_output',return_value=raw):
            result=updates.update(fixture,apply=True,components=['bases'])
        self.assertFalse(result['bases']['changed'])
        self.assertNotIn('FACTORY_APT_REFRESH=2000-01-01',docker.read_text())

    def test_second_same_day_attempt_refreshes_local_cache_even_if_base_lookup_fails(self):
        fixture=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (fixture/'ai_sandbox_factory').mkdir()
        raw=b'{"schemaVersion":2}'
        digest=hashlib.sha256(raw).hexdigest()
        docker=fixture/'ai_sandbox_factory/Dockerfile'
        docker.write_text(f'FROM debian:trixie@sha256:{digest}\nARG FACTORY_APT_REFRESH=old\n')
        with patch.dict(updates.os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}),patch.object(updates.subprocess,'check_output',return_value=raw):
            updates.update(fixture,apply=True,components=['bases'])
        self.assertIn('FACTORY_APT_REFRESH=123-1',docker.read_text())
        with patch.dict(updates.os.environ,{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}),patch.object(updates.subprocess,'check_output',side_effect=subprocess.CalledProcessError(1,['skopeo'],stderr=b'registry unavailable')):
            result=updates.update(fixture,apply=True,components=['bases'])
        self.assertEqual(result['bases']['status'],'failed')
        self.assertIn('FACTORY_APT_REFRESH=123-2',docker.read_text())
        self.assertIn('FROM debian:trixie@sha256:'+digest,docker.read_text())


class AgentInstaller(unittest.TestCase):
    def test_corrupt_download_is_removed_before_it_can_be_installed(self):
        import io
        spec = importlib.util.spec_from_file_location('agent_installer', ROOT / 'ai_sandbox_factory/containers/primary/install-agent.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'agent'
            with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
                with self.assertRaisesRegex(ValueError, 'sha256 mismatch'):
                    installer.download({'url': 'https://example.invalid/agent', 'sha256': '0' * 64}, target)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
