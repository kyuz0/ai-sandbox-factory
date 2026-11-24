"""Exercise dashboard jobs with disposable real Podman sandboxes on Linux."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails, JobsMenu
from ai_sandbox_factory.sandbox_tui import Cockpit, Service
from test_sandbox_jobs import begin, output, wait_for


async def exercise(state, service, names):
    first_config = service.config(names[0])
    first_container = sandbox.inspect(first_config, sandbox.names(first_config)[0])['Id']
    with patch('ai_sandbox_factory.updates.available_update', return_value=None):
        app = Cockpit(state, service)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await verify_rebuilds(pilot, app, service, names, first_config, first_container)
            finally:
                for job in app.rebuild_jobs:
                    if job.result is None:
                        job.cancel()
                await app.workers.wait_for_complete()


async def verify_rebuilds(pilot, app, service, names, first_config, first_container):
    alpha, beta = names
    await wait_for(pilot, lambda: len(app.records) == 2)
    # Hold the real shared build lock so both CLI children wait while
    # we exercise the TUI, a second CLI and cancellation deterministically.
    with sandbox.image_graph(first_config):
        first = await begin(pilot, app, alpha)
        await wait_for(pilot, lambda: 'Waiting for another build' in output(app.screen))
        await pilot.press('escape')
        conflict = await asyncio.to_thread(subprocess.run, service.command('down', alpha),
                                           capture_output=True, text=True)
        assert conflict.returncode and f'Cannot down {alpha}' in conflict.stderr, conflict
        app.perform('start', record=next(r for r in app.records if r['name'] == beta))
        await wait_for(pilot, lambda: getattr(app.screen, 'finished', False))
        assert app.screen.succeeded
        await pilot.press('escape')
        second = await begin(pilot, app, beta)
        await wait_for(pilot, lambda: 'Waiting for another build' in output(app.screen))
        await pilot.press('escape', 'j')
        assert isinstance(app.screen, JobsMenu)
        assert len(app.screen.labels) == 2
        await pilot.press('down', 'enter')
        assert isinstance(app.screen, JobDetails) and app.screen.job is first
        await wait_for(pilot, lambda: 'Waiting for another build' in output(app.screen))
        await pilot.click('#job-cancel')
        await pilot.click('#yes')
        await wait_for(pilot, lambda: first.result == 'Cancelled')
        assert sandbox.inspect(first_config, sandbox.names(first_config)[0])['Id'] == first_container
        await pilot.press('escape')
    # B now runs the real build and replacement. Wait for the workload,
    # including a cold image build if the cache is empty.
    await app.workers.wait_for_complete()
    assert second.result == 'Complete', second.read(0)
    assert app.screen is app.screen_stack[0]
    await pilot.press('j', 'enter')
    assert app.screen.job is second
    await wait_for(pilot, lambda: 'Update complete.' in output(app.screen))
    assert alpha not in output(app.screen), 'Another job wrote to the completion log'
    config = service.config(beta)
    info = sandbox.inspect(config, sandbox.names(config)[0])
    expected = sandbox.run(['podman', 'image', 'inspect', sandbox.image_names(config)[0]], capture=True)
    assert info['State']['Running'] and info['Image'] == json.loads(expected.stdout)[0]['Id']
    for name in names:
        content = Path(service.config(name)['workspace'], 'receipt.txt').read_text()
        assert content == name, content
    print('PASS: two real jobs in one TUI, working on B while A waits, cross-process conflict, '
          'cancelled A preserved, rebuilt B started from the intended image and isolated logs reopened.', flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix='factory-jobs-') as tmp:
        root = Path(tmp)
        state = root / 'state'
        service = Service(state)
        identifier = uuid4().hex[:10]
        names = [f'jobs-{identifier}-{suffix}' for suffix in ('a', 'b')]
        created = []
        try:
            for name in names:
                workspace = root / name
                workspace.mkdir()
                (workspace / 'receipt.txt').write_text(name)
                subprocess.run(service.command('create', name, '--workspace', str(workspace),
                                               '--engine', 'podman', '--network', 'unrestricted', '--no-browser'), check=True)
                created.append(name)
            subprocess.run(service.command('build', names[0]), check=True)
            subprocess.run(service.command('up', names[0]), check=True)
            asyncio.run(exercise(state, service, names))
        finally:
            for name in created:
                subprocess.run(service.command('delete', name), check=True)


if __name__ == '__main__':
    main()
