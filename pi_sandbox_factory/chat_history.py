"""Transfer saved chat files between a sandbox home volume and the host."""

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
from zipfile import BadZipFile, ZipFile

from . import folders, host_platform, sandbox, session_archive


def backup_folder():
    data = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'
    return Path(data).expanduser() / 'pi-sandbox-factory/chat-backups'


def suggested_file(name):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return backup_folder() / f'{name}-chats-{stamp}.zip'


def archive_command(config, *args):
    engine = config['engine']
    dev, _, _, volume = sandbox.names(config)
    container = sandbox.inspect(config, dev)
    listing = sandbox.run([engine, 'volume', 'ls', '--format', '{{.Name}}'], capture=True)
    if volume not in listing.stdout.splitlines():
        if args[0] == 'export':
            raise ValueError(session_archive.EMPTY)
        raise ValueError('Start this sandbox once to create its chat storage, then try again.')
    volume_info = sandbox.run([engine, 'volume', 'inspect', volume], capture=True)
    owner = (json.loads(volume_info.stdout)[0].get('Labels') or {}).get(sandbox.LABEL)
    if owner is not None and owner != config['name']:
        raise ValueError('Factory could not verify this sandbox’s chat storage.')
    if container:
        if not any(m.get('Type') == 'volume' and m.get('Name') == volume and m.get('Destination') == '/home/node'
                   for m in container.get('Mounts') or []):
            raise ValueError('Factory could not verify this sandbox’s chat storage.')
        image = container['Image']
    else:
        image = sandbox.image_names(config)[0]
    if sandbox.run([engine, 'image', 'inspect', image], capture=True, check=False,
                   stderr=subprocess.DEVNULL).returncode:
        raise ValueError('The sandbox image is missing. Restore it before transferring chats.')
    operation, *parameters = args
    mount = f'{volume}:/chat-home' + (':ro' if operation == 'export' else '')
    command = [engine, 'run', '--rm', '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--security-opt=label=disable', '--workdir=/',
               '--entrypoint=python3', '--user', f'{config["uid"]}:{config["gid"]}', '-v', mount]
    if engine == 'podman':
        command.append('--userns=keep-id:uid=1000,gid=1000')
    if operation == 'import':
        command.append('-i')
    script = Path(session_archive.__file__).read_text()
    return command + [image, '-I', '-c', script, operation, '/chat-home', *parameters]


def transfer(command, **streams):
    try:
        result = subprocess.run(command, stderr=subprocess.PIPE, **streams)
    except OSError as error:
        raise ValueError('Could not run the container engine to transfer chats.') from error
    if result.returncode:
        message = result.stderr.decode(errors='replace').strip()
        raise ValueError(message or 'The container engine could not transfer chats.')
    return result.stdout


def export_chats(config, state, value):
    path = host_platform.local_path(value).absolute()
    if path.suffix.lower() != '.zip':
        raise ValueError('Enter a backup filename ending in .zip.')
    if path.resolve().is_relative_to(state.resolve()):
        raise ValueError('Choose a folder outside this sandbox’s private storage.')
    if path.exists() or path.is_symlink():
        raise ValueError('A file already exists there. Choose another name.')
    command = archive_command(config, 'export')
    try:
        if path.parent == backup_folder().absolute():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='.chat-backup-', dir=path.parent) as temporary:
            transfer(command, stdout=temporary, stdin=subprocess.DEVNULL)
            with ZipFile(temporary.name) as archive:
                count = len(session_archive.archive_members(archive))
            os.fsync(temporary.fileno())
            os.link(temporary.name, path)
    except FileExistsError:
        raise ValueError('A file already exists there. Choose another name.') from None
    except FileNotFoundError:
        raise ValueError('That host folder does not exist. Choose an existing folder.') from None
    except BadZipFile:
        raise ValueError('Factory could not create a complete chat backup.') from None
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise ValueError('The host disk is full. Choose a folder with more space.') from error
        raise ValueError('Could not save the backup in that host folder. Choose another folder.') from error
    return path, count


def import_chats(config, value):
    path = host_platform.local_path(value)
    kind = path.suffix.lower().lstrip('.')
    if kind not in ('zip', 'jsonl'):
        raise ValueError('Choose a Factory chat backup (.zip) or a Pi chat file (.jsonl).')
    if not path.is_file():
        raise ValueError('Choose an existing file on this computer.')
    cwd = folders.container_cwd(config)
    try:
        # Pass the actual descriptor to the engine after validation. A buffered
        # seek could leave the child reading from the end of the Python buffer.
        source = path.open('rb', buffering=0)
    except OSError as error:
        raise ValueError('Could not read that file. Check its permissions or choose another file.') from error
    with source:
        if kind == 'zip':
            try:
                with ZipFile(source) as archive:
                    session_archive.archive_members(archive)
            except BadZipFile:
                raise ValueError('The chat backup is incomplete or damaged.') from None
            source.seek(0)
        else:
            session_archive.header(source)
            source.seek(0)
        command = archive_command(config, 'import', kind, cwd)
        result = json.loads(transfer(command, stdin=source, stdout=subprocess.PIPE))
    return result['added'], result['skipped']
