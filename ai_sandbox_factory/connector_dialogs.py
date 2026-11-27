"""Configure access to services in a saved sandbox."""

import asyncio

from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Label, Static

from . import connectors
from .connector_fields import ConnectorFields
from .tui_widgets import Checkbox, Command, Details, Feedback

DISCONNECT_EFFECT = 'Disconnecting removes access for this sandbox. Your saved token stays on this computer.'


class ConnectorSetup(ModalScreen):
    BINDINGS = [('escape', 'back', 'Back')]
    AUTO_FOCUS = '#connector-setup-back'
    DEFAULT_CSS = """
    ConnectorSetup { align: center middle; background: #000000 70%; }
    ConnectorSetup .connector-setup-body { height: auto; max-height: 57vh; scrollbar-size-vertical: 1; }
    ConnectorSetup .connector-command { padding: 0 2; margin: 1 0; }
    """

    def __init__(self, connector):
        super().__init__()
        self.connector = connector

    def compose(self):
        installed = next((item for item in connectors.catalog() if item.id == self.connector.id), None)
        with Vertical(id='confirm-box'):
            yield Label('Set up ' + self.connector.title, markup=False, classes='title')
            with VerticalScroll(classes='connector-setup-body'):
                if installed and installed.account == self.connector.account:
                    yield Static('Run this command on your computer to save your service token.')
                    yield Command('ai-sandbox-factory connector-login ' + self.connector.id,
                                  classes='connector-command')
                else:
                    yield Static('This sandbox uses a saved copy of the connector. To change its token, '
                                 'reinstall the connector version this sandbox uses.',
                                 classes='section-intro')
                yield Static(self.connector.documentation, markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Back', id='connector-setup-back')

    @on(Button.Pressed, '#connector-setup-back')
    def action_back(self):
        self.dismiss()


class ExternalConnectorDialog(ModalScreen):
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    DEFAULT_CSS = """
    ExternalConnectorDialog { align: center middle; background: #000000 70%; }
    ExternalConnectorDialog .connector-details {
        height: auto; max-height: 57vh; scrollbar-size-vertical: 1;
    }
    ExternalConnectorDialog .connector-endpoint { margin: 0 0 1 0; }
    ExternalConnectorDialog .connector-endpoint Checkbox { margin: 0; }
    ExternalConnectorDialog .connector-endpoint Static { width: 100%; height: auto; }
    ExternalConnectorDialog #connector-scope-help { height: auto; padding: 0 0 1 0; }
    ExternalConnectorDialog .connector-help-actions { height: auto; }
    ExternalConnectorDialog #connector-retry { margin: 0 1 0 0; }
    ExternalConnectorDialog #connector-agent-guide { height: auto; margin: 0 0 1 0; }
    ExternalConnectorDialog #connector-agent-text { height: auto; }
    ExternalConnectorDialog .token-state { height: auto; margin: 0 0 1 0; }
    ExternalConnectorDialog #connector-retry, ExternalConnectorDialog #connector-setup { width: auto; padding: 0 2; }
    ExternalConnectorDialog #connector-confirm:disabled { background: $surface-lighten-1; color: $text-muted; }
    ExternalConnectorDialog:nocolor #connector-confirm:disabled { background: #292929; color: #c9c9c9; }
    ExternalConnectorDialog:nocolor Button:focus { color: black; background: white; text-style: bold underline; }
    """

    def __init__(self, connector, *, enabled, enabled_operations=None, draft=False):
        super().__init__()
        self.connector = connector
        self.enabled = enabled
        self.draft = draft
        self.token_ready = enabled
        self.multiple = len(connector.routes) > 1
        self.original = set(dict(connector.routes) if enabled_operations is None else enabled_operations)
        if not enabled:
            self.original.clear()

    def compose(self):
        connector = self.connector
        with Vertical(id='confirm-box'):
            yield Label(connector.title, markup=False, classes='title')
            with VerticalScroll(classes='connector-details'):
                yield Static(connector.description, classes='section-intro', markup=False)
                if self.multiple:
                    yield Static('Select at least one request for this sandbox.', id='connector-scope-help')
                for operation, url in connector.routes:
                    with Vertical(classes='connector-endpoint setting'):
                        if self.multiple:
                            yield Checkbox(operation, name=operation, value=operation in self.original)
                        else:
                            yield Details([('Request', operation)])
                        yield Static('GET ' + url, classes='checkbox-help', markup=False)
                if connector.agent_instructions or connector.operation_instructions:
                    with Collapsible(title='Instructions for the coding agent', id='connector-agent-guide'):
                        yield Static('', id='connector-agent-text', markup=False)
                if not self.enabled:
                    yield Static('Checking for a saved token…', classes='token-state', id='connector-token', markup=False)
                if connector.routes:
                    with Horizontal(classes='connector-help-actions'):
                        if not self.enabled:
                            yield Button('Check again', id='connector-retry')
                        yield Button('Setup instructions…', id='connector-setup')
            with Horizontal(classes='dialog-actions'):
                if self.enabled:
                    yield Button('Remove' if self.draft else 'Disconnect…', id='connector-disconnect')
                yield Button('Cancel', id='connector-cancel')
                if self.multiple or not self.enabled:
                    yield Button('Save changes' if self.enabled else ('Add' if self.draft else 'Connect'),
                                 id='connector-confirm', disabled=True)

    def on_mount(self):
        self.update_choices()
        if self.multiple:
            self.query_one(Checkbox).focus()
        elif self.enabled:
            self.query_one('#connector-cancel', Button).focus()
        if not self.enabled:
            self.query_one('#connector-retry', Button).display = False
            self.check_token(initial=True)

    @work(exclusive=True)
    async def check_token(self, *, initial=False):
        try:
            self.token_ready = await asyncio.to_thread(connectors.has_token, self.connector)
            message = ('Token saved on this computer.' if self.token_ready else
                       'Save your service token on this computer, then choose Check again.\n'
                       'ai-sandbox-factory connector-login ' + self.connector.id)
        except (ValueError, OSError) as exc:
            self.token_ready = False
            message = 'Could not check the saved token. ' + str(exc)
        if not self.is_mounted:
            return
        self.query_one('#connector-token', Static).update(message)
        self.query_one('#connector-retry', Button).display = not self.token_ready
        self.update_choices()
        if self.is_active and not self.multiple and (initial or self.focused is None):
            self.query_one('#connector-confirm' if self.token_ready else '#connector-retry', Button).focus()

    @on(Button.Pressed, '#connector-retry')
    def retry_token(self):
        self.query_one('#connector-token', Static).update('Checking for a saved token…')
        self.check_token()

    def selected_operations(self):
        if not self.multiple:
            return [name for name, _ in self.connector.routes]
        return [box.name for box in self.query(Checkbox) if box.value]

    @on(Checkbox.Changed)
    def update_choices(self):
        selected = self.selected_operations()
        if self.query('#connector-confirm'):
            button = self.query_one('#connector-confirm', Button)
            button.disabled = not self.token_ready or not selected or set(selected) == self.original
            button.variant = 'default' if button.disabled else 'primary'
        guide = self.query('#connector-agent-guide')
        if guide:
            by_operation = dict(self.connector.operation_instructions)
            parts = [self.connector.agent_instructions] if self.connector.agent_instructions else []
            parts.extend(name + '\n' + by_operation[name] for name in selected if name in by_operation)
            guide.first().display = bool(parts)
            self.query_one('#connector-agent-text', Static).update('\n\n'.join(parts))

    @on(Button.Pressed, '#connector-disconnect')
    def disconnect(self):
        if self.draft:
            self.dismiss([])
            return
        from .sandbox_tui import Confirm

        def confirmed(yes):
            if yes:
                self.dismiss([])

        self.app.push_screen(Confirm(DISCONNECT_EFFECT, 'Disconnect', confirm_by_default=False,
                                     title='Disconnect ' + self.connector.title + '?'), confirmed)

    @on(Button.Pressed, '#connector-setup')
    def instructions(self):
        self.app.push_screen(ConnectorSetup(self.connector))

    @on(Button.Pressed, '#connector-confirm')
    def confirm(self):
        if self.token_ready and self.selected_operations():
            self.dismiss(self.selected_operations())

    @on(Button.Pressed, '#connector-cancel')
    def action_cancel(self):
        self.dismiss(None)


class ConnectorSettings(ModalScreen):
    BINDINGS = [('escape', 'back', 'Back')]
    DEFAULT_CSS = """
    ConnectorSettings { align: center middle; background: #000000 70%; }
    ConnectorSettings .connector-settings-body { height: auto; max-height: 58vh; scrollbar-size-vertical: 1; }
    """

    def __init__(self, sandbox_name, config):
        super().__init__()
        self.sandbox_name = sandbox_name
        self.config = config

    def compose(self):
        with Vertical(id='confirm-box'):
            yield Label('Connectors · ' + self.sandbox_name, markup=False, classes='title')
            with VerticalScroll(classes='connector-settings-body'):
                yield ConnectorFields(self.config, state=self.app.state_root / self.sandbox_name)
                yield Feedback('', id='connector-settings-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Back', id='connector-settings-back')

    def on_mount(self):
        self.query_one('ConnectorFields Button', Button).focus()

    async def on_screen_resume(self):
        if self.is_mounted:
            try:
                config = self.app.service.config(self.sandbox_name)
            except (OSError, ValueError) as exc:
                self.query_one('#connector-settings-error', Feedback).update(str(exc))
                return
            self.query_one('#connector-settings-error', Feedback).update('')
            if config != self.config:
                self.config = config
                fields = self.query_one(ConnectorFields)
                button_id = getattr(self.focused, 'id', None)
                fields.config = config
                await fields.recompose()
                target = self.query('#' + button_id) if button_id else []
                (target[0] if target else fields.query_one(Button)).focus()

    @on(ConnectorFields.Configure)
    def configure(self, event):
        event.stop()
        connector = event.connector
        if not connector.external:
            action = connectors.ADAPTERS[connector.id].saved_action
            self.app.perform(action, record={'name': self.sandbox_name, 'config': self.config})
            return
        current = list(self.config.get('connector_ids', []))
        operations = self.config.get('connector_operations', {})
        enabled = connector.id in current

        def selected(requests):
            if requests is None:
                return
            chosen = list(current)
            updated = dict(operations)
            if requests:
                if not enabled:
                    chosen.append(connector.id)
                updated[connector.id] = requests
            else:
                chosen.remove(connector.id)
                updated.pop(connector.id, None)
            changes = {'connector_ids': chosen, 'expected_connector_ids': current,
                       'connector_operations': updated, 'expected_connector_operations': operations}
            if requests and not enabled:
                changes['expected_connector_routes'] = {connector.id: dict(connector.routes)}
                changes['expected_connector_instructions'] = {connector.id: connector.reviewed_instructions}
            service = self.app.service
            from . import cli_recipes
            self.app.start_settings_job(
                self.sandbox_name, 'Update ' + connector.title,
                lambda log, update: service.update_settings(self.sandbox_name, changes, log, update),
                success_message=connector.title + (' access saved.' if requests else ' disconnected.'),
                cli_recipe=cli_recipes.update(self.sandbox_name, changes, self.app.state_root))

        self.app.push_screen(ExternalConnectorDialog(connector, enabled=enabled,
                                                    enabled_operations=operations.get(connector.id)), selected)

    @on(Button.Pressed, '#connector-settings-back')
    def action_back(self):
        self.dismiss(None)
