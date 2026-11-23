#!/bin/bash
set -euo pipefail
socket_dir=/run/primary-workspace
mkdir -p "$socket_dir"
umask 077
exec 9>"$socket_dir/worker.lock"
flock -n 9 || exit 0
# The lock owner is the only process allowed to publish socket paths.
rm -f "$socket_dir"/exec.sock "$socket_dir"/tools.sock
children=()
trap 'if (("${#children[@]}")); then kill "${children[@]}" 2>/dev/null || true; wait || true; fi' EXIT
trap 'exit 0' TERM INT
if [[ "$1" != codex || -f /opt/betterwright/browser.mjs ]]; then
    export FACTORY_PRIMARY_AGENT="$1"
    socat "UNIX-LISTEN:$socket_dir/tools.sock,fork,mode=0600" \
        'EXEC:node /opt/primary/worker-tools.mjs,pipes' 9>&- &
    children+=("$!")
fi
wait -n "${children[@]}"
