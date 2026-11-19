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
    'automatic': ('inline', 'specialist', 'nested', 'parallel', 'context'),
    'assisted': ('inline', 'background', 'cancel'),
    'manual': ('manual',),
    'off': ('off',),
}
APPROVALS = {
    'inline': {'DENY': False},
    'background': {'ALPHA': True, 'BETA': False},
    'cancel': {'CANCEL': False},
    'manual': {'MANUAL': True},
}
TERMINAL_CASES = {'background', 'parallel', 'cancel'}
APPROVAL_TAG = re.compile(r'FACTORY_REVIEW_(ALLOW|DENY|ALPHA|BETA|CANCEL|MANUAL)\b')
SUCCESS_FILES = {
    'inline': {'inline': 'FACTORY_NATIVE_EDIT', 'allow': 'FACTORY_REVIEW_ALLOW'},
    'background': {'alpha': 'FACTORY_REVIEW_ALPHA'},
    'manual': {'manual': 'FACTORY_REVIEW_MANUAL'},
    'off': {'off': 'FACTORY_OFF'},
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
    if case is not None:
        if case not in MODES[mode]:
            raise ValueError(f'{case} is not a fixture for {mode}')
        return (case,)
    return MODES[mode]


def expected_dialogs(mode, case):
    return {} if mode == 'automatic' else APPROVALS.get(case, {})


def parent_result(case):
    status = 'STARTED' if case in TERMINAL_CASES else 'OK'
    return f'FACTORY SUBAGENTS {case.upper()} {status}'


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
        'parallel': ('SUBAGENT CHILD READ_ALPHA OK', 'SUBAGENT CHILD READ_BETA OK'),
        'cancel': ('SUBAGENT CHILD CANCEL OK',),
    }.get(case, ())


def assert_trace(mode, case, trace):
    assert not any(' FAIL ' in line for line in trace), trace
    decisions = [line.split()[2:] for line in trace if re.fullmatch(r'SUBAGENT REVIEW \w+ (allow|deny|defer)', line)]
    expected = {
        'inline': [['ALLOW', 'allow'], ['DENY', 'deny']],
        'context': [['CONTEXT', 'deny']],
        'background': [['ALPHA', 'defer'], ['BETA', 'defer']],
        'cancel': [['CANCEL', 'defer']],
    }.get(case, [])
    assert sorted(decisions) == sorted(expected), trace
    if case in ('background', 'parallel'):
        roles = ('ALPHA', 'BETA') if case == 'background' else ('READ_ALPHA', 'READ_BETA')
        starts = [next(i for i, line in enumerate(trace) if line.startswith(f'SUBAGENT CHILD {role} START '))
                  for role in roles]
        first_finish = min(trace.index(marker) for marker in worker_markers(case))
        assert max(starts) < first_finish, trace
    if case == 'context':
        assert any(line.startswith('SUBAGENT REVIEW CONTEXT SOURCE root=true ')
                   for line in trace), f'Child reviewer omitted the original parent user prompt: {trace}'
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


def wait_for_workers(inside, before, workers, timeout=20):
    deadline = time.monotonic() + timeout
    statuses = {}
    while time.monotonic() < deadline:
        statuses = {key: value for key, value in async_statuses(inside).items() if key not in before}
        if len(statuses) == workers and all(value == 'complete' for value in statuses.values()):
            return statuses
        time.sleep(0.25)
    raise AssertionError(f'Expected {workers} new completed background runs, observed {statuses}')


def rpc_case(container, case, mode, workspace, inside, output):
    reset_files(workspace, case)
    offset = len(fixture_trace(inside))
    before = async_statuses(inside) if case in TERMINAL_CASES else {}
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
                if event.get('type') == 'extension_ui_request' and event.get('method') in ('select', 'confirm'):
                    detail = ' '.join(str(event.get(key, '')) for key in ('title', 'message'))
                    tag = APPROVAL_TAG.search(detail)
                    assert tag and tag[1] in expected and tag[1] not in dialogs, detail
                    dialogs[tag[1]] = event['id']
                    allowed = expected[tag[1]]
                    response = {'type': 'extension_ui_response', 'id': event['id']}
                    if case == 'cancel':
                        response['cancelled'] = True
                    elif event['method'] == 'confirm':
                        response['confirmed'] = allowed
                    else:
                        pattern = r'^(Allow.*once|Yes)$' if allowed else r'^(Deny|No)$'
                        options = event.get('options', [])
                        response['value'] = next((x for x in options if re.match(pattern, x)), None)
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


def check_frostpi_subagents(page, frame, send, workspace, inside, output, mode, case=None):
    for item in cases(mode, case):
        reset_files(workspace, item)
        offset = len(fixture_trace(inside))
        expected = expected_dialogs(mode, item)
        before = async_statuses(inside) if item in TERMINAL_CASES else {}
        send('Factory subagents ' + item)
        acted = set()
        try:
            if item == 'parallel':
                widget = frame.locator('.extension-widget pre').filter(has_text=re.compile(
                    r'^Subagents(?:\n|$)|^PI_SUBAGENT_ASYNC_JSON:'))
                widget.wait_for(timeout=15_000)
                page.wait_for_timeout(500)
                page.screenshot(path=str(output / 'frostpi-parallel-running.png'))
                label = widget.inner_text()
                assert 'PI_SUBAGENT_ASYNC_JSON:' not in label, 'FrostPi displayed raw subagent status JSON'
                two_workers = re.findall(r'^assistant \(([0-9a-f]{8})\) · (?:queued|running|complete)$', label, re.M)
                assert len(set(two_workers)) == 2 and len(label.splitlines()) == 3, label
                assert widget.evaluate('(node) => node.scrollWidth <= node.clientWidth'), label
            card = frame.get_by_role('region', name='Prompt details')
            while len(acted) < len(expected):
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    detail = card.inner_text(timeout=2_000) if card.is_visible() else ''
                    tag = APPROVAL_TAG.search(detail)
                    if tag and tag[1] not in acted:
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
                assert tag[1] in expected, detail
                assert 'Command' in detail and 'Justification' in detail and 'Execution scope' in detail, detail
                if mode == 'assisted':
                    assert f'Synthetic decision for the {tag[1]} workspace marker.' in detail, detail
                page.screenshot(path=str(output / f'frostpi-{item}-{tag[1].lower()}.png'))
                if item == 'cancel':
                    frame.get_by_role('button', name='Cancel', exact=True).click()
                else:
                    choice = 'Allow this exact command once' if expected[tag[1]] else 'Deny'
                    frame.get_by_role('button', name=choice, exact=True).click()
                acted.add(tag[1])
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
        except (BrowserTimeout, AssertionError):
            assert_unanswered_requests_did_not_run(workspace, expected, acted)
            page.screenshot(path=str(output / f'frostpi-{item}-failed.png'))
            raise
        print(f'PASS FrostPi {mode}: {item}', flush=True)
