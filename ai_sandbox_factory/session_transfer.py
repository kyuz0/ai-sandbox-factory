"""Copy selected conversations between sandbox agents with session-migrate."""
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from uuid import uuid4
from zipfile import ZipFile

from . import chat_history, codex_history, folders, primary_agents, primary_workspace, sandbox, session_archive

SOURCE = sandbox.ROOT / 'containers/session-transfer'
IMAGE = ('localhost/ai-sandbox-factory-session-transfer:0.13.0-' + sandbox.tree_digest(SOURCE)
         + '-' + sandbox.tree_digest(sandbox.ROOT / 'containers/primary'))
TARGETS = ('pi', 'codex', 'claude')


def build(config):
    engine = config['engine']
    if subprocess.run([engine, 'image', 'inspect', IMAGE], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        native = {**config, 'primary_agent': 'codex'}
        image = primary_agents.image(native)
        if subprocess.run([engine, 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            primary_agents.build(native)
        result = subprocess.run([engine, 'build', '--build-arg', 'CODEX_IMAGE=' + image,
                                 '-t', IMAGE, str(SOURCE)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'Could not build the session-migrate helper (exit {result.returncode}).\n{result.stdout}\n{result.stderr}')


class Transfer:
    """Keep an immutable history snapshot for preview and confirmation."""
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.agent = primary_workspace.agent(config)
        self.temporary = tempfile.TemporaryDirectory(prefix='factory-session-transfer-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.plan = None
        try:
            build(config)
            backup = self.root / 'source.zip'
            if self.agent == 'pi':
                with sandbox.native_chat_lock(state):
                    current = sandbox.inspect(config, sandbox.names(config)[0])
                    if current and current['State']['Status'] == 'running':
                        raise ValueError('Stop the source sandbox before copying a chat.')
                    chat_history.export_chats(config, state, backup)
            else:
                chat_history.export_chats(config, state, backup)
            with ZipFile(backup) as archive:
                if self.agent == 'pi':
                    for member in session_archive.archive_members(archive):
                        destination = self.source / member.filename
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as incoming, destination.open('wb') as output:
                            shutil.copyfileobj(incoming, output)
                else:
                    chat_history.native_backup_member(archive, config)
                    with archive.open(chat_history.HISTORY) as incoming, tarfile.open(fileobj=incoming, mode='r|') as native:
                        for member in native:
                            if not member.isfile() or not codex_history.allowed(member.name, self.agent):
                                raise ValueError(f'Invalid native chat entry {member.name!r}')
                            destination = self.source / member.name
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with native.extractfile(member) as content, destination.open('xb') as output:
                                shutil.copyfileobj(content, output)
            self.rows = self.run({'operation': 'list'})
        except BaseException:
            self.close()
            raise

    def close(self):
        self.temporary.cleanup()

    def run(self, request, target=None):
        config = target or self.config
        command = [config['engine'], 'run', '--rm', '-i', '--pull=never', '--network=none',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges', '--security-opt=label=disable',
                   '--user', f'{config["uid"]}:{config["gid"]}', '-v', f'{self.source}:/transfer/source:ro']
        if config['engine'] == 'podman':
            command.append('--userns=keep-id:uid=1000,gid=1000')
        if target:
            # Reuse ownership and mount checks before exposing the target's history volume.
            chat_history.archive_command(target, 'import')
            container = sandbox.inspect(target, sandbox.names(target)[0])
            if container is None or container['State']['Status'] not in ('created', 'configured', 'exited', 'stopped'):
                raise ValueError('Stop the destination sandbox before copying a chat into it.')
            command += ['-v', sandbox.names(target)[3] + ':/transfer/target']
        request['source_agent'] = 'antigravity' if self.agent == 'agy' else self.agent
        result = subprocess.run(command + [IMAGE], input=json.dumps(request), capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'session-migrate failed (exit {result.returncode}).\n{result.stderr.strip()}')
        return json.loads(result.stdout)

    def preview(self, path, target, state):
        agent = primary_workspace.agent(target)
        if agent not in TARGETS:
            raise ValueError('This version of session-migrate cannot write chats for the installed Antigravity version.')
        if not any(row.get('path') == path and not row.get('error') for row in self.rows):
            raise ValueError('Choose a readable chat from this sandbox.')
        build(target)
        request = {'operation': 'preview', 'path': path, 'target_agent': agent,
                   'cwd': folders.container_cwd(target), 'session_id': str(uuid4())}
        with sandbox.native_chat_lock(state):
            manifest = self.run(request, target)
        self.plan = (request, target, state)
        return manifest

    def apply(self):
        if self.plan is None:
            raise ValueError('Preview the chat copy before creating it.')
        request, target, state = self.plan
        with sandbox.native_chat_lock(state):
            saved = json.loads((state / 'config.json').read_text())
            if saved != target:
                raise ValueError('The destination settings changed. Preview the copy again.')
            result = self.run({**request, 'operation': 'apply'}, target)
        self.plan = None
        return result
