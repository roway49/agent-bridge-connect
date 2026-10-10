"""Managed Docker host command routing; business commands retain existing contracts."""
from __future__ import annotations

from .host_platform import validate_private, venv_python, cli_entry_name, add_user_path

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

from .installation import InstallationError, installation_lock, private_json, read_receipt, state_root
from .docker_install_runtime import compose, docker, ready, service_ids, verify_binding, verify_engine


def load(path: Path) -> dict:
    try:
        validate_private(path)
    except ValueError as exc:
        raise InstallationError('Unsafe Docker host profile') from exc
    profile = json.loads(path.read_text())
    receipt = read_receipt()
    if (profile.get('schema') != 2 or not receipt or receipt['type'] != 'docker'
            or receipt['installation_id'] != profile.get('installation_id')
            or Path(receipt['root']) != path.parent):
        raise InstallationError('Docker profile does not match the formal installation receipt')
    return profile


def entry_and_skills(profile: dict, path: Path, *, interactive: bool = False) -> None:
    from .setup import _current_skill_files
    from .skill_packages import build_skill_manifest, classify_skill_package, replace_managed_skill_package
    root = Path(profile['root'])
    entry = root / 'bin' / cli_entry_name()
    entry.parent.mkdir(exist_ok=True)
    if os.name == 'nt':
        from .docker_install_paths import windows_entry
        entry.write_text(windows_entry(profile, path), encoding='utf-8')
    else:
        entry.write_text('#!/bin/sh\n# AgentBC Docker host entry\nexec ' + shlex.join(
            [str(venv_python(root / 'venv')), '-m', 'agent_bridge_connect.docker_install', 'run', '--profile', str(path), '--']) + ' "$@"\n')
        entry.chmod(0o755)
    from .docker_install_paths import skill_destinations
    destinations = skill_destinations(profile)
    profile['skill_paths'] = {name: str(target) for name, target in destinations.items()}
    private_json(path, profile)
    packages = []
    for name in ('codex', 'claude', 'hermes'):
        skill = destinations[name]
        files = _current_skill_files(name)
        legacy_files = dict(files)
        legacy_files['SKILL.md'] += b'\n## Docker host binding\n\nUse the installed `agentbc` CLI. It manages the registered Docker installation. Never call the container CLI directly or start a native Runner. Use host setup for project mounts and webui start/close/status for monitoring.\n'
        files['SKILL.md'] += b'\n## Docker host binding\n\nUse the installed `agentbc` CLI. It manages the registered Docker installation. Never call the container CLI directly or start a native Runner. Use host setup for project mounts and webui start/close/status for monitoring.\n\nFor Docker Codex Executor sessions, the managed Runner performs official App Server archive/delete and verifies absence. The `container_session_archive_then_delete` receipt does not require Desktop archiving. Do not create a Desktop archive heartbeat for these sessions or synthesize a Desktop acknowledgement. Follow the authoritative cleanup receipt; never archive the Dispatcher. Native-only Desktop cleanup instructions do not apply to container sessions.\n'
        state = classify_skill_package(skill, platform=name, package_version=profile['version'], current_files=files)
        if state['classification'] == 'modified':
            legacy = classify_skill_package(skill, platform=name, package_version=profile['version'], current_files=legacy_files)
            if legacy['classification'] == 'current':
                state = dict(state, classification='managed_outdated')
        if state['classification'] == 'modified':
            raise InstallationError(f'Locally modified skill must be preserved: {skill}')
        if interactive and state['classification'] != 'current':
            from .setup import _confirm_skill_package_install
            print(f'{name.title()} AgentBC Skill requires writing: {skill}')
            if _confirm_skill_package_install(
                    f'Install {name.title()} AgentBC Skill?',
                    preview=files['SKILL.md'].decode(), modified=False) != 'confirmed':
                print(f'{name.title()} Skill: skipped; existing files kept.')
                continue
        packages.append((name, skill, files))
    link = Path.home() / '.local/bin' / cli_entry_name()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        owned = (not link.is_symlink() and link.read_bytes() == entry.read_bytes()) if os.name == 'nt' else (link.is_symlink() and link.resolve() == entry)
        if not owned:
            raise InstallationError('Refusing to overwrite another CLI entry')
    for name, skill, files in packages:
        replace_managed_skill_package(skill, platform=name, files=files,
                                      manifest=build_skill_manifest(name, profile['version'], files))
        print(f'{name.title()} Skill: installed; linked to Docker agentbc.')
    if os.name == 'nt':
        link.write_bytes(entry.read_bytes())
        add_user_path(link.parent)
    elif not link.is_symlink():
        link.symlink_to(entry)


