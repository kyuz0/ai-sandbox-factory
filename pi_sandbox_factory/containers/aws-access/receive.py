"""Pass a renewed session to the signer within its private network namespace."""
import socket
import sys
import time

ADDRESS = '\0pi-factory-aws-session'


def deliver(stream):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        # The engine can accept exec requests while PID 1 is initializing.
        while True:
            try:
                channel.connect(ADDRESS)
                break
            except ConnectionRefusedError:
                time.sleep(0.1)
        channel.sendall(stream.read())
        channel.shutdown(socket.SHUT_WR)
        if channel.recv(1) != b'1':
            raise RuntimeError('The signer did not accept the refreshed session.')


if __name__ == '__main__':
    deliver(sys.stdin.buffer)
