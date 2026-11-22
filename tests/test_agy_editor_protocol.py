"""The native Antigravity chat connection must not become a controller API."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid


SOURCE = Path(__file__).parents[1] / 'pi_sandbox_factory/containers/primary'
sys.path.insert(0, str(SOURCE))
from agy_editor_protocol import AntigravityProtocol, Rejected, project


def uid():
    return str(uuid.uuid4())


class AntigravityProtocolTests(unittest.TestCase):
    def setUp(self):
        self.protocol = AntigravityProtocol()
        self.public, self.private, self.trajectory = uid(), uid(), uid()
        self.protocol.chats[self.public] = self.private
        self.protocol.models.add('MODEL_FIXTURE')

    def request(self, method, payload=None):
        return self.protocol.request(method, payload or {})

    def stream(self, steps, *, private=None, extra=None):
        return self.protocol.response('StreamAgentStateUpdates', {'update': {
            'conversationId': private or self.private, 'trajectoryId': self.trajectory,
            'mainTrajectoryUpdate': {'stepsUpdate': {
                'indices': list(range(len(steps))), 'totalLength': len(steps), 'steps': steps,
            }}, **(extra or {}),
        }}, {'conversationId': self.private})

    def permission(self, target='factory_workspace/workspace_shell', status='CORTEX_STEP_STATUS_WAITING'):
        return {'type': 'CORTEX_STEP_TYPE_GENERIC', 'status': status,
                'requestedInteraction': {'permission': {'resource': {'action': 'mcp', 'target': target}}}}

    def reply(self, **changes):
        payload = {'cascadeId': self.public, 'interaction': {
            'trajectoryId': self.trajectory, 'stepIndex': 0,
            'permission': {'allow': True, 'scope': 'PERMISSION_SCOPE_ONCE'},
        }}
        payload.update(changes)
        return self.request('HandleCascadeUserInteraction', payload)

    def test_private_hub_operations_are_not_available(self):
        for method in ('GetHubOAuthAccessToken', 'ReadFile', 'WriteFile', 'ExecuteCommand',
                       'GetApiKey', 'UpdateUserSettings', 'UpdateMcpServerConfig',
                       'GetCascadeTrajectory', 'GetAllCascadeTrajectories', 'NewUnknownMethod'):
            with self.subTest(method=method), self.assertRaises(Rejected):
                self.request(method, {'path': '/home/node/.gemini/credentials.json'})

    def test_project_lists_only_its_owned_conversations(self):
        public, private = uid(), uid()
        self.protocol.projects[public] = private
        result = self.protocol.project({'id': private, 'projectConversations': {'conversations': [
            {'conversationId': self.private, 'environmentId': 'private-environment'},
            {'conversationId': uid(), 'environmentId': 'foreign-environment'}]}})
        self.assertEqual(result['projectConversations'], {'conversations': [{'conversationId': self.public}]})
        self.assertNotIn('environment', json.dumps(result))

    def test_client_ids_do_not_claim_private_projects_or_chats(self):
        existing_private = uid()
        _, created = self.request('CreateProject', {'project': {
            'id': existing_private, 'name': 'arbitrary', 'settings': {'command': 'unsafe'},
            'projectResources': {'resources': [{'folderUri': 'file:///workspace'}]},
        }})
        self.assertNotEqual(created['project']['id'], existing_private)
        self.assertEqual(created['project']['settings'], {})
        _, started = self.request('StartCascade', {'cascadeId': self.private,
            'requestedModel': 'MODEL_FIXTURE', 'projectEnvConfig': {'projectId': existing_private,
            'defaultProjectEnvironment': {'command': 'unsafe'}}})
        self.assertNotEqual(started['cascadeId'], self.private)
        self.assertEqual(started['projectEnvConfig']['defaultProjectEnvironment'], {})
        with self.assertRaises(Rejected):
            self.request('CancelCascadeInvocation', {'cascadeId': uid()})

    def test_project_folder_cannot_point_to_controller_home(self):
        with self.assertRaises(Rejected):
            self.request('CreateProject', {'project': {'id': uid(),
                'projectResources': {'resources': [{'folderUri': 'file:///home/node'}]}}})
        with self.assertRaises(Rejected):
            self.request('ResolveFolder', {'folderUri': 'file:///home/node'})

    def test_message_cannot_change_tools_auth_or_agent(self):
        _, result = self.request('SendUserCascadeMessage', {
            'cascadeId': self.public, 'items': [{'text': 'Use the workspace'}], 'clientMessageId': uid(),
            'cascadeConfig': {'plannerConfig': {'planModel': 'MODEL_FIXTURE', 'useAiCredits': True,
                'toolConfig': {'runCommand': {'enabled': True}}, 'apiKey': 'never-forward'},
                'executorConfig': {'command': 'never-forward'}},
            'customAgentSpec': {'command': 'never-forward'}, 'editorState': {'path': '/home/node'},
        })
        self.assertNotIn('never-forward', json.dumps(result))
        self.assertNotIn('toolConfig', result['cascadeConfig']['plannerConfig'])
        self.assertFalse(result['cascadeConfig']['plannerConfig']['useAiCredits'])
        self.assertEqual(result['items'], [{'text': 'Use the workspace'}])
        for item in ({'file': '/home/node/private'}, {'text': 'x', 'scope': {'path': '/home/node'}}, {'image': {'uri': 'file:///home/node/private'}}):
            with self.subTest(item=item), self.assertRaises(Rejected):
                self.request('SendUserCascadeMessage', {'cascadeId': self.public, 'items': [item]})

    def test_models_come_from_subscription_discovery_and_unknown_fields_are_removed(self):
        result = self.protocol.response('GetUserStatus', {'userStatus': {
            'name': 'Example', 'apiKey': 'private-token', 'cascadeModelConfigData': {
                'clientModelConfigs': [{'label': 'New model', 'modelOrAlias': {'model': 'NEW_MODEL', 'token': 'private-token'},
                    'quotaInfo': {'remainingFraction': 0.6, 'resetTime': '2026-11-22T13:00:00Z', 'token': 'private-token'}}],
                'defaultOverrideModelConfig': {'modelOrAlias': {'model': 'NEW_MODEL', 'token': 'private-token'}, 'token': 'private-token'},
            }}})
        self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(self.protocol.model('NEW_MODEL'), 'NEW_MODEL')
        with self.assertRaises(Rejected):
            self.protocol.model('MODEL_FIXTURE')
        with self.assertRaises(Rejected):
            self.protocol.model(True)

    def test_permission_shape_and_one_time_reply(self):
        result = self.stream([self.permission()])
        permission = result['update']['mainTrajectoryUpdate']['stepsUpdate']['steps'][0]['requestedInteraction']['permission']
        self.assertEqual(permission['approvalScopeFilter'], ['PERMISSION_SCOPE_ONCE'])
        _, reply = self.reply()
        self.assertEqual(reply['cascadeId'], self.private)
        self.assertEqual(reply['interaction']['permission'], {'allow': True, 'scope': 'PERMISSION_SCOPE_ONCE'})
        # A second subscriber's repeated pending update cannot revive a consumed reply.
        self.stream([self.permission()])
        with self.assertRaises(Rejected):
            self.reply()

    def test_permission_cannot_enable_other_tools_or_stale_steps(self):
        for target in ('native/run_command', 'factory_workspace/browser', 'other/workspace_shell'):
            self.stream([self.permission(target)])
            with self.subTest(target=target), self.assertRaises(Rejected):
                self.reply()
        self.stream([self.permission()])
        self.stream([self.permission(status='CORTEX_STEP_STATUS_DONE')])
        with self.assertRaises(Rejected):
            self.reply()
        with self.assertRaises(Rejected):
            self.stream([self.permission()], private=uid())

    def test_permission_cannot_be_forged_for_another_chat_or_scope(self):
        self.stream([self.permission()])
        with self.assertRaises(Rejected):
            self.reply(cascadeId=uid())
        with self.assertRaises(Rejected):
            self.reply(interaction={'trajectoryId': self.trajectory, 'stepIndex': 0,
                                   'permission': {'allow': True, 'scope': 'PERMISSION_SCOPE_ALWAYS'}})
        self.assertTrue(self.reply()[1]['interaction']['permission']['allow'])

    def test_stream_keeps_chat_data_but_removes_configuration_and_diagnostics(self):
        result = self.stream([{
            'type': 'CORTEX_STEP_TYPE_PLANNER_RESPONSE', 'status': 'CORTEX_STEP_STATUS_DONE',
            'metadata': {'source': 'CORTEX_STEP_SOURCE_MODEL', 'internalMetadata': {'token': 'private-token'}},
            'plannerResponse': {'response': 'Finished', 'passbackContext': [{'token': 'private-token'}]},
            'error': {'userErrorMessage': 'private-token', 'httpHeaders': {'Authorization': 'private-token'}},
            'permissions': {'credentials': 'private-token'},
        }], extra={'executorMetadata': {'executionId': 'safe', 'genSvcRequest': {'token': 'private-token'}},
                   'contextWindowUsage': {'estimatedTokensUsed': 42, 'tokenGroups': [
                       {'name': 'Tools', 'numTokens': 3, 'private': 'private-token'}]},
                   'activeHook': {'command': 'private-token'}})
        encoded = json.dumps(result)
        self.assertNotIn('private-token', encoded)
        self.assertIn('Finished', encoded)
        self.assertEqual(result['update']['conversationId'], self.public)

    def test_generic_workspace_tool_keeps_command_and_result_without_private_metadata(self):
        step = {'type': 'CORTEX_STEP_TYPE_GENERIC', 'status': 'CORTEX_STEP_STATUS_DONE',
                'generic': {'args': {'ServerName': 'factory_workspace', 'ToolName': 'workspace_shell',
                                     'Arguments': '{"command":"pwd"}', 'Private': 'private-token'},
                            'result': {'result': '/workspace', 'fullOutputUri': 'file:///private-token',
                                       'metadata': {'token': 'private-token'}, 'payload': {'token': 'private-token'}}}}
        result = self.protocol.step(step, self.private, self.trajectory, 0)
        self.assertEqual(result['generic'], {'args': {'ServerName': 'factory_workspace',
                         'ToolName': 'workspace_shell', 'Arguments': '{"command":"pwd"}'},
                         'result': {'result': '/workspace'}})
        self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(result['metadata']['toolCall'], {'name': 'call_mcp_tool'})
        step['generic']['args']['ServerName'] = 'other'
        self.assertNotIn('generic', self.protocol.step(step, self.private, self.trajectory, 0))
        step['generic']['args'].update(ServerName='factory_workspace', ToolName='browser')
        self.assertNotIn('generic', self.protocol.step(step, self.private, self.trajectory, 0))

    def test_project_and_chat_lists_exclude_other_controller_sessions(self):
        public, private = uid(), uid()
        self.protocol.projects[public] = private
        self.assertEqual(self.protocol.response('ListProjects', {'projectIds': [private, uid()]}), {'projectIds': [public]})
        summaries = self.protocol.response('JetboxSubscribeToSummaries', {'updates': {
            self.private: {'title': 'My chat', 'credentials': 'private-token'}, uid(): {'title': 'Other chat'},
        }})
        self.assertEqual(summaries['updates'], {self.public: {'title': 'My chat'}})

    def test_saved_preferences_never_update_native_configuration(self):
        kind, value = self.request('JetboxWriteState', {'appState': {
            'postOnboarding': {}, 'seenNuxs': {'uids': [77]},
            'userConfig': {'apiKey': 'private-token', 'toolPolicy': 'allow everything'},
        }})
        self.assertEqual((kind, value), ('local', {}))
        self.assertNotIn('private-token', json.dumps(self.request('JetboxSubscribeToState')))

    def test_reconnect_retains_owned_chats_without_storing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            self.protocol.path = Path(directory) / 'antigravity-editor.json'
            self.protocol.save()
            reopened = AntigravityProtocol(directory)
            self.assertEqual(reopened.owned(self.public), self.private)
            self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(reopened.models)  # Refresh subscription models after reconnect.
            self.assertFalse(reopened.pending)  # Learn waiting steps from the native stream again.

    def test_wrong_nested_shapes_are_rejected(self):
        with self.assertRaises(Rejected):
            project({'approvalScopeFilter': 'PERMISSION_SCOPE_ONCE'}, {'approvalScopeFilter': [str]})
        with self.assertRaises(Rejected):
            project({'count': True}, {'count': int})


if __name__ == '__main__':
    unittest.main()
