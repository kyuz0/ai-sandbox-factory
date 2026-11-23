"""Read and restore Pi transcripts inside the sandbox home volume."""

from contextlib import ExitStack, contextmanager, nullcontext
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


FORMAT = b'pi-sandbox-factory-chat-history-v1'
EMPTY = 'There are no saved chats to export.'


@contextmanager
def directory(name, parent=None, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def sessions(home, *, create=False):
    with directory(home) as root, directory('.pi', root, create=create) as pi:
        with directory('agent', pi, create=create) as agent:
            with directory('sessions', agent, create=create) as history:
                yield history


def group_name(name):
    return safe_name(name) and name.startswith('--') and name.endswith('--') and len(name) > 4


def safe_name(name):
    return bool(name) and '/' not in name and '\\' not in name and all(c.isprintable() for c in name)


@contextmanager
def transcript(name, folder):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('A saved chat is not a regular file.')
        yield source


def saved_chats(root):
    for group in sorted(os.listdir(root)):
        if not group_name(group):
            continue
        with directory(group, root) as folder:
            for name in sorted(os.listdir(folder)):
                if not (safe_name(name) and name.endswith('.jsonl')):
                    continue
                with transcript(name, folder) as source:
                    yield group, name, source


def header(source):
    line = source.readline()
    try:
        entry = json.loads(line)
    except (ValueError, UnicodeError) as error:
        raise ValueError('A chat file has an invalid Pi session header.') from error
    if (not isinstance(entry, dict) or entry.get('type') != 'session'
            or not isinstance(entry.get('id'), str) or not entry['id']
            or not isinstance(entry.get('cwd'), str)):
        raise ValueError('A chat file has an invalid Pi session header.')
    if entry.get('version', 1) not in (1, 2, 3):
        raise ValueError('This chat uses an unsupported Pi session version.')
    return entry, line


def export_archive(home, output):
    count = 0
    with ExitStack() as stack:
        try:
            root = stack.enter_context(sessions(home))
        except FileNotFoundError:
            raise ValueError(EMPTY) from None
        try:
            archive = stack.enter_context(ZipFile(output, 'w', compression=ZIP_DEFLATED))
            archive.comment = FORMAT
            for group, name, source in saved_chats(root):
                _, first_line = header(source)
                with archive.open(f'sessions/{group}/{name}', 'w', force_zip64=True) as target:
                    target.write(first_line)
                    shutil.copyfileobj(source, target)
                count += 1
        except FileNotFoundError:
            raise ValueError('Chat history changed during export. Try again after Pi finishes.') from None
    if not count:
        raise ValueError(EMPTY)


def archive_members(archive):
    if archive.comment != FORMAT:
        raise ValueError('Choose a chat backup exported by AI Sandbox Factory.')
    members = archive.infolist()
    paths = set()
    for member in members:
        parts = member.filename.split('/')
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (len(parts) != 3 or parts[0] != 'sessions' or not group_name(parts[1])
                or not safe_name(parts[2]) or not parts[2].endswith('.jsonl')
                or mode not in (0, stat.S_IFREG) or member.filename in paths):
            raise ValueError('The chat backup contains an invalid file entry.')
        paths.add(member.filename)
    if not members:
        raise ValueError('The backup contains no chats.')
    return members


def install_chats(root, group, cwd, sources):
    # Serialize restores. The final link prevents replacing a live Pi file.
    fcntl.flock(root, fcntl.LOCK_EX)
    existing = {header(source)[0]['id'] for _, _, source in saved_chats(root)}
    staged, published = {}, []
    skipped = 0
    with directory(group, root, create=True) as destination:
        try:
            for open_source in sources:
                with open_source() as source:
                    entry, _ = header(source)
                    session_id = entry['id']
                    if session_id in existing:
                        skipped += 1
                        # Read to the end so a corrupt ZIP entry still fails validation.
                        while source.read(1024 * 1024):
                            pass
                        continue
                    existing.add(session_id)
                    digest = hashlib.sha256(session_id.encode()).hexdigest()
                    filename = f'imported-{digest}.jsonl'
                    temporary = f'.import-{uuid4().hex}'
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination)
                    staged[filename] = temporary
                    entry['cwd'] = cwd
                    with os.fdopen(fd, 'wb') as target:
                        target.write((json.dumps(entry) + '\n').encode())
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
            for filename, temporary in staged.items():
                os.link(temporary, filename, src_dir_fd=destination, dst_dir_fd=destination)
                published.append(filename)
        except BaseException:
            for filename in published:
                os.unlink(filename, dir_fd=destination)
            raise
        finally:
            for temporary in staged.values():
                os.unlink(temporary, dir_fd=destination)
    return len(published), skipped


def import_archive(home, incoming, kind, cwd):
    group = '--' + cwd.lstrip('/').replace('/', '-').replace('\\', '-').replace(':', '-') + '--'
    with sessions(home, create=True) as root:
        if kind == 'jsonl':
            return install_chats(root, group, cwd, [lambda: nullcontext(incoming)])
        # ZIP's central directory is at the end. Spool on the volume rather than in RAM.
        name = f'.received-{uuid4().hex}'
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
        with os.fdopen(fd, 'w+b') as spool:
            os.unlink(name, dir_fd=root)
            shutil.copyfileobj(incoming, spool)
            with ZipFile(spool) as archive:
                return install_chats(root, group, cwd,
                                     [lambda member=member: archive.open(member) for member in archive_members(archive)])


def main():
    mode, home, *args = sys.argv[1:]
    try:
        if mode == 'export':
            export_archive(home, sys.stdout.buffer)
        else:
            added, skipped = import_archive(home, sys.stdin.buffer, *args)
            print(json.dumps({'added': added, 'skipped': skipped}))
    except (BadZipFile, RuntimeError, ValueError, OSError) as error:
        if isinstance(error, BadZipFile):
            message = 'The chat backup is incomplete or damaged.'
        elif isinstance(error, OSError):
            message = ('The sandbox storage is full.' if error.errno == errno.ENOSPC
                       else 'Could not access the sandbox chat files.')
        else:
            message = str(error)
        sys.exit(message)


if __name__ == '__main__':
    main()
