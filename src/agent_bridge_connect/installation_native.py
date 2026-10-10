"""Native maintenance guard and private pre-uninstall backup."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time

from .installation import InstallationError, discover, installation_lock, private_json, read_receipt, state_root


def assert_native() -> dict | None:
    receipt = read_receipt()
    if receipt and receipt['type'] != 'native':
        raise InstallationError('Docker AgentBC is installed; use its uninstaller before installing or maintaining Native')
    if any(item['type'] == 'docker' for item in discover()):
        raise InstallationError('Docker installation detected; native maintenance refused')
    return receipt


@contextmanager
def native_environment(receipt):
    values = {}
    if receipt:
        values['AGENTBC_ALPHA_HOME'] = receipt['root']
        resources = receipt.get('resources', {})
        if resources.get('entry'):
            values['AGENTBC_BIN_DIR'] = str(Path(resources['entry']).parent)
        if resources.get('config'):
            values['AGENTBC_CONFIG_PATH'] = resources['config']
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def uninstall(payload, *, package_remove=None, **kwargs):
    from .setup import _config_path, _effective_workspace_root, _load_config
    # Validate destructive choices before entering maintenance or backing up.
    if not kwargs.get('interactive', True) and (kwargs.get('remove_records') is None or kwargs.get('remove_artifacts') is None):
        return payload(**kwargs)
    with installation_lock():
        receipt = assert_native()
        with native_environment(receipt):
            config_path = _config_path()
            config = _load_config(config_path)
            workspace = _effective_workspace_root(config)
            board = Path(config.get('board_root') or workspace / 'record').expanduser().resolve()
            board.mkdir(parents=True, exist_ok=True)
            marker = board / '.agentbc-maintenance'
            if marker.exists():
                raise InstallationError('Existing business maintenance must be resolved before uninstall')
            marker.write_text(json.dumps({'active': True, 'reason': 'native uninstall'}))
            try:
                assert_idle_board(board)
                backup = state_root() / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-native')
                backup.mkdir(parents=True, mode=0o700, exist_ok=False)
                if config_path.exists():
                    shutil.copy2(config_path, backup / 'config.toml')
                    (backup / 'config.toml').chmod(0o600)
                for name in ('codex', 'claude', 'hermes'):
                    skill = Path.home() / f'.{name}/skills/agentbc'
                    if skill.exists():
                        shutil.copytree(skill, backup / ('skill-' + name), symlinks=True)
                private_json(backup / 'retained-data.json', {'board': str(board), 'workspace': str(workspace),
                                                          'records': 'preserved in place unless explicitly removed'})
                if receipt:
                    receipt['state'] = 'uninstalling'
                    private_json(state_root() / 'installation.json', receipt)
                result = payload(**kwargs)
                if package_remove is not None:
                    package_remove()
                root = Path(os.environ.get('AGENTBC_ALPHA_HOME', Path.home() / '.agentbc-alpha'))
                if root.exists():
                    raise InstallationError('Native runtime remains; uninstall incomplete')
                if receipt:
                    (state_root() / 'installation.json').unlink()
                result['backup'] = str(backup)
                return result
            finally:
                marker.unlink(missing_ok=True)


def guarded_update(payload, release):
    with installation_lock():
        receipt = assert_native()
        if receipt and receipt['state'] != 'installed':
            raise InstallationError('Native installation requires repair before update')
        with native_environment(receipt):
            result = payload(release)
        if receipt:
            receipt['version'] = result['version']
            private_json(state_root() / 'installation.json', receipt)
        return result


def assert_idle_board(board: Path) -> None:
    """Recognize legacy terminal labels without relaxing live lease checks."""
    from .terminal_states import TASK_TERMINAL_STATES
    terminal = TASK_TERMINAL_STATES | {'cancelled', 'rejected'}
    blockers = []
    for path in sorted(board.glob('*/*/task.json')):
        task = json.loads(path.read_text())
        if task.get('status') not in terminal:
            blockers.append(f"{task.get('id', path.parent.name)} ({task.get('status', 'unknown')})")
    for path in sorted(board.glob('*/*/run_lease.json')):
        lease = json.loads(path.read_text())
        if lease.get('state') != 'closed':
            blockers.append(f"{lease.get('task_id', path.parent.name)} (lease={lease.get('state', 'unknown')})")
    if blockers:
        raise InstallationError('Unfinished tasks or leases prevent uninstall: ' + '; '.join(blockers)
                                + '. Resolve them through task commands; records were not deleted.')
