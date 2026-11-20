#!/bin/sh
# Run in a disposable container with this directory mounted read-only at /proof.
set -eu
cd /workspace
mkdir -p "$HOME/.codex" "$HOME/.claude"
printf '%s\n' PROJECT_CONTEXT_702 > AGENTS.md
printf '%s\n' PROJECT_CONTEXT_702 > CLAUDE.md
printf '%s\n' FACTORY_CONTEXT_701 > /tmp/context.txt
git init -q .
export OPENAI_API_KEY=fixture-placeholder ANTHROPIC_API_KEY=fixture-placeholder
export ANTHROPIC_BASE_URL=http://127.0.0.1:18782 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 ENABLE_TOOL_SEARCH=false
cat > "$HOME/.codex/config.toml" <<'TOML'
model_provider = "fixture"
model = "gpt-5.4"
developer_instructions = "FACTORY_CONTEXT_701"
[model_providers.fixture]
name = "Offline fixture"
base_url = "http://127.0.0.1:18782/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
[mcp_servers.factory]
command = "node"
args = ["/proof/browser-fixture.mjs", "codex"]
[mcp_servers.factory.tools.view_fixture]
approval_mode = "approve"
[projects."/workspace"]
trust_level = "trusted"
TOML
printf '%s\n' '{"mcpServers":{"factory":{"command":"node","args":["/proof/browser-fixture.mjs","claude"]}}}' > /tmp/fixture-mcp.json
engine=$1; mode=$2
rm -f "/tmp/asked-$engine" "/tmp/view-$engine" "/tmp/checked-$engine"
printf '%s\n' "$mode" > "/tmp/run-$engine"
if [ "$engine" = codex ]; then
    search=disabled; [ "$mode" != search ] || search=live
    exec codex exec --ephemeral -c "web_search=\"$search\"" -c 'features.tool_search=false' --json 'Open the local browser fixture if requested.'
fi
if [ "$mode" = disabled ]; then set -- --disallowedTools WebSearch WebFetch; else set --; fi
exec claude -p --model sonnet --no-session-persistence --setting-sources user,project --strict-mcp-config --mcp-config /tmp/fixture-mcp.json --append-system-prompt-file /tmp/context.txt --allowedTools mcp__factory__view_fixture "$@" --output-format json -- 'Open the local browser fixture if requested.'
