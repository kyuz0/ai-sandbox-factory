"""Native hooks must deliver updates without losing cancelled or overlapping turns."""
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'ai_sandbox_factory/containers/primary-worker/sandbox-update.py'
spec = importlib.util.spec_from_file_location('factory_sandbox_update_hook', SOURCE)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class SandboxUpdateHooksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = self.root / 'sandbox-state.json'
        self.receipts = self.root / 'receipts'
        self.agy_initial_steps = {}
        self.state('one', 'allowlist', Browser='Off')

    def state(self, revision, network, **values):
        self.snapshot.write_text(json.dumps({'revision': revision, 'network': network,
            'values': {'Network': 'Restricted' if network == 'allowlist' else 'Unrestricted', **values}}))

    def payload(self, agent, turn='one', session='chat', stage='before'):
        if agent == 'agy':
            initial_steps = self.agy_initial_steps.setdefault(turn, 1 + 3 * len(self.agy_initial_steps))
            return {'conversationId': session, 'initialNumSteps': initial_steps, 'invocationNum': 0,
                    'hookEventName': 'PreInvocation' if stage == 'before' else 'PostInvocation'}
        return {'session_id': session, 'turn_id' if agent == 'codex' else 'prompt_id': turn}

    def call(self, agent, stage='before', turn='one', session='chat', client='process-one', payload=None):
        return hook.handle(agent, stage, self.payload(agent, turn, session, stage) if payload is None else payload,
                           snapshot=self.snapshot, receipts=self.receipts, client=client)

    def text(self, response):
        return (response.get('hookSpecificOutput', {}).get('additionalContext', '')
                or '\n'.join(step['systemMessage']['systemMessage'] for step in response.get('injectSteps', [])))

    def test_successful_turn_acknowledges_only_the_delivered_settings(self):
        for agent in ('codex', 'claude', 'agy'):
            with self.subTest(agent=agent):
                self.assertIn('Network: Restricted', self.text(self.call(agent)))
                self.call(agent, 'after')
                self.assertEqual(self.call(agent, turn='next'), {})

    def test_receipts_keep_only_the_delivered_public_summary(self):
        state = json.loads(self.snapshot.read_text())
        state['details'] = {'Additional allowed destinations': 'full-list-not-delivered'}
        self.snapshot.write_text(json.dumps(state))
        self.call('codex'); self.call('codex', 'after')
        record = next(self.receipts.glob('*.json')).read_text()
        self.assertNotIn('full-list-not-delivered', record)

    def test_damaged_receipt_redelivers_current_settings(self):
        self.call('codex')
        path = next(self.receipts.glob('*.json'))
        for record in ({'acknowledged': {'revision': 'one', 'values': None}},
                       {'pending': {'turn': 'one', 'state': None}},
                       {'pending': {'turn': 'one', 'state': {'revision': 'one'}}},
                       {'pending': {'turn': 'one', 'state': {'revision': 'one', 'values': {}}, 'changes': 'invalid'}}):
            path.write_text(json.dumps(record))
            self.assertIn('Network: Restricted', self.text(self.call('codex')))

    def test_existing_and_independent_chats_get_current_settings_after_resume(self):
        for agent in ('codex', 'claude', 'agy'):
            self.call(agent); self.call(agent, 'after')
            self.assertIn('Network: Restricted', self.text(self.call(agent, client='reopened-process')))
            self.assertIn('Network: Restricted', self.text(self.call(agent, session='other-chat')))

    def test_mode_changes_replace_old_factory_context_in_both_directions(self):
        for agent in ('codex', 'claude', 'agy'):
            self.state('one', 'allowlist')
            self.call(agent); self.call(agent, 'after')
            self.state('two', 'unrestricted')
            text = self.text(self.call(agent, turn='two'))
            self.assertIn('Network: Unrestricted', text)
            self.assertNotIn('gateway', text)
            self.assertIn('/opt/sandbox/context.md', text)
            self.assertIn('Factory updated /opt/sandbox/context.md with the full current configuration', text)
            self.assertIn('Re-read it instead of relying on earlier tool output.', text)
            self.assertIn('replace older Factory settings', text)
            self.call(agent, 'after', turn='two')
            self.state('three', 'allowlist')
            self.assertIn('Network: Restricted', self.text(self.call(agent, turn='three')))

    def test_cancelled_and_failed_turns_leave_updates_available_for_retry(self):
        for agent in ('codex', 'claude', 'agy'):
            self.assertTrue(self.call(agent, turn='cancelled'))
            self.assertTrue(self.call(agent, turn='retry'))
            self.call(agent, 'after', turn='cancelled')
            self.assertTrue(self.call(agent, turn='another-retry'))
            self.call(agent, 'after', turn='another-retry')
            self.assertEqual(self.call(agent, turn='done'), {})

    def test_a_change_during_inference_is_not_acknowledged_by_the_older_turn(self):
        for agent in ('codex', 'claude', 'agy'):
            self.state('one', 'allowlist')
            self.call(agent)
            self.state('two', 'unrestricted')
            self.call(agent, 'after')
            self.assertIn('Network: Unrestricted', self.text(self.call(agent, turn='new')))

    def test_reverting_settings_after_an_inflight_or_cancelled_turn_delivers_the_reversion(self):
        for agent in ('codex', 'claude', 'agy'):
            for complete in (False, True):
                with self.subTest(agent=agent, completed=complete):
                    self.state('one', 'allowlist', Browser='Off')
                    self.call(agent, turn='initial'); self.call(agent, 'after', turn='initial')
                    self.state('two', 'unrestricted', Browser='On')
                    self.call(agent, turn='in-flight')
                    self.state('one', 'allowlist', Browser='Off')
                    if complete:
                        self.call(agent, 'after', turn='in-flight')
                    text = self.text(self.call(agent, turn='reverted'))
                    self.assertIn('Network: Restricted', text)
                    self.assertIn('Browser: Off', text)
                    # Repeated cancellation must not erase the uncertainty of
                    # the earlier Unrestricted context saved in this chat.
                    text = self.text(self.call(agent, turn='reverted-again'))
                    self.assertIn('Network: Restricted', text)
                    self.assertIn('Browser: Off', text)
                    self.call(agent, 'after', turn='reverted-again')
                    self.call(agent, 'after', turn='reverted')
                    self.assertEqual(self.call(agent, turn='unchanged'), {})

    def test_missing_process_identity_repeats_without_writing_receipts(self):
        for agent in ('codex', 'claude', 'agy'):
            with patch.object(hook, 'client_identity', return_value=None):
                for turn in ('one', 'two'):
                    response = hook.handle(agent, 'before', self.payload(agent, turn),
                        snapshot=self.snapshot, receipts=self.receipts)
                    self.assertIn('Network: Restricted', self.text(response))
                    hook.handle(agent, 'after', self.payload(agent, turn), snapshot=self.snapshot, receipts=self.receipts)
            self.assertFalse(self.receipts.exists())

    def test_hook_errors_are_actionable_valid_json_and_do_not_fail_the_client(self):
        handle = hook.handle
        for malformed in (False, True):
            self.snapshot.unlink(missing_ok=True)
            if malformed:
                self.snapshot.write_text('{invalid json')
            for agent in ('codex', 'claude', 'agy'):
                for stage in ('before', 'after'):
                    output, errors = io.StringIO(), io.StringIO()
                    def invoke(*args):
                        return handle(*args, snapshot=self.snapshot, receipts=self.receipts)
                    with (patch.object(hook, 'SNAPSHOT', self.snapshot),
                          patch.object(hook, 'handle', side_effect=invoke),
                          patch('sys.argv', ['sandbox-update.py', agent, stage]),
                          patch('sys.stdin', io.StringIO(json.dumps(self.payload(agent)))),
                          redirect_stdout(output), redirect_stderr(errors)):
                        hook.main()
                    parsed = json.loads(output.getvalue())
                    self.assertIn(str(self.snapshot), errors.getvalue())
                    if stage == 'before':
                        text = self.text(parsed)
                        self.assertIn(str(self.snapshot), text)
                        self.assertIn('Reopen the sandbox from Factory', text)
                    else:
                        self.assertEqual(parsed, {})

    def test_malformed_native_input_names_the_hook_payload_instead_of_the_snapshot(self):
        for agent in ('codex', 'claude', 'agy'):
            for supplied in ('not json', '[]', 'null'):
                for stage in ('before', 'after'):
                    output, errors = io.StringIO(), io.StringIO()
                    with (patch('sys.argv', ['sandbox-update.py', agent, stage]),
                          patch('sys.stdin', io.StringIO(supplied)),
                          redirect_stdout(output), redirect_stderr(errors)):
                        hook.main()
                    parsed = json.loads(output.getvalue())
                    self.assertIn(f'{agent} sandbox-update hook input', errors.getvalue())
                    self.assertNotIn(str(hook.SNAPSHOT), errors.getvalue())
                    if supplied != 'not json':
                        self.assertIn('JSON', errors.getvalue())
                    if stage == 'before':
                        self.assertIn('JSON event object', self.text(parsed))
                    else:
                        self.assertEqual(parsed, {})

    def test_late_completions_cannot_acknowledge_or_regress_another_turn(self):
        for agent in ('codex', 'claude', 'agy'):
            self.state('one', 'allowlist')
            self.call(agent, turn='old')
            self.state('two', 'unrestricted')
            self.call(agent, turn='new')
            self.call(agent, 'after', turn='old')
            self.assertTrue(self.call(agent, turn='latest'))
            self.call(agent, 'after', turn='latest')
            self.call(agent, 'after', turn='new')
            self.assertEqual(self.call(agent, turn='unchanged'), {})

    def test_multiple_edits_are_compared_with_the_chat_acknowledgment(self):
        self.call('codex'); self.call('codex', 'after')
        self.state('two', 'allowlist', Browser='On')
        self.state('three', 'unrestricted', Browser='On', Connectors='SSH & Git')
        text = self.text(self.call('codex', turn='latest'))
        for value in ('Network: Unrestricted', 'Browser: On', 'Connectors: SSH & Git'):
            self.assertIn(value, text)

    def test_notices_contain_only_changed_settings_and_the_reference(self):
        for agent in ('codex', 'claude', 'agy'):
            self.state('one', 'allowlist', Browser='On', Folders='/workspace (read and write)')
            self.call(agent); self.call(agent, 'after')
            self.state('two', 'allowlist', Browser='Off', Folders='/workspace (read and write)')
            text = self.text(self.call(agent, turn='changed'))
            self.assertIn('- Browser: Off', text)
            for unchanged in ('Network:', 'Folders:', 'gateway', 'sign-in transport'):
                self.assertNotIn(unchanged, text)
            self.assertIn('/opt/sandbox/context.md', text)
            # A repeated cancellation must keep the same narrow correction.
            self.assertEqual(self.text(self.call(agent, turn='retry')), text)

    def test_prompt_budget_keeps_all_changed_labels_and_full_values_out_of_the_notice(self):
        values = {f'Configured feature {index}': 'Long fixture value ' * 200 for index in range(36)}
        state = {'revision': 'many', 'network': 'unrestricted', 'values': values}
        text = hook.message(state)
        self.assertLessEqual(len(text.encode('utf-8')), hook.MESSAGE_BYTES)
        for label in values:
            self.assertIn(label + ':', text)
        self.assertNotIn('Long fixture value', text)
        state['values'] = {label: '漢字🌍' * 10 for label in values}
        self.assertLessEqual(len(hook.message(state).encode('utf-8')), hook.MESSAGE_BYTES)

    def test_reference_only_guidance_changes_have_one_short_notice(self):
        state = {'revision': 'two', 'network': 'allowlist', 'values': {'Network': 'Restricted'}}
        text = hook.message(state, {**state, 'revision': 'one'})
        self.assertIn('- Sandbox guidance: updated', text)
        self.assertNotIn('- Network:', text)

    def test_antigravity_error_stop_does_not_acknowledge_delivery(self):
        self.call('agy')
        self.call('agy', 'after', payload={**self.payload('agy'), 'hookEventName': 'Stop',
                                           'terminationReason': 'ERROR'})
        self.assertTrue(self.call('agy', turn='retry'))
        self.call('agy', 'after', turn='retry')
        self.assertEqual(self.call('agy', payload={**self.payload('agy', 'retry'), 'invocationNum': 1}), {})

    def test_missing_turn_correlation_favors_redelivery(self):
        for agent in ('codex', 'claude', 'agy'):
            payload = {'session_id': 'chat', 'conversationId': 'chat'}
            self.assertTrue(self.call(agent, payload=payload))
            self.call(agent, 'after', payload=payload)
            self.assertTrue(self.call(agent, payload=payload))

    def test_receipt_write_failure_does_not_discard_current_settings(self):
        with patch.object(hook, 'write_record', side_effect=OSError('fixture disk full')):
            self.assertIn('Network: Restricted', self.text(self.call('claude')))
        self.assertTrue(self.call('claude', turn='retry'))

    def test_non_path_session_ids_are_supported_without_using_them_as_paths(self):
        self.assertTrue(self.call('codex', session='../../another/chat with spaces'))
        self.assertTrue(all(path.parent == self.receipts for path in self.receipts.iterdir()))


if __name__ == '__main__':
    unittest.main()
