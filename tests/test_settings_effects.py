import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from textual.widgets import Button
from ai_sandbox_factory import settings_effects
from ai_sandbox_factory.sandbox_options import DEFAULTS
from ai_sandbox_factory.sandbox_tui import Cockpit, DEFAULT_SETTINGS, ApplySettingsReview
from test_tui import FakeService


class SettingsEffectsTests(unittest.TestCase):
    def test_every_managed_option_has_an_effect_assessment(self):
        self.assertEqual(set(settings_effects.OPTION_GROUPS),set(DEFAULTS))

    def test_changes_are_specific_and_never_include_credentials(self):
        before={'options':{'browser':False},'model_providers':[{'name':'local','settings':{}}], 'allowlist':'example.com:443'}
        draft=copy.deepcopy(before);draft['model_providers']={'local':{}}
        self.assertEqual(settings_effects.configuration_changes(before,draft),[])
        draft['options']={'browser':True,'compaction_recall':'full'}
        draft['library_skills']=['id'];draft['_provider_keys']={'private':'SECRET_VALUE'}
        draft['_search_keys_changed']=True
        groups=settings_effects.configuration_changes(before,draft)
        self.assertIn('Skills',groups);self.assertIn('Browser and saved vault protection',groups)
        self.assertIn('Pi features',groups)
        self.assertIn('Search credentials',groups)
        notes=' '.join(settings_effects.configuration_notes(before,draft))
        self.assertNotIn('SECRET_VALUE',notes)
        self.assertIn('Resume your saved conversation',notes)
        self.assertIn('Reopen the browser viewer',notes)

    def test_notices_are_relevant_to_changed_settings_and_selected_client(self):
        before={'options':{},'memory':'8g'}
        after={'options':{},'memory':'16g'}
        for mode, expected in [('browser','Open in browser'),('pi','Restart Pi CLI'),('desktop','Reload the VS Code')]:
            notes=settings_effects.configuration_notes(before,after,mode)
            text=settings_effects.bullets(notes)
            self.assertIn(expected,text)
            for unrelated in ('Slack','SSH','Identity','browser viewer','clean context','removed connection'):
                self.assertNotIn(unrelated,text)
            self.assertTrue(all(line.startswith('• ') for line in text.splitlines()))
            if mode!='desktop': self.assertNotIn('VS Code',text)
            if mode!='pi': self.assertNotIn('Pi CLI',text)
        after['options']={'browser':False}
        self.assertNotIn('browser viewer',' '.join(settings_effects.configuration_notes({'options':{'browser':True}},after)))
        self.assertNotIn('Slack',' '.join(settings_effects.configuration_changes(before,after)))
        after['_communication_keys']={'botToken':'never-display-this'}
        self.assertIn('Slack credentials',settings_effects.configuration_changes(before,after))
        self.assertNotIn('never-display-this',settings_effects.bullets(settings_effects.configuration_notes(before,after)))

    def test_ui_review_and_completion_keep_actions_reachable(self):
        async def check():
            for size in ((80,24),(120,40),(220,50)):
                with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update',return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults',side_effect=lambda:copy.deepcopy(DEFAULT_SETTINGS)):
                    app=Cockpit(Path(temp),FakeService([]))
                    async with app.run_test(size=size) as pilot:
                        await pilot.pause();await app.workers.wait_for_complete()
                        draft={'options':{'browser':True,'goals':False,'git_identity':{'name':'Example','email':'example@example.com'}},'library_skills':['id']}
                        review=ApplySettingsReview({},draft)
                        await app.push_screen(review);await pilot.pause()
                        for key in ('#yes','#cancel'):
                            button=review.query_one(key,Button)
                            self.assertGreaterEqual(button.region.y,0)
                            self.assertLess(button.region.bottom,size[1])
                        await pilot.press('escape');await pilot.pause()
                        self.assertNotIsInstance(app.screen,ApplySettingsReview)
                        app.start_settings_job('example', 'Updated example', lambda log, update: log('Applied.'), success_message=settings_effects.REOPEN)
                        await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                        self.assertTrue(app.screen.query_one('#operation-next').display)
                        self.assertLess(app.screen.query_one('#job-hide',Button).region.bottom,size[1])
                        self.assertLessEqual(app.screen.query_one('#operation-next').region.bottom,app.screen.query_one('#operation-log').region.y)
                        await app.pop_screen()
                        def fail(log, update):
                            raise ValueError('Settings were not applied')
                        app.start_settings_job('example', 'Updating example', fail, success_message=settings_effects.REOPEN)
                        await pilot.pause();await app.workers.wait_for_complete();await pilot.pause()
                        self.assertEqual(app.screen.job.result, 'Failed')
                        self.assertFalse(app.screen.query_one('#operation-next').display)
        asyncio.run(check())
