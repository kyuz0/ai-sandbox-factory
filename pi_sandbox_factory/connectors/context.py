"""Render configured access for the agent from saved grants and public plugin metadata."""
from dataclasses import dataclass
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from . import ADAPTERS, client_socket, permitted_routes, saved


@dataclass
class Hint:
    title: str
    guidance: str
    entries: list
    instructions: str = ''
    reference: str = ''


# A compact prompt gives examples. The workspace reference includes every grant.
PREVIEW_ENTRIES = 3
PREVIEW_CHARACTERS = 480


def _external(config, plugin):
    socket = client_socket(config, plugin.id)
    entries = []
    notes = dict(plugin.operation_instructions)
    for name, url in permitted_routes(plugin, config):
        host = urlsplit(url).netloc
        entry = f'GET {name} from {host} with curl --fail-with-body --unix-socket {socket} http://localhost/{name}'
        if name in notes:
            entry += '\n  ' + notes[name]
        entries.append(entry)
    return Hint(plugin.title, 'The connector supplies its bearer token in a separate container. '
                'Use only the enabled requests below.', entries, plugin.agent_instructions)


def _preview(hint):
    selected = []
    for entry in hint.entries[:PREVIEW_ENTRIES]:
        if sum(map(len, selected)) + len(entry) > PREVIEW_CHARACTERS:
            break
        selected.append(entry)
    lines = [hint.guidance, *('- ' + entry for entry in selected)]
    remaining = len(hint.entries) - len(selected)
    if remaining:
        lines.append(f'The full connector reference lists {remaining} more.')
    if hint.instructions:
        if len(hint.instructions) <= PREVIEW_CHARACTERS:
            lines.append(hint.instructions)
        else:
            lines.append('Read the plugin instructions in the full connector reference before using this service.')
    return lines


def render(config, state):
    hints = [hint for adapter in ADAPTERS.values() if (hint := adapter.hint(config))]
    hints.extend(_external(config, plugin) for plugin in saved(config, state))
    if not hints:
        return '', ''
    pi = config.get('primary_agent', 'pi') == 'pi'
    path = '/opt/pi-security/config/connectors.md' if pi else '/run/primary-runtime/connectors.md'
    introduction = ['# Configured connectors', '', f'Read {path} in the workspace for the full connector reference.']
    options = config.get('options', {})
    if pi and options.get('inner_sandbox', True) and (config.get('ssh', {}).get('hosts') or config.get('connector_ids')):
        if options.get('approval', 'automatic') == 'off':
            introduction.append('The inner shell blocks SSH and HTTPS connector sockets. To use a socket, ask the user to enable approvals.')
        else:
            commands = []
            if config.get('ssh', {}).get('hosts'):
                commands.extend(['ssh', 'SSH-backed git', 'sandbox-ssh-status'])
            if config.get('connector_ids'):
                commands.append('curl --unix-socket')
            introduction.append('Use bash_escalate with the exact command and justification for ' +
                                ', '.join(commands) + '.')
    summary = introduction.copy()
    details = ['# Configured connectors', *introduction[3:]]
    for hint in hints:
        summary.extend(('', '## ' + hint.title, '', *_preview(hint)))
        details.extend(('', '## ' + hint.title, '', hint.guidance, '', *('- ' + entry for entry in hint.entries)))
        if hint.reference:
            details.extend(('', hint.reference))
        if hint.instructions:
            details.extend(('', hint.instructions))
    return '\n'.join(summary) + '\n', '\n'.join(details) + '\n'


def write_text(path, text):
    if not text:
        path.unlink(missing_ok=True)
        return
    if path.is_file() and path.read_bytes() == text.encode('utf-8'):
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, mode='w',
                            encoding='utf-8', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write(config, state):
    summary, details = render(config, state)
    root = Path(state) / ('runtime-config' if config.get('primary_agent', 'pi') == 'pi' else 'primary-runtime')
    write_text(root / 'connectors.md', details)
    write_text(root / 'connectors-prompt.md', summary)
