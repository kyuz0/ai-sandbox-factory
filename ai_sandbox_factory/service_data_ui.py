"""Find, export and restore data kept by reviewed profile changes."""
from datetime import datetime
from pathlib import Path

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

from . import profile_edit, profile_runtime, profile_services, service_data
from .file_browser import PathField
from .tui_widgets import Bullet, Details, Feedback


def saved_label(row):
    reason = {'replaced': 'service replaced', 'removed': 'service removed', 'restored': 'before data restore'}.get(row['reason'], row['reason'])
    return f'{row["service"]} · {saved_time(row)} · {reason}'


def saved_time(row):
    return datetime.fromisoformat(row['time']).strftime('%Y-%m-%d %H:%M:%S UTC')


def image_label(reference, identifier=None):
    return reference + (' · ' + identifier.removeprefix('sha256:')[:12] if identifier else '')


def targets(record, saved):
    return {name: spec for name, spec in profile_services.selected(record['config']).items()
            if saved['volumes'].keys() <= spec['volumes'].keys()}


class SavedServiceData(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record = record
        self.saved = service_data.entries(record['config'])

    def compose(self):
        yield Header()
        yield Label('Saved service data · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Static('Factory keeps the old data when you replace or remove a service, and before restoring saved data. '
                         'These Podman volumes stay on this computer until you delete the sandbox.', classes='section-intro')
            if self.saved:
                with Vertical(classes='setting'):
                    yield Label('Saved data')
                    yield Select([(saved_label(row), row['id']) for row in self.saved], value=self.saved[0]['id'],
                                 allow_blank=False, id='saved-data-set')
                with Vertical(classes='setting'):
                    yield Label('Where this data came from', classes='section-heading')
                    yield Details([], id='saved-data-details')
                with Vertical(classes='setting'):
                    yield Label('Data volumes', classes='section-heading')
                    yield Details([], id='saved-data-volumes')
                yield Static('Export saves a volume as a .tar file. Restore replaces a selected service’s data with a copy of these volumes.',
                             classes='section-intro', id='saved-data-help')
            else:
                yield Static('Factory has not kept any previous service data for this sandbox.')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='saved-data-back')
            if self.saved:
                with Horizontal(classes='data-actions'):
                    yield Button('Export data…', id='saved-data-export')
                    yield Button('Restore data…', id='saved-data-restore', variant='primary')
        yield Footer()

    def selected(self):
        value = self.query_one('#saved-data-set', Select).value
        return next(row for row in self.saved if row['id'] == value)

    def on_mount(self):
        if self.saved:
            self.refresh_details()
            self.query_one('#saved-data-set').focus()
        else:
            self.query_one('#saved-data-back').focus()

    @on(Select.Changed, '#saved-data-set')
    def changed(self, event):
        if self.is_mounted and event.value == event.select.value:
            self.refresh_details()

    def refresh_details(self):
        row = self.selected()
        self.query_one('#saved-data-details', Details).set_rows([
            ('Service', row['service']), ('Saved at', saved_time(row)), ('Source image', image_label(row['image'],row.get('image_id'))),
            ('Profile version', row['profile_digest'])])
        self.query_one('#saved-data-volumes', Details).set_rows([
            (key + ' · ' + item['target'] + ' · Podman volume', item['volume']) for key, item in row['volumes'].items()])
        can_restore = bool(targets(self.record,row))
        self.query_one('#saved-data-restore',Button).display = can_restore
        self.query_one('#saved-data-export',Button).variant = 'default' if can_restore else 'primary'
        self.query_one('#saved-data-help',Static).update(
            'Export saves a volume as a .tar file. Restore replaces a selected service’s data with a copy of these volumes.' if can_restore else
            'Export saves this data without adding the service back. To restore it, open '
            + profile_edit.action_path(self.record['config'])
            + ', add the service to the editable copy, review and apply the changes, then return here.')

    @on(Button.Pressed, '#saved-data-export')
    def export_data(self):
        self.app.push_screen(ExportServiceData(self.record, self.selected()))

    @on(Button.Pressed, '#saved-data-restore')
    def restore_data(self):
        self.app.push_screen(RestoreServiceData(self.record, self.selected()))

    @on(Button.Pressed, '#saved-data-back')
    def action_back(self):
        self.app.pop_screen()


