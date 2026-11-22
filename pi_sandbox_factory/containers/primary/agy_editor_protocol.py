"""Expose Antigravity chat operations without exposing its controller APIs.

The native hub also serves credentials, files, terminals and configuration. None
of those APIs belongs on the workspace connection. Requests are reconstructed;
responses are projected recursively, including nested native chat metadata.
"""
import base64
import binascii
import json
from pathlib import Path
import uuid

from editor_protocol import Rejected, object_value, text


def attachments(values, *, images=False):
    """Forward uploaded bytes only, never a URI resolved by the private hub."""
    if not isinstance(values, list):
        raise Rejected('Invalid chat attachments')
    result = []
    for supplied in values:
        item = object_value(supplied)
        field = 'base64Data' if images else 'inlineData'
        data = item.get(field)
        mime = text(item.get('mimeType', ''))
        if (not isinstance(data, str) or not data or item.get('uri') or item.get('blobref') or
                not mime or any(ord(c) < 33 or ord(c) > 126 for c in mime)):
            raise Rejected('Attach a file from your computer; private file references are unavailable')
        try:
            base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise Rejected('Invalid attachment data') from None
        safe = {field: data, 'mimeType': mime}
        label = 'caption' if images else 'description'
        if label in item:
            safe[label] = text(item[label])
        if not images and item.get('thumbnail'):
            thumbnail = text(item['thumbnail'])
            try:
                base64.b64decode(thumbnail, validate=True)
            except (ValueError, binascii.Error):
                raise Rejected('Invalid attachment preview') from None
            safe['thumbnail'] = thumbnail
        result.append(safe)
    return result


def identifier(value):
    value = text(value)
    try:
        valid = str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise Rejected('Invalid conversation identifier')
    return value


def project(value, schema):
    """Keep only declared fields and their declared JSON shapes."""
    if isinstance(schema, dict):
        value = object_value(value)
        return {key: project(value[key], child) for key, child in schema.items() if key in value}
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise Rejected('Expected a list')
        return [project(item, schema[0]) for item in value]
    allowed = schema if isinstance(schema, tuple) else (schema,)
    if type(value) not in allowed:
        raise Rejected('Unexpected native response field type')
    return value


def strings(names):
    return dict.fromkeys(names.split(), str)


ENUM = (str, int)
NUMBER = (int, float)
MODEL_ID = {'model': ENUM, 'alias': ENUM}
SLICE = {'startIndex': int, 'endIndexExclusive': int}
MODEL = {
    **strings('label description tagTitle tagDescription modelId betaWarningMessage'),
    'modelOrAlias': MODEL_ID, 'creditMultiplier': NUMBER, 'pricingType': ENUM,
    **dict.fromkeys('disabled supportsImages supportsLegacy isPremium isBeta isRecommended'.split(), bool),
    'provider': ENUM, 'quotaInfo': {'remainingFraction': NUMBER, 'resetTime': str},
}
MODEL_SORT = {'name': str, 'groups': [{'groupName': str, 'modelLabels': [str]}]}
MODELS = {
    'clientModelConfigs': [MODEL], 'battleModeModelConfigs': [MODEL],
    'clientModelSorts': [MODEL_SORT], 'battleModeModelSorts': [MODEL_SORT],
    'defaultOverrideModelConfig': {'modelOrAlias': MODEL_ID, 'versionId': str},
}
APP = {
    'postOnboarding': {}, 'agentOnboardingCompleted': ENUM,
    'seenNuxs': {'uids': [int]}, 'lastSelectedAgentModel': MODEL_ID,
}
EXECUTOR = {'terminationReason': ENUM, 'numGeneratorInvocations': int,
            'lastStepIdx': int, 'executionId': str}
