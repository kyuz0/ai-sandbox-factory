import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_sandbox_factory import aws_access, browser_editor, sandbox


class AWSAccessTests(unittest.TestCase):
    def setUp(self):
        library = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, SANDBOX_REGISTRY_DIR=library))
        self.grants = [{'Action': 'ec2:DescribeRegions', 'Resource': '*'},
                       {'Action': 'logs:GetLogEvents', 'Resource': 'arn:aws:logs:*:123456789012:log-group:demo:*'}]
        self.value = {'profile': 'research', 'role': 'arn:aws:iam::123456789012:role/Research',
                      'region': 'eu-west-1', 'grants': self.grants}

    def test_policy_denies_outside_actions_and_resources(self):
        policy = aws_access.session_policy(self.grants + [
            {'Action': 'logs:GetLogEvents', 'Resource': 'arn:aws:logs:*:123456789012:log-group:another:*'}])
        statements = policy['Statement']
        self.assertIn({'Effect': 'Deny', 'Resource': '*', 'NotAction': ['ec2:describeregions', 'logs:getlogevents']},
                      statements)
        scoped = next(row for row in statements if 'NotResource' in row)
        self.assertEqual(scoped['Action'], ['logs:getlogevents'])
        self.assertEqual(set(scoped['NotResource']), {self.grants[1]['Resource'],
                        'arn:aws:logs:*:123456789012:log-group:another:*'})

    def test_invalid_grants_fail_before_assuming_a_role(self):
        for action in ['*', 'ec2:*', 'sts:AssumeRole']:
            with self.subTest(action=action), self.assertRaises(ValueError):
                aws_access.settings({**self.value, 'grants': [{'Action': action, 'Resource': '*'}]})
        with self.assertRaisesRegex(ValueError, '2,048'):
            aws_access.settings({**self.value, 'grants': [
                {'Action': 'logs:GetLogEvents', 'Resource': ['arn:aws:logs:*:123456789012:log-group:demo' + str(i)
                                                            for i in range(100)]}]})
        self.assertEqual(aws_access.settings({}), {})

    def test_assume_uses_named_profile_and_compiled_policy(self):
        credentials = dict(AccessKeyId='fixture', SecretAccessKey='example', SessionToken='sample', Expiration='date')
        result = SimpleNamespace(returncode=0, stdout=json.dumps(credentials))
        with patch.dict(os.environ, AWS_ACCESS_KEY_ID='other-account', AWS_SESSION_TOKEN='unrelated'), \
                patch('ai_sandbox_factory.aws_access.subprocess.run', return_value=result) as run:
            self.assertEqual(aws_access.assume('aws-sandbox-preview', self.value), credentials)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--role-session-name') + 1], 'aws-sandbox-preview')
        self.assertEqual(command[command.index('--profile') + 1], 'research')
        self.assertEqual(json.loads(command[command.index('--policy') + 1]), aws_access.session_policy(self.grants))
        self.assertNotIn('AWS_ACCESS_KEY_ID', run.call_args.kwargs['env'])
        self.assertNotIn('AWS_SESSION_TOKEN', run.call_args.kwargs['env'])

    def test_renewal_reuses_grant_and_stops_when_signer_exits(self):
        refreshed = {'Expiration': '2100-01-01T00:00:00+00:00'}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'grant.json'
            path.write_text(json.dumps(self.value))
            with patch.object(aws_access, 'assume', return_value=refreshed) as assume, \
                    patch.object(aws_access, '_deliver') as deliver, \
                    patch('ai_sandbox_factory.aws_access.subprocess.run',
                          side_effect=[subprocess.TimeoutExpired('wait', 0), SimpleNamespace(returncode=0)]) as wait:
                aws_access.renew('podman', 'container-id', path, 'demo', '2000-01-01T00:00:00+00:00')
        assume.assert_called_once_with('demo', self.value)
        deliver.assert_called_once_with('podman', 'container-id', self.value['region'], refreshed)
        self.assertEqual(wait.call_count, 2)


    def test_connecting_older_snapshot_explains_runtime_update_before_build(self):
        with TemporaryDirectory() as tmp, patch.object(sandbox, 'run') as run:
            root = Path(tmp)
            path = root / 'snapshot'
            path.mkdir()
            (path / 'entrypoint.sh').write_text('#!/bin/sh\n')
            snapshot = {'name': 'base', 'path': str(path), 'digest': sandbox.tree_digest(path)}
            config = dict(name='demo', engine='podman', profile=snapshot, common=snapshot,
                          aws_access=self.value, uid=1000, gid=1000)
            with patch.object(sandbox.host_platform, 'check_engine'), \
                 patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=root):
                with self.assertRaisesRegex(ValueError, 'Update sources and recreate'):
                    sandbox.build(config)
            run.assert_not_called()

    def test_shell_route_is_scoped_to_the_enabled_signer(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp)
            for mode, expected in [('unrestricted', {'aws-access.internal:19432', '127.0.0.1:19432'}),
                                   ('allowlist', {'aws-access.internal:19432'})]:
                self.assertEqual(set(sandbox.shell_network_policy({'network': mode, 'aws_access': self.value}, state)
                                     ['allowedDomains']), expected)
                self.assertEqual(sandbox.shell_network_policy({'network': mode}, state)['allowedDomains'], [])

    def test_reconnect_reuses_aws_client_and_changes_only_signer(self):
        config = dict(name='test', engine='podman', network='unrestricted')
        mounted = dict(Id='workspace', State={'Running': True}, Mounts=[
            {'Type': 'bind', 'Destination': '/run/aws-access', 'RW': False}])
        with TemporaryDirectory() as tmp, patch.object(sandbox, 'inspect', return_value=mounted), \
                patch.object(sandbox, 'build') as build, patch.object(aws_access, 'build') as prepare, patch.object(sandbox, 'run') as run, \
                patch.object(aws_access, 'up') as signer, patch.object(aws_access, 'down') as disconnect:
            state = Path(tmp)
            sandbox.apply_aws_settings(config, state, self.value, approval='ready')
            enabled = json.loads((state / 'config.json').read_text())
            sandbox.apply_aws_settings(enabled, state, {}, approval='ready')
            signer.assert_called_once_with(enabled, state)
            disconnect.assert_called_once_with(enabled)
            prepare.assert_called_once()
            build.assert_not_called()
            run.assert_not_called()

    def test_reconnect_sign_in_failure_preserves_the_running_gateway(self):
        config = dict(name='test', engine='podman', network='allowlist')
        mounted = dict(Id='prepared', State={'Running': True}, Mounts=[
            {'Type': 'bind', 'Destination': '/run/aws-access', 'RW': False}])
        with TemporaryDirectory() as tmp, patch.object(sandbox, 'inspect', return_value=mounted), \
                patch.object(sandbox, 'restart_existing') as restart, patch.object(aws_access, 'build'), \
                patch.object(aws_access, 'up', side_effect=ValueError('Sign in again')):
            state = Path(tmp)
            (state / 'allowlist.txt').write_text('')
            original_policy = sandbox.network_policy(config, [])
            (state / 'squid.conf').write_text(original_policy)
            with self.assertRaisesRegex(ValueError, 'Sign in again'):
                sandbox.apply_aws_settings(config, state, self.value, approval='gateway')
            restart.assert_not_called()
            self.assertEqual((state / 'squid.conf').read_text(), original_policy)

    def test_first_install_preserves_stopped_state_and_running_state(self):
        config = dict(name='test', engine='podman', network='unrestricted')
        dev = sandbox.names(config)[0]
        for running, effect in [(False, 'replace'), (True, 'restart')]:
            with self.subTest(running=running), TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / 'allowlist.txt').write_text('')
                (state / 'squid.conf').write_text('')
                old_runtime = dict(Id='old-workspace', State={'Running': running})
                operations = Mock()
                with patch.object(sandbox, 'inspect', return_value=old_runtime), \
                        patch.object(sandbox, 'build', operations.build), patch.object(sandbox, 'up', operations.start), \
                        patch.object(sandbox, 'run', operations.engine), patch.object(aws_access, 'up', operations.signer):
                    sandbox.apply_aws_settings(config, state, self.value, approval=effect)
                updated = json.loads((state / 'config.json').read_text())
                self.assertEqual(updated['aws_access'], self.value)
                operations.build.assert_called_once_with(updated, state)
                commands = [args[0] for args, _ in operations.engine.call_args_list]
                self.assertIn(['podman', 'rm', 'old-workspace'], commands)
                self.assertFalse(any(cmd[1] == 'volume' for cmd in commands))
                if running:
                    operations.signer.assert_called_once_with(updated, state)
                    operations.start.assert_called_once_with(updated, state, prepare_sign_in=False)
                    self.assertEqual([cmd[1] for cmd in commands], ['stop', 'rename', 'exec', 'rm'])
                    self.assertEqual(commands[2][2], dev)
                    self.assertEqual([call[0] for call in operations.mock_calls],
                                     ['build', 'signer', 'engine', 'engine', 'start', 'engine', 'engine'])
                else:
                    operations.signer.assert_not_called()
                    operations.start.assert_not_called()
                    self.assertEqual([cmd[1] for cmd in commands], ['rename', 'rm'])

    def test_failed_replacement_restores_original_container(self):
        config = dict(name='test', engine='podman', network='unrestricted')
        dev = sandbox.names(config)[0]
        updated = {**config, 'aws_access': self.value}
        original = dict(Id='original', State={'Running': True})
        replacement = dict(Id='replacement', State={'Running': False})
        with TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'allowlist.txt').write_text('')
            (state / 'squid.conf').write_text('previous policy')
            (state / 'config.json').write_text(json.dumps(config))
            with patch.object(sandbox, 'up', side_effect=[ValueError('Startup failed'), None]) as up, \
                    patch.object(sandbox, 'inspect', side_effect=[replacement, None]), \
                    patch.object(sandbox, 'run') as run, patch.object(aws_access, 'up'), \
                    patch.object(aws_access, 'down') as down:
                with self.assertRaisesRegex(ValueError, 'Startup failed'):
                    sandbox.replace_for_aws(config, updated, state, 'restart', {dev: original})
            commands = [args[0] for args, _ in run.call_args_list]
            self.assertEqual(commands[-2:], [['podman', 'rm', '-f', 'replacement'],
                                             ['podman', 'rename', 'original', dev]])
            up.assert_called_with(config, state, prepare_sign_in=False)
            down.assert_called_once_with(updated)
            self.assertEqual((state / 'squid.conf').read_text(), 'previous policy')
            self.assertEqual(json.loads((state / 'config.json').read_text()), config)

    def test_first_aws_connection_restores_editor_before_retiring_old_container(self):
        for failure in (None, 'workspace', 'editor'):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp:
                state = Path(tmp)
                (state / 'squid.conf').write_text('previous policy')
                (state / 'allowlist.txt').write_text('')
                config = dict(name='test', engine='podman', network='unrestricted')
                updated = {**config, 'aws_access': self.value}
                dev = sandbox.names(config)[0]
                previous = {'Id':'previous', 'State':{'Running':True}}
                candidate = {'Id':'candidate', 'State':{'Running':True}}
                connection = {'url':'http://127.0.0.1:12345','preferences':{'mode':'browser'}}
                calls = []
                def resume(current, target, snapshot):
                    self.assertEqual(target,state)
                    self.assertEqual(snapshot,connection)
                    self.assertNotIn(['podman','rm','previous'],calls)
                    self.assertEqual(json.loads((state/'config.json').read_text()),current)
                    if failure == 'editor' and current == updated:
                        raise ValueError('editor startup diagnostic')
                with patch.object(browser_editor,'replacement_connection',return_value=connection), \
                     patch.object(browser_editor,'stop') as stop, \
                     patch.object(browser_editor,'active',return_value=False), \
                     patch.object(browser_editor,'resume_connection',side_effect=resume) as restore, \
                     patch.object(sandbox,'up',side_effect=[ValueError('workspace diagnostic'),None] if failure=='workspace' else None), \
                     patch.object(sandbox,'inspect',side_effect=lambda config,name:candidate if name==dev else None), \
                     patch.object(sandbox,'run',side_effect=lambda args,**kwargs:calls.append(args)), \
                     patch.object(aws_access,'up'), patch.object(aws_access,'down'), \
                     patch.object(sandbox.connector_settings,'refresh_agent_context'), \
                     patch.object(sandbox,'refresh_shell_network'):
                    if failure:
                        with self.assertRaisesRegex(ValueError,'workspace diagnostic' if failure=='workspace' else 'editor startup diagnostic'):
                            sandbox.replace_for_aws(config,updated,state,'restart',{dev:previous})
                        self.assertEqual(json.loads((state/'config.json').read_text()),config)
                        restore.assert_called_with(config,state,connection)
                        self.assertNotIn(['podman','rm','previous'],calls)
                    else:
                        sandbox.replace_for_aws(config,updated,state,'restart',{dev:previous})
                        restore.assert_called_once_with(updated,state,connection)
                        self.assertIn(['podman','rm','previous'],calls)
                    stop.assert_called_once_with(state)
