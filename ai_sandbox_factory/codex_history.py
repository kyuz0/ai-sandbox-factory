"""Preserve native chats when moving coding clients into the workspace.

The pinned Codex 0.171.0 stores paginated history in thread_history_1.sqlite
and listing metadata in state_5.sqlite. Rollouts alone may contain only a suffix.
This module also runs as a standalone helper in each container.
"""
from contextlib import closing
import io
import json
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
AGY_APPS = ('antigravity', 'antigravity-cli')
AGY_HISTORY_DIRS = ('conversations', 'implicit', 'annotations', 'brain',
                    'browser_recordings', 'html_artifacts', 'context_state')
AGY_HISTORY_FILES = ('conversation_summaries.db', 'jetbox_summaries_proto.pb', 'agyhub_summaries_proto.pb')
HOMES = {'codex': '.codex', 'claude': '.claude', 'agy': '.gemini'}


def allowed(name, provider='codex'):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name
            or not all(character.isprintable() for character in name)):
        return False
    if provider == 'claude':
        return (len(path.parts) > 2 and path.parts[0] == 'projects'
                and (name.endswith('.jsonl') or path.name == 'sessions-index.json'))
    if provider == 'agy':
        # Main SQLite files are backed up with committed WAL pages. Transferring
        # the live sidecars alongside that snapshot could overwrite its state.
        if path.name.endswith(('.db-wal', '.db-shm', '.db-journal')):
            return False
        return (len(path.parts) > 1 and path.parts[0] in AGY_APPS and
                (len(path.parts) == 2 and path.parts[1] in AGY_HISTORY_FILES or
                 len(path.parts) > 2 and path.parts[1] in AGY_HISTORY_DIRS))
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


def contains_secret(path, values):
    needles = [value.encode() for value in values if value]
    if not needles:
        return False
    overlap = max(map(len, needles)) - 1
    tail = b''
    with path.open('rb') as stream:
        while block := stream.read(io.DEFAULT_BUFFER_SIZE):
            data = tail + block
            if any(needle in data for needle in needles):
                return True
            tail = data[-overlap:] if overlap else b''
    return False


def claude_history(data, credentials):
    """Keep native transcript records, excluding private API diagnostics."""
    secrets = [value for key, value in credentials.get('claudeAiOauth', {}).items()
               if key in ('accessToken', 'refreshToken', 'idToken') and isinstance(value, str) and value]
    records, skipped = [], {}
    for line in data.decode('utf-8').splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get('type') not in ('user', 'assistant', 'summary'):
            if isinstance(record.get('uuid'), str):
                skipped[record['uuid']] = record.get('parentUuid')
            continue
        if (record.get('isApiErrorMessage') or record.get('is_api_error_message') or
                'error' in record or 'apiError' in record or
                any(tag in json.dumps(record.get('message', {})) for tag in
                    ('<local-command-stdout>', '<local-command-stderr>'))):
            record = {**record, 'message': {'role': record['type'], 'content': [
                {'type': 'text', 'text': 'Private command or API diagnostic omitted during chat preservation.'}]}}
            if 'summary' in record:
                record['summary'] = 'Private diagnostic omitted during chat preservation.'
        keys = ('type', 'uuid', 'parentUuid', 'sessionId', 'cwd', 'timestamp', 'message',
                'isSidechain', 'version', 'slug', 'gitBranch', 'userType', 'isMeta', 'summary', 'leafUuid')
        records.append({key: record[key] for key in keys if key in record})
    for record in records:
        parent, seen = record.get('parentUuid'), set()
        while isinstance(parent, str) and parent in skipped and parent not in seen:
            seen.add(parent)
            parent = skipped[parent]
        if 'parentUuid' in record:
            record['parentUuid'] = None if isinstance(parent, str) and parent in seen else parent
    encoded = ''.join(json.dumps(record) + '\n' for record in records)
    for value in secrets:
        encoded = encoded.replace(value, '[redacted]')
        encoded = encoded.replace(json.dumps(value)[1:-1], '[redacted]')
    return encoded.encode()


