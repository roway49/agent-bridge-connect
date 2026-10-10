"""Explicit recovery entry from an offline bundle, independent of the installed CLI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import os
import subprocess
import sys

from .installation import InstallationError, installation_lock, read_receipt, state_root


def uninstall_native() -> dict:
    from .installation import discover
    from .installation_native import uninstall
    from .setup import _run_uninstall_payload
    root = Path(os.environ.get('AGENTBC_ALPHA_HOME', Path.home() / '.agentbc-alpha')).resolve()
    external = [Path(item['path']) for item in discover()
                if item['source'] == 'entry' and not Path(item['path']).is_relative_to(root)]
    remove = None
    if external:
        brew = shutil.which('brew')
        if not brew or any('/Cellar/agentbc/' not in str(path) for path in external):
            raise InstallationError('Unmanaged native CLI detected; uninstall it with its owning package manager first')
        prefix = subprocess.check_output([brew, '--prefix', 'agentbc'], text=True, timeout=15).strip()
        if not all(path.is_relative_to(Path(prefix).resolve()) for path in external):
            raise InstallationError('Homebrew identity does not match the discovered native CLI')
        def remove():
            subprocess.run([brew, 'uninstall', '--formula', 'agentbc'], check=True,
                           env={**os.environ, 'HOMEBREW_NO_AUTO_UPDATE': '1'})
    return uninstall(_run_uninstall_payload, interactive=True, remove_records=None,
                     remove_artifacts=None, package_remove=remove)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('status', 'uninstall', 'uninstall-native', 'uninstall-preview', 'rollback'))
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    try:
        receipt = read_receipt()
        if args.action == 'uninstall-preview':
            if receipt or args.profile is None:
                raise InstallationError('Preview retirement requires --profile and no formal installation receipt')
            from .docker_preview_retire import retire
            print(json.dumps(retire(args.profile), indent=2))
            return 0
        if args.action == 'status':
            print(json.dumps(receipt, indent=2))
            return 0
        if args.action == 'rollback':
            from .docker_install_update import rollback_update
            with installation_lock():
                receipt = read_receipt()
                if not receipt or receipt['type'] != 'docker':
                    raise InstallationError('No Docker update to recover')
                rollback_update(receipt)
            print('Previous Docker installation restored.')
            return 0
        if args.action == 'uninstall-native':
            print(json.dumps(uninstall_native(), indent=2))
            return 0
        if not receipt:
            print(json.dumps({'ok': True, 'state': 'not_installed'}))
            return 0
        if receipt['type'] != 'docker':
            raise InstallationError('No registered Docker installation; Native remains unchanged')
        root = Path(receipt['root'])
        if root != Path.home() / '.agentbc-docker':
            raise InstallationError('Unexpected installation root')
        with installation_lock():
            path = root / 'docker-host.json'
            if path.exists():
                from .docker_install import load
                from .docker_install_maintenance import uninstall
                print(json.dumps(uninstall(load(path)), indent=2))
            else:
                from .docker_install_runtime import docker, verify_engine
                engine = receipt['resources']['engine']
                verify_engine({'engine': engine})
                containers = docker('ps', '-aq', '--filter', 'label=io.agentbc.installation=' + receipt['installation_id'],
                                    context=engine['context']).strip()
                if containers:
                    raise InstallationError('Owned services exist without a profile; manual identity repair required')
                if root.exists():
                    shutil.rmtree(root)
                (state_root() / 'installation.json').unlink()
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'recovery_error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
