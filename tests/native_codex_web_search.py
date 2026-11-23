#!/usr/bin/env python3
"""Check Factory's web-search choice in native CLI and VS Code.

Use only an existing disposable Codex sandbox. This changes its saved web-search
choice, reloads the browser editor and restores the original saved choice afterward.
Evidence contains effective native config and native tool events, never auth data.
"""
import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_sandbox_factory import sandbox
from native_codex_features import AppServer, sanitized
from native_codex_controls import composer_frame
from native_browser_editor import verify_reply
from playwright.sync_api import sync_playwright


def select(state, enabled):
    config = json.loads((state / 'config.json').read_text())
    sandbox.apply_settings(config, state, {'codex_web_search': enabled})
    assert json.loads((state / 'config.json').read_text())['options']['codex_web_search'] is enabled


def effective(container, home, timeout):
    server = AppServer(container, home)
    try:
        deadline = time.monotonic() + timeout
        server.call('initialize', {'clientInfo': {'name': 'factory-web-search-test', 'version': '1'},
                    'capabilities': {'experimentalApi': True}}, deadline)
        mode = server.call('config/read', {'cwd': '/workspace', 'includeLayers': False}, deadline)['config']['web_search']
        # Native Codex resolves an unset web_search choice to cached mode.
        return mode or 'cached'
    finally:
        server.close()


def prompt(enabled, marker):
    if enabled:
        return ('Use native web_search once to find the official OpenAI Codex CLI documentation. '
                f'Do not use shell, browser or MCP. Then reply {marker}.')
    return ('If the native web_search tool is available, call it once to find official OpenAI Codex CLI documentation. '
            f'If that tool is absent, reply {marker}. Do not use shell, browser or MCP.')


def cli_check(args, enabled):
    mode = effective(args.workspace_container, args.cli_home, args.timeout)
    assert mode == ('cached' if enabled else 'disabled'), f'CLI effective web_search was {mode}'
    marker = 'SEARCH-' + secrets.token_hex(6)
    command = ['podman', 'exec', '-e', 'CODEX_HOME=' + args.cli_home, args.workspace_container,
               '/usr/local/bin/codex', 'exec', '--json', '--skip-git-repo-check', '-C', '/workspace',
               '--model', args.model, '-c', 'model_reasoning_effort="low"', prompt(enabled, marker)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
    assert result.returncode == 0, (f'Native CLI in {args.workspace_container} exited with status {result.returncode}. '
                                    f'Stderr: {sanitized(result.stderr)}\nStdout: {sanitized(result.stdout)}')
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    types = [entry['item']['type'] for entry in records if entry.get('type') == 'item.completed']
    searches = sum(kind in ('web_search', 'web_search_call', 'webSearch') for kind in types)
    assert bool(searches) is enabled, f'CLI search tool event count was {searches}, enabled={enabled}'
    assert any(entry.get('type') == 'turn.completed' for entry in records), 'CLI turn unfinished'
    return {'client': 'cli', 'enabled': enabled, 'native_config': mode, 'search_tool_events': searches}


def ui_records(container, marker):
    source = r'''
import json, pathlib, sys
marker = sys.argv[1]
for path in pathlib.Path('/home/node/.codex/sessions').rglob('*.jsonl'):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    start = None
    for i, row in enumerate(rows):
        p = row.get('payload', {})
        if ((p.get('type') == 'user_message' or (p.get('type') == 'message' and p.get('role') == 'user'))
                and marker in json.dumps(p)):
            start = i
            break
    if start is None:
        continue
    types = [row.get('payload', {}).get('type') for row in rows[start:]]
    print(json.dumps({'search_tool_events': sum(t == 'web_search_end' for t in types) or sum(t == 'web_search_call' for t in types),
                      'session_record': path.name, 'native_event_types': sorted({t for t in types if t})}))
    break
else:
    raise SystemExit('Native VS Code session record for the synthetic test prompt was not found')
'''
    output = subprocess.check_output(['podman', 'exec', container, 'python3', '-c', source, marker], text=True)
    return json.loads(output)


def reload_codex(page, timeout):
    # Focus the VS Code shell so a webview cannot consume its keyboard shortcut.
    page.get_by_role('tab', name='Explorer (Ctrl+Shift+E)', exact=True).click()
    page.keyboard.press('Control+Shift+P')
    palette = page.get_by_role('textbox', name='Type the name of a command to run.', exact=True)
    palette.fill('>Developer: Reload Window')
    with page.expect_navigation(wait_until='domcontentloaded', timeout=timeout * 1000):
        palette.press('Enter')
    return composer_frame(page, timeout)


def ui_check(args, page, enabled):
    reload_codex(page, args.timeout)
    mode = effective(args.workspace_container, '/home/node/.codex', args.timeout)
    assert mode == ('cached' if enabled else 'disabled'), f'VS Code native config was {mode}'
    marker = 'SEARCH-' + secrets.token_hex(6)
    verify_reply(page, prompt(enabled, marker), marker, args.timeout)
    evidence = ui_records(args.workspace_container, marker)
    assert bool(evidence['search_tool_events']) is enabled, f'VS Code search event count differs: {evidence}'
    if args.output:
        page.screenshot(path=str(args.output / ('vscode-enabled.png' if enabled else 'vscode-disabled.png')))
    return {'client': 'vscode', 'enabled': enabled, 'native_config': mode, **evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--workspace-container', required=True)
    parser.add_argument('--url')
    parser.add_argument('--chromium', required=True)
    parser.add_argument('--cli-home', default='/home/node/.codex-web-search-test')
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.url:
        args.url = json.loads((args.state / 'browser-editor-runtime.json').read_text())['url']
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    config = json.loads((args.state / 'config.json').read_text())
    assert config.get('primary_agent') == 'codex', 'The supplied fixture must be a Codex sandbox'
    original = config.get('options', {}).get('codex_web_search', False)
    results = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
            try:
                page = browser.new_page(viewport={'width': 1500, 'height': 1000})
                page.goto(args.url + '/?folder=/workspace')
                for enabled in (True, False):
                    select(args.state, enabled)
                    results.append(cli_check(args, enabled))
                    print(json.dumps(results[-1]), flush=True)
                    results.append(ui_check(args, page, enabled))
                    print(json.dumps(results[-1]), flush=True)
                select(args.state, original)
                expected = 'cached' if original else 'disabled'
                assert effective(args.workspace_container, args.cli_home, args.timeout) == expected
                reload_codex(page, args.timeout)
                assert effective(args.workspace_container, '/home/node/.codex', args.timeout) == expected
            finally:
                browser.close()
    finally:
        select(args.state, original)
    results.append({'restored_original_and_reopened': True, 'enabled': original})
    if args.output:
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results[-1]), flush=True)


if __name__ == '__main__':
    main()
