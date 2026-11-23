"""The connector choices shared by Review sandbox and Settings."""
from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

from . import connectors


def connector_summary(config, state=None):
    selected = []
    if config.get('ssh', {}).get('hosts'):
        selected.append('SSH & Git')
    elif config.get('options', {}).get('git_identity'):
        selected.append('Git commit identity')
    if config.get('aws_access'):
        selected.append('AWS')
    ids = config.get('connector_ids', [])
    if ids:
        titles = {item.id: item.title for item in connectors.for_sandbox(config, state)}
        selected.extend(titles.get(identifier, identifier) for identifier in ids)
    # Keep the heading on one line in the 80-column Settings menu.
    summary = ' · '.join(selected)
    if cell_len(summary) <= 48:
        return summary or 'Off'
    for visible in range(len(selected) - 1, 0, -1):
        suffix = f' · +{len(selected) - visible} more'
        summary = ' · '.join(selected[:visible])
        if cell_len(summary + suffix) <= 48:
            return summary + suffix
    suffix = f' · +{len(selected) - 1} more' if len(selected) > 1 else ''
    return set_cell_size(selected[0], 47 - cell_len(suffix)).rstrip() + '…' + suffix


class ConnectorFields(Vertical):
    DEFAULT_CSS = """
    ConnectorFields { height: auto; max-width: 84; }
    ConnectorFields .connector-row { height: auto; }
    ConnectorFields .connector-row Button { width: auto; max-width: 60%; padding: 0 2; }
    ConnectorFields .connector-row Static { width: 1fr; height: auto; }
    ConnectorFields .connector-description { margin: 0; }
    """

    class Configure(Message):
        def __init__(self, connector):
            self.connector = connector
            super().__init__()

    def __init__(self, config, *, state=None):
        super().__init__()
        self.config = config
        self.state = state

    def compose(self):
        choices, errors = connectors.catalog_with_errors(self.config if self.state is not None else None, self.state)
        self.choices = {connector.id: connector for connector in choices}
        yield Static('Choose which services this sandbox can access. Each connector keeps its credentials outside the workspace.',
                     classes='section-intro')
        # Keep status values aligned beside even translated or wide-character service names.
        button_width = max(cell_len(connector.title + '…') for connector in choices if connector.available) + 4
        for connector in choices:
            if connector.available:
                with Vertical(classes='setting'):
                    with Horizontal(classes='connector-row'):
                        button = Button(Text(connector.title + '…'), id='connector-' + connector.id, name=connector.id)
                        button.styles.width = button_width
                        yield button
                        yield Static(self.status(connector.id), id='connector-status-' + connector.id, markup=False)
                    yield Static(connector.description, classes='setting-help connector-description', markup=False)
        planned = [connector.title for connector in choices if not connector.available]
        if planned:
            yield Static(' and '.join(planned) + ' access is planned.', id='planned-connectors', classes='setting-help', markup=False)
        for error in errors:
            yield Static(str(error), classes='error', markup=False)

    def status(self, identifier):
        connector = self.choices[identifier]
        if not connector.external:
            return connectors.ADAPTERS[identifier].status(self.config)
        if identifier not in self.config.get('connector_ids', []):
            return 'Off'
        routes = self.choices[identifier].routes
        if not routes:
            return 'Configuration unreadable'
        if len(routes) > 1:
            allowed = self.config.get('connector_operations', {}).get(identifier, dict(routes))
            return f'{len(allowed)} of {len(routes)} requests'
        return 'On'

    def update_config(self, config):
        self.config = config
        for identifier, connector in self.choices.items():
            if connector.available:
                self.query_one('#connector-status-' + identifier, Static).update(self.status(identifier))

    @on(Button.Pressed)
    def configure(self, event):
        event.stop()
        self.post_message(self.Configure(self.choices[event.button.name]))
