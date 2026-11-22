"""Exercise the shipped workspace MCP server without provider credentials."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import subprocess
import tempfile


async def check(image):
    name = 'factory-worker-test-' + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix=name) as folder:
        root = Path(folder)
        (root / 'cwd.json').write_text(json.dumps('/workspace'))
        subprocess.run(['podman', 'run', '-d', '--name', name, '--network=none',
                        '--userns=keep-id',
                        '--cap-drop=ALL', '--security-opt=no-new-privileges',
                        '-v', str(root) + ':/run/primary-runtime:ro,z',
                        '--tmpfs', '/workspace:rw,mode=1777', '--entrypoint', 'sleep', image, 'infinity'], check=True)
        process = None
        try:
            process = await asyncio.create_subprocess_exec('podman', 'exec', '-i', '-e', 'FACTORY_PRIMARY_AGENT=claude',
                name, 'node', '/opt/primary/worker-tools.mjs', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            def send(message):
                process.stdin.write((json.dumps(message) + '\n').encode())
            async def receive():
                return json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            def call(identifier, command):
                send({'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
                      'params': {'name': 'workspace_shell', 'arguments': {'command': command}}})
            call(0, 'printf baseline')
            baseline = await receive()
            assert 'baseline' in json.dumps(baseline), baseline
            # The measured ordinary workload is one command. Exercise ten together,
            # with five cancelled and five allowed to finish independently.
            for identifier in range(1, 11):
                call(identifier, f'echo $$ > /workspace/pid-{identifier}; printf ready-{identifier}; '
                     f'while [ ! -f /workspace/release-{identifier} ]; do sleep 0.05; done; printf done-{identifier}')
            send({'jsonrpc': '2.0', 'id': 11, 'method': 'ping'})
            assert (await receive())['id'] == 11, 'Running commands blocked the MCP connection'
            subprocess.run(['podman', 'exec', name, 'bash', '-c',
                'until [ "$(find /workspace -name \'pid-*\' | wc -l)" -eq 10 ]; do sleep 0.05; done'],
                check=True, timeout=30)
            for identifier in range(1, 6):
                send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': identifier}})
            call(12, 'touch /workspace/release-{6,7,8,9,10}')
            results = {}
            while len(results) < 11:
                result = await receive()
                results[result['id']] = result
            for identifier in range(1, 6):
                assert f'done-{identifier}' not in json.dumps(results[identifier])
            for identifier in range(6, 11):
                assert f'done-{identifier}' in json.dumps(results[identifier])
            call(13, 'printf recovered')
            assert 'recovered' in json.dumps(await receive())
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 30) == 0
            subprocess.run(['podman', 'exec', name, 'bash', '-c',
                'for f in /workspace/pid-*; do if kill -0 "$(cat "$f")" 2>/dev/null; then exit 1; fi; done'], check=True)
            print('PASS: baseline, ten concurrent commands, five cancellations, connection recovery and process cleanup.')
        finally:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            subprocess.run(['podman', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    asyncio.run(check(parser.parse_args().image))
