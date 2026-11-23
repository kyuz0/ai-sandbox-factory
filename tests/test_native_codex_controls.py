"""Native controls discovery must not query independently loading remote frames."""
import ast
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit


source = Path(__file__).with_name('native_codex_controls.py')
tree = ast.parse(source.read_text())
node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'composer_frame')
namespace = {'time': time, 'urlsplit': urlsplit, 'BrowserError': RuntimeError}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
composer_frame = namespace['composer_frame']


class ComposerFrameTests(unittest.TestCase):
    def test_only_same_origin_native_webview_is_queried(self):
        page = Mock()
        page.url = 'http://127.0.0.1:34069/?folder=/workspace'
        remote = Mock()
        remote.url = 'https://cdn.platform.openai.com/deployments/dil/v15/runner.html'
        remote.is_detached.return_value = False
        remote.locator.side_effect = AssertionError('Remote runner must not be queried')
        workbench = Mock()
        workbench.url = page.url
        workbench.is_detached.return_value = False
        workbench.locator.side_effect = AssertionError('Workbench must not be queried')
        native = Mock()
        native.url = 'http://127.0.0.1:34069/stable/static/out/vs/workbench/contrib/webview/browser/pre/fake.html?id=native'
        native.is_detached.return_value = False
        native.locator.return_value.count.return_value = 1
        page.frames = [workbench, remote, native]
        self.assertIs(composer_frame(page, 10), native)
        remote.locator.assert_not_called()
        workbench.locator.assert_not_called()
        page.wait_for_timeout.assert_not_called()


if __name__ == '__main__':
    unittest.main()
