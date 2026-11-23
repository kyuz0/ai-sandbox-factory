"""The untrusted extension can request work without changing its private controller."""
import importlib.util
import asyncio
from pathlib import Path
import unittest
import tempfile
import json
import sys
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary/editor_protocol.py'
spec = importlib.util.spec_from_file_location('native_editor_protocol', PATH)
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


def component(name):
    spec = importlib.util.spec_from_file_location(name, PATH.with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'path', [str(PATH.parent), *sys.path]):
        spec.loader.exec_module(module)
    return module


class NativeEditorProtocolTests(unittest.TestCase):
    def test_claude_api_errors_cannot_repeat_private_credentials(self):
        bridge = protocol.ClaudeProtocol()
        secret = 'fixture-private-credential'
        for event in (
            {'type': 'assistant', 'session_id': 'ours', 'is_api_error_message': True,
             'api_error_status': 400, 'error': secret,
             'message': {'id': 'message-id', 'content': [{'type': 'text', 'text': secret}], 'diagnostics': secret}},
            {'type': 'result', 'session_id': 'ours', 'is_error': True, 'subtype': 'success',
             'api_error_status': 401, 'result': secret, 'errors': [secret], 'diagnostics': secret},
            {'type': 'system', 'subtype': 'api_retry', 'api_error_status': 429,
             'error': {'message': secret}, 'attempt': 1, 'max_retries': 11},
        ):
            with self.subTest(kind=event['type']):
                result = bridge.outgoing(event)
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(result['type'], event['type'])
                self.assertEqual(result['api_error_status'], event['api_error_status'])
                if event['type'] != 'system':
                    self.assertIn('Claude could not complete', json.dumps(result))
        retry = bridge.outgoing({'type': 'system', 'subtype': 'api_retry', 'error_status': 429,
                                 'error': 'rate_limit', 'attempt': 2, 'max_retries': 11})
        self.assertEqual(retry['error'], 'rate_limit')
        self.assertEqual(retry['error_status'], 429)
        ordinary = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'A normal reply.'}]}}
        self.assertEqual(bridge.outgoing(ordinary), ordinary)


    def test_unsupported_backend_requests_get_an_error_instead_of_hanging(self):
        for bridge, message in (
            (protocol.ClaudeProtocol(), {'type': 'control_request', 'request_id': 'one',
                                        'request': {'subtype': 'hook_callback'}}),
        ):
            self.assertIsNone(bridge.outgoing(message))
            self.assertEqual(len(bridge.replies), 1)
            self.assertIn('unavailable', json.dumps(bridge.replies[0]))


    def test_claude_cancelled_permissions_cannot_be_answered(self):
        claude = protocol.ClaudeProtocol()
        claude.outgoing({'type': 'control_request', 'request_id': 'one', 'request': {'subtype': 'can_use_tool'}})
        claude.outgoing({'type': 'control_cancel_request', 'request_id': 'one'})
        with self.assertRaises(protocol.Rejected):
            claude.incoming({'type': 'control_response', 'response': {'request_id': 'one', 'subtype': 'error'}})

    def test_claude_auto_and_explicit_manual_choice(self):
        claude = protocol.ClaudeProtocol()
        for mode in ('auto', 'default'):
            message = {'type': 'control_request', 'request_id': mode,
                       'request': {'subtype': 'set_permission_mode', 'mode': mode}}
            self.assertEqual(claude.incoming(message), message)

    def test_claude_conversation_ownership_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = protocol.ClaudeProtocol(protocol.Conversations(directory))
            bridge.outgoing({'type': 'system', 'subtype': 'init', 'session_id': 'ours'})
            restarted = protocol.Conversations(directory)
            self.assertEqual(restarted.require('ours'), 'ours')
            for identifier in ('foreign', '../auth.json', '/home/node/.claude/.credentials.json'):
                with self.assertRaises(protocol.Rejected):
                    restarted.require(identifier)


    def test_claude_initialization_excludes_executable_options(self):
        bridge = protocol.ClaudeProtocol()
        actual = bridge.incoming({'type': 'control_request', 'request_id': 'init', 'request': {
            'subtype': 'initialize', 'hooks': {'test': 'example'}, 'sdkMcpServers': {'test': {}}}})
        self.assertEqual(actual['request'], {'subtype': 'initialize'})
        turn = bridge.incoming({'type': 'user', 'message': {'role': 'user', 'content': 'Review this function.'}})
        self.assertEqual(turn['message']['content'], 'Review this function.')

    def test_claude_permission_response_must_match_a_pending_request(self):
        bridge = protocol.ClaudeProtocol()
        answer = {'type': 'control_response', 'response': {'subtype': 'success', 'request_id': 'permission-1',
                  'response': {'behavior': 'allow'}}}
        with self.assertRaises(protocol.Rejected):
            bridge.incoming(answer)
        bridge.outgoing({'type': 'control_request', 'request_id': 'permission-1',
                         'request': {'subtype': 'can_use_tool'}})
        self.assertEqual(bridge.incoming(answer), answer)
        with self.assertRaises(protocol.Rejected):
            bridge.incoming(answer)

    def test_permission_cannot_write_claude_settings(self):
        bridge = protocol.ClaudeProtocol()
        bridge.outgoing({'type': 'control_request', 'request_id': 'one',
                         'request': {'subtype': 'can_use_tool'}})
        answer = bridge.incoming({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': 'one', 'response': {
                'behavior': 'allow', 'updatedInput': {'command': 'pwd'},
                'updatedPermissions': [{'type': 'setMode', 'mode': 'bypassPermissions'}]}}})
        self.assertNotIn('updatedPermissions', answer['response']['response'])
        self.assertEqual(answer['response']['response']['behavior'], 'deny')
        self.assertTrue(answer['response']['response']['interrupt'])
        self.assertNotIn('one', bridge.pending_permissions)

    def test_claude_session_permission_applies_only_to_workspace_tools(self):
        bridge = protocol.ClaudeProtocol()
        for index, name in enumerate(('mcp__factory_probe__workspace_shell', 'Bash')):
            bridge.outgoing({'type': 'control_request', 'request_id': index,
                             'request': {'subtype': 'can_use_tool'}})
            changes = [{'type': 'addRules', 'destination': 'session', 'behavior': 'allow',
                        'rules': [{'toolName': name}]}]
            answer = bridge.incoming({'type': 'control_response', 'response': {
                'subtype': 'success', 'request_id': index,
                'response': {'behavior': 'allow', 'updatedPermissions': changes}}})
            if index == 0:
                self.assertEqual(answer['response']['response']['updatedPermissions'], changes)
            else:
                self.assertEqual(answer['response']['response']['behavior'], 'deny')


    def test_claude_history_excludes_api_diagnostics_and_preserves_local_titles(self):
        bridge = component('editor_bridge')
        client = component('editor_client')
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = home / '.claude/projects/-workspace/conversation.jsonl'
            source.parent.mkdir(parents=True)
            records = [
                {'type': 'api-request-blob', 'authorization': 'synthetic-private-token'},
                {'type': 'assistant', 'uuid': 'one', 'message': {'content': [{'type': 'text', 'text': 'Answer'}]},
                 'internal_headers': 'synthetic-private-token'},
                {'type': 'user', 'uuid': 'two', 'message': {'content': 'Continue'}},
                {'type': 'assistant', 'uuid': 'three', 'parentUuid': 'two', 'isApiErrorMessage': True,
                 'message': {'content': [{'type': 'text', 'text': 'synthetic-private-token'}]}},
                {'type': 'system', 'uuid': 'retry', 'parentUuid': 'three', 'diagnostics': 'synthetic-private-token'},
                {'type': 'progress', 'uuid': 'progress', 'parentUuid': 'retry'},
                {'type': 'user', 'uuid': 'four', 'parentUuid': 'progress', 'message': {'content': 'Try again'}}]
            source.write_text(''.join(json.dumps(row) + '\n' for row in records))
            with patch.object(bridge.Path, 'home', return_value=home):
                exported = bridge.history_message('conversation')
            self.assertNotIn('synthetic-private-token', json.dumps(exported))
            self.assertEqual([row['type'] for row in exported['records']], ['assistant', 'user', 'assistant', 'user'])
            self.assertEqual(exported['records'][-2]['uuid'], exported['records'][-1]['parentUuid'])
            editor_home = home / 'editor'
            with patch.object(client.Path, 'home', return_value=editor_home):
                client.save_history(exported)
                local = editor_home / '.claude/projects/-workspace/conversation.jsonl'
                with local.open('a') as stream:
                    stream.write(json.dumps({'type': 'custom-title', 'customTitle': 'Review'}) + '\n')
                client.save_history(exported)
                self.assertIn('Review', local.read_text())
                with self.assertRaises(ValueError):
                    client.save_history({**exported, 'project': '../outside'})

    def test_claude_argv_cannot_install_settings_or_resume_a_foreign_chat(self):
        bridge = component('editor_bridge')
        with tempfile.TemporaryDirectory() as folder:
            conversations = bridge.Conversations()
            conversations.add('ours')
            trusted = ['--tools', '', '--strict-mcp-config', '--mcp-config', 'trusted',
                       '--permission-mode', 'auto']
            with patch.object(bridge, 'command', return_value=trusted.copy()), patch.object(bridge.os, 'chdir'):
                argv = bridge.arguments('claude', ['--settings', 'attacker', '--resume', 'ours'],
                                        {'working_directory': folder}, '', conversations)
                self.assertNotIn('attacker', argv)
                self.assertIn('stdio', argv)
                self.assertIn('ours', argv)
                self.assertEqual(argv[argv.index('--permission-mode') + 1], 'auto')
            with patch.object(bridge, 'command', return_value=trusted.copy()), patch.object(bridge.os, 'chdir'):
                argv = bridge.arguments('claude', ['--permission-mode', 'default'],
                                        {'working_directory': folder}, '', conversations)
                self.assertEqual(argv[argv.index('--permission-mode') + 1], 'default')
                with self.assertRaises(bridge.Rejected):
                    bridge.arguments('claude', ['--permission-mode', 'bypassPermissions'],
                                     {'working_directory': folder}, '', conversations)
            with patch.object(bridge, 'command', return_value=trusted.copy()), patch.object(bridge.os, 'chdir'):
                with self.assertRaises(ValueError):
                    bridge.arguments('claude', ['--resume', 'foreign'], {'working_directory': folder}, '', conversations)
                argv = bridge.arguments('claude', ['--resume=ours', '--permission-mode=default',
                    '--model=sonnet', '--effort=high'], {'working_directory': folder}, '', conversations)
                for flag, value in (('--resume', 'ours'), ('--permission-mode', 'default'),
                                    ('--model', 'sonnet'), ('--effort', 'high')):
                    self.assertEqual(argv[argv.index(flag) + 1], value)
                for supplied in (['--resume=foreign'], ['--permission-mode=bypassPermissions'],
                                 ['--resume=ours', '--resume', 'foreign'], ['--model'],
                                 ['--setting-sources', '--resume', 'ours']):
                    with self.assertRaises(ValueError):
                        bridge.arguments('claude', supplied, {'working_directory': folder}, '', conversations)


class NativeEditorLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = component('editor_bridge')
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        (root / 'settings.json').write_text(json.dumps({'working_directory': '/workspace'}))
        (root / 'context.md').write_text('')
        for target, name, value in ((self.bridge, 'SOURCE', root), (self.bridge.Path, 'home', lambda: root)):
            changed = patch.object(target, name, value)
            changed.start()
            self.addCleanup(changed.stop)
        self.tasks = set()
        self.agent = 'claude'
        async def connect(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                await self.bridge.serve_client(reader, writer, self.agent)
            finally:
                self.tasks.discard(task)
        self.server = await asyncio.start_server(connect, '127.0.0.1', 0)

    async def asyncTearDown(self):
        self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def connection(self, source):
        changed = patch.object(self.bridge, 'arguments', return_value=[sys.executable, '-u', '-c', source])
        changed.start()
        self.addCleanup(changed.stop)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        writer.write(b'{"args": ["--input-format", "stream-json"]}\n')
        await writer.drain()
        self.addCleanup(writer.close)
        return reader, writer

    async def test_process_exit_closes_connection_even_when_client_keeps_stdin_open(self):
        reader, writer = await self.connection('import sys; print("not JSON"); sys.exit(7)')
        result = json.loads(await asyncio.wait_for(reader.readline(), 10))
        self.assertEqual(result, {'type': 'factory_exit', 'code': 7})
        self.assertEqual(await asyncio.wait_for(reader.read(), 10), b'')

    async def test_claude_plugin_discovery_returns_without_starting_a_chat(self):
        self.agent = 'claude'
        for args, expected in ((['plugin', 'list', '--json'], []),
                (['plugin', 'list', '--json', '--available'], {'installed': [], 'available': []}),
                (['plugin', 'marketplace', 'list', '--json'], [])):
            with self.subTest(args=args), patch.object(self.bridge, 'arguments') as launch:
                reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
                try:
                    writer.write((json.dumps({'args': args}) + '\n').encode())
                    await writer.drain()
                    message = json.loads(await asyncio.wait_for(reader.readline(), 10))
                    self.assertEqual(message['code'], 0)
                    self.assertEqual(json.loads(message['stdout']), expected)
                    self.assertEqual(await asyncio.wait_for(reader.read(), 10), b'')
                    launch.assert_not_called()
                finally:
                    writer.close()
                    await writer.wait_closed()

    async def test_claude_unsupported_cli_command_fails_without_waiting_for_chat_input(self):
        self.agent = 'claude'
        reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
        try:
            writer.write(b'{"args":["plugin","install","unapproved"]}\n')
            await writer.drain()
            message = json.loads(await asyncio.wait_for(reader.readline(), 10))
            self.assertEqual(message['type'], 'factory_exit')
            self.assertEqual(message['code'], 1)
            self.assertEqual(await asyncio.wait_for(reader.read(), 10), b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_claude_trust_query_uses_only_the_configured_workspace(self):
        self.agent = 'claude'
        for args in (['--setting-sources', 'user', 'workspace-trust', '--json'],
                     ['--setting-sources=user', 'workspace-trust', '--json'],
                     ['/untrusted/extension/resources/native-binary/claude', '--setting-sources',
                      'user', 'workspace-trust', '--json']):
            with patch.object(self.bridge, 'workspace_directory', return_value=Path('/workspace')) as directory, \
                    patch.object(self.bridge.asyncio, 'create_subprocess_exec') as spawn:
                reader, writer = await asyncio.open_connection('127.0.0.1', self.server.sockets[0].getsockname()[1])
                try:
                    writer.write((json.dumps({'args': args}) + '\n').encode())
                    await writer.drain()
                    message = json.loads(await asyncio.wait_for(reader.readline(), 10))
                    self.assertEqual(message['code'], 0)
                    self.assertEqual(json.loads(message['stdout']), {'trusted': True, 'directory': '/workspace'})
                    directory.assert_called_once_with({'working_directory': '/workspace'})
                    spawn.assert_not_called()
                finally:
                    writer.close()
                    await writer.wait_closed()

    async def test_half_close_keeps_final_response(self):
        reader, writer = await self.connection('import sys, json; sys.stdin.read(); print(json.dumps({"method":"finished"}))')
        writer.write_eof()
        result = await asyncio.wait_for(reader.read(), 10)
        messages = [json.loads(line) for line in result.splitlines()]
        self.assertEqual(messages, [{'method': 'finished'}, {'type': 'factory_exit', 'code': 0}])

    async def test_explicit_disconnect_stops_process_and_releases_chat(self):
        source = ('import json, sys; print(json.dumps({"type":"system", "subtype":"init", "session_id":"owned"})); '
                  'sys.stdin.read()')
        reader, writer = await self.connection(source)
        await asyncio.wait_for(reader.readline(), 10)
        self.assertIn(('claude', 'owned'), self.bridge.OWNERS)
        writer.write(b'{"type":"factory_disconnect"}\n')
        await writer.drain()
        await asyncio.wait_for(reader.read(), 10)
        self.assertNotIn(('claude', 'owned'), self.bridge.OWNERS)

    async def test_reloaded_editor_takes_over_an_idle_chat(self):
        source = ('import json, sys; print(json.dumps({"type":"system", "subtype":"init", "session_id":"owned"})); '
                  'sys.stdin.read()')
        first, _ = await self.connection(source)
        await asyncio.wait_for(first.readline(), 10)
        second, _ = await self.connection(source)
        message = json.loads(await asyncio.wait_for(second.readline(), 10))
        self.assertEqual(message['subtype'], 'init')
        await asyncio.wait_for(first.read(), 10)
        self.assertIn(('claude', 'owned'), self.bridge.OWNERS)

    async def test_reloaded_editor_cannot_take_over_an_active_chat(self):
        source = ('import json, sys\nprint(json.dumps({"type":"system", "subtype":"init", "session_id":"owned"}))\n'
                  'for line in sys.stdin:\n message=json.loads(line); print(json.dumps({"type":"assistant","message":{"content":"working"}}))')
        first, writer = await self.connection(source)
        await asyncio.wait_for(first.readline(), 10)
        writer.write(b'{"type":"user","message":{"role":"user","content":"Work"}}\n')
        await writer.drain()
        await asyncio.wait_for(first.readline(), 10)
        second, _ = await self.connection(source)
        message = json.loads(await asyncio.wait_for(second.readline(), 10))
        self.assertEqual(message['type'], 'factory_exit')
        self.assertIn('open in another editor', message['error'])
        self.assertFalse(first.at_eof())


if __name__ == '__main__':
    unittest.main()
