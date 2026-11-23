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
        # Native API diagnostics may repeat request credentials in both the
        # assistant event and the terminal result. Keep them in the controller.
        kind = message.get('type')
        if (message.get('is_api_error_message') or message.get('isApiErrorMessage') or
                kind == 'result' and message.get('is_error') or
                kind == 'system' and message.get('subtype') == 'api_retry'):
            status = message.get('api_error_status')
            if status is None:
                status = message.get('error_status')
            status = status if type(status) is int and 100 <= status <= 599 else None
            description = ('Claude could not complete the request' +
                           (f' (HTTP {status}).' if status else '.'))
            if status == 401:
                description += ' Sign in to Claude again.'
            elif status == 429 or status and status >= 500:
                description += ' Try again later.'
            if not status:
                description = {
                    'error_max_turns': 'Claude reached the turn limit for this request.',
                    'error_max_budget_usd': 'Claude reached the spending limit for this request.',
                    'error_max_structured_output_retries': 'Claude could not produce the requested response format.',
                }.get(message.get('subtype'), description)
            safe = fields(message, ('type', 'session_id', 'uuid', 'parent_tool_use_id', 'timestamp',
                                     'user_message_uuid', 'queued_turn_count', 'result_index',
                                     'subtype', 'num_turns', 'duration_ms', 'duration_api_ms',
                                     'total_cost_usd', 'is_error', 'attempt', 'max_retries', 'retry_delay_ms'))
            if status:
                safe['api_error_status'] = status
                if 'error_status' in message:
                    safe['error_status'] = status
            category = message.get('error')
            if not isinstance(category, str) or category not in (
                    'authentication_failed', 'billing_error', 'rate_limit',
                    'invalid_request', 'server_error', 'unknown'):
                category = 'unknown'
            original = message.get('message')
            original = original if isinstance(original, dict) else {}
            usage = original.get('usage') if kind == 'assistant' else message.get('usage')
            usage = usage if isinstance(usage, dict) else {}
            counts = {key: value for key in ('input_tokens', 'output_tokens',
                      'cache_creation_input_tokens', 'cache_read_input_tokens')
                      if type(value := usage.get(key)) in (int, float)}
            if kind == 'assistant':
                safe.update(is_api_error_message=True, error=category, message={
                    'id': original.get('id'), 'type': 'message',
                    'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence',
                    'stop_sequence': '', 'usage': counts,
                    'content': [{'type': 'text', 'text': description}]})
            elif kind == 'result':
                safe.update(result=description, errors=[description], usage=counts)
                if message.get('terminal_reason') in ('api_error', 'interrupted', 'max_turns', 'max_budget_usd'):
                    safe['terminal_reason'] = message['terminal_reason']
                if message.get('stop_reason') in ('end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'pause_turn', 'refusal'):
                    safe['stop_reason'] = message['stop_reason']
            else:
                safe['error'] = category
            return safe
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
                changes = decision.get('updatedPermissions') or []
                valid = isinstance(changes, list) and all(
                    isinstance(change, dict) and change.get('destination') == 'session' and
                    change.get('type') == 'addRules' and change.get('behavior') == 'allow' and
                    isinstance(change.get('rules'), list) and all(
                        isinstance(rule, dict) and rule.get('toolName') in (
                            'mcp__factory_probe__workspace_shell', 'mcp__factory_probe__browser') and
                        set(rule).issubset({'toolName', 'ruleContent'}) for rule in change['rules'])
                    for change in changes)
                if decision.get('behavior') not in ('allow', 'deny') or not valid:
                    safe['response'] = {'behavior': 'deny', 'interrupt': True,
                        'message': 'Choose Allow once or Allow for this session. Permanent permission changes are unavailable here.'}
                else:
                    safe['response'] = fields(decision, ('behavior', 'updatedInput', 'message', 'interrupt'))
                    if changes:
                        safe['response']['updatedPermissions'] = changes
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
                'default', 'acceptEdits', 'plan', 'dontAsk', 'auto'):
            safe = {'subtype': subtype, 'mode': request['mode']}
        else:
            raise Rejected('This editor operation is unavailable in the sandbox')
        return {'type': kind, 'request_id': message['request_id'], 'request': safe}
