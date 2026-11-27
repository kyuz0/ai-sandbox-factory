"""Retained image tags must never move when release publication is retried."""
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_images', ROOT / 'scripts/release_factory_images.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseImageRetention(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(release, 'ROOT', self.root))
        (self.root / 'ai_sandbox_factory').mkdir()
        self.digest = 'sha256:' + 'a' * 64
        self.reference = 'docker.io/kyuz0/ai-sandbox-factory-gateway@' + self.digest
        self.index = self.root / 'ai_sandbox_factory/images.lock.json'
        self.index.write_text(json.dumps({'version':1,'images':{'recipe':{'linux/amd64':self.reference}}}))
        self.auth = self.root / 'registry-auth.json'

    def test_new_release_tag_preserves_the_exact_manifest(self):
        run = Mock(side_effect=[subprocess.CompletedProcess([],1,'','manifest unknown'),subprocess.CompletedProcess([],0)])
        release.retain('v2026.11.27.123456', self.auth, run=run)
        copied = run.call_args_list[1].args[0]
        self.assertIn('--preserve-digests', copied)
        self.assertEqual(copied[-2], 'docker://' + self.reference)
        self.assertEqual(copied[-1], 'docker://docker.io/kyuz0/ai-sandbox-factory-gateway:release-v2026.11.27.123456')

    def test_retry_of_same_release_does_not_copy_or_move_an_image(self):
        run = Mock(return_value=subprocess.CompletedProcess([],0,json.dumps({'Digest':self.digest}),''))
        release.retain('v2026.11.27.123456',self.auth,run=run)
        run.assert_called_once()

    def test_existing_release_with_different_digest_is_never_overwritten(self):
        run=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps({'Digest':'sha256:'+'b'*64}),''))
        with self.assertRaisesRegex(ValueError,'Refusing to move'):
            release.retain('v2026.11.27.123456',self.auth,run=run)
        run.assert_called_once()

    def test_registry_transport_or_permission_failure_is_not_treated_as_missing_tag(self):
        for message in ('connection reset by peer','unauthorized: invalid token','TLS certificate error'):
            with self.subTest(message=message):
                run=Mock(return_value=subprocess.CompletedProcess([],1,'',message))
                with self.assertRaisesRegex(ValueError,'Cannot inspect'):
                    release.retain('v2026.11.27.123456',self.auth,run=run)
                run.assert_called_once()

    def test_missing_candidate_evidence_fails_instead_of_publishing_incomplete_release(self):
        (self.root / '.github').mkdir()
        (self.root / '.github/image-maintenance.json').write_text('{}')
        run=Mock()
        with self.assertRaisesRegex(ValueError,'no candidate run ID'):
            release.evidence('v2026.11.27.123456',run=run)
        run.assert_not_called()

    def test_non_release_version_is_rejected_before_registry_access(self):
        run=Mock()
        with self.assertRaisesRegex(ValueError,'CalVer'):
            release.retain('latest',self.auth,run=run)
        run.assert_not_called()


