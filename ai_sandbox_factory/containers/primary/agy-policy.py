"""Keep model tool calls on the workspace side of the controller."""
import json
from pathlib import Path
import sys

name = ''
try:
    request = json.load(sys.stdin)['toolCall']
    name = request['name']
    args = request['args']
    settings = json.loads(Path('/run/primary-input/settings.json').read_text())
    allowed = name == 'call_mcp_tool' and args.get('ServerName') == 'factory_workspace'
    tools = {'workspace_shell', 'browser'} if settings['browser'] else {'workspace_shell'}
    allowed = allowed and args.get('ToolName') in tools
except (ValueError, KeyError, OSError, TypeError, AttributeError):
    allowed = False
print(json.dumps({'decision': 'allow' if allowed else 'deny',
                  'reason': 'Use the Factory workspace tools for file, shell, and browser operations.'}))
