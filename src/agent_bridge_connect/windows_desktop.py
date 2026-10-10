"""Current-user interactive task and single-instance Windows Docker desktop bridge."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .host_platform import ensure_private_directory, try_lock, validate_private, windows_private
from .installation import private_json
from .windows_dialog import desktop_state


def powershell(script: str):
    source = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script
    p = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                        base64.b64encode(source.encode('utf-16le')).decode()], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if p.returncode:
        raise RuntimeError('Windows desktop registration operation failed')
    return json.loads(p.stdout.decode('utf-8-sig')) if p.stdout.strip() else None



def installed_tasks() -> list[str]:
    value = powershell(r"""
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$names=@(Get-ScheduledTask | Where-Object {$_.TaskName -like 'AgentBC-*'} | ForEach-Object {
 $owner=$_.Principal.UserId
 if($owner -and $owner -notlike 'S-1-*'){$owner=([Security.Principal.NTAccount]::new($owner)).Translate([Security.Principal.SecurityIdentifier]).Value}
 if($owner -eq $sid){$_.TaskName}
})
ConvertTo-Json -InputObject $names -Compress
""")
    return value or []


def identity(pid=None):
    kernel, advapi = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('advapi32', use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [W.HLOCAL], W.HLOCAL
    advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    advapi.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    kernel.GetProcessTimes.argtypes = [W.HANDLE, *([C.POINTER(W.FILETIME)] * 4)]
    pid = pid or os.getpid()
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise RuntimeError('Desktop process identity unavailable')
    token, text = W.HANDLE(), W.LPWSTR()
    try:
        if not advapi.OpenProcessToken(process, 8, C.byref(token)):
            raise RuntimeError('Desktop process owner unavailable')
        needed = W.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, C.byref(needed))
        data = C.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, data, needed, C.byref(needed)):
            raise RuntimeError('Desktop process owner unavailable')
        sid = C.cast(data, C.POINTER(C.c_void_p)).contents
        if not advapi.ConvertSidToStringSidW(sid, C.byref(text)):
            raise RuntimeError('Invalid desktop process owner')
        times = [W.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *(C.byref(t) for t in times)):
            raise RuntimeError('Desktop process creation time unavailable')
        if times[1].dwHighDateTime or times[1].dwLowDateTime:
            raise RuntimeError('Desktop process has exited')
        session = W.DWORD()
        if not kernel.ProcessIdToSessionId(pid, C.byref(session)):
            raise RuntimeError('Desktop process session unavailable')
        stats = C.create_string_buffer(128)
        if not advapi.GetTokenInformation(token, 10, stats, C.sizeof(stats), C.byref(needed)):
            raise RuntimeError('Desktop logon identity unavailable')
        logon = int.from_bytes(stats.raw[8:16], 'little')
        return {'pid': pid, 'sid': text.value, 'session_id': session.value, 'logon_id': str(logon),
                'created': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
    finally:
        if text:
            kernel.LocalFree(C.cast(text, W.HLOCAL))
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def job_guard():
    """Kernel closes all bridge descendants if the scheduled owner is terminated."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    class Basic(C.Structure):
        _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong), ('flags', W.DWORD),
                    ('min', C.c_size_t), ('max', C.c_size_t), ('active', W.DWORD),
                    ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]
    class Limits(C.Structure):
        _fields_ = [('basic', Basic), ('io', C.c_ulonglong * 6), ('process_memory', C.c_size_t),
                    ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [C.c_void_p, W.LPCWSTR], W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.GetCurrentProcess.restype = W.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Limits()
    limits.basic.flags = 0x2000
    if not handle or not kernel.SetInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise RuntimeError('Cannot contain desktop child processes')
    return handle  # Kept for process lifetime; kernel closes it on exit.


def desktop_docker(*args, context=None):
    command = ['docker', *(['--context', context] if context else []), *args]
    p = subprocess.run(command, capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if p.returncode:
        raise RuntimeError('Desktop Docker transport unavailable')
    return p.stdout.decode('utf-8')


def desktop_engine():
    context = desktop_docker('context', 'show').strip()
    info = json.loads(desktop_docker('info', '--format', '{{json .}}', context=context))
    if not info.get('ID'):
        raise ValueError('Missing Docker daemon identity')
    return {'context': context, 'daemon_id': info['ID']}


def task_operation(binding: dict, operation: str):
    encoded = base64.b64encode(json.dumps(binding).encode()).decode()
    script = "$b=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'))|ConvertFrom-Json;"
    script += r'''
$t=Get-ScheduledTask -TaskName $b.task_name -ErrorAction SilentlyContinue
if($t){
 $sid=$t.Principal.UserId
 if($sid -notlike 'S-1-*'){$sid=([Security.Principal.NTAccount]::new($sid)).Translate([Security.Principal.SecurityIdentifier]).Value}
 if($t.Description -ne $b.marker -or $sid -ne $b.sid -or $t.Principal.LogonType -ne 'Interactive' -or $t.Principal.RunLevel -ne 'Limited' -or @($t.Actions).Count -ne 1 -or $t.Actions[0].Execute -ne $b.python -or $t.Actions[0].Arguments -ne $b.arguments){throw 'Foreign desktop task'}
 $triggerSid=$t.Triggers[0].UserId
 if($triggerSid -and $triggerSid -notlike 'S-1-*'){$triggerSid=([Security.Principal.NTAccount]::new($triggerSid)).Translate([Security.Principal.SecurityIdentifier]).Value}
 if($t.Actions[0].WorkingDirectory -ne $b.root -or @($t.Triggers).Count -ne 1 -or $t.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -or $triggerSid -ne $b.sid -or [int]$t.Settings.MultipleInstances -ne 2 -or -not $t.Settings.AllowDemandStart -or -not $t.Settings.Enabled){throw 'Desktop task settings changed'}
}
'''
    if operation == 'register':
        script += r'''
if(-not $t){
 $a=New-ScheduledTaskAction -Execute $b.python -Argument $b.arguments -WorkingDirectory $b.root
 $p=New-ScheduledTaskPrincipal -UserId $b.sid -LogonType Interactive -RunLevel Limited
 $g=New-ScheduledTaskTrigger -AtLogOn -User $b.sid
 $s=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskName $b.task_name -Action $a -Principal $p -Trigger $g -Settings $s -Description $b.marker|Out-Null
}
'''
    elif operation == 'start':
        script += "if(-not $t){throw 'Desktop task missing'};Start-ScheduledTask -TaskName $b.task_name;"
    elif operation == 'remove':
        script += "if($t){Stop-ScheduledTask -TaskName $b.task_name;Unregister-ScheduledTask -TaskName $b.task_name -Confirm:$false;}"
    elif operation != 'check':
        raise ValueError('Unknown desktop task operation')
    script += "[pscustomobject]@{exists=([bool](Get-ScheduledTask -TaskName $b.task_name -ErrorAction SilentlyContinue))}|ConvertTo-Json -Compress"
    return powershell(script)


def read_binding(path: Path) -> dict:
    validate_private(path)
    binding = json.loads(path.read_text(encoding='utf-8'))
    if binding.get('root') != str(path.parent) or binding.get('sid') != identity()['sid']:
        raise ValueError('Desktop binding owner changed')
    return binding


def register(profile_path: Path, *, enable: bool, start: bool = True) -> dict:
    root = profile_path.parent
    ensure_private_directory(root)
    path = root / 'windows-desktop.json'
    if not enable:
        if path.exists():
            binding = read_binding(path)
            previous = status(root)
            task_operation(binding, 'remove')
            until = time.monotonic() + 15
            while previous.get('pid'):
                try:
                    current = identity(previous['pid'])
                except RuntimeError:
                    break
                if any(current.get(k) != previous.get(k) for k in ('sid', 'created', 'session_id')):
                    break
                if time.monotonic() >= until:
                    raise RuntimeError('Desktop bridge has not exited; retry maintenance')
                time.sleep(.1)
            path.unlink()
        return {'state': 'disabled'}
    validate_private(profile_path)
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    engine = desktop_engine()
    info = json.loads(desktop_docker('inspect', profile['container_id'], context=engine['context']))[0]
    if info['Id'] != profile['container_id']:
        raise ValueError('Desktop container binding changed')
    if profile.get('schema') == 2:
        from .docker_install_runtime import verify_binding
        verify_binding(profile)
    install_id = profile.get('installation_id') or 'preview-' + hashlib.sha256(profile['container_id'].encode()).hexdigest()[:20]
    if path.exists():
        binding = read_binding(path)
        if binding['installation_id'] != install_id:
            raise ValueError('Desktop installation changed')
        task_operation(binding, 'check')
        if binding['engine'] != engine:
            raise ValueError('Desktop Docker engine changed')
        if binding['container_id'] != profile['container_id']:
            if profile.get('schema') != 2:
                raise ValueError('Preview container changed; explicit reinstall required')
            task_operation(binding, 'remove')
            binding.update(container_id=info['Id'], image_id=info['Image'], instance_key=uuid.uuid4().hex)
            private_json(path, binding)
    else:
        python = (root / 'venv/Scripts/pythonw.exe' if profile.get('schema') == 2
                  else Path(sys.executable).with_name('pythonw.exe'))
        if not python.is_file():
            raise ValueError('Managed Windows pythonw is missing')
        binding = {'schema': 1, 'root': str(root), 'profile': str(profile_path), 'installation_id': install_id,
                   'sid': identity()['sid'], 'container_id': info['Id'], 'image_id': info['Image'], 'engine': engine,
                   'python': str(python), 'docker': shutil.which('docker'), 'instance_key': uuid.uuid4().hex,
                   'task_name': 'AgentBC-Desktop-' + install_id, 'marker': 'AgentBC desktop owner ' + install_id,
                   'arguments': subprocess.list2cmdline(['-m', 'agent_bridge_connect.windows_desktop', 'run', '--binding', str(path)])}
        if not binding['docker']:
            raise ValueError('Docker executable unavailable')
        private_json(path, binding)
    task_operation(binding, 'register')
    if start:
        task_operation(binding, 'start')
    return binding


def status(root: Path) -> dict:
    try:
        binding = read_binding(root / 'windows-desktop.json')
        file = root / 'windows-desktop-state.json'
        validate_private(file)
        state = json.loads(file.read_text(encoding='utf-8'))
        if any(state.get(k) != binding.get(k) for k in ('installation_id', 'container_id', 'instance_key', 'sid')):
            raise ValueError('Desktop readiness binding mismatch')
        heartbeat_limit = 45 if state.get('state') == 'waiting_for_docker' else 15
        if not 0 <= time.time() - state['heartbeat'] <= heartbeat_limit:
            return {'state': 'degraded', 'reason': 'stale_heartbeat'}
        observed = identity(state['pid'])
        if any(observed[k] != state.get(k) for k in ('pid', 'sid', 'session_id', 'created')):
            raise ValueError('Desktop readiness process changed')
        if not observed['session_id']:
            raise ValueError('Desktop owner is not interactive')
        return state
    except FileNotFoundError:
        return {'state': 'waiting_for_session', 'reason': 'desktop_owner_unavailable'}
    except (OSError, ValueError, KeyError, RuntimeError):
        return {'state': 'degraded', 'reason': 'desktop_identity_or_process_invalid'}


def ensure(profile_path: Path) -> None:
    state = status(profile_path.parent)
    if state['state'] == 'ready':
        return
    register(profile_path, enable=True)
    until = time.monotonic() + 40
    while time.monotonic() < until:
        state = status(profile_path.parent)
        if state['state'] == 'ready':
            return
        time.sleep(0.5)
    raise RuntimeError('Windows desktop bridge not ready: ' + state.get('reason', state['state']))


def run(path: Path) -> None:
    binding = read_binding(path)
    job = job_guard()
    root = path.parent
    lock_path = root / 'windows-desktop.lock'
    new = not lock_path.exists()
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if new:
            windows_private(lock_path, create_acl=True)
        validate_private(lock_path)
        try_lock(fd)
    except BlockingIOError:
        os.close(fd)
        return
    owner = identity()
    os.environ['PATH'] = str(Path(binding['docker']).parent) + os.pathsep + os.environ.get('PATH', '')
    os.environ.pop('AGENTBC_HEADLESS_NOTIFICATIONS', None)
    from .docker_approval_bridge import Bridge, DeliveryQueue, pending
    from .docker_terminal_bridge import TerminalRelay
    queue, futures = DeliveryQueue(), {}
    queue.restore(root / 'approval-bridge-state.json', binding['container_id'])
    terminal = TerminalRelay(binding['container_id'], root / 'terminal-bridge-state.json')
    bridge, delay, offline = None, 2, False
    login_file = root / 'desktop-login.json'
    previous_login = None
    if login_file.exists():
        validate_private(login_file)
        previous_login = json.loads(login_file.read_text()).get('logon_id')
    new_login = previous_login is None or previous_login != owner['logon_id']
    webui_error, recovery_at = '', 0.0
    private_json(login_file, {'logon_id': owner['logon_id']})
    def save(state, reason=''):
        private_json(root / 'windows-desktop-state.json', {**owner, **{k: binding[k] for k in ('installation_id', 'container_id', 'instance_key')},
                     'state': state, 'reason': reason, 'webui_error': webui_error, 'heartbeat': time.time(), 'updated_at': datetime.now(timezone.utc).isoformat()})
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            while True:
                for future in list(futures):
                    if not future.done():
                        continue
                    key = futures.pop(future)
                    try:
                        queue.finish(key, now=time.monotonic(), result=future.result())
                    except Exception as exc:
                        queue.finish(key, now=time.monotonic(), error=exc)
                    queue.save(root / 'approval-bridge-state.json', binding['container_id'])
                try:
                    engine = desktop_engine()
                except Exception:
                    offline = True
                    save('waiting_for_docker', 'Docker unavailable')
                    time.sleep(delay)
                    delay = min(30, delay * 2)
                    continue
                if engine != binding['engine']:
                    save('degraded', 'Docker context or daemon changed')
                    return
                try:
                    info = json.loads(desktop_docker('inspect', binding['container_id'], context=engine['context']))[0]
                    if info['Id'] != binding['container_id'] or info['Image'] != binding['image_id']:
                        raise ValueError('Desktop container identity changed')
                    if not binding['installation_id'].startswith('preview-'):
                        from .installation import installation_lock
                        with installation_lock(shared=True):
                            from .docker_install_runtime import verify_binding, compose, service_ids
                            from .installation import read_receipt
                            receipt = read_receipt()
                            if not receipt or receipt.get('installation_id') != binding['installation_id']:
                                raise ValueError('Desktop installation receipt changed')
                            if receipt['state'] != 'installed':
                                save('waiting_for_docker', 'Installation maintenance is active')
                                time.sleep(2)
                                continue
                            validate_private(Path(binding['profile']))
                            profile = json.loads(Path(binding['profile']).read_text())
                            verify_binding(profile)
                            if new_login or offline:
                                (root / 'runner.closed').unlink(missing_ok=True)
                                (root / 'webui.closed').unlink(missing_ok=True)
                            if not info['State']['Running'] and not (root / 'runner.closed').exists():
                                compose(profile, 'start', 'runner')
                                info['State']['Running'] = True
                            if profile['settings']['autostart'] and not (root / 'webui.closed').exists():
                                if not service_ids(profile)['webui']['State']['Running']:
                                    try:
                                        compose(profile, 'start', 'webui')
                                        webui_error = ''
                                    except ValueError:
                                        webui_error = 'WebUI restore failed; check the fixed port and Docker service'
                    elif new_login or offline or time.monotonic() >= recovery_at:
                        from .docker_preview_setup import restore_services
                        try:
                            webui_error = restore_services(Path(binding['profile']), reset_closed=new_login or offline)
                        except BlockingIOError:
                            save('waiting_for_docker', 'Installation maintenance is active')
                            time.sleep(2)
                            continue
                        recovery_at = time.monotonic() + 30
                        info = json.loads(desktop_docker('inspect', binding['container_id'], context=engine['context']))[0]
                    new_login, offline = False, False
                    if not info['State']['Running']:
                        save('waiting_for_docker', 'Bound Runner is stopped')
                        time.sleep(2)
                        continue
                    if bridge is None:
                        bridge = Bridge(binding['container_id'])
                    if not desktop_state()['visible']:
                        save('waiting_for_session', 'Desktop is locked or disconnected')
                        time.sleep(2)
                        continue
                    tasks = bridge.discover()
                    terminal.tick()
                    active = set()
                    for task in tasks:
                        snap = bridge.snapshot(task)
                        key = (task, json.dumps(snap['request'], sort_keys=True))
                        active.add(key)
                        if not futures and pending(snap) and queue.eligible(key, time.monotonic()):
                            queue.start(key)
                            queue.save(root / 'approval-bridge-state.json', binding['container_id'])
                            futures[pool.submit(bridge.deliver, task, snap)] = key
                    queue.prune(active)
                    queue.save(root / 'approval-bridge-state.json', binding['container_id'])
                    degraded = any(v['state'] in ('manual_action_required', 'retry_wait') for v in queue.requests.values())
                    degraded = degraded or any(v['state'] in ('manual_action_required', 'retry_wait') for v in list(terminal.entries.values()))
                    save('degraded' if degraded else 'ready', 'Delivery needs attention' if degraded else '')
                    delay = 2
                except BlockingIOError:
                    save('waiting_for_docker', 'Installation maintenance is active')
                    time.sleep(2)
                    continue
                except (RuntimeError, subprocess.TimeoutExpired):
                    save('waiting_for_docker', 'Bound service is not ready; retrying')
                    time.sleep(delay)
                    delay = min(30, delay * 2)
                    continue
                except Exception as exc:
                    if isinstance(exc.__cause__, BlockingIOError):
                        save('waiting_for_docker', 'Installation maintenance is active')
                        time.sleep(2)
                        continue
                    save('degraded', type(exc).__name__ + ': desktop binding or transport failed')
                    return
                time.sleep(2)
        finally:
            terminal.close()
            os.close(fd)
            _ = job


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'register', 'remove', 'status'))
    parser.add_argument('--binding', type=Path)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if args.action == 'run':
        try:
            run(args.binding)
        except Exception as exc:
            binding = read_binding(args.binding)
            private_json(args.binding.parent / 'windows-desktop-state.json', {**identity(), **{k: binding[k] for k in ('installation_id', 'container_id', 'instance_key')}, 'heartbeat': time.time(), 'state': 'degraded', 'reason': type(exc).__name__ + ': desktop startup failed'})
            raise SystemExit(2) from None
    elif args.action == 'status':
        print(json.dumps(status(args.profile.parent)))
    else:
        result = register(args.profile, enable=args.action == 'register')
        print(json.dumps({'state': 'registered' if args.action == 'register' else result['state']}))


if __name__ == '__main__':
    main()
