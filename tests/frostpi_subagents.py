"""Checks against the installed Pi runtime, extension, and FrostPi webview."""

from contextlib import contextmanager
import json
import errno
import os
import select
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
import time

from playwright.sync_api import TimeoutError as BrowserTimeout

MODES = {
    'disabled': ('disabled',),
    'automatic': ('inline', 'specialist', 'nested', 'parallel', 'context', 'stop-all', 'named', 'interrupt-goal'),
    'assisted': ('inline', 'background', 'cancel', 'nested-approval', 'critical', 'native', 'mixed'),
    'manual': ('manual',),
    'off': ('off',),
}
APPROVALS = {
    'inline': {'DENY': False},
    'background': {'ALPHA': True, 'BETA': False},
    'mixed': {'ALPHA': True, 'NATIVE': True},
    'cancel': {'CANCEL': False},
    'manual': {'MANUAL': True},
    'nested-approval': {'NESTED_ESCALATE': True},
    'critical': {'CRITICAL': True},
    'native': {'NATIVE': True},
}
TERMINAL_CASES = {'background', 'mixed', 'parallel', 'named', 'cancel'}
NAMED_WORKERS = {'Cedar', 'Atlas', 'Juniper'}
APPROVAL_TAG = re.compile(r'FACTORY_REVIEW_(ALLOW|DENY|ALPHA|BETA|CANCEL|MANUAL|NESTED_ESCALATE|CRITICAL)\b')
NATIVE_ALLOW = 'Approve this action once'
SUCCESS_FILES = {
    'inline': {'inline': 'FACTORY_NATIVE_EDIT', 'allow': 'FACTORY_REVIEW_ALLOW'},
    'background': {'alpha': 'FACTORY_REVIEW_ALPHA'},
    'mixed': {'alpha': 'FACTORY_REVIEW_ALPHA'},
    'manual': {'manual': 'FACTORY_REVIEW_MANUAL'},
    'off': {'off': 'FACTORY_OFF'},
    'nested-approval': {'nested_escalate': 'FACTORY_REVIEW_NESTED_ESCALATE'},
    'critical': {'critical': 'FACTORY_REVIEW_CRITICAL'},
}
MUST_NOT_EXIST = {'inline': ('deny',), 'background': ('beta',), 'context': ('context',), 'cancel': ('cancel',)}


def prepare_subagent_fixtures(workspace, inside):
    (workspace / 'subagents-seed.txt').write_text('FACTORY_SEED')
    (workspace / 'subagents-protected.env').write_text('FACTORY_PROTECTED_SEED')
    inside('mkdir', '-p', '/home/node/.pi/agent/agents', '/home/node/.pi/agent/extensions')
    inside('tee', '/home/node/.pi/agent/agents/fixture-reader.md', input='''---
name: fixture-reader
description: Fixture role with only the native read tool
tools: read
---
Read the synthetic seed and report it.
''')
    inside('tee', '/home/node/.pi/agent/subagents-boundary-fixture.txt', input='FACTORY_BOUNDARY')
    inside('tee', '/home/node/.pi/agent/FACTORY_NATIVE_REVIEW.txt', input='FACTORY_NATIVE_ACCESS_OK')
    inside('tee', '/home/node/.pi/agent/extensions/fixture-echo.ts', input='''import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { Type } from 'typebox';
import { existsSync, writeFileSync } from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
export default function(pi: ExtensionAPI) {
  pi.registerTool({
    name: 'fixture_echo',
    label: 'Fixture echo',
    description: 'Echo a synthetic marker.',
    parameters: Type.Object({ text: Type.String() }),
    async execute(_id, { text }, signal) {
      if (['HOLD_WORKER', 'HOLD_PARENT', 'HOLD_ONE', 'HOLD_TWO', 'HOLD_OTHER', 'HOLD_KEEP'].includes(text)) {
        writeFileSync('/workspace/' + text + '.ready', '');
        while (!existsSync('/workspace/' + text + '.release')) await delay(100, undefined, { signal });
      } else {
        await delay(2500, undefined, { signal });
      }
      return { content: [{ type: 'text', text }], details: {} };
    },
  });
}
''')


def cases(mode, case):
    if mode == 'automatic' and case == 'web-tools':
        return ('nested', 'specialist', 'parallel')
    if case is not None:
        if case not in MODES[mode]:
            raise ValueError(f'{case} is not a fixture for {mode}')
        return (case,)
    return MODES[mode]


def expected_dialogs(mode, case):
    return {} if mode == 'automatic' else APPROVALS.get(case, {})


def parent_result(case):
    status = 'STARTED' if case in TERMINAL_CASES else 'OK'
    name = case.upper().replace('-', '_')
    return f'FACTORY SUBAGENTS {name} {status}'


def fixture_trace(inside):
    return [line for line in inside('cat', '/tmp/fixture.log').splitlines() if line.startswith('SUBAGENT ')]


def reset_files(workspace, case):
    for name in (*SUCCESS_FILES.get(case, {}), *MUST_NOT_EXIST.get(case, ())):
        (workspace / f'subagents-{name}.txt').unlink(missing_ok=True)


