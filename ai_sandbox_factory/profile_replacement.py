"""Transactional workspace and profile service replacement with recovery."""
import copy
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
    from . import activity, browser_editor, sandbox
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
    restore_target = saved.get('restore_target')
    browser = saved.get('browser_editor')
    if not saved.get('committed'):
        previous_workspace = saved['runtime'].get(sandbox.names(original)[0])
        if browser:
            editor = browser_editor.runtime(state)
            if editor and editor['container'] != (previous_workspace or {}).get('Id'):
                restore('replacement browser editor shutdown',lambda: browser_editor.stop(state))
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
            if saved.get('restore_target'):
                activity.save_config(state, original)
                return
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
                    def resume(previous=previous):
                        sandbox.run([engine, 'start', previous['Id']])
                        if restore_target:
                            profile_services.ensure_ssh(original, restore_target, previous['Id'])
                    restore('running state ' + name, resume)
        for name, previous in {**saved['runtime'], **saved['services']}.items():
            if previous and not previous['State']['Running']:
                restore('stopped state '+name,lambda previous=previous:sandbox.run([engine,'stop',previous['Id']]))
        if browser:
            restore('previous browser editor',lambda: browser_editor.resume_connection(original,state,browser))
        if not failures:
            if restore_target:
                print(f'Restored the previous container, data and running state of service {restore_target!r}.', flush=True)
            else:
                print('Profile preparation was rolled back; new service resources were removed and the previous runtime and settings were restored.', flush=True)
    else:
        if browser:
            restore('browser editor connection',lambda: browser_editor.resume_connection(updated,state,browser))
            if failures:
                raise ValueError('Could not reconnect the browser editor: ' + '; '.join(failures)
                                 + '. The committed sandbox and recovery information were kept; retry Start.')
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
        before, after = profile_services.resources(original), profile_services.resources(updated)
        for kind in ('volume', 'network'):
            for name in sorted(before[kind] - after[kind]):
                def retire_resource(kind=kind, name=name):
                    found = sandbox.run([engine, kind, 'exists', name], capture=True, check=False, stderr=subprocess.PIPE)
                    if found.returncode == 1:
                        return
                    if found.returncode:
                        raise ValueError(f'exit {found.returncode}: {(found.stderr or found.stdout).strip()}')
                    profile_services.check_ownership(original)
                    sandbox.run([engine, kind, 'rm', name], capture=True, stderr=subprocess.PIPE)
                restore('unused ' + kind + ' ' + name, retire_resource)
    if failures:
        action = f'Restore data for {restore_target!r}' if restore_target else 'the profile action'
        heading = f'Service data restore for {restore_target!r} needs attention: ' if restore_target else 'Profile recovery needs attention: '
        raise ValueError(heading + '; '.join(failures)
                         + f'. Saved recovery information and private volumes were retained; retry {action} after fixing the reported engine error.')
    shutil.rmtree(journal)