METADATA = {
    **strings('createdAt viewableAt startedAt completedAt finishedGeneratingAt lastCompletedChunkAt executionId'),
    'source': ENUM, 'generatorModel': ENUM, 'requestedModel': MODEL_ID,
    'sourceTrajectoryStepInfo': {
        **strings('trajectoryId cascadeId'), 'stepIndex': int, 'metadataIndex': int,
    },
}
TOOL_CALL = strings('id name argumentsJson')
PLANNER = {**strings('response modifiedResponse thinking thinkingDuration messageId'), 'stopReason': ENUM}
IMAGE = strings('base64Data mimeType caption')
MEDIA = strings('inlineData mimeType description thumbnail')
USER = {'images': [IMAGE], 'media': [MEDIA], **strings('userResponse clientMessageId query'), 'isQueuedMessage': bool, 'items': [{'text': str}]}
CONTEXT_USAGE = {
    **dict.fromkeys('generatorMetadataIndex lastStepIndex estimatedTokensUsed maxContextTokens'.split(), int),
    'estimatedAfterCompaction': bool,
    'tokenGroups': [{'name': str, 'type': ENUM, 'source': ENUM, 'numTokens': int}],
}
GENERATOR = {
    'stepIndices': [int], 'executionId': str,
    'plannerConfig': {'planModel': ENUM, 'requestedModel': MODEL_ID, 'modelName': str},
    'chatModel': {'model': ENUM},
}
EMPTY = frozenset({
    'GetAgentScripts', 'GetAllSkills', 'GetAllRules', 'GetAllWorkflows', 'GetCustomizationStates',
    'GetSlashCommands', 'ListRemotePlugins', 'SubscribeToPlugins', 'ListMcpPrompts',
    'ListContextProviders', 'SubscribeToMcpServerStates', 'GetCascadeNuxes', 'RecordAnalyticsEvent',
    'WarmUpEnvironment', 'FetchAdminControls', 'GetRemoteControlInfo', 'GetBuildWithGooglePlugins',
    'RecordError', 'GetMendelFlags', 'GetRepoInfos', 'GetTurnDiff',
})


