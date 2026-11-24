"""Exercise Factory's source snapshot, preview and copy across isolated volumes."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from ai_sandbox_factory import codex_history, sandbox, session_transfer


def engine(*args, **kwargs):
    result = subprocess.run(['podman', *args], check=True, capture_output=True, text=True, **kwargs)
    return result.stdout.strip()


def check(image):
    with tempfile.TemporaryDirectory(prefix='factory-copy-integration-') as temporary, ExitStack() as cleanup:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        configs = {}
        for agent in ('pi', 'codex', 'claude'):
            config = dict(name='copy-check-' + uuid4().hex[:10], engine='podman', primary_agent=agent,
                          workspace=str(project), uid=os.getuid(), gid=os.getgid())
            state = root / config['name']
            state.mkdir()
            (state / 'config.json').write_text(json.dumps(config))
            name, _, _, volume = sandbox.names(config)
            engine('volume', 'create', volume)
            cleanup.callback(engine, 'volume', 'rm', volume)
            engine('run', '-d', '--pull=never', '--network=none', '--cap-drop=ALL',
                   '--security-opt=label=disable', '--userns=keep-id:uid=1000,gid=1000',
                   '--label', f'{sandbox.LABEL}={config["name"]}', '--name', name,
                   '-v', volume + ':/home/node', '--entrypoint', 'sleep', image, 'infinity')
            cleanup.callback(engine, 'rm', '-f', name)
            engine('stop', name)
            configs[agent] = (config, state)
        pi, state = configs['pi']
        seed = [
            {'type': 'session', 'version': 3, 'id': str(uuid4()), 'cwd': '/workspace', 'timestamp': '2026-11-23T12:00:00Z'},
            {'type': 'message', 'id': 'u1', 'parentId': None, 'timestamp': '2026-11-23T12:00:01Z',
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Earlier synthetic question'}],
                         'timestamp': 1795435201000}},
            {'type': 'message', 'id': 'a1', 'parentId': 'u1', 'timestamp': '2026-11-23T12:00:02Z',
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Earlier synthetic answer'}],
                         'api': 'openai-responses', 'provider': 'fixture', 'model': 'fixture-model',
                         'stopReason': 'stop', 'timestamp': 1795435202000}},
        ]
        data = ''.join(json.dumps(row) + '\n' for row in seed)
        volume = sandbox.names(pi)[3]
        engine('run', '--rm', '-i', '--network=none', '--userns=keep-id:uid=1000,gid=1000',
               '--security-opt=label=disable', '-v', volume + ':/home/node',
               '--entrypoint', 'python3', image, '-c',
               'from pathlib import Path;import sys;p=Path("/home/node/.pi/agent/sessions/--workspace--/source.jsonl");'
               'p.parent.mkdir(parents=True,exist_ok=True);p.write_text(sys.stdin.read())', input=data)
        for source, destination in [('pi', 'claude'), ('pi', 'codex'), ('claude', 'pi'),
                                    ('claude', 'codex'), ('codex', 'pi'), ('codex', 'claude')]:
            config, source_state = configs[source]
            target, target_state = configs[destination]
            transfer = session_transfer.Transfer(config, source_state)
            try:
                valid = [row for row in transfer.rows if not row.get('error')]
                assert valid, transfer.rows
                manifest = transfer.preview(valid[0]['path'], target, target_state)
                identifier = manifest['target']['session_id']
                assert manifest['target']['format'] == destination
                result = transfer.apply()
                assert result['target']['session_id'] == identifier
                assert transfer.plan is None
                print(f'{source} → {destination}: production snapshot, preview and atomic install passed')
            finally:
                transfer.close()
        print('All six routes passed using the pinned isolated helper; no account credentials or model requests.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Cached Factory image containing Python 3')
    check(parser.parse_args().image)
