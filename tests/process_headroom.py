#!/usr/bin/env python3
"""Run inside a disposable Factory image with an exhausted or unrestricted PID budget."""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

expected = sys.argv[1]
target = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
stop = threading.Event()
backend = socket.socket(socket.AF_UNIX)
backend.bind('/tmp/headroom.sock')
backend.listen()

def respond():
    while not stop.is_set():
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
    for _ in range(target):
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
        assert count < target
        print('PASS 512: reproduced relay exit on PID exhaustion')
    else:
        assert count == target and reply == b'RELAY_OK' and relay.poll() is None
        deadline = time.monotonic() + 30
        requests = 0
        while time.monotonic() < deadline:
            with socket.create_connection(('127.0.0.1', 19431), timeout=5) as client:
                assert client.recv(100) == b'RELAY_OK'
            requests += 1
            time.sleep(.05)
        print(f'PASS: {count} worker threads sustained for 30 seconds; {requests} relay requests completed')
finally:
    stop.set()
    if relay.poll() is None:
        relay.terminate()
    relay.wait(timeout=5)
    backend.close()
