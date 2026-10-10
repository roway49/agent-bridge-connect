"""English setup and explicit configuration import for the managed Docker host."""
from __future__ import annotations

from pathlib import Path
import socket
import sys
import random

from .config import load_config, toml_dumps, validate_config
from .installation import InstallationError, private_json
from .docker_install_runtime import compose, docker, ready, refresh_binding, require_idle, write_compose


def choose(prompt: str, default: str, allowed: tuple[str, ...]) -> str:
    while True:
        try:
            value = input(prompt).strip().lower() or default
        except EOFError as exc:
            raise InstallationError('Setup input ended. Run setup again, or use --non-interactive.') from exc
        if value in allowed:
            return value
        print('Please choose ' + ' / '.join(allowed) + '.')


def free_port() -> int:
    if sys.platform == 'win32':
        # Avoid Windows' default ephemeral range (49152+) where HNS/WSL
        # reservations can move after a restart. This is not a port lease.
        candidates = random.SystemRandom().sample(range(18000, 48000), 256)
        for port in candidates:
            with socket.socket() as listener:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    listener.bind(('127.0.0.1', port))
                except OSError:
                    continue
                return port
        raise InstallationError('No available fixed WebUI port found. Check Windows port reservations and retry setup.')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def selections(profile: dict, interactive: bool) -> dict:
    settings = dict(profile.get('settings') or {})
    settings.setdefault('preferred_view', 'webui')
    if settings['preferred_view'] == 'terminal':
        settings['preferred_view'] = 'task_list'
    settings.setdefault('autostart', False)
    if not settings.get('port'):
        settings['port'] = free_port()
    if not interactive:
        return settings
    current = '2' if settings['preferred_view'] == 'webui' else '1'
    selected = choose(f'Task list style: 1) Terminal 2) WebUI [default: {current}]: ', current, ('1', '2'))
    settings['preferred_view'] = 'webui' if selected == '2' else 'task_list'
    if selected == '2':
        if profile.get('settings'):
            reassign = choose(f'Fixed WebUI port: {settings["port"]}. Assign a new port? [y/N] ', 'n', ('y', 'n'))
            if reassign == 'y':
                settings['port'] = free_port()
        default = ('y' if settings['autostart'] else 'n') if profile.get('settings') else 'y'
        suffix = '[Y/n]' if default == 'y' else '[y/N]'
        settings['autostart'] = choose(f'Keep WebUI running in the background? {suffix} ', default, ('y', 'n')) == 'y'
    else:
        settings['autostart'] = False
    return settings


def write_container(profile: dict, destination: str, data: bytes) -> None:
    code = "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True,mode=0o700); fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600); os.write(fd,sys.stdin.buffer.read()); os.close(fd)"
    docker('exec', '-i', profile['container_id'], 'python', '-c', code, destination,
           context=profile['engine']['context'], input=data)


def import_config(profile: dict, source: Path | None) -> None:
    config = load_config(source) if source else {}
    errors = validate_config(config)
    if errors:
        raise InstallationError('Invalid source configuration: ' + '; '.join(errors))
    # Only portable settings. Runtime paths, notifier routing, and task roots are
    # intentionally not copied from a host install into a Linux container.
    allowed = ('permissions', 'sessions', 'approval')
    target = {key: config[key] for key in allowed if key in config}
    target.update(workspace_root='/data/workspace', board_root='/data/workspace/record')
    target['executors'] = {}
    for name in (profile.get('available_executors') or ('codex', 'claude', 'hermes', 'opencode')):
        original = config.get('executors', {}).get(name, {})
        accepted = {'model', 'timeout_s', 'max_budget_usd', 'max_turns'}
        unsupported = set(original) - accepted - {'command'}
        if unsupported:
            raise InstallationError(f'Unsupported {name} settings: {", ".join(sorted(unsupported))}; configure a portable profile explicitly')
        target['executors'][name] = {key: value for key, value in original.items() if key in accepted}
        target['executors'][name]['command'] = '/opt/agentbc/bin/' + name
    write_container(profile, '/home/agentbc/.abc/config.toml', toml_dumps(target).encode())


