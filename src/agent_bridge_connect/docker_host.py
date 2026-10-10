"""Explicit host entry point for one Docker AgentBC instance (Mac preview)."""
from __future__ import annotations

from .host_platform import validate_private, docker_host_path, ensure_private_directory, cli_entry_name

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import select
import shlex
import subprocess
import sys
import uuid

from .docker_approval_bridge import execute

CONFIG = '/home/agentbc/.abc/config.toml'
ROOT = '/data/workspace/record'
FILE_OPTIONS = {'--steps', '--image', '--input-file'}
BLOCKED = {'setup', 'update', 'uninstall', 'webui', 'worker', 'init'}


def inspect_container(container: str) -> dict:
    info = json.loads(execute(['docker', 'inspect', container]))[0]
    if not info['State']['Running']:
        raise ValueError('Docker instance is stopped; start its Compose project first')
    return info


def load_profile(path: Path, *, require_running: bool = True) -> dict:
    try:
        validate_private(path)
    except ValueError as exc:
        raise ValueError('Host profile must be an owner-only regular file') from exc
    profile = json.loads(path.read_text())
    if profile.get('schema') != 1:
        raise ValueError('Unsupported Docker host profile')
    info = (inspect_container(profile['container_id']) if require_running else
            json.loads(execute(['docker', 'inspect', profile['container_id']]))[0])
    if info['Id'] != profile['container_id']:
        raise ValueError('Docker instance identity changed; explicitly reinstall the host binding')
    return profile


def map_project(value: str, mounts: list[dict]) -> str:
    if value == 'default path':
        return value
    source = Path(value).expanduser().resolve(strict=True)
    for mount in sorted(mounts, key=lambda m: len(m.get('Source', '')), reverse=True):
        if mount.get('Type') != 'bind' or not mount.get('RW'):
            continue
        try:
            relative = source.relative_to(docker_host_path(mount['Source']).resolve())
        except ValueError:
            continue
        return str(PurePosixPath(mount['Destination']).joinpath(*relative.parts))
    raise ValueError('Project path is not in a writable Docker bind mount; configure the exact project mount first')


def route_arguments(argv: list[str], mounts: list[dict], stage, *, supported_flags: set[str] | None = None) -> list[str]:
    if argv and argv[0] in BLOCKED:
        raise ValueError(f'{argv[0]} is not yet supported by this Docker host preview; native fallback is disabled')
    if argv[:1] == ['runner'] and argv[1:2] != ['status']:
        raise ValueError('Docker owns Runner lifecycle; use Compose for this instance')
    result, index = [], 0
    while index < len(argv):
        arg = argv[index]
        option, sep, value = arg.partition('=')
        if option in FILE_OPTIONS | {'--customer-path', '--config', '--root'}:
            if not sep:
                index += 1
                if index >= len(argv):
                    raise ValueError(f'{option} requires a value')
                value = argv[index]
            if option in FILE_OPTIONS:
                value = stage(Path(value).expanduser().resolve(strict=True), option)
            elif option == '--customer-path':
                value = map_project(value, mounts)
            elif value != {'--config': CONFIG, '--root': ROOT}[option]:
                raise ValueError(f'{option} must refer to the bound container path; omit the host override')
            result.extend([option, value])
        else:
            result.append(arg)
        index += 1
    # Do not rely on native CLI defaults: several commands resolve root before config.
    if supported_flags is None:
        from .cli import build_parser
        parser = build_parser()
        for token in result[:2]:
            sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
            if sub is None or token not in sub.choices:
                break
            parser = sub.choices[token]
        supported = {flag for action in parser._actions for flag in action.option_strings}
    else:
        supported = supported_flags
    present = {arg.partition('=')[0] for arg in result}
    for flag, value in (('--root', ROOT), ('--config', CONFIG)):
        if flag in supported and flag not in present:
            result.extend([flag, value])
    return result


def start_bridge(container: str, log_path: Path) -> None:
    if sys.platform == 'win32':
        from .windows_desktop import ensure
        ensure(log_path.with_name('docker-host.json'))
        return
    if sys.platform != 'darwin':
        raise ValueError('Automatic desktop approval requires a supported desktop host')
    # The bridge itself obtains the per-container lock; concurrent launches are idempotent.
    env = dict(os.environ)
    env.pop('AGENTBC_HEADLESS_NOTIFICATIONS', None)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    if os.fstat(log_fd).st_uid != os.getuid() or os.fstat(log_fd).st_mode & 0o077:
        os.close(log_fd)
        raise ValueError('Unsafe approval bridge log')
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, '-m', 'agent_bridge_connect.docker_approval_bridge',
             '--container', container, '--watch', '--ready-fd', str(write_fd)],
            stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
            start_new_session=True, close_fds=True, pass_fds=(write_fd,), env=env,
        )
    finally:
        os.close(log_fd)
        os.close(write_fd)
    try:
        available, _, _ = select.select([read_fd], [], [], 40)
        if not available or os.read(read_fd, 64) != b'ready\n':
            process.terminate()
            process.wait(timeout=5)
            raise RuntimeError('Host approval bridge did not become ready; no task command was sent')
    finally:
        os.close(read_fd)



