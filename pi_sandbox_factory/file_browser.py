"""Shared host file and folder browser for Factory forms."""

from pathlib import Path
import subprocess

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from textual_fspicker.parts import DirectoryNavigation
from textual_fspicker.icons import Icons
from textual_fspicker.path_filters import Filter
from textual_fspicker.safe_tests import is_dir

from . import host_platform
from .tui_widgets import Checkbox, Feedback


Icons.set_picker(lambda path: Text('▸' if is_dir(path) else '·'))


class FolderPicker(ModalScreen[str | None]):
    BINDINGS = [('escape', 'cancel', 'Cancel'), ('ctrl+l', 'location', 'Go to folder')]

    def __init__(self, start, *, title='Choose workspace folder', mode='folder', file_extensions=None,
                 places=None, root=None, filename=''):
        super().__init__()
        self.title_text = title
        self.mode = mode
        self.file_extensions = file_extensions
        self.file = None
        self.filename = filename
        self.root = Path(root).resolve() if root else None
        home = Path.home()
        try:
            path = host_platform.local_path(start) if start else home
            self.start = path if path.is_dir() else path.parent if path.parent.is_dir() else self.root or home
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            self.start = self.root or home
        self.places = {'Workspace': self.root} if self.root else {'Home': home, 'Downloads': home / 'Downloads'}
        self.places.update(places or {})
        self.places = {label: path for label, path in self.places.items() if path.is_dir()}

    def compose(self):
        with Vertical(id='folder-box'):
            yield Label(self.title_text, classes='title', markup=False)
            yield Label('Folder', classes='folder-input-label')
            with Horizontal(classes='path-row'):
                yield Input(str(self.start.absolute()), id='folder-path', select_on_focus=False)
                yield Button('Up', id='folder-up')
            with Horizontal(id='folder-places'):
                for index, label in enumerate(self.places):
                    yield Button(label, id=f'folder-place-{index}')
                yield Static('', classes='action-spacer')
                yield Checkbox('Show hidden items', id='folder-hidden')
            with Horizontal(id='folder-columns'):
                yield Static('Name', id='folder-name-column')
                yield Static('Bytes', id='folder-size-column')
                yield Static('Modified', id='folder-modified-column')
            yield DirectoryNavigation(self.start)
            if self.mode == 'save':
                yield Label('File name', classes='folder-input-label', id='folder-filename-label')
                yield Input(self.filename, id='folder-filename', select_on_focus=False)
            yield Feedback(id='folder-browse-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Cancel', id='folder-cancel')
                yield Button('Use this folder' if self.mode == 'folder' else 'Use this file',
                             id='folder-use', variant='primary', disabled=self.mode == 'file')

    @property
    def navigation(self):
        return self.query_one(DirectoryNavigation)

    def on_mount(self):
        navigation = self.navigation
        navigation.show_files = self.mode != 'folder'
        if self.file_extensions:
            navigation.file_filter = Filter('Supported files', self.allowed_file)
        navigation.focus()

    def action_cancel(self):
        self.dismiss(None)

    def action_location(self):
        self.query_one('#folder-path', Input).focus()

    def allowed_file(self, path):
        return self.mode != 'folder' and (not self.file_extensions or path.suffix.lower() in self.file_extensions)

    def choose(self, path):
        if self.root and not path.resolve().is_relative_to(self.root):
            self.query_one('#folder-browse-error', Feedback).update('Error · Choose a folder inside the workspace.')
            return
        self.dismiss(str(path.absolute()))

    def go_to(self, path):
        try:
            location = path.resolve(strict=True)
            if not location.is_dir():
                raise ValueError()
        except (OSError, RuntimeError, ValueError):
            self.query_one('#folder-browse-error', Feedback).update('Error · Cannot open that folder.')
            return
        self.navigation.location = location
        self.navigation.focus()

    def update_file(self, path=None):
        self.file = path if path and self.allowed_file(path) and path.is_file() else None
        if self.mode == 'file':
            self.query_one('#folder-use', Button).disabled = self.file is None

    @on(DirectoryNavigation.Changed)
    def location_changed(self, event):
        self.query_one('#folder-path', Input).value = str(event.navigation.location)
        self.query_one('#folder-browse-error', Feedback).update('')
        self.update_file()

    @on(DirectoryNavigation.Highlighted)
    def highlighted(self, event):
        self.update_file(event.path)

    @on(DirectoryNavigation.Selected)
    def selected(self, event):
        if self.allowed_file(event.path) and event.path.is_file():
            if self.mode == 'save':
                self.query_one('#folder-filename', Input).value = event.path.name
            else:
                self.choose(event.path)

    @on(DirectoryNavigation.PermissionError)
    def permission_error(self):
        self.query_one('#folder-browse-error', Feedback).update('Error · Cannot read that folder.')

    @on(Input.Submitted, '#folder-path')
    def typed_path(self, event):
        try:
            path = self.navigation.location / host_platform.local_path(event.value.strip())
            if path.is_dir():
                self.go_to(path)
                return
            if self.allowed_file(path) and path.is_file():
                self.choose(path)
                return
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            pass
        self.query_one('#folder-browse-error', Feedback).update('Error · Enter an existing folder.' if self.mode == 'folder'
                                                               else 'Error · Enter a folder or a supported file.')

    @on(Input.Submitted, '#folder-filename')
    def save_path(self):
        name = self.query_one('#folder-filename', Input).value.strip()
        if name in ('', '.', '..') or Path(name).name != name or not self.allowed_file(Path(name)):
            self.query_one('#folder-browse-error', Feedback).update('Error · Enter a supported file name.')
            return
        candidate = self.navigation.location / name
        if candidate.is_dir():
            self.go_to(candidate)
            return
        self.choose(candidate)

    @on(Checkbox.Changed, '#folder-hidden')
    def hidden_changed(self, event):
        self.navigation.show_hidden = event.value

    @on(Button.Pressed)
    def pressed(self, event):
        action = event.button.id
        if action == 'folder-cancel':
            self.action_cancel()
        elif action == 'folder-up':
            self.go_to(self.navigation.location.parent)
        elif action == 'folder-use':
            if self.mode == 'save':
                self.save_path()
            elif self.mode == 'folder':
                self.choose(self.navigation.location)
            elif self.file and self.file.is_file():
                self.choose(self.file)
        elif action and action.startswith('folder-place-'):
            self.go_to(list(self.places.values())[int(action.rsplit('-', 1)[1])])


