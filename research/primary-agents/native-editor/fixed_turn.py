"""One synthetic file operation for the two-container experiment. Never deploy this fixture."""
import json
import os
import subprocess
import sys
import threading

FILE_TASK = ("Use the workspace shell to copy the text from /workspace/input.txt into "
             "/workspace/output.txt, then reply with that text.")
BINARY = '/usr/local/bin/factory-agent'


def claude_request(message, emit):
    kind = message.get('type')
    if kind == 'user' and FILE_TASK in str(message.get('message')):
        return {'type': 'user', 'message': {'role': 'user', 'content': FILE_TASK},
                'parent_tool_use_id': None, 'session_id': ''}
    if kind == 'control_request':
        subtype = message['request'].get('subtype')
        if subtype in ('initialize', 'get_settings'):
            return {**message, 'request': {'subtype': subtype}}
        emit({'type': 'control_response', 'response': {
            'subtype': 'error', 'request_id': message['request_id'], 'error': 'Unavailable in this test.'}})
    return None


def codex_request(message, threads, emit):
    method = message.get('method')
    request_id = message.get('id')
    given = message.get('params') or {}
    if method == 'initialize':
        params = {'clientInfo': {'name': 'codex_vscode', 'version': '1', 'title': 'Test editor'},
                  'capabilities': {'experimentalApi': True}}
    elif method == 'initialized':
        return {'method': method}
    elif method == 'getAuthStatus':
        params = {'includeToken': False, 'refreshToken': False}
    elif method == 'account/read':
        params = {'refreshToken': False}
    elif method in ('model/list', 'thread/list', 'config/read', 'configRequirements/read',
                    'collaborationMode/list', 'permissionProfile/list', 'experimentalFeature/list',
                    'mcpServerStatus/list', 'remoteControl/status/read', 'app/installed',
                    'plugin/installed', 'hooks/list'):
        params = {}
    elif method == 'thread/start':
        # The native executor runs in the separate offline workspace.
        params = {'cwd': '/workspace', 'approvalPolicy': 'never', 'sandbox': 'danger-full-access',
                  'experimentalRawEvents': False, 'persistExtendedHistory': False}
    elif (method == 'turn/start' and given.get('threadId') in threads
          and FILE_TASK in str(given.get('input'))):
        params = {'threadId': given['threadId'],
                  'input': [{'type': 'text', 'text': FILE_TASK, 'text_elements': []}]}
    else:
        if request_id is not None:
            emit({'id': request_id, 'error': {'code': -32601, 'message': 'Unavailable in this test.'}})
        return None
    return {'id': request_id, 'method': method, 'params': params}


def main(provider):
    if provider == 'codex':
        os.environ['CODEX_EXEC_SERVER_URL'] = 'ws://127.0.0.1:36781'
        args = ['-c', 'cli_auth_credentials_store="file"', '-c', 'features.shell_tool=true', 'app-server']
    elif provider == 'claude':
        workspace = {'command': '/usr/bin/socat', 'args': ['STDIO', 'UNIX-CONNECT:/run/primary-workspace/tools.sock']}
        args = ['--print', '--restricted', '--tools', '', '--strict-mcp-config', '--mcp-config',
                json.dumps({'mcpServers': {'factory_probe': workspace}}), '--allowedTools',
                'mcp__factory_probe__workspace_shell', '--verbose', '--input-format', 'stream-json',
                '--output-format', 'stream-json', '--permission-mode', 'dontAsk']
    else:
        raise ValueError('Choose codex or claude')

    process = subprocess.Popen([BINARY, *args], cwd='/workspace', stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    pending = {}
    threads = set()
    output = threading.Lock()

    def emit(message):
        with output:
            print(json.dumps(message), flush=True)

    def receive():
        for line in process.stdout:
            reply = json.loads(line)
            method = pending.pop(reply.get('id'), None)
            result = reply.get('result') or {}
            if method == 'getAuthStatus' and 'result' in reply:
                reply['result'] = {key: result.get(key) for key in ('authMethod', 'requiresOpenaiAuth')}
                reply['result']['authToken'] = None
            if method == 'thread/start' and result.get('thread', {}).get('id'):
                threads.add(result['thread']['id'])
            emit(reply)

    threading.Thread(target=receive, daemon=True).start()
    try:
        for line in sys.stdin:
            incoming = json.loads(line)
            if provider == 'codex':
                outgoing = codex_request(incoming, threads, emit)
                if outgoing and 'id' in outgoing:
                    pending[outgoing['id']] = outgoing['method']
            else:
                outgoing = claude_request(incoming, emit)
            if outgoing:
                print(json.dumps(outgoing), file=process.stdin, flush=True)
    finally:
        process.stdin.close()
        process.terminate()
        process.wait()


if __name__ == '__main__':
    main(sys.argv[1])
