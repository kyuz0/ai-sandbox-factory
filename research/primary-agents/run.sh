#!/bin/sh
# Run from the checkout root. The only host mounts are these fixtures and binaries.
set -eu
: "${DEV_IMAGE:?Set DEV_IMAGE to a local Factory development image with the browser installed}"
: "${CODEX_BIN:?Set CODEX_BIN to the native Codex executable}"
: "${CLAUDE_BIN:?Set CLAUDE_BIN to the native Claude Code executable}"
: "${AGY_BIN:?Set AGY_BIN to the native Antigravity executable}"
proof=$(realpath research/primary-agents)
cid=$(podman run -d --pull=never --network=none --cap-drop=ALL --security-opt=no-new-privileges \
    --security-opt=label=disable --userns=keep-id:uid=1000,gid=1000 --user 1000:1000 \
    --tmpfs /workspace:mode=1777 -w /workspace -v "$proof:/proof:ro" \
    -v "$CODEX_BIN:/usr/local/bin/codex:ro" -v "$CLAUDE_BIN:/usr/local/bin/claude:ro" \
    -v "$AGY_BIN:/usr/local/bin/agy:ro" --entrypoint sleep "$DEV_IMAGE" infinity)
trap 'podman rm -f "$cid" >/dev/null' EXIT
python3 -c 'import sys; from pi_sandbox_factory.browser_viewer import ensure_viewer; ensure_viewer("podman", sys.argv[1])' "$cid"
inside() { podman exec "$cid" "$@"; }
podman exec -d "$cid" python3 /proof/model-fixture.py
podman exec -d "$cid" sh -c 'exec python3 /proof/gemini-fixture.py >/tmp/events-agy'
inside sh -c 'mkdir -p "$HOME/.gemini"; echo FACTORY_CONTEXT_701 > "$HOME/.gemini/AGENTS.md"'
inside agy mcp add factory node /proof/browser-fixture.mjs agy
google_turn() {
    inside env GEMINI_API_KEY=fixture-placeholder GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:18783 \
        agy --settings '{"modelProvider":"gemini","permissions":{"allow":["mcp(factory/view_fixture)"]}}' --print='Run the fixture.'
}
for engine in codex claude agy; do
    if [ "$engine" = agy ]; then
        google_turn &
    else
        inside sh /proof/fixture-turns.sh "$engine" browser &
    fi
    turn_pid=$!
    python3 "$proof/check-viewer.py" "$cid" "$engine"
    wait "$turn_pid"
    if [ "$engine" != agy ]; then
        inside sh /proof/fixture-turns.sh "$engine" search
        inside sh /proof/fixture-turns.sh "$engine" disabled
    fi
done
inside sh -c 'mkdir -p "$HOME/.gemini/config"; cp /proof/agy-hooks.json "$HOME/.gemini/config/hooks.json"; rm -f /tmp/asked-agy; touch /tmp/request-search-agy'
google_turn
inside python3 /proof/check-turns.py