@contextmanager
def prepared_services(original, updated, state, *, cancelled=None, defer_preparation=False, restore_target=None, restore_sources=None):
    """Journal allocations before preparation; retain old containers until commit."""
    from . import browser_editor, sandbox, service_data
    state = Path(state)
    recover_preparation(original,state)
    profile_services.check_ownership(original)
    if restore_target is None:
        changes = profile_services.changes(original, updated)
        affected = changes['replaced_services'] + changes['removed_services']
    else:
        affected = [restore_target]
    action = 'Restore data' if restore_target else 'Apply'
    # A replacement gets a copy of its previous data. Its entrypoint or migration
    # must never write into the recovery container's only data volume.
    mappings = copy.deepcopy(original.get('profile_service_volumes', {}))
    retained = set(original.get('profile_service_retained_volumes', []))
    copies = []
    for name in affected:
        previous = profile_services.selected(original)[name]
        current = profile_services.selected(updated).get(name)
        saved_volumes = {}
        mappings.pop(name, None)
        for volume in previous['volumes']:
            prior = profile_services.data_volume(original, name, volume)
            if not current or volume not in current['volumes']:
                found = sandbox.run([original['engine'], 'volume', 'exists', prior], capture=True,
                                    check=False, stderr=subprocess.PIPE)
                if found.returncode == 1:
                    print(f'Service volume {prior!r} does not exist. No data was saved for {name}/{volume}.', flush=True)
                    continue
                if found.returncode:
                    raise ValueError(f'Cannot check service volume {prior!r} (exit {found.returncode}): '
                                     f'{(found.stderr or found.stdout).strip()}. Check Podman before retrying Apply.')
            saved_volumes[volume] = {'volume': prior, 'target': previous['volumes'][volume]}
            retained.add(prior)
            if current and volume in current['volumes']:
                candidate = profile_services.data_volume_base(updated, name, volume) + '-revision-' + uuid4().hex[:12]
                mappings.setdefault(name, {})[volume] = candidate
                copies.append(((restore_sources or {}).get(volume, prior), candidate))
        if saved_volumes:
            reason = 'restored' if restore_target else ('replaced' if current else 'removed')
            service_data.record(updated, original, name, reason, saved_volumes)
    for name, spec in profile_services.selected(updated).items():
        for volume in spec['volumes']:
            if (volume not in mappings.get(name, {})
                    and profile_services.data_volume(updated, name, volume) in retained):
                mappings.setdefault(name, {})[volume] = profile_services.data_volume_base(updated, name, volume) + '-revision-' + uuid4().hex[:12]
    if mappings:
        updated['profile_service_volumes'] = mappings
    else:
        updated.pop('profile_service_volumes', None)
    if retained:
        updated['profile_service_retained_volumes'] = sorted(retained)
    before, after = profile_services.resources(original), profile_services.resources(updated)
    added = {kind:sorted(after[kind]-before[kind]) for kind in after}
    for kind,names in added.items():
        for name in names:
            found = sandbox.run([updated['engine'],kind,'exists',name],capture=True,check=False,stderr=subprocess.PIPE)
            if found.returncode == 0:
                operation = f'restore data for {restore_target!r}' if restore_target else 'add profile services'
                raise ValueError(f'Cannot {operation}: {kind} {name!r} already exists. Inspect this exact resource before retrying; nothing was replaced or deleted.')
            if found.returncode != 1:
                raise ValueError(f'Cannot check {kind} {name!r} (exit {found.returncode}): {(found.stderr or found.stdout).strip()}. Check the container engine and retry.')
    replaced = {profile_services.container(original, name) for name in affected}
    replaced.update(before['container'] - after['container'])
    runtime_scope = replaced if restore_target else runtime_names(original) | runtime_names(updated) | replaced
    service_scope = replaced if restore_target else before['container']
    runtime = {name:sandbox.inspect(original,name) for name in runtime_scope}
    services = {name:sandbox.inspect(original,name) for name in service_scope}
    browser = None if restore_target else browser_editor.replacement_connection(original,state)
    journal = state/'profile-preparation'
    journal.mkdir(mode=0o700)
    if not restore_target:
        if (state/'profile-services').exists():
            shutil.copytree(state/'profile-services',journal/'services')
        for name in ('allowlist.txt','squid.conf','profile-runtime/applied.json'):
            source = state/name
            if source.exists():
                target=journal/'files'/name
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,target)
    helpers = [profile_services.prefix(updated) + '-copy-' + uuid4().hex for _ in copies]
    added['container'] += helpers
    saved={'original':original,'updated':updated,'runtime':runtime,'services':services,'added':added,
           'restore_target':restore_target,'browser_editor':browser}
    sandbox.write_json(journal/'record.json',saved)
    def prepare():
        if cancelled and cancelled():
            from .updating import UpdateCancelled
            raise UpdateCancelled()
        for name in sorted(replaced):
            previous = runtime[name]
            if not previous:
                continue
            if previous['State']['Running']:
                sandbox.run([original['engine'], 'stop', previous['Id']])
            sandbox.run([original['engine'], 'rename', previous['Id'], name + '-previous-' + uuid4().hex])
        for (previous, candidate), helper in zip(copies, helpers):
            sandbox.run([updated['engine'], 'volume', 'create', *profile_services._labels(updated, 'data'), candidate])
            found = sandbox.run([original['engine'], 'volume', 'exists', previous], capture=True, check=False, stderr=subprocess.PIPE)
            if found.returncode == 1:
                advice = ('Choose another saved copy with the required data.' if restore_target else
                          'Restore it from a backup, or remove and re-add the service if you want fresh data.')
                raise ValueError(f'Cannot copy service data volume {previous!r}: the volume is missing. {advice}')
            if found.returncode:
                raise ValueError(f'Cannot copy service data volume {previous!r} (exit {found.returncode}): '
                                 f'{(found.stderr or found.stdout).strip()}')
            # Check both ends of the tar pipeline. A partial copy is a failure.
            script = ('import subprocess,sys; '
                      'source=subprocess.Popen(["tar","-C","/source","-cpf","-","."],stdout=subprocess.PIPE); '
                      'target=subprocess.run(["tar","-C","/target","-xpf","-"],stdin=source.stdout); '
                      'source.stdout.close(); result=source.wait(); sys.exit(result or target.returncode)')
            print(f'Copying service data volume {previous} to {candidate}.', flush=True)
            try:
                with sandbox.cancellable_build(cancelled):
                    sandbox.run([updated['engine'], 'run', '--rm', '--name', helper,
                                 *profile_services._labels(updated, helper), '--network=none', '--user', '0',
                                 '--security-opt=label=disable', '-v', previous + ':/source:ro', '-v', candidate + ':/target',
                                 '--entrypoint', 'python3', sandbox.image_names(updated)[1], '-c', script],
                                capture=True, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or error.stdout or '').strip()
                raise ValueError(f'Cannot copy service data volume {previous!r} to {candidate!r} '
                                 f'(exit {error.returncode}): {detail}. Previous data was kept; check storage and retry {action}.') from error
    try:
        if not defer_preparation:
            prepare()
        yield prepare
        stop={name for name,info in services.items() if info and not info['State']['Running']}
        workspace=runtime.get(sandbox.names(original)[0])
        if workspace and not workspace['State']['Running']:
            stop.update(after['container'])
        for name in sorted(stop):
            if sandbox.inspect(updated,name):
                sandbox.run([updated['engine'],'stop',name])
    except BaseException as error:
        failure_logs(updated,state,set(added['container']) | replaced)
        try:
            recover_preparation(original,state)
        except Exception as recovery:
            raise ValueError(f'{action} failed: {error}. {recovery}') from error
        if restore_target and isinstance(error, (ValueError, OSError, subprocess.CalledProcessError)):
            raise ValueError(f'{error} The previous container and data for {restore_target!r} were restored.') from error
        raise
    else:
        # Publish the decision before retiring any previous containers.
        saved['committed']=True
        operation = f'service data restore for {restore_target!r}' if restore_target else 'profile changes'
        retry = 'retry Restore data' if restore_target else 'retry the same profile review'
        try:
            sandbox.write_json(journal/'record.json',saved)
        except BaseException as error:
            # Until the commit marker is published, the saved journal still
            # owns rollback. Do not leave a failed Apply running its candidate.
            try:
                recover_preparation(original,state)
            except Exception as recovery:
                raise ValueError(f'Could not commit {operation}: {error}. {recovery}') from error
            if isinstance(error, Exception):
                import errno
                advice = ('Free storage space' if getattr(error, 'errno', None) == errno.ENOSPC
                          else f'Check the reported error and permissions on {journal}')
                raise ValueError(f'Could not commit {operation} for sandbox {original["name"]!r} '
                                 f'to {journal / "record.json"}: {error}. '
                                 f'The previous runtime and application data were restored. {advice}, '
                                 f'then {retry}.') from error
            raise
        recover_preparation(updated,state)


