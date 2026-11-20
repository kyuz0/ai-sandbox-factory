#!/bin/sh
# Live account smoke test. Uses the native accounts on this Linux machine.
set -eu
: "${DEV_IMAGE:?Set DEV_IMAGE to a local Factory dev image with Python and socat}"
: "${CODEX_BIN:?Set CODEX_BIN to the native Linux Codex executable}"
proof=$(realpath research/primary-agents/subscription)
umask 077
dir=$(mktemp -d /tmp/psf-subscription.XXXXXXXX)
cid=''
cleanup() {
    if [ -n "$cid" ]; then podman rm -f -t 1 "$cid" >/dev/null; fi
    rm -rf -- "$dir"
}
trap cleanup EXIT
mkdir "$dir/work" "$dir/agy-home"
cid=$(podman run --pull=never --network=none --cap-drop=all --security-opt=no-new-privileges \
    --security-opt=label=disable --userns=keep-id:uid=1000,gid=1000 --user 1000:1000 \
    --tmpfs /workspace:mode=1777 --tmpfs "$dir/work:mode=1777" \
    -v "$CODEX_BIN:/usr/local/bin/codex:ro" -v "$proof:/proof:ro" \
    -d --entrypoint sleep "$DEV_IMAGE" infinity)
printf '%s' "$cid" > "$dir/cid"
podman exec "$cid" python3 -c 'from pathlib import Path; import secrets; Path("/workspace/marker").write_text("factory-"+secrets.token_hex(10))'
podman exec "$cid" sh -ec 'test ! -e "$HOME/.codex/auth.json"; test ! -e "$HOME/.claude/.credentials.json"; test -z "${DBUS_SESSION_BUS_ADDRESS:-}"'
podman exec -d "$cid" codex exec-server --listen ws://127.0.0.1:36781
for client in codex claude agy; do
    if [ "$client" = codex ]; then set -- --edit; else set --; fi
    python3 "$proof/check-live.py" "$client" "$dir" "$@"
done
