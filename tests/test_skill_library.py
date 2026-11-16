import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pi_sandbox_factory import skill_library, sandbox, sandbox_skills


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SANDBOX_REGISTRY_DIR': str(self.root / 'library')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = self.root / 'source'
        self.source.mkdir()

    def skill(self, name, text=None):
        folder = self.source / name
        folder.mkdir()
        (folder / 'SKILL.md').write_text(text or f'---\nname: {name}\ndescription: Help with {name}.\n---\nInstructions\n')
        return folder

    def discover(self):
        return skill_library.discover(str(self.source), self.root / 'fetched')

    def test_discovery_under_symlinked_host_temp_directory(self):
        self.skill('one')
        actual = self.root / 'actual'
        actual.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(actual, target_is_directory=True)
        rows = skill_library.discover(str(self.source), alias / 'fetched')
        self.assertEqual([row['name'] for row in rows], ['one'])
        self.assertNotIn('error', rows[0])

    def test_multi_import_snapshots_survive_library_removal_and_build(self):
        self.skill('one'); self.skill('two')
        marker = self.root / 'executed'
        (self.source / 'setup.py').write_text(f'open({str(marker)!r},"w").write("ran")')
        rows = self.discover()
        skill_library.install(rows)
        state = self.root / 'sandbox'
        selected = skill_library.snapshot([r['id'] for r in reversed(rows)], state)
        self.assertEqual([r['name'] for r in selected], ['one', 'two'])
        self.assertFalse(marker.exists())
        for row in rows:
            skill_library.remove(row['id'])
        import shutil
        shutil.rmtree(self.root / 'library')
        for row in selected:
            self.assertEqual(sandbox.tree_digest(Path(row['path'])), row['digest'])
        commands = []
        class Launcher:
            tree_digest = staticmethod(sandbox.tree_digest)
            def run(self, command):
                context = Path(command[-1])
                resources = json.loads((context / 'extra-resources.json').read_text())
                self.assertions = resources
                commands.append(resources['skills'])
        sandbox_skills.build({'extra_skills': selected, 'engine': 'podman'}, 'fixture', Launcher())
        self.assertEqual(commands, [['/opt/extra-skills/one', '/opt/extra-skills/two']])

    def test_bad_frontmatter_and_reserved_names_not_selectable(self):
        self.skill('body-only', 'Instructions\nname: fake\ndescription: fake\n')
        self.skill('goal')
        self.skill('valid', '---\nname: valid\ndescription: |\n  Multiple lines\n  are supported.\n---\nBody')
        rows = self.discover()
        self.assertEqual(sum('error' in r for r in rows), 2)
        good = [r for r in rows if not r.get('error')]
        self.assertEqual(good[0]['description'], 'Multiple lines are supported.')
        with self.assertRaises(ValueError):
            skill_library.install(rows)
        self.assertFalse(skill_library.entries())
        skill_library.install(good)
        self.assertEqual(len(skill_library.entries()), 1)

    def test_duplicate_names_rejected_before_snapshot(self):
        self.skill('first', '---\nname: same\ndescription: First\n---\n')
        self.skill('second', '---\nname: same\ndescription: Second\n---\n')
        rows = self.discover(); skill_library.install(rows)
        with self.assertRaisesRegex(ValueError, 'same name'):
            skill_library.snapshot([r['id'] for r in rows], self.root / 'sandbox')
        self.assertFalse((self.root / 'sandbox').exists())

    def test_tampering_missing_entry_and_name_conflict(self):
        self.skill('one')
        rows = self.discover(); skill_library.install(rows)
        state = self.root / 'sandbox'
        saved = skill_library.snapshot([rows[0]['id']], state)
        self.assertEqual(skill_library.snapshot([rows[0]['id']], state, saved), saved)
        path = Path(skill_library.entries()[rows[0]['id']]['path'])
        (path / 'SKILL.md').write_text('---\nname: one\ndescription: Changed\n---\n')
        with self.assertRaisesRegex(ValueError, 'modified'):
            skill_library.snapshot([rows[0]['id']], self.root / 'other')
        skill_library.remove(rows[0]['id'])
        with self.assertRaisesRegex(ValueError, 'no longer'):
            skill_library.selections([rows[0]['id']])
        self.assertEqual(sandbox.tree_digest(Path(saved[0]['path'])), saved[0]['digest'])

    def test_symlinks_and_unsafe_yaml_do_not_execute(self):
        folder = self.skill('one')
        (folder / 'link').symlink_to('/etc/passwd')
        rows = self.discover()
        self.assertIn('regular files', rows[0]['error'])
        import shutil
        shutil.rmtree(self.root / 'fetched')
        (folder / 'link').unlink()
        marker = self.root / 'executed'
        (folder / 'SKILL.md').write_text('---\nname: one\ndescription: !!python/object/apply:os.system ["touch '+str(marker)+'"]\n---\n')
        rows = self.discover()
        self.assertIn('invalid YAML', rows[0]['error'])
        self.assertFalse(marker.exists())

    def test_nested_skills_are_not_imported_twice(self):
        parent = self.skill('parent')
        (parent / 'Nested').mkdir()
        (parent / 'Nested' / 'SKILL.md').write_text('---\nname: nested\ndescription: Nested\n---\n')
        self.assertEqual([r['name'] for r in self.discover()], ['parent'])

    def test_updated_library_does_not_mutate_retained_sandbox(self):
        self.skill('one')
        rows = self.discover(); skill_library.install(rows)
        saved = skill_library.snapshot([rows[0]['id']], self.root / 'sandbox')
        (Path(rows[0]['path']) / 'SKILL.md').write_text('---\nname: one\ndescription: New version\n---\n')
        rows[0]['digest'] = sandbox.tree_digest(Path(rows[0]['path']))
        rows[0]['description'] = 'New version'
        skill_library.install(rows)
        self.assertEqual(skill_library.snapshot([], self.root / 'sandbox', saved), saved)
        self.assertNotEqual(saved[0]['digest'], skill_library.entries()[rows[0]['id']]['digest'])

    def test_unrelated_symlink_is_not_followed_or_imported(self):
        self.skill('one')
        (self.source/'unrelated').symlink_to('/etc')
        rows=self.discover()
        self.assertEqual([row['name'] for row in rows], ['one'])
        self.assertTrue((self.root/'fetched'/'unrelated').is_symlink())

    def test_interrupted_copy_does_not_leave_partial_snapshot(self):
        self.skill('one');rows=self.discover()
        original=__import__('shutil').copytree
        def fail(source,target,**kwargs):
            Path(target).mkdir()
            (Path(target)/'partial').write_text('partial')
            raise OSError('disk full')
        with patch('pi_sandbox_factory.skill_library.shutil.copytree',side_effect=fail):
            with self.assertRaises(OSError):skill_library.install(rows)
        self.assertFalse(skill_library.entries())
        self.assertFalse((self.root/'library'/'skills'/rows[0]['digest']).exists())
        skill_library.install(rows)
        self.assertEqual(len(skill_library.entries()),1)

    def test_large_repository_has_no_arbitrary_skill_limit(self):
        for index in range(201):
            self.skill(f'skill-{index}')
        self.assertEqual(len(self.discover()), 201)

    def test_github_folder_file_and_slash_branch_links(self):
        import subprocess
        refs = 'a\trefs/heads/main\nb\trefs/heads/release/audit\n'
        with patch.object(skill_library.registry, 'git', return_value=subprocess.CompletedProcess([], 0, refs)):
            for path, expected_ref, expected_folder in (
                ('tree/main/skills/example', 'main', 'skills/example'),
                ('blob/main/skills/example/SKILL.md', 'main', 'skills/example'),
                ('tree/release/audit/skills/example', 'release/audit', 'skills/example'),
                ('tree/abcdef123/skills/example', 'abcdef123', 'skills/example'),
            ):
                with self.subTest(path=path):
                    self.assertEqual(skill_library.resolve_source('https://github.com/owner/repo/'+path),
                                     ('https://github.com/owner/repo.git', expected_ref, expected_folder))
            with self.assertRaisesRegex(ValueError, 'inside the repository'):
                skill_library.resolve_source('https://github.com/owner/repo/tree/main/%2e%2e/outside')
            with self.assertRaisesRegex(ValueError, 'not found'):
                skill_library.resolve_source('https://github.com/owner/repo/tree/missing/skill')
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                skill_library.resolve_source('https://github.com/owner/repo/tree/main/skills/example', 'other')
            self.assertEqual(skill_library.resolve_source('https://www.github.com/owner/repo/blob/main/skills/example/SKILL.md?plain=1#L3'),
                             ('https://github.com/owner/repo.git', 'main', 'skills/example'))

    def test_github_folder_discovery_and_repeat_import(self):
        import shutil
        self.skill('one')
        url = 'https://github.com/owner/repo/tree/main/skills/one'
        with patch.object(skill_library, 'resolve_source', return_value=('https://github.com/owner/repo.git', 'main', 'skills/one')), patch.object(skill_library.registry, 'export') as export:
            def fetched(source, destination, ref, **kwargs):
                self.assertEqual(kwargs['subdir'], 'skills/one')
                shutil.copytree(self.source/'one', destination)
                return 'abc123'
            export.side_effect = fetched
            rows = skill_library.discover(url, self.root/'fetched')
            self.assertEqual(rows[0]['folder'], 'skills/one')
            self.assertEqual(rows[0]['source'], url)
            skill_library.install(rows)
            again = skill_library.discover(rows[0]['source'], self.root/'again', rows[0]['ref'])
            self.assertEqual(again[0]['id'], rows[0]['id'])
            with patch.object(skill_library, 'resolve_source', return_value=('https://github.com/owner/repo/', 'main', 'skills/one')):
                root_rows = skill_library.discover('https://github.com/owner/repo/', self.root/'root-import')
            self.assertEqual(root_rows[0]['id'], rows[0]['id'])

    def test_fetch_failure_has_actionable_message(self):
        import subprocess
        with patch.object(skill_library.registry, 'export', side_effect=subprocess.CalledProcessError(128, ['git', 'clone'])):
            with self.assertRaisesRegex(ValueError, 'Check the repository URL, revision and your Git access'):
                self.discover()
