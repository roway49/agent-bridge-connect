"""Explicit macOS host approval bridge for a named Docker acceptance instance.

No Docker socket is mounted into containers. Only the host invokes Docker, and
all decisions go through the installed container CLI's normal validation.
"""
from __future__ import annotations

from .host_platform import try_lock

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .desktop_dialog import show_approval
from .reports import redact_secrets
from .installation import private_json

SNAPSHOT = '''
import json,sys
from agent_bridge_connect.config import load_config
from agent_bridge_connect.service import TaskService
from agent_bridge_connect.notifications import build_input_required_notification
s=TaskService('/data/workspace/record',config=load_config('/home/agentbc/.abc/config.toml'))
t=s.get_task(sys.argv[1]); r=(t.extensions or {}).get('agentbc.input',{})
p=build_input_required_notification(s,t.id) if t.status=='input_required' and r.get('status')=='waiting' and r.get('type')=='permission' else None
print(json.dumps({'status':t.status,'request':{k:r.get(k) for k in ('input_id','executor_run_id','session_id','request_id','request_fingerprint','deadline_at')},'payload':p}))
'''
DISCOVER = """
import json
from agent_bridge_connect.task_store import TaskStore
s=TaskStore('/data/workspace/record')
print(json.dumps([t['id'] for t in s.list_tasks(status='input_required')]))
"""
TERMINAL = {'completed', 'failed', 'cancelled', 'needs_recovery'}


