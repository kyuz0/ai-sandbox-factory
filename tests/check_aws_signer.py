"""Run with python3 inside the signer image, using only synthetic credentials."""
import io
import json
import sys
import unittest
from configparser import RawConfigParser
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, '/app')
import signer


class SignerTests(unittest.TestCase):
    def setUp(self):
        self.services = json.loads(Path('/app/services.json').read_text())
        self.handler = object.__new__(signer.Handler)
        self.handler.command = 'POST'
        self.handler.path = '/logs/'
        self.handler.rfile = io.BytesIO(b'{}')
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.headers = HTTPMessage()
        for key, value in {
            'Host': 'aws-access.internal:19432', 'Content-Length': '2', 'X-Amz-Target': 'Logs_20140328.DescribeLogStreams',
            'Authorization': 'AWS4-HMAC-SHA256 Credential=pi-sandbox/20260101/eu-west-1/logs/aws4_request,',
        }.items():
            self.handler.headers[key] = value
        self.handler.server = SimpleNamespace(
            services=self.services, resolver=signer.Session().get_component('endpoint_resolver'),
            session={'region': 'eu-west-1', 'credentials': {
                'AccessKeyId': 'TEST_ID', 'SecretAccessKey': 'TEST_SECRET', 'SessionToken': 'TEST_TOKEN'}})

    def test_resolves_from_catalogue_and_hides_private_diagnostics(self):
        upstream = MagicMock()
        upstream.getresponse.return_value = reply = MagicMock(status=400)
        reply.getheader.side_effect = lambda k, default=None: {'Content-Type': 'application/json'}.get(k, default)
        reply.read.return_value = b'{"__type":"AccessDeniedException","message":"diagnostic TEST_TOKEN"}'
        with patch.object(signer.http.client, 'HTTPSConnection', return_value=upstream) as connect:
            self.handler.forward()
        connect.assert_called_once_with('logs.eu-west-1.amazonaws.com')
        outgoing = upstream.request.call_args.kwargs['headers']
        self.assertIn('Credential=TEST_ID/', outgoing['Authorization'])
        self.assertEqual(outgoing['X-Amz-Security-Token'], 'TEST_TOKEN')
        result = self.handler.wfile.getvalue()
        self.assertIn(b'AccessDeniedException', result)
        self.assertNotIn(b'TEST_TOKEN', result)

    def test_only_configured_region_and_ordinary_requests(self):
        with patch.object(signer.http.client, 'HTTPSConnection') as connect:
            for path in ('/sts/', '/missing-service/', '/logs/?X-Amz-Signature=example'):
                self.handler.path = path
                with self.subTest(path=path), self.assertRaises((KeyError, ValueError)):
                    self.handler.forward()
            self.handler.path = '/logs/'
            self.handler.server.session['region'] = 'us-west-2'
            with self.assertRaises(ValueError):
                self.handler.forward()
            connect.assert_not_called()

    def test_cli_config_parses_each_endpoint_once(self):
        config = RawConfigParser()
        config.read_string(signer.client_config(self.services, 'eu-west-1'))
        self.assertEqual(config['default']['region'], 'eu-west-1')
        self.assertIn('http://aws-access.internal:19432/ec2', config['services factory']['ec2'])


if __name__ == '__main__':
    unittest.main()
