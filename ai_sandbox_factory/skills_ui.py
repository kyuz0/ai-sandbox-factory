"""Skill library import and per-sandbox selection controls."""
from pathlib import Path
import tempfile

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.message import Message
from textual.widgets import Button, Footer, Header, Input, Label, Static

from . import skill_library
from .file_browser import PathField
from .tui_widgets import Checkbox, Feedback


def saved_skills_summary(count):
    return f'{count} saved'


class SkillSelection(Vertical):
    class Changed(Message):
        pass

    DEFAULT_CSS = '''
    SkillSelection { height: auto; }
    SkillSelection #skill-choices { height: auto; }
    SkillSelection #manage-skills { margin: 1 0; }
    '''

    def __init__(self, draft):
        super().__init__()
        self.existing = draft.get('extra_skills', [])
        self.chosen = set(draft.get('library_skills', []))
        self.removed = set(draft.get('remove_skills', []))
        self.rows = {}

    def compose(self):
        yield Static('Select any number of skills. Each sandbox keeps its own validated copy.', classes='setting-help section-intro')
        yield Vertical(id='skill-choices')
        yield Button('Manage skill library…', id='manage-skills')
        yield Feedback('', id='skill-selection-error', markup=False)

    async def on_mount(self):
        await self.refresh_choices()

    async def refresh_choices(self, _=None):
        listing = self.query_one('#skill-choices', Vertical)
        await listing.remove_children()
        if not listing.is_attached:
            return
        self.rows = skill_library.entries()
        choices = []
        for index, row in enumerate(self.existing):
            newer = self.rows.get(row.get('library_id'), {}).get('digest', row['digest']) != row['digest']
            choices.append(Vertical(
                Checkbox(row['name'] + (' · saved copy; update available' if newer else ' · saved sandbox copy'),
                         value=row['name'] not in self.removed, id=f'saved-skill-{index}'),
                Static(row.get('description', ''), markup=False, classes='setting-help checkbox-help'),
                Feedback('', id=f'saved-skill-error-{index}', markup=False), classes='setting'))
        for key, row in self.rows.items():
            if any(old.get('library_id') == key and old['digest'] == row['digest'] for old in self.existing):
                self.chosen.discard(key)
                continue
            choices.append(Vertical(
                Checkbox(row['name'] + ' · library version', value=key in self.chosen, id='library-skill-' + key),
                Static(row['description'], markup=False, classes='setting-help checkbox-help'), classes='setting'))
        if not choices:
            choices.append(Static('No skills in your library yet. Add a source with Manage skill library….', markup=False))
        missing = self.chosen - self.rows.keys()
        self.query_one('#skill-selection-error', Feedback).update(
            'Error: a selected skill was removed from the library. Clear the missing selection or add its source again.' if missing else '')
        choices.extend(Checkbox('Missing library skill · ' + key, value=True, id='library-skill-' + key)
                       for key in sorted(missing))
        await listing.mount(*choices)
        self.post_message(self.Changed())

    def summary(self):
        saved = [row for row in self.existing if row['name'] not in self.removed]
        updates = sum(self.rows.get(row.get('library_id'), {}).get('digest', row['digest']) != row['digest'] for row in saved)
        parts = [f'{len(self.chosen)} from library', saved_skills_summary(len(saved))]
        if updates:
            parts.append(f'{updates} update' + ('s' if updates != 1 else '') + ' available')
        return ' · '.join(parts)

    @on(Checkbox.Changed)
    def changed(self, event):
        for feedback in self.query('Feedback'):
            feedback.update('')
        identifier = event.checkbox.id or ''
        if identifier.startswith('library-skill-'):
            key = identifier.removeprefix('library-skill-')
            self.chosen.add(key) if event.value else self.chosen.discard(key)
        elif identifier.startswith('saved-skill-'):
            name = self.existing[int(identifier.removeprefix('saved-skill-'))]['name']
            self.removed.discard(name) if event.value else self.removed.add(name)
        if not self.chosen - self.rows.keys():
            self.query_one('#skill-selection-error', Feedback).update('')
        self.post_message(self.Changed())

    @on(Button.Pressed, '#manage-skills')
    def manage(self, event):
        event.stop()
        from .sandbox_tui import PackagesScreen

        self.app.push_screen(PackagesScreen(initial='skills'), self.refresh_choices)

    def collect(self):
        for feedback in self.query('Feedback'):
            feedback.update('')
        try:
            rows = skill_library.selections(sorted(self.chosen))
            retained = {row['name'] for row in self.existing if row['name'] not in self.removed}
            conflicts = retained.intersection(row['name'] for row in rows)
            if conflicts:
                index = next(index for index, row in enumerate(self.existing) if row['name'] in conflicts)
                message = self.query_one(f'#saved-skill-error-{index}', Feedback)
                message.update('Error: deselect this saved copy to use the library version.')
                self.query_one(f'#saved-skill-{index}', Checkbox).focus()
                message.scroll_visible(animate=False)
                raise ValueError('Deselect the saved copy before selecting another version with the same name')
        except ValueError as error:
            if not any(message.display for message in self.query('Feedback')):
                self.query_one('#skill-selection-error', Feedback).update('Error: ' + str(error))
            raise
        return {'library_skills': sorted(self.chosen), 'remove_skills': sorted(self.removed)}