class ReleaseImageEvidence(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(release, 'ROOT', self.root))
        self.refs = {
            role: 'docker.io/kyuz0/ai-sandbox-factory-' + role + '@sha256:' + digit * 64
            for role, digit in [('gateway', 'a'), ('editor', 'b')]
        }
        self.index = self.root / 'ai_sandbox_factory/images.lock.json'
        self.index.parent.mkdir()
        self.index.write_text(json.dumps({'images': {
            role: {'linux/amd64': ref} for role, ref in self.refs.items()
        }}))
        self.report = self.root / '.github/image-maintenance.json'
        self.report.parent.mkdir()
        self.report.write_text(json.dumps({'candidate_run': 42}))
        self.candidate = {}
        for role, reference in self.refs.items():
            directory = 'image-' + role + '/'
            self.candidate.update({
                directory + role + '-inventory.json': {
                    'source_commit': 'c' * 40, 'images': {role: {'reference': reference}},
                },
                directory + role + '.cdx.json': {'components': [{'name': 'example'}]},
                directory + role + '-scan.json': {'Results': [{'Target': 'example'}]},
                directory + role + '-review.json': {'requires_review': False},
            })
        self.candidate['image-editor/raw.log'] = 'raw diagnostic output'
        self.candidate['image-editor/debug.json'] = {'log': 'raw diagnostic output'}
        self.archives = {}
        self.release_indices = {}
        self.archive_downloads = []
        self.downloads = []
        self.uploads = []

    def run_command(self, args, **kwargs):
        if args[:2] == ['git', 'show']:
            tag, path = args[2].split(':', 1)
            self.assertEqual(path, 'ai_sandbox_factory/images.lock.json')
            return subprocess.CompletedProcess(args, 0, self.release_indices[tag], '')
        self.assertEqual(args[0], 'gh')
        stdout = ''
        if args[1] == 'api':
            stdout = '\n'.join(self.archives)
        elif args[1:3] == ['release', 'download']:
            target = Path(args[args.index('--dir') + 1]) / 'factory-image-evidence.tar.gz'
            self.archive_downloads.append(args[3])
            target.write_bytes(self.archives[args[3]])
        elif args[1:3] == ['run', 'download']:
            self.downloads.append(args[3])
            self.assertEqual(args[3], '42')
            self.assertIsNotNone(self.candidate, 'Attempted to download an expired artifact')
            for name, value in self.candidate.items():
                target = Path(args[args.index('--dir') + 1]) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(value))
        elif args[1:3] == ['release', 'upload']:
            self.uploads.append(args[3])
            self.release_indices[args[3]] = self.index.read_text()
            self.archives[args[3]] = Path(args[4]).read_bytes()
        elif args[1:3] != ['release', 'view']:
            self.fail(f'Unexpected gh command {args}')
        return subprocess.CompletedProcess(args, 0, stdout, '')

    def test_release_archive_survives_artifact_expiry_and_republication(self):
        expected = {'images.lock.json', 'maintenance.json'} | {
            'images/' + name for name in self.candidate
            if name.endswith(('-inventory.json', '.cdx.json', '-scan.json', '-review.json'))
        }
        for serial in range(3):
            version = f'v2026.11.27.{serial}'
            release.evidence(version, run=self.run_command)
            archive = self.archives[version]
            with tarfile.open(fileobj=io.BytesIO(archive)) as package:
                self.assertCountEqual(package.getnames(), expected)
                self.assertEqual(package.extractfile('images.lock.json').read(), self.index.read_bytes())
            self.archives = {version: archive}
            self.candidate = None
        self.assertEqual(self.downloads, ['42'])
        self.assertEqual(self.archive_downloads, ['v2026.11.27.0', 'v2026.11.27.1'])

    def test_skips_unrelated_archive_before_reusing_a_matching_release(self):
        version = 'v2026.11.27.1'
        release.evidence(version, run=self.run_command)
        unrelated = 'v2026.11.27.2'
        self.release_indices[unrelated] = self.index.read_text().replace(
            self.refs['gateway'], self.refs['editor']
        )
        self.archives = {unrelated: b'unused archive', **self.archives}
        self.candidate = None
        release.evidence('v2026.11.27.3', run=self.run_command)
        self.assertEqual(self.archive_downloads, [version])
        self.assertEqual(self.downloads, ['42'])

    def test_changed_digest_requires_fresh_candidate_despite_matching_role(self):
        release.evidence('v2026.11.27.1', run=self.run_command)
        old = self.refs['editor']
        new = old.split('@')[0] + '@sha256:' + 'd' * 64
        self.index.write_text(self.index.read_text().replace(old, new))
        with self.assertRaisesRegex(ValueError, 'every promoted image digest'):
            release.evidence('v2026.11.27.2', run=self.run_command)
        self.assertEqual(self.uploads, ['v2026.11.27.1'])
        self.candidate['image-editor/editor-inventory.json']['images']['editor']['reference'] = new
        release.evidence('v2026.11.27.2', run=self.run_command)
        self.assertEqual(self.downloads, ['42'] * 3)
        self.assertEqual(self.archive_downloads, [])
        with tarfile.open(fileobj=io.BytesIO(self.archives['v2026.11.27.2'])) as package:
            inventory = json.load(package.extractfile('images/image-editor/editor-inventory.json'))
            self.assertEqual(inventory['images']['editor']['reference'], new)

    def test_each_digest_requires_source_sbom_scan_and_review(self):
        for name, invalid in [
            ('editor-inventory.json', {'source_commit': 'HEAD', 'images': {
                'editor': {'reference': self.refs['editor']},
            }}),
            ('editor.cdx.json', {'components': []}),
            ('editor-scan.json', {'Results': []}),
            ('editor-review.json', {}),
        ]:
            key = 'image-editor/' + name
            for value in (None, invalid):
                with self.subTest(name=name, value=value), patch.dict(self.candidate):
                    if value is None:
                        del self.candidate[key]
                    else:
                        self.candidate[key] = value
                    with self.assertRaisesRegex(ValueError, 'every promoted image digest'):
                        release.evidence('v2026.11.27.1', run=self.run_command)
                    self.assertEqual(self.uploads, [])

    def test_index_alone_cannot_be_published_as_evidence(self):
        self.report.unlink()
        with self.assertRaisesRegex(ValueError, 'No retained evidence'):
            release.evidence('v2026.11.27.1', run=self.run_command)
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.uploads, [])


if __name__ == '__main__':
    unittest.main()
