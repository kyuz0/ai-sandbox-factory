"""Native vendor transport remains distinct from credential substitution."""
import json
from pathlib import Path
import sys
import unittest

from ai_sandbox_factory import primary_agents, sandbox
from native_editor_environment import editor_environment

SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers'
sys.path.insert(0, str(SOURCE / 'primary'))
from codex_auth_proxy import NETWORK, SubscriptionProxy, native_transport
from mitmproxy import http
from mitmproxy.test import tflow


class CodexNetworkTests(unittest.TestCase):
    def test_shared_catalog_matches_gateway_acl(self):
        catalog = json.loads((SOURCE / 'primary/codex_network.json').read_text())
        self.assertEqual(catalog, NETWORK)
        rules = primary_agents.network_rules('codex')
        expected = [(host, 443) for host in catalog['transport_hosts']]
        expected += [('.' + domain, 443) for domain in catalog['transport_domains']]
        self.assertEqual(rules, expected)
        configuration = sandbox.squid_config(rules)
        self.assertIn('acl sandbox_hosts dstdomain -n ' + ' '.join(sorted(host for host, _ in rules)), configuration)
        self.assertIn('http_access deny all', configuration)
        for index, (host, _) in enumerate(rules):
            self.assertIn(f'acl host_{index} dstdomain -n {host}', configuration)
            self.assertIn(f'acl port_{index} port 443', configuration)

    def test_entire_catalog_routes_through_native_gateway(self):
        hosts = set(NETWORK['transport_hosts']) | set(NETWORK['transport_domains'])
        hosts |= {'service.' + domain for domain in NETWORK['transport_domains']}
        proxy = SubscriptionProxy(None)
        for host in hosts | set(NETWORK['credential_hosts']):
            with self.subTest(host=host):
                self.assertTrue(native_transport(host))
                flow = tflow.tflow()
                flow.request = http.Request.make('GET', 'https://' + host + '/')
                proxy.requestheaders(flow)
                self.assertEqual(flow.server_conn.via, ('http', ('127.0.0.1', 3128)))

    def test_suffix_lookalikes_use_workspace_rules(self):
        proxy = SubscriptionProxy(None)
        for domain in NETWORK['transport_domains']:
            for host in ('not' + domain, domain + '.attacker.invalid'):
                with self.subTest(host=host):
                    self.assertFalse(native_transport(host))
                    flow = tflow.tflow()
                    flow.request = http.Request.make('GET', 'https://' + host + '/')
                    proxy.requestheaders(flow)
                    self.assertEqual(flow.server_conn.via, ('http', ('127.0.0.1', 3129)))

    def test_other_native_gateways_keep_exact_official_destinations(self):
        self.assertEqual(primary_agents.network_rules('claude'),
            sandbox.parse_rules('\n'.join(primary_agents.PRIMARY_HOSTS['claude'])))
        agy = json.loads((SOURCE / 'primary/agy_network.json').read_text())
        expected = [(host, 443) for host in agy['transport_hosts']]
        self.assertEqual(primary_agents.network_rules('agy'), expected)
        self.assertFalse(agy['transport_domains'], 'Antigravity package transport uses exact hosts, not Google-wide suffixes')
        self.assertTrue(set(sandbox.parse_rules('\n'.join(primary_agents.PRIMARY_HOSTS['agy']))).issubset(expected))
        for domain in NETWORK['transport_domains']:
            with self.assertRaises(ValueError):
                sandbox.parse_rules('.' + domain + ':443')

    def test_editor_transport_environment_covers_each_native_client(self):
        inherited = {'HTTP_PROXY': 'http://127.0.0.1:3128', 'HTTPS_PROXY': 'http://127.0.0.1:3128',
                     'NODE_EXTRA_CA_CERTS': '/custom/public-ca.pem', 'NODE_OPTIONS': '--require=/existing/preload.cjs',
                     'PASSWORD': 'synthetic-private-value'}
        for agent in ('codex', 'pi', 'claude', 'agy'):
            with self.subTest(agent=agent):
                environment = editor_environment(agent, inherited)
                self.assertNotIn('PASSWORD', environment)
                self.assertEqual(environment['NODE_OPTIONS'], inherited['NODE_OPTIONS'])
                if agent != 'pi':
                    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                        self.assertEqual(environment[key], 'http://127.0.0.1:3130')
                    home = {'codex': '.codex', 'claude': '.claude', 'agy': '.gemini'}[agent]
                    for key in ('NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
                        self.assertEqual(environment[key], '/home/node/' + home + '/factory-ca.pem')
                    if agent == 'claude':
                        self.assertEqual(environment['CLAUDE_CONFIG_DIR'], '/home/node/.claude')
                    if agent == 'agy':
                        self.assertEqual(environment['AGY_CA_CERT'], '/home/node/.gemini/factory-ca.pem')
                        self.assertEqual(environment['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/tmp/factory-agy-session/bus')
                else:
                    self.assertEqual(environment, {key: value for key, value in inherited.items() if key != 'PASSWORD'})


if __name__ == '__main__':
    unittest.main()
