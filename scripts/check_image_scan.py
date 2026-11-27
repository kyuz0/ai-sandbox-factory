"""Require image scans to complete and surface actionable findings for review."""
import argparse
from datetime import date
import json
from pathlib import Path
import urllib.request

KEV = 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json'


def review(report, exploited, exceptions, today=None, role=None):
    today = today or date.today()
    if not isinstance(report.get('Results'), list) or not report['Results']:
        raise ValueError('The image scan has no Results inventory. A missing scan is not a clean result.')
    approved = {}
    for item in exceptions:
        for field in ('id', 'package', 'installed', 'role', 'target', 'reason', 'review_before'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f'Image scan exception is missing {field}: {item!r}')
        date.fromisoformat(item['review_before'])
        approved[(item['id'], item['package'], item['installed'], item['role'], item['target'])] = item
    findings, needed = [], []
    for result in report['Results']:
        target = result.get('Target', '')
        if result.get('Class') == 'os-pkgs':
            target = result.get('Type', 'os') + ':' + report.get('Metadata', {}).get('OS', {}).get('Name', '')
        for issue in result.get('Vulnerabilities') or []:
            row = {'id': issue['VulnerabilityID'], 'package': issue['PkgName'], 'target': target, 'role': role,
                   'installed': issue['InstalledVersion'], 'fixed': issue.get('FixedVersion', ''),
                   'severity': issue.get('Severity', 'UNKNOWN')}
            row['known_exploited'] = row['id'] in exploited
            # This is a maintenance review policy, not a claim that scanner
            # presence proves reachability. No-fix findings remain in the report.
            row['needs_review'] = row['known_exploited'] or (row['severity'] in ('HIGH', 'CRITICAL') and bool(row['fixed']))
            exception = approved.get((row['id'], row['package'], row['installed'], role, target))
            row['exception'] = exception if exception and date.fromisoformat(exception['review_before']) > today else None
            if exception and not row['exception']:
                row['expired_exception'] = exception['review_before']
            findings.append(row)
            if row['needs_review'] and not row['exception']:
                needed.append(row)
    return {'findings': findings, 'requires_review': needed,
            'inventory_targets': [result.get('Target') for result in report['Results']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--exceptions', type=Path, default=Path('.github/image-security-exceptions.json'))
    parser.add_argument('--kev', type=Path, help='Use a previously downloaded current CISA feed')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', help='Record the scanned content-addressed image')
    parser.add_argument('--current', choices=('true', 'false'), default='false')
    parser.add_argument('--enforce', choices=('true', 'false'), default='true',
                        help='Retained older images are reported without blocking a current-image refresh')
    parser.add_argument('--role', help='Factory image role; exceptions never carry between roles')
    parser.add_argument('--kind', choices=('stable', 'pending', 'previous'))
    args = parser.parse_args()
    if args.kev:
        feed = json.loads(args.kev.read_text())
    else:
        with urllib.request.urlopen(KEV, timeout=60) as response:
            feed = json.load(response)
    if not isinstance(feed.get('vulnerabilities'), list):
        raise ValueError('CISA returned no vulnerability list. Retry; the scan cannot claim known-exploitation coverage.')
    exploited = {row['cveID'] for row in feed['vulnerabilities']}
    import re
    matched = re.fullmatch(r'docker.io/kyuz0/ai-sandbox-factory-([a-z-]+)@sha256:[a-f0-9]{64}', args.image or '')
    role = args.role or (matched[1] if matched else None)
    if not role:
        raise ValueError('An image role is required to evaluate role-specific vulnerability reviews; pass --role or a Factory --image digest')
    result = review(json.loads(args.report.read_text()), exploited, json.loads(args.exceptions.read_text()), role=role)
    result.update(image=args.image, current=args.current == 'true', kind=args.kind)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for row in result['requires_review']:
        print(f'{row["id"]}: {row["package"]} {row["installed"]} in {row["target"]}; fixed: {row["fixed"] or "not available"}; '
              f'known exploited: {row["known_exploited"]}. Review the image finding before promotion.')
    return int(args.enforce == 'true' and bool(result['requires_review']))


if __name__ == '__main__':
    raise SystemExit(main())
