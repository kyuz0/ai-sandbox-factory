"""Names and navigation shared by sandbox creation and editing."""
from typing import NamedTuple


class Category(NamedTuple):
    label: str
    section: str
    focus: str | None = None


# In display order. A focus target opens the shared settings form for a saved
# sandbox. Other categories have a dedicated page for live operations.
CATEGORIES = {
    'models': Category('Models', 'hosted-section'),
    'editor-settings': Category('Editor', 'editor-section'),
    'folders': Category('Folders', 'folders-section'),
    'ssh-settings': Category('SSH & Git', 'git-identity-section'),
    'advisors': Category('Advisors', 'new-advisors-section'),
    'network-settings': Category('Network', 'network-section'),
    'web-access': Category('Web access', 'web-section', 'web-enabled'),
    'communications': Category('Communications', 'communications-section', 'slack-enabled'),
    'approval-settings': Category('Approvals & sandbox', 'agent-section', 'approval'),
    'agent-tools': Category('Agent tools', 'skills-section', 'subagents'),
    'skills': Category('Skills', 'skill-library-section', 'manage-skills'),
    'resources': Category('Resources & environment', 'resources-section', 'memory'),
}

EDIT_FORMS = {action: category for action, category in CATEGORIES.items() if category.focus}
EDIT_FORMS.update({
    'network-mode': Category('Network mode', CATEGORIES['network-settings'].section, 'network'),
    'model-sources': Category('Local models & provider plugins', CATEGORIES['models'].section, 'local-models'),
})

OPEN_LABELS = {'browser': 'Open in browser', 'pi': 'Open Pi CLI', 'desktop': 'Open VS Code'}
