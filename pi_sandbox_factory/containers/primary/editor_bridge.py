"""Serve the native editor protocol without exposing controller configuration or credentials."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal

from editor_protocol import ClaudeProtocol, CodexProtocol, Conversations, Rejected, model_name, object_value, session_id
from launch import AGENT, SOURCE, command

SOCKET = Path('/run/primary-editor/editor.sock')
OWNERS = {}


def history_message(identifier):
    """Expose chat records only. Claude diagnostics can contain request headers."""
    identifier = session_id(identifier)
    root = Path.home() / '.claude/projects'
    for source in root.glob('*/' + identifier + '.jsonl'):
        if not source.resolve().is_relative_to(root.resolve()):
            continue
        records = []
        for line in source.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (record.get('isApiErrorMessage') or 'error' in record or 'apiError' in record or
                    any(tag in json.dumps(record.get('message', {})) for tag in
                        ('<local-command-stdout>', '<local-command-stderr>'))):
                continue
            if record.get('type') in ('user', 'assistant', 'summary'):
                keys = ('type', 'uuid', 'parentUuid', 'sessionId', 'cwd', 'timestamp', 'message',
                        'isSidechain', 'version', 'slug', 'gitBranch', 'userType', 'isMeta',
                        'summary', 'leafUuid')
                records.append({key: record[key] for key in keys if key in record})
        return {'type': 'factory_history', 'project': source.parent.name,
                'session_id': identifier, 'records': records}
    return None


def arguments(agent, supplied, settings, context, conversations):
    if not isinstance(supplied, list) or any(not isinstance(arg, str) for arg in supplied):
        raise Rejected('Invalid editor arguments')
    base = command(agent, 'editor' if agent == 'codex' else 'open', settings, context)
    if agent == 'codex':
        # app-server has its own per-turn policy. Keep only controller-owned configuration.
        return [AGENT, *base]
    if agent != 'claude':
        raise Rejected('No native editor connection for this agent')
    # Every executable, tool and environment value comes from Factory.
    allowed = base.index('--allowedTools')
    del base[allowed:allowed + 2]
    cwd = Path(settings['working_directory'])
    cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(cwd)
    result = [AGENT, *base, '--print', '--verbose', '--input-format', 'stream-json',
              '--output-format', 'stream-json', '--permission-mode', 'default',
              '--permission-prompt-tool', 'stdio', '--setting-sources', '', '--include-partial-messages']
    for flag, validate in (('--resume', conversations.require), ('--model', model_name), ('--effort', model_name)):
        if flag in supplied:
            position = supplied.index(flag) + 1
            if position >= len(supplied):
                raise Rejected('Missing editor argument')
            result += [flag, validate(supplied[position])]
    return result


async def read_message(reader):
    chunks = []
    while True:
        try:
            chunks.append(await reader.readuntil(b'\n'))
            return b''.join(chunks)
        except asyncio.LimitOverrunError as error:
            chunks.append(await reader.readexactly(error.consumed))
        except asyncio.IncompleteReadError as error:
            if chunks or error.partial:
                raise ValueError('Incomplete editor message') from error
            return b''


async def serve_client(reader, writer, agent):
    process = None
    tasks = []
    ownership = {'closed': False, 'done': asyncio.Event(), 'turns': 0}

    def retire():
        # A browser reload may leave an idle extension process connected. Hand
        # its saved chat to the new client without allowing concurrent writers.
        ownership['closed'] = True
        writer.close()
        if process and process.returncode is None:
            process.terminate()

    ownership['retire'] = retire

    async def claim(identifier):
        key = (agent, session_id(identifier))
        while (owner := OWNERS.get(key)) and owner is not ownership:
            if not owner['closed'] and owner['turns']:
                raise Rejected('This chat is open in another editor. Close it there before reopening it here.')
            if not owner['closed']:
                owner['retire']()
            await owner['done'].wait()
        OWNERS[key] = ownership

    async def send(message):
        writer.write((json.dumps(message) + '\n').encode())
        await writer.drain()

    async def forward(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        await process.stdin.drain()

    try:
        greeting = object_value(json.loads(await read_message(reader)))
        supplied = greeting.get('args')
        if not isinstance(supplied, list) or any(not isinstance(arg, str) for arg in supplied):
            raise Rejected('Invalid editor arguments')
        if '--version' in supplied:
            process = await asyncio.create_subprocess_exec(AGENT, '--version', stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.DEVNULL)
            output, _ = await process.communicate()
            await send({'type': 'factory_cli', 'stdout': output.decode(), 'code': process.returncode})
            return
        if agent == 'claude' and any(supplied[i:i + 2] == ['auth', 'status'] for i in range(len(supplied))):
            process = await asyncio.create_subprocess_exec(AGENT, 'auth', 'status', '--json',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await process.communicate()
            status = json.loads(output)
            safe = {key: status[key] for key in ('loggedIn', 'authMethod', 'apiProvider', 'subscriptionType') if key in status}
            await send({'type': 'factory_cli', 'stdout': json.dumps(safe) + '\n', 'code': process.returncode})
            return
        settings = json.loads((SOURCE / 'settings.json').read_text())
        context = (SOURCE / 'context.md').read_text()
        conversations = Conversations(Path.home() / '.factory-editor' / agent / 'conversations')
        args = arguments(agent, greeting.get('args'), settings, context, conversations)
        if agent == 'claude' and '--resume' in args:
            identifier = args[args.index('--resume') + 1]
            await claim(identifier)
            if history := await asyncio.to_thread(history_message, identifier):
                await send(history)
        protocol = (CodexProtocol(settings['working_directory'], context, conversations)
                    if agent == 'codex' else ClaudeProtocol(conversations))
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        async def output():
            while line := await read_message(process.stdout):
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                failed_turn = (agent == 'codex' and 'error' in raw and
                               protocol.pending.get(raw.get('id')) == 'turn/start')
                message = protocol.outgoing(raw)
                if ((agent == 'claude' and raw.get('type') == 'result') or
                        (agent == 'codex' and raw.get('method') == 'turn/completed') or failed_turn):
                    ownership['turns'] = max(0, ownership['turns'] - 1)
                for reply in protocol.replies:
                    if not process.stdin.is_closing():
                        await forward(reply)
                protocol.replies.clear()
                if message is None:
                    continue
                if agent == 'claude' and message.get('type') == 'system' and message.get('subtype') == 'init':
                    await claim(message['session_id'])
                if agent == 'codex' and message.get('method') == 'thread/started':
                    await claim(message['params']['thread']['id'])
                if agent == 'claude' and message.get('type') == 'result' and message.get('session_id'):
                    if history := await asyncio.to_thread(history_message, message['session_id']):
                        await send(history)
                await send(message)
            await process.wait()
            await send({'type': 'factory_exit', 'code': process.returncode})

        async def input_messages():
            while line := await read_message(reader):
                if ownership['closed']:
                    break
                incoming = object_value(json.loads(line))
                if incoming.get('type') == 'factory_disconnect':
                    if process.returncode is None:
                        process.terminate()
                    break
                outgoing = None
                try:
                    outgoing = protocol.incoming(incoming)
                    if agent == 'codex' and outgoing.get('method') in ('thread/resume', 'thread/fork'):
                        await claim(outgoing['params']['threadId'])
                except (Rejected, KeyError, TypeError, AttributeError) as error:
                    if agent == 'codex' and outgoing is not None:
                        protocol.pending.pop(outgoing.get('id'), None)
                    if agent == 'codex' and 'id' in incoming:
                        await send({'id': incoming['id'], 'error': {'code': -32602, 'message': str(error)}})
                    elif incoming.get('type') == 'control_request':
                        await send({'type': 'control_response', 'response': {'subtype': 'error',
                            'request_id': incoming.get('request_id'), 'error': str(error)}})
                    continue
                if (agent == 'claude' and outgoing.get('type') == 'user' or
                        agent == 'codex' and outgoing.get('method') == 'turn/start'):
                    ownership['turns'] += 1
                await forward(outgoing)
            ownership['closed'] = True
            # Preserve half-close semantics: the client can still read the final result.
            process.stdin.close()

        receiving = asyncio.create_task(output())
        sending = asyncio.create_task(input_messages())
        tasks = [receiving, sending]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if not receiving.done():
            await receiving
    except (ValueError, OSError, ConnectionError, asyncio.IncompleteReadError) as error:
        with contextlib.suppress(ConnectionError, OSError):
            await send({'type': 'factory_exit', 'code': 1, 'error': str(error)})
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process:
            if process.returncode is None:
                process.stdin.close()
                process.terminate()
            await process.wait()
        for key, owner in list(OWNERS.items()):
            if owner is ownership:
                del OWNERS[key]
        ownership['done'].set()
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


async def main():
    os.umask(0o077)
    agent = os.environ['FACTORY_PRIMARY_AGENT']
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    sessions = set()

    async def connect(reader, writer):
        task = asyncio.current_task()
        sessions.add(task)
        try:
            await serve_client(reader, writer, agent)
        finally:
            sessions.discard(task)

    server = await asyncio.start_unix_server(connect, str(SOCKET))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await stopped.wait()
    finally:
        server.close()
        for task in list(sessions):
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)
        await server.wait_closed()
        SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    asyncio.run(main())
