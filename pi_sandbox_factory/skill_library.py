"""Reusable, validated skill snapshots. Importing never executes skill code."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import subprocess
from urllib.parse import unquote, urlsplit

import yaml

from . import registry

MAX_MANIFEST_BYTES = 256 * 1024


def metadata(path):
    from .sandbox_options import SKILLS
    path = Path(path)
    registry.safe_tree(path)
    file = path / 'SKILL.md'
    if not file.is_file() or file.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('SKILL.md is missing or exceeds 256 KiB')
    text = file.read_text(encoding='utf-8')
    match = re.match(r'\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        raise ValueError('SKILL.md needs YAML frontmatter at the start')
    try:
        data = yaml.safe_load(match[1])
    except (yaml.YAMLError, RecursionError) as error:
        raise ValueError('SKILL.md has invalid YAML frontmatter') from error
    name = data.get('name') if isinstance(data, dict) else None
    description = data.get('description') if isinstance(data, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('Skill name must use lowercase letters, digits and single hyphens (1–64 characters)')
    if name in SKILLS or name in ('goal', 'advisor'):
        raise ValueError('This skill name is reserved by a built-in feature')
    if not isinstance(description, str) or not description.strip() or len(description) > 4096:
        raise ValueError('Skill description must contain 1–4096 characters')
    return {'name': name, 'description': ' '.join(description.split())}


def entries():
    path = registry.root() / 'skills.json'
    return json.loads(path.read_text()) if path.exists() else {}


def _write(base, values):
    temporary = base / 'skills.tmp'
    temporary.write_text(json.dumps(values, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(base / 'skills.json')


def skill_source_url(source):
    if isinstance(source, str):
        url = urlsplit(source)
        if url.scheme == 'https' and url.hostname in ('github.com', 'www.github.com') and not url.username and not url.password and url.port in (None, 443):
            source = url._replace(netloc='github.com', query='', fragment='').geturl()
    return registry.source_url(source)


def resolve_source(source, ref=None):
    """Accept GitHub links copied from a folder or SKILL.md page."""
    source = skill_source_url(source)
    url = urlsplit(source)
    parts = url.path.strip('/').split('/')
    if url.scheme != 'https' or url.hostname != 'github.com' or len(parts) < 4 or parts[2] not in ('tree', 'blob'):
        return source, ref, '.'
    repository = f'https://github.com/{parts[0]}/{parts[1].removesuffix(".git")}.git'
    remainder = unquote('/'.join(parts[3:]))
    refs = registry.git('ls-remote', '--heads', '--tags', '--', repository, capture=True).stdout
    names = {line.split('\t', 1)[1].removeprefix('refs/heads/').removeprefix('refs/tags/')
             for line in refs.splitlines() if '\t' in line and not line.endswith('^{}')}
    # Longest match supports branch names containing slashes.
    matched = next((name for name in sorted(names, key=len, reverse=True)
                    if remainder == name or remainder.startswith(name + '/')), None)
    if matched is None:
        candidate = remainder.split('/', 1)[0]
        if re.fullmatch(r'[0-9a-fA-F]{7,40}', candidate):
            matched = candidate
        else:
            raise ValueError('The branch or tag in this GitHub link was not found. Copy a current folder link from GitHub.')
    folder = remainder[len(matched):].lstrip('/') or '.'
    if parts[2] == 'blob':
        if Path(folder).name != 'SKILL.md':
            raise ValueError('Choose a skill folder or a link to its SKILL.md file.')
        folder = str(Path(folder).parent)
    if Path(folder).is_absolute() or '..' in Path(folder).parts:
        raise ValueError('The skill folder must stay inside the repository.')
    if ref and ref != matched:
        raise ValueError('The revision field conflicts with the GitHub link. Clear it to use the link, or enter the same branch, tag or commit.')
    return repository, matched, folder


def discover(source, destination, ref=None):
    """Fetch once into a private staging directory and report each candidate separately."""
    from .sandbox import tree_digest
    source = skill_source_url(source)
    try:
        repository, revision_ref, subdir = resolve_source(source, ref)
        revision = registry.export(repository, Path(destination), revision_ref, subdir=subdir, check=False)
    except subprocess.CalledProcessError as error:
        raise ValueError('Could not fetch the skills. Check the repository URL, revision and your Git access.') from error
    root = Path(destination)
    rows = []
    parents = []
    for file in sorted(root.rglob('SKILL.md'), key=lambda file: (len(file.parts), file)):
        if any(part in ('.git', 'node_modules', '__pycache__') for part in file.relative_to(root).parts):
            continue
        if any(parent in file.parents for parent in parents):
            continue
        parents.append(file.parent)
        folder = (Path(subdir) / file.parent.relative_to(root)).as_posix()
        # Only source descendants matter; the host temporary directory may itself
        # be reached through a symlink (for example /var on macOS).
        relative = file.relative_to(root)
        if any((root.joinpath(*relative.parts[:end])).is_symlink()
               for end in range(1, len(relative.parts) + 1)):
            continue
        identity_source = repository
        parsed = urlsplit(repository)
        if parsed.scheme == 'https' and parsed.hostname == 'github.com':
            identity_source = repository.rstrip('/').removesuffix('.git') + '.git'
        row = dict(id=hashlib.sha256((identity_source + '\0' + folder).encode()).hexdigest()[:24],
                   source=source, folder=folder, commit=revision or 'local', path=str(file.parent), ref=ref)
        try:
            row.update(metadata(file.parent), digest=tree_digest(file.parent))
        except (ValueError, UnicodeError, OSError) as error:
            row.update(name=folder, description='', error=str(error))
        rows.append(row)
    if not rows:
        raise ValueError('No SKILL.md files were found in this source')
    return sorted(rows, key=lambda row: row['folder'])


def _store(source, target, digest):
    from .sandbox import tree_digest
    if target.exists():
        if tree_digest(target) != digest:
            raise ValueError('Stored skill snapshot was modified; import its source again')
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.skill-', dir=target.parent) as temp:
        staged = Path(temp) / 'skill'
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        if tree_digest(staged) != digest:
            raise ValueError('Skill changed during copy; validate it again')
        staged.rename(target)


def _collect_unused(base, values):
    used = {row['digest'] for row in values.values()}
    for folder in (base / 'skills').glob('*'):
        if re.fullmatch(r'[0-9a-f]{16}', folder.name) and folder.name not in used and not folder.is_symlink():
            shutil.rmtree(folder)


def install(rows):
    """Install only explicitly selected validated candidates, atomically as one list."""
    from .sandbox import tree_digest
    if not rows:
        raise ValueError('Select at least one valid skill')
    with registry.locked() as base:
        values = entries()
        for row in rows:
            source = Path(row['path'])
            info = metadata(source)
            if row.get('error') or tree_digest(source) != row['digest'] or info['name'] != row['name']:
                raise ValueError('Skill changed during import; validate it again')
            target = base / 'skills' / row['digest']
            _store(source, target, row['digest'])
            values[row['id']] = {**row, **info, 'path': str(target)}
        _write(base, values)
        _collect_unused(base, values)


def remove(identifier):
    with registry.locked() as base:
        values = entries()
        if identifier not in values:
            raise ValueError('Skill is no longer in the library')
        del values[identifier]
        _write(base, values)
        _collect_unused(base, values)


def selections(identifiers):
    if not isinstance(identifiers, list) or any(not isinstance(key, str) for key in identifiers):
        raise ValueError('Skills must be a list of library identifiers')
    values = entries()
    rows = []
    for identifier in dict.fromkeys(identifiers):
        if identifier not in values:
            raise ValueError('A selected skill is no longer in the library; choose it again')
        rows.append(values[identifier])
    names = [row['name'] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError('Selected skills have the same name; choose one source for each name')
    return sorted(rows, key=lambda row: row['name'])


def snapshot(identifiers, state, existing=()):
    """Copy into sandbox-owned state, preserving existing explicitly retained snapshots."""
    from .sandbox import tree_digest
    result = list(existing)
    if not identifiers:
        return sorted(result, key=lambda row: row['name'])
    with registry.locked():
        for row in selections(identifiers):
            previous = next((skill for skill in result if skill['name'] == row['name']), None)
            if previous:
                if previous.get('library_id') == row['id'] and previous['digest'] == row['digest']:
                    continue
                raise ValueError('Skill name is already selected in this sandbox: ' + row['name'])
            source = Path(row['path'])
            metadata(source)
            if tree_digest(source) != row['digest']:
                raise ValueError('Library skill was modified; import its source again')
            target = Path(state) / 'skills' / row['digest']
            _store(source, target, row['digest'])
            result.append(dict(name=row['name'], repo=row['source'], folder=row['folder'],
                               commit=row['commit'], digest=row['digest'], path=str(target),
                               library_id=row['id'], description=row['description']))
    return sorted(result, key=lambda row: row['name'])
