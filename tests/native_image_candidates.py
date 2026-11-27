"""Exercise native clients from the published candidate images with synthetic auth."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_sandbox_factory import image_sources, primary_workspace, sandbox

spec = importlib.util.spec_from_file_location('candidate_publisher', ROOT / 'scripts/build_factory_images.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def check(index, agent):
    image_sources.INDEX = index.resolve()
    images = publisher.verify_index(index, ['native-common', 'controller-' + agent])
    controller = images['controller-' + agent]['reference']
    if agent in ('claude', 'agy'):
        script = 'native_claude_login.py' if agent == 'claude' else 'agy_native_auth.py'
        command = ['podman', 'run', '--rm', '--pull=never', '--network=none',
                   '--userns=keep-id:uid=1000,gid=1000', '--user=1000:1000',
                   '--security-opt=label=disable', '-v', f'{ROOT / "ai_sandbox_factory"}:/factory/ai_sandbox_factory:ro',
                   '-v', f'{ROOT / "tests"}:/factory/tests:ro',
                   '-v', f'{ROOT / "ai_sandbox_factory/containers/primary-worker/sandbox-update.py"}:/opt/sandbox/sandbox-update.py:ro',
                   '--entrypoint', f'/opt/{agent}-proxy/bin/python', controller,
                   f'/factory/tests/{script}', '--binary', '/usr/local/bin/factory-agent']
        subprocess.run(command, check=True)
        if agent == 'agy':
            subprocess.run([*command, '--fresh'], check=True)
    config = publisher.public_config()
    config.update(primary_agent=agent, image_source='prebuilt', options={'browser': False})
    repository = 'localhost/factory-native-candidate-' + uuid.uuid4().hex
    target = repository + ':test'
    try:
        primary_workspace.build(config, target)
        subprocess.run([sys.executable, str(ROOT / 'tests/connector_native_clients.py'),
                        '--agent', agent, '--image', agent + '=' + target], check=True)
        print(f'PASS {agent}: candidate version, synthetic native auth and workspace connector permissions', flush=True)
    finally:
        result = subprocess.check_output(['podman', 'images', '--format', '{{.Repository}}:{{.Tag}}'], text=True)
        owned = [name for name in result.splitlines() if name.rsplit(':', 1)[0] == repository]
        if owned:
            subprocess.run(['podman', 'rmi', *owned], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-index', type=Path, required=True)
    parser.add_argument('--agent', choices=('codex', 'claude', 'agy'), required=True)
    args = parser.parse_args()
    check(args.candidate_index, args.agent)
