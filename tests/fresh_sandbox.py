"""Release gate: first-launch Pi and FrostPi against an existing project."""
import argparse
from pathlib import Path
import platform
import subprocess
import tempfile

from frostpi_editor import check
from pi_sandbox_factory import browser_editor, sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--image', help='Use an already built checkout image.')
    parser.add_argument('--code-server', type=Path, help='Use the extracted pinned code-server.')
    parser.add_argument('--chromium')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = {'engine': 'podman', 'uid': 1000, 'gid': 1000,
              'common': sandbox.common_profile(),
              'profile': sandbox.load_profile(str(sandbox.ROOT / 'profiles/base'))}
    if not args.image:
        sandbox.build(config)
    image = args.image or sandbox.image_names(config)[0]
    with tempfile.TemporaryDirectory(prefix='factory-fresh-editor-') as tmp:
        code_server = args.code_server
        if code_server is None:
            root = Path(tmp)
            archive = browser_editor.checked_archive(root, arch=platform.machine())
            subprocess.run(['tar', '-xzf', str(archive), '-C', str(root)], check=True)
            code_server = root / archive.name.removesuffix('.tar.gz')
        check(image, code_server.resolve(), output / 'automatic-startup', args.chromium,
              'automatic', False, False, False)
        check(image, code_server.resolve(), output / 'project-and-folders', args.chromium, None, False, False, False,
              folder_access=True)


if __name__ == '__main__':
    main()