# Exact portable files only; never copy a whole home or arbitrary scripts/hooks.
PROFILE_FILES = {
    'codex': ('.codex/config.toml', '.codex/auth.json'),
    'claude': ('.claude/settings.json', '.claude/.credentials.json', '.claude.json'),
    'hermes': ('.hermes/config.yaml', '.hermes/.env'),
    'opencode': ('.config/opencode/opencode.json', '.config/opencode/opencode.jsonc',
                 '.local/share/opencode/auth.json'),
}


def setup(profile: dict, path: Path, *, interactive: bool, initial: bool = False, project: str | None = None) -> None:
    if not initial:
        require_idle(profile)
    else:
        from .docker_install_paths import initialize_workspace
        initialize_workspace(profile, interactive=interactive)
    if interactive:
        from .docker_setup_guidance import print_intro
        print_intro(profile)
    imports = {}
    selected_config = None
    if interactive:
        import json
        from .docker_setup_native import configure
        config = {} if initial else json.loads(docker(
            'exec', profile['container_id'], 'python', '-c',
            "import json; from agent_bridge_connect.config import load_config; print(json.dumps(load_config('/home/agentbc/.abc/config.toml')))",
            context=profile['engine']['context']))
        selected_config, imports = configure(profile, config, interactive=True, available=profile.get('available_executors'))
    settings = selections(profile, interactive)
    projects = list(profile.get('projects', []))
    if interactive and project is None:
        print('Project folders give tasks access to your existing files. Only selected folders are shared.')
        print('Leave blank to keep current mounts and use the default Docker task workspace.')
        try:
            project = input('Host project folder to add (blank to keep current): ').strip() or None
        except EOFError as exc:
            raise InstallationError('Setup input ended. Run setup again, or use --non-interactive.') from exc
    if project:
        resolved = Path(project).expanduser().resolve(strict=True)
        if not resolved.is_dir() or resolved == Path.home() or resolved == Path(resolved.anchor):
            raise InstallationError('Choose a project directory, not your home or filesystem root')
        if str(resolved) not in projects:
            projects.append(str(resolved))
    profile.update(settings=settings, projects=projects)
    write_compose(profile)
    compose(profile, 'up', '-d', '--no-build', '--pull', 'never', 'runner')
    compose(profile, 'create', '--no-build', '--pull', 'never', 'webui')
    refresh_binding(profile, path)
    docker('exec', profile['container_id'], 'mkdir', '-p', '/data/workspace/record',
           '/data/workspace/tasks/report', '/data/workspace/tasks/artifacts',
           context=profile['engine']['context'])
    if selected_config is not None:
        selected_config.update(workspace_root='/data/workspace', board_root='/data/workspace/record')
        selected_config['webui'] = settings
        errors = validate_config(selected_config)
        if errors:
            raise InstallationError('Invalid setup configuration: ' + '; '.join(errors))
        write_container(profile, '/home/agentbc/.abc/config.toml', toml_dumps(selected_config).encode())
    elif initial:
        import_config(profile, None)
    for executor, files in imports.items():
        for name, body in files:
            write_container(profile, f'/profiles/{executor}/{name}', body)
        profile.setdefault('executor_imports', {})[executor] = 'Settings linked; model connection not tested'
    if selected_config is not None or initial:
        compose(profile, 'restart', 'runner')
    ready(profile)
    from .docker_install import entry_and_skills
    entry_and_skills(profile, path, interactive=interactive)
    from .docker_install_supervisor import register
    register(profile, enable=True)
    if settings['autostart']:
        compose(profile, 'up', '-d', '--no-build', '--pull', 'never', 'webui')
        ready(profile, 'webui')
    else:
        compose(profile, 'stop', 'webui')
    private_json(path, profile)
    print('Default task workspace: ' + (profile.get('host_workspace') or '/data/workspace (Docker volume)'))
    print('Task data is preserved on uninstall by default.')
    for index, folder in enumerate(projects):
        print(f'Host project: {folder} -> /projects/{index}')
    print('CLI: agentbc task list | agentbc runner status | agentbc doctor')
    print('Open a new terminal if the agentbc command is not yet on PATH.')
    print(f'Installation type: docker\nWebUI address: http://127.0.0.1:{settings["port"]}/\nFixed WebUI port: {settings["port"]}')
    print('WebUI service: ' + ('running in the background' if settings['autostart'] else 'not started'))
    print('Run agentbc webui start to open the authenticated monitoring page.')
    print('Run agentbc webui close to stop the page service without cancelling tasks.')
    from .docker_setup_guidance import print_summary
    print_summary(profile)
