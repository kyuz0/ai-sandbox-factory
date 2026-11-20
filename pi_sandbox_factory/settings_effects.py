"""User-facing effects of the settings update paths, without secret values."""
from .sandbox_options import options
from .settings_catalog import CATEGORIES, NETWORK_LABEL, OPEN_LABELS, WEB_SEARCH_LABEL

def bullets(items):
    return '\n'.join('• ' + item for item in items)


def open_client(mode):
    return f'Choose {OPEN_LABELS[mode]} in Factory.'


def reconnect(mode='browser', *, opened=False):
    action = {
        'browser': open_client('browser'),
        'desktop': 'Reload the VS Code window.',
        'pi': 'Restart Pi CLI.',
    }[mode]
    return ['Resume your saved conversation in the reopened client.'] if opened else [action, 'Resume your saved conversation.']


REOPEN = bullets(reconnect())
RECREATE = 'Recreates the sandbox and stops active tasks, terminals and browser processes.'
NETWORK = bullets(['Rules apply to new connections.', 'Retry any interrupted connections. No editor reload is needed.'])
MODELS = bullets(['Reopen the client to refresh model connections.', 'Select a model in the chat.'])
FOLDER = 'The new starting folder applies when you next open the client.'
APPROVALS_LABEL = CATEGORIES['approval-settings'].label


def editor_migration(config):
    from .sandbox import pendant_versions
    return ['The editor switches from Pendant to FrostPi.'] if pendant_versions(config) else []

# Keep every options key explicit so new settings require an effect assessment.
OPTION_GROUPS = {
    'approval': APPROVALS_LABEL, 'inner_sandbox': APPROVALS_LABEL,
    'protect_credentials': APPROVALS_LABEL,
    'protect_browser_credentials': 'Browser and saved vault protection',
    'browser': 'Browser and saved vault protection',
    'goals': 'Agent tools and conversation recall', 'scheduling': 'Agent tools and conversation recall',
    'question_forms': 'Agent tools and conversation recall', 'compaction_recall': 'Agent tools and conversation recall',
    'knowledge_embedding': 'Local knowledge',
    'disabled_skills': 'Skills', 'web_access': WEB_SEARCH_LABEL, 'website_groups': NETWORK_LABEL,
    'communications': 'Slack tools', 'git_identity': 'Git commit identity',
}


def configuration_changes(original, draft):
    """Describe changes to sandbox settings without credential values."""
    before, after = options(original.get('options')), options(draft.get('options'))
    groups = {title for key, title in OPTION_GROUPS.items() if before[key] != after[key]}
    for key, title in {'network': NETWORK_LABEL, 'memory': 'Memory limit',
                       'subagents': 'Agent tools and conversation recall',
                       'local_model_ports': 'Local model access', 'env_file': 'Environment file'}.items():
        if (original.get(key) or None) != (draft.get(key) or None):
            groups.add(title)
    from .sandbox import parse_rules
    if set(parse_rules(original.get('allowlist', ''))) != set(parse_rules(draft.get('allowlist', ''))):
        groups.add(NETWORK_LABEL)
    providers = original.get('model_providers', {'local': {}})
    if isinstance(providers, list):
        providers = {row['name']: row.get('settings', {}) for row in providers}
    if providers != draft.get('model_providers', {'local': {}}):
        groups.add('Model provider plugins')
    if draft.get('library_skills') or draft.get('remove_skills') or draft.get('skill_repo'):
        groups.add('Skills')
    if draft.get('_provider_keys'):
        groups.add('Provider credentials')
    if draft.get('_communication_keys'):
        groups.add('Slack credentials')
    if draft.get('_search_keys_changed'):
        groups.add('Search credentials')
    return sorted(groups)


def configuration_notes(original, draft, mode='browser'):
    groups = set(configuration_changes(original, draft))
    notes = [RECREATE, *editor_migration(original), 'Workspace files and saved chats are kept.', *reconnect(mode)]
    if groups & {'Skills', 'Agent tools and conversation recall', APPROVALS_LABEL}:
        notes.append('Earlier chat messages stay unchanged; a new chat gives a clean context.')
    before, after = options(original.get('options')), options(draft.get('options'))
    if 'Browser and saved vault protection' in groups and after['browser']:
        notes.append('Reopen the browser viewer; websites may require login.')
    if 'Git commit identity' in groups:
        notes.append('Identity applies to future commits; repository-local Git settings take precedence.')
    previous = original.get('model_providers', {'local': {}})
    if isinstance(previous, list):
        previous = {row['name']: row.get('settings', {}) for row in previous}
    if set(previous) - set(draft.get('model_providers', {'local': {}})) or (original.get('local_model_ports') and not draft.get('local_model_ports')):
        notes.append('Select another model in chats using a removed connection.')
    return notes
