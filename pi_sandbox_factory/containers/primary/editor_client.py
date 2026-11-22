"""Workspace-side native CLI entry point. No account material is available here."""
import json
import os
import socket
import sys
import threading
from pathlib import Path
import re
import signal


def save_history(message):
    """Keep the extension's chat list in its own writable home, without auth files."""
    project, identifier = message['project'], message['session_id']
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', project) or not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier):
        raise ValueError('Invalid chat history path')
    root = Path.home() / '.claude/projects'
    destination = root / project / (identifier + '.jsonl')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('Chat history must stay in the editor home')
    titles = []
    if destination.exists():
        for line in destination.read_text().splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('type') in ('custom-title', 'ai-title'):
                titles.append(item)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(''.join(json.dumps(record) + '\n' for record in message['records'] + titles))
    temporary.replace(destination)


def main():
    args = sys.argv[1:]
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect('/run/primary-editor/editor.sock')
        channel.sendall((json.dumps({'args': args}) + '\n').encode())

        def stop(signum, _frame):
            try:
                channel.sendall(b'{"type":"factory_disconnect"}\n')
            except OSError:
                pass
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        def input_stream():
            try:
                while chunk := os.read(sys.stdin.fileno(), 65536):
                    channel.sendall(chunk)
                channel.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=input_stream, daemon=True).start()
        with channel.makefile('rb') as incoming:
            for line in incoming:
                message = json.loads(line)
                if message.get('type') == 'factory_history':
                    save_history(message)
                    continue
                if message.get('type') == 'factory_cli':
                    sys.stdout.write(message['stdout'])
                    raise SystemExit(message['code'])
                if message.get('type') == 'factory_exit':
                    if message.get('error'):
                        print(message['error'], file=sys.stderr)
                    raise SystemExit(message['code'])
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
