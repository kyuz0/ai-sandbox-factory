"""Exercise the shared picker against the pinned filesystem widget."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Button, Input
from pi_sandbox_factory.file_browser import FolderPicker, matching_paths
from pi_sandbox_factory.sandbox_tui import Cockpit
from pi_sandbox_factory.tui_widgets import Checkbox


class PickerApp(App):
    CSS = Cockpit.CSS


class FileBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('reports', 'archive', '.hidden'):
            (self.root / name).mkdir()
        for name in ('report.zip', 'REPORT.txt', 'archive/report.zip', '.hidden/report.zip', 'archive/.report.zip'):
            (self.root / name).touch()

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def paths(self, picker):
        nav = picker.navigation
        return [nav.get_option_at_index(i).location for i in range(nav.option_count)
                if nav.get_option_at_index(i).location.name != '..']

    async def test_filter_keyboard_navigation_and_clear(self):
        app = PickerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = FolderPicker(self.root)
            await app.push_screen(picker)
            await self.settle(app, pilot)
            await pilot.press('r', 'e', 'p')
            self.assertEqual(app.focused.id, 'folder-filter')
            self.assertEqual(self.paths(picker), [self.root / 'reports'])
            await pilot.press('down', 'enter')
            await self.settle(app, pilot)
            self.assertEqual(picker.navigation.location, self.root / 'reports')
            self.assertEqual(picker.query_one('#folder-filter', Input).value, '')
            await pilot.press('backspace')
            await self.settle(app, pilot)
            await pilot.press('ctrl+f')
            picker.query_one('#folder-filter', Input).value = 'absent'
            await pilot.pause()
            self.assertEqual(self.paths(picker), [])
            self.assertIn('No matches', str(picker.query_one('#folder-name-column').render()))
            await pilot.click('#folder-clear')
            await pilot.pause()
            self.assertIn(self.root / 'reports', self.paths(picker))
            await pilot.press('ctrl+l')
            self.assertEqual(app.focused.id, 'folder-path')
            await pilot.press('escape')
            self.assertNotIn(picker, app.screen_stack)

    async def test_recursive_results_extensions_hidden_and_nested_save(self):
        app = PickerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = FolderPicker(self.root, mode='save', filename='untouched.zip', file_extensions={'.zip'}, root=self.root)
            chosen = []
            await app.push_screen(picker, chosen.append)
            await self.settle(app, pilot)
            self.assertEqual(app.focused.id, 'folder-filename')
            picker.query_one('#folder-filter', Input).value = 'REPORT'
            scope_label = str(picker.query_one('#folder-recursive', Checkbox).label)
            picker.query_one('#folder-recursive', Checkbox).value = True
            await self.settle(app, pilot)
            self.assertEqual(str(picker.query_one('#folder-recursive', Checkbox).label), scope_label)
            self.assertEqual(set(self.paths(picker)), {self.root / 'reports', self.root / 'report.zip', self.root / 'archive/report.zip'})
            self.assertEqual(picker.query_one('#folder-filename', Input).value, 'untouched.zip')
            nav = picker.navigation
            nested = self.root / 'archive/report.zip'
            nav.highlighted = next(i for i in range(nav.option_count) if nav.get_option_at_index(i).location == nested)
            nav.focus()
            await pilot.press('enter')
            await self.settle(app, pilot)
            self.assertEqual(nav.location, nested.parent)
            self.assertEqual(picker.query_one('#folder-filename', Input).value, 'report.zip')
            self.assertEqual(picker.query_one('#folder-filter', Input).value, '')
            picker.go_to(self.root)
            await self.settle(app, pilot)
            picker.query_one('#folder-filter', Input).value = 'report'
            picker.query_one('#folder-hidden', Checkbox).value = True
            await self.settle(app, pilot)
            self.assertIn(self.root / '.hidden/report.zip', self.paths(picker))
            self.assertIn(self.root / 'archive/.report.zip', self.paths(picker))
            self.assertNotIn(self.root / 'REPORT.txt', self.paths(picker))
            picker.query_one('#folder-recursive', Checkbox).value = False
            await self.settle(app, pilot)
            self.assertEqual(str(picker.query_one('#folder-recursive', Checkbox).label), scope_label)
            self.assertNotIn(nested, self.paths(picker))
            picker.go_to(nested.parent)
            await self.settle(app, pilot)
            await pilot.click('#folder-use')
            await pilot.pause()
            self.assertEqual(chosen, [str(nested)])

    async def test_filter_clears_stale_file_selection_and_ignores_old_results(self):
        app = PickerApp()
        async with app.run_test() as pilot:
            picker = FolderPicker(self.root, mode='file')
            await app.push_screen(picker)
            await self.settle(app, pilot)
            nav = picker.navigation
            nav.highlighted = next(i for i in range(nav.option_count) if nav.get_option_at_index(i).location == self.root / 'report.zip')
            await pilot.pause()
            self.assertFalse(picker.query_one('#folder-use', Button).disabled)
            picker.query_one('#folder-filter', Input).value = 'absent'
            await pilot.pause()
            self.assertTrue(picker.query_one('#folder-use', Button).disabled)
            generation = nav.generation
            picker.query_one('#folder-recursive', Checkbox).value = True
            await self.settle(app, pilot)
            nav.receive_entries(generation, [], True, False, ['stale failure'])
            self.assertFalse(picker.query_one('#folder-browse-error').display)
            self.assertEqual(self.paths(picker), [])

    async def test_save_geometry_and_errors_at_all_sizes(self):
        for size in ((80, 24), (120, 40), (220, 50)):
            with self.subTest(size=size):
                app = PickerApp()
                async with app.run_test(size=size) as pilot:
                    (self.root / 'Downloads').mkdir(exist_ok=True)
                    with patch('pi_sandbox_factory.file_browser.Path.home', return_value=self.root):
                        picker = FolderPicker(self.root, mode='save', filename='backup.zip', places={'Chat backups': self.root})
                    await app.push_screen(picker)
                    await self.settle(app, pilot)
                    picker.query_one('#folder-filename', Input).value = '../bad.zip'
                    await pilot.click('#folder-use')
                    await pilot.pause()
                    for control in ('#folder-use', '#folder-cancel', '#folder-recursive', '#folder-hidden', '#folder-clear', '#folder-place-2'):
                        widget = picker.query_one(control)
                        self.assertEqual(widget.region.height, 1)
                        self.assertTrue(picker.query_one('#folder-box').content_region.contains_region(widget.region), control)
                        self.assertIs(picker.get_widget_at(widget.region.x, widget.region.y)[0], widget, control)
                    self.assertGreater(picker.navigation.region.height, 0)
                    for group in ('folder-location-group', 'folder-filter-group'):
                        label = picker.query_one('#' + group + ' Label')
                        field = picker.query_one('#' + group + ' Input')
                        self.assertEqual(label.region.x, field.region.x)
                        self.assertEqual(field.region.y - label.region.bottom, 1)

    async def test_root_and_scan_errors_remain_navigable(self):
        app = PickerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = FolderPicker(self.root.parent, root=self.root, mode='save', filename='backup.zip')
            await app.push_screen(picker)
            await self.settle(app, pilot)
            self.assertEqual(picker.navigation.location, self.root)
            self.assertIsNotNone(picker.navigation.query('*'))
            nav = picker.navigation
            nav.receive_entries(nav.generation, [], False, False,
                                ['A very long unreadable path: ' + 'directory/' * 30] * 20)
            await pilot.pause()
            self.assertTrue(picker.query_one('#folder-errors').display)
            self.assertGreater(nav.region.height, 0)
            use = picker.query_one('#folder-use')
            self.assertIs(picker.get_widget_at(use.region.x, use.region.y)[0], use)
            await pilot.click('#folder-errors')
            await pilot.pause()
            self.assertEqual(len(app.screen.errors), 20)
            await pilot.press('escape')
            nav._load()
            await self.settle(app, pilot)
            self.assertFalse(picker.query_one('#folder-errors').display)
            await pilot.press('backspace')
            await self.settle(app, pilot)
            self.assertEqual(nav.location, self.root)
            alias = self.root / 'workspace-link'
            alias.symlink_to(self.root, target_is_directory=True)
            self.assertEqual(FolderPicker(alias, root=self.root).start, self.root)

    async def test_large_search_cancel_and_recovery(self):
        # Exercise ten times the initial 100-file listing through the actual UI.
        for index in range(100):
            (self.root / f'match-{index:04}.zip').touch()
        app = PickerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = FolderPicker(self.root, mode='file')
            await app.push_screen(picker)
            await self.settle(app, pilot)
            self.assertEqual(sum(p.name.startswith('match-') for p in self.paths(picker)), 100)
            for index in range(100, 1000):
                (self.root / 'archive' / f'match-{index:04}.zip').touch()
            picker.query_one('#folder-filter', Input).value = 'match-'
            picker.query_one('#folder-recursive', Checkbox).value = True
            await self.settle(app, pilot)
            self.assertEqual(len(self.paths(picker)), 1000)
            picker.query_one('#folder-filter', Input).value = 'no-result'
            await pilot.press('escape')
            await self.settle(app, pilot)
            self.assertNotIn(picker, app.screen_stack)
            await app.push_screen(FolderPicker(self.root))
            await self.settle(app, pilot)
            self.assertIn(self.root / 'archive', self.paths(app.screen))

    async def test_dismissal_cancels_an_active_scan(self):
        started, stopped = threading.Event(), threading.Event()
        def scanning(*args, cancelled, **kwargs):
            started.set()
            while not cancelled():
                stopped.wait(0.01)
            stopped.set()
            return
            yield  # generator protocol, with no result after cancellation
        app = PickerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            picker = FolderPicker(self.root, mode='file')
            await app.push_screen(picker)
            await self.settle(app, pilot)
            with patch('pi_sandbox_factory.file_browser.matching_paths', side_effect=scanning):
                picker.query_one('#folder-filter', Input).value = 'report'
                picker.query_one('#folder-recursive', Checkbox).value = True
                await pilot.pause()
                self.assertTrue(started.is_set())
                await pilot.press('escape')
                await self.settle(app, pilot)
                self.assertTrue(stopped.is_set())
                self.assertNotIn(picker, app.screen_stack)

    def scan(self, **kwargs):
        errors = []
        values = list(matching_paths(self.root, 'report', recursive=True, hidden=False, files=True,
                     allowed=lambda p: p.suffix == '.zip', root=self.root,
                     cancelled=lambda: False, errors=errors, **kwargs))
        return values, errors

    def test_symlinks_do_not_escape_or_loop_and_cancellation_stops_walk(self):
        (self.root / 'archive/loop').symlink_to(self.root, target_is_directory=True)
        (self.root / 'outside-report').symlink_to(self.root.parent, target_is_directory=True)
        values, errors = self.scan()
        self.assertIn(self.root / 'archive/report.zip', values)
        self.assertNotIn(self.root / 'outside-report', values)
        self.assertFalse(errors)
        errors = []
        self.assertEqual(list(matching_paths(self.root, '', recursive=True, hidden=True, files=True,
            allowed=lambda _: True, root=None, cancelled=lambda: True, errors=errors)), [])

    def test_unreadable_directory_is_reported(self):
        real = __import__('os').scandir
        def read(path):
            if path == self.root / 'archive':
                raise PermissionError('access denied')
            return real(path)
        with patch('pi_sandbox_factory.file_browser.os.scandir', side_effect=read):
            values, errors = self.scan()
        self.assertIn(self.root / 'report.zip', values)
        self.assertEqual(len(errors), 1)
        self.assertIn('access denied', errors[0])
