"""Shared host file and folder browser for Factory forms."""

import os
from pathlib import Path
import subprocess

from rich.text import Text
from textual import events, on, work
from textual.message import Message
from textual.worker import get_current_worker
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from textual_fspicker.parts import DirectoryNavigation
from textual_fspicker.parts.directory_navigation import DirectoryEntry
from textual_fspicker.icons import Icons
from textual_fspicker.path_filters import Filter
from textual_fspicker.safe_tests import is_dir

from . import host_platform
from .tui_widgets import Checkbox, Feedback


Icons.set_picker(lambda path: Text('▸' if is_dir(path) else '·'))


class SearchEntry(DirectoryEntry):
    """Keep an absolute result identity while displaying its relative path."""

    def __init__(self, location, styles, base):
        self.base = base
        super().__init__(location, styles)

    def _name(self, location):
        return Text.assemble(str(location.relative_to(self.base)), ' ',
                             self.LINK_ICON if location.is_symlink() else '')


def matching_paths(base, query, *, recursive, hidden, files, allowed, root, cancelled, errors):
    """Walk only the requested scope; directory symlinks never expand it."""
    pending = [base]
    while pending:
        directory = pending.pop()
        if cancelled():
            return
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancelled():
                        return
                    path = Path(entry.path)
                    if not hidden and path.name.startswith('.'):
                        continue
                    try:
                        if root and not path.resolve().is_relative_to(root):
                            continue
                        directory_entry = entry.is_dir()
                        if recursive and directory_entry and not entry.is_symlink():
                            pending.append(path)
                        if (directory_entry or (files and entry.is_file() and allowed(path))) and query in path.name.casefold():
                            yield path
                    except (OSError, RuntimeError) as error:
                        errors.append(f'{path}: {error}')
        except OSError as error:
            errors.append(f'{directory}: {error}')


class FilteredNavigation(DirectoryNavigation):
    """Small adapter for the pinned textual-fspicker list and entry renderer."""

    class Updated(Message):
        def __init__(self, navigation, *, searching=False, errors=()):
            super().__init__()
            self.navigation, self.searching, self.errors = navigation, searching, errors

    def __init__(self, location, *, root=None):
        self.filter_text = ''
        self.recursive = False
        self.root = root
        self.generation = 0
        self.errors = []
        super().__init__(location)

    @property
    def is_root(self):
        return super().is_root or self.location == self.root

    def hide(self, path):
        return (super().hide(path) or self.filter_text.casefold() not in path.name.casefold()
                or bool(self.root and not path.resolve().is_relative_to(self.root)))

    def refilter(self, query, recursive):
        old_recursive = self.recursive and bool(self.filter_text)
        self.filter_text, self.recursive = query, recursive
        if old_recursive or (recursive and query):
            self._load()
        else:
            self._repopulate_display()

    def _repopulate_display(self):
        previous = self.get_option_at_index(self.highlighted).location if self.highlighted is not None else None
        super()._repopulate_display()
        self.highlighted = next((i for i in range(self.option_count)
                                 if self.get_option_at_index(i).location == previous), None)
        self.post_message(self.Updated(self))

    def on_mount(self):
        super().on_mount()
        self._load()

    def _load(self):
        self.generation += 1
        if self.is_mounted:
            self.errors = []
            self._entries = []
            self._repopulate_display()
        self.load_entries(self.generation, self.location, self.filter_text.casefold(),
                          self.recursive and bool(self.filter_text), self.show_hidden,
                          self.show_files, self.file_filter, self._styles)

    @work(exclusive=True, thread=True, group='picker-listing')
    def load_entries(self, generation, base, query, recursive, hidden, files, allowed, styles):
        worker = get_current_worker()
        errors, batch = [], []
        cancelled = lambda: worker.is_cancelled or not self.is_mounted
        self.app.call_from_thread(self.receive_entries, generation, [], True, recursive, ())
        for path in matching_paths(base, query if recursive else '', recursive=recursive,
                                   hidden=hidden if recursive else True, files=files,
                                   allowed=allowed or (lambda _: True), root=self.root,
                                   cancelled=cancelled, errors=errors):
            if cancelled():
                return
            try:
                batch.append(SearchEntry(path, styles, base) if recursive else DirectoryEntry(path, styles))
            except OSError as error:
                errors.append(f'{path}: {error}')
            # A viewport-sized batch keeps the UI responsive and provides backpressure.
            if recursive and len(batch) >= max(1, self.size.height):
                self.app.call_from_thread(self.receive_entries, generation, batch, False, recursive, ())
                batch = []
        if not cancelled():
            self.app.call_from_thread(self.receive_entries, generation, batch, False, False, errors)

    def receive_entries(self, generation, entries, reset, searching, errors):
        if generation != self.generation or not self.is_mounted:
            return
        if reset:
            self.errors = []
            self._entries = []
            self._repopulate_display()
        self._entries.extend(entries)
        if searching:
            # Append streamed hits once; re-sorting the growing list per batch is quadratic.
            self.add_options(entry for entry in entries if not self.hide(entry.location))
        else:
            self.errors = list(errors)
            self._repopulate_display()
        self.post_message(self.Updated(self, searching=searching, errors=self.errors))

    def _watch__location(self):
        self.filter_text = ''
        super()._watch__location()

    def action_navigate_up(self):
        if not self.is_root:
            super().action_navigate_up()

    def on_unmount(self):
        self.generation += 1
        self.workers.cancel_group(self, 'picker-listing')


