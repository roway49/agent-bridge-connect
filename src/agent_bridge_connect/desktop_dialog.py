"""Platform adapter around the existing approval and notification contracts."""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time

from .adapters import DeliveryResult
from .notifiers.dialog import DialogNotifier
from .reports import redact_secrets
from .windows_dialog import MAX_BYTES, validate_request


def read_line(process, timeout: float, limit: int = MAX_BYTES):
    """Bounded pipe read on Windows and Unix; kill and reap on timeout/overflow."""
    import queue
    import threading
    results = queue.Queue(maxsize=1)
    def read():
        try:
            results.put((process.stdout.readline(limit + 1), None))
        except Exception as exc:
            results.put((None, exc))
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        try:
            line, error = results.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError('Desktop pipe response timed out') from exc
        if error:
            raise error
        newline = '\n' if isinstance(line, str) else b'\n'
        if not line or len(line) > limit or not line.endswith(newline):
            raise ValueError('Invalid bounded desktop pipe response')
        return line
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=5)


def windows_request(request: dict) -> dict:
    request = validate_request(request)
    encoded = (json.dumps(request, ensure_ascii=True) + '\n').encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError('Oversized desktop request')
    from pathlib import Path
    python = str(Path(sys.executable).with_name("python.exe")) if sys.platform == "win32" else sys.executable
    process = subprocess.Popen([python, '-m', 'agent_bridge_connect.windows_dialog'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        process.stdin.write(encoded)
        process.stdin.close()
        result = json.loads(read_line(process, request['timeout_s'] + 10))
        process.wait(timeout=5)
        if not isinstance(result, dict) or result.get('outcome') not in {'selected', 'closed', 'timeout', 'delivery_failed', 'uncertain'}:
            raise ValueError('Invalid desktop response')
        if result['outcome'] == 'selected' and result.get('button') not in {b['id'] for b in request['buttons']}:
            raise ValueError('Unoffered desktop choice')
        if result['outcome'] in {'selected', 'closed', 'timeout'} and (process.returncode != 0 or result.get('shown') is not True or result.get('visible') is not True):
            raise ValueError('Unverified desktop delivery')
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()


def show_approval(payload: dict) -> DeliveryResult:
    if sys.platform != 'win32':
        return DialogNotifier().send(payload)
    return windows_approval(payload)


def windows_approval(payload: dict, *, render=None, clock=time.monotonic) -> DeliveryResult:
    render = render or windows_request
    clean = redact_secrets(payload)
    version = clean.get('approval_version')
    if version not in (2, 3):
        return DeliveryResult(False, 'Unsupported Windows approval version')
    seconds = clean.get('approval_max_time_s', 300)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        return DeliveryResult(False, 'Invalid approval duration')
    deadline = clock() + seconds
    notifier = DialogNotifier()
    options = [o for o in clean.get('native_options', []) if isinstance(o, dict) and o.get('selectable', True) is not False]
    if version == 2 and not any(o.get('kind') == 'deny' and o.get('handle') for o in options):
        return DeliveryResult(False, 'Missing native denial choice')
    title = str(clean.get('dialog_title') or 'AgentBC approval')
    summary = str(clean.get('reason_summary') or clean.get('message') or '')
    detail = str(clean.get('reason_detail') or summary)
    lines = [('Task', clean.get('identity_task_id') or clean.get('task_id')), ('Title', clean.get('identity_task_title')),
             ('Executor', clean.get('identity_executor')), ('Blocked step', clean.get('identity_blocked_step'))]
    body = '\n'.join(f'{k}: {v}' for k, v in lines if v) + '\n\n' + summary
    if version == 3:
        body += '\nPermission scope: full'
    def deny(source):
        return notifier._native_deny_or_legacy_result(options if version == 2 else [], 'Windows approval denied', source)
    def window(body, buttons, default):
        left = min(deadline - clock(), notifier._permission_give_up_seconds(str(clean.get('deadline_at') or ''), max(1, math.ceil(deadline - clock()))))
        if left <= 0:
            return {'outcome': 'timeout'}
        return render({'title': title, 'body': body[:16000], 'buttons': buttons, 'default': default, 'timeout_s': left})
    def failed(result):
        if result['outcome'] in ('delivery_failed', 'uncertain'):
            return DeliveryResult(False, 'Windows approval delivery unavailable', details={'uncertain': result['outcome'] == 'uncertain'})
        if result['outcome'] in ('closed', 'timeout'):
            return deny('timeout' if result['outcome'] == 'timeout' else 'dialog_closed')
        return None
    while True:
        result = window(body, [{'id': 100, 'text': 'View Details'}, {'id': 101, 'text': 'Deny'},
                               {'id': 102, 'text': 'Approve Full' if version == 3 else 'Approve'}], 101)
        stop = failed(result)
        if stop is not None:
            return stop
        selected = result.get('button')
        if selected == 100:
            stop = failed(window(detail, [{'id': 100, 'text': 'Back'}], 100))
            if stop is not None:
                return stop
            continue
        if selected == 101:
            return deny('user')
        if selected != 102:
            return DeliveryResult(False, 'Unoffered approval choice')
        if version == 3:
            return DeliveryResult(True, 'Windows approval shown', details={'action': 'approve' if clean.get('native_live_elevation') else 'approve_full', 'decision_source': 'user'})
        offered = {110 + i: o for i, o in enumerate(options) if o.get('kind') in ('once', 'session') and o.get('handle')}
        if not offered:
            return DeliveryResult(False, 'No selectable native grant')
        result = window('Choose how long to allow this action.', [{'id': 100, 'text': 'Back'}] +
                        [{'id': i, 'text': 'Once' if o['kind'] == 'once' else 'This Session'} for i, o in offered.items()], 100)
        stop = failed(result)
        if stop is not None:
            return stop
        if result.get('button') == 100:
            continue
        option = offered.get(result.get('button'))
        if option is None:
            return DeliveryResult(False, 'Unoffered native choice')
        return DeliveryResult(True, 'Windows native approval shown', details={'action': 'permission_option', 'decision_source': 'user', 'option_kind': option['kind'], 'option_handle': option['handle']})


def windows_notice(payload: dict) -> bool:
    clean = redact_secrets(payload)
    task = clean['task_id']
    body = '\n'.join(line for line in str(clean.get('message', ''))[:3000].splitlines() if not line.startswith('Report:'))
    body += f'\nReport: agentbc task report {task}'
    result = windows_request({'title': 'AgentBC · Docker · ' + task, 'body': body,
                              'buttons': [{'id': 100, 'text': 'OK'}], 'timeout_s': 30})
    if result['outcome'] == 'uncertain':
        raise RuntimeError('Desktop notice visibility became uncertain')
    return result['outcome'] in ('selected', 'closed', 'timeout') and result.get('shown') is True
