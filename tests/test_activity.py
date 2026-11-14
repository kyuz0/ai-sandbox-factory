"""Lifecycle dates describe saved changes, independently of image identities."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pi_sandbox_factory import activity, editor_settings

class ActivityTests(unittest.TestCase):
    def test_creation_edits_and_noop_save(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)
            self.assertEqual(activity.load(state), dict(created=None,edited=None,started=None))
            activity.save_config(state, {'name':'demo'})
            activity.record(state,'created')
            activity.record(state,'started')
            original=activity.load(state)
            activity.save_config(state, {'name':'demo'})
            self.assertEqual(activity.load(state), original)
            activity.save_config(state, {'name':'demo','memory':'8g'})
            edited=activity.load(state)
            self.assertIsNotNone(edited['edited'])
            self.assertEqual(edited['created'], original['created'])
            self.assertEqual(edited['started'], original['started'])
            self.assertEqual(json.loads((state/'config.json').read_text()), {'name':'demo','memory':'8g'})
            self.assertEqual(activity.display(None),'—')

    def test_editor_changes_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);activity.save_config(state, {'name':'demo'})
            editor_settings.save(state, {'mode':'pi'})
            edited=activity.load(state)['edited'];self.assertIsNotNone(edited)
            editor_settings.save(state, {'mode':'pi'})
            self.assertEqual(activity.load(state)['edited'],edited)
            for content in ['null','123','"2026-11-14"','"bad"']:
                (state/'activity/created.json').write_text(content)
                self.assertIsNone(activity.load(state)['created'])

    def test_failed_write_records_no_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);activity.save_config(state, {'name':'demo'})
            with patch.object(activity,'write_json',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):activity.save_config(state, {'name':'changed'})
            self.assertIsNone(activity.load(state)['edited'])