class PickerErrors(ModalScreen):
    BINDINGS = [('escape', 'close', 'Close')]

    def __init__(self, errors):
        super().__init__()
        self.errors = errors

    def compose(self):
        with Vertical(id='picker-errors-box'):
            yield Label('Folders or files that could not be read', classes='title')
            with VerticalScroll():
                yield Static('\n\n'.join(self.errors), markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('Close', id='picker-errors-close', variant='primary')

    def on_mount(self):
        self.query_one(Button).focus()

    def action_close(self):
        self.dismiss()

    @on(Button.Pressed, '#picker-errors-close')
    def close_pressed(self):
        self.action_close()


class FolderPicker(ModalScreen[str | None]):
    BINDINGS = [('escape', 'cancel', 'Cancel'), ('ctrl+l', 'location', 'Go to folder'),
                ('ctrl+f', 'filter', 'Name filter')]

    def __init__(self, start, *, title=None, mode='folder', file_extensions=None,
                 places=None, root=None, filename=''):
        super().__init__()
        self.title_text = title or {'folder': 'Choose workspace folder', 'file': 'Choose a file',
                                    'save': 'Choose where to save the file'}[mode]
        self.mode = mode
        self.file_extensions = file_extensions
        self.file = None
        self.filename = filename
        self.root = Path(root).resolve() if root else None
        home = Path.home()
        self.start_error = ''
        try:
            path = host_platform.local_path(start) if start else home
            self.start = path if path.is_dir() else path.parent if path.parent.is_dir() else self.root or home
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            self.start = self.root or home
            self.start_error = str(error)
        if self.root:
            self.start = self.start.resolve()
            if not self.start.is_relative_to(self.root):
                self.start = self.root
        self.places = {'Workspace': self.root} if self.root else {'Home': home, 'Downloads': home / 'Downloads'}
        self.places.update(places or {})
        self.places = {label: path for label, path in self.places.items() if path.is_dir()}

    def compose(self):
        with Vertical(id='folder-box'):
            yield Label(self.title_text, classes='title', markup=False)
            with Horizontal(id='folder-inputs'):
                with Vertical(id='folder-location-group'):
                    yield Label('Folder', classes='folder-input-label')
                    with Horizontal(classes='path-row'):
                        yield Input(str(self.start.absolute()), id='folder-path', select_on_focus=False)
                        yield Button('Up', id='folder-up')
                with Vertical(id='folder-filter-group'):
                    with Horizontal(id='folder-filter-heading'):
                        yield Label('Name filter', classes='folder-input-label')
                        yield Checkbox('Search subfolders', id='folder-recursive')
                    with Horizontal(classes='path-row'):
                        yield Input(id='folder-filter', select_on_focus=False)
                        yield Button('Clear', id='folder-clear')
            with Horizontal(id='folder-places'):
                for index, label in enumerate(self.places):
                    yield Button(label, id=f'folder-place-{index}')
                yield Static('', classes='action-spacer')
                yield Checkbox('Show hidden items', id='folder-hidden')
            with Horizontal(id='folder-columns'):
                yield Static('Name', id='folder-name-column')
                yield Static('Bytes', id='folder-size-column')
                yield Static('Modified', id='folder-modified-column')
            yield FilteredNavigation(self.start, root=self.root)
            if self.mode == 'save':
                yield Label('File name', classes='folder-input-label', id='folder-filename-label')
                yield Input(self.filename, id='folder-filename')
            yield Feedback(id='folder-browse-error', markup=False)
            with Horizontal(classes='dialog-actions'):
                yield Button('View errors', id='folder-errors')
                yield Static('', classes='action-spacer')
                yield Button('Cancel', id='folder-cancel')
                yield Button('Use this folder' if self.mode == 'folder' else 'Use this file',
                             id='folder-use', variant='primary', disabled=self.mode == 'file')

    @property
    def navigation(self):
        return self.query_one(FilteredNavigation)

    def on_mount(self):
        navigation = self.navigation
        navigation.show_files = self.mode != 'folder'
        if self.file_extensions:
            navigation.file_filter = Filter('Supported files', self.allowed_file)
        if self.mode == 'save':
            self.query_one('#folder-filename', Input).focus()
        else:
            navigation.focus()
        if self.start_error:
            self.query_one('#folder-browse-error', Feedback).update('Error · ' + self.start_error)

    def action_cancel(self):
        self.dismiss(None)

    def action_location(self):
        self.query_one('#folder-path', Input).focus()

    def action_filter(self):
        self.query_one('#folder-filter', Input).focus()

    def on_key(self, event: events.Key):
        if self.focused is self.navigation and event.is_printable:
            field = self.query_one('#folder-filter', Input)
            field.value += event.character
            field.cursor_position = len(field.value)
            field.focus()
            event.stop()
            event.prevent_default()
        elif self.focused is self.query_one('#folder-filter', Input) and event.key == 'down':
            self.navigation.focus()
            if self.navigation.highlighted is None:
                self.navigation.highlighted = next((i for i in range(self.navigation.option_count)
                    if self.navigation.get_option_at_index(i).location.name != '..'), None)
            event.stop()
            event.prevent_default()

    @on(Input.Changed, '#folder-filter')
    @on(Checkbox.Changed, '#folder-recursive')
    def filter_changed(self):
        self.update_file()
        self.navigation.refilter(self.query_one('#folder-filter', Input).value,
                                 self.query_one('#folder-recursive', Checkbox).value)

    @on(Input.Submitted, '#folder-filter')
    def filter_submitted(self):
        self.navigation.focus()
        self.navigation.highlighted = next((i for i in range(self.navigation.option_count)
            if self.navigation.get_option_at_index(i).location.name != '..'), None)

    @on(FilteredNavigation.Updated)
    def listing_updated(self, event):
        nav = event.navigation
        count = nav.option_count - int(not nav.is_root)
        state = 'Searching…' if event.searching else 'No matches' if nav.filter_text and not count else ''
        self.query_one('#folder-name-column', Static).update('Name' + (f' · {state}' if state else ''))
        errors = self.query_one('#folder-errors', Button)
        errors.display = bool(nav.errors)
        errors.label = f'View errors ({len(nav.errors)})'
        if nav.errors and not count and not event.searching:
            self.query_one('#folder-name-column', Static).update('Name')
        if nav.highlighted is None:
            self.update_file()

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
        if self.root and not location.is_relative_to(self.root):
            self.query_one('#folder-browse-error', Feedback).update('Error · Choose a folder inside the workspace.')
            return
        self.start_error = ''
        self.query_one('#folder-browse-error', Feedback).update('')
        self.navigation.location = location
        self.navigation.focus()

    def update_file(self, path=None):
        self.file = path if path and self.allowed_file(path) and path.is_file() else None
        if self.mode == 'file':
            self.query_one('#folder-use', Button).disabled = self.file is None

    @on(DirectoryNavigation.Changed)
    def location_changed(self, event):
        self.query_one('#folder-filter', Input).value = ''
        event.navigation.filter_text = ''
        self.query_one('#folder-path', Input).value = str(event.navigation.location)
        self.query_one('#folder-up', Button).disabled = event.navigation.is_root
        if event.navigation.location != self.start:
            self.start_error = ''
        self.query_one('#folder-browse-error', Feedback).update('Error · ' + self.start_error if self.start_error else '')
        self.update_file()

    @on(DirectoryNavigation.Highlighted)
    def highlighted(self, event):
        nav = self.navigation
        if nav.highlighted is not None and nav.get_option_at_index(nav.highlighted).location == event.path:
            self.update_file(event.path)

    @on(DirectoryNavigation.Selected)
    def selected(self, event):
        if self.allowed_file(event.path) and event.path.is_file():
            if self.mode == 'save':
                self.go_to(event.path.parent)
                self.query_one('#folder-filename', Input).value = event.path.name
            else:
                self.choose(event.path)

    @on(DirectoryNavigation.PermissionError)
    def permission_error(self):
        self.query_one('#folder-browse-error', Feedback).update('Error · Cannot read that folder.')

    @on(Input.Submitted, '#folder-path')
    def typed_path(self, event):
        try:
            value = event.value.strip()
            path = self.navigation.location / (Path(value) if self.root else host_platform.local_path(value))
            if path.is_dir():
                self.go_to(path)
                return
            if self.allowed_file(path) and path.is_file():
                self.choose(path)
                return
        except ValueError as error:
            self.query_one('#folder-browse-error', Feedback).update('Error · ' + str(error))
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
        if self.navigation.recursive and self.navigation.filter_text:
            self.navigation._load()

    @on(Button.Pressed)
    def pressed(self, event):
        action = event.button.id
        if action == 'folder-cancel':
            self.action_cancel()
        elif action == 'folder-errors':
            self.app.push_screen(PickerErrors(self.navigation.errors))
        elif action == 'folder-clear':
            self.query_one('#folder-filter', Input).value = ''
            self.action_filter()
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
        error_message = ''
        try:
            path = (Path(value) if self.root else host_platform.local_path(value)) if value and '://' not in value else self.initial
            path = Path(self.root) / (path or '.') if self.root else path
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            error_message = str(error)
            path = self.initial or self.root
        path = Path(path) if path else None
        filename = path.name if self.mode == 'save' and path and not path.is_dir() else ''

        def selected(result):
            if result:
                chosen = Path(result)
                field.value = str(chosen) if self.root is None else chosen.resolve().relative_to(Path(self.root).resolve()).as_posix()
                field.focus()

        picker = FolderPicker(path, title=self.title_text, mode=self.mode, file_extensions=self.file_extensions,
                              places=self.places, root=self.root, filename=filename)
        picker.start_error = error_message or picker.start_error
        self.app.push_screen(picker, selected)