def execute(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        # Include only structural error identifiers, never command arguments,
        # notification bodies, credentials or arbitrary executor output.
        output = (result.stdout or '') + '\n' + (result.stderr or '')
        codes = re.findall(r'(?m)^(?:respond_error: )?([a-z][a-z0-9_]{2,80}):', output)
        exceptions = re.findall(r'(?m)^([A-Z][A-Za-z0-9]{0,80}(?:Error|Exception)):', output)
        phase = 'respond' if 'respond' in args else 'snapshot' if SNAPSHOT in args else 'docker'
        reason = ','.join(dict.fromkeys(codes + exceptions)) or 'unclassified'
        raise RuntimeError(f'Command failed ({result.returncode}): {args[0]} {args[1]}; phase={phase}; code={reason}')
    return result.stdout


def pending(snapshot: dict) -> bool:
    payload = snapshot.get('payload')
    if snapshot.get('status') != 'input_required' or not isinstance(payload, dict):
        return False
    try:
        deadline = datetime.fromisoformat(snapshot['request']['deadline_at'].replace('Z', '+00:00'))
        return deadline > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False


def decision_args(payload: dict, details: dict) -> list[str]:
    action = details.get('action')
    if payload.get('approval_version') == 3:
        if action in {'approve', 'approve_full'} and details.get('decision_source') == 'user':
            return ['--approve-full']
        if action == 'deny':
            source = details.get('decision_source', 'user')
            if source not in {'user', 'timeout', 'dialog_closed'}:
                raise ValueError('Unknown denial source; no response sent')
            return ['--deny'] if source == 'user' else ['--deny', '--denial-source', source]
    if payload.get('approval_version') == 2 and action == 'permission_option':
        handle = details.get('option_handle')
        offered = {o.get('handle') for o in payload.get('native_options', []) if isinstance(o, dict)}
        if handle and handle in offered:
            return ['--permission-option', handle]
    raise ValueError('Unsupported or non-decision dialog result; no response sent')


class DeliveryFailure(RuntimeError):
    """No user decision was obtained; a bounded delivery retry is safe."""


def safe_error(exc: Exception) -> str:
    return str(redact_secrets(str(exc))).replace('\n', ' ')[:800]


def notify_delivery_failure(task: str) -> None:
    # Informational only: no buttons, no approval response, no business mutation.
    script = ('on run argv\n display notification (item 1 of argv) '
              'with title "AgentBC approval delivery failed"\nend run')
    try:
        subprocess.run(['/usr/bin/osascript', '-',
                        f'{task}: approval window failed. Run agentbc task status {task} --json for the pending request.'],
                       input=script, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # The durable state file and doctor remain available without GUI.


def read_delivery_state(root: Path, container: str) -> dict:
    if sys.platform == 'win32':
        from .windows_desktop import status
        state = status(root)
        if state.get('container_id') not in (None, container):
            return {'state': 'degraded', 'reason': 'container_changed'}
        return state
    try:
        value = json.loads((root / 'approval-bridge-state.json').read_text())
        if value['container_id'] != container:
            return {'state': 'unknown', 'reason': 'container_changed'}
        updated = datetime.fromisoformat(value['updated_at'])
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            value['state'] = 'stale'
        terminal = root / 'terminal-bridge-state.json'
        if terminal.exists():
            saved = json.loads(terminal.read_text())
            value['terminal_notifications'] = saved
            if saved.get('state') == 'degraded':
                value['state'] = 'degraded'
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'reason': 'delivery_state_unavailable'}


class DeliveryQueue:
    """One in-flight attempt per request; never retry an ambiguous submission."""
    def __init__(self):
        self.requests = {}
        self.recent_failures = []

    def restore(self, path, container):
        if not path or not path.exists():
            return
        saved = json.loads(path.read_text())
        if saved.get('container_id') != container:
            return
        self.recent_failures = saved.get('recent_failures', [])[-16:]
        for item in saved.get('requests', []):
            key = (item['task_id'], item['request_key'])
            if item['state'] == 'delivering':
                # A killed process may already have shown or answered its dialog.
                # Never ask again when the previous outcome is unknown.
                item.update(state='manual_action_required', error='Bridge restarted during delivery; check the authoritative request.',
                            next_step=f"agentbc task status {key[0]} --json")
            if item['state'] == 'retry_wait':
                item['retry_at'] = time.monotonic() + 2 ** item['attempts']
            self.requests[key] = item

    def eligible(self, key, now):
        item = self.requests.get(key)
        return item is None or (item['state'] == 'retry_wait' and now >= item['retry_at'])

    def start(self, key):
        attempts = self.requests.get(key, {}).get('attempts', 0) + 1
        self.requests[key] = {'task_id': key[0], 'request_key': key[1], 'state': 'delivering', 'attempts': attempts}

    def finish(self, key, *, now, error=None, result=''):
        item = self.requests[key]
        if error is None:
            item.update(state='done', result=result)
            return
        item['error'] = safe_error(error)
        if isinstance(error, DeliveryFailure) and item['attempts'] < 3:
            item.update(state='retry_wait', retry_at=now + 2 ** item['attempts'])
        else:
            item['state'] = 'manual_action_required'
        item['next_step'] = f"agentbc task status {key[0]} --json"
        self.recent_failures = (self.recent_failures + [dict(item)])[-16:]

    def prune(self, active):
        self.requests = {key: value for key, value in self.requests.items()
                         if key in active or value['state'] == 'delivering'}

    def save(self, path, container):
        if path:
            items = list(self.requests.values())
            private_json(path, {'container_id': container, 'updated_at': datetime.now(timezone.utc).isoformat(),
                'state': 'degraded' if any(i['state'] in ('retry_wait', 'manual_action_required') for i in items) else 'ready',
                'requests': items, 'recent_failures': self.recent_failures})


class Bridge:
    def __init__(self, container: str):
        info = json.loads(execute(['docker', 'inspect', container]))[0]
        if not info['State']['Running']:
            raise RuntimeError('Container is not running')
        self.container = info['Id']  # Never follow a replacement with the same name.

    def discover(self) -> list[str]:
        tasks = json.loads(execute(['docker', 'exec', self.container, 'python', '-c', DISCOVER]))
        if not isinstance(tasks, list) or any(not isinstance(t, str) or not re.fullmatch(r'[A-Z0-9]+-\d+', t) for t in tasks):
            raise RuntimeError('Invalid discovery response')
        return tasks

    def snapshot(self, task: str) -> dict:
        return json.loads(execute(['docker', 'exec', self.container, 'python', '-c', SNAPSHOT, task]))

    def deliver(self, task: str, before: dict) -> str:
        if not pending(before):
            return 'expired or no longer pending'
        payload = dict(before['payload'])
        payload['dialog_title'] = 'Docker · ' + payload.get('dialog_title', task)
        result = show_approval(payload)
        if not result.ok and (result.details or {}).get("uncertain"):
            raise RuntimeError("Desktop approval outcome is uncertain; inspect the authoritative request")
        if not result.ok:
            raise DeliveryFailure('Host dialog was not delivered: ' + str(redact_secrets(result.message)))
        args = decision_args(payload, result.details or {})
        after = self.snapshot(task)
        if not pending(after) or after['request'] != before['request']:
            return 'stale decision discarded'
        execute(['docker', 'exec', self.container, 'agentbc', 'task', 'respond', task,
                 '--root', '/data/workspace/record', '--config', '/home/agentbc/.abc/config.toml',
                 '--input', before['request']['input_id'], *args])
        return 'decision accepted by container CLI'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--task', action='append', help='Exact task ID; omit with --watch for automatic discovery')
    parser.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--state-file', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--watch', action='store_true', help='Discover pending approvals until the pinned container stops')
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.environ.get('AGENTBC_HEADLESS_NOTIFICATIONS'):
        parser.error('Run on the macOS desktop host without AGENTBC_HEADLESS_NOTIFICATIONS')
    if not args.task and not args.watch:
        parser.error('Choose --task or --watch')
    if any(not re.fullmatch(r'[A-Z0-9]+-\d+', t) for t in (args.task or [])):
        parser.error('Invalid task ID')
    bridge = Bridge(args.container)
    if args.watch:
        bridge.discover()  # Verify the container read contract before advertising readiness.

    def ready():
        if args.ready_fd is not None:
            os.write(args.ready_fd, b'ready\n')
            os.close(args.ready_fd)

    # One bridge per exact container, including across terminal invocations.
    lock_dir = Path(tempfile.gettempdir()) / f'agentbc-docker-approval-{os.getuid()}'
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    if lock_dir.is_symlink() or lock_dir.stat().st_uid != os.getuid() or lock_dir.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe bridge lock directory')
    lock_path = lock_dir / hashlib.sha256(bridge.container.encode()).hexdigest()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock, ThreadPoolExecutor(max_workers=4) as pool:
        try:
            try_lock(lock.fileno())
        except BlockingIOError:
            try:
                owner = json.load(lock)
            except (ValueError, OSError):
                owner = {}
            if not owner.get('watch'):
                raise RuntimeError('An explicit-task bridge owns this container; stop it before automatic discovery') from None
            ready()
            return  # A watch owner already covers every pending task in this container.
        lock.seek(0)
        lock.truncate()
        json.dump({'watch': args.watch, 'pid': os.getpid()}, lock)
        lock.flush()
        ready()
        queue, futures = DeliveryQueue(), {}
        queue.restore(args.state_file, bridge.container)
        from .docker_terminal_bridge import TerminalRelay
        terminal = TerminalRelay(bridge.container, args.state_file.with_name('terminal-bridge-state.json')) if args.watch and args.state_file else None
        remaining = set(args.task or [])
        print('Host approval bridge ready; automatic discovery active.' if args.watch else 'Host approval bridge ready; keep this terminal open.', flush=True)
        while args.watch or remaining or futures:
            if args.watch:
                remaining = set(bridge.discover())
                if terminal is not None:
                    terminal.tick()
            active_keys = set()
            for task in sorted(remaining):
                snap = bridge.snapshot(task)
                if snap['status'] in TERMINAL:
                    print(f'{task}: {snap["status"]}', flush=True)
                    remaining.remove(task)
                    continue
                key = (task, json.dumps(snap['request'], sort_keys=True))
                active_keys.add(key)
                if pending(snap) and queue.eligible(key, time.monotonic()) and len(futures) < 4:
                    queue.start(key)
                    queue.save(args.state_file, bridge.container)
                    futures[pool.submit(bridge.deliver, task, snap)] = key
                    print(f'{task}: attempting host approval delivery', flush=True)
            for future in list(futures):
                if future.done():
                    key = futures.pop(future)
                    task = key[0]
                    try:
                        result = future.result()
                        queue.finish(key, now=time.monotonic(), result=result)
                        print(f'{task}: {result}', flush=True)
                    except Exception as exc:
                        queue.finish(key, now=time.monotonic(), error=exc)
                        item = queue.requests[key]
                        if item['attempts'] == 1:
                            notify_delivery_failure(task)
                        print(f'{task}: {item["state"]}: {item["error"]}; no automatic decision. Check: {item["next_step"]}', flush=True)
            queue.prune(active_keys)
            queue.save(args.state_file, bridge.container)
            if args.watch or remaining or futures:
                time.sleep(2)


if __name__ == '__main__':
    main()
