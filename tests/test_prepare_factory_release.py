"""A failed post-release step must resume its tag instead of skipping recovery."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('prepare_release',ROOT/'scripts/prepare_factory_release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class ReleaseRetry(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remote=self.root/'origin.git';self.checkout=self.root/'checkout'
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        self.git_global=['git','-c','protocol.file.allow=always']
        subprocess.run([*self.git_global,'clone','-q',str(self.remote),str(self.checkout)],check=True)
        self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.test')
        self.git('checkout','-b','main')
        (self.checkout/'ai_sandbox_factory').mkdir()
        (self.checkout/'ai_sandbox_factory/test.py').write_text('original\n')
        (self.checkout/'MANIFEST.in').write_text('include test\n')
        (self.checkout/'pyproject.toml').write_text('[project]\nname = "fixture"\nversion = "2026.1.1.0"\n')
        self.git('add','.');self.git('commit','-qm','Fixture package');self.git('push','-q','origin','HEAD:main')
        self.tested=self.git('rev-parse','HEAD')
        self.output=self.root/'outputs'
        self.now=datetime(2026,11,27,12,34,56,tzinfo=timezone.utc)

    def git(self,*args):
        return subprocess.check_output([*self.git_global,*args],cwd=self.checkout,text=True,stderr=subprocess.PIPE).strip()

    def test_retry_resumes_existing_release_after_image_retention_failed(self):
        version=release.prepare(self.checkout,'123',self.output,now=self.now)
        first=self.git('rev-parse','HEAD')
        self.assertEqual(version,'v2026.11.27.123456')
        self.git('checkout','--detach',self.tested)
        result=release.prepare(self.checkout,'123',self.output,now=self.now)
        self.assertEqual(result,version)
        self.assertEqual(self.git('rev-parse','HEAD'),first)
        self.assertEqual(self.git('ls-remote','origin','refs/tags/v*').count('refs/tags/'),1)
        self.assertEqual(self.output.read_text().count('version='+version),2)

    def test_a_newer_product_change_is_left_for_its_own_check_run(self):
        (self.checkout/'ai_sandbox_factory/test.py').write_text('newer package\n')
        self.git('add','.');self.git('commit','-qm','New package');self.git('push','-q','origin','HEAD:main')
        self.git('checkout','--detach',self.tested)
        self.assertIsNone(release.prepare(self.checkout,'123',self.output,now=self.now))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.git('ls-remote','origin','refs/tags/v*'),'')

    def test_retry_does_not_retag_a_release_that_lost_its_remote_tag(self):
        version=release.prepare(self.checkout,'123',self.output,now=self.now)
        self.git('push','origin',':refs/tags/'+version)
        self.git('checkout','--detach',self.tested)
        with self.assertRaisesRegex(ValueError,'matching remote tag'):
            release.prepare(self.checkout,'123',self.output,now=self.now)


if __name__=='__main__':unittest.main()
