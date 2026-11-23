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
umask 077
mkdir -p "$HOME/.local/share/keyrings" "$CODEX_HOME" /run/advisor /run/advisor-bus
chmod 700 /run/advisor /run/advisor-bus
rm -f /run/advisor-bus/bus
# A container restart has terminated every previous advisor process.
# Interrupted ledger entries remain unresolved until /advisor recover.
rm -f "$HOME/.pi/agent/advisor/active.lock"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --nofork --nopidfile &
bus_pid=$!
# This keyring belongs only to this companion; no host session bus is mounted.
# The private volume is the protection boundary, not an unattended password.
printf '\n' | gnome-keyring-daemon --unlock --components=secrets --control-directory=/run/advisor-bus/keyring
relay TCP-LISTEN:3128,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/egress/proxy.sock &
relay_pid=$!
node /opt/advisor/scripts/server.mjs &
server_pid=$!
trap 'kill "$server_pid" "$relay_pid" "$bus_pid" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
wait -n "$server_pid" "$relay_pid" "$bus_pid"
exit 1