def assert_files(workspace, case):
    for name, content in SUCCESS_FILES.get(case, {}).items():
        assert (workspace / f'subagents-{name}.txt').read_text() == content, name
    for name in MUST_NOT_EXIST.get(case, ()):
        assert not (workspace / f'subagents-{name}.txt').exists(), name
    assert (workspace / 'subagents-protected.env').read_text() == 'FACTORY_PROTECTED_SEED'


def assert_unanswered_requests_did_not_run(workspace, expected, answered):
    for tag in set(expected) - set(answered):
        marker = workspace / f'subagents-{tag.lower()}.txt'
        assert not marker.exists(), f'Unanswered {tag} request wrote {marker.name}'


def worker_markers(case):
    return {
        'background': ('SUBAGENT CHILD ALPHA OK', 'SUBAGENT CHILD BETA OK'),
        'mixed': ('SUBAGENT CHILD ALPHA OK', 'SUBAGENT CHILD NATIVE OK'),
        'parallel': ('SUBAGENT CHILD READ_ALPHA OK', 'SUBAGENT CHILD READ_BETA OK'),
        'named': ('SUBAGENT CHILD READ_ALPHA OK', 'SUBAGENT CHILD READ_BETA OK'),
        'cancel': ('SUBAGENT CHILD CANCEL OK',),
    }.get(case, ())


def assert_trace(mode, case, trace):
    assert not any(' FAIL ' in line for line in trace), trace
    decisions = [line.split()[2:] for line in trace if re.fullmatch(r'SUBAGENT REVIEW \w+ (allow|deny|defer)', line)]
    expected = {
        'inline': [['ALLOW', 'allow'], ['DENY', 'deny']],
        'context': [['CONTEXT', 'deny']],
        'background': [['ALPHA', 'defer'], ['BETA', 'defer']],
        'mixed': [['ALPHA', 'defer'], ['NATIVE', 'defer']],
        'cancel': [['CANCEL', 'defer']],
        'nested-approval': [['NESTED_ESCALATE', 'defer']],
        'critical': [['CRITICAL', 'deny']],
        'native': [['NATIVE', 'defer']],
    }.get(case, [])
    assert sorted(decisions) == sorted(expected), trace
    if case in ('background', 'parallel'):
        roles = ('ALPHA', 'BETA') if case == 'background' else ('READ_ALPHA', 'READ_BETA')
        starts = [next(i for i, line in enumerate(trace) if line.startswith(f'SUBAGENT CHILD {role} START '))
                  for role in roles]
        first_finish = min(trace.index(marker) for marker in worker_markers(case))
        assert max(starts) < first_finish, trace
    if case in ('context', 'native', 'mixed'):
        tag = 'CONTEXT' if case == 'context' else 'NATIVE'
        assert f'SUBAGENT REVIEW {tag} SOURCE root=true delegated=false' in trace, trace
    if case == 'specialist':
        assert 'SUBAGENT CHILD SPECIALIST START read' in trace, trace
        assert 'SUBAGENT CHILD SPECIALIST CALL 0 read' in trace, trace
    if case == 'nested':
        assert 'SUBAGENT CHILD NESTED CALL 0 subagent' in trace, trace
        assert 'SUBAGENT CHILD LEAF CALL 0 read' in trace, trace
        assert 'SUBAGENT CHILD LEAF OK' in trace, trace


def async_statuses(inside):
    return json.loads(inside('python3', '-c', """
import json
from pathlib import Path
root = Path('/tmp/factory-subagent-runs/async-subagent-runs')
print(json.dumps({p.parent.name: json.loads(p.read_text())['state'] for p in root.glob('*/status.json')}))
"""))


def saved_worker_names(inside):
    return json.loads(inside('python3', '-c', """
import json
from pathlib import Path
names = {}
for path in (Path.home() / '.pi/agent/sessions').rglob('session.jsonl'):
    for entry in map(json.loads, path.read_text().splitlines()):
        if entry.get('type') == 'session_info' and entry.get('name'):
            names[str(path)] = entry['name']
print(json.dumps(names))
"""))


def assert_named_workers(inside, before):
    names = [name for path, name in saved_worker_names(inside).items() if path not in before]
    assert all(names.count(name) == 1 for name in NAMED_WORKERS), names


def wait_for_workers(inside, before, workers, timeout=20):
    deadline = time.monotonic() + timeout
    statuses = {}
    while time.monotonic() < deadline:
        statuses = {key: value for key, value in async_statuses(inside).items() if key not in before}
        if len(statuses) == workers and all(value == 'complete' for value in statuses.values()):
            return statuses
        time.sleep(0.25)
    raise AssertionError(f'Expected {workers} new completed background runs, observed {statuses}')


def approval_tag(detail):
    if 'FACTORY_NATIVE_REVIEW.txt' in detail:
        return 'NATIVE'
    match = APPROVAL_TAG.search(detail)
    return match[1] if match else None


