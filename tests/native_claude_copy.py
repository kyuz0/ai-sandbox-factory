"""Check fresh and explicit Claude sign-in copies using synthetic accounts only."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import uuid

from ai_sandbox_factory import primary_agents, primary_workspace, sandbox
from ai_sandbox_factory.sandbox_options import write_json


def check(controller_image, workspace_image):
    name = 'native-copy-check-' + uuid.uuid4().hex[:10]
    created = []
    def engine(*args, **kwargs):
        return subprocess.run(['podman', *args], text=True, capture_output=True, check=True, **kwargs).stdout.strip()
    with tempfile.TemporaryDirectory(prefix=name) as temporary:
        state = Path(temporary)
        (state / 'project').mkdir()
        public = state / 'public'
        public.mkdir()
        inputs = state / 'primary-agent'
        config = {'name': name, 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                  'primary_agent': 'claude', 'primary_auth': 'copy', 'workspace': str(state / 'project'),
                  'options': {'browser': False}}
        primary_agents.prepare_workspace(config, state)
        primary_agents.write_context(config, state)
        write_json(inputs / 'settings.json', {'network_mode': 'allowlist'})
        controller = primary_agents.names(config)[0]
        worker = sandbox.names(config)[0]
        common = ['--pull=never', '--network=none', '--userns=keep-id:uid=1000,gid=1000',
                  '--security-opt=label=disable', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  '--label', sandbox.LABEL + '=' + name]
        try:
            created.append(engine('run', '-d', *common, '--name', controller,
                '-e', 'FACTORY_PRIMARY_AGENT=claude', '-v', str(public) + ':/run/primary-editor',
                '-v', str(inputs) + ':/run/primary-input:ro', controller_image))
            primary_agents._wait(config, controller,
                'test -S /run/primary-editor/auth.sock && test -s /run/primary-editor/claude-ca.pem'
                " && bash -c 'true </dev/tcp/127.0.0.1/3130'",
                'Synthetic Claude credential companion did not start')
            created.append(engine('run', '-d', *common, '--name', worker,
                '-v', str(public) + ':/run/primary-editor:ro',
                '-v', str(state / 'primary-runtime') + ':/run/primary-runtime:ro',
                '--tmpfs', '/workspace:rw,mode=1777', workspace_image))

            # These are the production sandbox.up operations in their existing
            # order: worker preparation happens before initial host-sign-in copy.
            primary_agents.worker_start(config, worker)
            missing = subprocess.run(['podman', 'exec', worker, 'test', '-e', '/home/node/.claude/.credentials.json'])
            assert missing.returncode == 1, 'Fresh worker unexpectedly contained Claude authentication'

            def account(number):
                return {'claudeAiOauth': {'accessToken': f'synthetic-private-access-{number}',
                    'refreshToken': f'synthetic-private-refresh-{number}',
                    'subscriptionType': 'pro' if number == 1 else 'max',
                    'expiresAt': 9999999999999, 'scopes': ['user:inference']}}
            with patch.object(primary_agents, 'up'), \
                    patch.object(primary_agents.advisor_credentials, 'read_host', return_value=json.dumps(account(1))) as read:
                primary_workspace.initial_sign_in(config, state)
                primary_workspace.initial_sign_in(config, state)
                assert read.call_count == 1, 'Initial host sign-in was copied more than once'
            assert (state / 'primary-sign-in-copied').is_file(), 'Successful initial copy was not recorded'

            def verify(number):
                private = json.loads(engine('exec', controller, 'cat', '/home/node/.claude/.credentials.json'))
                public = json.loads(engine('exec', worker, 'cat', '/home/node/.claude/.credentials.json'))
                assert private == account(number), 'Private companion did not retain the selected synthetic account'
                oauth = public['claudeAiOauth']
                assert oauth['subscriptionType'] == private['claudeAiOauth']['subscriptionType'], \
                    'Worker retained previous subscription metadata after explicit host-sign-in copy'
                for key in ('accessToken', 'refreshToken'):
                    assert oauth[key].startswith('factory-claude-'), f'Worker {key} was not a placeholder'
                    assert oauth[key] != private['claudeAiOauth'][key], f'Private {key} reached the worker'
                return oauth
            first = verify(1)
            with patch.object(primary_agents, 'up'), \
                    patch.object(primary_agents.advisor_credentials, 'read_host', return_value=json.dumps(account(2))):
                primary_agents.copy_host(config, state)
            second = verify(2)
            assert first['accessToken'] != second['accessToken'], 'Explicit account replacement kept the old placeholders'
            assert engine('inspect', '-f', '{{.Id}}', worker) == created[1], 'Host-sign-in copy recreated the worker'
            assert engine('inspect', '-f', '{{.State.Running}}', worker) == 'true'
            print('PASS: production prepare→initial-copy order and explicit account replacement seeded only placeholders; worker stayed running.')
        finally:
            for identifier in reversed(created):
                engine('rm', '-f', '--ignore', identifier)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-image', required=True)
    parser.add_argument('--workspace-image', required=True)
    args = parser.parse_args()
    check(args.controller_image, args.workspace_image)
