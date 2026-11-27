"""Concise review of the same container update plan used by the CLI."""
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from . import cli_recipes
from .container_updates import STATUS
from .tui_widgets import Bullet, Details


class ContainerUpdates(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record):
        super().__init__()
        self.add_class('bounded-form')
        self.record = record
        self.plan = record.get('updates') or {'status': 'unknown', 'reason': 'Refresh the sandbox list to check its container versions.'}

    def compose(self):
        yield Header()
        yield Label('Container updates · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Details([('Updates', STATUS[self.plan['status']] or 'Up to date')])
                if self.plan['status'] == 'available':
                    labels = list(dict.fromkeys(change['label'] for change in self.plan['changes']))
                    yield Details([('Containers to update', ', '.join(labels))])
                    if self.plan.get('unknown'):
                        yield Details([('Cannot compare', ', '.join(self.plan['unknown']))])
                    for effect in self.plan['effects']:
                        yield Bullet(effect)
                    yield Static('A stopped sandbox stays stopped. An open browser editor keeps its address.', classes='setting-help')
                elif self.plan.get('reason'):
                    yield Static(self.plan['reason'], markup=False)
            yield Button('More information…', id='container-updates-help')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='container-updates-back')
            if self.plan['status'] == 'available':
                yield Button('Update containers', id='container-updates-apply', variant='primary')
        yield Footer()

    def on_mount(self):
        self.query_one('#container-updates-apply' if self.plan['status'] == 'available' else '#container-updates-back', Button).focus()

    @on(Button.Pressed, '#container-updates-back')
    def action_back(self):
        self.app.pop_screen()

    @on(Button.Pressed, '#container-updates-help')
    def help(self):
        from .sandbox_tui import FactoryUpdateDetails
        self.app.push_screen(FactoryUpdateDetails('About container updates',
            'Factory compares the versions applied to this sandbox with the recipes and tested images in your installed Factory version. '
            'Upgrade Factory to receive newer tested versions. Checking does not download images.\n\n'
            'Prebuilt images are downloaded by their tested digest. Local builds use the same installed recipes. '
            'Only containers used by this sandbox are checked.\n\n'
            'Custom and editable profiles are maintained by their owner. You manage their software, service updates and rebuilds. '
            'Factory still manages its credential containers and network controls.'))

    @on(Button.Pressed, '#container-updates-apply')
    def apply(self):
        app, name, plan = self.app, self.record['name'], self.plan
        app.pop_screen()
        command = ['update-containers', name, '--revision', plan['revision']]
        recipe = cli_recipes.Recipe('Update containers', (cli_recipes.Step(tuple(command)),), str(app.state_root))
        app.start_settings_job(name, 'Update containers · ' + name,
            lambda log, update: app.service.run_update(command, log, update),
            cooperative=True, success_message='Containers updated.', cli_recipe=recipe)
