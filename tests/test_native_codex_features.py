"""Feature-check failures retain useful diagnostics without exposing credentials."""
import io
import queue
import time
import unittest
from unittest.mock import Mock

from native_codex_features import AppServer, sanitized


class FeatureDiagnosticsTests(unittest.TestCase):
    def server(self):
        server = AppServer.__new__(AppServer)
        server.container = 'disposable-workspace'
        server.private_values = ['synthetic-refresh-secret']
        server.stderr = ['connection reset by peer\n']
        server.operation = 'startup'
        server.process = Mock()
        server.process.poll.return_value = 17
        server.process.stdin = io.StringIO()
        server.sequence = 0
        server.messages = queue.Queue()
        server.events = []
        return server

    def test_rpc_error_includes_operation_id_code_cause_and_stderr(self):
        server = self.server()
        server.messages.put({'id': 1, 'error': {'code': -32602,
            'message': 'Unsupported parameter browserMode; credential synthetic-refresh-secret'}})
        with self.assertRaises(RuntimeError) as failure:
            server.call('thread/start', {'browserMode': True}, time.monotonic() + 10)
        detail = str(failure.exception)
        for expected in ('RPC thread/start, request 1', 'disposable-workspace', '-32602',
                         'Unsupported parameter browserMode', 'exit status 17', 'connection reset by peer'):
            self.assertIn(expected, detail)
        self.assertNotIn('synthetic-refresh-secret', detail)

    def test_failed_turn_keeps_status_and_upstream_error(self):
        server = self.server()
        server.messages.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1',
            'turn': {'id': 'turn-1', 'status': 'failed', 'error': {
                'message': 'HTTP 429 quota exhausted; Bearer synthetic-bearer'}}}})
        with self.assertRaises(RuntimeError) as failure:
            server.wait_turn('thread-1', 'turn-1', time.monotonic() + 10)
        detail = str(failure.exception)
        self.assertIn('turn turn-1 in thread thread-1', detail)
        self.assertIn('HTTP 429 quota exhausted', detail)
        self.assertNotIn('synthetic-bearer', detail)

    def test_stderr_and_unknown_signed_tokens_are_redacted(self):
        server = self.server()
        server.process.stderr = io.StringIO('refresh rejected: synthetic-refresh-secret\n')
        server._read_stderr()
        self.assertNotIn('synthetic-refresh-secret', ''.join(server.stderr))
        self.assertEqual(sanitized('failure eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.signature'),
                         'failure [redacted JWT]')


if __name__ == '__main__':
    unittest.main()