def rpc_case(container, case, mode, workspace, inside, output):
    reset_files(workspace, case)
    offset = len(fixture_trace(inside))
    before = async_statuses(inside) if case in TERMINAL_CASES else {}
    if case == 'named':
        names_before = saved_worker_names(inside)
    command = ['podman', 'exec', '-i', '--workdir', '/workspace', container, 'pi', '--mode', 'rpc',
               '--approve', '--provider', 'fixture', '--model', 'fixture']
    events = Queue()
    dialogs = {}
    expected = expected_dialogs(mode, case)
    ready = False
    event_path = output / f'pi-{case}.jsonl'
    with (output / f'pi-{case}.stderr').open('w') as errors, event_path.open('w') as saved:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)

        def read_events():
            for line in process.stdout:
                events.put(line)
            events.put(None)

        def send(message):
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()

        Thread(target=read_events, daemon=True).start()
        try:
            send({'type': 'prompt', 'message': 'Factory subagents ' + case})
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    line = events.get(timeout=0.25)
                except Empty:
                    if ready:
                        trace = fixture_trace(inside)[offset:]
                        if any(' FAIL ' in line for line in trace):
                            pending = set(expected) - set(dialogs)
                            raise AssertionError(f'Subagent {case} failed. Pending approvals: {pending}. {trace}') from None
                        if all(marker in trace for marker in worker_markers(case)):
                            break
                    continue
                assert line is not None, f'Pi exited before completing {case}. See {event_path}'
                saved.write(line)
                saved.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('type') == 'extension_ui_request' and event.get('method') == 'input':
                    detail = ' '.join(str(event.get(key, '')) for key in ('title', 'message'))
                    assert case == 'critical' and 'Type APPROVE' in detail, detail
                    assert not (workspace / 'subagents-critical.txt').exists()
                    send({'type': 'extension_ui_response', 'id': event['id'], 'value': 'APPROVE'})
                if event.get('type') == 'extension_ui_request' and event.get('method') in ('select', 'confirm'):
                    detail = ' '.join(str(event.get(key, '')) for key in ('title', 'message'))
                    tag = approval_tag(detail)
                    assert tag and tag in expected and tag not in dialogs, detail
                    dialogs[tag] = event['id']
                    allowed = expected[tag]
                    response = {'type': 'extension_ui_response', 'id': event['id']}
                    if case == 'cancel':
                        response['cancelled'] = True
                    elif event['method'] == 'confirm':
                        response['confirmed'] = allowed
                    else:
                        pattern = r'^(Allow this exact command once|' + NATIVE_ALLOW + r'|Yes)$' if allowed else r'^(Deny|No)$'
                        options = event.get('options', [])
                        response['value'] = next((x for x in options if re.match(pattern, x, re.I)), None)
                        assert response['value'] is not None, options
                    send(response)
                if event.get('type') == 'agent_end':
                    result = json.dumps(event.get('messages', [])[-1:])
                    assert 'FACTORY SUBAGENT FIXTURE FAILED' not in result, (result, fixture_trace(inside)[offset:])
                    ready |= parent_result(case) in result
            else:
                raise AssertionError(f'Pi did not complete {case}. Trace: {fixture_trace(inside)[offset:]}')
            assert set(dialogs) == set(expected) and len(set(dialogs.values())) == len(dialogs), dialogs
            assert_files(workspace, case)
            assert_trace(mode, case, fixture_trace(inside)[offset:])
            if case in TERMINAL_CASES:
                wait_for_workers(inside, before, len(worker_markers(case)))
            if case == 'named':
                assert_named_workers(inside, names_before)
        except AssertionError:
            assert_unanswered_requests_did_not_run(workspace, expected, dialogs)
            raise
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print(f'PASS Pi {mode}: {case}', flush=True)


def prepare_interrupt(workspace):
    for marker in ('HOLD_PARENT.ready', 'HOLD_WORKER.ready', 'HOLD_WORKER.release'):
        (workspace / marker).unlink(missing_ok=True)


def wait_for_markers(workspace, markers):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if all((workspace / marker).exists() for marker in markers):
            return
        time.sleep(.1)
    raise AssertionError(f'Fixture tools did not reach {markers}')


@contextmanager
def fixture_rpc(container, output, label, session=None):
    args = ['podman', 'exec', '-i', '--workdir', '/workspace', container,
            'pi', '--mode', 'rpc', '--approve', '--provider', 'fixture', '--model', 'fixture']
    if session is not None:
        args.extend(['--session', session])
    with (output / f'{label}.jsonl').open('w') as log, (output / f'{label}.stderr').open('w') as err:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
        events = Queue()

        def read():
            for line in proc.stdout:
                log.write(line)
                log.flush()
                events.put(json.loads(line))
            events.put(None)

        def send(**command):
            proc.stdin.write(json.dumps(command) + '\n')
            proc.stdin.flush()

        def until(predicate):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=max(0, deadline - time.monotonic()))
                except Empty:
                    break
                assert event is not None, f'{label} exited early'
                if predicate(event):
                    return event
            raise AssertionError(f'Expected RPC event missing. See {log.name}')

        reader = Thread(target=read, daemon=True)
        reader.start()
        try:
            yield send, until
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=10)
            reader.join()


def goal_notice(event, *terms):
    body = event.get('message')
    return event.get('method') == 'notify' and isinstance(body, str) and all(term in body for term in terms)


def rpc_result(marker):
    return lambda e: e.get('type') == 'agent_end' and marker in json.dumps(e.get('messages', [])[-1:])


