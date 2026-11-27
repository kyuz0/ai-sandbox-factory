"""Scanner findings need scoped review; missing scans must never pass."""
from datetime import date
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('image_scan',ROOT/'scripts/check_image_scan.py')
scan=importlib.util.module_from_spec(spec);spec.loader.exec_module(scan)


def finding(id='CVE-fixture',package='fixture',severity='HIGH',fixed='2.0',installed='1.0'):
    return {'VulnerabilityID':id,'PkgName':package,'Severity':severity,'InstalledVersion':installed,'FixedVersion':fixed}


def report(*items):
    return {'Results':[{'Target':'fixture-image','Vulnerabilities':list(items)}]}


class ImageScanReview(unittest.TestCase):
    def test_all_findings_remain_visible_while_only_actionable_ones_block(self):
        result=scan.review(report(finding(),finding(id='CVE-no-fix',fixed=''),finding(id='CVE-low',severity='LOW')),set(),[])
        self.assertEqual(len(result['findings']),3)
        self.assertEqual([row['id'] for row in result['requires_review']],['CVE-fixture'])

    def test_known_exploitation_requires_review_even_with_low_severity_or_no_fix(self):
        result=scan.review(report(finding(severity='LOW',fixed='')),{'CVE-fixture'},[])
        self.assertTrue(result['requires_review'][0]['known_exploited'])

    def test_missing_or_empty_inventory_is_a_failure(self):
        for value in ({},{'Results':[]},{'Results':None}):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'inventory'):
                scan.review(value,set(),[])

    def test_exception_cannot_hide_another_package_or_scan_target(self):
        exception={'id':'CVE-fixture','package':'fixture','installed':'1.0','role':'gateway','target':'fixture-image','reason':'Reviewed fixture call path is not exposed','review_before':'2027-01-01'}
        result=scan.review(report(finding(),finding(package='other')),set(),[exception],date(2026,11,27),role='gateway')
        self.assertEqual([row['package'] for row in result['requires_review']],['other'])
        other=scan.review(report(finding()),set(),[exception],date(2026,11,27),role='controller-claude')
        self.assertEqual(len(other['requires_review']),1)
        result=scan.review({'Results':[{'Target':'other-image','Vulnerabilities':[finding()]}]},set(),[exception],date(2026,11,27),role='gateway')
        self.assertEqual(len(result['requires_review']),1)
        result=scan.review(report(finding(installed='1.1')),set(),[exception],date(2026,11,27),role='gateway')
        self.assertEqual(len(result['requires_review']),1)

    def test_expired_or_unexplained_exception_does_not_silently_pass(self):
        exception={'id':'CVE-fixture','package':'fixture','installed':'1.0','role':'gateway','target':'fixture-image','reason':'Reviewed fixture call path','review_before':'2026-11-26'}
        result=scan.review(report(finding()),set(),[exception],date(2026,11,27),role='gateway')
        self.assertEqual(len(result['requires_review']),1)
        self.assertEqual(scan.review(report(),set(),[exception],date(2026,11,27),role='gateway')['requires_review'],[])
        for value in (dict(exception,review_before='2027-01-01',reason=''),dict(exception,review_before='not a date')):
            with self.subTest(value=value),self.assertRaises(ValueError):
                scan.review(report(finding()),set(),[value],date(2026,11,27),role='gateway')

    def test_no_vulnerabilities_with_a_valid_inventory_is_clean(self):
        result=scan.review(report(),set(),[])
        self.assertEqual(result['findings'],[])
        self.assertEqual(result['inventory_targets'],['fixture-image'])


if __name__=='__main__':unittest.main()
