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
REFERENCE = '/opt/sandbox/context.md'
MESSAGE_BYTES = 1500


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


def changes(state, previous=None):
    old = previous.get('values', {}) if previous else {}
    return [name for name, value in state['values'].items() if not previous or old.get(name) != value] + [
        name for name in old if name not in state['values']]


def message(state, previous=None, changed=None):
    lines = ['Sandbox update: current Factory settings.',
             'These replace older Factory settings in this conversation; project instructions still apply.']
    changed = changes(state, previous) if changed is None else changed
    fields = []
    for name in changed:
        value = ' '.join(state['values'].get(name, 'no longer configured').split())
        if len(value.encode('utf-8')) > 120:
            value = 'updated'
        fields.append([name, value])
    if not fields:
        fields = [['Sandbox guidance', 'updated']]
    footer = f'Full current configuration, guidance and examples: {REFERENCE}.'
    def render():
        return '\n'.join([*lines, *(f'- {name}: {value}' for name, value in fields), footer])
    # This is a prompt-size budget, not a limit on supported configuration.
    # Full values remain available in the reference; every changed label stays visible.
    for field in sorted(fields, key=lambda row: len(row[1].encode('utf-8')), reverse=True):
        if len(render().encode('utf-8')) <= MESSAGE_BYTES:
            break
        if len(field[1].encode('utf-8')) > len('updated'):
            field[1] = 'updated'
    return render()


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
                                    or not isinstance(pending['state'].get('revision'), str)
                                    or not isinstance(pending['state'].get('values'), dict)
                                    or ('changes' in pending and (not isinstance(pending['changes'], list)
                                        or not all(isinstance(name, str) for name in pending['changes'])))))):
                record = {}
            acknowledged = record.get('acknowledged')
            if stage == 'before':
                pending = record.get('pending')
                if acknowledged and acknowledged.get('revision') == state['revision'] and not pending:
                    return {}
                changed = changes(state, acknowledged)
                if pending:
                    # A cancelled turn may have persisted its notice. Keep the
                    # affected labels until a matching completion acknowledges them.
                    for name in pending.get('changes', changes(pending['state'], acknowledged)):
                        if name not in changed:
                            changed.append(name)
                text = message(state, acknowledged, changed)
                if turn is not None:
                    # A later turn may supersede this pending delivery. Its own
                    # completion must match before it can acknowledge anything.
                    record['pending'] = {'turn': turn, 'state': state, 'changes': changed}
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
