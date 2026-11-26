"""Service data commands use lifecycle locking and preserve actionable errors."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_sandbox_factory import sandbox, service_data


class ServiceDataCLI(unittest.TestCase):
    def test_export_and_restore_dispatch_under_lifecycle_lock_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root/'example'
            state.mkdir()
            config = {'name':'example','engine':'podman','profile':{}}
            (state/'config.json').write_text(json.dumps(config))
            held = []
            @contextmanager
            def lock(name, operation):
                held.append(name)
                try: yield
                finally: held.pop()
            def recovered(config, path):
                self.assertEqual(held,['example'])
                return {**config,'recovered':True}
            for operation in ('export','restore'):
                args = ['factory','--state-dir',str(root),'service-data-'+operation,'example','--saved','saved-id']
                args += ['--volume','data','--output',str(root/'copy.tar')] if operation=='export' else ['--service','database']
                def work(config, path, *arguments, **kwargs):
                    self.assertEqual(held,['example'])
                    self.assertTrue(config['recovered'])
                    return root/'copy.tar' if operation=='export' else config
                with patch.object(sys,'argv',args), patch.object(sandbox.operation_locks,'sandbox',side_effect=lock), \
                        patch.object(sandbox,'recover_profile',side_effect=recovered), \
                        patch.object(service_data,operation,side_effect=work) as action, redirect_stdout(io.StringIO()) as output:
                    sandbox.main()
                self.assertEqual(action.call_args.args[2],'saved-id')
                self.assertIn('exported to' if operation=='export' else 'restored to database',output.getvalue())
                self.assertEqual(held,[])

    def test_error_preserves_volume_cause_and_next_step_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'example').mkdir()
            (root/'example/config.json').write_text(json.dumps({'name':'example','engine':'podman'}))
            error = "Cannot export volume 'exact-volume' (exit 125): no space left. Choose a writable destination."
            with patch.object(sys,'argv',['factory','--state-dir',str(root),'service-data-export','example',
                                          '--saved','id','--volume','data','--output','copy.tar']), \
                    patch.object(sandbox.operation_locks,'private_runtime_dir',return_value=root), \
                    patch.object(service_data,'export',side_effect=ValueError(error)), redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    sandbox.main()
            self.assertEqual(raised.exception.code,1)
            self.assertIn(error,output.getvalue())
            self.assertNotIn('Traceback',output.getvalue())
