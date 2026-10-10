"""Isolated Win32 task dialog process. No business writes or permission decisions."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

MAX_BYTES = 65536


def desktop_state() -> dict:
    if sys.platform != 'win32':
        return {'visible': False, 'session_id': None, 'reason': 'not_windows'}
    kernel = C.WinDLL('kernel32', use_last_error=True)
    user = C.WinDLL('user32', use_last_error=True)
    session = W.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), C.byref(session)) or not session.value:
        return {'visible': False, 'session_id': session.value, 'reason': 'noninteractive_session'}
    wts = C.WinDLL('wtsapi32', use_last_error=True)
    wts.WTSQuerySessionInformationW.argtypes = [W.HANDLE, W.DWORD, C.c_int, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
    wts.WTSFreeMemory.argtypes = [C.c_void_p]
    class Session(C.Structure):
        _fields_ = [('id', W.DWORD), ('station', W.LPWSTR), ('state', C.c_int)]
    sessions, count = C.POINTER(Session)(), W.DWORD()
    wts.WTSEnumerateSessionsW.argtypes = [W.HANDLE, W.DWORD, W.DWORD, C.POINTER(C.POINTER(Session)), C.POINTER(W.DWORD)]
    if not wts.WTSEnumerateSessionsW(None, 0, 1, C.byref(sessions), C.byref(count)):
        return {'visible': False, 'session_id': session.value, 'reason': 'session_query_failed'}
    try:
        active_sessions = [sessions[i].id for i in range(count.value) if sessions[i].id and sessions[i].state == 0]
    finally:
        wts.WTSFreeMemory(C.cast(sessions, C.c_void_p))
    if active_sessions != [session.value]:
        return {'visible': False, 'session_id': session.value, 'reason': 'ambiguous_or_inactive_session'}
    data, size = C.c_void_p(), W.DWORD()
    if not wts.WTSQuerySessionInformationW(None, session.value, 8, C.byref(data), C.byref(size)):
        return {'visible': False, 'session_id': session.value, 'reason': 'session_query_failed'}
    try:
        active = C.cast(data, C.POINTER(C.c_int)).contents.value == 0
    finally:
        wts.WTSFreeMemory(data)
    user.OpenInputDesktop.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    user.OpenInputDesktop.restype = W.HANDLE
    user.CloseDesktop.argtypes = [W.HANDLE]
    user.GetUserObjectInformationW.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    handle = user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
    visible = False
    if handle:
        try:
            name, needed = C.create_unicode_buffer(256), W.DWORD()
            visible = bool(user.GetUserObjectInformationW(handle, 2, name, C.sizeof(name), C.byref(needed))) and name.value.casefold() == 'default'
        finally:
            user.CloseDesktop(handle)
    return {'visible': active and visible, 'session_id': session.value,
            'reason': 'ready' if active and visible else 'waiting_for_session'}


def validate_request(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('Invalid dialog request')
    for key in ('title', 'body', 'details'):
        field = value.get(key, '')
        if not isinstance(field, str) or '\0' in field or len(field) > 16000:
            raise ValueError('Invalid dialog text')
    timeout = value.get('timeout_s', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Invalid dialog timeout')
    buttons = value.get('buttons', [{'id': 100, 'text': 'OK'}])
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= 8:
        raise ValueError('Invalid dialog buttons')
    ids = []
    for button in buttons:
        if not isinstance(button, dict) or type(button.get('id')) is not int or not 100 <= button['id'] <= 1000:
            raise ValueError('Invalid button identifier')
        if not isinstance(button.get('text'), str) or not button['text'] or len(button['text']) > 100 or '\0' in button['text']:
            raise ValueError('Invalid button text')
        ids.append(button['id'])
    if len(set(ids)) != len(ids) or value.get('default', ids[0]) not in ids:
        raise ValueError('Invalid default button')
    return dict(value, buttons=buttons, timeout_s=float(timeout), default=value.get('default', ids[0]))


def show(value: dict) -> dict:
    value = validate_request(value)
    state = desktop_state()
    if not state['visible']:
        return dict(state, outcome='delivery_failed', shown=False)
    # These structures use the packing specified by the Windows common-controls ABI.
    class Button(C.Structure):
        _pack_ = 1
        _fields_ = [('id', C.c_int), ('text', W.LPCWSTR)]
    callback_type = C.WINFUNCTYPE(C.c_long, W.HWND, W.UINT, W.WPARAM, W.LPARAM, C.c_ssize_t)
    class Config(C.Structure):
        _pack_ = 1
        _fields_ = [('size', W.UINT), ('parent', W.HWND), ('instance', W.HINSTANCE),
                    ('flags', C.c_int), ('common', C.c_int), ('title', W.LPCWSTR),
                    ('icon', C.c_void_p), ('instruction', W.LPCWSTR), ('body', W.LPCWSTR),
                    ('count', W.UINT), ('buttons', C.POINTER(Button)), ('default', C.c_int),
                    ('radio_count', W.UINT), ('radios', C.POINTER(Button)), ('radio_default', C.c_int),
                    ('verification', W.LPCWSTR), ('details', W.LPCWSTR), ('expanded', W.LPCWSTR),
                    ('collapsed', W.LPCWSTR), ('footer_icon', C.c_void_p), ('footer', W.LPCWSTR),
                    ('callback', callback_type), ('callback_data', C.c_ssize_t), ('width', W.UINT)]
    class ActCtx(C.Structure):
        _fields_ = [('size', W.ULONG), ('flags', W.DWORD), ('source', W.LPCWSTR), ('architecture', W.USHORT),
                    ('language', W.LANGID), ('directory', W.LPCWSTR), ('resource', W.LPCWSTR),
                    ('application', W.LPCWSTR), ('module', W.HMODULE)]
    kernel, user = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('user32', use_last_error=True)
    kernel.CreateActCtxW.argtypes, kernel.CreateActCtxW.restype = [C.POINTER(ActCtx)], W.HANDLE
    kernel.ActivateActCtx.argtypes = [W.HANDLE, C.POINTER(C.c_size_t)]
    kernel.DeactivateActCtx.argtypes = [W.DWORD, C.c_size_t]
    kernel.ReleaseActCtx.argtypes = [W.HANDLE]
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.SendMessageW.restype = W.LPARAM
    user.IsWindowVisible.argtypes = [W.HWND]
    user.IsWindowVisible.restype = W.BOOL
    deadline, outcome = time.monotonic() + value['timeout_s'], {'shown': False, 'reason': ''}
    @callback_type
    def callback(hwnd, event, wp, lp, context):
        try:
            if event in (0, 2, 4):
                visible = desktop_state()['visible']
                if not visible:
                    outcome['reason'] = 'session_lost'
                    user.SendMessageW(hwnd, 1126, 2, 0)
                elif user.IsWindowVisible(hwnd):
                    outcome['shown'] = True
                if not outcome['reason'] and time.monotonic() >= deadline:
                    outcome['reason'] = 'timeout'
                    user.SendMessageW(hwnd, 1126, 2, 0)
        except Exception:
            outcome['reason'] = 'callback_failed'
            user.SendMessageW(hwnd, 1126, 2, 0)
        return 0
    manifest = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
                '<assemblyIdentity version="1.0.0.0" name="AgentBC.Dialog" type="win32"/>'
                '<dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" '
                'version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>'
                '</dependentAssembly></dependency></assembly>')
    with tempfile.TemporaryDirectory(prefix='agentbc-dialog-') as temp:
        path = Path(temp) / 'dialog.manifest'
        path.write_text(manifest, encoding='utf-8')
        act = ActCtx()
        act.size = C.sizeof(act)
        act.source = str(path)
        handle = kernel.CreateActCtxW(C.byref(act))
        if handle == C.c_void_p(-1).value:
            raise OSError('Cannot activate Windows common controls')
        cookie = C.c_size_t()
        try:
            if not kernel.ActivateActCtx(handle, C.byref(cookie)):
                raise OSError('Cannot activate Windows dialog context')
            dll = C.WinDLL('comctl32', use_last_error=True)
            call = dll.TaskDialogIndirect
            call.argtypes = [C.POINTER(Config), C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(W.BOOL)]
            call.restype = C.c_long
            buttons = (Button * len(value['buttons']))(*(Button(b['id'], b['text']) for b in value['buttons']))
            config = Config()
            config.size = C.sizeof(config)
            config.flags = 8 | 0x800 | 0x8000  # cancellation, timer, minimizable
            config.title = value.get('title', 'AgentBC')
            config.body = value.get('body', '')
            config.details = value.get('details') or None
            config.expanded, config.collapsed = 'View Details', 'Hide Details'
            config.buttons, config.count, config.default = buttons, len(buttons), value['default']
            config.callback = callback
            selected = C.c_int()
            hr = call(C.byref(config), C.byref(selected), None, None)
            if hr != 0:
                raise OSError(f'TaskDialogIndirect failed: {hr}')
        finally:
            if cookie.value:
                kernel.DeactivateActCtx(0, cookie)
            kernel.ReleaseActCtx(handle)
    reason = outcome['reason']
    if not outcome['shown'] or reason in ('session_lost', 'callback_failed'):
        result = 'uncertain' if outcome['shown'] else 'delivery_failed'
    elif reason == 'timeout':
        result = 'timeout'
    elif selected.value == 2:
        result = 'closed'
    else:
        result = 'selected'
    return dict(state, outcome=result, shown=outcome['shown'], button=selected.value, reason=reason)


def main() -> int:
    try:
        line = sys.stdin.buffer.readline(MAX_BYTES + 1)
        if len(line) > MAX_BYTES or not line.endswith(b'\n'):
            raise ValueError('Invalid bounded UI request')
        result = show(json.loads(line))
    except Exception as exc:
        result = {'outcome': 'delivery_failed', 'shown': False, 'reason': type(exc).__name__}
    print(json.dumps(result), flush=True)
    return 0 if result['outcome'] in ('selected', 'closed', 'timeout') else 2


if __name__ == '__main__':
    raise SystemExit(main())
