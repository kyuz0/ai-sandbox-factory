"""Scan the exact current candidate images again at the promotion boundary."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

from build_factory_images import validate_index
from check_image_scan import KEV, review, exploited_ids


def scan(index, roles, output, *, executable='trivy'):
    references = validate_index(index, roles)
    output.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(KEV, timeout=60) as response:
        feed = json.load(response)
    exploited = exploited_ids(feed)
    exceptions = json.loads(Path('.github/image-security-exceptions.json').read_text())
    with tempfile.TemporaryDirectory(prefix='factory-scan-socket-') as temporary:
        socket = Path(temporary) / 'podman.sock'
        with (output / 'podman-service.log').open('w') as log:
            server = subprocess.Popen(['podman','system','service','--time=0','unix:' + str(socket)],stdout=log,stderr=log)
            try:
                while not socket.exists():
                    if server.poll() is not None:
                        raise ValueError('The scan-only Podman service exited: ' + (output / 'podman-service.log').read_text())
                    time.sleep(.05)
                blocked = []
                for role, reference in references.items():
                    sbom = output / (role + '.cdx.json')
                    report = output / (role + '-scan.json')
                    subprocess.run([executable,'image','--image-src','podman','--podman-host',str(socket),
                                    '--format','cyclonedx','--output',str(sbom),reference],check=True)
                    subprocess.run([executable,'sbom','--format','json','--output',str(report),str(sbom)],check=True)
                    result = review(json.loads(report.read_text()),exploited,exceptions,role=role)
                    result.update(image=reference,kind='pending',current=True)
                    (output / (role + '-review.json')).write_text(json.dumps(result,indent=2) + '\n')
                    print(f'{role}: {result["policy"]} scan; {len(result["findings"])} findings, '
                          f'{len(result["requires_review"])} blocking review findings. Full report: {report}')
                    for finding in result['requires_review']:
                        blocked.append(f'{role}: {finding["id"]} in {finding["package"]} {finding["installed"]}; fixed {finding["fixed"] or "not available"}')
                if blocked:
                    raise ValueError('Image findings need review before promotion:\n' + '\n'.join(blocked))
            finally:
                server.terminate()
                server.wait()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--roles',nargs='+')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    scan(args.index,args.roles,args.output)
