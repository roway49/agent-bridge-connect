"""Explicit setup for a provisioned Windows preview; never creates a formal receipt."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import time

from .docker_approval_bridge import execute
from .installation import private_json
from .host_platform import ensure_private_directory, validate_private


def documents_workspace() -> Path:
    if os.name == 'nt':
        # Respect redirected/localized Documents instead of guessing USERPROFILE.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:
            raise ValueError('Cannot resolve your Documents folder')
        return Path(buffer.value) / 'AgentBC' / 'workspace'
    return Path.home() / 'Documents/AgentBC/workspace'


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(profile: dict) -> tuple[Path, dict]:
    from .docker_install_runtime import engine_identity
    from .docker_webui import verify as verify_webui
    saved = profile.get('setup_binding')
    if not saved:
        raise ValueError('This preview has no verified setup binding; use its deployment preparation tool first')
    if engine_identity() != saved['engine']:
        raise ValueError('Docker context or daemon changed; restore the configured engine')
    path = Path(saved['compose'])
    validate_private(path)
    if checksum(path) != saved['compose_sha256']:
        raise ValueError('Compose configuration changed outside setup; restore the verified file')
    spec = json.loads(path.read_text())
    ids = execute(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + spec['name']]).split()
    if set(ids) != {profile['container_id'], profile['webui']['container_id']}:
        raise ValueError('Preview project has missing or foreign containers')
    for cid in ids:
        info = json.loads(execute(['docker', 'inspect', cid]))[0]
        expected = saved['containers'][cid]
        if (info['Image'] != expected['image'] or info['Mounts'] != expected['mounts']
                or info['Config'].get('Labels') != expected['labels']):
            raise ValueError('Preview image or mount ownership changed')
    verify_webui(profile)
    return path, spec


def refresh(profile: dict, path: Path, compose_path: Path, spec: dict) -> None:
    from .docker_webui import binding
    infos = {name: json.loads(execute(['docker', 'inspect', service['container_name']]))[0]
             for name, service in spec['services'].items()}
    for name, info in infos.items():
        if (info['Config']['Labels'].get('com.docker.compose.project') != spec['name']
                or info['Config']['Labels'].get('com.docker.compose.service') != name
                or info['Image'] != spec['services'][name]['image']):
            raise ValueError('Rebuilt preview service identity mismatch')
    profile.update(container_id=infos['runner']['Id'], container_name=infos['runner']['Name'],
                   webui=binding(infos['runner'], infos['webui']))
    profile['setup_binding'].update(compose_sha256=checksum(compose_path), containers={
        i['Id']: {'image': i['Image'], 'mounts': i['Mounts'], 'labels': i['Config']['Labels']} for i in infos.values()})
    private_json(path, profile)


def idle(container: str) -> None:
    code = """import json
from pathlib import Path
from agent_bridge_connect.task_store import TaskStore
from agent_bridge_connect.terminal_states import TASK_TERMINAL_STATES
root=Path('/data/workspace/record')
active=[t['id'] for t in TaskStore(root).list_tasks() if t['status'] not in TASK_TERMINAL_STATES]
for p in root.rglob('*lease*.json'):
 if json.loads(p.read_text()).get('state') != 'closed': active.append(str(p))