def new_birch(inside, before):
    paths = [path for path, name in saved_worker_names(inside).items() if name == 'Birch' and path not in before]
    assert len(paths) == 1, paths
    return paths[0]


def goal_completed(event):
    return (event.get('type') == 'tool_execution_end' and event.get('toolName') == 'goal_update'
            and not event.get('isError') and event['result']['details']['goal']['status'] == 'complete')


def interrupt_rpc(container, workspace, inside, output):
    prepare_interrupt(workspace)
    before, names = async_statuses(inside), saved_worker_names(inside)
    with fixture_rpc(container, output, 'pi-interrupt-parent') as (send, until):
        send(type='prompt', message='/goal Factory parent worker goal')
        wait_for_markers(workspace, ('HOLD_PARENT.ready', 'HOLD_WORKER.ready'))
        send(type='abort')
        until(lambda e: e.get('type') == 'agent_settled' and e.get('aborted'))
        send(type='prompt', message='/goal status')
        until(lambda e: goal_notice(e, 'paused', 'Factory parent worker goal'))
        send(type='prompt', message='Factory subagents inspect-worker')
        until(rpc_result('FACTORY SUBAGENTS INSPECT_WORKER OK'))
        (workspace / 'HOLD_WORKER.release').touch()
        wait_for_workers(inside, before, 1, timeout=45)
        send(type='prompt', message='Factory subagents continue-worker')
        until(rpc_result('FACTORY SUBAGENTS CONTINUE_WORKER OK'))
        child = new_birch(inside, names)
        with fixture_rpc(container, output, 'pi-interrupt-child', child) as (child_send, child_until):
            child_send(type='prompt', message='/goal status')
            child_until(lambda e: goal_notice(e, 'No goal.'))
            child_send(type='prompt', message='/goal Factory child worker goal')
            child_until(goal_completed)
            child_send(type='prompt', message='/goal status')
            child_until(lambda e: goal_notice(e, 'complete', 'Factory child worker goal'))
        send(type='prompt', message='/goal status')
        until(lambda e: goal_notice(e, 'paused', 'Factory parent worker goal'))
        send(type='prompt', message='/goal resume')
        until(goal_completed)
        send(type='prompt', message='/goal status')
        until(lambda e: goal_notice(e, 'complete', 'Factory parent worker goal'))
    print('PASS Pi RPC: parent interruption, worker status and continuation, isolated session goals', flush=True)


def interrupt_frostpi(page, frame, send, workspace, inside, output):
    prepare_interrupt(workspace)
    before = async_statuses(inside)
    if frame.locator('.session-title').filter(has_text=re.compile(r'^Atlas$')).count():
        frame.get_by_role('button', name='New or resume session').click()
        frame.locator('button.launcher-create').click()
        frame.locator('button.session-heading .session-title').filter(has_text=re.compile(r'^New session$')).wait_for()
    send('/goal Factory parent worker goal')
    wait_for_markers(workspace, ('HOLD_PARENT.ready', 'HOLD_WORKER.ready'))
    frame.get_by_role('button', name=re.compile(r'^Stop (?:Pi|turn)$')).click()
    worker = frame.get_by_role('button', name='View transcript for Birch')
    worker.get_by_text(re.compile(r'^Running')).wait_for()
    page.screenshot(path=str(output / 'frostpi-interrupted-worker-running.png'))
    send('/goal status')
    frame.get_by_text(re.compile(r'Goal:\s+paused[\s\S]+Factory parent worker goal')).first.wait_for()
    send('Factory subagents inspect-worker')
    frame.get_by_text('FACTORY SUBAGENTS INSPECT_WORKER OK', exact=True).last.wait_for(timeout=45_000)
    (workspace / 'HOLD_WORKER.release').touch()
    wait_for_workers(inside, before, 1, timeout=45)
    worker.get_by_text(re.compile(r'^Complete')).wait_for()
    send('Factory subagents continue-worker')
    frame.get_by_text('FACTORY SUBAGENTS CONTINUE_WORKER OK', exact=True).last.wait_for(timeout=45_000)
    send('/goal resume')
    frame.locator('.extension-statuses').get_by_text(re.compile(r'goal complete')).wait_for(timeout=45_000)
    send('/goal status')
    frame.get_by_text(re.compile(r'Goal:\s+complete[\s\S]+Factory parent worker goal')).first.wait_for()
    frame.get_by_role('button', name='New or resume session', exact=True).click()
    frame.get_by_text('Resume session', exact=True).click()
    page.locator('.quick-input-widget').get_by_text('Birch', exact=True).click(timeout=30_000)
    frame.locator('.session-title').filter(has_text=re.compile(r'^Birch$')).wait_for(timeout=30_000)
    send('/goal status')
    frame.get_by_text(re.compile(r'No goal\. Usage:')).first.wait_for()
    send('/goal Factory child worker goal')
    frame.locator('.extension-statuses').get_by_text(re.compile(r'goal complete')).wait_for(timeout=45_000)
    send('/goal status')
    frame.get_by_text(re.compile(r'Goal:\s+complete[\s\S]+Factory child worker goal')).first.wait_for()
    page.screenshot(path=str(output / 'frostpi-retained-child-goal.png'))
    print('PASS FrostPi: parent interruption, worker status and continuation, isolated session goals', flush=True)


