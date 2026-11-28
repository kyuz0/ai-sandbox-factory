"""Declarative controls for existing Factory operations.

Descriptors contain public values only. Validation and execution remain in the
shared settings, network and package operations, not in browser handlers.
"""
from dataclasses import dataclass
from pathlib import Path

from . import image_sources, primary_workspace, settings_catalog
from .sandbox_options import APPROVAL_MODES, options

NATIVE_APPROVAL_LABELS = {'codex': 'Approve for me', 'claude': 'Auto', 'agy': 'Allow configured tools'}
RECALL_CHOICES = (('Lite — search history on demand', 'lite'), ('Full — automatic history hints', 'full'), ('Off', 'off'))


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = 'checkbox'
    option: bool = False
    choices: tuple = ()
    help: str = ''
    depends: str = ''

    def describe(self, config, state):
        selected = options(config.get('options')) if self.option else config
        value = selected.get(self.key)
        if self.key == 'allowlist':
            value = (Path(state) / 'allowlist.txt').read_text()
        elif self.key == 'packages':
            value = ''
        elif self.key == 'image_source':
            value = image_sources.mode(config)
        elif self.key == 'working_directory':
            value = config.get(self.key, '.')
        return {'key': self.key, 'label': self.label, 'type': self.kind,
                'value': bool(value) if self.kind == 'checkbox' else (value or [] if self.kind == 'multi' else value or ''),
                'choices': self.choices, 'help': self.help, 'depends': self.depends}


NETWORK = Field('network', settings_catalog.NETWORK_MODE_LABEL, 'select', choices=settings_catalog.NETWORK_MODES,
                help='Restricted uses the sandbox’s allowed destinations. Unrestricted allows ordinary outbound connections.')
PI_APPROVALS = (
    Field('approval', settings_catalog.CONTROL_LABELS['approval'], 'select', True, tuple((row[0], key) for key,row in APPROVAL_MODES.items()),
          ' '.join(f'{row[0]}: {row[1]}' for row in APPROVAL_MODES.values())),
    Field('inner_sandbox', settings_catalog.CONTROL_LABELS['inner_sandbox'], option=True,
          help='Runs Bash with file and network limits. Bash can still edit the workspace.'),
    Field('protect_credentials', settings_catalog.CONTROL_LABELS['protect_credentials'], option=True,
          help='Blocks file tools and sandboxed Bash from reading or writing .env files, private keys and similar files.'),
)
FEATURES = (
    Field('subagents', settings_catalog.CONTROL_LABELS['subagents']),
    Field('compaction_recall', settings_catalog.CONTROL_LABELS['compaction_recall'], 'select', True, RECALL_CHOICES,
          'Finds earlier details after conversation compaction. Full adds automatic hints and may reduce prefix-cache reuse.'),
    Field('browser', settings_catalog.CONTROL_LABELS['browser'], option=True,
          help='Adds a browser with a live viewer for you to see and interact with its pages.'),
    Field('protect_browser_credentials', settings_catalog.CONTROL_LABELS['protect_browser_credentials'], option=True, depends='browser',
          help='Blocks native file tools from the saved vault; browser account selection and login still work. '
               'Active browser sessions remain accessible to browser automation.'),
    Field('question_forms', settings_catalog.CONTROL_LABELS['question_forms'], option=True),
    Field('goals', settings_catalog.CONTROL_LABELS['goals'], option=True),
    Field('scheduling', settings_catalog.CONTROL_LABELS['scheduling'], option=True),
)
RESOURCES = (Field('memory', 'Memory limit', 'text', help='For example 8g. Leave empty for no explicit limit.'),
             Field('image_source', image_sources.LABEL, 'select', choices=image_sources.CHOICES))


def groups(config):
    selected = primary_workspace.agent(config)
    result = {'network': ('Network mode', (NETWORK,))}
    if config.get('network', 'allowlist') == 'allowlist':
        from . import network_groups
        result['destinations'] = ('Allowed destinations', (
            Field('allowlist', 'Additional allowed destinations', 'textarea',
                  help='One hostname:port per line, for example example.com:443.'),
            Field('website_groups','Website categories','multi',True,
                  tuple((value['title'],key) for key,value in network_groups.catalog().items())),))
    result['approvals'] = (settings_catalog.CATEGORIES['approval-settings'].label if selected == 'pi' else 'Approvals', PI_APPROVALS if selected == 'pi' else (
        Field('native_approval', settings_catalog.CONTROL_LABELS['native_approval'], 'select', True,
              ((NATIVE_APPROVAL_LABELS[selected], 'automatic'), ('Ask for approval', 'manual'))),))
    if selected == 'pi':
        result['features'] = (settings_catalog.PI_FEATURES_LABEL, FEATURES)
    else:
        result['web-search'] = (settings_catalog.WEB_SEARCH_LABEL, (
            Field(primary_workspace.WEB_SEARCH_OPTIONS[selected], settings_catalog.CONTROL_LABELS['native_web_search'], option=True),))
        result['browser'] = ('Browser', (Field('browser', settings_catalog.CONTROL_LABELS['native_browser'], option=True),))
    result['resources'] = ('Resources', RESOURCES)
    result['directory'] = ('Starting directory', (Field('working_directory', 'Starting directory', 'text',
        help='Relative to /workspace. Applies to the next client or terminal launch.'),))
    result['packages'] = ('Install system packages', (Field('packages', 'Debian package names', 'text',
        help='Separate names with spaces or commas, for example jq ripgrep.'),))
    return result


