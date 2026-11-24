"""Disposable real Podman creation, SSH, database and workspace replacement checks."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import creation, profile_runtime, profile_services, registry, sandbox
from ai_sandbox_factory.creation import Creation
from ai_sandbox_factory.sandbox_tui import DEFAULT_SETTINGS, Service, copy_draft, preset
from creation_provider import ADAPTER, KEY


def exercise(agent, root, log):
    service = Service(root / 'state')
    workspace = root / (agent + '-project')
    workspace.mkdir()
    (workspace / 'AGENTS.md').write_text('PROJECT_INSTRUCTIONS_STAY_UNCHANGED\n')
    name = 'profile-fixture-' + agent + '-' + uuid4().hex[:8]
    state = service.state / name
    draft = preset('developer-postgres', copy.deepcopy(DEFAULT_SETTINGS))
    draft.update(name=name, primary_agent=agent, primary_auth='login', workspace=str(workspace),
                 engine='podman', network='allowlist', open_editor=False, allowlist='example.com:443\n')
    draft['options']['browser'] = False
    if agent != 'pi':
        draft.update(model_providers={}, local_model_ports=None, subagents=False, advisors={})
    try:
        for model in (('unavailable', 'available') if agent == 'pi' else ('available',)):
            if agent == 'pi':
                draft.update(model_providers={'fixture': {'model': model}}, _provider_keys={'fixture': KEY})
            try:
                service.create(draft, None, log, cancellation=Creation())
            except ValueError as error:
                assert agent == 'pi' and model == 'unavailable', str(error)
                assert 'HTTP 503' in str(error), str(error)
                assert not state.exists(), 'Failed creation left saved state'
                empty = {**draft, 'profile': sandbox.load_profile('developer-postgres')}
                for resource in profile_services.resources(empty)['container']:
                    assert sandbox.inspect(empty, resource) is None, resource
                print('PASS failed provider creation cleans profile containers and permits same-name retry', flush=True)
                continue
            assert model == 'available', 'Unavailable provider unexpectedly succeeded'
        config = service.config(name)
        dev = sandbox.names(config)[0]
        db = profile_services.container(config, 'database')
        def command(*argv, check=True):
            return sandbox.run(['podman', 'exec', dev, *argv], capture=True, check=check, stderr=subprocess.PIPE)
        def sql(text):
            return command('ssh', '-F', '/run/profile-services/config', 'database.profile.internal',
                           "psql -U app -d app -Atc " + __import__('shlex').quote(text)).stdout.strip()
        assert command('cat', '/workspace/AGENTS.md').stdout == 'PROJECT_INSTRUCTIONS_STAY_UNCHANGED\n'
        assert command('cat', profile_runtime.PATH + '/profile/profile.json').returncode == 0
        assert command('sh', '-c', 'echo changed > ' + profile_runtime.PATH + '/profile/profile.md', check=False).returncode
        db_id = sandbox.inspect(config, db)['Id']
        context_path = '/home/node/.pi/agent/AGENTS.md' if agent == 'pi' else '/run/primary-runtime/context.md'
        if agent == 'pi':
            sandbox.run(['podman', 'exec', '--user', '0', dev, 'sh', '-c',
                         'printf IMAGE_COPY_MUST_NOT_OVERRIDE_SAVED_PROFILE > /opt/sandbox/profile.md'])
            command('python3', '/usr/local/lib/sandbox-bootstrap.py')
        context = command('cat', context_path).stdout
        instructions = (Path(config['profile']['path']) / 'profile.md').read_text().rstrip()
        assert instructions in context, 'Full saved profile.md is missing from managed instructions'
        if agent != 'pi':
            wrapper = {'codex':'/usr/local/bin/codex', 'claude':'/usr/local/bin/claude', 'agy':'/usr/local/bin/agy'}[agent]
            command(wrapper, '--factory-prepare')
            if agent == 'codex':
                assert 'codex' in command(wrapper, '--version').stdout.lower()
            if agent != 'codex':
                rules = '/home/node/.claude/CLAUDE.md' if agent == 'claude' else '/home/node/.gemini/AGENTS.md'
                assert instructions in command('cat', rules).stdout
        assert profile_runtime.PATH in context and 'database.profile.internal' in context, context
        generated = profile_services.guidance(config).rstrip()
        assert generated in context, 'Generated service guidance missing from client context'
        assert ('bash_escalate' in generated) == (agent == 'pi'), generated
        from ai_sandbox_factory.profile_replacement import runtime_names
        for resource in runtime_names(config) - {dev}:
            info = sandbox.inspect(config, resource)
            if info:
                assert profile_services.prefix(config) not in info.get('NetworkSettings', {}).get('Networks', {}), resource
        credentials = (state / 'profile-services/client/connections.json').read_bytes()
        assert sql('SELECT 42') == '42'
        sql('CREATE TABLE fixture (value text); INSERT INTO fixture VALUES (\'preserved\')')
        if agent == 'pi':
            copied = copy_draft({'name':name, 'config':config}, service.state)
            copied.update(name=name + '-copy', open_editor=False, _provider_keys={'fixture':KEY},
                          model_providers={row['name']:row.get('settings', {}) for row in config['model_providers']})
            try:
                service.create(copied, None, log, cancellation=Creation())
                copied_config = service.config(copied['name'])
                copied_dev = sandbox.names(copied_config)[0]
                check = sandbox.run(['podman','exec',copied_dev,'ssh','-F','/run/profile-services/config',
                    'database.profile.internal', "psql -U app -d app -Atc \"SELECT count(*) FROM pg_tables WHERE tablename='fixture'\""],
                    capture=True)
                assert check.stdout.strip() == '0', 'Create from existing copied database contents'
                assert sql('SELECT value FROM fixture') == 'preserved'
                assert copied_config['profile']['digest'] == config['profile']['digest']
                assert (service.state / copied['name'] / 'profile-services/client/connections.json').read_bytes() != credentials
                print('PASS Create from existing keeps the saved profile, initializes empty database data and leaves the source unchanged', flush=True)
            finally:
                if (service.state / copied['name'] / 'config.json').exists():
                    service.run(['delete', copied['name']], log)

        settings = command('ssh', '-G', '-F', '/run/profile-services/config', 'database.profile.internal').stdout
        for value in ('batchmode yes', 'stricthostkeychecking true', 'identityagent none'):
            assert value in settings, settings
        assert command('ping', '-c', '1', 'database.profile.internal').returncode == 0
        assert command('python3', '-c', 'import socket; s=socket.socket(); s.settimeout(2); s.connect(("1.1.1.1",443))', check=False).returncode
        assert command('python3', '-c', 'import socket; s=socket.socket(socket.AF_INET6); s.settimeout(2); s.connect(("2606:4700:4700::1111",443))', check=False).returncode
        assert command('curl', '-fsS', 'https://example.com').returncode == 0
        denied = command('curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', 'https://www.iana.org', check=False)
        assert denied.returncode and 'response 403' in denied.stderr, denied
        # Service applications use the same approved external destinations.
        proxy_url = 'http://proxy.profile.internal:3128'
        assert command('curl', '--proxy', proxy_url, '-fsS', 'https://example.com').returncode == 0
        restricted = root / (agent + '-revoked.txt')
        restricted.write_text('www.iana.org:443\n')
        service.run(['network', name, '--allowlist', str(restricted)], log)
        for proxy in ('http://127.0.0.1:3128', proxy_url):
            blocked = command('curl', '--proxy', proxy, '-sS', 'https://example.com', check=False)
            assert blocked.returncode and '403' in blocked.stderr, blocked
        restricted.write_text('example.com:443\n')
        service.run(['network', name, '--allowlist', str(restricted)], log)
        assert command('curl', '--proxy', proxy_url, '-fsS', 'https://example.com').returncode == 0
        assert sandbox.inspect(config, db)['Id'] == db_id
        # Actual application credentials work over the LAN, not just through SSH.
        test = 'import json,os,subprocess; c=json.load(open("/run/profile-services/connections.json"))["database"]; subprocess.run(["psql","-h",c["host"],"-U",c["user"],"-d",c["database"],"-Atc","SELECT 42"],env={**os.environ,"PGPASSWORD":c["password"]},check=True)'
        assert command('python3', '-c', test).stdout.strip() == '42'
        if agent == 'pi':
            sandbox.run(['podman', 'exec', '-i', dev, 'python3', '-'], input=(Path(__file__).parent / 'profile_clients.py').read_text())
        dev_id = sandbox.inspect(config, dev)['Id']
        service.run(['recreate', name, '--profile', 'developer-postgres'], log)
        config = service.config(name)
        assert sandbox.inspect(config, dev)['Id'] != dev_id
        assert sandbox.inspect(config, db)['Id'] == db_id, 'Workspace rebuild replaced the database'
        assert sql('SELECT value FROM fixture') == 'preserved'
        assert (state / 'profile-services/client/connections.json').read_bytes() == credentials
        service.run(['down', name], log)
        assert not sandbox.inspect(config, db)['State']['Running']
        service.run(['recreate', name, '--profile', 'developer-postgres'], log)
        from ai_sandbox_factory.profile_replacement import runtime_names
        for resource in runtime_names(config) | profile_services.resources(config)['container']:
            info = sandbox.inspect(config, resource)
            assert not info or not info['State']['Running'], 'Stopped rebuild left a component running: ' + resource
        service.run(['up', name], log)
        assert sandbox.inspect(config, db)['Id'] == db_id
        assert sql('SELECT value FROM fixture') == 'preserved'
        # A failed replacement restores the exact previous workspace and live DB.
        from ai_sandbox_factory import profile_replacement
        saved_up = sandbox.up
        failed_id = sandbox.inspect(config, dev)['Id']
        calls = 0
        def fail_once(candidate, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError('synthetic workspace startup failure')
            return saved_up(candidate, *args, **kwargs)
        with patch.object(sandbox, 'up', side_effect=fail_once):
            try:
                profile_replacement.apply(config, copy.deepcopy(config), state)
            except ValueError as error:
                assert 'synthetic workspace startup failure' in str(error)
            else:
                raise AssertionError('Replacement fault was ignored')
        assert sandbox.inspect(config, dev)['Id'] == failed_id
        assert sandbox.inspect(config, dev)['State']['Running']
        assert sandbox.inspect(config, db)['Id'] == db_id
        assert sql('SELECT value FROM fixture') == 'preserved'
        print(f'PASS {agent}: real creation, RO recipes, SSH, authenticated SQL, private LAN, proxy, workspace rebuild, rollback, stop/start and persistent data', flush=True)
    finally:
        if (state / 'config.json').exists():
            config = service.config(name)
            expected = profile_services.resources(config)
            service.run(['delete', name], log)
            for kind, names in expected.items():
                for resource in names:
                    assert subprocess.run(['podman', kind, 'exists', resource], capture_output=True).returncode, resource
        assert (workspace / 'AGENTS.md').read_text() == 'PROJECT_INSTRUCTIONS_STAY_UNCHANGED\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=('pi', 'codex', 'claude', 'agy'), action='append')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-profile-services-') as temporary:
        root = Path(temporary)
        provider = root / 'provider'
        provider.mkdir()
        (provider / 'provider.json').write_bytes((Path(__file__).resolve().parents[1] / 'examples/provider/provider.json').read_bytes())
        (provider / 'adapter.mjs').write_text(ADAPTER)
        def log(line):
            assert KEY not in line, 'Managed provider credential leaked into output'
            print(line, flush=True)
        with patch.dict(os.environ, SANDBOX_REGISTRY_DIR=str(root / 'registry')):
            registry.add('provider', 'fixture', str(provider))
            for agent in args.agent or ('pi', 'codex', 'claude', 'agy'):
                exercise(agent, root, log)


if __name__ == '__main__':
    main()
