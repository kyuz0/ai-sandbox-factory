"""Keep worker TCP listeners tied to the existing fixed-purpose Unix relays."""
import os
from pathlib import Path
import signal
import subprocess
import time

children = {}
relays = {}
if os.environ.get("SANDBOX_NETWORK_MODE") == "allowlist":
    relays[3128] = "/run/egress/proxy.sock"
if Path("/run/aws-access").is_dir():
    relays[19432] = "/run/aws-access/request.sock"
if Path('/run/primary-editor/auth.sock').exists():
    relays[3130] = '/run/primary-editor/auth.sock'


def stop(_signal, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while True:
        for port, socket in relays.items():
            if port not in children or children[port].poll() is not None:
                children[port] = subprocess.Popen([
                    "socat", f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork", f"UNIX-CONNECT:{socket}",
                ])
        Path("/tmp/factory-workspace-ready").touch(exist_ok=True)
        time.sleep(1)
finally:
    for child in children.values():
        if child.poll() is None:
            child.terminate()
    for child in children.values():
        child.wait()
