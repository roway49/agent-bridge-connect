"""Identity-checked Docker engine and Compose operations for managed installations."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from .host_platform import image_architecture, docker_host_path

from .installation import InstallationError, private_json

LABEL = 'io.agentbc.installation'


def docker(*args: str, context: str | None = None, input: bytes | None = None) -> str:
    command = ['docker', *(['--context', context] if context else []), *args]
    result = subprocess.run(command, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        # Engine errors may echo command/config data. Keep receipts and secrets out.
        raise InstallationError(f'Docker operation failed ({args[0] if args else "probe"}); check Docker and owned service status')
    return result.stdout.decode()


def engine_identity() -> dict:
    context = docker('context', 'show').strip()
    info = json.loads(docker('info', '--format', '{{json .}}', context=context))
    if not info.get('ID'):
        raise InstallationError('Docker daemon identity is unavailable')
    docker('compose', 'version', context=context)
    return {'context': context, 'daemon_id': info['ID']}


def verify_engine(profile: dict) -> None:
    current = engine_identity()
    if current != profile['engine']:
        raise InstallationError('Docker context or daemon changed; restore the registered Docker engine before continuing')


def inspect(profile: dict, container: str) -> dict:
    value = json.loads(docker('inspect', container, context=profile['engine']['context']))[0]
    labels = value.get('Config', {}).get('Labels') or {}
    if (labels.get(LABEL) != profile['installation_id']
            or labels.get('com.docker.compose.project') != profile['project']
            or value['Image'] != profile['image_id']):
        raise InstallationError('Container identity does not match this installation')
    return value


def compose(profile: dict, *args: str) -> str:
    verify_engine(profile)
    assert_project_owned(profile)
    return docker('compose', '-p', profile['project'], '-f', profile['compose'], *args,
                  context=profile['engine']['context'])


def assert_project_owned(profile: dict) -> None:
    context = profile['engine']['context']
    ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + profile['project'], context=context).split()
    for container in ids:
        info = json.loads(docker('inspect', container, context=context))[0]
        if (info.get('Config', {}).get('Labels') or {}).get(LABEL) != profile['installation_id']:
            raise InstallationError('Compose project contains a foreign container; no services changed')
    for name in ('config', 'data', 'runtime', 'profiles'):
        volume = profile['project'] + '_' + name
        ids = docker('volume', 'ls', '-q', '--filter', 'name=^' + volume + '$', context=context).split()
        for found in ids:
            info = json.loads(docker('volume', 'inspect', found, context=context))[0]
            if (info.get('Labels') or {}).get(LABEL) != profile['installation_id']:
                raise InstallationError('Compose volume belongs to another installation')


def service_ids(profile: dict) -> dict:
    ids = docker('ps', '-aq', '--filter', f'label={LABEL}={profile["installation_id"]}',
                 context=profile['engine']['context']).split()
    result = {}
    for container in ids:
        info = inspect(profile, container)
        service = info['Config']['Labels'].get('com.docker.compose.service')
        if service not in ('runner', 'webui') or service in result:
            raise InstallationError('Unexpected or duplicate service for this installation')
        result[service] = info
    return result


def refresh_binding(profile: dict, path: Path) -> None:
    from .docker_webui import binding
    current = service_ids(profile)
    if set(current) != {'runner', 'webui'}:
        raise InstallationError('Runner/WebUI container missing; run setup to repair owned services')
    runner, webui = current['runner'], current['webui']
    expected = {m['target']: m for m in compose_spec(profile)['services']['runner']['volumes']}
    actual = {m['Destination']: m for m in runner.get('Mounts', []) if m['Type'] != 'tmpfs'}
    if set(actual) != set(expected):
        raise InstallationError('Runner mounts changed')
    for target, mount in expected.items():
        observed = actual[target]
        source = mount['source'] if mount['type'] == 'bind' else profile['project'] + '_' + mount['source']
        observed_source = observed.get('Name')
        if mount['type'] == 'bind':
            observed_source = docker_host_path(observed.get('Source', '')).resolve()
            source = Path(source).resolve()
        if (observed_source != source
                or observed.get('RW') != (not mount.get('read_only', False))):
            raise InstallationError('Runner mount ownership changed')
    profile['container_id'] = runner['Id']
    profile['webui'] = binding(runner, webui)
    private_json(path, profile)


def verify_binding(profile: dict) -> None:
    verify_engine(profile)
    current = service_ids(profile)
    if (current.get('runner', {}).get('Id') != profile.get('container_id')
            or current.get('webui', {}).get('Id') != profile.get('webui', {}).get('container_id')):
        raise InstallationError('Service instance changed outside managed setup; run setup to verify and repair')
    from .docker_webui import binding
    if binding(current['runner'], current['webui']) != profile['webui']:
        raise InstallationError('WebUI binding changed')


def compose_spec(profile: dict) -> dict:
    root = Path(profile['root'])
    labels = {LABEL: profile['installation_id']}
    common = {'image': profile['image_id'], 'platform': 'linux/' + image_architecture(profile.get('platform', 'macos-arm64')), 'user': '1000:1000',
              'init': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
              'labels': labels, 'restart': 'no'}
    mounts = [{'type': 'volume', 'source': name, 'target': target} for name, target in
              (('config', '/home/agentbc/.abc'), ('data', '/data/workspace'),
               ('runtime', '/home/agentbc/.local'), ('profiles', '/profiles'))]
    from .docker_install_paths import data_mount
    mounts = [m for m in mounts if m['source'] != 'data'] + [data_mount(profile)]
    mounts.extend({'type': 'bind', 'source': path, 'target': f'/projects/{i}',
                   'bind': {'create_host_path': False}} for i, path in enumerate(profile.get('projects', [])))
    runner = {**common, 'environment': {'AGENTBC_SESSION_STORE': 'isolated_container'}, 'volumes': mounts, 'tmpfs': ['/run/agentbc:uid=1000,gid=1000,mode=0700'],
              'security_opt': ['no-new-privileges:true', f'seccomp={root / "bundle/seccomp.json"}'],
              'command': ['runner', 'serve', '--state-root', '/home/agentbc/.abc/runner',
                          '--allow-root', '/data/workspace', *[x for i in range(len(profile.get('projects', [])))
                                                            for x in ('--allow-root', f'/projects/{i}')]],
              'working_dir': '/data/workspace', 'stop_grace_period': '30s',
              'healthcheck': {'test': ['CMD', 'agentbc', 'runner', 'status'],
                              'interval': '5s', 'timeout': '3s', 'start_period': '10s', 'retries': 3}}
    if profile.get('managed_runner_entry'):
        runner['entrypoint'] = ['python', '-m', 'agent_bridge_connect.docker_runner_entry']
    port = profile['settings']['port']
    webui = {**common, 'entrypoint': ['agentbc', 'webui', 'serve'],
             'command': ['--root', '/data/workspace/record', '--report-root', '/data/workspace/tasks/report',
                         '--listen-host', '0.0.0.0', '--port', '8080', '--public-origin',
                         f'http://127.0.0.1:{port}', '--launch-file', '/run/webui/launch-url'],
             'volumes': [data_mount(profile, readonly=True)],
             'ports': [{'target': 8080, 'published': str(port), 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
             'read_only': True, 'tmpfs': ['/run/webui:mode=0700,uid=1000,gid=1000'],
             'healthcheck': {'test': ['CMD', 'python', '-c',
                 "from pathlib import Path; from urllib.request import Request,urlopen; from urllib.parse import urlsplit; u=urlsplit(Path('/run/webui/launch-url').read_text().strip()); urlopen(Request('http://127.0.0.1:8080/',headers={'Host':u.netloc}),timeout=2).read(1)"],
                 'interval': '5s', 'timeout': '3s', 'retries': 3}}
    return {'services': {'runner': runner, 'webui': webui},
            'volumes': {name: {'labels': labels} for name in ('config', 'data', 'runtime', 'profiles')},
            'networks': {'default': {'labels': labels}}}


def write_compose(profile: dict) -> None:
    def escape(value):
        if isinstance(value, str):
            return value.replace('$', '$$')
        if isinstance(value, list):
            return [escape(item) for item in value]
        if isinstance(value, dict):
            return {key: escape(item) for key, item in value.items()}
        return value
    private_json(Path(profile['compose']), escape(compose_spec(profile)))


def ready(profile: dict, service: str = 'runner') -> None:
    deadline = time.monotonic() + 45
    last_status = 'missing'
    while time.monotonic() < deadline:
        current = service_ids(profile).get(service, {})
        state = current.get('State', {})
        health = state.get('Health', {}).get('Status')
        last_status = health or state.get('Status', 'missing')
        if state.get('Running') and health == 'healthy':
            return
        if state.get('Running') and not health:
            raise InstallationError(f'{service} has no active healthcheck; repair the managed Compose configuration')
        if state.get('Status') in ('exited', 'dead'):
            raise InstallationError(f'{service} exited before readiness (exit code {state.get("ExitCode", "unknown")}); inspect its startup logs')
        time.sleep(0.5)
    raise InstallationError(f'{service} did not become healthy within 45 seconds (last state: {last_status})')


def volume_python(profile: dict, code: str, *, readonly: bool = False) -> str:
    from .docker_install_paths import snapshot_mount
    if profile.get('host_workspace'):
        return docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                      '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                      '--mount', snapshot_mount(profile, 'data', readonly=readonly, target='/data/workspace'),
                      '--entrypoint', 'python', profile['image_id'], '-c', code, context=profile['engine']['context'])
    volume = profile['project'] + '_data'
    info = json.loads(docker('volume', 'inspect', volume, context=profile['engine']['context']))[0]
    if (info.get('Labels') or {}).get(LABEL) != profile['installation_id']:
        raise InstallationError('Data volume ownership changed')
    return docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                  '--mount', f'type=volume,src={volume},dst=/data/workspace' + (',readonly' if readonly else ''),
                  '--entrypoint', 'python', profile['image_id'], '-c', code,
                  context=profile['engine']['context'])


def require_idle(profile: dict) -> None:
    """Use authoritative store + open leases, never infer idle from PID/health."""
    code = """
