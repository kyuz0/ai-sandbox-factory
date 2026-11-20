"""Disposable controller hook. Only the fixed research MCP operation is allowed."""
import json
from pathlib import Path
import sys

request = json.load(sys.stdin)["toolCall"]
name = request["name"]
args = request.get("args", {})
allowed = name == "call_mcp_tool" and args.get("ServerName") == "workspace" and args.get("ToolName") == "read_marker"
record = Path.home() / "proof-tool-decisions"
with record.open("a") as log:
    print(name, "allow" if allowed else "deny", file=log)
print(json.dumps({"decision": "allow" if allowed else "deny", "reason": "Factory test controller policy"}))