STOP_READY = ('HOLD_ONE.ready', 'HOLD_TWO.ready')


def prepare_stop(workspace):
    fifo = workspace / 'subagents-foreground.pipe'
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    for name in (*STOP_READY, 'HOLD_OTHER.ready', 'HOLD_OTHER.release', 'HOLD_KEEP.ready', 'HOLD_KEEP.release'):
        (workspace / name).unlink(missing_ok=True)


@contextmanager
def foreground_reader(workspace):
    # Opening only succeeds after the worker opens the FIFO for reading.
    fifo = workspace / 'subagents-foreground.pipe'
    deadline = time.monotonic() + 45
    while True:
        try:
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO or time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    try:
        yield
        # The stream stays open while the foreground command is alive.
        poller = select.poll()
        poller.register(writer, select.POLLERR)
        assert poller.poll(45_000), 'Foreground worker still holds its input open after interruption'
    finally:
        os.close(writer)


def active_pair(inside, before):
    statuses = {k: v for k, v in async_statuses(inside).items() if k not in before}
    assert len(statuses) == 2 and set(statuses.values()) == {'running'}, statuses
    return set(statuses)


def wait_for_stop(inside, ids):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        states = {key: value for key, value in async_statuses(inside).items() if key in ids}
        if set(states) == ids and set(states.values()) == {'stopped'}:
            return
        time.sleep(.2)
    raise AssertionError(f'Expected stopped workers {ids}, saw {states}')


def stop_rpc(container, workspace, inside, output):
    prepare_stop(workspace)
    baseline = async_statuses(inside)
    with fixture_rpc(container, output, 'pi-stop-other') as (other_send, other_until):
        other_send(type='prompt', message='Factory subagents stop-other')
        other_until(rpc_result('FACTORY SUBAGENTS STOP_OTHER OK'))
        wait_for_markers(workspace, ('HOLD_OTHER.ready', 'HOLD_KEEP.ready'))
        before = async_statuses(inside)
        other_ids = set(before) - set(baseline)
        assert len(other_ids) == 2, other_ids
        with fixture_rpc(container, output, 'pi-stop-all') as (send, until):
            send(type='prompt', message='Factory subagents stop-all')
            wait_for_markers(workspace, STOP_READY)
            ids = active_pair(inside, before)

            def pick(command, accept, parent_active):
                send(type='prompt', id=f'pick-{accept}', message=command, streamingBehavior='steer')
                choice = until(lambda e: e.get('method') == 'select' and e.get('title') == 'Choose workers to stop')
                values = choice['options']
                assert values[0] == 'Stop all 2 background workers', values
                assert sorted(values[1:]) == ['Alder', 'Elm'], values
                send(type='extension_ui_response', id=choice['id'], value=values[0])
                confirm = until(lambda e: e.get('method') == 'confirm' and e.get('title') == values[0])
                detail = 'Stop these 2 background workers in this session?'
                if parent_active:
                    detail += ' The parent turn will keep running.'
                assert confirm['message'] == detail, confirm
                send(type='extension_ui_response', id=confirm['id'], confirmed=accept)
                until(lambda e: e.get('type') == 'response' and e.get('id') == f'pick-{accept}')

            with foreground_reader(workspace):
                pick('/subagents-stop-picker-rpc', False, parent_active=True)
                assert active_pair(inside, before) == ids
                send(type='abort')
                until(lambda e: e.get('type') == 'agent_settled' and e.get('aborted'))
            pick('/subagents-stop', True, parent_active=False)
            wait_for_stop(inside, ids)
            untouched = async_statuses(inside)
            assert all(untouched[key] == 'running' for key in other_ids), untouched
        other_send(type='prompt', message='/subagents-stop-picker-rpc')
        choice = other_until(lambda e: e.get('method') == 'select' and e.get('title') == 'Choose workers to stop')
        assert sorted(choice['options'][1:]) == ['Ash', 'Maple'], choice
        named = {}
        for run_id in other_ids:
            path = f'/tmp/factory-subagent-runs/async-subagent-runs/{run_id}/status.json'
            status = json.loads(inside('cat', path))
            named[status['steps'][0]['label']] = run_id
        assert set(named) == {'Ash', 'Maple'}, named
        other_send(type='extension_ui_response', id=choice['id'], value='Maple')
        confirm = other_until(lambda e: e.get('method') == 'confirm' and e.get('message') == 'Stop Maple in this session?')
        other_send(type='extension_ui_response', id=confirm['id'], confirmed=True)
        wait_for_stop(inside, {named['Maple']})
        assert async_statuses(inside)[named['Ash']] == 'running'
        (workspace / 'HOLD_KEEP.release').touch()
        deadline = time.monotonic() + 45
        while async_statuses(inside)[named['Ash']] != 'complete':
            assert time.monotonic() < deadline, 'Untargeted worker did not complete'
            time.sleep(.2)
    print('PASS Pi RPC: stop all stays in its session and stopping Maple leaves Ash running', flush=True)


