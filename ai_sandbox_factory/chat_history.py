"""Transfer saved chat files between a sandbox home volume and the host."""

from contextlib import nullcontext
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from . import codex_history, folders, host_platform, primary_workspace, sandbox, session_archive


NATIVE_FORMAT = b'ai-sandbox-factory-native-chat-history-v1'
MANIFEST = 'chat-backup.json'
HISTORY = 'history.tar'


def backup_folder():
    data = os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'
    return Path(data).expanduser() / 'ai-sandbox-factory/chat-backups'


def suggested_file(name):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return backup_folder() / f'{name}-chats-{stamp}.zip'


def archive_command(config, *args):
    operation, *parameters = args
    agent = primary_workspace.agent(config)
    native = agent != 'pi'
    engine = config['engine']
    dev, _, _, volume = sandbox.names(config)
    container = sandbox.inspect(config, dev)
    if native:
        if container is None:
            raise ValueError('Start this sandbox once to create its chat storage, then stop it and try again.')
        if container['State']['Status'] not in ('created', 'configured', 'exited', 'stopped'):
            raise ValueError('Choose Actions → Stop sandbox, then try again.')
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
    mount = f'{volume}:/chat-home' + (':ro' if operation == 'export' else '')
    command = [engine, 'run', '--rm', '--pull=never', '--network=none', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--security-opt=label=disable', '--workdir=/',
               '--entrypoint=python3', '--user', f'{config["uid"]}:{config["gid"]}', '-v', mount]
    if not native:
        command.append('--read-only')
    if engine == 'podman':
        command.append('--userns=keep-id:uid=1000,gid=1000')
    if operation == 'import':
        command.append('-i')
    if native:
        script = Path(codex_history.__file__).read_text()
        return command + [image, '-I', '-c', script, 'backup' if operation == 'export' else 'import',
                          agent, '/chat-home']
    script = Path(session_archive.__file__).read_text()
    return command + [image, '-I', '-c', script, operation, '/chat-home', *parameters]


def transfer(command, **streams):
    try:
        result = subprocess.run(command, stderr=subprocess.PIPE, **streams)
    except OSError as error:
        raise ValueError(f'Could not run {command[0]} to transfer chats: {error}') from error
    if result.returncode:
        message = result.stderr.decode(errors='replace').strip()
        raise ValueError(f'{command[0]} exited with status {result.returncode} while transferring chats.'
                         + ('\n' + message if message else ' No diagnostic output was returned.'))
    return result.stdout


def backup_zip(config, output, contents):
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        archive.comment = NATIVE_FORMAT
        archive.writestr(MANIFEST, json.dumps({
            'agent': primary_workspace.agent(config), 'cwd': folders.container_cwd(config),
        }))
        contents.seek(0)
        with archive.open(HISTORY, 'w', force_zip64=True) as destination:
            shutil.copyfileobj(contents, destination)


def native_backup_member(archive, config):
    info = archive.infolist()
    if (archive.comment != NATIVE_FORMAT or len(info) != 2 or
            {item.filename for item in info} != {MANIFEST, HISTORY} or
            any(stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG) for item in info) or
            archive.getinfo(MANIFEST).file_size > 8192):
        raise ValueError('Choose a native chat backup exported by AI Sandbox Factory.')
    try:
        manifest = json.loads(archive.read(MANIFEST))
    except (ValueError, UnicodeError) as error:
        raise ValueError('The chat backup has an invalid manifest.') from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get('agent'), str) or manifest['agent'] not in codex_history.HOMES:
        raise ValueError('The chat backup has an invalid coding agent.')
    source = manifest['agent']
    target = primary_workspace.agent(config)
    if source != target:
        raise ValueError(f'This backup is for {primary_workspace.AGENTS[source]}. '
                         f'Choose a {primary_workspace.AGENTS[source]} sandbox to restore it.')
    source_cwd = manifest.get('cwd')
    if not isinstance(source_cwd, str) or not (source_cwd == '/workspace' or source_cwd.startswith('/workspace/')):
        raise ValueError('The chat backup has an invalid starting folder.')
    if source_cwd != folders.container_cwd(config):
        starting_folder = 'the workspace root' if source_cwd == '/workspace' else repr(source_cwd.removeprefix('/workspace/'))
        raise ValueError(f'This backup uses a different starting folder. '
                         f'In Settings → Folders, set the starting folder to {starting_folder}.')
    return archive.getinfo(HISTORY)


