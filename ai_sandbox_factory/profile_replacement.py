"""Replace a workspace while retaining its profile applications and recovery copy."""
from pathlib import Path
from contextlib import contextmanager
import shutil
import subprocess
from uuid import uuid4

from . import profile_services


def runtime_names(config):
    from . import aws_access, connectors, inference, primary_agents, primary_workspace, sandbox, sandbox_advisors, sandbox_ssh
    names = set(sandbox.names(config)[:2])
    names.update(sandbox_advisors.names(config)[:2])
    names.update((sandbox_ssh.names(config)[0], inference.names(config)[0], aws_access.worker_name(config)))
    names.update(connectors.worker_name(config, identifier) for identifier in config.get('connector_ids', []))
    if primary_workspace.agent(config) != 'pi':
        names.update(primary_agents.resources(config)['container'])
    return names


def failure_logs(config, state, names):
    """Preserve failed candidate diagnostics before rollback removes containers."""
    import json
    from . import model_providers, sandbox
    file = Path(state) / 'profile-services/values.json'
    values = list(json.loads(file.read_text()).values()) if file.exists() else []
    for name in sorted(names):
        try:
            info = sandbox.inspect(config,name)
            if not info:
                continue
            result = sandbox.run([config['engine'], 'logs', name], capture=True,
                                 check=False, stderr=subprocess.PIPE)
            text = result.stdout + (result.stderr or '')
            status = info.get('State', {})
            if status.get('Error') or status.get('ExitCode'):
                text = f'Container exit {status.get("ExitCode", "unknown")}: {status.get("Error", "")}\n' + text
            for value in values:
                text = text.replace(value, '[redacted]')
            text = model_providers.diagnostic(text)
            if text:
                print(f'Container diagnostics for {name}:\n{text}',flush=True)
        except Exception as error:
            print(f'Could not collect container diagnostics for {name}: {error}',flush=True)


def recover_preparation(config, state):
    """Finish an interrupted profile transaction under the sandbox lifecycle lock."""
    import json
    from . import activity, sandbox
    state = Path(state)
    journal = state / 'profile-preparation'
    record = journal / 'record.json'
    if not journal.exists():
        return
    if not record.exists():
        # No resource allocation starts until the record has been published.
        shutil.rmtree(journal)
        return
    saved = json.loads(record.read_text())
    original, updated = saved['original'], saved['updated']
    failures = []
    def restore(label, action):
        try:
            action()
        except Exception as error:
            failures.append(f'{label}: {error}')
    engine = original['engine']
    if not saved.get('committed'):
        for name, previous in saved['runtime'].items():
            def container_back(name=name, previous=previous):
                current = sandbox.inspect(updated, name)
                if current and current['Id'] != (previous or {}).get('Id'):
                    sandbox.run([engine, 'rm', '-f', current['Id']])
                if previous:
                    held = sandbox.inspect(original, previous['Id'])
                    if held is None:
                        raise ValueError(f'Previous container {previous["Id"]} is missing; its private volumes were not removed')
                    # Inspect by ID works even after the old name was retained.
                    if held.get('Name', '').lstrip('/') != name:
                        sandbox.run([engine, 'rename', previous['Id'], name])
            restore('previous container ' + name, container_back)
        for kind in ('container', 'volume', 'network'):
            for name in saved['added'][kind]:
                def remove(kind=kind, name=name):
                    found = sandbox.run([engine,kind,'exists',name],capture=True,check=False,stderr=subprocess.PIPE)
                    if found.returncode == 1:
                        return
                    if found.returncode:
                        raise ValueError(f'exit {found.returncode}: {(found.stderr or found.stdout).strip()}')
                    profile_services.check_ownership(updated)
                    args = ['rm','-f',name] if kind == 'container' else [kind,'rm',name]
                    sandbox.run([engine,*args],capture=True,stderr=subprocess.PIPE)
                restore(kind + ' ' + name, remove)
        directory, backup = state / 'profile-services', journal / 'services'
        def restore_files():
            if backup.exists():
                if directory.exists():
                    for path in directory.rglob('*'):
                        if path.is_file() and not (backup / path.relative_to(directory)).exists():
                            path.unlink()
                shutil.copytree(backup,directory,dirs_exist_ok=True)
            elif directory.exists():
                shutil.rmtree(directory)
            for name in ('allowlist.txt','squid.conf','profile-runtime/applied.json'):
                prior = journal / 'files' / name
                target = state / name
                if prior.exists():
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(prior,target)
                else:
                    target.unlink(missing_ok=True)
            activity.save_config(state,original)
        restore('previous settings',restore_files)
        workspace = saved['runtime'].get(sandbox.names(original)[0])
        if workspace and workspace['State']['Running']:
            restore('previous sandbox startup',lambda: sandbox.up(original,state,prepare_sign_in=False))
        else:
            for name, previous in saved['runtime'].items():
                if previous and previous['State']['Running']:
                    restore('running state '+name,lambda previous=previous:sandbox.run([engine,'start',previous['Id']]))
        for name, previous in {**saved['runtime'], **saved['services']}.items():
            if previous and not previous['State']['Running']:
                restore('stopped state '+name,lambda previous=previous:sandbox.run([engine,'stop',previous['Id']]))
        if not failures:
            print('Profile preparation was rolled back; new service resources were removed and the previous runtime and settings were restored.',flush=True)
    else:
        for name, previous in saved['runtime'].items():
            if not previous:
                continue
            def retire(name=name, previous=previous):
                current = sandbox.inspect(updated,name)
                if current and current['Id'] == previous['Id']:
                    return
                if sandbox.inspect(original,previous['Id']):
                    sandbox.run([engine,'rm',previous['Id']],capture=True,stderr=subprocess.PIPE)
            restore('previous stopped container '+name,retire)
    if failures:
        raise ValueError('Profile recovery needs attention: ' + '; '.join(failures)
                         + '. Saved recovery information and private volumes were retained; retry the profile action after fixing the reported engine error.')
    shutil.rmtree(journal)


