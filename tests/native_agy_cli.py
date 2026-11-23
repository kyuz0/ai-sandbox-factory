"""Opt-in native Antigravity subscription and credential-isolation check.

Creates disposable containers and reads the host sign-in only through the
existing import helper. It never changes a saved sandbox or prints tokens.
"""
from contextlib import ExitStack
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from ai_sandbox_factory import advisor_credentials, primary_agents, sandbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controller-image', dest='controller', required=True)
    parser.add_argument('--workspace-image', dest='workspace', required=True)
    parser.add_argument('--client', type=Path, help='Override only the disposable workspace client while developing its configuration.')
    parser.add_argument('--search', action='store_true', help='Exercise native Web Search off and on.')
    parser.add_argument('--browser', action='store_true', help='Use the managed native browser MCP tool against a local fixture.')
    parser.add_argument('--git-subfolder', action='store_true', help='Start the same native checks inside a Unicode subfolder of a Git project.')
    parser.add_argument('--migrate-history', action='store_true', help='Export and restore native history before resuming it.')
    args = parser.parse_args()
    prefix = 'factory-agy-cli-' + secrets.token_hex(4)
    def run(*command, payload=None, check=True):
        return subprocess.run(['podman', *command], input=payload, text=True, capture_output=True, check=check)
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=prefix)))
        for name in ('public', 'runtime', 'workspace', 'tools'):
            (root / name).mkdir()
        cwd = '/workspace'
        if args.git_subfolder:
            subprocess.run(['git', 'init', '-q', str(root / 'workspace')], check=True)
            (root / 'workspace' / 'Project Δ').mkdir()
            cwd += '/Project Δ'
        (root / 'runtime/settings.json').write_text(json.dumps({'agent': 'agy', 'network_mode': 'allowlist',
            'working_directory': cwd, 'write_roots': ['/workspace'], 'native_approval': 'automatic', 'browser': args.browser}))
        (root / 'runtime/context.md').write_text('Work in /workspace with your native tools.')
        volumes = [prefix + '-provider-egress', prefix + '-workspace-egress']
        for volume in volumes:
            run('volume', 'create', volume)
            stack.callback(run, 'volume', 'rm', volume)
        def start(name, *command):
            run('run', '-d', '--name', name, *command)
            stack.callback(run, 'rm', '-f', name)
        for index, rules in enumerate((primary_agents.network_rules('agy'), [])):
            config = root / f'squid-{index}.conf'
            config.write_text(sandbox.squid_config(rules))
            start(prefix + '-gateway-' + str(index), '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID',
                  '--cap-add=SETGID', '--cap-add=DAC_OVERRIDE', '--security-opt=no-new-privileges',
                  '-v', f'{config}:/etc/squid/sandbox.conf:ro,z', '-v', f'{volumes[index]}:/run/egress:z',
                  'localhost/pi-sandbox-factory-proxy:1')
        isolated = ['--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                    '--security-opt=label=disable', '--user=1000:1000', '--userns=keep-id:uid=1000,gid=1000',
                    '--tmpfs', '/home/node:rw,mode=1777']
        controller = prefix + '-credential'
        worker = prefix + '-workspace'
        start(controller, *isolated, '-e', 'FACTORY_PRIMARY_AGENT=agy',
              '-v', f'{volumes[0]}:/run/primary-egress:ro', '-v', f'{volumes[1]}:/run/workspace-egress:ro',
              '-v', f'{root / "public"}:/run/primary-editor', '-v', f'{root / "runtime"}:/run/primary-input:ro',
              args.controller)
        def wait(command):
            while True:
                result = run(*command, check=False)
                if result.returncode == 0:
                    return
                state = json.loads(run('inspect', controller).stdout)[0]['State']
                if not state.get('Running'):
                    raise RuntimeError('Credential companion stopped: ' + run('logs', controller).stdout)
                time.sleep(.1)
        wait(['exec', controller, 'sh', '-c', 'test -s /run/primary-editor/agy-ca.pem && test -S /run/primary-editor/auth.sock'])
        signed = advisor_credentials.read_host('agy')
        private_tokens = list(json.loads(signed)['token'].values())
        private_tokens = [value for value in private_tokens if isinstance(value, str) and len(value) > 30]
        run('exec', '-i', controller, 'secret-tool', 'store', '--label=Disposable AGy check',
            'service', 'gemini', 'username', 'antigravity', payload=signed)
        start(worker, *isolated, '-e', 'SANDBOX_NETWORK_MODE=allowlist',
              *[part for key, value in sandbox.RUNTIME_ENV.items() for part in ('-e', f'{key}={value}')],
              '-v', f'{volumes[1]}:/run/egress:ro', '-v', f'{root / "public"}:/run/primary-editor:ro',
              '-v', f'{root / "runtime"}:/run/primary-runtime:ro', '-v', f'{root / "workspace"}:/workspace',
              '-v', f'{root / "tools"}:/run/primary-workspace',
              *(['-v', f'{args.client.resolve()}:/opt/primary/agy_client.py:ro'] if args.client else []),
              '--entrypoint=python3', args.workspace, '/opt/primary/entrypoint.py')
        wait(['exec', worker, 'sh', '-c', 'test -f /tmp/factory-workspace-ready && bash -c "true </dev/tcp/127.0.0.1/3130"'])
        run('exec', worker, 'python3', '/opt/primary/agy_client.py', '--factory-prepare')
        if args.browser:
            run('exec', '-d', worker, 'bash', '/opt/primary/worker.sh', 'agy')
            wait(['exec', worker, 'test', '-S', '/run/primary-workspace/tools.sock'])
            from ai_sandbox_factory.browser_viewer import ensure_viewer
            identifier = run('inspect', '--format', '{{.Id}}', worker).stdout.strip()
            ensure_viewer('podman', identifier)
        sentinel = secrets.token_hex(10)
        (root / 'workspace/input.txt').write_text(sentinel + '\n')
        prompt = ('Use your native file and shell tools. Read /workspace/input.txt. Run a shell command that '
                  'writes its uppercase contents to /workspace/shell-output.txt. Then use your native file editing '
                  'tool to create /workspace/final.txt containing the original contents followed by exactly one '
                  'line saying native-edit-ok. Reply DONE after verifying both files.')
        result = run('exec', '-w', cwd, worker, 'agy', '--print', prompt,
                     '--output-format=json', check=False)
        combined = result.stdout + result.stderr
        if result.returncode or not (root / 'workspace/shell-output.txt').exists() or not (root / 'workspace/final.txt').exists():
            # The proxy has already redacted native diagnostics. Also defend
            # the test reporter if an upstream ever echoes a credential.
            for token in private_tokens:
                combined = combined.replace(token, '[redacted]')
            raise RuntimeError(f'Native Antigravity exited {result.returncode}: {combined}')
        assert (root / 'workspace/shell-output.txt').read_text().strip() == sentinel.upper()
        assert (root / 'workspace/final.txt').read_text().splitlines() == [sentinel, 'native-edit-ok']
        resumed = run('exec', '-w', cwd, worker, 'agy', '--continue', '--print',
                      'Without reading files or using tools, reply with the exact original input value from this conversation.',
                      '--output-format=json', check=False)
        assert resumed.returncode == 0 and sentinel in resumed.stdout, 'Native saved conversation was not recalled: ' + resumed.stdout
        headers = run('exec', worker, 'python3', '-c',
            'from pathlib import Path;import json;'
            'p=Path.home()/".gemini/antigravity-cli";'
            'print(json.dumps({str(x.relative_to(p)):x.read_bytes()[:16].hex() for x in p.rglob("*.db")}))').stdout
        if args.migrate_history:
            helper = (Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/codex_history.py').read_text()
            exported = subprocess.run(['podman', 'exec', '-e', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/factory-agy-session/bus',
                worker, 'python3', '-c', helper, 'export', 'agy'], capture_output=True, check=True).stdout
            run('exec', worker, 'mv', '/home/node/.gemini', '/home/node/.gemini-before-history-restore')
            subprocess.run(['podman', 'exec', '-i', worker, 'python3', '-c', helper, 'restore', 'agy'],
                           input=exported, capture_output=True, check=True)
            migrated = run('exec', '-w', cwd, worker, 'agy', '--continue', '--print',
                'Without reading files or using tools, repeat the exact original input value from our earlier conversation.',
                '--output-format=json', check=False)
            assert migrated.returncode == 0 and sentinel in migrated.stdout, 'Restored native conversation was not recalled: ' + migrated.stdout
        if args.search:
            env = ['-e', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/factory-agy-session/bus',
                   '-e', 'HTTP_PROXY=http://127.0.0.1:3130', '-e', 'HTTPS_PROXY=http://127.0.0.1:3130',
                   '-e', 'http_proxy=http://127.0.0.1:3130', '-e', 'https_proxy=http://127.0.0.1:3130',
                   '-e', 'SSL_CERT_FILE=/home/node/.gemini/factory-ca.pem', '-e', 'AGY_CA_CERT=/home/node/.gemini/factory-ca.pem']
            instrument = ('import json;from pathlib import Path;'
                'p=Path.home()/".gemini/config/hooks.json";v=json.loads(p.read_text());'
                'v["factory-native-web-search"]["PreToolUse"][0]["hooks"][0]["command"]='
                '"tee /tmp/factory-search-input.json | /usr/bin/python3 -I /opt/primary/agy_client.py --factory-search-policy | tee /tmp/factory-search-output.json";'
                'v["factory-search-observer"]={"PostToolUse":[{"matcher":"^search_web$","hooks":[{"command":"tee /tmp/factory-search-completion.json >/dev/null; printf \'{}\'"}]}]};'
                'p.write_text(json.dumps(v))')
            run('exec', worker, 'python3', '-c', instrument)
            for enabled in (False, True):
                settings = json.loads((root / 'runtime/settings.json').read_text())
                settings['agy_web_search'] = enabled
                (root / 'runtime/settings.json').write_text(json.dumps(settings))
                run('exec', worker, 'rm', '-f', '/tmp/factory-search-input.json', '/tmp/factory-search-output.json', '/tmp/factory-search-completion.json')
                preferences = run('exec', worker, 'python3', '-c',
                    'import sys,json;from pathlib import Path;sys.path.insert(0,"/opt/primary");'
                    'from agy_client import native_preferences;'
                    'print(json.dumps(native_preferences(json.loads(Path("/run/primary-runtime/settings.json").read_text()))))').stdout.strip()
                search = run('exec', *env, '-w', cwd, worker, '/usr/local/bin/factory-agent', '--settings', preferences, '--print',
                    'Call the native search_web tool exactly once to search for the Python programming language official release notes. '
                    'Use no other tools, do not edit any settings or files. If search is blocked, report the reason. '
                    'If it succeeds, cite one result URL.', '--output-format=json', check=False)
                observation = run('exec', worker, 'cat', '/tmp/factory-search-output.json', check=False)
                if observation.returncode:
                    raise AssertionError(f'Native search_web hook did not execute with agy_web_search={enabled}: '
                                         + search.stdout + search.stderr)
                observed = json.loads(observation.stdout)
                assert observed['decision'] == ('allow' if enabled else 'deny'), observed
                if enabled:
                    completed = json.loads(run('exec', worker, 'cat', '/tmp/factory-search-completion.json').stdout)
                    assert not completed.get('error'), 'Native search_web failed: ' + str(completed['error'])
                    assert 'https://' in search.stdout, 'Enabled native Web Search returned no result URL: ' + search.stdout
                else:
                    assert 'off' in search.stdout.lower() or 'blocked' in search.stdout.lower(), search.stdout
        if args.browser:
            browser_marker = 'native-browser-' + secrets.token_hex(8)
            server = ('import http.server;from pathlib import Path;'
                'H=type("Fixture",(http.server.BaseHTTPRequestHandler,),{'
                '"do_GET":lambda self:(Path("/tmp/factory-browser-user-agent").write_text(self.headers.get("User-Agent","")),'
                'self.send_response(200),self.end_headers(),self.wfile.write(' +
                repr(('<title>' + browser_marker + '</title><h1>Native browser fixture</h1>').encode()) + ')),'
                '"log_message":lambda *args:None});'
                's=http.server.HTTPServer(("127.0.0.1",0),H);'
                'Path("/tmp/factory-browser-port").write_text(str(s.server_port));s.serve_forever()')
            run('exec', '-d', worker, 'python3', '-c', server)
            wait(['exec', worker, 'test', '-s', '/tmp/factory-browser-port'])
            port = int(run('exec', worker, 'cat', '/tmp/factory-browser-port').stdout)
            browser = run('exec', '-w', cwd, worker, 'agy', '--print',
                f'Use only the factory_workspace browser MCP tool to navigate Chromium to http://127.0.0.1:{port}/ '
                'and read the page title with Playwright. Reply with the exact title. Do not use shell or file tools.',
                '--output-format=json', check=False)
            assert browser.returncode == 0 and browser_marker in browser.stdout, 'Native managed browser call failed: ' + browser.stdout
            user_agent = run('exec', worker, 'cat', '/tmp/factory-browser-user-agent').stdout
            assert 'Chrome/' in user_agent, 'Local fixture was not opened by Chromium: ' + user_agent
        profile = run('exec', '-e', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/factory-agy-session/bus', worker,
                      'secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity').stdout
        environment = run('exec', worker, 'sh', '-c', 'cat /proc/1/environ; grep -E "CapEff|CapPrm|NoNewPrivs" /proc/self/status').stdout
        for token in private_tokens:
            assert token not in profile and token not in environment and token not in combined, 'Workspace received a subscription token'
        assert 'factory-agy-' in profile
        assert '0000000000000000' in environment and 'NoNewPrivs:\t1' in environment
        mounts = json.loads(run('inspect', worker).stdout)[0]['Mounts']
        assert not any(row['Destination'] in ('/run/primary-bus', '/run/primary-egress', '/run/workspace-egress') for row in mounts)
        blocked = run('exec', worker, 'curl', '--proxy', 'http://127.0.0.1:3130', '-sS', '-o', '/dev/null',
                      '-w', '%{http_code}', 'http://example.net/', check=False)
        assert blocked.stdout == '403', blocked.stdout
        history = run('exec', worker, 'find', '/home/node/.gemini/antigravity-cli', '-maxdepth', '3', '-type', 'f').stdout.splitlines()
        history_groups = sorted({str(Path(value).relative_to('/home/node/.gemini/antigravity-cli')).split('/')[0] for value in history})
        suffixes = sorted(set(Path(value).suffix for value in history))
        print(json.dumps({'native_file_read_shell_and_edit': 'passed', 'masked_keyring': 'passed',
                          'private_companion_mounts_absent': 'passed', 'unselected_destination': 'HTTP 403',
                          'native_saved_conversation_resume': 'passed', 'native_history_groups': history_groups,
                          'native_history_file_suffixes': suffixes, 'native_database_headers': json.loads(headers),
                          'restored_native_history': 'passed' if args.migrate_history else 'not requested',
                          'native_web_search_off_and_on': 'passed' if args.search else 'not requested',
                          'native_managed_browser': 'passed' if args.browser else 'not requested',
                          'git_project_unicode_starting_directory': cwd if args.git_subfolder else 'not requested'}))


if __name__ == '__main__':
    main()
