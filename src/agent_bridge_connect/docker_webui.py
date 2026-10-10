"""Manage only an explicitly bound read-only Docker WebUI service."""
from __future__ import annotations

import argparse
import json
import time
import socket
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import webbrowser

from .docker_approval_bridge import execute


def inspect(container: str) -> dict:
    return json.loads(execute(['docker', 'inspect', container]))[0]


def binding(runner: dict, webui: dict) -> dict:
    labels = webui['Config'].get('Labels') or {}
    runner_labels = runner['Config'].get('Labels') or {}
    if (labels.get('com.docker.compose.service') != 'webui'
            or not runner_labels.get('com.docker.compose.project')
            or labels.get('com.docker.compose.project') != runner_labels['com.docker.compose.project']):
        raise ValueError('WebUI must be the webui service of the bound Runner Compose project')
    data = next((m for m in runner.get('Mounts', []) if m.get('Destination') == '/data/workspace'), None)
    mounts = webui.get('Mounts', [])
    shared = next((m for m in mounts if m.get('Destination') == '/data/workspace'), None)
    if not data or not shared or shared.get('Source') != data.get('Source') or shared.get('RW'):
        raise ValueError('WebUI must mount the exact Runner data volume read-only')
    if any(m.get('Type') != 'tmpfs' and m.get('Destination') != '/data/workspace' for m in mounts):
        raise ValueError('WebUI cannot mount Executor profiles or other host data')
    if not webui['HostConfig'].get('ReadonlyRootfs'):
        raise ValueError('WebUI root filesystem must be read-only')
    ports = webui['HostConfig'].get('PortBindings') or {}
    published = ports.get('8080/tcp') or []
    if set(ports) != {'8080/tcp'} or len(published) != 1 or published[0].get('HostIp') != '127.0.0.1':
        raise ValueError('WebUI must publish exactly one fixed loopback port')
    port = int(published[0]['HostPort'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid fixed WebUI port')
    return {'container_id': webui['Id'], 'port': port,
            'origin': f'http://127.0.0.1:{port}', 'data_source': shared['Source']}


def verify(profile: dict) -> tuple[dict, dict]:
    saved = profile.get('webui')
    if not isinstance(saved, dict):
        raise ValueError('No Docker WebUI binding; create the Compose webui service and install with --webui-container')
    runner = inspect(profile['container_id'])
    webui = inspect(saved['container_id'])
    if runner['Id'] != profile['container_id'] or binding(runner, webui) != saved:
        raise ValueError('Docker WebUI identity, port or data source changed; explicitly rebind')
    return saved, webui


def launch_url(saved: dict) -> str:
    url = execute(['docker', 'exec', saved['container_id'], 'cat', '/run/webui/launch-url']).strip()
    parsed = urlsplit(url)
    tokens = parse_qs(parsed.fragment)
    if (f'{parsed.scheme}://{parsed.netloc}' != saved['origin'] or parsed.path != '/'
            or parsed.query or set(tokens) != {'token'} or len(tokens['token']) != 1
            or len(tokens['token'][0]) < 32):
        raise ValueError('Invalid private WebUI launch receipt')
    return url


def port_error(port: int) -> str:
    """Read-only bind probe for a stopped Windows service; never changes its port."""
    if sys.platform != 'win32':
        return ''
    try:
        with socket.socket() as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', port))
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 10013 or exc.errno in (13, 10013):
            return f'Windows blocks fixed WebUI port {port} (reserved or access denied). Run agentbc setup and choose Assign a new port. Port was not changed.'
        return f'Fixed WebUI port {port} is unavailable. Run agentbc setup and choose Assign a new port. Port was not changed.'
    return ''


def command(profile: dict, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='agentbc webui')
    parser.add_argument('action', choices=('start', 'close', 'status'), nargs='?', default='start')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    saved, current = verify(profile)
    running = current['State'].get('Running', False)
    if args.action == 'status':
        print(json.dumps({'running': running, 'port': saved['port'], 'origin': saved['origin'],
                          'restart_policy': current['HostConfig'].get('RestartPolicy', {}).get('Name', 'no'),
                          'health': current['State'].get('Health', {}).get('Status', 'unknown') if running else 'stopped',
                          'error': current['State'].get('Error', '') or (port_error(saved['port']) if not running else '')}, ensure_ascii=False))
        return 0
    if args.action == 'close':
        if running:
            execute(['docker', 'stop', '--time', '5', saved['container_id']])
        print('Docker WebUI stopped; Runner and task state unchanged.')
        return 0
    if not running:
        error = port_error(saved['port'])
        if error:
            raise RuntimeError(error)
        try:
            execute(['docker', 'start', saved['container_id']])
        except RuntimeError as exc:
            raise RuntimeError('Docker WebUI start failed; check the saved port for conflicts and Docker service state. Port was not changed.') from exc
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            url = launch_url(saved)
            with urlopen(saved['origin'] + '/', timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError('WebUI is not ready')
            if not args.no_open:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass  # A missing desktop must not terminate the service.
            print('WebUI: ' + url)
            return 0
        except (OSError, RuntimeError):
            time.sleep(0.25)
    raise RuntimeError('Docker WebUI did not become ready; check webui status and container logs; fixed port was not changed')
