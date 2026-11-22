"""Validate native editor messages before they reach the signed-in controller."""
import re
from pathlib import Path


class Rejected(ValueError):
    pass


def fields(value, names):
    if not isinstance(value, dict):
        raise Rejected('Expected an object')
    return {key: value[key] for key in names if key in value}


def text(value):
    if not isinstance(value, str):
        raise Rejected('Expected text')
    return value


def model_name(value):
    value = text(value)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/\[\]-]*', value):
        raise Rejected('Invalid model name')
    return value


def object_value(value):
    if not isinstance(value, dict):
        raise Rejected('Expected an object')
    return value


def session_id(value):
    value = text(value)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', value):
        raise Rejected('Invalid conversation identifier')
    return value


class Conversations:
    """Record controller-created conversations; never accept client-supplied paths."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self.ids = set()

    def add(self, value):
        value = session_id(value)
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.directory / value).touch(mode=0o600)
        self.ids.add(value)

    def contains(self, value):
        value = session_id(value)
        return value in self.ids or bool(self.directory and (self.directory / value).is_file())

    def require(self, value):
        if not self.contains(value):
            raise Rejected('This conversation was not created in this browser editor')
        return value


def content(items):
    """Text and inline images are data; file paths and remote images need workspace handling."""
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        raise Rejected('Expected message content')
    clean = []
    for item in items:
        if not isinstance(item, dict):
            raise Rejected('Expected a message content object')
        if item.get('type') == 'text':
            clean.append({'type': 'text', 'text': text(item['text'])})
        elif item.get('type') == 'image':
            source = object_value(item.get('source', {}))
            if source.get('type') != 'base64' or source.get('media_type') not in (
                    'image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                raise Rejected('Attach a local image')
            clean.append({'type': 'image', 'source': {
                'type': 'base64', 'media_type': source['media_type'], 'data': text(source['data'])}})
        else:
            raise Rejected('Unsupported message content')
    return clean


class ClaudeProtocol:
    def __init__(self, conversations=None):
        self.pending_permissions = set()
        self.conversations = conversations or Conversations()
        self.replies = []

    def outgoing(self, message):
        if message.get('type') == 'control_cancel_request':
            self.pending_permissions.discard(message.get('request_id'))
        if message.get('type') == 'system' and message.get('subtype') == 'init' and message.get('session_id'):
            self.conversations.add(message['session_id'])
        if message.get('type') == 'control_request' and message.get('request', {}).get('subtype') == 'can_use_tool':
            self.pending_permissions.add(message['request_id'])
        elif message.get('type') == 'control_request':
            self.replies.append({'type': 'control_response', 'response': {'subtype': 'error',
                'request_id': message['request_id'], 'error': 'This editor operation is unavailable in the sandbox'}})
            return None
        return message

    def incoming(self, message):
        object_value(message)
        kind = message.get('type')
        if kind == 'user':
            return {'type': 'user', 'message': {'role': 'user', 'content': content(message['message']['content'])},
                    'parent_tool_use_id': None, 'session_id': ''}
        if kind == 'control_response':
            response = object_value(message.get('response', {}))
            request_id = response.get('request_id')
            if request_id not in self.pending_permissions:
                raise Rejected('No matching permission request')
            safe = fields(response, ('subtype', 'request_id', 'error'))
            if response.get('subtype') == 'success':
                decision = object_value(response.get('response', {}))
                if decision.get('behavior') not in ('allow', 'deny'):
                    raise Rejected('Choose whether to allow this tool request')
                # A one-time decision must not install persistent permission rules.
                safe['response'] = fields(decision, ('behavior', 'updatedInput', 'message', 'interrupt'))
            self.pending_permissions.remove(request_id)
            return {'type': kind, 'response': safe}
        if kind != 'control_request':
            raise Rejected('Unsupported editor message')
        request = object_value(message.get('request', {}))
        subtype = request.get('subtype')
        if subtype in ('initialize', 'get_settings', 'interrupt'):
            # SDK hooks and dynamic MCP servers would run in the private controller.
            safe = {'subtype': subtype}
        elif subtype == 'set_model':
            safe = {'subtype': subtype, 'model': model_name(request['model'])}
        elif subtype == 'generate_session_title':
            safe = {'subtype': subtype, 'description': text(request['description']), 'persist': False}
        elif subtype == 'set_permission_mode' and request.get('mode') in (
                'default', 'acceptEdits', 'plan', 'dontAsk'):
            safe = {'subtype': subtype, 'mode': request['mode']}
        else:
            raise Rejected('This editor operation is unavailable in the sandbox')
        return {'type': kind, 'request_id': message['request_id'], 'request': safe}


class CodexProtocol:
    READS = {'model/list', 'account/rateLimits/read', 'thread/list', 'thread/loaded/list',
             'config/read', 'configRequirements/read', 'collaborationMode/list',
             'permissionProfile/list', 'experimentalFeature/list', 'mcpServerStatus/list',
             'remoteControl/status/read', 'app/installed', 'plugin/installed', 'hooks/list'}

    def __init__(self, cwd, instructions, conversations=None):
        self.cwd = cwd
        self.instructions = instructions
        self.pending = {}
        self.permissions = set()
        self.conversations = conversations or Conversations()
        self.replies = []
        self.questions = set()

    def outgoing(self, message):
        method = self.pending.pop(message.get('id'), None) if 'method' not in message else None
        result = message.get('result')
        if method in ('thread/start', 'thread/fork') and isinstance(result, dict):
            thread = result.get('thread', {})
            if thread.get('id'):
                self.conversations.add(thread['id'])
        if message.get('method') == 'thread/started':
            thread = message.get('params', {}).get('thread', {})
            if thread.get('id'):
                self.conversations.add(thread['id'])
        if method in ('thread/list', 'thread/loaded/list') and isinstance(result, dict):
            key = 'data'
            values = result.get(key, [])
            values = [item for item in values if self.conversations.contains(item['id'] if isinstance(item, dict) else item)]
            message = {**message, 'result': {**result, key: values}}
        if method == 'getAuthStatus' and 'result' in message:
            message = {**message, 'result': {**fields(message['result'], ('authMethod', 'requiresOpenaiAuth')),
                                          'authToken': None}}
        if method == 'account/read' and isinstance(result, dict):
            account = result.get('account')
            message = {**message, 'result': {
                'account': fields(account, ('type', 'email', 'planType')) if account else None,
                'requiresOpenaiAuth': result.get('requiresOpenaiAuth', True)}}
        if method == 'config/read' and isinstance(result, dict):
            config = fields(result.get('config', {}), ('model', 'model_provider', 'model_reasoning_effort',
                'model_reasoning_summary', 'service_tier', 'approval_policy', 'sandbox_mode', 'web_search'))
            message = {**message, 'result': {'config': config, 'origins': {}, 'layers': None}}
        if message.get('method', '').endswith('/requestApproval') and 'id' in message:
            self.permissions.add(message['id'])
        elif message.get('method') == 'item/tool/requestUserInput' and 'id' in message:
            self.questions.add(message['id'])
        elif 'method' in message and 'id' in message:
            self.replies.append({'id': message['id'], 'error': {'code': -32601,
                'message': 'This editor operation is unavailable in the sandbox'}})
            return None
        return message

    def incoming(self, message):
        object_value(message)
        method = message.get('method')
        given = object_value(message.get('params') or {})
        identifier = message.get('id')
        if identifier is not None and not isinstance(identifier, (str, int)):
            raise Rejected('Invalid request identifier')
        if method and identifier is not None and identifier in self.pending:
            raise Rejected('Request identifier is already in use')
        if not method and identifier in self.questions:
            result = object_value(message.get('result'))
            answers = object_value(result.get('answers'))
            safe = {}
            for key, value in answers.items():
                choices = object_value(value).get('answers')
                if not isinstance(choices, list):
                    raise Rejected('Expected a list of answers')
                safe[text(key)] = {'answers': [text(choice) for choice in choices]}
            self.questions.remove(identifier)
            return {'id': identifier, 'result': {'answers': safe}}
        if not method and identifier in self.permissions:
            result = object_value(message.get('result', {}))
            if result.get('decision') not in ('accept', 'decline', 'cancel'):
                raise Rejected('Choose a one-time permission decision')
            self.permissions.remove(identifier)
            return {'id': identifier, 'result': {'decision': result['decision']}}
        if method == 'initialize':
            params = {'clientInfo': {'name': 'codex_vscode', 'version': '1', 'title': 'Factory browser editor'},
                      'capabilities': {'experimentalApi': True}}
        elif method == 'initialized':
            return {'method': method}
        elif method == 'getAuthStatus':
            params = {'includeToken': False, 'refreshToken': False}
        elif method == 'account/read':
            params = {'refreshToken': False}
        elif method in self.READS:
            params = fields(given, ('cursor', 'limit', 'includeHidden', 'archived', 'sortKey', 'sortDirection'))
        elif method in ('thread/start', 'thread/resume', 'thread/fork'):
            params = fields(given, ('model', 'effort', 'personality', 'ephemeral'))
            params.update(cwd=self.cwd, approvalPolicy='on-request', sandbox='danger-full-access',
                          developerInstructions=self.instructions)
            if method != 'thread/start':
                params['threadId'] = self.conversations.require(given['threadId'])
        elif method == 'turn/start':
            inputs = given.get('input')
            if not isinstance(inputs, list) or any(not isinstance(item, dict) or item.get('type') != 'text' for item in inputs):
                raise Rejected('This message requires supported workspace attachments')
            params = {'threadId': self.conversations.require(given['threadId']),
                      'input': [{'type': 'text', 'text': text(item['text']), 'text_elements': []} for item in inputs]}
            params.update(fields(given, ('model', 'effort', 'summary', 'serviceTier')))
        elif method in ('thread/read', 'thread/turns/list', 'thread/items/list', 'thread/archive', 'thread/unarchive', 'thread/unsubscribe',
                        'thread/attachment/list', 'thread/queue/list', 'thread/name/set', 'turn/interrupt'):
            params = {'threadId': self.conversations.require(given['threadId'])}
            if method == 'thread/name/set':
                params['name'] = text(given['name'])
            if method == 'turn/interrupt':
                params['turnId'] = session_id(given['turnId'])
            params.update(fields(given, ('cursor', 'limit', 'includeTurns')))
            if method == 'thread/turns/list':
                params.update(fields(given, ('itemsView', 'sortDirection')))
            if method == 'thread/items/list':
                params.update(fields(given, ('turnId', 'sortDirection')))
        else:
            raise Rejected('This editor operation is unavailable in the sandbox')
        if identifier is not None:
            self.pending[identifier] = method
        return {'id': identifier, 'method': method, 'params': params}
