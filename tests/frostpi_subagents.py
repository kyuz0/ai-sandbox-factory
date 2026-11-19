"""Checks against the installed Pi runtime, extension, and FrostPi webview."""

import json
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
import time

from playwright.sync_api import TimeoutError as BrowserTimeout

MODES = {
    'disabled': ('disabled',),
    'automatic': ('inline', 'specialist', 'nested', 'parallel', 'context', 'named'),
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
export default function(pi: ExtensionAPI) {
  pi.registerTool({
    name: 'fixture_echo',
    label: 'Fixture echo',
    description: 'Echo a synthetic marker.',
    parameters: Type.Object({ text: Type.String() }),
    async execute(_id, { text }) {
      await new Promise(resolve => setTimeout(resolve, 2500));
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


def check_native_subagents(container, workspace, inside, output, mode, case=None):
    for item in cases(mode, case):
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
        reset_files(workspace, item)
        offset = len(fixture_trace(inside))
        expected = expected_dialogs(mode, item)
        before = async_statuses(inside) if item in TERMINAL_CASES else {}
        if item == 'named':
            names_before = saved_worker_names(inside)
        send('Factory subagents ' + item)
        acted = set()
        try:
            if item in ('parallel', 'named'):
                widget = frame.locator('.extension-widget pre').filter(has_text=re.compile(
                    r'^Subagents(?:\n|$)|^PI_SUBAGENT_ASYNC_JSON:'))
                if item == 'named':
                    widget.filter(has_text='Juniper').wait_for(timeout=30_000)
                else:
                    widget.wait_for(timeout=15_000)
                page.wait_for_timeout(500)
                page.screenshot(path=str(output / f'frostpi-{item}-running.png'))
                label = widget.inner_text()
                assert 'PI_SUBAGENT_ASYNC_JSON:' not in label, 'FrostPi displayed raw subagent status JSON'
                pattern = r'^(Atlas|Juniper) · (?:queued|running|complete)$' if item == 'named' else (
                    r'^assistant \(([0-9a-f]{8})\) · (?:queued|running|complete)$')
                two_workers = re.findall(pattern, label, re.M)
                assert len(set(two_workers)) == 2 and len(label.splitlines()) == 3, label
                assert widget.evaluate('(node) => node.scrollWidth <= node.clientWidth'), label
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
