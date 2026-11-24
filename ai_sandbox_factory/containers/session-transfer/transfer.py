"""Convert selected, staged chat files using the pinned session-migrate package."""
import json
from pathlib import Path
import sys

from session_migrate.conversion import (
    ConversionOptions, convert_session, load_session, target_import_paths, write_artifact,
)
from session_migrate.model import AgentFormat, TargetFormat


class NativeCodex:
    """Read complete persisted items through the pinned Codex public history API."""
    def __init__(self, root, binary='/usr/local/bin/factory-codex'):
        import os
        import queue
        import shutil
        import subprocess
        import tempfile
        import threading
        self.temporary = tempfile.TemporaryDirectory(prefix='factory-codex-projection-')
        home = Path(self.temporary.name) / 'home'
        shutil.copytree(root, home)
        # The native index stores absolute rollout paths. Rebase only our disposable copy.
        import sqlite3
        index = home / 'state_5.sqlite'
        if index.exists():
            with sqlite3.connect(index) as database:
                for identifier, rollout in database.execute('SELECT id,rollout_path FROM threads').fetchall():
                    parts = Path(rollout).parts
                    for directory in ('sessions', 'archived_sessions'):
                        if directory in parts:
                            relative = Path(*parts[parts.index(directory):])
                            if (home / relative).is_file():
                                database.execute('UPDATE threads SET rollout_path=? WHERE id=?',
                                                 (str(home / relative), identifier))
                            break
        self.log = tempfile.TemporaryFile(mode='w+')
        self.queue = queue.Queue()
        self.sequence = 0
        self.process = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, text=True,
            env={**os.environ, 'CODEX_HOME': str(home), 'HOME': self.temporary.name})
        def read():
            try:
                for line in self.process.stdout:
                    self.queue.put(json.loads(line))
            except Exception as error:
                self.queue.put(error)
            finally:
                self.queue.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'factory-session-transfer', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            self.process.stdin.write(json.dumps({'method': 'initialized'}) + '\n')
            self.process.stdin.flush()
        except BaseException:
            self.close()
            raise

    def call(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'id': self.sequence, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        while True:
            reply = self.queue.get()
            if reply is None or isinstance(reply, Exception):
                self.log.seek(0)
                raise ValueError(f'Codex history operation {method} stopped before replying: '
                                 f'{reply}; exit {self.process.poll()}; {self.log.read()}')
            if reply.get('id') == self.sequence:
                if 'error' in reply:
                    raise ValueError(f'Codex history operation {method} rejected {params}: {reply["error"]}')
                return reply['result']

    def pages(self, method, params):
        seen = set()
        while True:
            page = self.call(method, params)
            yield from page['data']
            cursor = page.get('nextCursor')
            if not cursor:
                return
            if cursor in seen:
                raise ValueError(f'Codex history operation {method} repeated cursor {cursor!r}; cannot prove complete history')
            seen.add(cursor)
            params = {**params, 'cursor': cursor}

    def session(self, path, source):
        import hashlib
        from datetime import UTC, datetime
        from session_migrate.model import Event, EventKind, Provenance, Role, Session
        thread = self.call('thread/read', {'threadId': source.session_id, 'includeTurns': False})['thread']
        events = []
        native_items = []
        def add(kind, index, **values):
            events.append(Event(kind, Provenance(index, 'native_codex_item'), **values))
        for turn in self.pages('thread/turns/list', {'threadId': source.session_id,
                                                   'sortDirection': 'asc', 'itemsView': 'notLoaded'}):
            for entry in self.pages('thread/items/list', {'threadId': source.session_id,
                                                         'turnId': turn['id'], 'sortDirection': 'asc'}):
                item = entry['item']
                index = len(native_items)
                native_items.append(entry)
                kind = item['type']
                timestamp = (datetime.fromtimestamp(entry['startedAtMs'] / 1000, UTC).isoformat()
                             if entry.get('startedAtMs') is not None else None)
                if kind == 'userMessage':
                    for block in item['content']:
                        if block['type'] == 'text':
                            add(EventKind.MESSAGE, index, role=Role.USER, text=block['text'], timestamp=timestamp)
                        else:
                            add(EventKind.CONTEXT, index, role=Role.USER, timestamp=timestamp,
                                payload={'block_type': block['type']})
                elif kind == 'agentMessage':
                    add(EventKind.MESSAGE, index, role=Role.ASSISTANT, text=item['text'], timestamp=timestamp)
                elif kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall'):
                    if kind == 'commandExecution':
                        name = 'exec_command'
                        arguments = {'cmd': item['command'], 'workdir': item['cwd']}
                        result = item.get('aggregatedOutput')
                    elif kind == 'mcpToolCall':
                        name = item['tool']
                        arguments = item['arguments']
                        result = item.get('result') if item.get('error') is None else item['error']
                    else:
                        name = item['tool']
                        arguments = item['arguments']
                        result = item.get('contentItems')
                    add(EventKind.TOOL_CALL, index, tool_name=name, tool_call_id=item['id'],
                        timestamp=timestamp, payload={'input': arguments})
                    if result is not None:
                        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                        add(EventKind.TOOL_RESULT, index, role=Role.TOOL, tool_name=name,
                            tool_call_id=item['id'], text=output, timestamp=timestamp,
                            payload={'is_error': item.get('status') in ('failed', 'declined')
                                     or item.get('error') is not None})
                else:
                    # Private reasoning and unsupported native items are counted, never copied as invented messages.
                    add(EventKind.OPAQUE, index, timestamp=timestamp,
                        payload={'source_item_type': kind})
        if not native_items:
            raise ValueError(f'Codex chat {source.session_id} has no canonical persisted items in its native history. '
                             'Keep the source chat and retry after it has been saved by Codex.')
        digest = hashlib.sha256(json.dumps(native_items, sort_keys=True).encode()).hexdigest()
        return Session(source.source_format, path, digest, source.session_id,
                       Path(thread['cwd']), source.started_at, source.cli_version,
                       source.model, thread.get('name') or source.title, tuple(events), len(native_items),
                       model_provider=source.model_provider)

    def close(self):
        self.process.stdin.close()
        self.process.terminate()
        self.process.wait()
        self.reader.join()
        self.process.stdout.close()
        self.log.close()
        self.temporary.cleanup()


def load_source(path, source_format, native=None):
    if source_format == AgentFormat.CODEX:
        try:
            with path.open() as stream:
                first = json.loads(stream.readline())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f'Cannot read Codex chat metadata from {path}: {error}. '
                             'Keep the original chat and export its complete native history again.') from error
        metadata = first.get('payload', {})
        if metadata.get('history_mode') == 'paginated':
            identifier = metadata.get('id') or metadata.get('session_id')
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError(f'Codex paginated chat {path} has no session ID in its session_meta record. '
                                 'Export the complete native chat history and retry.')
            if native is None:
                raise ValueError(f"Codex chat {metadata.get('id')} uses paginated history, but its "
                                 'thread_history_1.sqlite database is missing from the saved history. '
                                 'Export the complete native chat history and retry.')
            from session_migrate.model import Session
            source = Session(source_format, path, '', identifier,
                             Path(metadata['cwd']) if metadata.get('cwd') else None, metadata.get('timestamp') or first.get('timestamp'),
                             metadata.get('cli_version'), None, None, (), 0,
                             model_provider=metadata.get('model_provider'))
            return native.session(path, source)
    return load_session(path, source_format)


