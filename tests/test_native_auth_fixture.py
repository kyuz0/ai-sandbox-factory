"""The account-free native test companion must model its exact public route."""
import json
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from native_auth_fixture import AUTH_SERVICE


class NativeAuthFixtureTests(unittest.TestCase):
    def test_only_matching_agent_auth_route_succeeds_without_credentials(self):
        namespace = {}
        exec(AUTH_SERVICE, namespace)
        for agent in ('codex', 'claude', 'agy'):
            with self.subTest(agent=agent):
                server, thread = namespace['start_auth_fixture'](agent, port=0)
                try:
                    opener = build_opener(ProxyHandler({'http': 'http://127.0.0.1:' + str(server.server_port)}))
                    with opener.open('http://factory-' + agent + '.internal/auth') as response:
                        self.assertEqual(json.load(response), {'generation': 'synthetic', 'auth': None})
                        self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    for route in ('http://other.invalid/auth', 'http://factory-' + agent + '.internal/other',
                                  'http://factory-' + agent + '.internal/auth?unexpected=1'):
                        with self.assertRaises(HTTPError) as failure:
                            opener.open(route)
                        self.assertEqual(failure.exception.code, 404)
                        self.assertIn('No synthetic ' + agent + ' credential-service route', failure.exception.read().decode())
                        failure.exception.close()
                    self.assertEqual(server.auth_requests, 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
