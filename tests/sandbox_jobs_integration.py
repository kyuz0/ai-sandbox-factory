"""Exercise dashboard jobs with disposable real Podman sandboxes on Linux."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails, JobsMenu
from ai_sandbox_factory.sandbox_tui import Cockpit, CreationRequest, DEFAULT_SETTINGS, SandboxTable, Service, preset
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
                for job in app.jobs:
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
        assert len(app.screen.labels) == 3
        assert any(label.startswith('Start sandbox · ') and label.endswith(' · Complete') for label in app.screen.labels)
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


async def verify_creation(app, service, root, existing):
    name = root.name + '-creation'
    workspace = root / name
    workspace.mkdir()
    draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
    draft.update(name=name, workspace=str(workspace), engine='podman', network='unrestricted',
                 open_editor=False, allowlist='')
    draft['options']['browser'] = False
    with patch('ai_sandbox_factory.updates.available_update', return_value=None):
        async with app.run_test(size=(80, 24)) as pilot:
            job = None
            try:
                await wait_for(pilot, lambda: len(app.records) == 2)
                with sandbox.image_graph(service.config(existing)):
                    job = app.start_creation(CreationRequest(draft, {}, None, False))
                    await wait_for(pilot, lambda: 'Build sandbox images' in job.status)
                    assert 'Step ' in job.status, job.status
                    await pilot.press('escape')
                    assert app.screen is app.screen_stack[0]
                    table = app.query_one(SandboxTable)
                    assert str(table.get_cell(name, 'dev')) == 'Creating'
                    assert str(table.get_cell(name, 'open')).strip() == 'View job'
                    table.move_cursor(row=table.get_row_index(name), column=1)
                    table.focus()
                    await pilot.press('enter')
                    assert app.screen.job is job
                    await pilot.press('ctrl+q')
                    assert app.screen.heading == 'Jobs still running'
                    await pilot.press('escape', 'escape')
                    conflict = await asyncio.to_thread(subprocess.run, service.command('down', name),
                                                       capture_output=True, text=True)
                    assert conflict.returncode and f'Cannot down {name}' in conflict.stderr, conflict
                    app.show_sandbox_actions(existing)
                    await pilot.pause()
                    menu = app.screen
                await app.workers.wait_for_complete()
                assert job.result == 'Complete', job.read(0)
                assert app.screen is menu, 'Hidden creation changed the active screen'
                await pilot.press('escape', 'j', 'enter')
                await wait_for(pilot, lambda: isinstance(app.screen, JobDetails))
                assert app.screen.job is job
                await wait_for(pilot, lambda: name + ' is ready.' in output(app.screen))
                assert '--creation-token' not in output(app.screen) or '[redacted]' in output(app.screen)
                config = service.config(name)
                assert sandbox.inspect(config, sandbox.names(config)[0])['State']['Running']
                print('PASS: real creation reports a build step, hides during build, blocks conflicting CLI work, '
                      'preserves focus and reopens its output.', flush=True)
            finally:
                if job and job.result is None:
                    job.cancel()
                await app.workers.wait_for_complete()


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
            creation = root.name + '-creation'
            try:
                asyncio.run(verify_creation(Cockpit(state, service), service, root, names[0]))
            finally:
                if (state / creation).exists():
                    subprocess.run(service.command('delete', creation), check=True)
        finally:
            for name in created:
                subprocess.run(service.command('delete', name), check=True)


if __name__ == '__main__':
    main()