def stop_tui(workspace, inside, output):
    prepare_stop(workspace)
    before = async_statuses(inside)
    def tmux(*args):
        return inside('tmux', '-S', '/tmp/factory-stop-tmux.sock', *args)

    def screen(text):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            content = tmux('capture-pane', '-p', '-J')
            if text in content:
                return content
            time.sleep(.2)
        raise AssertionError(f'Missing {text}: {content}')

    tmux('new-session', '-d', '-c', '/workspace', '-x', '135', '-y', '35',
         'pi', '--approve', '--provider', 'fixture', '--model', 'fixture')
    try:
        screen('subagents')
        tmux('send-keys', '-l', 'Factory subagents stop-all')
        tmux('send-keys', 'Enter')
        wait_for_markers(workspace, STOP_READY)
        with foreground_reader(workspace):
            tmux('send-keys', 'Escape')
            screen('background subagent runs are still active')
        ids = active_pair(inside, before)
        tmux('send-keys', '-l', '/subagents-stop')
        tmux('send-keys', 'Enter')
        (output / 'pi-stop-picker.txt').write_text(screen('stop all 2 async runs'))
        tmux('send-keys', 'Enter')
        (output / 'pi-stop-confirm.txt').write_text(screen('Confirm: stop all 2 async runs?'))
        tmux('send-keys', 'Enter')
        wait_for_stop(inside, ids)
    finally:
        tmux('kill-server')
    print('PASS Pi terminal: Escape keeps two workers running and stop all ends both', flush=True)


def stop_frostpi(page, frame, send, workspace, inside, output):
    prepare_stop(workspace)
    before = async_statuses(inside)
    send('Factory subagents stop-all')
    wait_for_markers(workspace, STOP_READY)
    with foreground_reader(workspace):
        frame.get_by_role('button', name=re.compile(r'^Stop (?:Pi|turn)$')).click()
    frame.get_by_text('Turn stopped', exact=True).wait_for(timeout=15000)
    ids = active_pair(inside, before)
    for accept in (False, True):
        stop_button = frame.get_by_role('button', name=re.compile(r'^Stop workers(?:…|\.\.\.)$'))
        if accept and stop_button.count():
            stop_button.click()
        else:
            composer = frame.get_by_role('textbox', name='Message Pi')
            composer.fill('/subagents-stop')
            composer.press('Control+Enter')
        frame.get_by_role('button', name='Stop all 2 background workers', exact=True).click(timeout=30_000)
        frame.get_by_text('Stop these 2 background workers in this session?', exact=True).wait_for()
        if accept:
            page.screenshot(path=str(output / 'frostpi-stop-confirm.png'))
        frame.get_by_role('button', name='Yes' if accept else 'No', exact=True).click()
        if not accept:
            assert active_pair(inside, before) == ids
    wait_for_stop(inside, ids)
    for name in ('Alder', 'Elm'):
        row = frame.get_by_role('button', name=f'View transcript for {name}')
        row.get_by_text(re.compile('^Stopped')).wait_for(timeout=15_000)
    page.screenshot(path=str(output / 'frostpi-workers-stopped.png'))
    print('PASS FrostPi: cancelled and confirmed stopping both current-session workers', flush=True)


def check_native_subagents(container, workspace, inside, output, mode, case=None):
    for item in cases(mode, case):
        if item == 'stop-all':
            stop_rpc(container, workspace, inside, output)
            stop_tui(workspace, inside, output)
            continue
        if item == 'interrupt-goal':
            interrupt_rpc(container, workspace, inside, output)
            continue
        rpc_case(container, item, mode, workspace, inside, output)
        if item in ('manual', 'mixed'):
            check_terminal_approval(workspace, inside, output, item)


def check_terminal_approval(workspace, inside, output, case):
    """Confirm a worker actually presents a usable Pi terminal decision."""
    reset_files(workspace, case)
    offset = len(fixture_trace(inside))
    before = async_statuses(inside) if case in TERMINAL_CASES else {}
    socket = '/tmp/factory-approval-tmux.sock'

    def tmux(*args):
        return inside('tmux', '-S', socket, *args)

    def capture_when(*labels):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            view = tmux('capture-pane', '-p', '-J')
            for label in labels:
                if label.casefold() in view.casefold():
                    return label, view
            time.sleep(0.2)
        raise AssertionError(f'Pi terminal did not show {labels!r}. Last screen:\n{view}')

    tmux('new-session', '-d', '-c', '/workspace', '-x', '135', '-y', '35',
         'pi', '--approve', '--provider', 'fixture', '--model', 'fixture')
    try:
        capture_when('subagents')
        tmux('send-keys', '-l', 'Factory subagents ' + case)
        tmux('send-keys', 'Enter')
        pending_choices = {'Allow this exact command once': 'MANUAL' if case == 'manual' else 'ALPHA'}
        if case == 'mixed':
            pending_choices[NATIVE_ALLOW] = 'NATIVE'
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                trace = fixture_trace(inside)[offset:]
                if all(f'SUBAGENT REVIEW {tag} defer' in trace for tag in pending_choices.values()):
                    break
                time.sleep(0.2)
            else:
                raise AssertionError(f'The native and Bash approvals did not overlap. {trace}')
        while pending_choices:
            choice, pending = capture_when(*pending_choices)
            tag = pending_choices.pop(choice)
            (output / f'pi-terminal-{case}-{tag.lower()}.txt').write_text(pending)
            if tag == 'NATIVE':
                assert 'Permission Required (Subagent)' in pending and 'FACTORY_NATIVE_REVIEW.txt' in pending, pending
            else:
                assert re.search(r'Subagent: assistant \([0-9a-f]{8}\)', pending), pending
                assert f'FACTORY_REVIEW_{tag}' in pending, pending
                assert not (workspace / f'subagents-{tag.lower()}.txt').exists()
            tmux('send-keys', 'Enter')
        _, done = capture_when(parent_result(case))
        (output / f'pi-terminal-{case}-approved.txt').write_text(done)
        if case in TERMINAL_CASES:
            wait_for_workers(inside, before, len(worker_markers(case)))
        assert_trace('manual' if case == 'manual' else 'assisted', case, fixture_trace(inside)[offset:])
        assert_files(workspace, case)
    finally:
        tmux('kill-server')
    print(f'PASS Pi terminal {case}: displayed and applied the child approvals', flush=True)


