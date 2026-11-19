"""Exercise two real Chaucer/DeepSeek workers in each Factory client.

Run against a fresh, disposable Factory sandbox with subagents enabled and the
browser editor already running. No provider credentials are read by this script.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pi_sandbox_factory import browser_editor, sandbox

MODEL = 'eu-deepseek-v4-flash'
WORKER_ROLE = 'fixture-arithmetic'


def probe_workspace(state, name):
    config = json.loads((state / name / 'config.json').read_text())
    assert config['subagents'] and config['engine'] == 'podman'
    assert config['network'] == 'allowlist'
    assert [row['name'] for row in config['model_providers']] == ['chaucer']
    return Path(config['workspace']), sandbox.names(config)[0]


def prompt_for(workspace, prefix):
    workers = []
    for role, number in (('alpha', 17), ('beta', 29)):
        name = f'{prefix}-{role}'
        (workspace / f'{name}-seed.txt').write_text(str(number))
        task = (
            f'Work only in /workspace. Use the read tool to get the number from /workspace/{name}-seed.txt. '
            f'Add 3 and use the write tool to save only the result to /workspace/{name}-result.txt. '
            f'Reply {name}-done with your result. Use no other tools, network, or escalation.'
        )
        workers.append({'agent': WORKER_ROLE, 'task': task,
                        'options': {'acceptance': {'level': 'none', 'reason': 'The test verifies both files.'}}})
    arguments = [{**worker, 'async': True} for worker in workers]
    return (
        'Run this disposable subagent test. Make exactly two separate subagent tool calls, one for each '
        'argument object below. The seed files already exist. Do not read, create or change any files '
        'yourself. Do not use a workflow or change the arguments. Once both are launched, return control '
        'so their background completion can wake you. Arguments for the two separate calls: '
        + json.dumps(arguments)
    )


def verify_files(workspace, prefix):
    for role, expected in (('alpha', '20'), ('beta', '32')):
        path = workspace / f'{prefix}-{role}-result.txt'
        assert path.exists(), f'Worker {role} produced no file'
        assert path.read_text().strip() == expected, f'Worker {role} produced incorrect output'


def workers_finished(workspace, prefix):
    return all((workspace / f'{prefix}-{role}-result.txt').exists() for role in ('alpha', 'beta'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium')
    args = parser.parse_args()
    workspace, container = probe_workspace(args.state_dir, args.name)
    runtime = browser_editor.runtime(args.state_dir / args.name)
    if not runtime:
        parser.error('Start the browser editor for this disposable sandbox first.')
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    run = 'subagent-' + uuid4().hex[:8]

    def inside(*command):
        return subprocess.check_output(['podman', 'exec', '--workdir', '/workspace', container, *command], text=True)

    # Make the live tool contract explicit so the model cannot substitute Bash
    # for the read and write we are checking.
    agent_dir = '/home/node/.pi/agent/agents'
    inside('mkdir', '-p', agent_dir)
    subprocess.run(['podman', 'exec', '-i', container, 'tee', f'{agent_dir}/{WORKER_ROLE}.md'],
                   input=f'---\nname: {WORKER_ROLE}\ndescription: Read and write a disposable arithmetic fixture\n'
                         'tools: read, write\n---\nFollow the assigned test task. Do the arithmetic mentally.\n',
                   text=True, stdout=subprocess.DEVNULL, check=True)

    terminal = '/tmp/' + run + '.sock'
    inside('tmux', '-S', terminal, 'new-session', '-d', '-x', '135', '-y', '35',
           'pi', '--approve', '--provider', 'chaucer', '--model', MODEL, '--thinking', 'off')
    try:
        terminal_prefix = run + '-pi'
        prompt = prompt_for(workspace, terminal_prefix)
        deadline = time.monotonic() + 45
        while 'subagents' not in inside('tmux', '-S', terminal, 'capture-pane', '-p').lower():
            if time.monotonic() >= deadline:
                raise AssertionError('Pi did not show its startup settings in the terminal')
            time.sleep(0.2)
        inside('tmux', '-S', terminal, 'send-keys', '-l', prompt)
        inside('tmux', '-S', terminal, 'send-keys', 'Enter')
        deadline = time.monotonic() + 160
        while not workers_finished(workspace, terminal_prefix) and time.monotonic() < deadline:
            time.sleep(0.35)
        (output / 'pi-terminal.txt').write_text(inside('tmux', '-S', terminal, 'capture-pane', '-p', '-J', '-S', '-1000'))
        verify_files(workspace, terminal_prefix)
        # Capture completion feedback while this terminal remains attached.
        for _ in range(25):
            text = inside('tmux', '-S', terminal, 'capture-pane', '-p', '-J', '-S', '-1000')
            if 'completed' in text.lower():
                break
            time.sleep(0.2)
        (output / 'pi-terminal.txt').write_text(text)
        assert 'subagent' in text.lower(), 'Pi terminal did not render delegation'
        print('PASS live Chaucer/DeepSeek Pi terminal: both workers produced their own correct results', flush=True)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=args.chromium)
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            page.goto(runtime['url'])
            page.locator('.monaco-workbench').wait_for(timeout=60_000)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                frame = next((f for f in page.frames if f.locator('[aria-label="Message Pi"]').count()), None)
                if frame:
                    break
                page.wait_for_timeout(100)
            else:
                raise AssertionError('FrostPi did not open')
            frame.get_by_text(re.compile(re.escape(MODEL))).first.wait_for(timeout=30_000)
            editor_prefix = run + '-frostpi'
            frame.get_by_label('Message Pi', exact=True).fill(prompt_for(workspace, editor_prefix))
            frame.get_by_role('button', name='Send to Pi', exact=True).click()
            deadline = time.monotonic() + 160
            captured = False
            while not workers_finished(workspace, editor_prefix) and time.monotonic() < deadline:
                if not captured and frame.get_by_role('button', name=re.compile(r'^subagent\b')).count():
                    page.screenshot(path=str(output / 'frostpi-running.png'))
                    captured = True
                page.wait_for_timeout(250)
            verify_files(workspace, editor_prefix)
            page.wait_for_timeout(500)
            page.screenshot(path=str(output / 'frostpi-results.png'))
            browser.close()
            print('PASS live Chaucer/DeepSeek FrostPi: both workers produced their own correct results', flush=True)

        # The files alone could have been written by the parent. Check that two
        # distinct saved child sessions in each client received the named tasks.
        proof = '''
import json, sys
from pathlib import Path
root = Path.home() / ".pi/agent/sessions"
for client in ("pi", "frostpi"):
    ids = set()
    for role in ("alpha", "beta"):
        name = sys.argv[1] + "-" + client + "-" + role
        matches = set()
        for path in root.rglob("session.jsonl"):
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            messages = [entry["message"] for entry in entries if entry.get("type") == "message"]
            user = json.dumps([m for m in messages if m.get("role") == "user"])
            if name + "-done" not in user or "Run this disposable subagent test." in user:
                continue
            calls = [(part["name"], part["arguments"]) for message in messages if message.get("role") == "assistant"
                     for part in message.get("content", []) if part.get("type") == "toolCall"]
            expected = [("read", "/workspace/" + name + "-seed.txt"),
                        ("write", "/workspace/" + name + "-result.txt")]
            if expected == [(tool, arguments.get("path")) for tool, arguments in calls]:
                matches.add(path.parent.parent.name)
        if len(matches) != 1:
            raise SystemExit(f"{client} {role}: expected one matching child transcript, saw {len(matches)}")
        ids.update(matches)
    if len(ids) != 2:
        raise SystemExit(f"{client}: expected two independent child sessions")
    print(f"PASS {client}: two distinct child session files")
'''
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            checked = subprocess.run(['podman', 'exec', container, 'python3', '-c', proof, run],
                                     capture_output=True, text=True)
            if checked.returncode == 0:
                print(checked.stdout.strip(), flush=True)
                break
            time.sleep(0.35)
        else:
            raise AssertionError(checked.stderr)
    finally:
        subprocess.run(['podman', 'exec', container, 'tmux', '-S', terminal, 'kill-server'],
                       capture_output=True)


if __name__ == '__main__':
    main()
