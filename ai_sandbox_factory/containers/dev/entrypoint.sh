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
install -d -m 700 /var/tmp/pi
rm -f /var/tmp/pi/pi-sandbox-factory-ready
python3 /usr/local/lib/sandbox-bootstrap.py
relay TCP-LISTEN:19431,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/inference/proxy.sock &
inference_pid=$!
if [ "${SANDBOX_NETWORK_MODE:-allowlist}" = unrestricted ]; then
    sleep infinity &
else
    relay TCP-LISTEN:3128,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/egress/proxy.sock &
fi
network_pid=$!
pids=("$inference_pid" "$network_pid")
if [[ -d /run/aws-access ]]; then
    relay TCP-LISTEN:19432,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/run/aws-access/request.sock &
    pids+=("$!")
fi
trap 'kill "${pids[@]}" 2>/dev/null || true; wait || true' EXIT
trap 'exit 0' TERM INT
touch /var/tmp/pi/pi-sandbox-factory-ready
wait -n "${pids[@]}"
exit 1
