"""Installed-package actions and installation navigation."""
import unittest
from unittest.mock import patch

from textual.widgets import Button, Collapsible, Input, Static, TabbedContent, TextArea, Tabs
from ai_sandbox_factory import registry
from ai_sandbox_factory.sandbox_tui import Cockpit, Confirm, InstallPackage, PackageRow, PackagesScreen, ProfileDetails
from ai_sandbox_factory.sandbox_jobs_ui import JobDetails
from test_tui import FakeService
from tui_geometry import check_input_layout
import test_tui


class PackageUITests(unittest.TestCase):
    setUpClass = classmethod(test_tui.TUITests.setUpClass.__func__)
    tearDownClass = classmethod(test_tui.TUITests.tearDownClass.__func__)
    _callTestMethod = test_tui.TUITests._callTestMethod
    setUp = test_tui.TUITests.setUp
    tearDown = test_tui.TUITests.tearDown
    ready = test_tui.TUITests.ready

    async def test_installed_rows_actions_and_refresh(self):
        entries = {'company': {'description': 'Company models', 'source': 'https://example.com/providers.git', 'subdir': 'company', 'ref': 'main'}}
        service = FakeService([])
        original = service.run
        def run(args, log):
            original(args, log)
            if args[1] == 'remove': entries.pop(args[2])
        service.run = run
        app = Cockpit(self.root, service)
        with patch.object(registry, 'installed', side_effect=lambda kind: entries if kind == 'provider' else {}), patch.object(registry, 'choices', side_effect=lambda kind: {'base': 'Default Developer Sandbox'} if kind == 'profile' else {'local': 'Local models', **{k: v['description'] for k, v in entries.items()}}):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                await pilot.click('#packages'); await pilot.pause()
                listing = app.screen
                self.assertEqual(len(listing.query(PackageRow)), 1)
                self.assertEqual(str(listing.query(PackageRow).first().query_one(Button).label), 'Details…')
                listing.query_one('#package-kind', TabbedContent).active = 'provider'
                await pilot.pause()
                row = list(listing.query('#provider-packages PackageRow'))[-1]
                update, remove = list(row.query(Button))
                update.focus(); update.scroll_visible(animate=False); await pilot.pause()
                await pilot.press('enter'); await self.ready(app, pilot)
                self.assertEqual(service.calls[-1], ['provider', 'update', 'company'])
                self.assertIsInstance(app.screen, JobDetails)
                await pilot.press('escape'); await pilot.pause()
                self.assertIs(app.screen, listing)
                self.assertEqual(listing.query_one('#package-kind', TabbedContent).active, 'provider')
                for confirm in (False, True):
                    row = list(listing.query('#provider-packages PackageRow'))[-1]
                    remove = list(row.query(Button))[-1]
                    remove.focus(); remove.scroll_visible(animate=False); await pilot.pause()
                    await pilot.press('enter'); await pilot.pause()
                    self.assertIsInstance(app.screen, Confirm)
                    if not confirm:
                        self.assertEqual(app.screen.query_one('#cancel', Button).variant, 'default')
                        await pilot.press('enter'); await pilot.pause()
                        self.assertEqual(len(service.calls), 1)
                    else:
                        await pilot.click('#yes'); await self.ready(app, pilot)
                        self.assertEqual(service.calls[-1], ['provider', 'remove', 'company'])
                        await pilot.press('escape'); await pilot.pause()
                self.assertEqual(len(listing.query('#provider-packages PackageRow')), 1)
                await pilot.click('#package-install'); await pilot.pause()
                self.assertEqual(app.screen.kind, 'provider')
                await pilot.press('escape'); await pilot.pause()
                self.assertIs(app.screen, listing)

    async def test_library_tabs_explain_each_kind_and_show_read_only_profile_prompt(self):
        app = Cockpit(self.root, FakeService([]))
        with patch.object(registry, 'installed', return_value={}):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                await pilot.click('#packages')
                await pilot.pause()
                library = app.screen
                tabs = library.query_one(TabbedContent)
                self.assertEqual(tabs.active, 'profile')
                self.assertIn('Dockerfile', str(library.query_one('#profile .section-intro', Static).render()))
                library.query_one(Tabs).focus()
                await pilot.press('right')
                self.assertEqual(tabs.active, 'provider')
                self.assertIn('connect Pi to models', str(library.query_one('#provider .section-intro', Static).render()))
                self.assertEqual(str(library.query_one('#package-install', Button).label), 'Install provider…')
                await pilot.press('right')
                self.assertEqual(tabs.active, 'skills')
                self.assertIn('reusable instructions', str(library.query_one('#skills .section-intro', Static).render()))
                self.assertEqual(str(library.query_one('#package-install', Button).label), 'Add skills…')
                await pilot.press('left', 'left')
                self.assertEqual(tabs.active, 'profile')
                profiles = {row.package_name: row for row in library.query('#profile-packages PackageRow')}
                self.assertTrue({'base', 'deep-research', 'local-knowledge'}.issubset(profiles))
                details = profiles['base'].query_one(Button)
                details.focus()
                await pilot.press('enter')
                self.assertIsInstance(app.screen, ProfileDetails)
                prompt = str(app.screen.query_one('#profile-system-prompt', Static).render())
                self.assertIn('general software development and agent-assisted workflows', prompt)
                self.assertIn('Follow the user’s request', prompt)
                self.assertEqual(len(app.screen.query(Input)), 0)
                self.assertEqual(len(app.screen.query(TextArea)), 0)
                await pilot.press('escape')
                self.assertIs(app.screen, library)
                self.assertEqual(tabs.active, 'profile')

    async def test_install_validation_and_optional_revision(self):
        service = FakeService([]); app = Cockpit(self.root, service)
        with patch.object(registry, 'installed', return_value={}):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(app, pilot)
                await pilot.click('#packages'); await pilot.pause()
                await pilot.click('#package-install'); await pilot.pause()
                form = app.screen
                # Textual ignores mouse clicks while its 200 ms press effect is
                # active. Exercise validation without racing that animation.
                form.query_one('#package-add', Button).active_effect_duration = 0
                await pilot.click('#package-add'); await pilot.pause()
                self.assertIs(app.screen, form)
                self.assertIn('package name', str(form.query_one('#package-error', Static).render()))
                self.assertEqual(service.calls, [])
                form.query_one('#package-name', Input).value = 'reporting'
                form.query_one('#package-source', Input).value = str(self.root)
                form.query_one(Collapsible).collapsed = False
                form.query_one('#package-subdir', Input).value = '../outside'
                await pilot.click('#package-add'); await pilot.pause()
                error = form.query_one('#package-error', Static)
                self.assertIn('inside', str(error.render()))
                field = form.query_one('#package-subdir', Input)
                self.assertIs(app.focused, field)
                self.assertIs(form.get_widget_at(field.region.x, field.region.y)[0], field)
                self.assertLessEqual(error.region.bottom, form.query_one('.wizard-actions').region.y)
                form.query_one('#package-subdir', Input).value = 'profiles/reporting'
                form.query_one('#package-ref', Input).value = 'v1'
                await pilot.click('#package-add'); await self.ready(app, pilot)
                self.assertTrue(service.calls, str(form.query_one('#package-error', Static).render()) if app.screen is form else str(app.screen))
                self.assertEqual(service.calls[-1], ['profile', 'add', 'reporting', str(self.root), '--ref', 'v1', '--subdir', 'profiles/reporting'])
                await pilot.press('escape'); await pilot.pause()
                self.assertIsInstance(app.screen, PackagesScreen)

    async def test_package_pages_have_padded_reachable_actions(self):
        record = {'description': 'Reporting tools for a development team', 'source': 'https://example.com/' + 'long-repository-name/' * 8}
        for size in ((80, 24), (120, 40), (220, 50)):
            app = Cockpit(self.root, FakeService([]))
            with patch.object(registry, 'installed', return_value={'reporting': record}), patch.object(registry, 'choices', return_value={'base': 'Default Developer Sandbox', 'reporting': record['description']}):
                async with app.run_test(size=size) as pilot:
                    await self.ready(app, pilot)
                    await pilot.click('#packages'); await pilot.pause()
                    tabs = app.screen.query_one(TabbedContent)
                    for tab in ('provider', 'skills', 'profile'):
                        tabs.active = tab
                        await pilot.pause()
                        primary = app.screen.query_one('#package-install', Button)
                        self.assertGreaterEqual(primary.content_region.width, len(str(primary.label)))
                    for primary_id in ('package-install', 'package-add'):
                        primary = app.screen.query_one('#'+primary_id, Button)
                        bar = app.screen.query_one('.wizard-actions')
                        self.assertEqual(primary.region.right, bar.content_region.right)
                        self.assertGreater(primary.region.y, bar.region.y)
                        self.assertLess(primary.region.bottom, app.screen.query_one('Footer').region.y)
                        self.assertEqual(primary.region.height, 1)
                        self.assertIs(app.screen.get_widget_at(primary.region.x, primary.region.y)[0], primary)
                        if primary_id == 'package-install':
                            await pilot.click('#package-install'); await pilot.pause()
                    app.screen.query_one(Collapsible).collapsed = False
                    await pilot.pause()  # Lay out the expanded fields before scrolling.
                    self.assertCountEqual(check_input_layout(app.screen), ['package-source', 'package-name',
                                                                          'package-subdir', 'package-ref'])
                    field = app.screen.query_one('#package-ref', Input)
                    field.focus(); await pilot.pause()
                    await pilot.wait_for_scheduled_animations()
                    field.scroll_visible(animate=False); await pilot.pause()
                    self.assertLessEqual(field.region.bottom, app.screen.query_one('.wizard-actions').region.y)
                    await pilot.press('escape'); await pilot.pause()
                    self.assertIsInstance(app.screen, PackagesScreen)