def run(path: Path, argv: list[str]) -> int:
    from .docker_install_setup import setup
    profile = load(path)
    command = argv[0] if argv else '--help'
    if command == '--version':
        print('agentbc ' + profile['version'])
        return 0
    if command in ('--help', '-h'):
        print('AgentBC Docker: setup, doctor, task, runner start/stop/status, webui start/close/status, update --bundle DIR, uninstall')
        return 0
    # Keep lifecycle changes exclusive, while dispatch and the desktop bridge
    # can run together. Hold the shared barrier through the container command.
    exclusive = command in ('setup', 'uninstall', 'update', 'runner', 'webui')
    with installation_lock(wait_seconds=30, shared=not exclusive):
        receipt = read_receipt()
        if not receipt or receipt['installation_id'] != profile['installation_id']:
            raise InstallationError('Installation changed while reading status; retry the command')
        if receipt['state'] != 'installed' and command not in ('doctor', 'uninstall', 'setup'):
            raise InstallationError('Installation requires recovery before use')
        if command == 'doctor':
            try:
                verify_binding(profile)
                services = {name: info['State'] for name, info in service_ids(profile).items()}
                error = ''
            except (ValueError, RuntimeError) as exc:
                services, error = {}, str(exc)
            from .docker_approval_bridge import read_delivery_state
            if os.name == 'nt':
                from .windows_desktop import status as desktop_status
                approval = desktop_status(Path(profile['root']))
                error = error or approval.get('webui_error', '')
            else:
                approval = read_delivery_state(Path(profile['root']), profile.get('container_id', ''))
            print(json.dumps({'installation_type': 'docker', 'version': profile['version'],
                              'root': profile['root'], 'image_id': profile['image_id'],
                              'project': profile['project'], 'engine': profile['engine'],
                              'state': receipt['state'], 'settings': profile['settings'],
                              'services': services, 'approval_bridge': approval, 'error': error}, indent=2))
            return 2 if error else (0 if approval['state'] == 'ready' else 1)
        verify_engine(profile)
        if command == 'setup':
            if receipt.get('update'):
                raise InstallationError('Interrupted update; run the offline bundle recover_docker.sh rollback first')
            parser = argparse.ArgumentParser(prog='agentbc setup')
            parser.add_argument('--non-interactive', action='store_true')
            parser.add_argument('--show', action='store_true')
            parser.add_argument('--project', help='Explicit host project folder to mount')
            selected = parser.parse_args(argv[1:])
            if selected.show:
                print(json.dumps({'installation_type': 'docker', 'settings': profile['settings'], 'projects': profile['projects']}, indent=2))
                return 0
            from .docker_install_runtime import assert_project_owned, require_idle, write_compose, refresh_binding
            assert_project_owned(profile)
            require_idle(profile)
            if not profile.get('managed_runner_entry') and not any(info['State']['Running'] for info in service_ids(profile).values()):
                from .docker_install_runtime import clear_stopped_legacy_pid
                clear_stopped_legacy_pid(profile)
            write_compose(profile)
            compose(profile, 'create', '--no-build', '--pull', 'never', 'runner', 'webui')
            refresh_binding(profile, path)
            from .docker_install_maintenance import maintenance
            with maintenance(profile):
                try:
                    receipt['state'] = 'installing'
                    private_json(state_root() / 'installation.json', receipt)
                    setup(profile, path, interactive=not selected.non_interactive, project=selected.project)
                    receipt['state'] = 'installed'
                except BaseException:
                    receipt['state'] = 'repair_required'
                    raise
                finally:
                    private_json(state_root() / 'installation.json', receipt)
            return 0
        if command != 'uninstall' or receipt['state'] != 'uninstalling':
            verify_binding(profile)
        if command == 'uninstall':
            from .docker_install_maintenance import uninstall
            parser = argparse.ArgumentParser(prog='agentbc uninstall')
            parser.add_argument('--non-interactive', action='store_true')
            parser.add_argument('--keep-credentials', action='store_true')
            for kind in ('records', 'artifacts'):
                group = parser.add_mutually_exclusive_group()
                group.add_argument('--remove-' + kind, action='store_true')
                group.add_argument('--keep-' + kind, action='store_true')
            args = parser.parse_args(argv[1:])
            print(json.dumps(uninstall(profile, remove_records=args.remove_records, remove_artifacts=args.remove_artifacts, keep_credentials=args.keep_credentials), indent=2))
            return 0
        if command == 'update':
            from .docker_install_update import update
            parser = argparse.ArgumentParser(prog='agentbc update')
            parser.add_argument('--bundle', type=Path, required=True)
            args = parser.parse_args(argv[1:])
            update(profile, path, args.bundle)
            return 0
        if command == 'runner':
            if argv[1:] not in (['start'], ['stop'], ['status']):
                raise InstallationError('Docker runner supports start, stop and status')
            if argv[1] == 'start':
                (path.parent / 'runner.closed').unlink(missing_ok=True)
                compose(profile, 'start', 'runner')
                ready(profile)
            elif argv[1] == 'stop':
                from .docker_install_maintenance import maintenance
                with maintenance(profile):
                    compose(profile, 'stop', 'runner')
                (path.parent / 'runner.closed').touch(mode=0o600)
            else:
                current = service_ids(profile)['runner']
                if not current['State']['Running']:
                    print(json.dumps({'ok': False, 'running': False, 'installation_type': 'docker'}))
                    return 2
                print(docker('exec', profile['container_id'], 'agentbc', 'runner', 'status',
                             context=profile['engine']['context']))
            return 0
        if command == 'webui':
            from .docker_webui import command as web_command
            result = web_command(profile, argv[1:])
            marker = path.parent / 'webui.closed'
            if argv[1:2] == ['close']:
                marker.touch(mode=0o600)
            elif not argv[1:] or argv[1] == 'start':
                marker.unlink(missing_ok=True)
            return result
        # Reuse the tested snapshot/path mapper and approval bridge. Its schema-1
        # binding is private and transient; never creates a second installation.
        from .docker_host import run as preview_run
        transient = path.parent / ('command-binding-' + uuid.uuid4().hex + '.json')
        private_json(transient, {**profile, 'schema': 1})
        try:
            return preview_run(transient, argv)
        finally:
            transient.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('initialize', 'run'))
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--non-interactive', action='store_true')
    args, rest = parser.parse_known_args()
    try:
        if args.action == 'initialize':
            from .docker_install_setup import setup
            profile = load(args.profile)
            setup(profile, args.profile, interactive=not args.non_interactive, initial=True)
            return 0
        return run(args.profile, rest[1:] if rest[:1] == ['--'] else rest)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'docker_install_error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
