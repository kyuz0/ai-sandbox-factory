"""Coordinate lifecycle commands and shared image builds across Factory processes."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import stat
from threading import local
import time

from .host_platform import private_runtime_dir

_held = local()
_HANDOFF = 'AI_SANDBOX_FACTORY_OPERATION_LOCK'
# Only trusted Factory subprocesses receive this descriptor. Consume the handoff
# before any engine/agent command can inherit its environment, and restore CLOEXEC.
_inherited = os.environ.pop(_HANDOFF, None)
if _inherited is not None:
    try:
        _inherited = json.loads(_inherited)
        if (not isinstance(_inherited, list) or len(_inherited) != 2
                or not isinstance(_inherited[0], str) or type(_inherited[1]) is not int):
            raise ValueError('expected a sandbox name and file descriptor')
        os.set_inheritable(_inherited[1], False)
    except (ValueError, TypeError, OSError) as error:
        raise ValueError(f'Cannot inherit the Factory operation lock: {error}. Restart the Factory command.') from error


def _path(key):
    return private_runtime_dir() / ('operation-' + hashlib.sha256(key.encode()).hexdigest() + '.lock')


def _inherited_sandbox(name):
    if _inherited is None or _inherited[0] != name:
        return None
    fd = _inherited[1]
    path = _path('sandbox:' + name)
    try:
        opened, expected = os.fstat(fd), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(expected.st_mode)
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError('the inherited descriptor does not refer to the sandbox lock file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ValueError, OSError) as error:
        raise ValueError(f'Cannot inherit the Factory operation lock for {name}: {error}. '
                         'Restart the Factory command.') from error
    return fd


def factory_child(name):
    """Pass a held lifecycle lock only to a trusted Factory subprocess."""
    fd = getattr(_held, 'sandboxes', {}).get(name)
    if fd is None:
        fd = _inherited_sandbox(name)
    if fd is None:
        return {}
    return {'pass_fds': (fd,), 'env': {**os.environ, _HANDOFF: json.dumps([name, fd])}}


def in_use(name):
    path = _path('sandbox:' + name)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True
    return False


@contextmanager
def _lock(key, *, busy, wait=False, cancelled=None):
    # Keep a stable inode outside state directories that Delete can remove.
    path = _path(key)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'rb') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                raise ValueError(busy) from None
            print(busy, flush=True)
            if cancelled is None:
                fcntl.flock(file, fcntl.LOCK_EX)
            else:
                from . import updating
                while True:
                    if cancelled():
                        raise updating.UpdateCancelled()
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.1)
        yield file.fileno()


@contextmanager
def sandbox(name, action):
    held = getattr(_held, 'sandboxes', {})
    if name in held:
        yield
        return
    inherited = _inherited_sandbox(name)
    if inherited is not None:
        held[name] = inherited
        _held.sandboxes = held
        try:
            yield
        finally:
            del held[name]
        return
    with _lock('sandbox:' + name, busy=f'Cannot {action} {name}: another Factory operation is using this sandbox. '
                                     'Wait for it to finish or check Jobs in the terminal that started it.') as fd:
        held[name] = fd
        _held.sandboxes = held
        try:
            yield
        finally:
            del held[name]


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


@contextmanager
def image_artifact(engine, reference, cancelled=None):
    with _lock(f'artifact:{engine}:{reference}', busy='Waiting for another image download to finish…',
               wait=True, cancelled=cancelled):
        yield


def proxy_image(engine):
    return _lock(f'proxy-image:{engine}', busy='Waiting for the shared network gateway image…', wait=True)
