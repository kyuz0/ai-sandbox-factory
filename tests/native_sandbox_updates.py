"""Check native hook delivery with pinned clients and synthetic local inference.

Run with --agent codex|claude --image CACHED_WORKSPACE_IMAGE. No sign-in,
external network, saved sandbox or project is used.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from session_migrate_native import provider

INITIAL_CONTEXT = 'Original Factory settings fixture. Network is Restricted; use the inherited proxy and allowlist.'


def child(agent):
    root = Path(tempfile.mkdtemp(prefix='factory-context-'))
    home = root / 'home'; home.mkdir()
    work = root / 'work'; work.mkdir()
    client = home / ('.codex' if agent == 'codex' else '.claude'); client.mkdir()
    state = Path('/run/primary-runtime/sandbox-state.json')
    state.parent.mkdir(parents=True, exist_ok=True)
    def snapshot(revision, mode):
        state.write_text(json.dumps({'revision': revision, 'network': mode,
            'values': {'Network': 'Restricted' if mode == 'allowlist' else 'Unrestricted',
                       'Browser': f'Off (fixture {revision})'}}))
        reference = Path('/reference-fixtures') / (revision + '.md')
        (state.parent / 'sandbox-context.md').write_text(reference.read_text())
        current = Path('/opt/sandbox/context.md').read_text()
        assert '# Current sandbox configuration' in current and 'Examples for this sandbox' in current
        assert ('Network is Restricted' if mode == 'allowlist' else 'Network is Unrestricted') in current
    env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'TERM': 'dumb',
           'NO_COLOR': '1', 'TMPDIR': str(root),
           'NODE_OPTIONS': '--require=/factory/ai_sandbox_factory/containers/editor/watch-fallback.cjs',
           'NO_PROXY': 'localhost,127.0.0.1,::1', 'no_proxy': 'localhost,127.0.0.1,::1'}
    with provider() as server:
        endpoint = f'http://127.0.0.1:{server.server_address[1]}'
        if agent == 'codex':
            env.update(CODEX_HOME=str(client), FIXTURE_API_KEY='synthetic')
            (client / 'config.toml').write_text('developer_instructions = ' + json.dumps(INITIAL_CONTEXT) + '\n'
                'model = "fixture-model"\nmodel_provider = "fixture"\n'
                '[model_providers.fixture]\nname = "Fixture"\nbase_url = "' + endpoint + '/v1"\n'
                'env_key = "FIXTURE_API_KEY"\nwire_api = "responses"\nrequires_openai_auth = false\n')
            first = ['/usr/local/bin/factory-agent', 'exec', '--skip-git-repo-check', '--json', 'Reply OK.']
        else:
            import sys
            sys.path.insert(0, '/factory/ai_sandbox_factory/containers/primary')
            import claude_client
            claude_client.configure(client, {'working_directory': str(work), 'native_approval': 'manual'})
            (client / 'CLAUDE.md').write_text(INITIAL_CONTEXT + '\n')
            env.update(CLAUDE_CONFIG_DIR=str(client), ANTHROPIC_API_KEY='synthetic', ANTHROPIC_BASE_URL=endpoint,
                DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1', DISABLE_AUTOUPDATER='1',
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
            first = ['/usr/local/bin/factory-agent', '--print', '--verbose', '--output-format', 'stream-json',
                     '--model', 'claude-haiku-4-5-20251001', '--no-chrome', 'Reply OK.']
        session = None
        for revision, mode in [('one', 'allowlist'), ('two', 'unrestricted'), ('three', 'allowlist')]:
            snapshot(revision, mode)
            command = list(first)
            if session:
                if agent == 'codex':
                    command = [first[0], 'exec', 'resume', session, '--skip-git-repo-check', '--json', 'Reply OK again.']
                else:
                    command[1:1] = ['--resume', session]
            start = len(server.requests)
            receipts = home / '.local/state/ai-sandbox-factory/context-receipts'
            previous_records = set(receipts.glob('*.json'))
            result = subprocess.run(command, env=env, cwd=work, capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stdout + result.stderr
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            if not session:
                session = next((row.get('thread_id') or row.get('session_id') for row in events
                                if row.get('thread_id') or row.get('session_id')), None)
                assert session, result.stdout
            requests = json.dumps(server.requests[start:])
            assert INITIAL_CONTEXT in requests, f'{agent} lost the original managed context during resume'
            expected = 'Network: Unrestricted' if mode == 'unrestricted' else 'Network: Restricted'
            assert expected in requests, f'{agent} omitted {expected}: {requests}'
            assert f'Off (fixture {revision})' in requests, f'{agent} omitted the new revision: {requests}'
            assert '/opt/sandbox/context.md' in requests, f'{agent} omitted the full configuration reference'
            # A normal completion must acknowledge the hook context actually sent.
            records = set(receipts.glob('*.json')) - previous_records
            assert len(records) == 1, f'{agent} expected one receipt for the newly opened client: {records}'
            record = next(iter(records))
            assert json.loads(record.read_text()).get('acknowledged', {}).get('revision') == revision, (
                f'{agent} did not acknowledge completed turn: {record.read_text()}')
            print(f'{agent}: {revision} {mode}, native request and completion acknowledgment passed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=['codex', 'claude'], required=True)
    parser.add_argument('--image')
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.agent)
        return
    if not args.image:
        parser.error('--image is required')
    source = Path(__file__).resolve().parents[1]
    from ai_sandbox_factory import runtime_context
    with tempfile.TemporaryDirectory(prefix='factory-native-reference-') as temporary:
        root = Path(temporary)
        for revision, mode in [('one', 'allowlist'), ('two', 'unrestricted'), ('three', 'allowlist')]:
            config = {'name': 'context-fixture', 'primary_agent': args.agent, 'workspace': str(root), 'network': mode}
            current = runtime_context.snapshot(config, root)
            (root / (revision + '.md')).write_text(runtime_context.reference_text(config, root, current))
        command = ['podman', 'run', '--rm', '--pull=never', '--network=none', '--security-opt=label=disable',
                   '--userns=keep-id:uid=1000,gid=1000', '--tmpfs', '/run/primary-runtime:rw,mode=1777',
                   '-v', f'{source}:/factory:ro', '-v', f'{root}:/reference-fixtures:ro',
                   '-v', f'{source}/ai_sandbox_factory/containers/primary-worker/sandbox-update.py:/opt/sandbox/sandbox-update.py:ro',
                   '-v', f'{source}/ai_sandbox_factory/containers/primary-worker/codex-hooks.json:/etc/codex/hooks.json:ro',
                   '--entrypoint', 'python3', args.image, '/factory/tests/native_sandbox_updates.py', '--child', '--agent', args.agent]
        subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