class SkillLibrary(Vertical):
    def compose(self):
        yield Static('Skills give coding agents reusable instructions for specific tasks. Import and validate a skill once, '
                     'then select it for any sandbox.', classes='section-intro')
        yield Vertical(id='library-list')
        yield Feedback('', id='library-error', markup=False)

    def add_skills(self):
        self.app.push_screen(ImportSkills())

    async def refresh_library(self, _=None):
        listing = self.query_one('#library-list', Vertical)
        await listing.remove_children()
        rows = skill_library.entries()
        if not rows:
            await listing.mount(Static('Your skill library is empty. Use Add skills… to import a Git repository or local folder.'))
        for key, row in sorted(rows.items(), key=lambda pair: pair[1]['name']):
            box = Vertical(classes='package-row')
            await listing.mount(box)
            folder = 'Source root' if row['folder'] == '.' else row['folder']
            await box.mount(Label(row['name'], classes='package-title', markup=False),
                            Static(row['description'], markup=False, classes='setting-help'),
                            Static(' · '.join((row['source'], folder, row['commit'][:12])),
                                   markup=False, classes='setting-help'))
            actions = Horizontal(classes='buttons package-actions')
            await box.mount(actions)
            await actions.mount(Button('Update…', id='update-' + key), Button('Remove', id='remove-' + key))

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        identifier = event.button.id or ''
        if identifier.startswith('update-'):
            entries = skill_library.entries()
            row = entries.get(identifier.removeprefix('update-'))
            if row is None:
                self.query_one('#library-error', Feedback).update('Error: this skill was removed in another window. Reopen the library.')
                return
            selected = {key for key, item in entries.items() if item['source'] == row['source']}
            self.app.push_screen(ImportSkills(row['source'], row.get('ref') or '', selected))
        elif identifier.startswith('remove-'):
            from .sandbox_tui import Confirm
            key = identifier.removeprefix('remove-')
            self.app.push_screen(Confirm('Remove this skill from the library? Saved sandboxes keep their copies.', 'Remove', confirm_by_default=False),
                                 lambda yes: self.remove_entry(key) if yes else None)

    @work(thread=True, exit_on_error=False)
    def remove_entry(self, key):
        try:
            skill_library.remove(key)
        except (ValueError, OSError) as error:
            self.app.call_from_thread(self.query_one('#library-error', Feedback).update, 'Error: ' + str(error))
        self.app.call_from_thread(self.refresh_library)


