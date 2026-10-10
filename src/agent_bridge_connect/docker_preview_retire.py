"""Explicit, identity-checked retirement of the Windows acceptance preview."""
from __future__ import annotations

import json
import hashlib
from contextlib import ExitStack
from pathlib import Path
import shutil
import time

from .installation import InstallationError, installation_lock, private_json, state_root
from .host_platform import ensure_private_directory, validate_private


def retire(path: Path, *, confirmed: bool = False) -> dict:
    from .docker_preview_setup import verify
    from .docker_approval_bridge import execute
    from .windows_desktop import register, desktop_engine
    from .installation_native import assert_idle_board
    from .docker_install_paths import skill_destinations
    path = path.expanduser().absolute()
    validate_private(path)
    profile = json.loads(path.read_text(encoding='utf-8'))
    if profile.get('schema') != 1 or not profile.get('setup_binding'):
        raise InstallationError('Only an explicitly bound setup preview can be retired')
    workspace = Path(profile.get('host_workspace', ''))
    if not workspace.is_absolute():
        raise InstallationError('Preview workspace is unavailable')
    from .host_platform import _reject_reparse
    _reject_reparse(workspace)
    root = path.parent
    entry = root / 'bin/agentbc.cmd'
    public = Path.home() / '.local/bin/agentbc.cmd'
    journal_path = root / 'preview-uninstall.json'
    with installation_lock(), ExitStack() as locks:
        from .host_platform import try_lock
        lock = locks.enter_context((root / "preview-setup.lock").open("a+b"))
        try_lock(lock.fileno())
        if journal_path.exists():
            validate_private(journal_path)
            journal = json.loads(journal_path.read_text())
            if journal['profile'] != profile:
                raise InstallationError('Preview uninstall identity changed')
        else:
            if not workspace.is_dir():
                raise InstallationError('Preview workspace is unavailable')
            _, spec = verify(profile)
            assert_idle_board(workspace / 'record')
            if not entry.is_file() or b'agent_bridge_connect.docker_host' not in entry.read_bytes():
                raise InstallationError('Preview CLI ownership is unavailable')
            if public.exists() and public.read_bytes() != entry.read_bytes():
                raise InstallationError('Public CLI belongs to another installation')
            skills = []
            for name, target in skill_destinations().items():
                if target.exists():
                    body = (target / 'SKILL.md').read_bytes()
                    if b'Use the installed agentbc host CLI for this Windows Docker preview.' not in body:
                        raise InstallationError('A skill belongs to another installation')
                    skills.append((name, str(target)))
            saved = state_root() / 'backups' / ('windows-preview-' + time.strftime('%Y%m%d-%H%M%S'))
            archive = workspace.with_name('workspace-preview-backup-' + time.strftime('%Y%m%d-%H%M%S'))
            if saved.exists() or archive.exists():
                raise InstallationError('Preview backup path already exists')
            print(f'Preview to remove: {root}\nTask history to preserve: {workspace}\nHistory archive: {archive}\nConfiguration/skill backup: {saved}')
            print('Only this preview Runner, WebUI, desktop task and CLI/skills will be removed. Docker, images and credential volumes are kept.')
            if not confirmed and input('Back up and uninstall this preview? [y/N] ').strip().lower() not in ('y', 'yes'):
                return {'ok': True, 'state': 'cancelled'}
            marker = workspace / 'record/.agentbc-maintenance'
            marker_body = json.dumps({'active': True, 'reason': 'preview uninstall', 'profile': str(path)})
            if marker.exists() and marker.read_text(encoding='utf-8') != marker_body:
                raise InstallationError('Existing business maintenance must be resolved first')
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(marker_body, encoding='utf-8')
            assert_idle_board(workspace / 'record')
            # Freeze bridge state/log writes before verifying the host backup.
            # No program or business data is removed until the backup passes.
            register(path, enable=False)
            ensure_private_directory(saved)
            checked_copy(root, saved / 'host', exclude=('preview-setup.lock',))
            for name, target in skills:
                checked_copy(Path(target), saved / ('skill-' + name))
            journal = {'profile': profile, 'project': spec['name'], 'backup': str(saved), 'archive': str(archive), 'skills': skills,
                       'entry_sha256': hashlib.sha256(entry.read_bytes()).hexdigest()}
            private_json(journal_path, journal)
        if desktop_engine() != profile['setup_binding']['engine']:
            raise InstallationError('Preview Docker engine changed')
        # Missing owned containers are allowed on a repeat; foreign containers are not.
        ids = execute(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + journal['project']]).split()
        for cid in ids:
            expected = profile['setup_binding']['containers'].get(cid)
            info = json.loads(execute(['docker', 'inspect', cid]))[0]
            if not expected or any(info[key] != expected[field] for key, field in [('Image', 'image'), ('Mounts', 'mounts')]) or info['Config']['Labels'] != expected['labels']:
                raise InstallationError('Foreign or changed preview container; no teardown performed')
        marker = workspace / 'record/.agentbc-maintenance'
        marker_body = json.dumps({'active': True, 'reason': 'preview uninstall', 'profile': str(path)})
        if marker.exists() and marker.read_text(encoding='utf-8') != marker_body:
            raise InstallationError('Existing business maintenance must be resolved first')
        if workspace.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(marker_body, encoding='utf-8')
        elif not Path(journal['archive']).is_dir():
            raise InstallationError('Both workspace and history archive are missing')
        assert_idle_board(workspace / 'record')
        register(path, enable=False)
        for cid in ids:
            execute(['docker', 'rm', '-f', cid])
        for name, target in journal['skills']:
            target = Path(target)
            if target.exists():
                if target.is_symlink():
                    raise InstallationError('Skill ownership changed during uninstall')
                # Preserve modifications made since the first backup as well.
                saved = Path(journal['backup']) / ('skill-final-' + name)
                checked_copy(target, saved)
                shutil.rmtree(target)
        if public.exists():
            if hashlib.sha256(public.read_bytes()).hexdigest() != journal['entry_sha256']:
                raise InstallationError('Public CLI ownership changed')
            public.unlink()
        marker.unlink(missing_ok=True)
        if workspace.exists():
            if Path(journal['archive']).exists():
                raise InstallationError('History archive already exists; refusing overwrite')
            workspace.rename(journal['archive'])
        locks.close()
        # Preserve the development venv and other caches; only the owned host
        # entry/profile directory is retired. They no longer register an install.
        shutil.rmtree(root)
        return {'ok': True, 'state': 'uninstalled', 'backup': journal['backup'], 'history': journal['archive'], 'images_retained': True}


def checked_copy(source: Path, target: Path, *, exclude: tuple[str, ...] = ()) -> None:
    """Verify every regular file before removing any source; reject linked paths."""
    from .host_platform import _reject_reparse
    def inventory(root):
        values = {}
        for item in root.rglob('*'):
            if item.relative_to(root).as_posix() in exclude:
                continue
            _reject_reparse(item)
            if item.is_file():
                values[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
        return values
    before = inventory(source)
    if not target.exists():
        shutil.copytree(source, target, ignore=lambda directory, names: [name for name in names if (Path(directory) / name).relative_to(source).as_posix() in exclude])
    if inventory(target) != before or inventory(source) != before:
        raise InstallationError('Backup verification failed; source was preserved')
