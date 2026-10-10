"""Same-type offline update with owned-service rollback."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid

from .host_platform import image_architecture, venv_python, ensure_private_directory
from .docker_install_paths import skill_destinations, snapshot_mount

from .installation import InstallationError, private_json, read_receipt, state_root
from .docker_install_bundle import verify_bundle
from .docker_install_runtime import compose, docker, ready, refresh_binding, service_ids, write_compose, clear_stopped_legacy_pid, verify_engine, assert_project_owned, require_idle, volume_python
from .docker_install_maintenance import backup, maintenance


def update(profile: dict, path: Path, bundle: Path) -> None:
    manifest = verify_bundle(bundle)
    if manifest['platform'] != profile.get('platform', 'macos-arm64'):
        raise InstallationError('Offline update bundle platform does not match installation')
    if manifest['data_schema'] != profile['data_schema']:
        raise InstallationError('Offline update has an incompatible data format')
    docker('load', '--input', str(bundle / 'image.tar'), context=profile['engine']['context'])
    image = json.loads(docker('image', 'inspect', manifest['image_id'], context=profile['engine']['context']))[0]
    if (image['Id'] != manifest['image_id'] or image['Architecture'] != image_architecture(manifest['platform'])
            or (image.get('Config', {}).get('Labels') or {}).get('io.agentbc.wheel-sha256') != manifest['files'][manifest['wheel']]):
        raise InstallationError('Candidate image identity mismatch')
    old = copy.deepcopy(profile)
    states = {key: value['State']['Running'] for key, value in service_ids(profile).items()}
    root = Path(profile['root'])
    staged = root / 'versions' / uuid.uuid4().hex
    staged.mkdir(parents=True)
    subprocess.run([getattr(sys, '_base_executable', sys.executable), '-m', 'venv', str(staged / 'venv')], check=True)
    subprocess.run([str(venv_python(staged / 'venv')), '-m', 'pip', 'install', '--no-index', '--no-deps',
                    str(bundle / manifest['wheel'])], check=True)
    receipt = read_receipt()
    from .docker_install_supervisor import register
    with maintenance(profile):
        saved = backup(profile, credentials=True)
        skill_paths = list(skill_destinations(profile).values())
        for index, skill in enumerate(skill_paths):
            if skill.exists():
                shutil.copytree(skill, saved / f'skill-{index}', symlinks=True)
        shutil.copytree(root / 'bundle', saved / 'bundle')
        old_venv = root / 'previous-venv'
        if old_venv.exists():
            raise InstallationError('Previous update recovery directory exists; repair before updating')
        receipt['update'] = {'old': old, 'states': states, 'backup': str(saved), 'candidate_image': manifest['image_id']}
        receipt['state'] = 'installing'
        private_json(state_root() / 'installation.json', receipt)
        switched = False
        try:
            register(profile, enable=False)
            compose(profile, 'stop')
            for name in manifest['files']:
                if name == 'image.tar':
                    continue
                target = root / 'bundle' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle / name, target)
            private_json(root / 'bundle/bundle.json', manifest)
            profile.update(image_id=manifest['image_id'], version=manifest['version'],
                           managed_runner_entry=manifest.get('managed_runner_entry', False))
            write_compose(profile)
            compose(profile, 'up', '-d', '--no-build', '--pull', 'never', 'runner')
            compose(profile, 'create', '--no-build', '--pull', 'never', 'webui')
            refresh_binding(profile, path)
            ready(profile)
            (root / 'venv').rename(old_venv)
            # venv scripts embed their original path. Install into the final path
            # instead of moving the staged environment into it.
            subprocess.run([getattr(sys, '_base_executable', sys.executable), '-m', 'venv', str(root / 'venv')], check=True)
            subprocess.run([str(venv_python(root / 'venv')), '-m', 'pip', 'install', '--no-index', '--no-deps',
                            str(bundle / manifest['wheel'])], check=True)
            switched = True
            subprocess.run([str(venv_python(root / 'venv')), '-c',
                            'from pathlib import Path; from agent_bridge_connect.docker_install import load,entry_and_skills; p=Path(__import__("sys").argv[1]); entry_and_skills(load(p),p)', str(path)], check=True)
            register(profile, enable=True)
            if states['webui']:
                compose(profile, 'start', 'webui')
                ready(profile, 'webui')
            if not states['runner']:
                compose(profile, 'stop', 'runner')
            private_json(path, profile)
            receipt.pop('update', None)
            receipt.update(state='installed', version=manifest['version'])
            private_json(state_root() / 'installation.json', receipt)
        except BaseException:
            try:
                rollback_update(receipt)
            except BaseException as rollback_error:
                receipt['state'] = 'repair_required'
                private_json(state_root() / 'installation.json', receipt)
                raise InstallationError('Update rollback failed; installation requires repair') from rollback_error
            raise InstallationError('Update failed; previous installation restored')
        if switched:
            shutil.rmtree(old_venv)
    shutil.rmtree(staged)
    print('Docker update completed; installation type unchanged.')


def validate_restore_archive(path: Path) -> None:
    # backup() always archives "."; stripping that prefix must never drop a file.
    with tarfile.open(path) as archive:
        for member in archive:
            if member.name != '.' and not member.name.startswith('./'):
                raise InstallationError('Unsupported backup member prefix; no services changed')


def rollback_update(receipt: dict) -> None:
    """Resume a journaled rollback after an interrupted offline update."""
    journal = receipt.get('update')
    if not isinstance(journal, dict) or receipt['state'] not in ('installing', 'repair_required'):
        raise InstallationError('No interrupted update journal is available')
    old, states = journal['old'], journal['states']
    root = Path(receipt['root'])
    if (old['installation_id'] != receipt['installation_id'] or old['root'] != str(root)
            or root != Path.home() / '.agentbc-docker'):
        raise InstallationError('Update recovery identity mismatch')
    saved = Path(journal['backup'])
    if not saved.is_relative_to(state_root() / 'backups') or saved.is_symlink():
        raise InstallationError('Invalid private update backup')
    from .docker_install_bundle import digest
    for name, expected in json.loads((saved / 'checksums.json').read_text()).items():
        if Path(name).name != name or digest(saved / name) != expected:
            raise InstallationError('Update backup checksum mismatch; no services changed')
    for name in ('data', 'config', 'profiles'):
        validate_restore_archive(saved / (name + '.tar'))
    verify_engine(old)
    assert_project_owned(old)
    ids = docker('ps', '-aq', '--filter', 'label=io.agentbc.installation=' + old['installation_id'],
                 context=old['engine']['context']).split()
    for container in ids:
        info = json.loads(docker('inspect', container, context=old['engine']['context']))[0]
        if info['Image'] not in (old['image_id'], journal['candidate_image']):
            raise InstallationError('Unexpected image during update recovery')
    require_idle(old)
    path = root / 'docker-host.json'
    old_venv = root / 'previous-venv'
    skill_paths = list(skill_destinations(old).values())
    from .docker_install_supervisor import register
    register(old, enable=False)
    compose(old, 'stop')
    if old_venv.exists():
        if (root / 'venv').exists():
            shutil.rmtree(root / 'venv')
        old_venv.rename(root / 'venv')
    shutil.rmtree(root / 'bundle', ignore_errors=True)
    ensure_private_directory(root / 'bundle')
    shutil.copytree(saved / 'bundle', root / 'bundle', dirs_exist_ok=True)
    for index, skill in enumerate(skill_paths):
        source = saved / f'skill-{index}'
        if source.exists():
            if skill.exists():
                shutil.rmtree(skill)
            shutil.copytree(source, skill, symlinks=True)
    # Candidate has no business permission while maintenance is set;
    # restore volume snapshots before restarting the old image.
    for name in ('data', 'config', 'profiles'):
        archive = saved / (name + '.tar')
        restore = ['docker', '--context', old['engine']['context'], 'run', '--rm', '-i',
                   '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--user', '1000:1000',
                   '--mount', snapshot_mount(old, name),
                   '--entrypoint', 'sh', old['image_id'], '-c',
                   'find /snapshot -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar --strip-components=1 -C /snapshot -xf -']
        with archive.open('rb') as stream:
            subprocess.run(restore, stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    clear_stopped_legacy_pid(old)
    write_compose(old)
    compose(old, 'up', '-d', '--no-build', '--pull', 'never', 'runner')
    compose(old, 'create', '--no-build', '--pull', 'never', 'webui')
    refresh_binding(old, path)
    ready(old)
    if states['webui']:
        compose(old, 'start', 'webui')
    register(old, enable=True)
    if not states['runner']:
        compose(old, 'stop', 'runner')
    volume_python(old, "from pathlib import Path; Path('/data/workspace/record/.agentbc-maintenance').unlink(missing_ok=True)")
    receipt.pop('update', None)
    receipt.update(state='installed', version=old['version'])
    private_json(state_root() / 'installation.json', receipt)