def main():
    request = json.load(sys.stdin)
    root = Path('/transfer/source')
    source_format = AgentFormat(request['source_agent'])
    native = NativeCodex(root) if source_format == AgentFormat.CODEX and (root / 'thread_history_1.sqlite').exists() else None
    try:
        execute(request, root, source_format, native)
    finally:
        if native is not None:
            native.close()


def execute(request, root, source_format, native):
    if request['operation'] == 'list':
        rows = []
        pattern = '**/*.db' if source_format == AgentFormat.ANTIGRAVITY else '**/*.jsonl'
        for path in sorted(root.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                session = load_source(path, source_format, native)
            except Exception as error:
                rows.append({'path': str(path.relative_to(root)), 'error': str(error)})
                continue
            if not session.events:
                continue
            rows.append({'path': str(path.relative_to(root)), 'title': session.title or session.session_id or path.name,
                         'events': session.event_counts(), 'cwd': str(session.cwd or '')})
        print(json.dumps(rows))
        return
    relative = Path(request['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid source chat path')
    source = (root / relative).resolve(strict=True)
    if not source.is_relative_to(root):
        raise ValueError('Source chat is outside the selected history')
    home = Path('/transfer/target') / {'pi': '.pi/agent', 'codex': '.codex', 'claude': '.claude'}[request['target_agent']]
    preferences = {}
    if request['target_agent'] == 'codex':
        import tomllib
        config = home / 'config.toml'
        preferences = tomllib.loads(config.read_text()) if config.exists() else {}
        preferences.setdefault('model_provider', 'openai')
    elif request['target_agent'] == 'pi':
        settings = home / 'settings.json'
        configured = json.loads(settings.read_text()) if settings.exists() else {}
        preferences = {'model_provider': configured.get('defaultProvider'), 'model': configured.get('defaultModel')}
    artifact = convert_session(load_source(source, source_format, native), ConversionOptions(
        target_format=TargetFormat(request['target_agent']), cwd=Path(request['cwd']),
        session_id=request['session_id'], model_provider=preferences.get('model_provider'),
        model=preferences.get('model')))
    native, manifest = target_import_paths(artifact, home)
    if request['operation'] == 'apply':
        write_artifact(artifact, output_path=native, manifest_path=manifest)
    print(json.dumps(artifact.manifest(output_path=native)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        sys.exit(f'{type(error).__name__}: {error}')
