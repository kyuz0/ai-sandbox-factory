"""Deliver credentials on stdin through the companion's private network namespace."""
import os
import socket
import sys

# The broker and its engine-exec helper share this container-private tmpfs. A FIFO
# synchronizes socket setup without polling or a fixed startup deadline.
READY = '/dev/shm/pi-factory-connector-ready'
ADDRESS = '\0pi-factory-connector-control'


def readiness_fifo():
    try:
        os.mkfifo(READY, 0o600)
    except FileExistsError:
        pass
    return READY


def deliver(payload, *, starting=False):
    if starting:
        with open(readiness_fifo(), 'rb', buffering=0) as ready:
            if ready.read(1) != b'1':
                raise ValueError('The connector exited during initialization.')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(ADDRESS)
        channel.sendall(payload)
        channel.shutdown(socket.SHUT_WR)
        if channel.recv(1) != b'1':
            raise ValueError('The connector did not accept the settings.')


if __name__ == '__main__':
    deliver(sys.stdin.buffer.read(), starting='--starting' in sys.argv[1:])
