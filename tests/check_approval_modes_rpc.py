"""Exercise approval dialogs, decisions and side effects through installed Pi RPC."""
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
from threading import Thread
import time


def run_case(name, case):
    mode = os.environ['SANDBOX_APPROVAL_MODE']
    target = 'pi_' + name
    output = Path(f'/workspace/approval_{target}.txt')
    assert not output.exists(), output
    answers = list(case['answers'])
    process = subprocess.Popen(
        ['pi', '--mode', 'rpc', '--no-session', '--provider', 'fixture', '--model', 'fixture-model'],
        cwd='/workspace', stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    received = Queue()
    errors = []

    def receive():
        for line in process.stdout:
            received.put(line)
        received.put(None)

    def send(event):
        process.stdin.write(json.dumps(event) + '\n')
        process.stdin.flush()

    Thread(target=receive, daemon=True).start()
    Thread(target=lambda: errors.extend(process.stderr.readlines()), daemon=True).start()
    try:
        send({'id': 'fixture', 'type': 'prompt', 'message': f'RUN_APPROVAL_MODE pi {name}'})
        deadline = time.monotonic() + 180
        results = []
        late_answer = None
        while True:
            line = received.get(timeout=max(0.001, deadline - time.monotonic()))
            assert line is not None, (name, errors)
            event = json.loads(line)
            if event.get('type') == 'extension_ui_request' and event.get('method') in ('select', 'input', 'confirm'):
                assert answers, ('Unexpected approval prompt', name, event)
                answer = answers.pop(0)
                response = {'type': 'extension_ui_response', 'id': event['id']}
                if answer in ('APPROVE', 'wrong'):
                    assert event['method'] == 'input' and 'Type APPROVE' in event['title'], event
                    response['value'] = answer
                else:
                    assert event['method'] == 'select' and target in event['title'], event
                    if name == 'long':
                        assert 'VISIBLE_END_MARKER' in ''.join(event['title'].split()), event
                    if mode == 'assisted':
                        assert 'The synthetic reviewer checked this exact action.' in event['title'], event
                        assert len(event['options']) == 2 and 'Deny' in event['options'], event
                    if answer == 'cancel':
                        response['cancelled'] = True
                    else:
                        label = next((option for option in event['options']
                                      if 'session' not in option.lower() and
                                      (option == 'Yes' or option.startswith(('Allow', 'Approve')))), None)
                        assert label, event
                        response['value'] = label if answer in ('approve', 'abort') else 'Deny'
                        if answer == 'abort':
                            late_answer = response
                            send({'type': 'abort', 'id': 'abort-fixture'})
                            continue
                send(response)
            if event.get('type') == 'tool_execution_end':
                results.append(event)
            if event.get('type') == 'agent_settled':
                break
        assert not answers, (name, answers, results)
        if name == 'abort':
            assert late_answer, (name, results)
            send(late_answer)
            send({'type': 'get_state', 'id': 'after-late-answer'})
            while True:
                line = received.get(timeout=max(0.001, deadline - time.monotonic()))
                assert line is not None, errors
                state = json.loads(line)
                if state.get('type') == 'response' and state.get('id') == 'after-late-answer':
                    assert state['success'] and not state['data']['isStreaming'], state
                    break
        else:
            count = 2 if name == 'repeat' else 1
            assert [e['toolName'] for e in results] == [case['tool']] * count, (name, results, errors)
        actual = output.read_text() if output.exists() else ''
        assert actual == target * case['writes'], (name, actual, results)
        if name == 'protected':
            assert results[0]['isError'] and 'PROTECTED_FIXTURE' not in json.dumps(results), results
        if name == 'external':
            assert not results[0]['isError'] and 'EXTERNAL_FIXTURE' in json.dumps(results), results
        print('PASS Pi RPC:', mode, name, flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


for scenario, spec in json.loads(Path('/tmp/approval-mode-cases.json').read_text()).items():
    run_case(scenario, spec)
