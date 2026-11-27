"""Retain released image tags and their inventory/verification evidence."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def retain(version, authfile, *, run=subprocess.run):
    if not re.fullmatch(r'v\d+\.\d+\.\d+\.\d+', version):
        raise ValueError(f'Expected a Factory CalVer release tag, got {version!r}')
    value = json.loads((ROOT / 'ai_sandbox_factory/images.lock.json').read_text())
    for platforms in value['images'].values():
        for reference in platforms.values():
            if not re.fullmatch(r'docker.io/kyuz0/ai-sandbox-factory-[a-z-]+@sha256:[0-9a-f]{64}', reference):
                raise ValueError(f'Invalid released image reference {reference!r}')
            repository, digest = reference.split('@')
            tag = repository + ':release-' + version
            # A retry may reuse the tag only for the identical manifest.
            existing = run(['skopeo', 'inspect', '--authfile', str(authfile), 'docker://' + tag],
                           check=False, text=True, capture_output=True)
            if existing.returncode == 0:
                actual = json.loads(existing.stdout)['Digest']
                if actual != digest:
                    raise ValueError(f'Refusing to move released image tag {tag}: {actual} differs from {digest}')
                continue
            if not any(text in existing.stderr.lower() for text in ('manifest unknown', 'name unknown')):
                raise ValueError(f'Cannot inspect release image tag {tag} (exit {existing.returncode}): {existing.stderr}')
            run(['skopeo', 'copy', '--all', '--preserve-digests', '--authfile', str(authfile),
                 'docker://' + reference, 'docker://' + tag], check=True)


def complete_evidence(directory, references):
    covered = set()
    for inventory in directory.rglob('*-inventory.json'):
        value = json.loads(inventory.read_text())
        if not re.fullmatch('[a-f0-9]{40}', value.get('source_commit', '')):
            continue
        for role, row in value.get('images', {}).items():
            reference = row.get('reference')
            if reference not in references:
                continue
            sbom = inventory.with_name(role + '.cdx.json')
            scan = inventory.with_name(role + '-scan.json')
            review = inventory.with_name(role + '-review.json')
            if not all(path.is_file() for path in (sbom, scan, review)):
                continue
            if (not json.loads(sbom.read_text()).get('components') or
                    not json.loads(scan.read_text()).get('Results') or
                    json.loads(review.read_text()).get('requires_review') is None):
                continue
            covered.add(reference)
    return references <= covered


def evidence(version, *, run=subprocess.run):
    report = ROOT / '.github/image-maintenance.json'
    run_id = None
    if report.exists():
        maintenance = json.loads(report.read_text())
        run_id = str(maintenance.get('candidate_run') or '')
        if not run_id.isdecimal():
            raise ValueError('The promoted image report has no candidate run ID; rebuild and verify the candidates')
    index = ROOT / 'ai_sandbox_factory/images.lock.json'
    references = {reference for platforms in json.loads(index.read_text())['images'].values() for reference in platforms.values()}
    with tempfile.TemporaryDirectory(prefix='factory-release-evidence-') as temporary:
        folder = Path(temporary)
        source = None
        # Released evidence outlives Actions artifacts. Reuse it when every
        # current digest is covered, even if this package release is months later.
        listed = run(['gh','api','--paginate','repos/kyuz0/ai-sandbox-factory/releases',
                      '--jq','.[] | select(any(.assets[]; .name == "factory-image-evidence.tar.gz")) | .tag_name'],
                     check=True,capture_output=True,text=True)
        for count, tag in enumerate(listed.stdout.splitlines()):
            # The small versioned index tells us whether this archive can cover
            # these digests. Do not download years of unrelated image evidence.
            saved = run(['git', 'show', tag + ':ai_sandbox_factory/images.lock.json'],
                        check=True, capture_output=True, text=True)
            released = json.loads(saved.stdout)
            retained = {reference for platforms in released['images'].values() for reference in platforms.values()}
            if not references <= retained:
                continue
            previous = folder / ('release-' + str(count))
            previous.mkdir()
            run(['gh','release','download',tag,'--repo','kyuz0/ai-sandbox-factory','--pattern',
                 'factory-image-evidence.tar.gz','--dir',str(previous)],check=True)
            with tarfile.open(previous / 'factory-image-evidence.tar.gz') as package:
                package.extractall(previous, filter='data')
            if complete_evidence(previous, references):
                source = previous
                break
        if source is None and run_id:
            source = folder / 'candidate'
            run(['gh', 'run', 'download', run_id, '--repo', 'kyuz0/ai-sandbox-factory',
                 '--pattern', 'image-*', '--dir', str(source)], check=True)
            if not complete_evidence(source, references):
                raise ValueError('Candidate evidence does not cover every promoted image digest with a source commit, SBOM, scan and review. Rebuild and verify the missing roles.')
        if source is None:
            raise ValueError('No retained evidence covers this image set. Publish and verify the candidates before releasing; an image index alone is not scan evidence.')
        archive = folder / 'factory-image-evidence.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            package.add(index, arcname='images.lock.json')
            if report.exists():
                package.add(report, arcname='maintenance.json')
            # Include only image inventories and scan evidence, never logs.
            for path in source.rglob('*.json'):
                if path.is_file() and (path.name.endswith(('-inventory.json','.cdx.json','-scan.json','-review.json'))):
                    package.add(path, arcname='images/' + path.parent.name + '/' + path.name)
        found = run(['gh', 'release', 'view', version, '--repo', 'kyuz0/ai-sandbox-factory'],
                    capture_output=True, text=True)
        if found.returncode:
            run(['gh', 'release', 'create', version, '--repo', 'kyuz0/ai-sandbox-factory',
                 '--title', 'AI Sandbox Factory ' + version,
                 '--notes', 'Tested Factory release. Image digests, dependency versions and scan evidence are attached.'], check=True)
        run(['gh', 'release', 'upload', version, str(archive), '--repo', 'kyuz0/ai-sandbox-factory', '--clobber'], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--authfile', type=Path, required=True)
    args = parser.parse_args()
    retain(args.version, args.authfile)
    evidence(args.version)
