"""Applying a Codex search toggle must not reapply unchanged network rules."""
import unittest
from unittest.mock import Mock

from textual.widgets import TextArea
from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import SettingsForm, WebsiteGroups


class CodexNetworkSettingsTests(unittest.TestCase):
    def form(self, text='api.example.invalid:443\ncode.example.invalid:443',
             groups=('auth-microsoft', 'auth-google'), revision=None):
        form = Mock()
        form.draft = {'allowlist': 'code.example.invalid:443\napi.example.invalid:443',
                      'options': {'website_groups': ['auth-google', 'auth-microsoft']},
                      'website_groups': {'fixture': {'curlie_download': {'revision': 'saved-revision'}}}}
        form.network_rules.side_effect = sandbox.parse_rules
        area = Mock(text=text)
        websites = Mock()
        websites.selected.return_value = list(groups)
        websites.curlie_revision.return_value = revision
        form.query_one.side_effect = lambda target, kind=None: websites if target is WebsiteGroups else area
        return form

    def test_rule_and_group_order_and_same_revision_do_not_apply_network(self):
        for revision in (None, 'saved-revision'):
            with self.subTest(revision=revision):
                self.assertFalse(SettingsForm.network_rules_changed(self.form(revision=revision)))

    def test_changed_rule_group_or_revision_requires_network_apply(self):
        for changes in ({'text': 'other.example.invalid:443'},
                        {'groups': ('auth-google',)}, {'revision': 'new-revision'}):
            with self.subTest(changes=changes):
                self.assertTrue(SettingsForm.network_rules_changed(self.form(**changes)))

    def test_invalid_allowlist_is_reported_before_saving_search(self):
        form = self.form(text='https://invalid.example.invalid/path')
        with self.assertRaises(ValueError):
            SettingsForm.network_rules_changed(form)
        form.network_rules.assert_called_once_with('https://invalid.example.invalid/path')


if __name__ == '__main__':
    unittest.main()
