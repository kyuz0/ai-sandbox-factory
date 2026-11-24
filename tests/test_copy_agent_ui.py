import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, Select

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import Cockpit, NewSandbox, SettingsForm, copy_draft, options
from test_tui import FakeService


class CopyAgentUITests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_all_agents_and_return_to_the_source_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'allowlist.txt').write_text('example.test:443\nlocal-model.internal:8000\n')
            profile = sandbox.snapshot_profile(sandbox.load_profile('base'), source)
            reference = root / 'reference'
            reference.mkdir()
            config = dict(name='source', primary_agent='pi', engine='podman', profile=profile,
                          memory='12g', network='allowlist', workspace=str(root / 'original'),
                          model_providers={'local': {}}, local_model_ports='8000', subagents=True,
                          extra_folders=[dict(name='reference', source=str(reference), read_only=True)],
                          advisors={'providers': ['claude']},
                          options=options({'browser': False, 'knowledge_embedding': 'endpoint',
                                           'knowledge_embedding_model': 'fixture',
                                           'knowledge_embedding_url': 'https://embeddings.example.test/v1',
                                           'git_identity': {'name': 'Test Author', 'email': 'author@example.test'}}))
            original = copy.deepcopy(config)
            Path(config['workspace']).mkdir()
            submitted = []
            service = FakeService([])
            app = Cockpit(root / 'state', service)
            app.start_creation = submitted.append
            with patch('ai_sandbox_factory.updates.available_update', return_value=None), \
                    patch.dict('os.environ', {'SANDBOX_REGISTRY_DIR': str(root / 'packages')}):
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    for source_agent, target_agent in (('pi', 'codex'), ('pi', 'claude'), ('pi', 'agy'),
                                                       ('codex', 'pi'), ('claude', 'agy'), ('agy', 'codex')):
                        config['primary_agent'] = source_agent
                        draft = copy_draft({'name': 'source', 'config': config}, root)
                        if source_agent != 'pi':
                            draft.update(model_providers={}, local_model_ports=None, subagents=False, advisors={})
                            draft['options'] = options({'browser': False, 'git_identity': config['options']['git_identity']})
                        new = NewSandbox(draft, search_keys={})
                        await app.push_screen(new)
                        await pilot.pause()
                        self.assertEqual(new.query_one('#workspace', Input).value, config['workspace'])
                        selector = new.query_one('#primary-agent', Select)
                        self.assertFalse(selector.disabled)
                        selector.value = target_agent
                        await pilot.pause()
                        self.assertEqual(new.query_one('#workspace', Input).value, config['workspace'])
                        self.assertLessEqual(new.query_one('#name', Input).region.bottom,
                                             new.query_one('.form').content_region.bottom)
                        workspace = (Path(config['workspace']) if (source_agent, target_agent) == ('pi', 'codex')
                                     else root / f'{source_agent}-{target_agent}')
                        workspace.mkdir(exist_ok=True)
                        new.query_one('#workspace', Input).value = str(workspace)
                        await pilot.pause()
                        new.query_one('#continue', Button).press()
                        await pilot.pause()
                        form = app.screen
                        self.assertIsInstance(form, SettingsForm)
                        self.assertEqual(form.primary, target_agent)
                        selected, _ = form.collect()
                        self.assertEqual(selected['primary_agent'], target_agent)
                        self.assertEqual(selected['workspace'], str(workspace))
                        self.assertEqual(selected['memory'], '12g')
                        self.assertEqual(selected['extra_folders'], draft['extra_folders'])
                        self.assertEqual(selected['reuse_from'], 'source')
                        self.assertEqual(selected['options']['git_identity'], config['options']['git_identity'])
                        if target_agent != 'pi':
                            self.assertEqual(selected['profile'], 'base')
                            self.assertFalse(selected['model_providers'])
                            self.assertFalse(selected['advisors'].get('providers'))
                            self.assertFalse(selected['local_model_ports'])
                            self.assertEqual(selected['options']['knowledge_embedding'], 'off')
                            self.assertIn('example.test:443', selected['allowlist'])
                        form.action_back()
                        await pilot.pause()
                        self.assertIs(app.screen, new)
                        selector.value = source_agent
                        await pilot.pause()
                        self.assertEqual(new.query_one('#workspace', Input).value, str(workspace))
                        self.assertEqual(new.copied_draft, draft)
                        await app.pop_screen()
                    self.assertFalse(submitted)
                    original['primary_agent'] = config['primary_agent']
                    self.assertEqual(config, original)
