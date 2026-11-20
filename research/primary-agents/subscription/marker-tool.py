"""A fixed MCP read operation that runs entirely inside the disposable workspace."""
import json
import sys
from pathlib import Path

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "serverInfo": {"name": "workspace", "version": "1"}, "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": "read_marker", "description": "Return this test workspace's random marker.",
                             "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call" and msg["params"]["name"] == "read_marker":
        Path("/tmp/tool-called").touch()
        result = {"content": [{"type": "text", "text": Path("/workspace/marker").read_text().strip()}]}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
