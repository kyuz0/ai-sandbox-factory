"""Configure SSH destinations, key setup and the Git author for a sandbox."""

saved_action = 'ssh-settings'


def status(config):
    count = len(config.get('ssh', {}).get('hosts', []))
    parts = [f'{count} destination' if count == 1 else f'{count} destinations'] if count else []
    if config.get('options', {}).get('git_identity'):
        if not count:
            parts.append('No destinations')
        parts.append('Git commit identity set')
    return ' · '.join(parts) or 'Off'


def draft_editor(draft, connector):
    from ....repository_ui import CreateSSHDialog
    return CreateSSHDialog(draft, connector.title)


def apply_draft(draft, value):
    settings, identity = value
    for key in ('_ssh_key_file', '_ssh_key_duration'):
        draft.pop(key, None)
    draft.update(settings)
    draft['options']['git_identity'] = identity


def hint(config):
    from ...context import Hint
    hosts = config.get('ssh', {}).get('hosts', [])
    if not hosts:
        return None
    destinations = []
    for host in hosts:
        destinations.append(f'{host["alias"]} connects to {host["hostname"]} as {host["user"]} on port {host["port"]}.')
    status_command = 'sandbox-ssh-status' if config.get('primary_agent', 'pi') == 'pi' else 'ssh-add -l'
    return Hint(
        'SSH & Git',
        f'Use ssh with a configured alias. The SSH companion holds the private key. Run {status_command} '
        'to check whether it is unlocked. The remote server controls shell, repository and branch permissions.',
        destinations,
        reference='Use the alias in Git remotes, for example USER@ALIAS:group/project.git. '
        'Keep the configured proxy, signing agent and pinned host keys. A direct DNS or TCP probe does not test '
        'this connection. If the key is locked or missing, ask the user to open Settings → Connectors → SSH & Git '
        '→ SSH key. Never request a private key or passphrase, or generate a replacement inside the sandbox. '
        'Keep the Git exit status when reporting a failed operation.',
    )
