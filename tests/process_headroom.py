#!/usr/bin/env python3
"""Run inside a disposable Factory image with --pids-limit=512 or 2048."""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

expected = sys.argv[1]
stop = threading.Event()
backend = socket.socket(socket.AF_UNIX)
backend.bind('/tmp/headroom.sock')
backend.listen()

def respond():
    connection, _ = backend.accept()
    with connection:
        connection.sendall(b'RELAY_OK')

threading.Thread(target=respond, daemon=True).start()
relay = subprocess.Popen(['socat', 'TCP-LISTEN:19431,bind=127.0.0.1,reuseaddr,fork',
                          'UNIX-CONNECT:/tmp/headroom.sock'], stderr=subprocess.PIPE)
try:
    # Establish readiness without a connection that consumes the backend fixture.
    for _ in range(100):
        if ':4BE7 ' in Path('/proc/net/tcp').read_text():
            break
        time.sleep(.02)
    else:
        raise AssertionError('Relay failed to listen')
    count = 0
    for _ in range(600):
        try:
            threading.Thread(target=stop.wait, daemon=True).start()
            count += 1
        except RuntimeError:
            break
    reply = b''
    try:
        with socket.create_connection(('127.0.0.1', 19431), timeout=5) as client:
            reply = client.recv(100)
    except (ConnectionError, TimeoutError):
        pass
    if expected == 'exhausted':
        assert relay.wait(timeout=5) != 0
        assert b'fork(): Resource temporarily unavailable' in relay.stderr.read()
        assert count < 600
        print('PASS 512: reproduced relay exit on PID exhaustion')
    else:
        assert count == 600 and reply == b'RELAY_OK' and relay.poll() is None
        print('PASS 2048: 600 worker threads and relay remain operational')
finally:
    stop.set()
    if relay.poll() is None:
        relay.terminate()
    relay.wait(timeout=5)
    backend.close()
