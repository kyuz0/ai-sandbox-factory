"""Opening a text-entry dialog must route the first keystroke to its field."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Input, Select

from ai_sandbox_factory.aws_ui import AWSAccessDialog
from ai_sandbox_factory.chat_history_ui import ChatTransfer
from ai_sandbox_factory.communications_ui import SlackCredentials
from ai_sandbox_factory.credentials_ui import CredentialDialog
from ai_sandbox_factory.file_browser import FolderPicker
from ai_sandbox_factory.folders_ui import FolderEditor
from ai_sandbox_factory.models_ui import ModelDialog, ProviderDialog
from ai_sandbox_factory.prompt_ui import EditPrompt
from ai_sandbox_factory.repository_ui import RepositorySetup
from ai_sandbox_factory.sandbox_tui import Cockpit, SearchProviderDialog
from test_hosted_models_ui import ModelService


class DialogFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_type_immediately_after_opening(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update', return_value=None), \
                patch('ai_sandbox_factory.provider_credentials.entries', return_value={}):
            root = Path(temp)
            service = ModelService([])
            service.plugin_model = service.hosted_request
            app = Cockpit(root, service)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                for dialog, field in [
                    (AWSAccessDialog({}), 'aws-name'),
                    (ModelDialog('demo', 'company', plugin_label='Example provider'), 'model-search'),
                    (ModelDialog('demo', 'openrouter'), 'model-search'),
                    (FolderEditor(None, [], root, root), 'folder-source'),
                    (FolderPicker(root, mode='save'), 'folder-filename'),
                    (RepositorySetup(), 'repository-url'),
                    (SlackCredentials({}, {}), 'slack-botToken'),
                    (CredentialDialog('Example', None), 'managed-key'),
                    (ProviderDialog({'provider': 'openrouter'}), 'hosted-key'),
                    (ProviderDialog({'provider': 'azure'}), 'hosted-endpoint'),
                    (ProviderDialog({'provider': 'amazon-bedrock'}), 'hosted-region'),
                    (SearchProviderDialog({'exa': {}}, 'exa'), 'search-key'),
                    (SearchProviderDialog({'searxng': {}}, 'searxng'), 'search-endpoint'),
                    (ChatTransfer('demo', importing=True), 'chat-transfer-path'),
                    (EditPrompt('New prompt', None, lambda value: None), 'prompt-name'),
                ]:
                    with self.subTest(dialog=type(dialog).__name__, field=field):
                        await app.push_screen(dialog)
                        await pilot.press('x')
                        await app.workers.wait_for_complete()
                        control = dialog.query_one('#' + field, Input)
                        self.assertEqual(control.value, 'x')
                        self.assertIs(app.focused, control)
                    await app.pop_screen()
                    await pilot.pause()

                for dialog, field in [(ProviderDialog(), 'hosted-provider'), (SearchProviderDialog({}), 'search-provider-name')]:
                    with self.subTest(field=field):
                        await app.push_screen(dialog)
                        await pilot.pause()
                        self.assertIs(app.focused, dialog.query_one('#' + field, Select))
                    await app.pop_screen()
                    await pilot.pause()

    async def test_model_search_keeps_typed_filter_while_catalogue_loads(self):
        with tempfile.TemporaryDirectory() as temp, patch('ai_sandbox_factory.updates.available_update', return_value=None):
            service = ModelService([])
            release = threading.Event()

            def catalogue(*args, **kwargs):
                release.wait()
                reply = service.hosted_request(*args, **kwargs)
                reply['models'].append(dict(reply['models'][0], id='other', name='Other'))
                return reply

            service.plugin_model = catalogue
            app = Cockpit(Path(temp), service)
            async with app.run_test(size=(80, 24)) as pilot:
                dialog = ModelDialog('demo', 'company', plugin_label='Example provider')
                await app.push_screen(dialog)
                try:
                    await pilot.press('e', 'x')
                    self.assertEqual(dialog.query_one('#model-search', Input).value, 'ex')
                    self.assertFalse(dialog.models)
                finally:
                    release.set()
                await app.workers.wait_for_complete()
                self.assertEqual([model['id'] for model in dialog.filtered], ['model-1'])
                self.assertIs(app.focused, dialog.query_one('#model-search', Input))
