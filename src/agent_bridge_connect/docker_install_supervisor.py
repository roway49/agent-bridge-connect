"""User LaunchAgent supervising only the registered Docker installation."""
from __future__ import annotations

from .host_platform import try_lock

import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

from .installation import InstallationError, installation_lock, private_json, read_receipt
from .docker_install_runtime import compose, ready, service_ids, verify_binding


def register(profile: dict, *, enable: bool) -> None:
    if sys.platform == 'win32':
        from .windows_desktop import register as windows_register
        windows_register(Path(profile['root']) / 'docker-host.json', enable=enable)
        return
    label = 'com.agentbc.docker.' + profile['installation_id']
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    domain = f'gui/{os.getuid()}'
    old = None
    if path.exists():
        old = plistlib.loads(path.read_bytes())
        if old.get('AgentBCInstallation') != profile['installation_id']:
            raise InstallationError('Foreign login item at the managed path')
    if not enable:
        if old:
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
        path.unlink(missing_ok=True)
        return
    root = Path(profile['root'])
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label': label, 'AgentBCInstallation': profile['installation_id'],
             'ProgramArguments': [str(root / 'venv/bin/python'), '-m', 'agent_bridge_connect.docker_install_supervisor',
                                  str(root / 'docker-host.json')], 'RunAtLoad': True,
             'ProcessType': 'Background', 'ExitTimeOut': 10,
             'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                                      'HOME': str(Path.home()),
                                      **({'DOCKER_CONFIG': os.environ['DOCKER_CONFIG']} if 'DOCKER_CONFIG' in os.environ else {})},
             'StandardOutPath': str(root / 'supervisor.log'), 'StandardErrorPath': str(root / 'supervisor.log')}
    if old == value and subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True).returncode == 0:
        if subprocess.run(['launchctl', 'kickstart', domain + '/' + label], capture_output=True).returncode:
            raise InstallationError('Could not resume the registered Docker login service')
        return
    if old:
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
        deadline = time.monotonic() + 10
        while subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise InstallationError('Previous Docker login service has not exited')
            time.sleep(0.2)
    fd = os.open(root / 'supervisor.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(plistlib.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)
    # A maintenance restart is not a new login. Consume this once so explicit
    # close/stop choices survive updates; a later real login restores services.
    (root / 'supervisor-preserve-once').touch(mode=0o600)
    deadline = time.monotonic() + 5
    while True:
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
        if result.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise InstallationError('Could not register the Docker login service; run setup again to repair')
        time.sleep(0.25)


def supervise(path: Path) -> None:
    root = path.parent
    fd = os.open(root / 'supervisor.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try_lock(fd)
    except BlockingIOError:
        os.close(fd)
        return
    stopping = False
    bridge = None

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    preserve = root / 'supervisor-preserve-once'
    offline, delay = not preserve.exists(), 2
    preserve.unlink(missing_ok=True)
    try:
        while not stopping:
            receipt = read_receipt()
            if not receipt or receipt['type'] != 'docker':
                return
            if receipt['state'] != 'installed':
                time.sleep(2)
                continue
            try:
                with installation_lock():
                    profile = json.loads(path.read_text())
                    verify_binding(profile)
                    if offline:
                        (root / 'webui.closed').unlink(missing_ok=True)
                        (root / 'runner.closed').unlink(missing_ok=True)
                        compose(profile, 'start', 'runner')
                        ready(profile)
                    current = service_ids(profile)
                    if (profile['settings']['autostart'] and not (root / 'webui.closed').exists()
                            and not current['webui']['State']['Running']):
                        compose(profile, 'start', 'webui')
                    if (current['runner']['State']['Running'] and not (root / 'runner.closed').exists()
                            and (bridge is None or bridge.poll() is not None)):
                        log = os.open(root / 'approval-bridge.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                        try:
                            env = dict(os.environ)
                            env.pop('AGENTBC_HEADLESS_NOTIFICATIONS', None)
                            bridge = subprocess.Popen([sys.executable, '-m', 'agent_bridge_connect.docker_approval_bridge',
                                                       '--container', profile['container_id'], '--watch',
                                                       '--state-file', str(root / 'approval-bridge-state.json')],
                                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
                        finally:
                            os.close(log)
                    private_json(root / 'supervisor-state.json', {'state': 'ready'})
                    offline, delay = False, 2
            except InstallationError as exc:
                message = str(exc)
                if 'Another installation' in message:
                    pass
                elif 'Docker operation failed' in message:
                    offline, delay = True, min(delay * 2, 30)
                    private_json(root / 'supervisor-state.json', {'state': 'waiting_for_docker'})
                else:
                    private_json(root / 'supervisor-state.json', {'state': 'repair_required', 'reason': message})
                    return
            for _ in range(delay):
                if stopping:
                    break
                time.sleep(1)
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=5)
        os.close(fd)


if __name__ == '__main__':
    supervise(Path(sys.argv[1]))
