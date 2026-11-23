import json
from pathlib import Path
import tempfile
import unittest
from ai_sandbox_factory import sandbox, folders
from ai_sandbox_factory.sandbox_options import guidance

class LocalDevelopmentTests(unittest.TestCase):
    def test_trusted_network_and_scratch_are_consistent(self):
        source = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/dev'
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state/'allowlist.txt').write_text('git.example.com:22\napi.example.com:8443\n')
            config = {'common': {'path': str(source)}, 'options': {},
                      'website_groups': {'fixture': {'domains': ['.npmjs.org', 'pypi.org']}}}
            sandbox.prepare_options(config,state)
            data=json.loads((state/'runtime-config/sandbox.json').read_text())
            self.assertEqual(data['network']['allowedDomains'], sorted([
                'git.example.com:22','api.example.com:8443','npmjs.org:80','npmjs.org:443',
                '*.npmjs.org:80','*.npmjs.org:443','pypi.org:80','pypi.org:443']))
            self.assertIn('/tmp',data['filesystem']['additionalWriteRoots'])
            self.assertNotIn('/var/tmp/pi',data['filesystem']['additionalWriteRoots'])
            permissions=json.loads((state/'runtime-config/permissions.json').read_text())['permission']
            for surface in ('external_directory_read','external_directory_write'):
                self.assertEqual(permissions[surface]['/tmp/*'],'allow')
            self.assertEqual(permissions['path']['/home/node/.betterwright/vault/*'],'deny')
            self.assertNotIn('/tmp',[root for root,ro in folders.roots(config)])
            (state/'allowlist.txt').write_text('new.example.com:443\n')
            config['website_groups']={}
            sandbox.refresh_shell_network(config,state)
            after=json.loads((state/'runtime-config/sandbox.json').read_text())
            self.assertEqual(after['network']['allowedDomains'],['new.example.com:443'])
            self.assertEqual(after['filesystem'],data['filesystem'])

    def test_runtime_and_guidance_keep_private_temp_separate(self):
        source=Path(__file__).resolve().parents[1]/'ai_sandbox_factory/containers/dev'
        self.assertIn('RUN install -d -o node -g node -m 700 /var/tmp/pi',(source/'Dockerfile').read_text())
        self.assertNotIn('/tmp/pi-sandbox-factory-ready',(source/'entrypoint.sh').read_text())
        self.assertIn('/var/tmp/pi/pi-sandbox-factory-ready',(source/'entrypoint.sh').read_text())
        text=guidance({'browser':True})
        for phrase in ('running it in the background', 'Container /tmp','http://localhost:PORT','bash_escalate','preapproved'):
            self.assertIn(phrase,text)

    def test_server_guidance_matches_available_shell(self):
        for selected, expected, absent in [
            ({'browser': True}, 'start the dev server with bash_escalate', 'start the dev server with bash ('),
            ({'browser': True, 'approval': 'off'}, 'start the dev server with the editor terminal', 'start the dev server with bash_escalate'),
            ({'browser': True, 'inner_sandbox': False}, 'start the dev server with bash (', 'Ordinary bash has a separate network namespace'),
        ]:
            text=guidance(selected)
            self.assertIn(expected,text)
            self.assertNotIn(absent,text)

    def test_browser_disabled_has_no_localhost_instructions(self):
        self.assertNotIn('http://localhost:PORT',guidance({'browser':False}))
