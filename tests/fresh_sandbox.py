"""Release gate: first-launch Pi and FrostPi against an existing project."""
import argparse
from pathlib import Path
import platform
import subprocess
import tempfile

from frostpi_editor import check
from ai_sandbox_factory import browser_editor, sandbox, image_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--image', help='Use an already built checkout image.')
    parser.add_argument('--code-server', type=Path, help='Use the extracted pinned code-server.')
    parser.add_argument('--chromium')
    parser.add_argument('--knowledge', action='store_true', help='Exercise the stock image Knowledge tools in Pi and FrostPi')
    parser.add_argument('--candidate-index', type=Path, help='Use the exact published Pi stock candidate instead of rebuilding it')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = {'engine': 'podman', 'uid': 1000, 'gid': 1000,
              'common': sandbox.common_profile(),
              'profile': sandbox.load_profile(str(sandbox.ROOT / 'profiles/base'))}
    if args.candidate_index:
        image_sources.INDEX = args.candidate_index.resolve()
        config['image_source'] = 'prebuilt'
        config['profile']['digest'] = sandbox.tree_digest(config['profile']['path'])
        key = image_sources.recipe(config, 'pi-stock')
        if key not in image_sources.index():
            raise ValueError(f'No published Pi stock candidate matches the current sources ({key}). Rebuild the candidate.')
        image = image_sources.ensure(config, 'pi-stock', sandbox.image_names(config)[0])
    elif not args.image:
        sandbox.build(config)
        image = sandbox.image_names(config)[0]
    else:
        image = args.image
    with tempfile.TemporaryDirectory(prefix='factory-fresh-editor-') as tmp:
        code_server = args.code_server
        if code_server is None:
            root = Path(tmp)
            archive = browser_editor.checked_archive(root, arch=platform.machine())
            subprocess.run(['tar', '-xzf', str(archive), '-C', str(root)], check=True)
            code_server = root / archive.name.removesuffix('.tar.gz')
        check(image, code_server.resolve(), output / 'automatic-startup', args.chromium,
              'automatic', False, False, False, full_width=True)
        print('PASS FrostPi maximizes to the available editor width and restores its panel.')
        check(image, code_server.resolve(), output / 'project-and-folders', args.chromium, None, False, False, False,
              folder_access=True)
        if args.knowledge:
            check(image, code_server.resolve(), output / 'local-knowledge', args.chromium,
                  None, False, False, False, local_knowledge='endpoint')


if __name__ == '__main__':
    main()
