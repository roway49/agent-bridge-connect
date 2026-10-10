"""Host terminal notices: serial dialogs, durable deduplication, exact receipts."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import sys
import subprocess
import time

from .installation import private_json

MODULE = 'agent_bridge_connect.docker_terminal_delivery'


def show_notice(payload):
    # No container path is opened on the host. Reports remain available through
    # the installed CLI / WebUI; the dialog is informational, with one OK button.
    if sys.platform == 'win32':
        from .desktop_dialog import windows_notice
        return windows_notice(payload)
    task = payload['task_id']
    body = str(payload.get('message', ''))[:3000]
    body = '\n'.join(line for line in body.splitlines() if not line.startswith('Report:'))
    body += f'\nReport: agentbc task report {task}'
    script = ('on run argv\n activate\n display dialog (item 2 of argv) with title (item 1 of argv) '
              'buttons {"OK"} default button "OK" giving up after 30\nend run')
    result = subprocess.run(['/usr/bin/osascript', '-', 'AgentBC · Docker · ' + task, body],
                            input=script, text=True, capture_output=True, timeout=35)
    return result.returncode == 0


class TerminalRelay:
    def __init__(self, container, path):
        self.container, self.path = container, Path(path)
        self.entries = json.loads(self.path.read_text()).get('entries', {}) if self.path.exists() else {}
        for item in self.entries.values():
            if item['state'] == 'delivering':
                item.update(state='manual_action_required', error='Host restarted during dialog; delivery uncertain')
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.save()

    def save(self):
        private_json(self.path, {'entries': self.entries,
            'state': 'degraded' if any(v['state'] in ('retry_wait', 'manual_action_required') for v in self.entries.values()) else 'ready'})

    def discover(self):
        from .docker_approval_bridge import execute
        reply = json.loads(execute(['docker', 'exec', self.container, 'python', '-m', MODULE, 'discover']))
        values = reply['result']
        if not isinstance(values, list) or any(not isinstance(v, dict) or not re.fullmatch(r'[A-Z0-9]+-\d+', v.get('task_id', ''))
                or not re.fullmatch(r'[a-f0-9-]{36}', v.get('delivery_id', '')) for v in values):
            raise ValueError('Invalid terminal discovery response')
        return values

    def tick(self):
        if self.future is not None:
            if not self.future.done():
                return
            self.future.result()
            self.future = None
        values = self.discover()
        keys = {self.key(v) for v in values}
        self.entries = {k: v for k, v in self.entries.items() if k in keys}
        for value in values:
            key = self.key(value)
            item = self.entries.get(key)
            if item and (item['state'] in ('done', 'manual_action_required') or item.get('retry_at', 0) > time.time()):
                continue
            self.future = self.pool.submit(self.deliver, value)
            break
        # Worker owns persistence until its future completes.
        if self.future is None:
            self.save()

    @staticmethod
    def key(value):
        return hashlib.sha256((value['task_id'] + ':' + value['delivery_id']).encode()).hexdigest()

    def deliver(self, value):
        key = self.key(value)
        item = self.entries.setdefault(key, dict(value, state='queued', attempts=0))
        replay_ack = item['state'] == 'ack_pending'
        item['attempts'] += 1
        process = None
        prompted = False
        try:
            self.save()
            process = subprocess.Popen(['docker', 'exec', '-i', self.container, 'python', '-m', MODULE,
                                        'deliver', value['task_id'], value['delivery_id']],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            from .desktop_dialog import read_line
            line = read_line(process, 30)
            request = json.loads(line)
            if 'result' in request:
                item['state'] = 'done'  # Already confirmed, stale, or no longer due: recheck discovery.
                output, _ = process.communicate(timeout=10)
                self.entries.pop(key, None)
                return
            if request.get('task_id') != value['task_id'] or request.get('delivery_id') != value['delivery_id'] or request.get('payload', {}).get('task_id') != value['task_id']:
                raise ValueError('Terminal receipt binding mismatch')
            if not replay_ack:
                item['state'] = 'delivering'
                self.save()  # Must be durable BEFORE UI.
                prompted = True
                if not show_notice(request['payload']):
                    prompted = False  # Explicit osascript failure, no success acknowledgement.
                    raise RuntimeError('Host terminal dialog failed')
                item['state'] = 'ack_pending'
                self.save()  # Persist actual UI success before acknowledging Core.
            output, _ = process.communicate(json.dumps({'delivery_id': value['delivery_id'], 'shown': True}) + '\n', timeout=15)
            if process.returncode != 0:
                raise RuntimeError('Terminal acknowledgement transport failed')
            result = json.loads(output)['result']
            if result.get('delivery_id') != value['delivery_id'] or result.get('receipt', {}).get('stages', {}).get('ui_notification', {}).get('state') != 'succeeded':
                raise RuntimeError('Terminal acknowledgement was not committed')
            item.update(state='done', error='')
        except Exception as exc:
            from .docker_approval_bridge import safe_error
            item['error'] = safe_error(exc)
            if item['state'] == 'ack_pending':
                item['retry_at'] = time.time() + min(30, 2 ** min(item['attempts'], 5))
            elif prompted:
                item['state'] = 'manual_action_required'
            else:
                item.update(state='retry_wait' if item['attempts'] < 3 else 'manual_action_required', retry_at=time.time() + 2 ** min(item['attempts'], 5))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            self.save()

    def close(self):
        self.pool.shutdown(wait=True)
