"""Supervise only the profile LAN's fixed external-proxy route."""
import signal
import subprocess
import time

child = None

def stop(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while True:
        child = subprocess.Popen(['socat', 'TCP-LISTEN:3128,bind=0.0.0.0,reuseaddr,fork',
                                  'UNIX-CONNECT:/run/egress/proxy.sock'])
        child.wait()
        time.sleep(1)
finally:
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait()