def describe(config, state):
    return [{'id': key, 'label': label, 'fields': [field.describe(config,state) for field in fields]}
            for key,(label,fields) in groups(config).items()]


def prepare(config, state, section, values):
    """Translate one form into existing CLI inputs, rejecting hidden/unknown keys."""
    available = groups(config)
    if section not in available:
        raise ValueError(f'Settings section {section!r} is not available for this sandbox. Reload its current settings.')
    title, fields = available[section]
    allowed = {field.key:field for field in fields}
    if not isinstance(values, dict) or set(values) - allowed.keys():
        raise ValueError(f'{title} accepts only these settings: {", ".join(allowed)}. Reload the form.')
    changed = {}
    selected_options = options(config.get('options'))
    for key,value in values.items():
        field = allowed[key]
        if field.kind == 'checkbox' and type(value) is not bool:
            raise ValueError(f'{field.label} must be on or off.')
        if field.kind == 'multi' and (not isinstance(value,list) or any(not isinstance(item,str) for item in value)):
            raise ValueError(f'{field.label} must contain a list of its options.')
        if field.kind not in ('checkbox','multi') and not isinstance(value, str):
            raise ValueError(f'{field.label} must be text.')
        selected = value if field.kind == 'multi' else [value]
        if field.choices and any(item not in {value for _,value in field.choices} for item in selected):
            raise ValueError(f'Choose {field.label} from its listed options.')
        previous = field.describe(config,state)['value']
        before = sorted(previous) if field.kind == 'multi' else previous
        after = sorted(value) if field.kind == 'multi' else value
        if before != after:
            changed[key] = value
    if section == 'packages':
        from . import system_packages
        packages = system_packages.package_names(values.get('packages','').replace(',',' ').split())
        return {'operation':'install-system-packages', 'packages':packages, 'title':title}
    if (section == 'approvals' and primary_workspace.agent(config) != 'pi') or section == 'web-search':
        changes = changed  # Existing native-client shortcuts, no replacement.
    else:
        changes = {key:value for key,value in changed.items() if not allowed[key].option}
        option_changes = {key:value for key,value in changed.items() if allowed[key].option}
        if option_changes:
            changes['options'] = {**selected_options, **option_changes}
    return {'operation':'update', 'changes':changes, 'title':title,
            'summary':[{'label':allowed[key].label, 'before':allowed[key].describe(config,state)['value'],
                        'after':value, 'type':allowed[key].kind, 'choices':allowed[key].choices}
                       for key,value in changed.items()]}

# New settings must be added above or deliberately assigned to their existing
# dedicated flow. The completeness test prevents silent browser omissions.
DEDICATED_OPTIONS = {
    'approval_model':'Reviewer model discovery and availability checks.',
    'knowledge_embedding':'Document search provider setup.',
    'knowledge_embedding_provider':'Document search provider setup.',
    'knowledge_embedding_url':'Document search provider setup.',
    'knowledge_embedding_model':'Document search provider setup.',
    'disabled_skills':'Installed skill selection.',
    'web_access':'Search providers and endpoint-bound credentials.',
    'communications':'Channel setup and private credentials.',
    'git_identity':'SSH & Git connector settings.',
}
DEDICATED_SETTINGS = {
    'aws_access':'AWS grants and credential companion.',
    'extra_folders':'Host folder selection and mount grants.',
    'ssh':'SSH destinations, host keys and private key setup.',
    'advisors':'Advisor selection and sign-in.',
    'env_file':'Host credential environment file selection.',
    'skill_source':'Skill installation.',
    'skill_path':'Skill installation.',
    'remove_skills':'Skill installation.',
    'library_skills':'Skill installation.',
    'local_model_ports':'Local model setup and connection checks.',
    'model_providers':'Provider setup, discovery and private credentials.',
    'curlie_list':'Website-list download and version selection.',
}
