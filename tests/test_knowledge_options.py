import subprocess
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_options import options


class DocumentSearchTests(unittest.TestCase):
    endpoint = {'knowledge_embedding': 'endpoint', 'knowledge_embedding_url': 'https://models.example/v1/',
                'knowledge_embedding_model': 'test-embedding'}

    def test_default_sends_nothing_and_endpoint_requires_url_and_model(self):
        self.assertEqual(options()['knowledge_embedding'], 'off')
        disabled = options({**self.endpoint, 'knowledge_embedding': 'off'})
        self.assertEqual((disabled['knowledge_embedding_url'], disabled['knowledge_embedding_model']), ('', ''))
        self.assertEqual(options(self.endpoint)['knowledge_embedding_url'], 'https://models.example/v1')
        for url in ('', 'file:///tmp/models', 'https://user:password@models.example/v1',
                    'https://models.example/v1/embeddings', 'https://models.example/v1?key=secret',
                    'https://models.example:65536/v1', 'https://models.example/v1/../other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                options({**self.endpoint, 'knowledge_embedding_url': url})
        with self.assertRaisesRegex(ValueError, 'model ID'):
            options({**self.endpoint, 'knowledge_embedding_model': ''})

    def test_build_uses_only_the_selected_recipe_and_never_runs_the_old_model_download(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            common = Path(tmp)
            layer = common / 'local-knowledge'
            layer.mkdir()
            # A frozen sandbox can still have this old recipe on disk.
            legacy = layer / 'Dockerfile'
            legacy.touch()
            config = {'name': 'demo', 'engine': 'podman', 'uid': 1000, 'gid': 1000,
                      'profile': sandbox.load_profile('base'), 'common': {'path': tmp, 'digest': 'fixture'}}
            stack.enter_context(patch.object(sandbox.host_platform, 'check_engine'))
            stack.enter_context(patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=common))
            stack.enter_context(patch.object(sandbox, 'tree_digest', side_effect=lambda path:
                'fixture' if path == common else config['profile']['digest']))
            stack.enter_context(patch.object(sandbox.sandbox_extensions, 'build_contexts', return_value=nullcontext([])))
            for module in (sandbox.sandbox_ssh, sandbox.inference, sandbox.sandbox_advisors, sandbox.sandbox_skills):
                stack.enter_context(patch.object(module, 'build'))
            run = stack.enter_context(patch.object(sandbox, 'run', return_value=subprocess.CompletedProcess([], 0)))

            sandbox.build(config)
            self.assertFalse(any(layer in call.args[0] for call in run.call_args_list))
            config['options'] = self.endpoint
            with self.assertRaisesRegex(ValueError, 'Update sources'):
                sandbox.build(config)
            recipe = layer / 'Dockerfile.endpoint'
            recipe.touch()
            sandbox.build(config)
            self.assertEqual([call.args[0][-3:] for call in run.call_args_list if layer in call.args[0]],
                             [['-f', recipe, layer]])

            config['options'] = {'knowledge_embedding': 'local'}
            run.reset_mock()
            sandbox.build(config)
            self.assertTrue(any(call.args[0][1:3] == ['image', 'inspect'] for call in run.call_args_list))
            self.assertFalse(any(layer in call.args[0] for call in run.call_args_list))
            run.return_value = subprocess.CompletedProcess([], 1)
            with self.assertRaisesRegex(ValueError, 'previously bundled embedding model is missing'):
                sandbox.build(config)
