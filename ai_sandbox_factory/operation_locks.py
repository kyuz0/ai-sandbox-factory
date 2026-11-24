"""Coordinate lifecycle commands and shared image builds across Factory processes."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from threading import local

from .host_platform import private_runtime_dir

_held = local()


@contextmanager
def _lock(key, *, busy, wait=False):
    # Keep a stable inode outside state directories that Delete can remove.
    path = private_runtime_dir() / ('operation-' + hashlib.sha256(key.encode()).hexdigest() + '.lock')
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'rb') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise ValueError(busy) from None
            print(busy, flush=True)
            fcntl.flock(file, fcntl.LOCK_EX)
        yield


def sandbox(name, action):
    return _lock('sandbox:' + name, busy=f'Cannot {action} {name}: another Factory operation is using this sandbox. '
                                        'Wait for it to finish or check Jobs in the terminal that started it.')


@contextmanager
def image_graph(engine, key):
    # Recreate also holds the graph through startup, so another rebuild cannot
    # retag its new image between the build and container creation.
    key = f'images:{engine}:{key}'
    if getattr(_held, 'image_key', None) == key:
        yield
        return
    with _lock(key, busy='Waiting for another build to finish using the same image layers…', wait=True):
        _held.image_key = key
        try:
            yield
        finally:
            _held.image_key = None


def proxy_image(engine):
    return _lock(f'proxy-image:{engine}', busy='Waiting for the shared network gateway image…', wait=True)