import json
from pathlib import Path
from agent_bridge_connect.task_store import TaskStore
root=Path('/data/workspace/record')
s=TaskStore(root) if root.exists() else None
from agent_bridge_connect.terminal_states import TASK_TERMINAL_STATES
active=[]
for task in (s.list_tasks() if s else []):
    if task.get('status') not in (TASK_TERMINAL_STATES | {'cancelled', 'rejected'}):
        active.append(task.get('id'))
# Any open lease, including a failed task with unfinished execution, blocks maintenance.
for path in root.rglob('*lease*.json'):
    value=json.loads(path.read_text())
    if value.get('state') != 'closed':
        active.append(str(path))
print(json.dumps(active))
"""
    active = json.loads(volume_python(profile, code, readonly=True))
    if active:
        raise InstallationError('Unfinished tasks or leases prevent maintenance; resolve them with normal task commands first')


def clear_stopped_legacy_pid(profile: dict) -> None:
    """Recovery for the pre-managed development fixture, after all services stop."""
    assert_project_owned(profile)
    ids = docker('ps', '-q', '--filter', 'label=io.agentbc.installation=' + profile['installation_id'],
                 context=profile['engine']['context']).split()
    if ids:
        raise InstallationError('Cannot clear runtime identity while a service is running')
    docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--user', '1000:1000',
           '--mount', f'type=volume,src={profile["project"]}_config,dst=/snapshot',
           '--entrypoint', 'python', profile['image_id'], '-c',
           "from pathlib import Path; Path('/snapshot/runner/runner.pid').unlink(missing_ok=True)",
           context=profile['engine']['context'])
