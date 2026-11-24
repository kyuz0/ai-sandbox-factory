"""Check live APT installs in a disposable Factory sandbox with rootless Podman."""
import argparse
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from ai_sandbox_factory import sandbox
from ai_sandbox_factory.sandbox_tui import Service
from ai_sandbox_factory.updating import Update


def main(agent):
    with TemporaryDirectory(prefix='factory-live-apt-test-') as directory:
        root = Path(directory)
        workspace = root / 'project'
        workspace.mkdir()
        env = root / 'env'
        env.write_text('APT_CONFIG=/workspace/untrusted-apt.conf\n')
        (workspace / 'untrusted-apt.conf').write_text('This is not valid APT configuration.\n')
        service = Service(root / 'state')
        name = 'live-apt-' + uuid4().hex[:10]
        output, progress = [], []

        def log(line):
            output.append(line)
            print(line, flush=True)

        def run(*args):
            service.run(list(args), log)

        def install(package, expected_error=None):
            args = ['install-system-packages', name, package]
            output.clear()
            progress.clear()
            try:
                service.run_update(args, log, Update(progress=lambda *step: progress.append(step)))
            except ValueError as error:
                assert expected_error, str(error)
                assert 'Step 2 of 2' in str(error), str(error)
                assert any(expected_error in line for line in output), output[-20:]
                assert not any('Traceback' in line for line in output), output[-20:]
            else:
                assert not expected_error, f'Installation of {package} should have failed'
            assert progress[-1] == (2, 2, 'Install packages with APT'), progress

        run('create', name, '--engine', 'podman', '--workspace', str(workspace), '--no-browser',
            '--primary-agent', agent, '--primary-auth', 'login', '--env-file', str(env))
        terminal = None
        try:
            if agent != 'pi':
                run('build', name)
            service.start(name, log)
            config = service.config(name)
            container = sandbox.names(config)[0]
            old = sandbox.inspect(config, container)

            def check(*command):
                return subprocess.check_output(['podman', 'exec', container, *command], text=True).strip()

            terminal = subprocess.Popen(['podman', 'exec', '-i', container, 'bash', '-c',
                'printf "ready\\n"; read -r message; printf "%s\\n" "$message"'], text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            assert terminal.stdout.readline().strip() == 'ready'
            install('hello', 'allow its hostname and port')
            assert sandbox.inspect(config, container)['Id'] == old['Id']
            allow = root / 'allowlist.txt'
            allow.write_text('deb.debian.org:80\ndeb.debian.org:443\n')
            run('network', name, '--allowlist', str(allow))
            missing = 'factory-fixture-missing-package-0137'
            install(missing, f'Package {missing} is unavailable')
            install('hello')
            assert check('/usr/bin/hello') == 'Hello, world!'
            assert check('id', '-u') == '1000'
            status = check('cat', '/proc/self/status')
            assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
            current = sandbox.inspect(config, container)
            assert current['Id'] == old['Id'] and current['State']['StartedAt'] == old['State']['StartedAt']
            assert terminal.communicate('same session\n')[0].strip() == 'same session'
            runtime = '/home/node/.pi/agent/AGENTS.md' if agent == 'pi' else '/run/primary-runtime/context.md'
            assert 'Actions → Install system packages…' in check('cat', runtime)
            run('down', name)
            service.start(name, log)
            assert check('/usr/bin/hello') == 'Hello, world!'
            assert sandbox.inspect(config, container)['Id'] == old['Id']
            print(f'PASS {agent}: blocked repo, missing package, live install, preserved session and stop/start.')
        finally:
            if terminal and terminal.poll() is None:
                terminal.communicate('finish\n')
            run('delete', name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=('pi', 'codex', 'claude', 'agy'), default='pi')
    main(parser.parse_args().agent)