def export_history(home, stream, provider='codex', known_secrets=(), *, workspace=False):
    """Write only native chat files; leave the private home untouched."""
    home = Path(home)
    if home.is_symlink():
        raise ValueError(f'Cannot preserve native chats from symbolic-link home {home}. Use a regular private home directory.')
    with tempfile.TemporaryDirectory(prefix='factory-codex-history-') as temporary:
        files = []
        roots = {'codex': ROOTS, 'claude': ('projects',), 'agy': AGY_APPS}[provider]
        for root in roots:
            directory = home / root
            if directory.is_symlink():
                continue
            for parent, directories, names in os.walk(directory, followlinks=False):
                directories[:] = sorted(name for name in directories if not (Path(parent) / name).is_symlink())
                for name in sorted(names):
                    source = Path(parent) / name
                    relative = source.relative_to(home).as_posix()
                    if allowed(relative, provider) and regular(source):
                        files.append((relative, source))
        for name in (METADATA if provider == 'codex' else ()):
            if regular(home / name):
                files.append((name, home / name))
        for name in (DATABASES if provider == 'codex' else ()):
            source = home / name
            if regular(source):
                snapshot = Path(temporary) / name
                # Backup includes committed WAL pages without copying locks or
                # changing the private database's journal/checkpoint settings.
                with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(snapshot)) as target:
                        original.backup(target)
                files.append((name, snapshot))
        if provider == 'claude' and not workspace:
            credentials = json.loads((home / '.credentials.json').read_text()) if regular(home / '.credentials.json') else {}
            cleaned = []
            for index, (name, source) in enumerate(files):
                snapshot = Path(temporary) / str(index)
                if name.endswith('.jsonl'):
                    snapshot.write_bytes(claude_history(source.read_bytes(), credentials))
                else:
                    content = json.dumps(json.loads(source.read_text()))
                    for key in ('accessToken', 'refreshToken', 'idToken'):
                        secret = credentials.get('claudeAiOauth', {}).get(key)
                        if isinstance(secret, str) and secret:
                            content = content.replace(json.dumps(secret)[1:-1], '[redacted]')
                    snapshot.write_text(content)
                cleaned.append((name, snapshot))
            files = cleaned
        elif provider == 'agy':
            snapshots = []
            for index, (name, source) in enumerate(files):
                snapshot = Path(temporary) / str(index)
                with source.open('rb') as content:
                    sqlite = content.read(16) == b'SQLite format 3\x00'
                if sqlite:
                    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
                        with closing(sqlite3.connect(snapshot)) as target:
                            original.backup(target)
                else:
                    shutil.copyfile(source, snapshot)
                # Preserve binary formats exactly. A private diagnostic inside
                # a legacy history needs an explicit repair, never a byte patch
                # that could corrupt protobuf lengths or database indexes.
                if contains_secret(snapshot, known_secrets):
                    raise ValueError(f'Cannot preserve Antigravity chat file {name}: it contains a private sign-in token. '
                                     'The private source home was retained. Keep it and have the affected history file sanitized before retrying the update.')
                snapshots.append((name, snapshot))
            files = snapshots
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
            raise ValueError(f'Cannot restore native chat file {name}: destination {target} is a symbolic link. Keep both homes and remove the conflicting link before retrying.')
    return target


def saved_paths(home, provider):
    roots = {'codex': ROOTS, 'claude': ('projects',), 'agy': AGY_APPS}[provider]
    for root in roots:
        for parent, directories, names in os.walk(safe_target(home, root), followlinks=False):
            directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
            for name in names:
                path = Path(parent) / name
                if allowed(path.relative_to(home).as_posix(), provider):
                    yield path
    if provider == 'codex':
        yield from (home / name for name in (*DATABASES, *METADATA) if os.path.lexists(home / name))


def restore_history(home, stream, provider='codex', *, fresh=False):
    """Preflight the complete transfer, then install without replacing files."""
    home = Path(home)
    if home.is_symlink():
        raise ValueError(f'Cannot restore native chats into symbolic-link home {home}. Use a regular workspace home directory.')
    if fresh and next(saved_paths(home, provider), None) is not None:
        raise ValueError('This sandbox already has chat storage. Restore into a new sandbox before opening its coding agent.')
    # Spool outside the destination so malformed transfers cannot leave files there.
    with tempfile.TemporaryDirectory(prefix='factory-codex-restore-') as temporary:
        stage = Path(temporary)
        incoming = []
        seen = set()
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                if not member.isfile() or not allowed(member.name, provider) or member.name in seen:
                    raise ValueError(f'Cannot restore native chats: rejected archive entry {member.name!r}. Only regular native chat files are accepted; the original chats are unchanged.')
                seen.add(member.name)
                target = stage / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                incoming.append((member.name, target))
        if not incoming:
            return 0
        if not fresh and (home / MARKER).exists():
            return 0
        pending = []
        for name, source in incoming:
            target = safe_target(home, name)
            if target.exists():
                if not regular(target) or not same_content(source, target):
                    raise ValueError(f'Cannot preserve native chats: workspace file {target} already contains different data. Both chat homes were kept unchanged. Back up both homes and resolve this conflict before retrying the update.')
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
            if not fresh:
                marker = stage / MARKER
                marker.write_text(provider + ' native chat files preserved.\n')
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