def apply(original, updated, state, *, text=None, allow_service_changes=False, retain_previous=False):
    import json
    from . import activity, browser_editor, sandbox
    state = Path(state)
    profile_services.validate_update(original, updated, allow_service_changes=allow_service_changes)
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
    journal = state/'profile-preparation/record.json'
    browser = (json.loads(journal.read_text()).get('browser_editor') if retain_previous and journal.is_file()
               else browser_editor.replacement_connection(original,state))
    moved = {}
    try:
        if browser:
            browser_editor.stop(state)
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
        if browser:
            browser_editor.resume_connection(updated,state,browser)
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
        if browser:
            editor = browser_editor.runtime(state)
            if editor and editor['container'] != (prior[workspace] or {}).get('Id'):
                restore('replacement browser editor shutdown', lambda: browser_editor.stop(state))
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
        if browser:
            restore('previous browser editor',lambda: browser_editor.resume_connection(original,state,browser))
        if failures:
            raise ValueError(f'Profile replacement failed: {error}. Some recovery steps need attention: '
                             + '; '.join(failures) + '. Project files and private volumes were not deleted.') from error
        print('The rebuild failed. The previous main agent sandbox, Factory helper containers and settings were restored; profile containers and their data were kept.', flush=True)
        raise
    if not retain_previous:
        for name, container_id in moved.items():
            sandbox.run([engine, 'rm', container_id])
    if text is not None and text != old_text:
        activity.record(state, 'edited')
