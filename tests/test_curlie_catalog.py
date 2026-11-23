import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import curlie_catalog as curlie, network_groups
from ai_sandbox_factory.sandbox_options import options


class CurlieTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)
        self.log = lambda message: None

    def archive(self, news):
        paths = [('News', news), ('Health', 'www.nhs.uk'), ('Science', 'example.edu'),
                 ('Regional/North_America/United_States/Government', 'www.sec.gov'),
                 ('Business', 'business.example'), ('Computers', 'computer.example'),
                 ('Reference', 'reference.example'), ('Society', 'society.example'),
                 ('Arts', 'arts.example'), ('Sports', 'sports.example'), ('Home', 'home.example')]
        structure = ''.join(f'{index}\t{path}\t1\n' for index, (path, _) in enumerate(paths))
        listings = ''.join(f'https://{host}/path\ttitle\tdescription\t{index}\n'
                           for index, (_, host) in enumerate(paths))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            for name, text in [('export-s.tsv', structure), ('export-c.tsv', listings)]:
                data = text.encode()
                item = tarfile.TarInfo(name)
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
        output.seek(0)
        return output

    def response(self, domain, etag):
        archive = self.archive(domain)
        archive.headers = {'ETag': etag}
        return archive

    def snapshots(self, names, saved=None, **kwargs):
        with patch.object(network_groups, 'fetch', return_value='ut1.example\n'):
            return network_groups.snapshots(names, saved, cache=self.cache, warn=self.log, **kwargs)

    def test_first_use_downloads_once_and_preserves_ut1_and_exact_hosts(self):
        self.assertIsNone(curlie.metadata(self.cache))
        self.assertFalse({'curlie-health', 'curlie-government'}.intersection(options()['website_groups']))
        with patch.object(curlie, 'urlopen', return_value=self.response('www.cnbc.com', 'first')) as fetch:
            self.snapshots(['ut1-bank'])
            fetch.assert_not_called()
            saved = self.snapshots(['ut1-press', 'curlie-health', 'curlie-government'])
            self.assertEqual(self.snapshots(list(saved)), saved)
            fetch.assert_called_once()
        self.assertEqual(saved['ut1-press']['domains'], ['.ut1.example', 'www.cnbc.com'])
        self.assertEqual(saved['curlie-government']['domains'], ['www.sec.gov'])
        self.assertEqual(saved['curlie-health']['domains'], ['www.nhs.uk'])

    def test_new_saved_copy_requires_opt_in_for_an_existing_sandbox(self):
        with patch.object(curlie, 'urlopen', return_value=self.response('old.example', 'first')):
            existing = self.snapshots(['ut1-press'])
        first = curlie.metadata(self.cache)
        with patch.object(curlie, 'urlopen', return_value=self.response('new.example', 'second')):
            latest = curlie.refresh(self.cache, force=True, log=self.log)
        self.assertNotEqual(latest['revision'], first['revision'])
        self.assertEqual(self.snapshots(['ut1-press'], existing, download_curlie=False), existing)
        self.assertEqual(self.snapshots(['ut1-press'], existing), existing)
        self.assertIn('new.example', self.snapshots(['ut1-press'])['ut1-press']['domains'])
        updated = self.snapshots(['ut1-press'], existing, curlie_revision=latest['revision'])
        self.assertEqual(updated['ut1-press']['domains'], ['.ut1.example', 'new.example'])
        curlie.version_path(self.cache, first['revision']).unlink()
        self.assertEqual(self.snapshots(['ut1-press'], existing), existing)

    def test_bad_archive_or_unchanged_etag_keeps_previous_download(self):
        with patch.object(curlie, 'urlopen', return_value=self.response('old.example', 'first')):
            first = curlie.refresh(self.cache, log=self.log)
        same = io.BytesIO()
        same.headers = {'ETag': 'first'}
        with patch.object(curlie, 'urlopen', return_value=same):
            self.assertEqual(curlie.refresh(self.cache, force=True, log=self.log), first)
        invalid = io.BytesIO(b'not a gzip archive')
        invalid.headers = {'ETag': 'second'}
        with patch.object(curlie, 'urlopen', return_value=invalid):
            with self.assertRaisesRegex(ValueError, 'The Curlie download failed'):
                curlie.refresh(self.cache, force=True, log=self.log)
        self.assertEqual(curlie.metadata(self.cache), first)

    def test_starting_legacy_sandbox_does_not_download_or_apply_shared_curlie(self):
        existing = {'ut1-press': {'domains': ['.old-news.example']}}
        with patch.object(curlie, 'urlopen', side_effect=AssertionError('unexpected request')):
            self.assertEqual(self.snapshots(['ut1-press'], existing, download_curlie=False), existing)