class PathField(Horizontal):
    """An editable host path with the same Browse control in every form."""

    def __init__(self, field_id, value='', *, title, mode='folder', file_extensions=None, placeholder='',
                 initial=None, places=None, root=None, browse_id=None, disabled=False):
        super().__init__(id=f'{field_id}-field', classes='path-row', disabled=disabled)
        self.field_id, self.value_text, self.placeholder = field_id, value, placeholder
        self.title_text, self.mode, self.file_extensions = title, mode, file_extensions
        self.initial, self.places, self.root = initial, places, root
        self.browse_id = browse_id or f'{field_id}-browse'

    def compose(self):
        yield Input(self.value_text, id=self.field_id, placeholder=self.placeholder, select_on_focus=False,
                    disabled=self.disabled)
        yield Button('Browse…', id=self.browse_id, disabled=bool(self.root and not Path(self.root).is_dir()))

    @on(Button.Pressed)
    def browse(self, event):
        event.stop()
        field = self.query_one(Input)
        value = field.value.strip()
        try:
            path = host_platform.local_path(value) if value and '://' not in value else self.initial
            path = Path(self.root) / (path or '.') if self.root else path
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            path = self.initial or self.root
        path = Path(path) if path else None
        filename = path.name if self.mode == 'save' and path and not path.is_dir() else ''

        def selected(result):
            if result:
                chosen = Path(result)
                field.value = str(chosen) if self.root is None else chosen.resolve().relative_to(Path(self.root).resolve()).as_posix()
                field.focus()

        self.app.push_screen(FolderPicker(path, title=self.title_text, mode=self.mode, file_extensions=self.file_extensions,
                                          places=self.places, root=self.root, filename=filename), selected)
