"""User-wide installation identity and serialization, independent of install prefix."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from .host_platform import ensure_private_directory, validate_private, windows_private, try_lock


class InstallationError(ValueError):
    """Fail closed before modifying an installation of unknown ownership."""


def state_root() -> Path:
    return Path.home() / '.agentbc-install'


def private_json(path: Path, value: dict) -> None:
    if os.name == 'nt':
        ensure_private_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise InstallationError(f'Refusing symlink: {path}')
    fd, temporary = tempfile.mkstemp(prefix='.transaction-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if os.name == 'nt':
                windows_private(Path(temporary), create_acl=True)
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_receipt() -> dict | None:
    path = state_root() / 'installation.json'
    if not path.exists() and not path.is_symlink():
        return None
    try:
        validate_private(path)
    except ValueError as exc:
        raise InstallationError('Installation receipt must be an owner-only regular file') from exc
    try:
        value = json.loads(path.read_text())
        if (value['schema'] != 1 or value['type'] not in ('native', 'docker')
                or value['state'] not in ('installing', 'installed', 'uninstalling', 'repair_required')
                or not value['installation_id'] or not Path(value['root']).is_absolute()):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallationError('Invalid installation receipt; repair before installing') from exc


@contextmanager
def installation_lock(*, wait_seconds: float = 0, shared: bool = False):
    root = state_root()
    try:
        ensure_private_directory(root)
    except ValueError as exc:
        raise InstallationError('Installation state directory must be private and owned by this user') from exc
    lock_path = root / 'lock'
    existed = lock_path.exists()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.name == 'nt' and not existed:
            windows_private(lock_path, create_acl=True)
        validate_private(lock_path)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                try_lock(fd, shared=shared)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise InstallationError('Another installation or maintenance operation is active') from exc
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _entry_type(path: Path) -> str:
    try:
        body = path.read_bytes()[:8192]
    except OSError:
        return 'unknown'
    if b'agent_bridge_connect.docker_' in body or b'AgentBC Docker host entry' in body:
        return 'docker'
    if b'agent_bridge_connect' in body or '.agentbc-alpha' in str(path.resolve()):
        return 'native'
    return 'unknown'


def shared_entries() -> tuple[Path, ...]:
    if os.name == 'nt':
        return ()
    return (Path('/opt/homebrew/bin/agentbc'), Path('/usr/local/bin/agentbc'))


def discover() -> list[dict]:
    """Inspect known entry points without executing an untrusted candidate CLI."""
    home = Path.home()
    candidates = {home / '.local/bin/agentbc', home / '.agentbc-alpha/venv/bin/agentbc',
                  home / '.agentbc-docker/bin/agentbc', *shared_entries()}
    candidates.update(Path(p) / 'agentbc' for p in os.environ.get('PATH', '').split(os.pathsep) if p)
    candidates.add(Path(os.environ.get('AGENTBC_ALPHA_HOME', home / '.agentbc-alpha')) / 'venv/bin/agentbc')
    candidates.add(Path(os.environ.get('AGENTBC_BIN_DIR', home / '.local/bin')) / 'agentbc')
    if os.name == 'nt':
        candidates.update(path.with_suffix(suffix) for path in list(candidates) for suffix in ('.exe', '.cmd', '.bat', '.ps1'))
        for root in (home / '.agentbc-alpha', home / '.agentbc-docker'):
            candidates.add(root / 'venv/Scripts/agentbc.exe')
    found, seen = [], set()
    for path in sorted(candidates):
        if not path.exists() and not path.is_symlink():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append({'type': _entry_type(path), 'path': str(resolved), 'source': 'entry'})
    for kind, root, entry in (
        ('native', Path(os.environ.get('AGENTBC_ALPHA_HOME', home / '.agentbc-alpha')), 'venv/bin/agentbc'),
        ('docker', home / '.agentbc-docker', 'bin/agentbc'),
    ):
        if os.name == 'nt':
            entry = 'venv/Scripts/agentbc.exe' if kind == 'native' else 'bin/agentbc.cmd'
        if not (root / entry).exists() and any((root / name).exists() for name in ('venv', 'docker-host.json', 'compose.json')):
            found.append({'type': kind, 'path': str(root.resolve()), 'source': 'partial_runtime'})
    from .docker_install_paths import skill_destinations
    for destination in skill_destinations().values():
        skill = destination / 'SKILL.md'
        if skill.exists():
            body = skill.read_bytes()
            found.append({'type': 'docker' if b'Docker host binding' in body else 'native',
                          'path': str(skill), 'source': 'skill'})
    for path in (home / 'Library/LaunchAgents').glob('*agentbc*.plist'):
        found.append({'type': 'docker' if 'docker' in path.name else 'native',
                      'path': str(path), 'source': 'service'})
    if os.name == 'nt':
        from .windows_desktop import installed_tasks
        found.extend({'type': 'docker', 'path': name, 'source': 'service'} for name in installed_tasks())
    return found


def discover_managed_docker() -> list[str]:
    if not shutil.which('docker'):
        return []
    try:
        result = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=io.agentbc.installation'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError('Cannot complete Docker installation discovery; make Docker available and retry') from exc
    if result.returncode:
        raise InstallationError('Cannot complete Docker installation discovery; make Docker available and retry')
    return result.stdout.split()


def preflight(kind: str, root: Path) -> dict | None:
    if kind not in ('native', 'docker'):
        raise InstallationError('Unknown installation type')
    receipt = read_receipt()
    if receipt:
        if receipt['type'] != kind:
            raise InstallationError(f"{receipt['type'].title()} AgentBC is already installed. Uninstall it before installing {kind.title()}.")
        if receipt['state'] != 'installed':
            raise InstallationError(f"Installation is {receipt['state']}; finish recovery or uninstall first")
        if Path(receipt['root']).resolve() != root.resolve():
            raise InstallationError('Same-type update must use the existing installation root')
    for evidence in discover():
        if receipt and kind == 'docker' and evidence['source'] == 'entry':
            candidate = Path(evidence['path'])
            managed = root / 'bin/agentbc.cmd'
            public = Path(os.environ.get('AGENTBC_BIN_DIR', Path.home() / '.local/bin')) / 'agentbc.cmd'
            if candidate == (root / 'venv/Scripts/agentbc.exe').resolve():
                continue
            if candidate == public.resolve() and managed.is_file() and candidate.read_bytes() == managed.read_bytes():
                continue
        if kind == 'docker' and not receipt and evidence['source'] in ('skill', 'service'):
            raise InstallationError(f"Existing AgentBC {evidence['source']} at {evidence['path']}; uninstall its owner first")
        if evidence['source'] == 'partial_runtime':
            raise InstallationError(f"Partial AgentBC runtime at {evidence['path']}; recover or uninstall it first")
        if evidence['type'] != kind:
            raise InstallationError(f"Conflicting or unknown AgentBC installation at {evidence['path']}; uninstall it first")
        if evidence['source'] == 'entry' and not Path(evidence['path']).is_relative_to(root.resolve()):
            raise InstallationError(f"Another AgentBC entry exists at {evidence['path']}; uninstall it first")
    docker_ids = discover_managed_docker()
    if docker_ids and (not receipt or receipt['type'] != 'docker'):
        raise InstallationError('Managed Docker services exist; uninstall the owning installation first')
    return receipt


@contextmanager
def transaction(kind: str, root: Path, *, version: str, resources: dict | None = None):
    preflight(kind, root)
    with installation_lock():
        old = preflight(kind, root)
        receipt = {**(old or {}), 'schema': 1, 'type': kind, 'state': 'installing',
                   'installation_id': (old or {}).get('installation_id', uuid.uuid4().hex),
                   'root': str(root.resolve()), 'version': version, 'resources': resources or {}}
        private_json(state_root() / 'installation.json', receipt)
        try:
            yield receipt
        except BaseException:
            if receipt.pop('rollback_complete', False):
                (state_root() / 'installation.json').unlink()
            else:
                receipt['state'] = 'repair_required'
                private_json(state_root() / 'installation.json', receipt)
            raise
        else:
            receipt['state'] = 'installed'
            private_json(state_root() / 'installation.json', receipt)


def _native_script(wheel: Path, script: Path, argv: list[str]) -> int:
    """Run the native shell payload under a parent-owned installation lock."""
    from . import __version__
    root = Path(os.environ.get('AGENTBC_ALPHA_HOME', Path.home() / '.agentbc-alpha'))
    resources = {'entry': str(Path(os.environ.get('AGENTBC_BIN_DIR', Path.home() / '.local/bin')).resolve() / 'agentbc'),
                 'config': str(Path(os.environ.get('AGENTBC_CONFIG_PATH', Path.home() / '.abc/config.toml')).expanduser().resolve())}
    with transaction('native', root, version=__version__, resources=resources):
        # The payload is the same script minus its bootstrap section. It cannot
        # start another guard, and no environment variable can bypass the guard.
        text = script.read_text()
        marker = '# AGENTBC_NATIVE_PAYLOAD\n'
        if text.count(marker) != 1:
            raise InstallationError('Invalid native installer payload')
        payload = text.split(marker, 1)[1]
        result = subprocess.run(['sh', '-c', 'set -eu\n' + payload, str(script), str(wheel), *argv])
        if result.returncode:
            raise InstallationError('Native installation failed; installation requires repair')
    return 0


def native_script(wheel: Path, script: Path, argv: list[str]) -> int:
    import sys
    try:
        return _native_script(wheel, script, argv)
    except (InstallationError, OSError, subprocess.SubprocessError) as exc:
        print(f'install_error: {exc}', file=sys.stderr)
        return 2


def installation_view() -> dict:
    try:
        receipt = read_receipt()
        if receipt:
            return {key: receipt[key] for key in ('type', 'state', 'root', 'version', 'installation_id')}
        return {'type': 'native', 'state': 'unregistered'}
    except InstallationError as exc:
        return {'type': 'unknown', 'state': 'repair_required', 'error': str(exc)}
