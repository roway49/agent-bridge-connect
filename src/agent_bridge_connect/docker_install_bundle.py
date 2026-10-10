"""Offline bundle verification and installation bootstrap; no network downloads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .host_platform import bundle_platform, image_architecture, venv_python, ensure_private_directory, remove_owned_entry

from .installation import InstallationError, preflight, private_json, transaction
from .docker_install_runtime import docker, engine_identity


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def verify_bundle(root: Path) -> dict:
    root = root.resolve(strict=True)
    manifest_path = root / 'bundle.json'
    if manifest_path.is_symlink():
        raise InstallationError('Bundle manifest must not be a symlink')
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('schema') != 1 or manifest.get('type') != 'docker'
            or manifest.get('platform') not in ('macos-arm64', 'windows-amd64') or manifest.get('data_schema') != 1
            or not str(manifest.get('image_id', '')).startswith('sha256:')):
        raise InstallationError('Unsupported Docker bundle identity or data format')
    files = manifest.get('files', {})
    scripts = ('install_docker.ps1', 'recover_docker.ps1') if manifest['platform'] == 'windows-amd64' else ('install_docker.sh', 'recover_docker.sh')
    required = {'image.tar', 'seccomp.json', *scripts}
    if not required <= set(files) or manifest.get('wheel') not in files:
        raise InstallationError('Incomplete Docker bundle')
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise InstallationError('Bundle may not contain symlinks')
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise InstallationError('Bundle inventory does not match manifest')
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or digest(root / relative) != expected:
            raise InstallationError(f'Bundle checksum failed: {name}')
    return manifest


def install(bundle: Path, *, interactive: bool = True) -> dict:
    root = Path.home() / '.agentbc-docker'
    previous = preflight('docker', root)
    if previous:
        raise InstallationError('Docker AgentBC already installed; use agentbc update --bundle DIR')
    if root.exists():
        raise InstallationError('Docker installation directory exists; recover or uninstall it first')
    from .docker_install_paths import skill_destinations
    for skill in skill_destinations().values():
        if skill.exists():
            raise InstallationError('Existing AgentBC skill directory must be uninstalled before Docker installation')
    manifest = verify_bundle(bundle)
    if manifest['platform'] != bundle_platform():
        raise InstallationError('Bundle platform does not match this host')
    if manifest['platform'] == 'windows-amd64' and manifest.get('windows_install_contract') != 1:
        raise InstallationError('This Windows bundle predates the managed installation contract; use a new candidate bundle')
    engine = engine_identity()
    own = docker('ps', '-aq', '--filter', 'label=io.agentbc.installation', context=engine['context']).strip()
    if own:
        raise InstallationError('Existing managed Docker services must be uninstalled first')
    needed = (bundle / 'image.tar').stat().st_size * 2 + 1024 ** 3
    if shutil.disk_usage(root.parent).free < needed:
        raise InstallationError('Insufficient disk space for offline image import and rollback')
    with transaction('docker', root, version=manifest['version'], resources={'engine': engine}) as receipt:
        profile = None
        try:
            ensure_private_directory(root)
            # Retain support files and wheel, not a second copy of the large archive.
            support = root / 'bundle'
            ensure_private_directory(support)
            for name in manifest['files']:
                if name == 'image.tar':
                    continue
                target = support / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle / name, target)
            private_json(support / 'bundle.json', manifest)
            docker('load', '--input', str(bundle / 'image.tar'), context=engine['context'])
            image = json.loads(docker('image', 'inspect', manifest['image_id'], context=engine['context']))[0]
            if (image['Id'] != manifest['image_id'] or image['Architecture'] != image_architecture(manifest['platform']) or image['Os'] != 'linux'
                    or (image.get('Config', {}).get('Labels') or {}).get('io.agentbc.wheel-sha256') != manifest['files'][manifest['wheel']]):
                raise InstallationError('Loaded image identity or architecture mismatch')
            subprocess.run([sys.executable, '-m', 'venv', str(root / 'venv')], check=True)
            python = venv_python(root / 'venv')
            subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps',
                            str(support / manifest['wheel'])], check=True)
            profile = {'schema': 2, 'root': str(root), 'installation_id': receipt['installation_id'],
                       'project': 'agentbc-' + receipt['installation_id'][:12], 'engine': engine,
                       'platform': manifest['platform'], 'image_id': image['Id'], 'version': manifest['version'], 'data_schema': 1,
                       'managed_runner_entry': manifest.get('managed_runner_entry', False),
                       'compose': str(root / 'compose.json'), 'projects': [], 'settings': {},
                       'available_executors': manifest.get('available_executors'),
                       'skill_paths': {name: str(path) for name, path in skill_destinations().items()}}
            private_json(root / 'docker-host.json', profile)
            command = [str(python), '-m', 'agent_bridge_connect.docker_install', 'initialize', '--profile',
                       str(root / 'docker-host.json')]
            if not interactive:
                command.append('--non-interactive')
            subprocess.run(command, check=True)
            receipt['resources'].update({'profile': str(root / 'docker-host.json'), 'project': profile['project']})
        except BaseException:
            try:
                rollback_install(receipt, root, profile)
                receipt['rollback_complete'] = True
            except BaseException:
                pass  # The transaction keeps repair_required and exact ownership.
            raise
    return {'ok': True, 'installation_type': 'docker', 'root': str(root), 'image_id': image['Id']}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--non-interactive', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.bundle.resolve(), interactive=not args.non_interactive), indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'install_error: {exc}', file=sys.stderr)
        return 2


def rollback_install(receipt: dict, root: Path, profile: dict | None) -> None:
    """Remove only resources created by this failed first installation."""
    from .docker_install_runtime import LABEL, service_ids, require_idle
    if (root / 'docker-host.json').exists():
        from .docker_install import load
        saved = load(root / 'docker-host.json')
        if saved['installation_id'] != receipt['installation_id']:
            raise InstallationError('Rollback profile identity changed')
        profile = saved
    if profile:
        services = service_ids(profile)
        if services:
            require_idle(profile)
        from .docker_install_supervisor import register
        register(profile, enable=False)
        for info in services.values():
            docker('rm', '-f', info['Id'], context=profile['engine']['context'])
        networks = docker('network', 'ls', '-q', '--filter', f'label={LABEL}={receipt["installation_id"]}',
                          context=profile['engine']['context']).split()
        for network in networks:
            docker('network', 'rm', network, context=profile['engine']['context'])
    # Data volumes and imported configuration remain private recovery material.
    remove_owned_entry(root)
    from .docker_install_paths import skill_destinations
    for name, skill in skill_destinations(profile).items():
        if skill.exists():
            # Initial preflight proved these destinations absent. Back them up
            # rather than discarding potential edits made during installation.
            saved = Path.home() / '.agentbc-install/backups' / receipt['installation_id'] / name
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(skill), saved)
    if root.exists():
        shutil.rmtree(root)


if __name__ == '__main__':
    raise SystemExit(main())
