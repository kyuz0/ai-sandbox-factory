"""Drive the child compaction fixture through the installed clients."""

import json

from frostpi_subagents import async_statuses, wait_for_workers


def prepare(workspace, inside):
    (workspace / 'compact-seed.txt').write_text('Cobalt calibration token: COBALT_VALUE_8123')
    (workspace / 'compact-tail.txt').write_text('TAIL_CONTENT ' * 96)
    settings_file = '/home/node/.pi/agent/settings.json'
    settings = json.loads(inside('cat', settings_file))
    settings['compaction'] = {'enabled': True, 'modelOverrides': {
        'fixture/fixture': {'reserveTokens': 65536, 'keepRecentTokens': 64}}}
    inside('tee', settings_file, input=json.dumps(settings))


def read_trace(inside):
    return [json.loads(line) for line in inside('cat', '/tmp/fixture.log').splitlines() if line.startswith('{')]


def saved_checkpoint(inside, key):
    return json.loads(inside('python3', '-c', """
import json, sys
from pathlib import Path
key = sys.argv[1]
paths = []
for root in ['/tmp', '/workspace', '/home/node/.pi', '/home/node/.local/share/pi-browser-editor']:
    paths.extend(Path(root).rglob('session.jsonl'))
for path in paths:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    checkpoints = [r for r in rows if r.get('type') == 'compaction' and 'FIXTURE_CHECKPOINT_' + key in r['summary']]
    if checkpoints:
        header = rows[0]
        parent = Path(header['parentSession'])
        parents = [json.loads(line) for line in parent.read_text().splitlines() if line]
        assert len(checkpoints) == 1
        assert not any(r.get('type') == 'compaction' for r in parents)
        assert header['id'] != parents[0]['id']
        assert any('Factory compaction ' + key in json.dumps(r) for r in parents)
        assert checkpoints[0]['tokensBefore'] >= 1_000_000
        print(json.dumps({'child':header['id'], 'parent':parents[0]['id'], 'tokensBefore':checkpoints[0]['tokensBefore'],
                          'childCompactions':len(checkpoints), 'parentCompactions':0}))
        break
else:
    raise AssertionError('No saved child checkpoint in ' + str(paths))
""", key))


def run(client, inside, output, mode, invoke):
    for worker in ('foreground', 'background', 'specialist'):
        key = f'{client}-{worker}'
        prompt = f'Factory compaction {key}. Private parent marker PARENT_PRIVATE_7294.'
        before = async_statuses(inside) if worker == 'background' else None
        invoke(prompt, 'PARENT FINISHED ' + key, key)
        if before is not None:
            wait_for_workers(inside, before, 1, timeout=90)
        trace = read_trace(inside)
        assert not any(event['event'] == 'error' for event in trace), trace
        events = [event for event in trace if event.get('id') == key]
        order = [event['event'] for event in events]
        assert 'summary' in order and order.count('child-finished') == 1, events
        assert any(event.get('containsPrivate') for event in events if event['event'] == 'summary'), events
        children = [event for event in events if event['event'] == 'child']
        assert [e['checkpoint'] for e in children[:3]] == [False, False, True], events
        assert not any(event.get('leakedParent') for event in children), events
        assert not any(event.get('leakedSecret') for event in events if event['event'] == 'parent-result'), events
        recall = [] if mode == 'off' else ['history_expand', 'history_grep'] + (['history_recall'] if mode == 'full' else [])
        assert next(event for event in events if event['event'] == 'parent')['tools'] == recall, events
        if worker == 'specialist':
            recall = []
        assert all(child['tools'] == recall for child in children), events
        assert ('expand-found' in order) == bool(recall), events
        assert ('lexical-found' in order) == ('history_recall' in recall), events
        if recall:
            negative = next(event for event in events if event['event'] == 'grep-parent')
            assert 'No matches in compacted history' in negative['result'] and 'totalEntries=0' in negative['result'], negative
        (output / f'{key}-checkpoint.json').write_text(json.dumps(saved_checkpoint(inside, key), indent=2) + '\n')
        print(f'PASS {client} {worker}, {mode} recall, automatic child compaction at one million reported tokens', flush=True)


def check_pi(inside, output, mode):
    def invoke(prompt, expected, key):
        result = inside('pi', '--print', '--approve', '--session', f'/tmp/{key}.jsonl',
                        '--provider', 'fixture', '--model', 'fixture', prompt)
        (output / f'{key}.txt').write_text(result)
        assert expected in result, result
    run('pi', inside, output, mode, invoke)


def check_frostpi(page, frame, send, inside, output, mode):
    def invoke(prompt, expected, key):
        send(prompt)
        frame.get_by_text(expected, exact=True).last.wait_for(timeout=90_000)
        page.screenshot(path=str(output / f'{key}.png'))
    run('frostpi', inside, output, mode, invoke)