def run(profile_path: Path, argv: list[str]) -> int:
    # Serialize setup against dispatch/mutations; read-only monitoring stays available.
    mutating = (argv[:1] == ['runner'] and argv[1:2] in [['start'], ['stop']]) or argv[:1] == ['setup'] or (argv[:1] == ['webui'] and argv[1:2] != ['status']) or (argv[:1] == ['task'] and argv[1:2] not in
        [['status'], ['list'], ['report'], ['logs'], ['preflight']])
    if mutating and (profile_path.parent / 'preview-setup.enabled').is_file():
        from .host_platform import try_lock
        with (profile_path.parent / 'preview-setup.lock').open('a+b') as lock:
            try_lock(lock.fileno())
            return _run(profile_path, argv)
    return _run(profile_path, argv)


def _run(profile_path: Path, argv: list[str]) -> int:
    if sys.platform == 'win32' and argv[:1] == ['doctor']:
        from .windows_desktop import status
        validate_private(profile_path)
        profile = json.loads(profile_path.read_text())
        if profile.get('schema') != 1:
            raise ValueError('Unsupported Docker host profile')
        desktop = status(profile_path.parent)
        print(json.dumps({'installation_type': 'docker-preview', 'container_id': profile['container_id'],
                          'approval_delivery': profile.get('approval_delivery', 'desktop'),
                          'desktop': desktop}, ensure_ascii=True, indent=2))
        return 0 if desktop['state'] == 'ready' and not desktop.get('webui_error') else 1
    profile = load_profile(profile_path, require_running=argv[:1] not in [['webui'], ['runner']])
    if profile.get('setup_binding') and argv[:1] == ['runner'] and argv[1:2] in [['start'], ['stop'], ['status']]:
        from .docker_preview_setup import runner_command
        return runner_command(profile_path, profile, argv[1])
    if argv[:1] == ['setup']:
        from .docker_preview_setup import command
        return command(profile_path, profile, argv[1:])
    if argv[:1] == ['webui']:
        from .docker_webui import command
        result = command(profile, argv[1:])
        if profile.get('setup_binding') and result == 0:
            marker = profile_path.parent / 'webui.closed'
            if argv[1:2] == ['close']:
                marker.touch()
            elif argv[1:2] != ['status']:
                marker.unlink(missing_ok=True)
        return result
    container = profile['container_id']
    info = inspect_container(container)
    staging = f'/data/workspace/host-inputs/{uuid.uuid4().hex}'
    copied = False

    def stage(path: Path, option: str) -> str:
        nonlocal copied
        if not path.is_file():
            raise ValueError(f'{option} requires a regular file')
        execute(['docker', 'exec', container, 'mkdir', '-p', staging])
        # Each copy has its own name so repeated equal basenames never collide.
        target = f'{staging}/{uuid.uuid4().hex}-{path.name}'
        writer = "import os,sys,shutil; f=os.fdopen(os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb'); shutil.copyfileobj(sys.stdin.buffer,f); f.close()"
        with path.open('rb') as stream:
            result = subprocess.run(['docker', 'exec', '-i', container, 'python', '-c', writer, target],
                                    stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise RuntimeError('Failed to snapshot host input')
        copied = True
        return target

    flags = None
    if os.name == 'nt':
        # Parse command metadata in Linux, never import the business runtime on
        # the Windows thin host merely to discover accepted root/config flags.
        query = """import argparse,json,sys
from agent_bridge_connect.cli import build_parser
parser=build_parser()
for token in json.loads(sys.argv[1]):
 sub=next((a for a in parser._actions if isinstance(a,argparse._SubParsersAction)),None)
 if sub is None or token not in sub.choices: break
 parser=sub.choices[token]
print(json.dumps(sorted({f for a in parser._actions for f in a.option_strings})))
"""
        metadata = json.loads(execute(['docker', 'exec', container, 'python', '-c', query, json.dumps(argv[:2])]))
        if not isinstance(metadata, list) or not all(isinstance(flag, str) for flag in metadata):
            raise ValueError('Invalid container CLI metadata')
        flags = set(metadata)
    args = route_arguments(argv, info.get('Mounts', []), stage, supported_flags=flags)
    delivery = profile.get('approval_delivery', 'desktop')
    if delivery == 'desktop':
        diagnostic = argv[:2] in (['task', 'status'], ['task', 'report'], ['task', 'logs'], ['task', 'list'], ['task', 'preflight'], ['runner', 'status'])
        if not (sys.platform == 'win32' and diagnostic):
            start_bridge(container, profile_path.with_name('approval-bridge.log'))
    elif delivery == 'cli':
        print('Docker preview: approvals require explicit task respond; no desktop delivery is claimed.', file=sys.stderr)
    else:
        raise ValueError('Unsupported approval delivery mode')
    command = ['docker', 'exec', '-i']
    if sys.stdin.isatty() and sys.stdout.isatty():
        command.append('-t')
    command.extend([container, 'agentbc', *args])
    # Docker container configuration defines the board and config defaults.
    result = subprocess.run(command)
    if copied:
        # Input references can be used by future handoffs; retain staged inputs.
        print(f'Docker input snapshot retained: {staging}', file=sys.stderr)
    return result.returncode


def install(prefix: Path, skill_home: Path, container: str, webui_container: str | None = None, *, approval_delivery: str = 'desktop') -> dict:
    from . import __version__
    from .setup import _current_skill_files
    from .skill_packages import build_skill_manifest, classify_skill_package, replace_managed_skill_package

    if approval_delivery not in {'desktop', 'cli'}:
        raise ValueError('Unsupported approval delivery mode')
    info = inspect_container(container)
    webui_binding = None
    if webui_container:
        from .docker_webui import binding, inspect
        webui_binding = binding(info, inspect(webui_container))
    prefix = prefix.expanduser().resolve()
    skill_home = skill_home.expanduser().resolve()
    profile_path = prefix / 'docker-host.json'
    entry = prefix / 'bin' / cli_entry_name()
    if profile_path.exists() or entry.exists() or entry.is_symlink():
        raise ValueError('Use an empty installation prefix; existing CLI installation will not be overwritten')
    if os.name == 'nt' and any(c in str(prefix) + sys.executable for c in '%!\"\r\n'):
        raise ValueError('Unsupported Windows entry path characters')
    packages = []
    for platform in ('codex', 'claude', 'hermes'):
        root = skill_home / f'.{platform}' / 'skills' / 'agentbc'
        files = _current_skill_files(platform)
        native_state = classify_skill_package(root, platform=platform, package_version=__version__, current_files=files)
        files['SKILL.md'] += (f'\n## Docker host binding\n\nUse `{entry}` (or put its directory first in PATH). '
            'This entry targets only the explicitly bound Docker instance. Omit host --config and --root overrides. '
            'Host steps/images/input files are snapshotted into the container. Customer paths must already be '
            'writable bind mounts; never substitute an unrequested path. Runner lifecycle is managed by Compose; '
            'do not run native setup or runner start. Approval dialogs are discovered automatically on macOS. '
            'Use webui start/close/status only when this entry has an explicit WebUI container binding. '
            'Install/update lifecycle remains a later Docker acceptance stage.\n').encode()
        if approval_delivery == 'cli':
            files['SKILL.md'] += b'\nThis isolated preview uses explicit CLI approval delivery. No desktop popup is available. Check task status for an authoritative input request and obtain the human decision before task respond. Never approve on behalf of the user or change permission mode to bypass it.\n'
        state = classify_skill_package(root, platform=platform, package_version=__version__, current_files=files)
        if state['classification'] == 'modified' and native_state['classification'] == 'modified':
            raise ValueError(f'Locally modified skill must be preserved: {root}')
        packages.append((root, platform, files))
    ensure_private_directory(prefix)
    profile = {'schema': 1, 'container_id': info['Id'], 'container_name': info['Name'],
               'skill_home': str(skill_home), 'approval_delivery': approval_delivery}
    if webui_binding:
        profile['webui'] = webui_binding
    from .installation import private_json
    private_json(profile_path, profile)
    entry.parent.mkdir(parents=True, exist_ok=True)
    script = '#!/bin/sh\n# AgentBC Docker host entry\nexec ' + shlex.join(
        [sys.executable, '-m', 'agent_bridge_connect.docker_host', 'run', '--profile', str(profile_path), '--']) + ' "$@"\n'
    if os.name == 'nt':
        script = ('@echo off\nchcp 65001 >nul\nrem AgentBC Docker preview entry\n'
                  + '"' + sys.executable + '" -m agent_bridge_connect.docker_host run --profile "'
                  + str(profile_path) + '" -- %*\n')
    with entry.open('x', encoding='utf-8') as stream:
        stream.write(script)
    entry.chmod(0o755)
    for root, platform, files in packages:
        replace_managed_skill_package(root, platform=platform, files=files,
                                      manifest=build_skill_manifest(platform, __version__, files))
    return {'entry': str(entry), 'profile': str(profile_path), 'skills': [str(p[0]) for p in packages]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    installer = commands.add_parser('install')
    installer.add_argument('--prefix', type=Path, required=True)
    installer.add_argument('--skill-home', type=Path, required=True)
    installer.add_argument('--container', required=True)
    installer.add_argument('--webui-container')
    installer.add_argument('--approval-delivery', choices=('desktop', 'cli'), default='desktop')
    client = commands.add_parser('run')
    client.add_argument('--profile', type=Path, required=True)
    client.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == 'install':
            print(json.dumps(install(args.prefix, args.skill_home, args.container, args.webui_container, approval_delivery=args.approval_delivery), indent=2))
            return 0
        argv = args.args[1:] if args.args[:1] == ['--'] else args.args
        return run(args.profile, argv)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f'docker_host_error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
