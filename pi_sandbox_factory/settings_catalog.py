"""Names and navigation shared by sandbox creation and editing."""
from typing import NamedTuple


class Category(NamedTuple):
    label: str
    section: str
    focus: str | None = None


NETWORK_LABEL = 'Network'
NETWORK_MODE_LABEL = 'Network mode'
WEB_SEARCH_LABEL = 'Web Search'
LOCAL_MODELS_LABEL = 'Local models'
HOSTED_PROVIDERS_LABEL = 'Built-in hosted providers'
PROVIDER_PLUGINS_LABEL = 'Provider plugins'


# In display order. A focus target opens the shared settings form for a saved
# sandbox. Other categories have a dedicated page for live operations.
CATEGORIES = {
    'models': Category('Model Providers', 'hosted-section'),
    'editor-settings': Category('Editor', 'editor-section'),
    'folders': Category('Folders', 'folders-section'),
    'network-settings': Category(f'{NETWORK_LABEL} and {WEB_SEARCH_LABEL}', 'network-search-section', 'network-section'),
    'approval-settings': Category('Approvals & sandbox', 'agent-section', 'inner-sandbox'),
    'agent-tools': Category('Agent tools', 'skills-section', 'subagents'),
    'ssh-settings': Category('SSH & Git', 'git-identity-section'),
    'advisors': Category('Advisors', 'new-advisors-section'),
    'communications': Category('Communications', 'communications-section', 'slack-enabled'),
    'skills': Category('Skills', 'skill-library-section', 'manage-skills'),
    'resources': Category('Resources & environment', 'resources-section', 'memory'),
}

EDIT_FORMS = {action: category for action, category in CATEGORIES.items() if category.focus}
EDIT_FORMS.update({
    'model-sources': Category('Local models & provider plugins', CATEGORIES['models'].section, 'local-provider'),
})

NETWORK_PATH = f'Settings → {CATEGORIES["network-settings"].label} → {NETWORK_LABEL}'
MODEL_PROVIDERS_PATH = 'Settings → ' + CATEGORIES['models'].label
LOCAL_MODELS_PATH = MODEL_PROVIDERS_PATH + ' → ' + EDIT_FORMS['model-sources'].label
OPEN_LABELS = {'browser': 'Open in browser', 'pi': 'Open Pi CLI', 'desktop': 'Open VS Code'}