class AntigravityProtocol:
    def __init__(self, directory=None, *, cwd='/workspace', browser=False):
        self.path = Path(directory) / 'antigravity-editor.json' if directory else None
        self.cwd = cwd
        self.workspace_uri = Path(cwd).as_uri()
        self.tools = {'factory_workspace/workspace_shell'}
        if browser:
            self.tools.add('factory_workspace/browser')
        self.chats = {}
        self.projects = {}
        self.app = {}
        self.models = set()
        self.attachments = {}
        self.pending = set()
        self.resolved = set()
        if self.path and self.path.exists():
            saved = object_value(json.loads(self.path.read_text()))
            for name, target in (('chats', self.chats), ('projects', self.projects)):
                target.update({identifier(k): identifier(v) for k, v in object_value(saved[name]).items()})
            self.app = project(saved.get('app', {}), APP)
            self.attachments = {key: attachments([value])[0] for key, value in
                                object_value(saved.get('attachments', {})).items()}

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'chats': self.chats, 'projects': self.projects, 'app': self.app, 'attachments': self.attachments}))
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def owned(self, value, *, projects=False):
        mapping = self.projects if projects else self.chats
        value = identifier(value)
        if value not in mapping:
            raise Rejected('This conversation was not created in this sandbox')
        return mapping[value]

    def public_id(self, value, *, projects=False):
        mapping = self.projects if projects else self.chats
        return next((key for key, private in mapping.items() if private == value), None)

    def model(self, value):
        if type(value) not in ENUM or value not in self.models:
            raise Rejected('Choose an available subscription model')
        return value

    def request(self, method, supplied):
        """Return (local/remote, payload); never forward the supplied object."""
        p = object_value(supplied)
        if method == 'SaveMediaAsArtifact':
            media = attachments([p.get('media')])[0]
            uri = 'factory-attachment://' + str(uuid.uuid4())
            self.attachments[uri] = media
            self.save()
            return 'local', {'uri': uri}
        if method == 'DeleteMediaArtifact':
            uri = text(p.get('uri'))
            if uri not in self.attachments:
                raise Rejected('This attachment was not uploaded in this sandbox')
            del self.attachments[uri]
            self.save()
            return 'local', {'success': True}
        if method in EMPTY:
            return 'local', {}
        if method == 'GetLocalUserInfo':
            return 'local', {'username': 'node', 'homeDirUri': 'file:///home/node'}
        if method == 'GetServerConfiguration':
            return 'local', {'config': {'antigravityHub': True, 'standalone': True}}
        if method == 'FetchUserInfo':
            return 'local', {'userSettings': {}}
        if method in ('ResolveFolder', 'StatUri'):
            if p.get('folderUri' if method == 'ResolveFolder' else 'uri') != self.workspace_uri:
                raise Rejected('Use the configured workspace')
            return 'local', ({'resolvedUri': self.workspace_uri} if method == 'ResolveFolder' else {'isDirectory': True})
        if method == 'JetboxWriteState':
            # Presentation preferences are private to this editor. Never forward
            # native userConfig, model credentials or custom executable settings.
            self.app.update(project(p.get('appState', {}), APP))
            self.save()
            return 'local', {}
        if method == 'JetboxSubscribeToState':
            return 'local', {'appState': self.app.copy()}
        if method in ('HasAuthToken', 'GetAuthStatus', 'GetUserStatus'):
            return 'remote', {}
        if method == 'CreateProject':
            q = object_value(p.get('project'))
            public = identifier(q.get('id'))
            if public in self.projects:
                return 'local', {}
            resources = object_value(q.get('projectResources', {})).get('resources', [])
            if resources != [{'folderUri': self.workspace_uri}]:
                raise Rejected('Use the configured workspace')
            self.projects[public] = str(uuid.uuid4())
            self.save()
            return 'remote', {'project': {
                'id': self.projects[public], 'name': Path(self.cwd).name,
                'projectResources': {'resources': [{'folderUri': self.workspace_uri}]},
                'settings': {}, 'isWorkspaceOnly': False,
            }}
        if method in ('ListProjects', 'ProjectUpdatesStream', 'JetboxSubscribeToSummaries'):
            return 'remote', {}
        if method == 'ReadProject':
            return 'remote', {'id': self.owned(p.get('id'), projects=True)}
        if method == 'ReadProjects':
            ids = project(p.get('ids', []), [str])
            return 'remote', {'ids': [self.owned(value, projects=True) for value in ids]}
        if method == 'StartCascade':
            public = identifier(p.get('cascadeId'))
            if public in self.chats:
                return 'local', {'cascadeId': public}
            environment = object_value(p.get('projectEnvConfig'))
            project_id = self.owned(environment.get('projectId'), projects=True)
            selected = self.model(p.get('requestedModel'))
            self.chats[public] = str(uuid.uuid4())
            self.save()
            return 'remote', {
                'cascadeId': self.chats[public], 'requestedModel': selected,
                'source': 'CORTEX_TRAJECTORY_SOURCE_VSCODE',
                'projectEnvConfig': {'projectId': project_id, 'defaultProjectEnvironment': {}},
            }
        if method == 'SendUserCascadeMessage':
            items = p.get('items')
            if not isinstance(items, list) or any(
                    not isinstance(item, dict) or set(item) != {'text'} for item in items):
                raise Rejected('Attach a file from your computer or ask the agent to read it from the workspace')
            supplied_media = p.get('media', [])
            if not isinstance(supplied_media, list):
                raise Rejected('Invalid chat attachments')
            media = []
            for item in supplied_media:
                uri = object_value(item).get('uri')
                if uri:
                    if uri not in self.attachments:
                        raise Rejected('This attachment was not uploaded in this sandbox')
                    media.append(self.attachments[uri].copy())
                else:
                    media.extend(attachments([item]))
            images = attachments(p.get('images', []), images=True)
            if not items and not media and not images:
                raise Rejected('Enter a message or attach a file')
            config = object_value(p.get('cascadeConfig', {}))
            planner = object_value(config.get('plannerConfig', {}))
            selected = self.model(planner.get('planModel'))
            result = {
                'cascadeId': self.owned(p.get('cascadeId')), 'items': project(items, [{'text': str}]),
                'media': media, 'images': images,
                'clientMessageId': identifier(p.get('clientMessageId')),
                'cascadeConfig': {
                    'plannerConfig': {'planModel': selected, 'useAiCredits': False, 'supportsLatexRendering': True},
                    'executorConfig': {'useCoreDirect': True},
                },
                'customAgentSpec': {'builtinAgent': {'defaultAgent': {
                    'isGoogle': False, 'isInteractive': True, 'surface': 'AGENT_SURFACE_HUB',
                }}},
            }
            if p.get('deliveryStrategy') == 'MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION':
                result['deliveryStrategy'] = p['deliveryStrategy']
            return 'remote', result
        if method == 'CancelCascadeInvocation':
            return 'remote', {'cascadeId': self.owned(p.get('cascadeId'))}
        if method == 'StreamAgentStateUpdates':
            return 'remote', {
                'conversationId': self.owned(p.get('conversationId')),
                'subscriberId': identifier(p.get('subscriberId')),
                'initialStepsPageBounds': project(p.get('initialStepsPageBounds', {}), SLICE),
                'trajectoryVerbosity': 'CLIENT_TRAJECTORY_VERBOSITY_PROD_UI',
                'initialGeneratorMetadatasPageBounds': project(p.get('initialGeneratorMetadatasPageBounds', {}), SLICE),
                'initialExecutorMetadatasPageBounds': {'endIndexExclusive': 0},
            }
        if method == 'RequestAgentStatePageUpdate':
            return 'remote', {
                'conversationId': self.owned(p.get('conversationId')),
                'subscriberId': identifier(p.get('subscriberId')),
                'stepPageBounds': project(p.get('stepPageBounds', {}), SLICE),
            }
        if method == 'HandleCascadeUserInteraction':
            chat = self.owned(p.get('cascadeId'))
            interaction = object_value(p.get('interaction'))
            index = interaction.get('stepIndex', 0)
            if type(index) is not int or index < 0:
                raise Rejected('Invalid permission request')
            key = (chat, identifier(interaction.get('trajectoryId')), index)
            if key not in self.pending:
                raise Rejected('No workspace permission is waiting for this reply')
            permission = object_value(interaction.get('permission'))
            allow = project(permission.get('allow', False), bool)
            if permission.get('scope', 'PERMISSION_SCOPE_ONCE') != 'PERMISSION_SCOPE_ONCE':
                raise Rejected('Approve this workspace operation once')
            self.pending.remove(key)
            self.resolved.add(key)
            return 'remote', {'cascadeId': chat, 'interaction': {
                'trajectoryId': key[1], 'stepIndex': index,
                'permission': {'allow': allow, 'scope': 'PERMISSION_SCOPE_ONCE'},
            }}
        if method == 'JetboxWriteSummary':
            return 'local', {}
        if method == 'UpdateConversationAnnotations':
            ids = p.get('cascadeIds') or [p.get('cascadeId')]
            return 'remote', {'cascadeIds': [self.owned(value) for value in project(ids, [str])],
                              'mergeAnnotations': True, 'annotations': project(p.get('annotations', {}), {'title': str})}
        raise Rejected('This operation is unavailable in the protected sandbox')

    def response(self, method, payload, request=None):
        p = object_value(payload)
        if method == 'HasAuthToken':
            return project(p, {'hasToken': bool})
        if method == 'GetAuthStatus':
            return project(p, {'authResult': {'hasValidAuth': bool, 'authMode': ENUM}})
        if method == 'GetUserStatus':
            result = project(p, {'userStatus': {
                **strings('name email'), 'pro': bool, 'hasUsedAntigravity': bool,
                'acceptedLatestTermsOfService': bool, 'g1Tier': ENUM,
                'userUsedPromptCredits': int, 'userUsedFlowCredits': int,
                'cascadeModelConfigData': MODELS,
            }, 'planInfo': {'planName': str, 'monthlyPromptCredits': int, 'monthlyFlowCredits': int}})
            configs = result.get('userStatus', {}).get('cascadeModelConfigData', {})
            self.models = {item['modelOrAlias']['model'] for group in ('clientModelConfigs', 'battleModeModelConfigs')
                           for item in configs.get(group, []) if not item.get('disabled') and
                           'model' in item.get('modelOrAlias', {})}
            return result
        if method == 'StartCascade':
            public = self.public_id(p.get('cascadeId'))
            if not public or not request or p.get('cascadeId') != request.get('cascadeId'):
                raise Rejected('Unexpected conversation response')
            return {'cascadeId': public, 'projectEnvInfo': {'workspaceUris': [self.workspace_uri]}}
        if method == 'StreamAgentStateUpdates':
            return self.stream(p, request)
        if method == 'ListProjects':
            return {'projectIds': [key for key, private in self.projects.items() if private in p.get('projectIds', [])]}
        if method == 'ReadProject':
            return {'project': self.project(p.get('project', {}))}
        if method == 'ReadProjects':
            return {'projects': [self.project(value) for value in p.get('projects', []) if value.get('id') in self.projects.values()]}
        if method == 'ProjectUpdatesStream':
            result = {'projectList': {'projectIds': list(self.projects)}}
            public = self.public_id(p.get('projectUpdatedId'), projects=True)
            if public:
                result['projectUpdatedId'] = public
            return result
        if method == 'JetboxSubscribeToSummaries':
            updates = object_value(p.get('updates', {}))
            return {
                'updates': {key: project(updates[private], {
                    **strings('summary title lastModifiedTime lastUserInputTime'), 'numSteps': int,
                }) for key, private in self.chats.items() if private in updates},
                'deletes': [key for key, private in self.chats.items() if private in p.get('deletes', [])],
            }
        return {}

    def project(self, value):
        value = object_value(value)
        public = self.public_id(value.get('id'), projects=True)
        if not public:
            raise Rejected('Unexpected project response')
        conversations = object_value(value.get('projectConversations', {})).get('conversations', [])
        return {'id': public, 'name': Path(self.cwd).name,
                'projectResources': {'resources': [{'folderUri': self.workspace_uri}]}, 'isWorkspaceOnly': False,
                'projectConversations': {'conversations': [
                    {'conversationId': self.public_id(item.get('conversationId'))}
                    for item in conversations if isinstance(item, dict) and self.public_id(item.get('conversationId'))]}}

    def step(self, supplied, chat, trajectory, index):
        s = object_value(supplied)
        result = project(s, {'type': ENUM, 'status': ENUM, 'metadata': METADATA,
                             'userInput': USER, 'plannerResponse': PLANNER})
        source = result.get('metadata', {}).get('sourceTrajectoryStepInfo')
        if source is not None:
            # Never expose unrelated private conversation identities.
            source.update(cascadeId=self.public_id(chat), trajectoryId=trajectory, stepIndex=index)
        mcp = s.get('mcpTool')
        if isinstance(mcp, dict) and mcp.get('serverName') == 'factory_workspace':
            result['mcpTool'] = project(mcp, {
                'serverName': str, 'toolCall': TOOL_CALL, 'resultString': str, 'userRejected': bool,
                'progressMessage': str, 'progress': NUMBER, 'progressTotal': NUMBER,
            })
        generic = s.get('generic')
        args = generic.get('args', {}) if isinstance(generic, dict) else {}
        if (isinstance(args, dict) and args.get('ServerName') == 'factory_workspace' and
                'factory_workspace/' + str(args.get('ToolName')) in self.tools):
            # Native MCP steps use this generic envelope. Keep the workspace
            # command and its text result visible, never native payloads, media,
            # controller file URIs or arbitrary result metadata.
            result['generic'] = project(generic, {
                'args': strings('ServerName ToolName Arguments'),
                'result': {'result': str},
            })
            # The native renderer uses this name to show MCP arguments and
            # results. Its generic fallback hides both. Reconstruct only the
            # known workspace call, without forwarding other tool metadata.
            result.setdefault('metadata', {})['toolCall'] = {'name': 'call_mcp_tool'}
        permission = object_value(s.get('requestedInteraction', {})).get('permission', {})
        resource = object_value(permission).get('resource', {})
        key = (chat, trajectory, index)
        if (s.get('status') == 'CORTEX_STEP_STATUS_WAITING' and isinstance(resource, dict) and
                resource.get('action') == 'mcp' and resource.get('target') in self.tools):
            if key not in self.resolved:
                self.pending.add(key)
            result['requestedInteraction'] = {'permission': {
                **project(permission, strings('reason actionDescription')),
                'resource': project(resource, strings('action target')),
                'approvalScopeFilter': ['PERMISSION_SCOPE_ONCE'],
            }}
            result.setdefault('generic', {})
        else:
            self.pending.discard(key)
        if 'error' in s:
            result['error'] = {'userErrorMessage': 'Antigravity could not complete this step.'}
        if 'errorMessage' in s:
            result['errorMessage'] = {'error': {'userErrorMessage': 'Antigravity could not complete this step.'},
                                      'shouldShowUser': True}
        if 'finish' in s:
            result['finish'] = project(s['finish'], {'reason': ENUM})
        return result

    def stream(self, supplied, request):
        update = object_value(supplied.get('update', {}))
        private = update.get('conversationId')
        public = self.public_id(private)
        if not public or not request or request.get('conversationId') != private:
            raise Rejected('Unexpected conversation response')
        trajectory = identifier(update.get('trajectoryId'))
        result = project(update, {
            'status': ENUM, 'executableStatus': ENUM, 'executorLoopStatus': ENUM, 'fullyIdle': bool,
            'executorMetadata': EXECUTOR, 'contextWindowUsage': CONTEXT_USAGE,
            'executionStatus': {'initialization': str, 'modelRequest': {'attempt': int, 'maxAttempts': int, 'retryAt': str}},
            'turnsUpdate': {'totalLength': int, 'turns': [{'index': int, 'startStepIndex': int, 'startTime': str}]},
        })
        result.update(conversationId=public, trajectoryId=trajectory)
        if 'mainTrajectoryUpdate' in update:
            source = object_value(update['mainTrajectoryUpdate'])
            main = project(source, {'trajectoryType': ENUM, 'lastStepType': ENUM, 'status': ENUM,
                                    'fullyIdle': bool, 'killed': bool, 'metadata': {'createdAt': str}})
            if 'stepsUpdate' in source:
                steps = object_value(source['stepsUpdate'])
                values = project(steps, {'indices': [int], 'totalLength': int, 'pageBounds': SLICE})
                if len(values.get('indices', [])) != len(steps.get('steps', [])):
                    raise Rejected('Invalid conversation update')
                values['steps'] = [self.step(step, private, trajectory, index)
                                   for index, step in zip(values.get('indices', []), steps.get('steps', []))]
                main['stepsUpdate'] = values
            if 'waitingSteps' in source:
                main['waitingSteps'] = [
                    {'stepIndex': project(item.get('stepIndex', 0), int),
                     'step': self.step(item['step'], private, trajectory, item.get('stepIndex', 0))}
                    for item in source['waitingSteps']]
            if 'generatorMetadatasUpdate' in source:
                main['generatorMetadatasUpdate'] = project(source['generatorMetadatasUpdate'], {
                    'indices': [int], 'totalLength': int, 'pageBounds': SLICE, 'generatorMetadatas': [GENERATOR],
                })
            result['mainTrajectoryUpdate'] = main
        return {'update': result}
