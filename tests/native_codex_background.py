"""Exercise the native background-command controls through Factory's native workspace client."""
import json
import queue
import subprocess
import threading
import time


def check(editor, output, timeout):
    fixture = '''import os,signal,sys
from pathlib import Path
Path('/workspace/background-'+sys.argv[1]+'.pid').write_text(str(os.getpid()))
signal.pause()
'''
    subprocess.run(['podman', 'exec', '-i', editor, 'tee', '/workspace/background-fixture.py'],
                   input=fixture, text=True, stdout=subprocess.DEVNULL, check=True)
    messages = queue.Queue()
    process = subprocess.Popen(['podman', 'exec', '-i', editor, '/usr/local/bin/codex', 'app-server'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=(output / 'background.log').open('w'), text=True)

    def read():
        try:
            for line in process.stdout:
                messages.put(json.loads(line))
        finally:
            messages.put(None)

    threading.Thread(target=read, daemon=True).start()
    sequence = 0

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def call(method, params):
        nonlocal sequence
        sequence += 1
        identifier = sequence
        send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError('Native Codex connection closed')
            if message.get('id') == identifier:
                if 'error' in message:
                    raise RuntimeError(method + ' failed: ' + str(message['error']))
                return message.get('result')
            if 'id' in message and message.get('method', '').endswith('/requestApproval'):
                # Only the two harmless fixture jobs are authorized in this run.
                command = str(message.get('params', {}).get('command', ''))
                send({'id': message['id'], 'result': {'decision': 'accept' if 'background-fixture.py' in command else 'decline'}})
        raise RuntimeError('Native Codex request did not complete: ' + method)

    try:
        call('initialize', {'clientInfo': {'name': 'factory-background-check', 'version': '1'},
                            'capabilities': {'experimentalApi': True}})
        send({'method': 'initialized'})
        thread = call('thread/start', {})['thread']['id']
        call('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text':
            'Start two independent native terminal commands: python3 /workspace/background-fixture.py one '
            'and python3 /workspace/background-fixture.py two. Use a short tool yield so both remain resumable. '
            'Do not use shell ampersands, modify the scripts, stop the jobs, or wait for completion. '
            'The integration test will stop them through the native background-terminal controls.'}]})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = call('thread/backgroundTerminals/list', {'threadId': thread})['data']
            jobs = [job for job in jobs if 'background-fixture.py' in job['command']]
            if len(jobs) == 2:
                break
            time.sleep(.25)
        else:
            raise RuntimeError('Two resumable fixture commands were not available')
        pids = [subprocess.check_output(['podman', 'exec', editor, 'cat', '/workspace/background-' + name + '.pid'], text=True)
                for name in ('one', 'two')]
        result = call('thread/backgroundTerminals/terminate', {'threadId': thread, 'processId': jobs[0]['processId']})
        assert result['terminated'], result
        call('thread/backgroundTerminals/clean', {'threadId': thread})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listed = call('thread/backgroundTerminals/list', {'threadId': thread})['data']
            alive = [pid for pid in pids if subprocess.run(['podman', 'exec', editor, 'test', '-e', '/proc/' + pid]).returncode == 0]
            if not listed and not alive:
                break
            time.sleep(.25)
        else:
            raise RuntimeError('Background terminal cleanup left a fixture process running')
        (output / 'background-results.json').write_text(json.dumps({'listed': 2, 'terminated': True, 'cleaned': True,
                                                                  'remainingProcesses': 0}, indent=2))
        print('Native Codex listed two workspace jobs; terminate and clean stopped them, with no processes left.', flush=True)
    finally:
        process.stdin.close()
        process.wait(timeout=30)
