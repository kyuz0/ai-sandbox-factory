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

    def test_reviewed_upstream_ignore_has_no_forced_expiry_and_keeps_the_finding_visible(self):
        exception={'id':'CVE-fixture','package':'fixture','installed':'1.0','role':'inference','target':'fixture-image','reason':'Upstream-bundled dependency; use vendor updates, no custom patches','reviewed_on':'2026-11-27'}
        result=scan.review(report(finding(),finding(id='CVE-other')),set(),[exception],date(2027,1,1),role='inference')
        self.assertEqual(len(result['findings']),2)
        self.assertEqual([row['id'] for row in result['requires_review']],['CVE-other'])
        self.assertEqual(result['findings'][0]['exception'],exception)
        changed=scan.review(report(finding(installed='1.1')),set(),[exception],date(2027,1,1),role='inference')
        self.assertEqual(len(changed['requires_review']),1)

    def test_workspace_findings_are_informational_and_all_remain_visible(self):
        for role in ('pi-common','pi-stock','native-common'):
            with self.subTest(role=role):
                result=scan.review(report(finding(),finding(id='CVE-kev',severity='LOW',fixed='')),{'CVE-kev'},[],role=role)
                self.assertEqual(result['policy'],'informational')
                self.assertEqual(len(result['findings']),2)
                self.assertTrue(result['findings'][1]['known_exploited'])
                self.assertEqual(result['requires_review'],[])
                with self.assertRaisesRegex(ValueError,'inventory'):
                    scan.review({},set(),[],role=role)

    def test_every_authority_role_and_an_unknown_new_role_retain_blocking_gates(self):
        roles=('controller-codex','controller-claude','controller-agy','advisor','inference','ssh','aws','connector','gateway','new-companion')
        for role in roles:
            with self.subTest(role=role):
                result=scan.review(report(finding(),finding(id='CVE-kev',severity='LOW',fixed='')),{'CVE-kev'},[],role=role)
                self.assertEqual(result['policy'],'blocking')
                self.assertEqual(len(result['requires_review']),2)


if __name__=='__main__':unittest.main()
