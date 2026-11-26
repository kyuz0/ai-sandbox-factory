"""Public image assembly using the same recipes as local sandbox builds."""
import copy
from pathlib import Path
import shutil
import tempfile

from . import image_sources


def layer(config, base, target, context, *, dockerfile=None, arguments=()):
    from . import sandbox
    command = [config['engine'], 'build', '-t', target, '--build-arg', f'BASE_IMAGE={base}']
    if dockerfile:
        command += ['-f', str(dockerfile)]
    command += list(arguments) + [str(context)]
    sandbox.run(command)
    return target


def gateway(config):
    from . import sandbox
    local = 'localhost/pi-sandbox-factory-proxy:1'
    return image_sources.ensure(config, 'gateway', local,
        lambda: sandbox.run([config['engine'], 'build', '-t', local, sandbox.ROOT / 'containers/proxy']))


def common(config):
    from . import sandbox, sandbox_extensions
    local = f'localhost/pi-sandbox-factory-base:{sandbox.base_tag(config)}'
    def build():
        pi, *pendant = sandbox.pendant_versions(config) or (sandbox.PI_VERSION,)
        arguments = ['--build-arg', f'USER_UID={config["uid"]}', '--build-arg', f'USER_GID={config["gid"]}',
                     '--build-arg', f'PI_VERSION={pi}']
        if pendant:
            arguments += ['--build-arg', f'PENDANT_VERSION={pendant[0]}']
        with sandbox_extensions.build_contexts(config['common']['path']) as contexts:
            sandbox.run([config['engine'], 'build', '-t', local, *contexts, *arguments, config['common']['path']])
    return image_sources.ensure(config, 'pi-common', local, build)


def native_common(config, target):
    from . import sandbox
    root = sandbox.ROOT / 'containers'
    return image_sources.ensure(config, 'native-common', target, lambda: sandbox.run([
        config['engine'], 'build', '-t', target, '--build-arg', f'USER_UID={config["uid"]}',
        '--build-arg', f'USER_GID={config["gid"]}', '--build-context', f'primary-files={root / "primary"}',
        '--build-context', f'primary-dev={config["common"]["path"]}', root / 'primary-worker']))


def stock_eligible(config):
    from . import sandbox
    # Matching content, not a registration name: edited profiles keep their own build.
    return (config.get('primary_agent', 'pi') == 'pi'
            and config.get('profile', {}).get('digest') == sandbox.tree_digest(sandbox.ROOT / 'profiles/base')
            and image_sources._files(config['profile']['path']) == image_sources._files(sandbox.ROOT / 'profiles/base')
            and not sandbox.pendant_versions(config))


def stock(config, target, cache):
    """All Pi tools, no user configuration, credentials, providers or model weights."""
    from . import sandbox, model_providers, browser_editor
    if not stock_eligible(config):
        raise ValueError('The stock image is built only from Factory’s unchanged bundled base profile.')
    common_path = Path(config['common']['path'])
    base = common(config)
    base = layer(config, base, sandbox.stage_image(target, '-aws'), sandbox.ROOT / 'containers/aws-access/client')
    base = layer(config, base, sandbox.stage_image(target, '-profile'), sandbox.ROOT / 'profiles/base')
    for name, directory, dockerfile in (
            ('browser', 'browser', None), ('web', 'web-access', None),
            ('slack', 'communications', None), ('knowledge', 'local-knowledge', 'Dockerfile.endpoint')):
        context = common_path / directory
        base = layer(config, base, sandbox.stage_image(target, '-' + name), context,
                     dockerfile=context / dockerfile if dockerfile else None)
    empty = copy.deepcopy(config)
    empty['model_providers'] = []
    provider_base = sandbox.stage_image(target, '-providers')
    model_providers.build(empty, base, provider_base)
    platform = image_sources._platform(config['engine'], config.get('podman_connection'))
    arch = 'x86_64' if platform == 'linux/amd64' else platform.split('/')[1]
    archive = browser_editor.checked_archive(Path(cache), arch=arch)
    with tempfile.TemporaryDirectory(prefix='factory-stock-editor-') as temporary:
        context = Path(temporary)
        shutil.copyfile(archive, context / 'code-server.tar.gz')
        recipe = sandbox.ROOT / 'containers/prebuilt/Dockerfile.editor'
        layer(config, provider_base, target, context, dockerfile=recipe,
              arguments=['--build-arg', f'EDITOR_VERSION={browser_editor.VERSION}'])
    return target
