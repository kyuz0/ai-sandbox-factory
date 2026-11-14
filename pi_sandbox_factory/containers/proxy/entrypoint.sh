#!/bin/bash
set -euo pipefail

# A relay failure must not terminate the editor or other active work.
# Retry only this fixed route; no direct-network fallback is introduced.
relay() {
    exec python3 - "$@" <<'PYTHON'
import signal
import subprocess
import sys
import time

child = None
def stop(*_):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while True:
        try:
            child = subprocess.Popen(['socat', *sys.argv[1:]])
            child.wait()
        except OSError as exc:
            print(f'Relay restart waiting: {exc}', file=sys.stderr, flush=True)
        time.sleep(1)
finally:
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait()
PYTHON
}
# Podman Machine resolves this name through its host networking service.
if [[ "${SANDBOX_MODEL_HOST:-}" == host.containers.internal ]]; then
    model_address=$(getent ahostsv4 host.containers.internal | awk 'NR==1 {print $1}') || model_address=
    if [[ ! "$model_address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo 'Cannot resolve host.containers.internal; check Podman Machine host networking.' >&2
        exit 1
    fi
    printf '%s local-model.internal\n' "$model_address" >> /etc/hosts
fi
# Only this container can change the socket directory. Clients mount it read-only.
chmod 755 /run/egress
rm -f /run/egress/proxy.sock
squid -N -f /etc/squid/sandbox.conf &
squid_pid=$!
relay UNIX-LISTEN:/run/egress/proxy.sock,fork,unlink-early,mode=0666 TCP:127.0.0.1:3128 &
relay_pid=$!
trap 'kill "$squid_pid" "$relay_pid" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
# Squid failure stops the gateway; relay failures retry the same fixed route.
wait -n "$squid_pid" "$relay_pid"
exit 1