def check_frostpi_subagents(page, frame, send, workspace, inside, output, mode, case=None):
    for item in cases(mode, case):
        if item == 'stop-all':
            stop_frostpi(page, frame, send, workspace, inside, output)
            continue
        if item == 'interrupt-goal':
            interrupt_frostpi(page, frame, send, workspace, inside, output)
            continue
        reset_files(workspace, item)
        offset = len(fixture_trace(inside))
        expected = expected_dialogs(mode, item)
        before = async_statuses(inside) if item in TERMINAL_CASES else {}
        if item == 'named':
            names_before = saved_worker_names(inside)
        if item == 'mixed':
            page.set_viewport_size({'width': 1050, 'height': 720})
        send('Factory subagents ' + item)
        acted = set()
        try:
            if item in ('parallel', 'named'):
                widget = frame.get_by_role('list', name='Worker status')
                if item == 'named':
                    widget.get_by_role('button', name='View transcript for Juniper').wait_for(timeout=30_000)
                else:
                    widget.wait_for(timeout=15_000)
                page.wait_for_timeout(500)
                page.screenshot(path=str(output / f'frostpi-{item}-running.png'))
                label = widget.inner_text()
                assert 'PI_SUBAGENT_ASYNC_JSON:' not in label, 'FrostPi displayed raw subagent status JSON'
                pattern = r'^(Atlas|Juniper)\s+(?:Queued|Running|Complete)' if item == 'named' else (
                    r'^assistant \(([0-9a-f]{8})\)\s+(?:Queued|Running|Complete)')
                two_workers = re.findall(pattern, label, re.M)
                assert len(two_workers) == 2 and len(set(two_workers)) == 2, label
                assert widget.evaluate('(node) => node.scrollWidth <= node.clientWidth'), label
                if item == 'named':
                    assert re.search(r'Running\s*·\s*(?:<1s|\d+s)', label), label
                    widget.get_by_role('button', name='View transcript for Atlas').click()
                    atlas = frame.get_by_role('region', name='Transcript for Atlas')
                    atlas.get_by_text('FACTORY_CHILD_READ_ALPHA', exact=False).first.wait_for(timeout=8_000)
                    assert not atlas.get_by_text('FACTORY_CHILD_READ_BETA', exact=False).count()
                    page.screenshot(path=str(output / 'frostpi-atlas-live.png'))
                    widget.get_by_role('button', name='View transcript for Juniper').click()
                    juniper = frame.get_by_role('region', name='Transcript for Juniper')
                    juniper.get_by_text('FACTORY_CHILD_READ_BETA', exact=False).first.wait_for(timeout=8_000)
                    assert not atlas.count()
                    assert not juniper.get_by_text('FACTORY_CHILD_READ_ALPHA', exact=False).count()
            card = frame.get_by_role('region', name='Prompt details')
            while len(acted) < len(expected):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    detail = card.inner_text(timeout=2_000) if card.is_visible() else ''
                    tag = approval_tag(detail)
                    if tag and tag not in acted:
                        break
                    trace = fixture_trace(inside)[offset:]
                    if any(' FAIL ' in line for line in trace) or (
                        item == 'inline' and 'SUBAGENT PARENT INLINE OK' in trace
                    ) or (item == 'cancel' and 'SUBAGENT CHILD CANCEL OK' in trace):
                        pending = set(expected) - acted
                        raise AssertionError(f'Child finished before FrostPi received approvals {pending}. {trace}')
                    page.wait_for_timeout(250)
                else:
                    raise AssertionError(f'FrostPi did not receive approvals {set(expected) - acted}. {trace}')
                assert tag in expected, detail
                if tag != 'NATIVE':
                    assert 'Command' in detail and 'Justification' in detail and 'Execution scope' in detail, detail
                    assert re.search(r'Subagent: assistant \([0-9a-f]{8}\)', detail), detail
                    if mode == 'assisted':
                        assert f'Synthetic decision for the {tag} workspace marker.' in detail, detail
                else:
                    assert re.search(r'subagent\s*:\s*assistant\s*·\s*session\s+[0-9a-f-]{36}', detail, re.I), detail
                if item == 'mixed' and not acted:
                    frame.get_by_role('list', name='Worker status').get_by_role('button').first.click()
                    panel = frame.get_by_role('region', name=re.compile(r'^Transcript for '))
                    panel.get_by_text(re.compile(r'FACTORY_CHILD_(NATIVE|ALPHA)')).first.wait_for(timeout=8_000)
                    assert card.is_visible(), 'Opening worker details hid the parent approval'
                    assert panel.evaluate('(node) => node.scrollWidth <= node.clientWidth')
                    assert panel.get_by_role('region', name='Recent worker activity').evaluate('(node) => node.clientHeight >= 65'), (
                        'Opening worker details left too little room to read the transcript')
                    assert frame.locator('.composer-box').evaluate('(node) => node.getBoundingClientRect().bottom <= innerHeight'), (
                        'Opening worker details clipped the composer')
                    page.screenshot(path=str(output / 'frostpi-worker-and-approval.png'))
                    panel.get_by_role('button', name='Close worker transcript').click()
                    assert not panel.count()
                    assert card.is_visible(), 'Closing worker details dismissed the approval'
                page.screenshot(path=str(output / f'frostpi-{item}-{tag.lower()}.png'))
                if item == 'cancel':
                    frame.get_by_role('button', name='Cancel', exact=True).click()
                else:
                    choice = (NATIVE_ALLOW if tag == 'NATIVE'
                              else 'Allow this exact command once' if expected[tag] else 'Deny')
                    frame.get_by_role('button', name=choice, exact=True).click()
                acted.add(tag)
                if tag == 'CRITICAL':
                    confirmation = frame.get_by_role('textbox', name=re.compile(r'Type APPROVE'))
                    confirmation.wait_for()
                    assert not (workspace / 'subagents-critical.txt').exists()
                    confirmation.fill('APPROVE')
                    confirmation.press('Enter')
            result = frame.get_by_text(parent_result(item), exact=True)
            failure = frame.get_by_text(re.compile(r'^FACTORY SUBAGENT FIXTURE FAILED'))
            result.or_(failure).last.wait_for(timeout=60_000)
            assert result.count() == 1, failure.last.inner_text()
            deadline = time.monotonic() + 45
            while any(marker not in fixture_trace(inside)[offset:] for marker in worker_markers(item)):
                if time.monotonic() >= deadline:
                    raise AssertionError(f'FrostPi child result did not arrive: {fixture_trace(inside)[offset:]}')
                page.wait_for_timeout(200)
            assert not frame.get_by_role('region', name='Prompt details').count()
            assert_files(workspace, item)
            assert_trace(mode, item, fixture_trace(inside)[offset:])
            if item in TERMINAL_CASES:
                wait_for_workers(inside, before, len(worker_markers(item)))
            if item == 'named':
                assert_named_workers(inside, names_before)
                juniper.get_by_text('FACTORY CHILD READ_BETA OK', exact=True).last.wait_for(timeout=12_000)
                juniper.get_by_text('complete', exact=True).wait_for(timeout=12_000)
                assert not juniper.get_by_text('FACTORY CHILD READ_ALPHA OK', exact=True).count()
                page.screenshot(path=str(output / 'frostpi-juniper-complete.png'))
                juniper.get_by_role('button', name='Close worker transcript').click()
                assert not juniper.count()
            if item in ('inline', 'specialist', 'nested'):
                worked = frame.get_by_role('button', name=re.compile(r'^Expand work trace:'))
                if worked.count():
                    worked.last.click()
                card = frame.locator('button.activity-trigger').filter(
                    has=frame.locator('.tool-activity-name', has_text=re.compile(r'^subagent$'))).last
                card.click(timeout=10_000)
                panel = card.locator('xpath=..')
                tabs = panel.get_by_role('tab', name='Output', exact=True)
                if tabs.count():
                    tabs.last.click()
                panel.locator('.tool-output').get_by_text(f'FACTORY CHILD {item.upper()} OK', exact=False).last.wait_for(
                    timeout=10_000)
            page.screenshot(path=str(output / f'frostpi-{item}-completed.png'))
            if item == 'named':
                frame.get_by_role('button', name='New or resume session', exact=True).click()
                frame.get_by_text('Resume session', exact=True).click()
                picker = page.locator('.quick-input-widget')
                picker.get_by_text('Atlas', exact=True).click(timeout=30_000)
                frame.locator('.session-title').filter(has_text=re.compile(r'^Atlas$')).wait_for(timeout=30_000)
                frame.get_by_text('FACTORY CHILD READ_ALPHA OK', exact=True).last.wait_for(timeout=30_000)
                page.screenshot(path=str(output / 'frostpi-worker-atlas.png'))
        except (BrowserTimeout, AssertionError):
            assert_unanswered_requests_did_not_run(workspace, expected, acted)
            page.screenshot(path=str(output / f'frostpi-{item}-failed.png'))
            raise
        print(f'PASS FrostPi {mode}: {item}', flush=True)
