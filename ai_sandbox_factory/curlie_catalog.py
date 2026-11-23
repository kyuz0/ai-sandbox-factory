"""Download and cache Curlie's website categories on the workstation."""

from email.utils import parsedate_to_datetime
import fcntl
from functools import lru_cache
from hashlib import sha256
from http import HTTPStatus
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

URL = 'https://curlie.org/directory-dl'
DOWNLOAD_SIZE = 'about 180 MB'
MANIFEST = Path(__file__).with_name('network-catalog') / 'curlie.json'


@lru_cache(maxsize=1)
def manifest():
    return json.loads(MANIFEST.read_text())['groups']


def category_cache():
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'ai-sandbox-factory/categories'


@lru_cache(maxsize=1)
def category_layout():
    branches = {name: (group['branches'], group.get('regional_topics', []))
                for name, group in manifest().items()}
    return sha256(json.dumps(branches, sort_keys=True).encode()).hexdigest()


def metadata(cache=None):
    try:
        root = Path(cache or category_cache())
        info = json.loads((root / 'curlie-latest.json').read_text())
        if info.get('layout') != category_layout() or not isinstance(info.get('downloaded_at'), (float, int)):
            return None
        with ZipFile(version_path(root, info['revision'])) as catalog:
            catalog.getinfo('download.json')
        return info
    except (OSError, ValueError, KeyError, TypeError, AttributeError, BadZipFile):
        return None


def version_path(cache, revision):
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{32}', revision):
        raise ValueError('Invalid Curlie website list version.')
    return cache / 'curlie' / (revision + '.zip')


def read(cache, revision, names):
    try:
        with ZipFile(version_path(cache, revision)) as catalog:
            info = json.loads(catalog.read('download.json'))
            rules = {name: catalog.read(name + '.txt').decode('ascii').splitlines() for name in names}
            return info, rules
    except (OSError, ValueError, KeyError, BadZipFile) as error:
        raise ValueError('This Curlie list is no longer available. Choose a saved or newer list in Network settings.') from error


def assigned(groups):
    for group in groups.values():
        if info := group.get('curlie_download'):
            return info
    return None


class HeadRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirect is not None:
            redirect.method = 'HEAD'
        return redirect


def check(cache=None):
    current = metadata(cache)
    try:
        with build_opener(HeadRedirects()).open(Request(URL, method='HEAD'), timeout=15) as response:
            modified = response.headers.get('Last-Modified')
            etag = response.headers.get('ETag')
            if current and ((etag and etag == current.get('etag')) or
                            (not etag and modified and modified == current.get('last_modified'))):
                return None
            return {'size': response.headers.get('Content-Length')}
    except OSError as error:
        raise ValueError(f'The Curlie update check failed. {error}') from error


def matches(path, group):
    if any(path == branch or path.startswith(branch + '/') for branch in group['branches']):
        return True
    return path.startswith('Regional/') and not set(path.split('/')[1:]).isdisjoint(
        group.get('regional_topics', ()))


def hostname(row):
    from .network_groups import domain

    try:
        url = urlsplit(row.split(b'\t', 1)[0].decode('utf-8').strip())
        if url.scheme in ('http', 'https') and url.hostname:
            return domain(url.hostname.removesuffix('.'))
    except ValueError:
        pass
    return None


def build(archive, output, info):
    groups = manifest()
    by_id = {}
    with tarfile.open(fileobj=archive, mode='r|gz') as source:
        for member in source:
            if member.isfile() and member.name.endswith('-s.tsv'):
                for row in source.extractfile(member):
                    identifier, path, _ = row.decode('utf-8').split('\t', 2)
                    names = tuple(name for name, group in groups.items() if matches(path, group))
                    if names:
                        by_id[int(identifier)] = names

    rules = {name: set() for name in groups}
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode='r|gz') as source:
        for member in source:
            if member.isfile() and member.name.endswith('-c.tsv'):
                for row in source.extractfile(member):
                    names = by_id.get(int(row.rsplit(b'\t', 1)[1]))
                    if names and (host := hostname(row)):
                        for name in names:
                            rules[name].add(host)

    if empty := [groups[name]['title'] for name, items in rules.items() if not items]:
        raise ValueError('Curlie did not include websites for ' + ', '.join(empty))
    with ZipFile(output, 'w', ZIP_DEFLATED) as catalog:
        for name, items in rules.items():
            catalog.writestr(name + '.txt', '\n'.join(sorted(items)) + '\n')
        catalog.writestr('download.json', json.dumps(info))
    return info


def refresh(cache=None, *, force=False, log=print):
    cache = Path(cache or category_cache())
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with (cache / 'curlie.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = metadata(cache)
            if current and not force:
                return current
            return download(cache, current, log)
    except (OSError, EOFError, ValueError, tarfile.TarError, BadZipFile) as error:
        raise ValueError(f'The Curlie download failed. {error}') from error


def download(cache, current, log):
    headers = {'If-None-Match': current['etag']} if current and current.get('etag') else {}
    if current and current.get('last_modified'):
        headers['If-Modified-Since'] = current['last_modified']
    if current:
        log('Checking Curlie for a newer list…')
    else:
        log(f'Downloading the Curlie list ({DOWNLOAD_SIZE})…')
    try:
        response = urlopen(Request(URL, headers=headers), timeout=60)
    except HTTPError as error:
        if current and error.code == HTTPStatus.NOT_MODIFIED:
            error.close()
            log('Curlie has no newer lists.')
            return current
        raise

    with response, tempfile.TemporaryDirectory(prefix='curlie-', dir=cache) as temporary:
        modified = response.headers.get('Last-Modified')
        etag = response.headers.get('ETag')
        if current and ((etag and etag == current.get('etag')) or
                        (modified and not etag and modified == current.get('last_modified'))):
            log('Curlie has no newer lists.')
            return current
        if current and modified and current.get('last_modified') and (
                parsedate_to_datetime(modified) < parsedate_to_datetime(current['last_modified'])):
            raise ValueError('The download is older than the lists already on this computer.')
        if current:
            log(f'Downloading the newer Curlie list ({DOWNLOAD_SIZE})…')
        root = Path(temporary)
        with tempfile.TemporaryFile(dir=cache) as archive:
            shutil.copyfileobj(response, archive)
            archive.seek(0)
            log('Preparing the Curlie list…')
            info = build(archive, root / 'curlie.zip', {'revision': uuid4().hex, 'layout': category_layout(),
                         'downloaded_at': time.time(), 'last_modified': modified, 'etag': etag})
        version = version_path(cache, info['revision'])
        version.parent.mkdir(exist_ok=True)
        (root / 'curlie.zip').replace(version)
        latest = root / 'latest.json'
        latest.write_text(json.dumps(info))
        latest.replace(cache / 'curlie-latest.json')
    log('Curlie website lists are ready.')
    return info
