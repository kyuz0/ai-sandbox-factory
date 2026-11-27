"""Real shell replay used by the disposable image/provider lifecycle fixture."""
import os
from pathlib import Path
import errno
import pty
import selectors
import signal
import time

from ai_sandbox_factory import browser_editor, cli_recipes, editor_settings, registry, sandbox
from browser_editor_lifecycle import request
from creation_provider import KEY


def replay(recipe, service, root, *, key=None):
    script = Path(root) / 'replay.sh'
    # The fixture uses a private package/index, just as an installed Factory uses its own package.
    script.write_text(recipe.render(launcher=service.command()[:-2]))
    assert KEY not in script.read_text(), 'Managed key leaked into exported commands'
    pid, descriptor = pty.fork()
    if pid == 0:
        os.execl('/bin/sh', 'sh', str(script))
    chunks, entered, status = [], False, None
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    # Failure guard; every credential entry waits for the actual masked prompt.
    deadline = time.monotonic() + 600
    try:
        while selector.get_map():
            for selected, _ in selector.select(1):
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b''  # A closed Linux PTY reports EIO instead of EOF.
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                chunks.append(chunk)
                text = b''.join(chunks).decode(errors='replace')
                assert KEY not in text, 'Masked input echoed the managed key'
                if key and not entered and 'fixture API key: ' in text:
                    os.write(descriptor, (key + '\n').encode())
                    entered = True
            if time.monotonic() > deadline:
                raise AssertionError('Recipe stopped progressing or exceeded its 10-minute failure guard: '
                                     + b''.join(chunks)[-6000:].decode(errors='replace'))
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, b''.join(chunks)[-10000:].decode(errors='replace')
        assert entered == bool(key), 'The recipe did not request its required credential'
    finally:
        selector.close()
        os.close(descriptor)
        if status is None:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


def check(service, source, root, generated_names):
    # Recover the selected prebuilt preference through the exported public update path.
    replay(cli_recipes.update(source['name'], {'image_source': 'prebuilt'}, service.state), service, root)
    source = service.config(source['name'])
    recipe = service.cli_recipe(source['name'])
    name = recipe.steps[0].argv[1]
    generated_names.append(name)
    # Registry drift must not replace the source's provider snapshot.
    provider = Path(root) / 'provider' / 'adapter.mjs'
    adapter = provider.read_text()
    try:
        provider.write_text("export function run(){throw Error('NEW_REGISTRY_VERSION_MUST_NOT_RUN')}\n")
        registry.update('provider', 'fixture')
        replay(recipe, service, root, key=KEY)
    finally:
        provider.write_text(adapter)
        registry.update('provider', 'fixture')
    config = service.config(name)
    assert config['profile']['digest'] == source['profile']['digest']
    assert config['model_providers'][0]['digest'] == source['model_providers'][0]['digest']
    assert config['profile']['path'] != source['profile']['path']
    assert editor_settings.load(service.state / name)['port'] == 0
    assert not editor_settings.load(service.state / name)['password_hash']
    container = sandbox.names(config)[0]
    assert sandbox.run(['podman', 'exec', container, 'cat',
        '/home/node/.pi/agent/fixture-provider-ready'], capture=True).stdout == 'available'
    assert KEY not in sandbox.run(['podman', 'exec', container, 'env'], capture=True).stdout
    browser_editor.open_browser_editor(config, service.state / name, background=True, open_tab=False)
    address = browser_editor.runtime(service.state / name)['url']
    assert request(address)[0] == 200
    identity = sandbox.inspect(config, container)['Id']
    rules = (service.state / name / 'allowlist.txt').read_text() + '\ndocs.python.org:443\n'
    replay(cli_recipes.network(name, rules, [], service.state), service, root)
    assert sandbox.inspect(config, container)['Id'] == identity
    assert request(address)[0] == 200
    replay(cli_recipes.update(name, {'memory': '9g'}, service.state), service, root)
    updated = service.config(name)
    assert updated['memory'] == '9g'
    assert sandbox.inspect(updated, container)['Id'] != identity
    assert browser_editor.runtime(service.state / name)['url'] == address
    assert request(address)[0] == 200
    return {'case': 'exported-cli-shell-provider-snapshots-live-network-and-editor-update', 'passed': True}
