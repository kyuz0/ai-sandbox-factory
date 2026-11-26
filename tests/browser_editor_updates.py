"""Keep an authenticated editor at its original URL through real workspace replacements."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from ai_sandbox_factory import browser_editor, editor_settings, operation_locks, sandbox
from browser_editor_lifecycle import request


def check(root, image, proxy_image, archive):
    name = 'editor-updates-' + uuid4().hex[:12]
    state = root / 'state' / name
    state.mkdir(parents=True)
    project = root / 'project'
    project.mkdir()
    (project / 'project-marker').write_text('project preserved\n')
    config = {'name': name, 'engine': 'podman', 'workspace': str(project),
              'uid': os.getuid(), 'gid': os.getgid(), 'network': 'allowlist', 'memory': '2g',
              'profile': sandbox.load_profile('base'), 'options': {'browser': False},
              'common': {'path': str(sandbox.ROOT / 'containers/dev')}}
    (state / 'allowlist.txt').write_text('')
    (state / 'squid.conf').write_text(sandbox.network_policy(config, []))
    sandbox.write_json(state / 'config.json', config)
    cache = state.parent / '.browser-editor-downloads'
    cache.mkdir()
    (cache / archive.name).symlink_to(archive)
    editor_settings.save(state, {'mode': 'browser',
        'password_hash': editor_settings.password_hash('disposable-editor-password')})
    workspace = sandbox.names(config)[0]

    def saved():
        return json.loads((state / 'config.json').read_text())

    def inside(*args):
        return sandbox.run(['podman', 'exec', workspace, *args], capture=True).stdout

    def update():
        current = saved()
        sandbox.apply_settings(current, state, {'memory': '3g' if current['memory'] == '2g' else '2g'})

    def rebuild():
        sandbox.sandbox_command(SimpleNamespace(command='recreate', profile=None,
            subagents=None, browser=None, apply_gate=None), state, None)

    connection = None
    cookie = None

    def verify(stage):
        current = browser_editor.runtime(state)
        assert current and current['url'] == connection['url'], (stage, current)
        assert current['preferences'] == connection['preferences'], stage
        assert current['container'] == sandbox.inspect(saved(), workspace)['Id'], stage
        assert request(connection['url'], headers={'Cookie': cookie})[0] == 200, stage
        assert request(connection['url'])[0] == 302, stage
        assert inside('cat', '/home/node/update-marker') == 'private home preserved\n', stage
        assert (project / 'project-marker').read_text() == 'project preserved\n', stage
        print('PASS same browser URL and authenticated cookie, project and private home: ' + stage, flush=True)

    # Cached images replace only the build cost. All engine operations, bootstrap,
    # editor processes, authentication and replacement/rollback code are real.
    with operation_locks.sandbox(name, 'test browser editor updates in'), \
            patch.object(sandbox, 'image_names', return_value=(image, proxy_image)), \
            patch.object(sandbox, 'build'):
        try:
            sandbox.up(config, state)
            inside('sh', '-c', 'printf "private home preserved\\n" > /home/node/update-marker')
            update()
            rebuild()
            assert browser_editor.runtime(state) is None
            print('PASS update and rebuild do not launch an editor that was not open', flush=True)
            browser_editor.open_browser_editor(saved(), state, archive=str(archive),
                                               background=True, open_tab=False)
            connection = browser_editor.runtime(state)
            status, headers, _ = request(connection['url'], '/login', method='POST',
                body=urlencode({'password': 'disposable-editor-password'}),
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
            assert status == 302, status
            cookie = headers['Set-Cookie'].split(';')[0]
            verify('initial editor')
            for action in (update, rebuild):
                previous = sandbox.inspect(saved(), workspace)['Id']
                action()
                assert sandbox.inspect(saved(), workspace)['Id'] != previous
                verify(action.__name__)
                original = saved()
                previous = sandbox.inspect(original, workspace)['Id']
                up = sandbox.up
                failed = []

                def fail_candidate(candidate, target, **kwargs):
                    up(candidate, target, **kwargs)
                    if not failed:
                        failed.append(True)
                        raise ValueError('Injected candidate startup failure after readiness')

                with patch.object(sandbox, 'up', side_effect=fail_candidate):
                    try:
                        action()
                    except ValueError as error:
                        assert 'Injected candidate startup failure' in str(error), str(error)
                    else:
                        raise AssertionError('Candidate failure was not reported')
                assert saved() == original
                assert sandbox.inspect(saved(), workspace)['Id'] == previous
                verify(action.__name__ + ' startup rollback')

            original = saved()
            previous = sandbox.inspect(original, workspace)['Id']
            resume = browser_editor.resume_connection
            attempts = []

            def fail_editor(candidate, target, snapshot):
                attempts.append(True)
                if len(attempts) == 1:
                    raise ValueError('Injected replacement editor startup failure')
                return resume(candidate, target, snapshot)

            with patch.object(browser_editor, 'resume_connection', side_effect=fail_editor):
                try:
                    update()
                except ValueError as error:
                    assert 'Injected replacement editor startup failure' in str(error), str(error)
                else:
                    raise AssertionError('Editor failure was not reported')
            assert len(attempts) == 2
            assert saved() == original
            assert sandbox.inspect(saved(), workspace)['Id'] == previous
            verify('editor startup rollback')

            browser_editor.stop(state)
            sandbox.down(saved())
            for action in (update, rebuild):
                action()
                assert not sandbox.inspect(saved(), workspace)['State']['Running']
                assert browser_editor.runtime(state) is None
            print('PASS stopped update and rebuild keep the sandbox and editor stopped', flush=True)
        finally:
            browser_editor.stop(state)
            sandbox.delete(saved(), state)
            for resource in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
                remaining = sandbox.run(['podman', *resource, '--filter',
                    f'label={sandbox.LABEL}={name}'], capture=True).stdout
                assert not remaining.strip(), remaining
            print('PASS all disposable test resources removed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Cached Pi workspace image')
    parser.add_argument('--proxy-image', default='localhost/pi-sandbox-factory-proxy:1')
    parser.add_argument('--archive', required=True, type=Path, help='Pinned code-server archive')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='factory-editor-updates-') as folder:
        check(Path(folder), args.image, args.proxy_image, args.archive.resolve())