class ImportSkills(Screen):
    BINDINGS = [('escape', 'back', 'Cancel')]

    def __init__(self, source='', ref='', preselect=()):
        super().__init__()
        self.source, self.ref = source, ref
        self.preselect = set(preselect)
        self.staging = None
        self.rows = []
        self.candidates = []
        self.search_scroll_timer = None
        self.busy = False

    def compose(self):
        yield Header()
        with VerticalScroll(classes='form spaced-form-fields'):
            yield Label('Add skills to library', classes='title')
            with Vertical(classes='setting'):
                yield Label('Git repository, GitHub skill link or local folder')
                yield PathField('skill-source', self.source, title='Choose local skill folder', browse_id='skill-browse')
                yield Static('Validation checks format and files without running skill code. Only import sources you trust.', classes='setting-help')
            with Vertical(classes='setting'):
                yield Label('Tag, branch or commit · optional')
                yield Input(self.ref, id='skill-ref')
            with Vertical(classes='setting'):
                yield Button('Find and validate skills', id='skill-discover')
            yield Feedback('', id='skill-import-error', markup=False)
            yield Static('', id='skill-import-status', markup=False)
            with Vertical(classes='setting', id='skill-search-setting'):
                yield Label('Search skills')
                yield Input(placeholder='Name, description or folder', id='skill-search')
                yield Static('', id='skill-search-status', classes='setting-help')
            yield Vertical(id='skill-candidates')
        with Horizontal(classes='wizard-actions'):
            yield Button('Cancel', id='skill-import-cancel')
            yield Button('Add selected skills', id='skill-import-save', variant='primary', disabled=True)
        yield Footer()

    def on_mount(self):
        self.query_one('#skill-search-setting').display = False

    def set_busy(self, busy):
        self.busy = busy
        for identifier in ('skill-source', 'skill-ref', 'skill-browse', 'skill-discover', 'skill-import-cancel', 'skill-search'):
            self.query_one('#' + identifier).disabled = busy
        self.query_one('#skill-import-save', Button).disabled = busy or not any(not row.get('error') for row in self.rows)

    @on(Input.Changed, '#skill-source')
    @on(Input.Changed, '#skill-ref')
    async def source_changed(self):
        if self.is_mounted:
            self.rows = []
            self.candidates = []
            self.query_one('#skill-search', Input).value = ''
            self.query_one('#skill-search-setting').display = False
            self.query_one('#skill-import-save', Button).disabled = True
            await self.query_one('#skill-candidates', Vertical).remove_children()
            self.query_one('#skill-import-status', Static).display = True
            self.query_one('#skill-import-status', Static).update('Find and validate skills after changing the source.')

    @on(Input.Changed, '#skill-search')
    def search_changed(self):
        self.filter_candidates()
        if self.search_scroll_timer is not None:
            self.search_scroll_timer.stop()
        if self.rows:
            # Let Input's cursor scrolling and the filtered layout settle first.
            self.search_scroll_timer = self.set_timer(0.05, self.show_search_results)

    def show_search_results(self):
        search = self.query_one('#skill-search-setting')
        form = self.query_one(VerticalScroll)
        visible = next((box for _, _, box, _ in self.candidates if box.display), None)
        if visible is not None:
            visible.scroll_visible(animate=False, immediate=True)
        else:
            form.scroll_to(y=search.virtual_region.y, animate=False, immediate=True)

    @on(Checkbox.Changed)
    def filter_candidates(self):
        if not self.is_mounted:
            return
        terms = self.query_one('#skill-search', Input).value.casefold().split()
        shown = selected = 0
        for row, checkbox, box, searchable in self.candidates:
            matches = all(term in searchable for term in terms)
            box.display = matches
            shown += matches
            selected += checkbox.value and not row.get('error')
        status = f'{shown} of {len(self.rows)} skills · {selected} selected'
        if not shown and self.rows:
            status = f'No matching skills · {selected} selected'
        self.query_one('#skill-search-status', Static).update(status)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'skill-import-cancel':
            self.action_back()
        elif event.button.id == 'skill-discover':
            self.set_busy(True)
            self.query_one('#skill-import-error', Feedback).update('')
            self.query_one('#skill-import-status', Static).display = True
            self.query_one('#skill-import-status', Static).update('Fetching source and validating skills…')
            self.discover(self.query_one('#skill-source', Input).value.strip(), self.query_one('#skill-ref', Input).value.strip() or None)
        elif event.button.id == 'skill-import-save':
            selected = [row for row, checkbox, _, _ in self.candidates if not row.get('error') and checkbox.value]
            self.set_busy(True)
            self.install_selected(selected)

    @work(thread=True, exit_on_error=False)
    def install_selected(self, selected):
        try:
            skill_library.install(selected)
            self.app.call_from_thread(self.dismiss, None)
        except (ValueError, OSError) as error:
            self.app.call_from_thread(self.install_failed, str(error))

    def install_failed(self, error):
        self.query_one('#skill-import-error', Feedback).update('Error: ' + error)
        self.set_busy(False)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def discover(self, source, ref):
        try:
            if self.staging:
                self.staging.cleanup()
            self.staging = tempfile.TemporaryDirectory(prefix='factory-skill-import-')
            rows = skill_library.discover(source, Path(self.staging.name) / 'source', ref)
            self.app.call_from_thread(self.show_candidates, rows)
        except Exception as error:
            self.app.call_from_thread(self.show_failure, str(error))

    async def show_candidates(self, rows):
        self.rows = rows
        listing = self.query_one('#skill-candidates', Vertical)
        await listing.remove_children()
        boxes = []
        self.candidates = []
        for index, row in enumerate(rows):
            checkbox = Checkbox(Text(row['name'] + (' · invalid' if row.get('error') else '')), value=row['id'] in self.preselect and not row.get('error'), disabled=bool(row.get('error')), id=f'candidate-{index}')
            box = Vertical(
                checkbox,
                Static('Error: ' + row['error'] if row.get('error') else row['description'], markup=False, classes='setting-help checkbox-help'),
                Static('Folder: ' + row['folder'], markup=False, classes='setting-help checkbox-help'),
                classes='setting')
            boxes.append(box)
            searchable = ' '.join(row.get(key, '') for key in ('name', 'description', 'folder')).casefold()
            self.candidates.append((row, checkbox, box, searchable))
        await listing.mount(*boxes)
        valid = sum(not row.get('error') for row in rows)
        self.query_one('#skill-import-status', Static).display = True
        self.query_one('#skill-import-status', Static).update(f'{valid} valid ' + ('skill' if valid == 1 else 'skills') + f', {len(rows)-valid} invalid. Select the skills to add.')
        self.query_one('#skill-search-setting').display = bool(rows)
        self.filter_candidates()
        self.set_busy(False)

    async def show_failure(self, error):
        self.rows = []
        self.candidates = []
        self.query_one('#skill-search-setting').display = False
        await self.query_one('#skill-candidates', Vertical).remove_children()
        self.query_one('#skill-import-status', Static).update('')
        self.query_one('#skill-import-status', Static).display = False
        feedback = self.query_one('#skill-import-error', Feedback)
        feedback.update('Error: ' + error)
        feedback.scroll_visible(animate=False)
        self.set_busy(False)

    def action_back(self):
        if not self.busy:
            self.dismiss(None)

    def on_unmount(self):
        if self.staging:
            self.staging.cleanup()
