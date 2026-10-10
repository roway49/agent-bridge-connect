"""Backup and uninstall of an owned Docker installation, preserving data by default."""
from __future__ import annotations

from .host_platform import remove_owned_entry, ensure_private_directory, windows_private
import os
from .docker_install_paths import skill_destinations, snapshot_mount

from contextlib import contextmanager
import copy
from pathlib import Path
import shutil
import subprocess
import time

from .installation import InstallationError, private_json, read_receipt, state_root
from .docker_install_runtime import docker, require_idle, verify_binding, volume_python, assert_project_owned, service_ids


@contextmanager
def maintenance(profile: dict):
    verify_binding(profile)
    cleanup_profile = copy.deepcopy(profile)
    marker = '/data/workspace/record/.agentbc-maintenance'
    code = "import os,json; from pathlib import Path; p=Path(%r); p.parent.mkdir(parents=True,exist_ok=True); f=os.fdopen(os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w'); json.dump({'active':True,'reason':'installation maintenance'},f); f.close()" % marker
    volume_python(profile, code)
    try:
        require_idle(profile)
        yield
    finally:
        current = read_receipt()
        if not current or current.get('state') != 'uninstalling':
            volume_python(cleanup_profile, f'from pathlib import Path; Path({marker!r}).unlink(missing_ok=True)')


def backup(profile: dict, *, credentials: bool = False) -> Path:
    target = state_root() / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + profile['installation_id'][:8])
    if target.exists():
        raise InstallationError('Backup directory already exists')
    ensure_private_directory(target)
    private_json(target / 'installation.json', profile)
    for name in ('data', 'config', *(['profiles'] if credentials else [])):
        file = target / (name + '.tar')
        command = ['docker', '--context', profile['engine']['context'], 'run', '--rm', '--network', 'none',
                   '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--user', '1000:1000', '--mount', snapshot_mount(profile, name, readonly=True),
                   '--entrypoint', 'tar', profile['image_id'], '--format=pax', '-C', '/snapshot', '-cf', '-', '.']
        with file.open('xb') as stream:
            file.chmod(0o600)
            if os.name == 'nt':
                windows_private(file, create_acl=True)
            result = subprocess.run(command, stdout=stream, stderr=subprocess.DEVNULL, timeout=180)
        if result.returncode:
            raise InstallationError('Backup failed; installed services and data were retained')
    from .docker_install_bundle import digest
    private_json(target / 'checksums.json', {p.name: digest(p) for p in target.iterdir() if p.is_file()})
    return target


def uninstall(profile: dict, *, remove_records: bool = False, remove_artifacts: bool = False,
              keep_credentials: bool = False) -> dict:
    from .docker_install_supervisor import register
    root = Path(profile['root'])
    receipt = read_receipt()
    if not receipt or receipt['installation_id'] != profile['installation_id']:
        raise InstallationError('Uninstall receipt does not match installation')
    if receipt['state'] != 'uninstalling':
        with maintenance(profile):
            saved = backup(profile, credentials=keep_credentials)
            receipt['state'] = 'uninstalling'
            receipt['uninstall'] = {'backup': str(saved), 'remove_records': remove_records,
                                    'remove_artifacts': remove_artifacts, 'keep_credentials': keep_credentials}
            private_json(state_root() / 'installation.json', receipt)
    journal = receipt.get('uninstall')
    if not journal:
        raise InstallationError('Uninstall journal missing; repair installation identity first')
    saved = Path(journal['backup'])
    if not saved.is_dir() or saved.is_symlink():
        raise InstallationError('Verified uninstall backup is unavailable')
    assert_project_owned(profile)
    require_idle(profile)
    # Repeatable after partial teardown: never rely on a remaining Runner PID.
    register(profile, enable=False)
    for info in service_ids(profile).values():
        docker('rm', '-f', info['Id'], context=profile['engine']['context'])
    targets = (['/data/workspace/record', '/data/workspace/tasks/report'] if journal['remove_records'] else [])
    targets += ['/data/workspace/tasks/artifacts'] if journal['remove_artifacts'] else []
    if targets:
        volume_python(profile, 'import shutil; from pathlib import Path; '
                      + f'[shutil.rmtree(p) for p in {targets!r} if Path(p).exists()]')
    for name, skill in skill_destinations(profile).items():
        if skill.exists():
            if skill.is_symlink():
                raise InstallationError('Uninstall refuses a symlinked skill directory')
            destination = saved / ('skill-' + name)
            if destination.exists():
                destination = saved / ('skill-' + name + '-' + str(time.time_ns()))
            shutil.copytree(skill, destination, symlinks=True)
            shutil.rmtree(skill)
    remove_owned_entry(root)
    networks = docker('network', 'ls', '-q', '--filter',
                      'label=io.agentbc.installation=' + profile['installation_id'],
                      context=profile['engine']['context']).split()
    for network in networks:
        docker('network', 'rm', network, context=profile['engine']['context'])
    # Runtime authentication copies are not retained unless explicitly selected.
    if not journal['keep_credentials']:
        for name in ('profiles', 'runtime'):
            volume = profile['project'] + '_' + name
            existing = docker('volume', 'ls', '-q', '--filter', 'name=^' + volume + '$',
                              context=profile['engine']['context']).split()
            for found in existing:
                docker('volume', 'rm', found, context=profile['engine']['context'])
    if service_ids(profile):
        raise InstallationError('Owned containers remain; uninstall remains incomplete')
    volume_python(profile, "from pathlib import Path; Path('/data/workspace/record/.agentbc-maintenance').unlink(missing_ok=True)")
    shutil.rmtree(root)
    (state_root() / 'installation.json').unlink()
    return {'ok': True, 'installation_type': 'docker', 'backup': str(saved),
            'data_retained': True, 'image_retained': True, 'credentials_retained': journal['keep_credentials']}
