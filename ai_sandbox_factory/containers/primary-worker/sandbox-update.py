"""Deliver current Factory settings through native agent hooks."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


SNAPSHOT = Path('/run/primary-runtime/sandbox-state.json')
RECEIPTS = Path.home() / '.local/state/ai-sandbox-factory/context-receipts'


def client_identity():
    # Hooks execute in Linux containers. `exec` in the hook command leaves the
    # native client as parent; PID start time also distinguishes reused PIDs.
    parent = os.getppid()
    try:
        started = Path(f'/proc/{parent}/stat').read_text().rsplit(')', 1)[1].split()[19]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{parent}:{started}'
    except (OSError, IndexError):
        return None  # Without a stable client identity, repeat rather than miss updates.


def read_snapshot(path):
    state = json.loads(Path(path).read_text())
    if (not isinstance(state, dict) or not isinstance(state.get('revision'), str)
            or not state['revision'] or state.get('network') not in ('allowlist', 'unrestricted')
            or not isinstance(state.get('values'), dict)
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in state['values'].items())):
        raise ValueError('expected a revision, network mode and named setting values')
    return {key: state[key] for key in ('revision', 'network', 'values')}


def message(state, previous=None):
    lines = ['Sandbox update: current Factory settings.',
             'These replace older Factory settings in this conversation; project instructions still apply.']
    if state['network'] == 'unrestricted':
        lines.append('Network: Unrestricted. Use ordinary network connections; do not configure the Restricted gateway. '
                     'Keep the native client’s managed sign-in transport unchanged.')
    else:
        lines.append('Network: Restricted. Use the inherited network settings. '
                     'If a destination is blocked, ask for its hostname and port in Factory Settings → Network and Web Search → Network.')
    old = previous.get('values', {}) if previous else {}
    for name, value in state['values'].items():
        if name != 'Network' and (not previous or old.get(name) != value):
            lines.append(f'{name}: {value}')
    for name in old.keys() - state['values'].keys():
        if name != 'Network':
            lines.append(f'{name}: no longer configured')
    lines.append('Full current instructions: /run/primary-runtime/context.md. '
                 'Use only tools actually available in this client.')
    return '\n'.join(lines)


def response(agent, text):
    if not text:
        return {}
    if agent == 'agy':
        return {'injectSteps': [{'systemMessage': {'systemMessage': text}}]}
    return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}}


def correlation(agent, payload):
    if agent == 'agy':
        session = payload.get('conversationId')
        initial_steps = payload.get('initialNumSteps')
        invocation = payload.get('invocationNum')
        # Invocation numbers start at zero again on the next user prompt.
        turn = [initial_steps, invocation] if type(initial_steps) is int and type(invocation) is int else None
    else:
        session = payload.get('session_id')
        turn = payload.get('turn_id' if agent == 'codex' else 'prompt_id')
    return (session, turn) if session and turn is not None else (None, None)


def write_record(path, record):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def handle(agent, stage, payload, *, snapshot=SNAPSHOT, receipts=RECEIPTS, client=None):
    state = read_snapshot(snapshot)
    session, turn = correlation(agent, payload)
    identity = client if client is not None else client_identity()
    if not session or not identity:
        return response(agent, message(state)) if stage == 'before' else {}
    key = hashlib.sha256(json.dumps([agent, identity, session]).encode()).hexdigest()
    receipts = Path(receipts)
    try:
        receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (receipts / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = receipts / (key + '.json')
            try:
                record = json.loads(path.read_text()) if path.exists() else {}
            except (ValueError, OSError):
                record = {}  # A lost receipt must cause redelivery, not suppress it.
            if (not isinstance(record, dict)
                    or (record.get('acknowledged') is not None
                        and not isinstance(record['acknowledged'], dict))
                    or (record.get('pending') is not None
                        and not isinstance(record['pending'], dict))):
                record = {}
            acknowledged = record.get('acknowledged')
            pending = record.get('pending')
            if ((acknowledged and not isinstance(acknowledged.get('values'), dict))
                    or (pending and (not isinstance(pending.get('state'), dict)
                                    or not isinstance(pending['state'].get('revision'), str)))):
                record = {}
            acknowledged = record.get('acknowledged')
            if stage == 'before':
                pending = record.get('pending')
                if acknowledged and acknowledged.get('revision') == state['revision'] and not pending:
                    return {}
                # A cancelled or failed turn may still have persisted its
                # context. With an uncertain delivery, send all current facts.
                text = message(state, None if pending else acknowledged)
                if turn is not None:
                    # A later turn may supersede this pending delivery. Its own
                    # completion must match before it can acknowledge anything.
                    record['pending'] = {'turn': turn, 'state': state}
                    write_record(path, record)
                return response(agent, text)
            pending = record.get('pending')
            if (not pending or turn is None or pending.get('turn') != turn
                    or (agent == 'agy' and payload.get('hookEventName') != 'PostInvocation')):
                return {}
            # Record what this completed turn actually saw. The host may have
            # changed again during inference; the next turn must compare against
            # the delivered state, including when settings return to an older revision.
            record['acknowledged'] = pending['state']
            record.pop('pending', None)
            write_record(path, record)
    except OSError as error:
        print(f'Cannot record sandbox settings delivery in {receipts}: {error}. '
              'Free space or restore access; settings will be sent again.', file=sys.stderr)
        return response(agent, message(state)) if stage == 'before' else {}
    return {}


def main():
    agent, stage = sys.argv[1:]
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError('expected a JSON object containing the native hook event')
    except (OSError, ValueError, TypeError) as error:
        detail = (f'Cannot read {agent} sandbox-update hook input: {error}. '
                  'Reopen the native client; its hook must supply a JSON event object.')
        print(detail, file=sys.stderr)
        print(json.dumps(response(agent, detail) if stage == 'before' else {}))
        return
    try:
        output = handle(agent, stage, payload)
    except (OSError, ValueError, TypeError, KeyError) as error:
        detail = (f'Cannot refresh Factory sandbox settings from {SNAPSHOT}: {error}. '
                  'Reopen the sandbox from Factory to restore its current settings; '
                  'do not assume older Factory network settings are current.')
        print(detail, file=sys.stderr)
        output = response(agent, detail) if stage == 'before' else {}
    print(json.dumps(output))


if __name__ == '__main__':
    main()