print(json.dumps(active))
"""
    if json.loads(execute(['docker', 'exec', container, 'python', '-c', code])):
        raise ValueError('Unfinished tasks or leases prevent setup; finish them using normal task commands')


def container_file(container: str, path: str, body: bytes | None = None) -> bytes | None:
    if body is None:
        code = "import sys;from pathlib import Path;p=Path(sys.argv[1]);sys.exit(3) if not p.exists() else sys.stdout.buffer.write(p.read_bytes())"
        result = subprocess.run(['docker', 'exec', container, 'python', '-c', code, path], capture_output=True, timeout=30)
        if result.returncode == 3:
            return None
    else:
        code = "import sys,os;from pathlib import Path;p=Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(p.name+'.setup-new');t.write_bytes(sys.stdin.buffer.read());t.chmod(0o600);os.replace(t,p)"
        result = subprocess.run(['docker', 'exec', '-i', container, 'python', '-c', code, path], input=body, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Container configuration read/write failed')
    return result.stdout


def skill_plan(profile: dict, interactive: bool) -> list[tuple[Path, str, dict]]:
    from . import __version__
    from .setup import _current_skill_files, _active_hermes_home, _confirm_skill_package_install
    from .skill_packages import classify_skill_package
    result = []
    if interactive:
        print('\nInstall AgentBC command skills for your local AI assistants.')
        print('A skill does not install the AI CLI or enable its Docker executor.')
    home = Path.home()
    for name, root in [('codex', Path(os.environ.get('CODEX_HOME', home / '.codex'))),
                       ('claude', Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'))),
                       ('hermes', _active_hermes_home())]:
        target = root / 'skills/agentbc'
        files = _current_skill_files(name)
        files['SKILL.md'] += b'\n## Docker host binding\n\nUse the installed agentbc host CLI for this Windows Docker preview. Never run a native Runner or call the container CLI directly. Use agentbc setup for settings and webui start/close/status for monitoring. Docker Executor sessions use official container cleanup; never archive the Dispatcher.\n'
        state = classify_skill_package(target, platform=name, package_version=__version__, current_files=files)
        if state['classification'] == 'modified':
            raise ValueError(f'User-modified skill preserved: {target}. Back it up and resolve before setup.')
        if not interactive:
            continue
        print(f'{name.title()} AgentBC Skill: {target}')
        if _confirm_skill_package_install(f'Install {name.title()} AgentBC Skill?', preview=files['SKILL.md'].decode(), modified=False) == 'confirmed':
            result.append((target, name, files))
    return result


def show(profile: dict) -> None:
    root = profile.get('host_workspace') or str(documents_workspace())
    print('Installation type: docker-preview (Windows setup acceptance)')
    print(f'Workspace: {root}')
    print(f'Reports: {Path(root) / "tasks/report"}')
    print(f'Task files: {Path(root) / "tasks/artifacts"}')
    print('Docker workspace: /data/workspace; historical tasks are preserved.')
    print(f'WebUI address: {profile["webui"]["origin"]}/')
    print(f'Fixed WebUI port: {profile["webui"]["port"]}')
    print('Run agentbc webui start to open the authenticated page.')
    print('Run agentbc task list, agentbc runner status, or agentbc doctor.')


def command(path: Path, profile: dict, argv: list[str]) -> int:
    from .docker_setup_native import configure
    from .docker_install_setup import selections
    from .config import toml_dumps, validate_config
    from .windows_desktop import register, ensure
    from .docker_webui import command as webui_command
    from .skill_packages import build_skill_manifest, replace_managed_skill_package
    from . import __version__
    parser = argparse.ArgumentParser(prog='agentbc setup')
    parser.add_argument('--show', action='store_true')
    parser.add_argument('--non-interactive', action='store_true')
    args = parser.parse_args(argv)
    compose_path, spec = verify(profile)
    if args.show:
        show(profile)
        return 0
    idle(profile['container_id'])
    interactive = not args.non_interactive
    old_config = container_file(profile['container_id'], '/home/agentbc/.abc/config.toml')
    config = json.loads(execute(['docker', 'exec', profile['container_id'], 'python', '-c',
        "import json;from agent_bridge_connect.config import load_config;print(json.dumps(load_config('/home/agentbc/.abc/config.toml')))"]))
    candidate = copy.deepcopy(profile)
    imports = {}
    if interactive:
        show(profile)
        print('\nSetup: detect AI tools -> link settings -> permissions -> monitoring -> skills.')
        print('Press Enter to keep existing choices. No model request is sent by setup.')
        config, imports = configure(candidate, config, interactive=True,
                                    available=profile['setup_binding']['executors'])
    settings = selections(candidate, interactive)
    skills = skill_plan(candidate, interactive)
    config.update(workspace_root='/data/workspace', board_root='/data/workspace/record', webui=settings)
    errors = validate_config(config)
    if errors:
        raise ValueError('Invalid setup settings: ' + '; '.join(errors))
    # Prepare/confirm all user choices before stopping anything.
    old_spec = copy.deepcopy(spec)
    old_profile = copy.deepcopy(profile)
    was_running = json.loads(execute(['docker', 'inspect', profile['webui']['container_id']]))[0]['State']['Running']
    backup = path.parent / ('setup-backup-' + str(time.time_ns()))
    ensure_private_directory(backup)
    private_json(backup / 'profile.json', old_profile)
    private_json(backup / 'compose.json', old_spec)
    import base64
    originals = {'/home/agentbc/.abc/config.toml': old_config}
    for executor, files in imports.items():
        for relative, _ in files:
            destination = profile['setup_binding']['profile_roots'][executor] + '/' + relative
            originals[destination] = container_file(profile['container_id'], destination)
    private_json(backup / 'config-files.json', {k: None if v is None else base64.b64encode(v).decode() for k, v in originals.items()})
    def compose(*items):
        execute(['docker', 'compose', '-f', str(compose_path), *items])
    skill_backups = []
    for index, (target, _, _) in enumerate(skills):
        saved = backup / f'skill-{index}'
        if target.exists():
            shutil.copytree(target, saved)
        skill_backups.append((target, saved))
    register(path, enable=False)
    try:
        idle(profile['container_id'])
        for executor, files in imports.items():
            for relative, body in files:
                container_file(profile['container_id'], profile['setup_binding']['profile_roots'][executor] + '/' + relative, body)
            candidate.setdefault('executor_imports', {})[executor] = 'Settings linked; connection not tested'
        container_file(profile['container_id'], '/home/agentbc/.abc/config.toml', toml_dumps(config).encode())
        service = spec['services']['webui']
        service['ports'] = [f'127.0.0.1:{settings["port"]}:8080']
        index = service['command'].index('--public-origin') + 1
        service['command'][index] = f'http://127.0.0.1:{settings["port"]}'
        # Closing is persistent in this login; the desktop bridge restores at next login.
        service['restart'] = 'no'
        private_json(compose_path, spec)
        compose('up', '-d', '--no-build', '--pull', 'never', 'runner')
        compose('restart', 'runner')
        compose('create', '--no-build', '--pull', 'never', 'webui')
        candidate['settings'] = settings
        refresh(candidate, path, compose_path, spec)
        for attempt in range(20):
            status = json.loads(execute(['docker', 'exec', candidate['container_id'], 'agentbc', 'runner', 'status']))
            if status.get('ok'):
                break
            if attempt == 19:
                raise ValueError('Runner did not become ready')
            time.sleep(0.5)
        should_run_webui = settings['autostart'] if interactive else was_running
        if should_run_webui:
            webui_command(candidate, ['start', '--no-open'])
            (path.parent / 'webui.closed').unlink(missing_ok=True)
        else:
            webui_command(candidate, ['close'])
            (path.parent / 'webui.closed').touch()
        for target, name, files in skills:
            replace_managed_skill_package(target, platform=name, files=files,
                manifest=build_skill_manifest(name, __version__, files))
            print(f'{name.title()} AgentBC command skill installed. AI runtime unchanged.')
        ensure(path)
    except BaseException:
        for target, saved in skill_backups:
            if target.exists():
                shutil.rmtree(target)
            if saved.exists():
                shutil.copytree(saved, target)
        for destination, body in originals.items():
            if body is None:
                execute(['docker', 'exec', profile['container_id'], 'python', '-c',
                         'from pathlib import Path;import sys;Path(sys.argv[1]).unlink(missing_ok=True)', destination])
            else:
                container_file(profile['container_id'], destination, body)
        private_json(compose_path, old_spec)
        compose('up', '-d', '--no-build', '--pull', 'never', 'runner')
        compose('restart', 'runner')
        compose('create', '--no-build', '--pull', 'never', 'webui')
        refresh(old_profile, path, compose_path, old_spec)
        if was_running:
            webui_command(old_profile, ['start', '--no-open'])
        ensure(path)
        raise
    show(candidate)
    from .docker_setup_guidance import print_summary
    print_summary(candidate)
    print('Setup complete.')
    return 0


def runner_command(path: Path, profile: dict, action: str) -> int:
    """Manage the owned preview container, including when its Runner is stopped."""
    if action not in {'start', 'stop', 'status'}:
        raise ValueError('Preview Runner supports start, stop and status')
    cp, _ = verify(profile)
    info = json.loads(execute(['docker', 'inspect', profile['container_id']]))[0]
    running = info['State']['Running']
    marker = path.parent / 'runner.closed'
    if action == 'stop':
        if running:
            idle(profile['container_id'])
            execute(['docker', 'compose', '-f', str(cp), 'stop', 'runner'])
        marker.touch()
        print(json.dumps({'ok': True, 'status': 'stopped'}))
        return 0
    if action == 'start':
        if not running:
            execute(['docker', 'compose', '-f', str(cp), 'start', 'runner'])
        marker.unlink(missing_ok=True)
    if action == 'status' and not running:
        print(json.dumps({'ok': False, 'status': 'stopped'}))
        return 0
    for attempt in range(20 if action == 'start' else 1):
        try:
            state = json.loads(execute(['docker', 'exec', profile['container_id'], 'agentbc', 'runner', 'status']))
            if state.get('ok') or action == 'status':
                print(json.dumps(state))
                return 0 if state.get('ok') else 1
        except RuntimeError:
            if action == 'status':
                raise
        time.sleep(0.5)
    raise RuntimeError('Docker Runner did not become ready')


def restore_services(path: Path, *, reset_closed: bool) -> str:
    """Called by the desktop owner; failed WebUI startup cannot stop approvals."""
    from .host_platform import try_lock
    from .docker_webui import command as webui_command
    import contextlib
    import io
    with (path.parent / 'preview-setup.lock').open('a+b') as lock:
        try_lock(lock.fileno())
        validate_private(path)
        profile = json.loads(path.read_text())
        cp, _ = verify(profile)
        if reset_closed:
            for name in ('runner.closed', 'webui.closed'):
                (path.parent / name).unlink(missing_ok=True)
        info = json.loads(execute(['docker', 'inspect', profile['container_id']]))[0]
        if not info['State']['Running'] and not (path.parent / 'runner.closed').exists():
            execute(['docker', 'compose', '-f', str(cp), 'start', 'runner'])
        if profile.get('settings', {}).get('autostart') and not (path.parent / 'webui.closed').exists():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    webui_command(profile, ['start', '--no-open'])
            except (RuntimeError, OSError):
                return 'WebUI recovery failed; run agentbc webui status. Approval bridge remains active.'
        return ''
