"""Install the same pinned agent in a controller or advisor image."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


def download(specification, target):
    algorithm = 'sha256' if 'sha256' in specification else 'sha512'
    expected = specification[algorithm]
    digest = hashlib.new(algorithm)
    with urllib.request.urlopen(specification['url'], timeout=60) as source, target.open('wb') as output:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            output.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected:
        target.unlink()
        raise ValueError(f'{specification["url"]}: {algorithm} mismatch; expected {expected}, got {digest.hexdigest()}')


def install(agent, target):
    pins = json.loads(Path(__file__).with_name('agent-versions.json').read_text())
    pin = pins[agent]
    if agent == 'codex':
        subprocess.run(['npm', 'install', '-g', '@openai/codex@' + pin['version']], check=True)
        sources = list(Path('/usr/local/lib/node_modules/@openai').rglob('codex'))
        source = next(path for path in sources if path.is_file() and path.stat().st_mode & 0o111)
        shutil.copyfile(source, target)
        # Newer Codex commands start this companion from beside the CLI.
        for helper in source.parent.glob('codex-code-mode-host'):
            shutil.copyfile(helper, target.with_name(helper.name))
            target.with_name(helper.name).chmod(0o755)
    else:
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
        with tempfile.TemporaryDirectory(prefix='factory-agent-install-') as folder:
            archive = Path(folder) / 'download'
            download(pin['platforms'][arch], archive)
            if agent == 'agy':
                with tarfile.open(archive) as package:
                    member = package.getmember('antigravity')
                    if not member.isfile():
                        raise ValueError('Antigravity archive does not contain its executable')
                    with package.extractfile(member) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
            else:
                shutil.copyfile(archive, target)
    target.chmod(0o755)
    version = subprocess.check_output([str(target), '--version'], text=True).strip()
    if pin['version'] not in version.split():
        # Claude appends its product name; AGY can prefix its version with v.
        import re
        if not re.search(r'(?<![\d.])v?' + re.escape(pin['version']) + r'(?![\d.])', version):
            raise ValueError(f'{agent}: installed {version!r}; expected {pin["version"]}')
    print(version, flush=True)


if __name__ == '__main__':
    install(sys.argv[1], Path(sys.argv[2]))
