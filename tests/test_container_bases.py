"""Keep Factory-owned container stages on the supported Debian baseline."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory'


class ContainerBaseTests(unittest.TestCase):
    def test_owned_bases_explicitly_select_debian_13(self):
        count = 0
        for dockerfile in ROOT.rglob('Dockerfile*'):
            stages = set()
            for line in dockerfile.read_text().splitlines():
                match = re.match(r'FROM\s+(\S+)', line, re.I)
                if not match:
                    continue
                image = match[1]
                previous_stage = image in stages
                alias = re.search(r'\sAS\s+(\S+)', line, re.I)
                if alias:
                    stages.add(alias[1])
                if image == '${BASE_IMAGE}' or previous_stage:
                    continue
                if image == '${CODEX_IMAGE}':
                    # Only the pinned native binary is copied from Factory's Codex image.
                    self.assertEqual(dockerfile.relative_to(ROOT), Path('containers/session-transfer/Dockerfile'))
                    self.assertEqual(line, 'FROM ${CODEX_IMAGE} AS native-codex')
                    self.assertIn('COPY --from=native-codex /usr/local/bin/factory-agent /usr/local/bin/factory-codex',
                                  dockerfile.read_text())
                    continue
                if image.startswith('public.ecr.aws/aws-cli/aws-cli:'):
                    # Vendor tooling is copied into our own Debian runtime.
                    self.assertIn(' AS awscli', line)
                    continue
                self.assertRegex(image, r'^docker\.io/library/(?:node|python|debian):')
                self.assertIn('trixie', image, str(dockerfile.relative_to(ROOT)))
                count += 1
        self.assertGreaterEqual(count, 9)

    def test_credential_companions_keep_immutable_base_digests(self):
        for name in ('containers/inference/Dockerfile', 'connectors/companion/Dockerfile'):
            first = (ROOT / name).read_text().splitlines()[0]
            self.assertRegex(first, r'trixie[^@\s]*@sha256:[0-9a-f]{64}$', name)


if __name__ == '__main__':
    unittest.main()
