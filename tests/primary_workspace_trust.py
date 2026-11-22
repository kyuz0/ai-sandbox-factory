"""Check workspace trust with the pinned Codex binary, offline and without credentials."""
import argparse
import json
from pathlib import Path
import runpy
import select
import subprocess
import time


def inside():
    command = runpy.run_path('/tmp/factory-launch.py')['command']
    saved = Path.home() / '.codex' / 'config.toml'
    saved.parent.mkdir(exist_ok=True)
    saved.write_text('[projects."/unrelated"]\ntrust_level = "untrusted"\n')
    for cwd in ('/workspace', '/workspace/v1.2', '/workspace/🌱', '/workspace/quoted "name"',
                '/workspace/back\\slash', '/workspace/control\x7f'):
        args = command('codex', 'editor', {'browser': False, 'working_directory': cwd}, '')
        process = subprocess.Popen(['/usr/local/bin/factory-agent', *args], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            requests = [
                {'id': 1, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'trust_check', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}},
                {'method': 'initialized'},
                {'id': 2, 'method': 'config/read', 'params': {}},
            ]
            for request in requests:
                process.stdin.write((json.dumps(request) + '\n').encode())
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not select.select([process.stdout], [], [], 1)[0]:
                    continue
                line = process.stdout.readline()
                if not line:
                    raise AssertionError('Codex exited before returning its configuration')
                message = json.loads(line)
                if message.get('id') != 2:
                    continue
                assert 'error' not in message, message.get('error')
                config = message['result']['config']
                assert config['projects'] == {
                    cwd: {'trust_level': 'trusted'}, '/unrelated': {'trust_level': 'untrusted'}
                }, config['projects']
                assert config['approval_policy'] == 'on-request'
                assert config['approvals_reviewer'] == 'auto_review'
                assert config['sandbox_mode'] == 'workspace-write'
                print(f'PASS: trusted {cwd!r}; automatic review retained', flush=True)
                break
            else:
                raise AssertionError('Codex did not return its configuration')
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Locally built Codex controller image')
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside()
    elif args.image:
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['podman', 'run', '--rm', '--network=none', '--security-opt=label=disable',
                        '--entrypoint', 'python3',
                        '-v', f'{Path(__file__).resolve()}:/tmp/check.py:ro',
                        '-v', f'{root / "pi_sandbox_factory/containers/primary/launch.py"}:/tmp/factory-launch.py:ro',
                        args.image, '/tmp/check.py', '--inside'], check=True)
    else:
        parser.error('--image is required')