class ExportServiceData(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, saved):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record, self.saved = record, saved

    def default_path(self, key):
        timestamp = datetime.fromisoformat(self.saved['time']).strftime('%Y%m%dT%H%M%SZ')
        return str(Path.home() / f'{self.record["name"]}-{self.saved["service"]}-{timestamp}-{key}.tar')

    def compose(self):
        yield Header()
        yield Label('Export service data · ' + self.record['name'], classes='title focused-settings-title')
        key = next(iter(self.saved['volumes']))
        with VerticalScroll(classes='form spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Label('Saved copy', classes='section-heading')
                yield Details([('Service', self.saved['service']), ('Saved at', saved_time(self.saved))])
            with Vertical(classes='setting'):
                yield Label('Data volume')
                yield Select([(name + ' · ' + value['target'], name) for name, value in self.saved['volumes'].items()],
                             value=key, allow_blank=False, id='service-export-volume')
            with Vertical(classes='setting'):
                yield Label('Archive file on this computer')
                yield PathField('service-export-path', self.default_path(key), title='Choose an archive file')
                yield Static('Choose a new .tar file. The saved volume and running services stay unchanged.', classes='setting-help')
                yield Feedback('', id='service-export-error')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='service-export-back')
            yield Button('Export data', id='service-export-go', variant='primary')
        yield Footer()

    def on_mount(self):
        self.query_one('#service-export-path', Input).focus()

    @on(Select.Changed, '#service-export-volume')
    def volume_changed(self, event):
        if self.is_mounted and event.value == event.select.value and event.value != Select.BLANK:
            field = self.query_one('#service-export-path', Input)
            if field.value in [self.default_path(key) for key in self.saved['volumes']]:
                field.value = self.default_path(event.value)

    @on(Button.Pressed, '#service-export-go')
    def export_data(self):
        path = self.query_one('#service-export-path', Input).value.strip()
        if not path:
            self.query_one('#service-export-error', Feedback).update('Enter a new archive file on this computer.')
            self.query_one('#service-export-path', Input).focus()
            return
        volume = self.query_one('#service-export-volume', Select).value
        app, name = self.app, self.record['name']
        args = ['service-data-export', name, '--saved', self.saved['id'], '--volume', volume, '--output', path]
        app.pop_screen()
        app.pop_screen()
        app.start_settings_job(name, 'Export service data · ' + name,
            lambda log, update: app.service.run_update(args, log, update), cooperative=True,
            success_message='Service data exported to ' + path + '.')

    @on(Button.Pressed, '#service-export-back')
    def action_back(self):
        self.app.pop_screen()


