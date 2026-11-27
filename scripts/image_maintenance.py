"""Coordinate Factory's weekly candidate branch, reviewed PR and image retention."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'kyuz0/ai-sandbox-factory'
BRANCH = 'codex/image-maintenance'
INDEX = 'ai_sandbox_factory/images.lock.json'
REPORT = '.github/image-maintenance.json'


def command(*args, **kwargs):
    result = subprocess.run(list(map(str, args)), check=True, text=True, capture_output=True, **kwargs)
    return result.stdout.strip()


def api(method, path, value=None):
    arguments = ['gh', 'api', '--method', method, path]
    if value is not None:
        arguments += ['--input', '-']
    text = command(*arguments, input=json.dumps(value) if value is not None else None)
    return json.loads(text) if text else None


def output(**values):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a') as file:
            for name, value in values.items():
                file.write(f'{name}={value}\n')
    print(json.dumps(values))


def repo():
    if os.environ.get('GITHUB_REPOSITORY', REPOSITORY) != REPOSITORY:
        raise ValueError('Container maintenance is configured only for ' + REPOSITORY)
    return REPOSITORY


def git_author():
    command('git', 'config', 'user.name', 'github-actions[bot]')
    command('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    command('gh', 'auth', 'setup-git')


def prepare(report, test=False):
    repo()
    git_author()
    branch = BRANCH + ('-test-' if test else '-staging-') + os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    old = command('git', 'ls-remote', 'origin', 'refs/heads/' + BRANCH).split() if not test else []
    if old:
        command('git', 'fetch', 'origin', BRANCH)
        author = command('git', 'log', '-1', '--format=%ae', 'FETCH_HEAD')
        if author != '41898282+github-actions[bot]@users.noreply.github.com':
            raise ValueError(f'{BRANCH} contains a maintainer commit. Preserve it and review the pending maintenance PR before retrying.')
    base = command('git', 'rev-parse', 'HEAD')
    command('git', 'checkout', '-B', branch)
    # Only component pins/locks changed by the updater are staged. The report
    # records version outcomes, never model conversations, credentials or logs.
    value = {'checked_at': datetime.now(timezone.utc).isoformat(),
             'candidate_run': os.environ.get('GITHUB_RUN_ID'), 'components': json.loads(report.read_text()),
             'base_commit': base, 'review_head': old[0] if old else ''}
    target = ROOT / REPORT
    target.write_text(json.dumps(value, indent=2) + '\n')
    command('git', 'add', 'ai_sandbox_factory', REPORT)
    command('git', 'commit', '-m', 'Update public container dependency candidates')
    command('git', 'push', 'origin', 'HEAD:refs/heads/' + branch)
    output(ref=command('git', 'rev-parse', 'HEAD'), branch=branch)


def index_commit(index, branch):
    repo()
    if not re.fullmatch(re.escape(BRANCH) + r'-(?:test|staging)-\d+-\d+', branch):
        raise ValueError(f'Unexpected maintenance branch {branch!r}')
    from build_factory_images import validate_index
    validate_index(index)
    git_author()
    command('git', 'checkout', '-B', branch)
    (ROOT / INDEX).write_bytes(index.read_bytes())
    command('git', 'add', INDEX)
    if command('git', 'diff', '--cached', '--name-only'):
        command('git', 'commit', '-m', 'Record published container candidates for verification')
    command('git', 'push', 'origin', 'HEAD:refs/heads/' + branch)
    output(ref=command('git', 'rev-parse', 'HEAD'), branch=branch)


def pull_request(sha, branch, run_url):
    repo()
    if command('git', 'rev-parse', 'HEAD') != sha:
        raise ValueError('The PR checkout does not match the candidate commit that passed testing')
    current = command('git', 'ls-remote', 'origin', 'refs/heads/' + branch).split()
    if not current or current[0] != sha:
        raise ValueError('The candidate branch changed during testing. Rerun maintenance against its current commit.')
    report = json.loads((ROOT / REPORT).read_text())
    rows = ['| Component | Previous | Available | Result |', '|---|---|---|---|']
    failed = []
    for name, row in report['components'].items():
        clean = lambda value: str(value).replace('|', '/').replace('\n', ' ')
        available = row.get('available', row.get('error', 'unknown'))
        rows.append(f'| {name} | {clean(row.get("current", "unchanged"))} | {clean(available)} | {row["status"]} |')
        if row['status'] == 'failed':
            failed.append(name)
    body = ('Update the bundled container dependencies and the tested prebuilt image set.\n\n' +
            '\n'.join(rows) + '\n\nThe exact published candidate images passed the required checks. '
            f'[Verification run]({run_url}).\n\n')
    if failed:
        body += 'This is a verified partial update. The following components remain unchanged and need attention: ' + ', '.join(failed) + '.\n\n'
    body += 'Merging runs the normal release checks. Existing sandboxes update through Factory’s explicit rebuild workflow.'
    inventories = os.environ.get('FACTORY_PROMOTION_INVENTORIES')
    if inventories and not meaningful_update(report, Path(inventories)):
        if failed:
            raise ValueError('No images were promoted; the weekly check failed for: ' + ', '.join(failed))
        print('Component versions and package inventories are unchanged. The tested candidates are not promoted.')
        return
    if re.fullmatch(re.escape(BRANCH) + r'-test-\d+-\d+', branch):
        if failed:
            raise ValueError('The test cycle left failed components unchanged: ' + ', '.join(failed))
        print('Test cycle verified; no production maintenance PR is created.')
        return
    if not re.fullmatch(re.escape(BRANCH) + r'-staging-\d+-\d+', branch):
        raise ValueError('Promotion requires a verified per-run staging branch')
    base = report.get('base_commit')
    if not isinstance(base, str) or not re.fullmatch('[a-f0-9]{40}', base):
        raise ValueError('The maintenance report has no source base commit; rebuild this candidate')
    latest = command('git', 'ls-remote', 'origin', 'refs/heads/main').split()[0]
    command('git', 'fetch', 'origin', latest)
    drift = subprocess.run(['git', 'diff', '--quiet', base, latest, '--',
        'ai_sandbox_factory', 'scripts', 'tests', '.github/workflows', '.github/image-security-exceptions.json',
        'pyproject.toml', 'MANIFEST.in'], text=True, capture_output=True)
    if drift.returncode == 1:
        raise ValueError('Factory code or image inputs changed on main during candidate testing. Rebuild before updating the review branch.')
    drift.check_returncode()
    lease = '--force-with-lease=refs/heads/' + BRANCH + ':' + report.get('review_head', '')
    command('gh', 'auth', 'setup-git')
    command('git', 'push', lease, 'origin', sha + ':refs/heads/' + BRANCH)
    branch = BRANCH
    api('POST', f'repos/{REPOSITORY}/statuses/{sha}', {
        'state': 'success', 'context': 'maintenance/verified', 'description': 'Published candidate set passed integration checks', 'target_url': run_url})
    if os.environ.get('FACTORY_AUTOMATED_PRS') != 'true':
        print(f'Verified update ready for manual PR creation: https://github.com/{REPOSITORY}/compare/main...{branch}')
        print('Automatic PR creation is disabled. No workflow approval permission is changed by this run.')
        if failed:
            raise ValueError('The verified subset leaves failed components unchanged: ' + ', '.join(failed))
        return
    pulls = api('GET', f'repos/{REPOSITORY}/pulls?state=open&head=kyuz0:{branch}&base=main')
    value = {'title': 'Update tested Factory container images', 'body': body}
    if pulls:
        api('PATCH', f'repos/{REPOSITORY}/pulls/{pulls[0]["number"]}', value)
    else:
        api('POST', f'repos/{REPOSITORY}/pulls', dict(value, head=branch, base='main'))
    if failed:
        raise ValueError('The safe subset is verified and ready for review, but the weekly cycle is incomplete: ' + ', '.join(failed))


def same_image_sources(previous):
    from build_factory_images import public_config, SMOKE
    from ai_sandbox_factory import image_sources, sandbox
    config=public_config()
    paths=sorted({str(path.relative_to(sandbox.ROOT.parent)) for role in SMOKE for path in image_sources.recipe_inputs(config,role)})
    difference = subprocess.run(['git','diff','--quiet','--ignore-matching-lines=^ARG FACTORY_APT_REFRESH=',
        previous,'HEAD','--',*paths],
        text=True,capture_output=True,cwd=ROOT)
    if difference.returncode not in (0,1):
        difference.check_returncode()
    return difference.returncode == 0


def meaningful_update(report, directory):
    """A new digest alone is not a software update."""
    old, pending, candidate, scanned = {}, {}, {}, {}
    packages = lambda value: sorted((row.get('name') or '', row.get('version') or '', row.get('purl') or '')
                                    for row in value.get('components', []))
    for metadata in directory.rglob('factory-image-review.json'):
        value = json.loads(metadata.read_text())
        if not value.get('current') or not value.get('image'):
            continue
        sbom = metadata.with_name('factory-image.cdx.json')
        if sbom.exists():
            role = value['image'].split('@')[0].split('ai-sandbox-factory-')[1]
            target = pending if value.get('kind') == 'pending' else old
            target[role] = packages(json.loads(sbom.read_text()))
            scanned[value['image']] = target[role]
    for sbom in directory.glob('image-*/*.cdx.json'):
        if sbom.parent.name.startswith('image-scan-'):
            continue
        role = sbom.name.removesuffix('.cdx.json')
        candidate[role] = packages(json.loads(sbom.read_text()))
    if candidate and set(candidate)-set(pending) and report.get('review_head'):
        # A digest shared by stable and pending is scanned once. Recover its
        # membership from the exact pending index rather than dropping a role.
        file=api('GET',f'repos/{REPOSITORY}/contents/{INDEX}?ref={report["review_head"]}')
        index=json.loads(base64.b64decode(file['content']))
        for platforms in index['images'].values():
            for reference in platforms.values():
                if reference in scanned:
                    role=reference.split('@')[0].split('ai-sandbox-factory-')[1]
                    pending[role]=scanned[reference]
    if candidate and set(candidate) <= set(pending) and report.get('review_head'):
        command('git','fetch','origin',report['review_head'])
        if same_image_sources(report['review_head']) and all(candidate[role] == pending[role] for role in candidate):
            print('The same recipes and packages already have a verified update waiting for review; preserve that branch.')
            return False
        return True
    if any(row.get('changed') for row in report['components'].values()):
        return True
    if not candidate or set(candidate) - set(old):
        print('The previous package inventory is incomplete; retain the candidate for review rather than claiming no changes.')
        return True
    base = report.get('base_commit')
    if not isinstance(base,str) or not re.fullmatch('[a-f0-9]{40}',base):
        print('The previous source revision is missing; retain the candidate for review.')
        return True
    previous = command('git','log','-1','--format=%H',base,'--',INDEX)
    if not previous or not same_image_sources(previous):
        return True
    return any(candidate[role] != old[role] for role in candidate)


def image_sets(root=ROOT):
    """Current release, previous release and newest owned maintenance candidate."""
    paths = [('stable', json.loads((root / INDEX).read_text()))]
    tags = command('git', 'tag', '--list', 'v*', '--sort=-creatordate').splitlines()
    released = 0
    for tag in tags:
        text = subprocess.run(['git', 'show', tag + ':' + INDEX], text=True, capture_output=True)
        if text.returncode:
            # Factory releases before prebuilt images have no image index.
            continue
        value = json.loads(text.stdout)
        if not value.get('images'):
            continue
        paths.append(('previous', value))
        released += 1
        if released == 2:
            break
    pending = api('GET', f'repos/{repo()}/pulls?state=open&head=kyuz0:{BRANCH}&base=main')
    head = pending[0]['head'] if pending else None
    if head is None:
        try:
            reference = api('GET', f'repos/{REPOSITORY}/git/ref/heads/{BRANCH}')
        except subprocess.CalledProcessError as error:
            if 'HTTP 404' not in (error.stderr or ''):
                raise
            reference = None
        if reference:
            sha = reference['object']['sha']
            statuses = api('GET', f'repos/{REPOSITORY}/commits/{sha}/status')['statuses']
            if any(row['context'] == 'maintenance/verified' and row['state'] == 'success' for row in statuses):
                head = {'sha': sha, 'repo': {'full_name': REPOSITORY}}
    if head:
        if head['repo']['full_name'] != REPOSITORY:
            raise ValueError('The maintenance image index is not from the Factory repository')
        file = api('GET', f'repos/{REPOSITORY}/contents/{INDEX}?ref={head["sha"]}')
        paths.append(('pending', json.loads(base64.b64decode(file['content']))))
    references = {}
    priority = {'previous': 0, 'pending': 1, 'stable': 2}
    for kind, value in paths:
        if value.get('version') != 1 or not isinstance(value.get('images'), dict):
            raise ValueError('Cannot scan an invalid Factory image index')
        for platforms in value['images'].values():
            for reference in platforms.values():
                if not re.fullmatch(r'docker.io/kyuz0/ai-sandbox-factory-[a-z-]+@sha256:[0-9a-f]{64}', reference):
                    raise ValueError(f'Invalid Factory image reference in scan inventory: {reference!r}')
                if priority[kind] >= priority.get(references.get(reference), -1):
                    references[reference] = kind
    if not references:
        raise ValueError('No Factory image digests were found; a missing inventory is not a clean vulnerability scan')
    output(matrix=json.dumps({'include': [{'image': reference, 'current': references[reference] != 'previous',
                                          'kind': references[reference]} for reference in sorted(references)]}))


def overdue():
    repo()
    pending = api('GET', f'repos/{REPOSITORY}/pulls?state=open&head=kyuz0:{BRANCH}&base=main')
    if pending:
        print(f'Waiting for review: {pending[0]["html_url"]}')
    runs = api('GET', f'repos/{REPOSITORY}/actions/workflows/maintenance.yml/runs?event=schedule&per_page=100')['workflow_runs']
    now = datetime.now(timezone.utc)
    latest = None
    for run in runs:
        if run['status'] != 'completed':
            continue
        jobs = api('GET', f'repos/{REPOSITORY}/actions/runs/{run["id"]}/jobs?per_page=100')['jobs']
        if any(job['name'] == 'Prepare verified weekly update' and job['conclusion'] == 'success' for job in jobs):
            latest = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
            break
    if latest is None:
        first = min((datetime.fromisoformat(run['created_at'].replace('Z', '+00:00')) for run in runs), default=now)
        if now - first <= timedelta(days=7):
            print('The first weekly cycle is pending within its initial weekly cadence.')
            return
        raise ValueError('No successful weekly maintenance cycle is recorded. Check the first scheduled cycle and pending PR.')
    # Seven days is the user's requested maintenance cadence, not a package-age cap.
    if now - latest > timedelta(days=7):
        raise ValueError('The weekly container maintenance cycle is overdue. ' +
                         ('A verified update is waiting for review.' if pending else 'Inspect the latest failed maintenance run.'))
    print('A successful maintenance cycle is recorded within the weekly cadence.')


def retention():
    """Report the release keep-set. This command never deletes registry contents."""
    keep = set()
    tags = command('git', 'tag', '--list', 'v*').splitlines()
    for tag in tags:
        text = subprocess.run(['git', 'show', tag + ':' + INDEX], text=True, capture_output=True)
        if text.returncode:
            continue
        value = json.loads(text.stdout)
        if value.get('version') != 1 or not isinstance(value.get('images'), dict):
            raise ValueError(f'Release {tag} has an invalid image index; retention must not delete anything')
        for platforms in value['images'].values():
            keep.update(platforms.values())
    print(json.dumps({'released_images_to_keep': sorted(keep), 'registry_deletion': False}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'index', 'pr', 'scan-set', 'overdue', 'retention'))
    parser.add_argument('--report', type=Path)
    parser.add_argument('--index', type=Path)
    parser.add_argument('--branch')
    parser.add_argument('--sha')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    if args.operation == 'prepare':
        prepare(args.report, args.test)
    elif args.operation == 'index':
        index_commit(args.index, args.branch)
    elif args.operation == 'pr':
        pull_request(args.sha, args.branch, os.environ['GITHUB_SERVER_URL'] + '/' + repo() + '/actions/runs/' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    elif args.operation == 'scan-set':
        image_sets()
    elif args.operation == 'overdue':
        overdue()
    else:
        retention()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        raise SystemExit(1)
