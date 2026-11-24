import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_sandbox_factory import folders, sandbox
from ai_sandbox_factory.sandbox_tui import Cockpit, FoldersScreen, NewSandbox, SettingsForm, DEFAULT_SETTINGS, copy_draft, preset
from ai_sandbox_factory.folders_ui import FolderFields, FolderEditor
from textual.widgets import Input, Button, Checkbox, Collapsible, Static
from tui_geometry import check_input_layout
from test_tui import FakeService


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = patch.object(sandbox.operation_locks, 'private_runtime_dir', return_value=self.root)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        (self.workspace / 'app').mkdir()
        self.extra = self.root / 'reference'
        self.extra.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = dict(workspace=str(self.workspace), working_directory='app', engine='podman', extra_folders=[])

    def validate(self, rows, **kwargs):
        return folders.validate(rows, self.workspace, self.state, **kwargs)

    def test_pi_cli_starts_discovers_and_launches_in_starting_folder(self):
        config=dict(self.config, name='example', local_model_ports='8000')
        state=self.state/'example';state.mkdir()
        (state/'config.json').write_text(json.dumps(config))
        argv=['pi-sandbox-factory','--state-dir',str(self.state),'pi','example','--','--continue']
        calls=[]
        with patch('sys.argv',argv), \
             patch.object(sandbox,'up',side_effect=lambda *a:calls.append('up')), \
             patch.object(sandbox,'initialize_models',side_effect=lambda *a:calls.append('models')), \
             patch('sys.stdin.isatty',return_value=True), \
             patch.object(sandbox,'run',side_effect=lambda args:calls.append(args)):
            sandbox.main()
        self.assertEqual(calls[:2],['up','models'])
        self.assertEqual(calls[2],['podman','exec','--workdir','/workspace/app','-it',
                                   sandbox.names(config)[0],'pi','--continue'])
        with patch('sys.argv',argv), patch.object(sandbox,'up',side_effect=ValueError('start failed')), \
             patch.object(sandbox,'run') as run:
            with self.assertRaisesRegex(ValueError,'start failed'):sandbox.main()
            run.assert_not_called()

    def test_starting_folder_resolution(self):
        self.assertEqual(folders.container_cwd(self.config), '/workspace/app')
        self.assertEqual(folders.working_directory(self.workspace), '.')
        (self.workspace / 'alias').symlink_to(self.workspace / 'app')
        self.assertEqual(folders.working_directory(self.workspace, 'alias'), 'app')
        (self.workspace / 'escape').symlink_to(self.extra)
        (self.workspace / 'file').touch()
        for value in ('..', '/etc', 'app/../../etc', 'escape', 'missing', 'file'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                folders.working_directory(self.workspace, value)

    def test_mount_paths_modes_and_aliases(self):
        rows = self.validate([dict(source=str(self.extra))])
        self.assertEqual(rows[0]['name'], 'reference')
        self.assertTrue(rows[0]['read_only'])
        for engine, flag in [('podman', 'bind-nonrecursive'), ('docker', 'bind-recursive=disabled')]:
            args = folders.mounts(dict(self.config, engine=engine, extra_folders=rows))
            self.assertEqual(args, ['--mount', f'type=bind,src={self.extra},dst=/mnt/reference,{flag},ro'])
        rows[0]['read_only'] = False
        self.assertTrue(folders.mounts(dict(self.config, extra_folders=rows))[1].endswith(',rw'))
        self.assertEqual(folders.suggested_name(self.extra, rows), 'reference-2')
        name = 'a' * 255
        self.assertEqual(self.validate([dict(source=str(self.extra), name=name)])[0]['name'], name)
        with self.assertRaisesRegex(ValueError, 'filename limit'):
            self.validate([dict(source=str(self.extra), name=name + 'a')])

    def test_sources_and_overlaps(self):
        child = self.extra / 'child'; child.mkdir()
        hidden = self.root / '.ssh'; hidden.mkdir()
        invalid = ['/', str(Path.home()), '/etc', '/tmp', '/var/tmp', str(self.workspace),
                   str(self.workspace / 'app'), str(self.state), str(self.root), str(self.root / 'missing')]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.validate([dict(source=source)])
        for name in ('../bad', 'data/path', 'with space', 'UPPER'):
            with self.assertRaises(ValueError):
                self.validate([dict(source=str(self.extra), name=name)])
        with self.assertRaises(ValueError):
            self.validate([dict(source=str(self.extra)), dict(source=str(child))])
        with self.assertRaises(ValueError):
            self.validate([dict(source=str(self.extra), read_only='false')])
        for name in ('comma,ro', 'line\nbreak', 'double"quote'):
            bad = self.root / name; bad.mkdir()
            with self.assertRaises(ValueError): self.validate([dict(source=str(bad))])
        (self.root / 'comma-alias').symlink_to(self.root / 'comma,ro')
        with self.assertRaises(ValueError): self.validate([dict(source=str(self.root / 'comma-alias'))])
        with patch.object(Path, 'home', return_value=self.root):
            with self.assertRaises(ValueError): self.validate([dict(source=str(hidden))])

    def test_hidden_data_folder_can_be_mounted_without_host_sign_ins(self):
        home = self.root / 'home'
        data = home / '.datasets' / 'manuals'
        data.mkdir(parents=True)
        sign_in = home / '.pi' / 'agent'
        sign_in.mkdir(parents=True)
        alias = home / '.aliases'
        alias.symlink_to(sign_in, target_is_directory=True)
        with patch.object(Path, 'home', return_value=home):
            rows = self.validate([dict(source=str(data))])
            self.assertEqual(rows, [dict(source=str(data.resolve()), name='manuals', read_only=True)])
            self.assertEqual(self.validate(rows, saved=True), rows)
            for path in (sign_in, sign_in.parent, alias):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'host sign-in'):
                    self.validate([dict(source=str(path))])

    def test_saved_source_replacement_and_private_seed(self):
        rows = self.validate([dict(source=str(self.extra))])
        original = self.root / 'original'
        self.extra.rename(original)
        self.extra.symlink_to(original)
        with self.assertRaises(ValueError): self.validate(rows, saved=True)
        with self.assertRaises(ValueError):
            self.validate([dict(source=str(original))], private_paths=[original])

    def test_read_only_temporary_folder_can_have_a_different_owner(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            source = Path(directory).resolve()
            real_stat = Path.stat

            def stat_with_different_owner(path, *args, **kwargs):
                info = real_stat(path, *args, **kwargs)
                if path != source:
                    return info
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)

            with patch.object(Path, 'stat', stat_with_different_owner):
                rows = folders.validate([dict(source=str(source))], self.workspace, self.state)
            self.assertEqual(rows[0]['source'], str(source))
            self.assertTrue(rows[0]['read_only'])

    def test_directional_permissions_keep_secret_denials(self):
        config = dict(self.config, extra_folders=self.validate([dict(source=str(self.extra))]))
        template = json.loads((sandbox.ROOT / 'containers/dev/security/config/permissions.json').read_text())
        original = copy.deepcopy(template['permission']['path'])
        result = folders.permissions(template, config)['permission']
        self.assertEqual(result['external_directory_read']['/mnt/reference/*'], 'allow')
        self.assertEqual(result['external_directory_write']['/mnt/reference/*'], 'deny')
        self.assertEqual(result['external_directory_write']['/workspace/*'], 'allow')
        self.assertEqual(result['path'], original)
        self.assertNotIn('/mnt/reference2/*', result['external_directory_read'])
        self.assertNotIn(str(self.root), folders.guidance(config))

    def test_cwd_update_preserves_container_and_private_state(self):
        state = self.state / 'demo'; state.mkdir()
        (state / 'private-marker').write_text('keep')
        with patch.object(sandbox, 'down') as down, patch.object(sandbox, 'build') as build:
            sandbox.apply_settings(self.config, state, {'working_directory': '.'})
        down.assert_not_called(); build.assert_not_called()
        self.assertEqual(json.loads((state / 'config.json').read_text())['working_directory'], '.')
        self.assertEqual((state / 'private-marker').read_text(), 'keep')
        with patch.object(sandbox, 'down') as down, self.assertRaises(ValueError):
            sandbox.apply_settings(self.config, state, {'working_directory': 'missing'})
        down.assert_not_called()

    def test_copy_keeps_mounts_but_resets_starting_folder(self):
        config = dict(self.config, profile={'path': '/profile'}, extra_folders=self.validate([dict(source=str(self.extra))]))
        state = self.state / 'source'; state.mkdir(); (state / 'allowlist.txt').write_text('')
        draft = copy_draft(dict(config=config, name='source'), self.state)
        self.assertEqual(draft['working_directory'], '.')
        self.assertEqual(draft['extra_folders'], config['extra_folders'])
        self.assertIsNot(draft['extra_folders'], config['extra_folders'])
        self.assertNotIn('extra_folders', DEFAULT_SETTINGS)

    def test_folder_screen_add_edit_remove_at_80_columns(self):
        async def check():
            class Service:
                def inventory(self): return []
            with patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults', return_value=copy.deepcopy(DEFAULT_SETTINGS)):
                app = Cockpit(self.state, Service())
                async with app.run_test(size=(80, 24)) as pilot:
                    await app.workers.wait_for_complete()
                    await app.push_screen(FoldersScreen(dict(name='test', config=self.config)))
                    await pilot.pause()
                    await pilot.click('#folder-add')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, FolderEditor)
                    app.screen.query_one('#folder-source', Input).value = str(self.extra)
                    await pilot.click('#folder-save')
                    await pilot.pause()
                    fields = app.screen.query_one(FolderFields)
                    self.assertTrue(fields.rows[0]['read_only'])
                    app.screen.query_one('#folders-apply', Button).press()
                    await pilot.pause()
                    self.assertEqual(app.focused.id, 'yes')
                    await pilot.press('escape')
                    await pilot.click('#folder-edit-0')
                    await pilot.pause()
                    app.screen.query_one('#folder-read-only', Checkbox).value = False
                    await pilot.click('#folder-save')
                    await pilot.pause()
                    self.assertFalse(fields.collect()['extra_folders'][0]['read_only'])
                    for button in app.screen.query(Button):
                        self.assertLessEqual(button.region.right, 80)
                    app.screen.query_one('#folder-remove-0', Button).press()
                    await pilot.pause()
                    self.assertEqual(fields.rows, [])
        asyncio.run(check())

    def test_folder_controls_share_a_left_edge_in_setup_and_settings(self):
        async def check():
            mounted = self.validate([dict(source=str(self.extra))])
            draft = preset('base', copy.deepcopy(DEFAULT_SETTINGS))
            draft.update(name='demo', workspace=str(self.workspace), extra_folders=mounted)
            for size in ((80, 24), (120, 40), (220, 50)):
                app = Cockpit(self.state, FakeService([]))
                async with app.run_test(size=size) as pilot:
                    for screen in (SettingsForm(draft), FoldersScreen(dict(name='demo', config=draft))):
                        await app.push_screen(screen)
                        if isinstance(screen, SettingsForm):
                            screen.query_one('#folders-section', Collapsible).collapsed = False
                        await pilot.pause()
                        fields = screen.query_one(FolderFields)
                        self.assertIn('working-directory', check_input_layout(screen))
                        start = fields.query_one('#working-directory', Input)
                        heading = fields.query_one('#additional-folders-label')
                        self.assertEqual(heading.region.y, start.region.bottom + 1)
                        self.assertEqual({widget.region.x for widget in (
                            fields.query_one('#starting-folder-label'), start, heading, fields.query_one('.mount-name'),
                            fields.query_one('.mount-row .setting-help'), fields.query_one('#folder-edit-0'), fields.query_one('#folder-add')
                        )}, {fields.content_region.x})
                        for button in (fields.query_one('#folder-edit-0'), fields.query_one('#folder-add')):
                            self.assertEqual(button.region.height, 1)
                        await app.pop_screen()
                    for row in (None, mounted[0]):
                        dialog = FolderEditor(row, [], self.workspace, self.state)
                        await app.push_screen(dialog)
                        await pilot.pause()
                        self.assertEqual(set(check_input_layout(dialog)), {'folder-source', 'folder-name'})
                        if row:
                            preview = dialog.query_one('#folder-preview', Static)
                            self.assertEqual(preview.region.y, dialog.query_one('#folder-name').region.bottom + 1)
                        save = dialog.query_one('#folder-save')
                        self.assertIs(dialog.get_widget_at(save.region.x, save.region.y)[0], save)
                        await app.pop_screen()
        with patch('ai_sandbox_factory.updates.available_update', return_value=None):
            asyncio.run(check())

    def test_creation_passes_selected_workspace_to_folder_review(self):
        async def check():
            class Service:
                def inventory(self): return []
            with patch('ai_sandbox_factory.updates.available_update', return_value=None), patch('ai_sandbox_factory.sandbox_tui.load_defaults', return_value=copy.deepcopy(DEFAULT_SETTINGS)):
                app = Cockpit(self.state, Service())
                async with app.run_test(size=(80, 24)) as pilot:
                    await app.workers.wait_for_complete()
                    for path, create in [(self.workspace, False), (self.root / 'new-project', True)]:
                        await app.push_screen(NewSandbox())
                        app.screen.query_one('#workspace', Input).value = str(path)
                        app.screen.query_one('#name', Input).value = 'new-example'
                        app.screen.query_one('#mkdir', Checkbox).value = create
                        app.screen.query_one('#continue', Button).press()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, SettingsForm)
                        fields = app.screen.query_one(FolderFields)
                        self.assertEqual(fields.config['workspace'], str(path))
                        self.assertEqual(fields.collect()['working_directory'], '.')
                        if not create:
                            fields.query_one('#working-directory', Input).value = 'app'
                            self.assertEqual(fields.collect()['working_directory'], 'app')
                        app.pop_screen(); app.pop_screen()
                        await pilot.pause()
        asyncio.run(check())