def migrate(config, workspace_container, provider='codex'):
    """Transfer chats through the trusted host before starting a native client."""
    from . import primary_agents
    controller = primary_agents.names(config)[0]
    engine = config['engine']
    home = HOMES[provider]
    checked = subprocess.run([engine, 'exec', workspace_container, 'test', '-f',
                              '/home/node/' + home + '/' + MARKER],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if checked.returncode == 0:
        return {'codex': 'Codex', 'claude': 'Claude Code', 'agy': 'Antigravity'}[provider] + ' chats were already preserved.'
    if checked.returncode != 1:
        raise ValueError(f'Cannot check preserved native chats in {workspace_container} '
                         f'(exit {checked.returncode}): '
                         f'{checked.stderr.decode(errors="replace").strip()}. '
                         'Check the workspace container and retry the update.')
    script = Path(__file__).read_text()
    # Complete export before import: an exporter failure cannot install a partial
    # history or completion marker. The private spool is anonymous host storage.
    with tempfile.TemporaryFile() as transfer:
        exported = subprocess.run([engine, 'exec', controller, 'python3', '-c', script, 'export', provider],
                                  stdout=transfer, stderr=subprocess.PIPE)
        if exported.returncode:
            raise ValueError(f'Cannot preserve native chats from {controller} (export exit {exported.returncode}): '
                             f'{exported.stderr.decode(errors="replace").strip()}. The private source home was retained; resolve the error and retry the update.')
        transfer.seek(0)
        restored = subprocess.run([engine, 'exec', '-i', workspace_container, 'python3', '-c', script, 'restore', provider],
                                  stdin=transfer, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if restored.returncode:
        raise ValueError(f'Cannot preserve native chats in {workspace_container} (restore exit {restored.returncode}): '
                         f'{restored.stderr.decode(errors="replace").strip()}. The private source home was retained; resolve the error and retry the update.')
    return restored.stdout.decode().strip()


if __name__ == '__main__':
    try:
        provider = sys.argv[2] if len(sys.argv) > 2 else 'codex'
        operation = sys.argv[1]
        private_home = (Path(sys.argv[3]) if len(sys.argv) > 3 else Path.home()) / HOMES[provider]
        if operation == 'backup':
            if not export_history(private_home, sys.stdout.buffer, provider, workspace=True):
                raise ValueError('There are no saved chats to export.')
        elif operation == 'import':
            count = restore_history(private_home, sys.stdin.buffer, provider, fresh=True)
            if not count:
                raise ValueError('The backup contains no chats.')
            print(json.dumps({'added': count, 'skipped': 0}))
        elif operation == 'export':
            secrets = []
            if provider == 'agy':
                result = subprocess.run(['secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity'],
                                        capture_output=True, text=True)
                if result.returncode not in (0, 1):
                    raise ValueError(f'Cannot inspect private Antigravity credentials before preserving chats (keyring lookup exit {result.returncode}). Unlock the credential companion keyring and retry.')
                profile = json.loads(result.stdout) if result.returncode == 0 else {}
                secrets = [value for name, value in profile.get('token', {}).items()
                           if name in ('access_token', 'refresh_token', 'id_token') and isinstance(value, str)]
            export_history(private_home, sys.stdout.buffer, provider, secrets)
        elif operation == 'restore':
            count = restore_history(private_home, sys.stdin.buffer, provider)
            print(f'Preserved {count} native chat files.' if count else 'No native chat transfer needed.')
        else:
            raise ValueError('Choose the native history helper operation export or restore.')
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        sys.exit(f'{type(error).__name__}: {error}')