@contextmanager
def prepared_services(original, updated, state):
    """Journal allocations before preparation; retain old containers until commit."""
    from . import sandbox
    state = Path(state)
    recover_preparation(original,state)
    before, after = profile_services.resources(original), profile_services.resources(updated)
    added = {kind:sorted(after[kind]-before[kind]) for kind in after}
    for kind,names in added.items():
        for name in names:
            found = sandbox.run([updated['engine'],kind,'exists',name],capture=True,check=False,stderr=subprocess.PIPE)
            if found.returncode == 0:
                raise ValueError(f'Cannot add profile services: {kind} {name!r} already exists. Inspect this exact resource before retrying; nothing was replaced or deleted.')
            if found.returncode != 1:
                raise ValueError(f'Cannot check {kind} {name!r} (exit {found.returncode}): {(found.stderr or found.stdout).strip()}. Check the container engine and retry.')
    runtime = {name:sandbox.inspect(original,name) for name in runtime_names(original)|runtime_names(updated)}
    services = {name:sandbox.inspect(original,name) for name in before['container']}
    journal = state/'profile-preparation'
    journal.mkdir(mode=0o700)
    if (state/'profile-services').exists():
        shutil.copytree(state/'profile-services',journal/'services')
    for name in ('allowlist.txt','squid.conf','profile-runtime/applied.json'):
        source = state/name
        if source.exists():
            target=journal/'files'/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
    saved={'original':original,'updated':updated,'runtime':runtime,'services':services,'added':added}
    sandbox.write_json(journal/'record.json',saved)
    try:
        yield
        stop={name for name,info in services.items() if info and not info['State']['Running']}
        workspace=runtime.get(sandbox.names(original)[0])
        if workspace and not workspace['State']['Running']:
            stop.update(after['container'])
        for name in sorted(stop):
            if sandbox.inspect(updated,name):
                sandbox.run([updated['engine'],'stop',name])
    except BaseException as error:
        failure_logs(updated,state,added['container'])
        try:
            recover_preparation(original,state)
        except Exception as recovery:
            raise ValueError(f'Profile change failed: {error}. {recovery}') from error
        raise
    else:
        # From this point recovery keeps the new runtime and only retires backups.
        saved['committed']=True
        sandbox.write_json(journal/'record.json',saved)
        recover_preparation(updated,state)


