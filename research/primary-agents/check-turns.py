"""Check what the three real clients sent to the private model fixtures."""
import json
from pathlib import Path

for client, search_tool in (("codex", "web_search"), ("claude", "WebSearch")):
    rows = [json.loads(line) for line in Path("/tmp/events-" + client).read_text().splitlines()]
    for mode in ("search", "disabled"):
        row = next(row for row in reversed(rows) if row["run"] == mode)
        assert row["context"] and row["project"] and row["placeholder"], (client, mode)
        assert (search_tool in row["tools"]) == (mode == "search"), (client, mode)
        if client == "claude":
            assert ("WebFetch" in row["tools"]) == (mode == "search")
    assert any(row["clicked"] for row in rows if row["run"] == "browser"), client
    print(client, "context, search preference, browser tool result and placeholder verified")

events = Path("/tmp/events-agy").read_text().splitlines()
assert any(all(marker in line for marker in ("context True", "project True", "placeholder True", "clicked True")) for line in events)
assert any("search_denied True" in line for line in events)
print("Antigravity context, placeholder, browser tool result and native search denial verified")
