"""Frozen host paths shared by installation, setup and maintenance."""
from __future__ import annotations

from pathlib import Path

from .installation import InstallationError


def skill_destinations(profile: dict | None = None) -> dict[str, Path]:
    saved = (profile or {}).get('skill_paths')
    if saved is not None:
        if set(saved) != {'codex', 'claude', 'hermes'} or any(not Path(p).is_absolute() for p in saved.values()):
            raise InstallationError('Invalid saved skill destinations')
        return {name: Path(path) for name, path in saved.items()}
    from .setup import _hermes_skill_path, _claude_skill_path, _codex_skill_root
    return {'codex': _codex_skill_root(),
            'claude': _claude_skill_path().parent, 'hermes': _hermes_skill_path().parent}


def initialize_workspace(profile: dict, *, interactive: bool) -> None:
    if profile.get('platform') != 'windows-amd64' or profile.get('host_workspace'):
        return
    from .docker_preview_setup import documents_workspace
    from .host_platform import ensure_private_directory, _reject_reparse
    default = documents_workspace()
    print(f'Default AgentBC workspace: {default}')
    while True:
        try:
            value = input(f'Workspace folder [Enter={default}]: ').strip() if interactive else ''
        except EOFError as exc:
            raise InstallationError('Setup input ended before workspace selection') from exc
        path = Path(value).expanduser() if value else default
        if not path.is_absolute():
            raise InstallationError('Workspace must be an absolute local folder')
        _reject_reparse(path)
        path = path.resolve()
        if path in (Path.home().resolve(), Path(path.anchor)) or (path.exists() and not path.is_dir()):
            raise InstallationError('Choose a workspace directory, not your home or filesystem root')
        if not path.exists() or not any(path.iterdir()):
            break
        if not interactive:
            raise InstallationError('Workspace must be empty for a clean installation. Preserve the old folder as a backup, then rerun installation; old tasks are never imported automatically.')
        from .docker_install_setup import choose
        from datetime import datetime
        archive = path.with_name(path.name + '-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        print(f'Existing workspace files found: {path}\nA new installation cannot resume old task state.')
        print(f'Archive destination: {archive}\n1) Preserve this folder as an archive and create a new workspace\n2) Choose another folder\n3) Cancel')
        selected = choose('Workspace action [1/2/3; default: 2]: ', '2', ('1', '2', '3'))
        if selected == '3':
            raise InstallationError('Workspace selection cancelled; existing files were preserved')
        if selected == '2':
            continue
        from .installation_native import assert_idle_board
        assert_idle_board(path / 'record')
        if archive.exists():
            raise InstallationError('Workspace archive already exists; choose another location')
        path.rename(archive)
        print(f'Previous workspace preserved: {archive}')
        break
    ensure_private_directory(path)
    profile['host_workspace'] = str(path)


def data_mount(profile: dict, *, readonly: bool = False, target: str = '/data/workspace') -> dict:
    source = profile.get('host_workspace')
    mount = {'type': 'bind', 'source': source, 'target': target, 'bind': {'create_host_path': False}} if source else {'type': 'volume', 'source': 'data', 'target': target}
    if readonly:
        mount['read_only'] = True
    return mount


def snapshot_mount(profile: dict, name: str, *, readonly: bool = False, target: str = '/snapshot') -> str:
    if name == 'data' and profile.get('host_workspace'):
        from .host_platform import _reject_reparse
        path = Path(profile['host_workspace'])
        _reject_reparse(path)
        if not path.is_absolute() or not path.is_dir() or ',' in str(path):
            raise InstallationError('Workspace binding is missing or unsupported')
        source = 'type=bind,src=' + str(path)
    else:
        source = f'type=volume,src={profile["project"]}_{name}'
    return source + ',dst=' + target + (',readonly' if readonly else '')


def windows_entry(profile: dict, path: Path) -> str:
    """Maintenance runs outside the venv so Windows can remove its executable."""
    import json
    import sys
    from .host_platform import venv_python
    root = Path(profile['root'])
    bundle = root / 'bundle'
    manifest = json.loads((bundle / 'bundle.json').read_text())
    wheel = bundle / manifest['wheel']
    python = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    if python.is_relative_to(root.resolve()):
        raise InstallationError('Maintenance requires a base Python outside the managed installation')
    for value in (root, path, wheel, python):
        if any(c in str(value) for c in '%!"\r\n'):
            raise InstallationError('Installation path contains unsupported command characters')
    # -c code contains no caller-provided text; paths are individual quoted argv.
    code = "import sys,tempfile,shutil,atexit,pathlib;t=tempfile.TemporaryDirectory(prefix='agentbc-maintenance-');atexit.register(t.cleanup);w=pathlib.Path(sys.argv.pop(1));p=pathlib.Path(t.name)/w.name;shutil.copyfile(w,p);sys.path.insert(0,str(p));from agent_bridge_connect.docker_install import main;raise SystemExit(main())"
    normal = f'"{venv_python(root / "venv")}" -m agent_bridge_connect.docker_install run --profile "{path}" -- %*'
    maintenance = f'"{python}" -c "{code}" "{wheel}" run --profile "{path}" -- %*'
    return ('@echo off\n@chcp 65001 >nul\nrem AgentBC Docker host entry\n'
            'if /I "%~1"=="uninstall" goto maintenance\n'
            'if /I "%~1"=="update" goto maintenance\n' + normal + '\nexit /b %errorlevel%\n'
            # Leave the batch context before invoking code that can delete it.
            # Only the deliberate context-exit diagnostic is redirected; the
            # Python command retains its own stderr and real exit status.
            ':maintenance\n(goto) 2>nul & ' + maintenance + '\n')
