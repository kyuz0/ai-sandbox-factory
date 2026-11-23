"""Preserve native Codex chats when moving its client into the workspace.

The pinned Codex 0.171.0 stores paginated history in thread_history_1.sqlite
and listing metadata in state_5.sqlite. Rollouts alone may contain only a suffix.
This module also runs as a standalone helper in each container.
"""
from contextlib import closing
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile

DATABASES = ('state_5.sqlite', 'thread_history_1.sqlite')
METADATA = ('session_index.jsonl', 'history.jsonl')
ROOTS = ('sessions', 'archived_sessions')
MARKER = '.factory-private-history-imported'


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        return False
    return name in DATABASES + METADATA or (
        len(path.parts) > 1 and path.parts[0] in ROOTS
        and (name.endswith('.jsonl') or name.endswith('.jsonl.zst')))


def regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Do not export links to files outside the selected history tree.
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def export_history(home, stream):
    """Write only native chat files; leave the private home untouched."""
    home = Path(home)
    if home.is_symlink():
        raise ValueError(f'Cannot preserve Codex chats from symbolic-link home {home}. Use a regular private home directory.')
    with tempfile.TemporaryDirectory(prefix='factory-codex-history-') as temporary:
        files = []
        for root in ROOTS:
            directory = home / root
            if directory.is_symlink():
                continue
            for parent, directories, names in os.walk(directory, followlinks=False):
                directories[:] = sorted(name for name in directories if not (Path(parent) / name).is_symlink())
                for name in sorted(names):
                    source = Path(parent) / name
                    relative = source.relative_to(home).as_posix()
                    if allowed(relative) and regular(source):
                        files.append((relative, source))
        for name in METADATA:
            if regular(home / name):
                files.append((name, home / name))
        for name in DATABASES:
            source = home / name
            if regular(source):
                snapshot = Path(temporary) / name
                # Backup includes committed WAL pages without copying locks or
                # changing the private database's journal/checkpoint settings.
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(snapshot)) as target:
                        original.backup(target)
                files.append((name, snapshot))
        with tarfile.open(fileobj=stream, mode='w|') as archive:
            for name, source in files:
                with source.open('rb') as content:
                    member = tarfile.TarInfo(name)
                    member.size = os.fstat(content.fileno()).st_size
                    member.mode = 0o600
                    archive.addfile(member, content)
        return len(files)


def safe_target(home, name):
    target = home
    for part in PurePosixPath(name).parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f'Cannot restore Codex chat file {name}: destination {target} is a symbolic link. Keep both homes and remove the conflicting link before retrying.')
    return target


def restore_history(home, stream):
    """Preflight the complete transfer, then install without replacing files."""
    home = Path(home)
    if home.is_symlink():
        raise ValueError(f'Cannot restore Codex chats into symbolic-link home {home}. Use a regular workspace home directory.')
    # Spool outside the destination so malformed transfers cannot leave files there.
    with tempfile.TemporaryDirectory(prefix='factory-codex-restore-') as temporary:
        stage = Path(temporary)
        incoming = []
        seen = set()
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                if not member.isfile() or not allowed(member.name) or member.name in seen:
                    raise ValueError(f'Cannot restore Codex chats: rejected archive entry {member.name!r}. Only regular native chat files are accepted; the original chats are unchanged.')
                seen.add(member.name)
                target = stage / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                incoming.append((member.name, target))
        if not incoming:
            return 0
        if (home / MARKER).exists():
            return 0
        pending = []
        for name, source in incoming:
            target = safe_target(home, name)
            if target.exists():
                if not regular(target) or not same_content(source, target):
                    raise ValueError(f'Cannot preserve Codex chats: workspace file {target} already contains different data. Both chat homes were kept unchanged. Back up both homes and resolve this conflict before retrying the update.')
            else:
                pending.append((source, target))
        installed = []
        created = []
        try:
            for source, target in pending:
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir(mode=0o700)
                    created.append(directory)
                # Stage on the destination filesystem and hard-link atomically:
                # an unexpected concurrent file is never overwritten.
                fd, name = tempfile.mkstemp(prefix='.factory-chat-', dir=target.parent)
                try:
                    with os.fdopen(fd, 'wb') as output, source.open('rb') as content:
                        shutil.copyfileobj(content, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.link(name, target)
                    installed.append(target)
                finally:
                    Path(name).unlink(missing_ok=True)
            marker = stage / MARKER
            marker.write_text('Codex 0.171.0 native chat files preserved.\n')
            fd, name = tempfile.mkstemp(prefix='.factory-chat-', dir=home)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(marker.read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                os.link(name, home / MARKER)
            finally:
                Path(name).unlink(missing_ok=True)
        except BaseException:
            for target in reversed(installed):
                target.unlink()
            for directory in reversed(created):
                directory.rmdir()
            raise
        return len(installed)


def same_content(left, right):
    with left.open('rb') as first, right.open('rb') as second:
        while True:
            a, b = first.read(io.DEFAULT_BUFFER_SIZE), second.read(io.DEFAULT_BUFFER_SIZE)
            if a != b:
                return False
            if not a:
                return True


def migrate(config, workspace_container):
    """Transfer chats through the trusted host before starting workspace Codex."""
    from . import primary_agents
    controller = primary_agents.names(config)[0]
    engine = config['engine']
    checked = subprocess.run([engine, 'exec', workspace_container, 'test', '-f',
                              '/home/node/.codex/' + MARKER],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if checked.returncode == 0:
        return 'Codex chats were already preserved.'
    if checked.returncode != 1:
        raise ValueError(f'Cannot check preserved Codex chats in {workspace_container} '
                         f'(exit {checked.returncode}): '
                         f'{checked.stderr.decode(errors="replace").strip()}. '
                         'Check the workspace container and retry the update.')
    script = Path(__file__).read_text()
    # Complete export before import: an exporter failure cannot install a partial
    # history or completion marker. The private spool is anonymous host storage.
    with tempfile.TemporaryFile() as transfer:
        exported = subprocess.run([engine, 'exec', controller, 'python3', '-c', script, 'export'],
                                  stdout=transfer, stderr=subprocess.PIPE)
        if exported.returncode:
            raise ValueError(f'Cannot preserve Codex chats from {controller} (export exit {exported.returncode}): '
                             f'{exported.stderr.decode(errors="replace").strip()}. The private source home was retained; resolve the error and retry the update.')
        transfer.seek(0)
        restored = subprocess.run([engine, 'exec', '-i', workspace_container, 'python3', '-c', script, 'restore'],
                                  stdin=transfer, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if restored.returncode:
        raise ValueError(f'Cannot preserve Codex chats in {workspace_container} (restore exit {restored.returncode}): '
                         f'{restored.stderr.decode(errors="replace").strip()}. The private source home was retained; resolve the error and retry the update.')
    return restored.stdout.decode().strip()


if __name__ == '__main__':
    try:
        private_home = Path.home() / '.codex'
        if sys.argv[1:] == ['export']:
            export_history(private_home, sys.stdout.buffer)
        elif sys.argv[1:] == ['restore']:
            count = restore_history(private_home, sys.stdin.buffer)
            print(f'Preserved {count} Codex chat files.' if count else 'No Codex chat transfer needed.')
        else:
            raise ValueError('Choose the Codex history helper operation export or restore.')
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        sys.exit(f'{type(error).__name__}: {error}')
