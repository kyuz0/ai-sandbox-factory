"""Background GitHub release checks for the pipx installation."""
import json
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import urllib.request

from packaging.version import InvalidVersion, Version

PACKAGE = 'ai-sandbox-factory'
TAGS_URL = 'https://api.github.com/repos/kyuz0/ai-sandbox-factory/tags?per_page=20'
UPGRADE_COMMAND = ('pipx', 'upgrade', PACKAGE)
RELAUNCH = 'relaunch-after-update'


def installed_version():
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return '0.0.0+source'


def pipx_install():
    return (Path(sys.prefix) / 'pipx_metadata.json').is_file()


def upgrade(log):
    log('› ' + ' '.join(UPGRADE_COMMAND))
    try:
        with subprocess.Popen(UPGRADE_COMMAND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors='replace') as process:
            for line in process.stdout:
                log(line.rstrip())
            code = process.wait()
    except OSError as error:
        raise ValueError(f'Could not run pipx: {error}') from error
    if code:
        raise ValueError(f'pipx exited with status {code}. See Output for details.\n'
                         'To retry from a terminal, run pipx upgrade ai-sandbox-factory.')


def available_update(current=None):
    try:
        current = Version(current or installed_version())
        request = urllib.request.Request(TAGS_URL, headers={'User-Agent': PACKAGE})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(262144))
        if not isinstance(payload, list):
            return None
        versions = []
        for tag in payload:
            if not isinstance(tag, dict):
                continue
            try:
                version = Version(str(tag.get('name', '')).removeprefix('v'))
            except InvalidVersion:
                continue
            if not version.is_prerelease and not version.is_devrelease:
                versions.append(version)
        latest = max(versions, default=current)
        return str(latest) if latest > current else None
    except (OSError, ValueError):
        return None