def export_chats(config, state, value):
    native = primary_workspace.agent(config) != 'pi'
    path = host_platform.local_path(value).absolute()
    if path.suffix.lower() != '.zip':
        raise ValueError('Enter a backup filename ending in .zip.')
    if path.resolve().is_relative_to(state.resolve()):
        raise ValueError('Choose a folder outside this sandbox’s private storage.')
    if path.exists() or path.is_symlink():
        raise ValueError('A file already exists there. Choose another name.')
    try:
        if path.parent == backup_folder().absolute():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='.chat-backup-', dir=path.parent) as temporary:
            with sandbox.native_chat_lock(state) if native else nullcontext():
                command = archive_command(config, 'export')
                if native:
                    with tempfile.TemporaryFile(dir=path.parent) as contents:
                        transfer(command, stdout=contents, stdin=subprocess.DEVNULL)
                        backup_zip(config, temporary, contents)
                    count = None
                else:
                    transfer(command, stdout=temporary, stdin=subprocess.DEVNULL)
                    with ZipFile(temporary.name) as archive:
                        count = len(session_archive.archive_members(archive))
            os.fsync(temporary.fileno())
            os.link(temporary.name, path)
    except FileExistsError:
        raise ValueError('A file already exists there. Choose another name.') from None
    except FileNotFoundError as error:
        raise ValueError(f'Could not export the chat backup to {path}: {error}') from error
    except BadZipFile as error:
        raise ValueError(f'Factory could not create a complete chat backup: {error}') from error
    except OSError as error:
        if error.errno == errno.ENOSPC:
            raise ValueError(f'The host disk is full. Could not save {path}: {error}') from error
        raise ValueError(f'Could not save the chat backup to {path}: {error}') from error
    return path, count


def import_chats(config, value, *, state=None):
    native = primary_workspace.agent(config) != 'pi'
    path = host_platform.local_path(value)
    kind = path.suffix.lower().lstrip('.')
    if kind not in (('zip',) if native else ('zip', 'jsonl')):
        raise ValueError('Choose a Factory chat backup (.zip).' if native else
                         'Choose a Factory chat backup (.zip) or a Pi chat file (.jsonl).')
    if not path.is_file():
        raise ValueError('Choose an existing file on this computer.')
    cwd = folders.container_cwd(config)
    try:
        # Pass the actual descriptor to the engine after validation. A buffered
        # seek could leave the child reading from the end of the Python buffer.
        source = path.open('rb', buffering=0)
    except OSError as error:
        raise ValueError(f'Could not read {path}: {error}') from error
    with source:
        if native:
            try:
                with ZipFile(source) as archive, tempfile.TemporaryFile() as contents:
                    member = native_backup_member(archive, config)
                    with archive.open(member) as history:
                        shutil.copyfileobj(history, contents)
                    contents.seek(0)
                    with sandbox.native_chat_lock(state):
                        command = archive_command(config, 'import')
                        result = json.loads(transfer(command, stdin=contents, stdout=subprocess.PIPE))
            except (BadZipFile, RuntimeError, NotImplementedError) as error:
                raise ValueError(f'Could not read the chat backup: {error}') from error
            return result['added'], result['skipped']
        if kind == 'zip':
            try:
                with ZipFile(source) as archive:
                    if archive.comment == NATIVE_FORMAT:
                        native_backup_member(archive, config)
                    session_archive.archive_members(archive)
            except BadZipFile as error:
                raise ValueError(f'The chat backup is incomplete or damaged: {error}') from error
            source.seek(0)
        else:
            session_archive.header(source)
            source.seek(0)
        command = archive_command(config, 'import', kind, cwd)
        result = json.loads(transfer(command, stdin=source, stdout=subprocess.PIPE))
    return result['added'], result['skipped']
