"""Execute shipped editor environment setup without launching an editor."""
import ast
import os
from pathlib import Path
from unittest.mock import patch


def editor_environment(agent, inherited):
    path = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/editor/launch.py'
    tree = ast.parse(path.read_text())
    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets)
    start = next((index for index, node in enumerate(tree.body) if assigns(node, 'env')), None)
    if start is None:
        raise AssertionError('Cannot inspect the shipped editor environment: its env initialization changed. Update this test helper to match the launcher.')
    # The next file write prepares the watch fallback. Stop before it; everything
    # before this boundary operates only on the copied environment dictionary.
    end = next((index for index in range(start + 1, len(tree.body))
                if assigns(tree.body[index], 'watch_fallback')), None)
    if end is None:
        raise AssertionError('Cannot inspect the shipped editor environment: its watch-fallback boundary changed. Update this test helper before executing launcher setup.')
    code = compile(ast.Module(body=tree.body[start:end], type_ignores=[]), str(path), 'exec')
    with patch.dict(os.environ, inherited, clear=True):
        namespace = {'os': os, 'settings': {'agent': agent}}
        exec(code, namespace)
        return namespace['env']