def apply(original, updated, state, *, text=None, allow_additions=False, retain_previous=False):
    from . import activity, sandbox
    state = Path(state)
    profile_services.validate_update(original, updated, allow_additions=allow_additions)
    engine = original['engine']
    names = runtime_names(original) | runtime_names(updated)
    workspace = sandbox.names(original)[0]
    prior = {name: sandbox.inspect(original, name) for name in sorted(names)}
    services = {name: sandbox.inspect(original, name)
                for name in profile_services.resources(original)['container']}
    old_text = (state / 'allowlist.txt').read_text()
    old_policy = (state / 'squid.conf').read_text()
    pointer = state / 'profile-runtime/applied.json'
    old_pointer = pointer.read_bytes() if pointer.exists() else None
    moved = {}
    try:
        for name, info in prior.items():
            if not info:
                continue
            if info['State']['Running']:
                sandbox.run([engine, 'stop', info['Id']])
            sandbox.run([engine, 'rename', info['Id'], name + '-previous-' + uuid4().hex])
            moved[name] = info['Id']
        if text is not None:
            (state / 'allowlist.txt').write_text(text)
        (state / 'squid.conf').write_text(sandbox.network_policy(
            updated, sandbox.parse_rules((state / 'allowlist.txt').read_text())))
        activity.save_config(state, updated)
        profile_services.update_policy(updated, state, (state / 'allowlist.txt').read_text(), updated.get('website_groups', {}))
        sandbox.up(updated, state, prepare_sign_in=False)
        # Validation may start a stopped sandbox, but never changes its saved state.
        if prior[workspace] and not prior[workspace]['State']['Running']:
            for name in names:
                if sandbox.inspect(updated, name):
                    sandbox.run([engine, 'stop', name])
        for name, info in services.items():
            if info and not info['State']['Running']:
                sandbox.run([engine, 'stop', name])
    except BaseException as error:
        failure_logs(updated,state,{name for name in names if name in moved})
        if retain_previous:
            # The outer journal owns recovery and keeps exact prior IDs/files.
            raise
        failures = []
        def restore(label, action):
            try:
                action()
            except Exception as failure:
                failures.append(f'{label}: {failure}')
        for name in names:
            def container_back(name=name):
                current = sandbox.inspect(updated, name)
                if current and current['Id'] != (prior[name] or {}).get('Id'):
                    sandbox.run([engine, 'rm', '-f', current['Id']])
                if name in moved:
                    sandbox.run([engine, 'rename', moved[name], name])
            restore(f'container {name}', container_back)
        restore('network allowlist', lambda: (state / 'allowlist.txt').write_text(old_text))
        restore('network policy', lambda: (state / 'squid.conf').write_text(old_policy))
        restore('saved configuration', lambda: activity.save_config(state, original))
        restore('applied profile record', lambda: pointer.unlink(missing_ok=True) if old_pointer is None else pointer.write_bytes(old_pointer))
        restore('profile gateway policy', lambda: profile_services.update_policy(original, state, old_text, original.get('website_groups', {})))
        if prior[workspace] and prior[workspace]['State']['Running']:
            restore('previous sandbox startup', lambda: sandbox.up(original, state, prepare_sign_in=False))
        else:
            from . import connector_settings
            restore('previous agent instructions', lambda: connector_settings.refresh_agent_context(original, state))
            for name, info in prior.items():
                if info and info['State']['Running']:
                    restore(f'start {name}', lambda info=info: sandbox.run([engine, 'start', info['Id']]))
        for name, info in {**prior, **services}.items():
            if info and not info['State']['Running']:
                restore(f'stopped state {name}', lambda info=info: sandbox.run([engine, 'stop', info['Id']]))
        if failures:
            raise ValueError(f'Profile replacement failed: {error}. Some recovery steps need attention: '
                             + '; '.join(failures) + '. Project files and private volumes were not deleted.') from error
        print('The rebuild failed. The previous coding-agent container, Factory helper containers and settings were restored; profile containers and their data were kept.', flush=True)
        raise
    if not retain_previous:
        for name, container_id in moved.items():
            sandbox.run([engine, 'rm', container_id])
    if text is not None and text != old_text:
        activity.record(state, 'edited')
