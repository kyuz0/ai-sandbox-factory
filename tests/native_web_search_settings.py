"""Exercise a live Claude search-setting save without account data or restarts."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from ai_sandbox_factory import primary_agents, sandbox
from ai_sandbox_factory.sandbox_options import write_json
from native_auth_fixture import AUTH_SERVICE, public_ca_source


AUTH_FIXTURE = AUTH_SERVICE + public_ca_source('claude') + """
import sys
server, thread = start_auth_fixture('claude')
print('ready',flush=True)
sys.stdin.read()
server.shutdown()
server.server_close()
thread.join()
"""


def check(image):
    name = 'native-search-save-' + uuid.uuid4().hex[:10]
    auth = None
    identifier = None
    with tempfile.TemporaryDirectory(prefix=name) as temporary:
        state = Path(temporary)
        config = {'name': name, 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                  'primary_agent': 'claude', 'workspace': str(state / 'project'),
                  'options': {'browser': False, 'claude_web_search': False}}
        (state / 'project').mkdir()
        write_json(state / 'config.json', config)
        primary_agents.prepare_workspace(config, state)
        primary_agents.write_context(config, state)
        container = sandbox.names(config)[0]
        def engine(*args, **kwargs):
            return subprocess.run(['podman', *args], text=True, capture_output=True, check=True, **kwargs).stdout.strip()
        try:
            identifier = engine('run', '-d', '--pull=never', '--network=none', '--name', container,
                '--label', sandbox.LABEL + '=' + name, '--userns=keep-id:uid=1000,gid=1000',
                '--security-opt=label=disable', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '-v', str(state / 'primary-runtime') + ':/run/primary-runtime:ro',
                '--tmpfs', '/run/primary-editor:rw,mode=1777', '--tmpfs', '/workspace:rw,mode=1777',
                '--entrypoint', 'sleep', image, 'infinity')
            auth = subprocess.Popen(['podman', 'exec', '-i', identifier, 'python3', '-c', AUTH_FIXTURE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if auth.stdout.readline().strip() != 'ready':
                _, diagnostic = auth.communicate()
                raise AssertionError(f'Synthetic authentication fixture exited {auth.returncode}: {diagnostic}')
            preferences = {'permissions': {'allow': ['WebFetch'], 'deny': ['Bash(rm -rf *)']},
                           'env': {'FACTORY_SYNTHETIC_SETTING': 'kept'}}
            engine('exec', identifier, 'mkdir', '-p', '/home/node/.claude')
            engine('exec', '-i', identifier, 'tee', '/home/node/.claude/settings.json', input=json.dumps(preferences))
            for enabled in (False, True, False):
                current = json.loads((state / 'config.json').read_text())
                sandbox.apply_settings(current, state, {'claude_web_search': enabled})
                native = json.loads(engine('exec', identifier, 'cat', '/home/node/.claude/settings.json'))
                denied = native['permissions']['deny']
                assert denied.count('WebSearch') == int(not enabled), native
                assert native['permissions']['allow'] == ['WebFetch'], native
                assert 'Bash(rm -rf *)' in denied, native
                assert native['env'] == preferences['env'], native
                assert engine('inspect', '-f', '{{.Id}}', container) == identifier
                assert engine('inspect', '-f', '{{.State.Running}}', container) == 'true'
            print('PASS: live search off/on/off saved native rules; WebFetch and user settings kept; workspace not restarted.')
        finally:
            try:
                if auth is not None:
                    _, diagnostic = auth.communicate()
                    if auth.returncode:
                        raise AssertionError(f'Synthetic authentication fixture exited {auth.returncode}: {diagnostic}')
            finally:
                if identifier is not None:
                    engine('rm', '-f', '--ignore', identifier)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Locally built Claude workspace image')
    check(parser.parse_args().image)
