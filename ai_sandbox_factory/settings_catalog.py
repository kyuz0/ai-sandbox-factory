"""Names and navigation shared by sandbox creation and editing."""
from typing import NamedTuple

class Category(NamedTuple):
    label: str
    section: str
    focus: str | None = None


NETWORK_LABEL = 'Network'
NETWORK_MODE_LABEL = 'Network mode'
NETWORK_MODES = (('Restricted — only allowed destinations', 'allowlist'),
                 ('Unrestricted — ordinary container network', 'unrestricted'))
WEB_SEARCH_LABEL = 'Web Search'
LOCAL_MODELS_LABEL = 'Local models'
HOSTED_PROVIDERS_LABEL = 'Built-in hosted providers'
PROVIDER_PLUGINS_LABEL = 'Provider plugins'
ADVISORS_LABEL = 'Advisors (Codex/Claude/Antigravity)'
SYSTEM_PROMPT_LABEL = 'Custom system prompt add-on'
SYSTEM_PROMPT_LIBRARY_LABEL = 'System prompt add-ons'
NO_SYSTEM_PROMPT_ADD_ON = 'No add-on'
PI_FEATURES_LABEL = 'Pi features'
INSTALL_SYSTEM_PACKAGES = 'Install system packages…'

CONTROL_LABELS = {
    'approval': 'Approval mode',
    'inner_sandbox': 'Use inner command sandbox',
    'protect_credentials': 'Protect common credential files',
    'subagents': 'Enable optional subagents',
    'compaction_recall': 'Conversation recall',
    'browser': 'Agent browser · Betterwright',
    'protect_browser_credentials': 'Protect saved browser vault files',
    'question_forms': 'Question forms',
    'goals': 'Goals',
    'scheduling': 'Scheduled prompts',
    'native_browser': 'Enable the browser',
    'native_web_search': 'Enable web search',
    'native_approval': 'Tool approvals',
}


def network_mode_label(mode):
    return dict((value, label) for label, value in NETWORK_MODES)[mode].split(" — ")[0]


# In display order. A focus target opens the shared settings form for a saved
# sandbox. Other categories have a dedicated page for live operations.
CATEGORIES = {
    'models': Category('Model Providers', 'hosted-section'),
    'editor-settings': Category('Editor', 'editor-section'),
    'folders': Category('Folders', 'folders-section'),
    'network-settings': Category(f'{NETWORK_LABEL} and {WEB_SEARCH_LABEL}', 'network-search-section', 'network-section'),
    'approval-settings': Category('Approvals & sandbox', 'agent-section', 'inner-sandbox'),
    'agent-tools': Category(PI_FEATURES_LABEL, 'skills-section', 'subagents'),
    'system-prompt': Category(SYSTEM_PROMPT_LABEL, 'system-prompt-section'),
    'advisors': Category(ADVISORS_LABEL, 'new-advisors-section'),
    'communications': Category('Communications', 'communications-section', 'slack-enabled'),
    'skills': Category('Skills', 'skill-library-section', 'manage-skills'),
    'connectors': Category('Connectors', 'connectors-section'),
    'document-search': Category('Document search', 'document-search-section', 'configure-document-search'),
    'resources': Category('Resources & environment', 'resources-section', 'image-source'),
}

EDIT_FORMS = {action: category for action, category in CATEGORIES.items() if category.focus}
EDIT_FORMS.update({
    'model-sources': Category('Local models & provider plugins', CATEGORIES['models'].section, 'local-provider'),
})

NETWORK_PATH = f'Settings → {CATEGORIES["network-settings"].label} → {NETWORK_LABEL}'
ADVISORS_PATH = 'Settings → ' + ADVISORS_LABEL
MODEL_PROVIDERS_PATH = 'Settings → ' + CATEGORIES['models'].label
LOCAL_MODELS_PATH = MODEL_PROVIDERS_PATH + ' → ' + EDIT_FORMS['model-sources'].label
OPEN_LABELS = {'browser': 'Open in browser', 'pi': 'Open Pi CLI'}
