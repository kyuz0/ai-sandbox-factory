import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pi_sandbox_factory import aws_access, sandbox


class AWSAccessTests(unittest.TestCase):
    def setUp(self):
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
                patch('pi_sandbox_factory.aws_access.subprocess.run', return_value=result) as run:
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
                    patch('pi_sandbox_factory.aws_access.subprocess.run',
                          side_effect=[subprocess.TimeoutExpired('wait', 0), SimpleNamespace(returncode=0)]) as wait:
                aws_access.renew('podman', 'container-id', path, 'demo', '2000-01-01T00:00:00+00:00')
        assume.assert_called_once_with('demo', self.value)
        deliver.assert_called_once_with('podman', 'container-id', self.value['region'], refreshed)
        self.assertEqual(wait.call_count, 2)


    def test_connecting_older_snapshot_explains_runtime_update_before_build(self):
        with TemporaryDirectory() as tmp, patch.object(sandbox, 'run') as run:
            path = Path(tmp)
            (path / 'entrypoint.sh').write_text('#!/bin/sh\n')
            snapshot = {'name': 'base', 'path': tmp, 'digest': sandbox.tree_digest(path)}
            config = dict(name='demo', engine='podman', profile=snapshot, common=snapshot,
                          aws_access=self.value, uid=1000, gid=1000)
            with patch.object(sandbox.host_platform, 'check_engine'):
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