class RestoreServiceData(Screen):
    BINDINGS = [('escape', 'back', 'Back')]

    def __init__(self, record, saved):
        super().__init__()
        self.add_class('bounded-form', 'profile-edit-flow')
        self.record, self.saved = record, saved
        self.targets = targets(record,saved)

    def compose(self):
        yield Header()
        yield Label('Restore service data · ' + self.record['name'], classes='title focused-settings-title')
        with VerticalScroll(classes='form spaced-form-fields'):
            with Vertical(classes='setting'):
                yield Label('Saved copy', classes='section-heading')
                yield Details([('Service', self.saved['service']), ('Saved at', saved_time(self.saved)),
                               ('Source image', image_label(self.saved['image'],self.saved.get('image_id')))])
            if self.targets:
                with Vertical(classes='setting'):
                    yield Label('Restore into')
                    yield Select([(name, name) for name in self.targets], allow_blank=False, id='service-restore-target',
                                 value=self.saved['service'] if self.saved['service'] in self.targets else next(iter(self.targets)))
                with Vertical(classes='setting'):
                    yield Details([], id='service-restore-image')
                with Vertical(classes='setting'):
                    yield Label('Saved data → Service data', classes='section-heading')
                    yield Details([], id='service-restore-mapping')
                yield Bullet('Factory stops this service during restore.')
                yield Bullet('Its current data is kept in Saved service data.')
                yield Bullet('Its current image and connection settings are kept.')
                yield Bullet('Its current image must be able to read the saved data format.')
                yield Bullet('Factory starts the service briefly to check it. A previously stopped service is stopped again afterwards.')
                yield Bullet('Other services, project files and coding-agent sessions stay unchanged.')
                yield Bullet('',id='service-restore-credentials')
            else:
                yield Static('There is no service with matching data storage: ' + ', '.join(self.saved['volumes']) + '. '
                             'Open ' + profile_edit.action_path(self.record['config'])
                             + ', add the service to the editable copy, review and apply the changes, '
                             'then return here to restore its data. Choose Back, then Export data… to save an archive now.', classes='section-intro')
        with Horizontal(classes='wizard-actions'):
            yield Button('Back', id='service-restore-back')
            if self.targets:
                yield Button('Restore data…', id='service-restore-go', variant='primary')
        yield Footer()

    def on_mount(self):
        if self.targets:
            self.refresh_mapping()
            self.query_one('#service-restore-target').focus()
        else:
            self.query_one('#service-restore-back').focus()

    @on(Select.Changed, '#service-restore-target')
    def target_changed(self, event):
        if self.is_mounted and event.value == event.select.value:
            self.refresh_mapping()

    def refresh_mapping(self):
        name = self.query_one('#service-restore-target', Select).value
        spec = self.targets[name]
        frozen = profile_runtime.applied(self.app.state_root/self.record['name']) or {}
        self.query_one('#service-restore-image',Details).set_rows([
            ('Current image',image_label(profile_services.image(self.record['config'],name,spec),frozen.get('services',{}).get(name)))])
        self.query_one('#service-restore-mapping', Details).set_rows([
            (key, 'Saved ' + value['target'] + ' → ' + name + ' ' + spec['volumes'][key]) for key, value in self.saved['volumes'].items()])
        notice = self.query_one('#service-restore-credentials',Bullet)
        notice.display = name != self.saved['service']
        notice.query_one('.bullet-text',Static).update('Passwords stored inside the saved data come from '+self.saved['service']+'. They may differ from this service’s current connection settings.')

    @on(Button.Pressed, '#service-restore-go')
    def restore_data(self):
        from .sandbox_tui import Confirm
        app, name = self.app, self.record['name']
        target = self.query_one('#service-restore-target', Select).value
        def confirmed(yes):
            if yes:
                app.pop_screen()
                app.pop_screen()
                args = ['service-data-restore', name, '--saved', self.saved['id'], '--service', target]
                app.start_settings_job(name, 'Restore service data · ' + name,
                    lambda log, update: app.service.run_update(args, log, update), cooperative=True,
                    success_message=f'Saved data restored to {target}. Its previous data is available in Actions… → Profile and services… → Saved service data…')
        notes = ['Factory stops this service during restore.', 'Its current data is kept for recovery.',
                 'Its current image and connection settings are kept.', 'The image must be able to read the saved data format.',
                 'Other services and coding-agent work are unchanged.']
        if target != self.saved['service']:
            notes.append('Passwords stored in the saved data may differ from this service’s connection settings.')
        app.push_screen(Confirm(f'Restore the {self.saved["service"]} data saved at {saved_time(self.saved)} '
                               f'into {target} in {name}?', 'Restore data', confirm_by_default=True, notes=notes), confirmed)

    @on(Button.Pressed, '#service-restore-back')
    def action_back(self):
        self.app.pop_screen()
