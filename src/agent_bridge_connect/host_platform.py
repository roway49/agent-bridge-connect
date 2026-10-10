"""Small host-platform primitives; Linux container business code stays separate."""
from __future__ import annotations

import os
from pathlib import Path
import platform


def bundle_platform() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == 'Darwin' and machine == 'arm64':
        return 'macos-arm64'
    if system == 'Windows' and machine in ('amd64', 'x86_64'):
        return 'windows-amd64'
    raise ValueError(f'Unsupported installation host: {system}/{machine}')


def image_architecture(target: str) -> str:
    try:
        return {'macos-arm64': 'arm64', 'windows-amd64': 'amd64'}[target]
    except KeyError as exc:
        raise ValueError('Unsupported bundle platform') from exc


def venv_python(root: Path) -> Path:
    return root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def try_lock(fd: int, *, shared: bool = False) -> None:
    """Nonblocking OS lock until close; shared users exclude lifecycle maintenance."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                        ('hEvent', wintypes.HANDLE)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.LockFileEx.restype = wintypes.BOOL
        # Lock the same first byte as historical msvcrt locks, including empty files.
        flags = 1 | (0 if shared else 2)  # FAIL_IMMEDIATELY, optional EXCLUSIVE_LOCK
        if not kernel.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(Overlapped())):
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError('Host lock already held')
            raise ctypes.WinError(error)
    else:
        import fcntl
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)


def _reject_reparse(path: Path) -> None:
    import stat
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if item.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('Private host path must not traverse a reparse point')


def windows_private(path: Path, *, create_acl: bool = False) -> None:
    """Protect or verify a private Windows object by current-user SID and DACL."""
    import base64
    import subprocess
    _reject_reparse(path)
    encoded_path = base64.b64encode(str(path.absolute()).encode('utf-8')).decode()
    script = r"""
$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH64'))
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$a=Get-Acl -LiteralPath $p
if (CREATEACL) {
 $a.SetAccessRuleProtection($true,$false)
 foreach($r in @($a.Access)){$a.RemoveAccessRuleSpecific($r)}
 $a.SetOwner($sid)
 $inherit=if(Test-Path -LiteralPath $p -PathType Container){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
 $r=[Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl',$inherit,'None','Allow')
 $a.AddAccessRule($r)
 Set-Acl -LiteralPath $p -AclObject $a
 $a=Get-Acl -LiteralPath $p
}
if($a.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'Private path owner mismatch'}
if(-not $a.AreAccessRulesProtected){throw 'Private path inherits permissions'}
$own=$false
foreach($r in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
 if($r.AccessControlType -eq 'Allow') {
  if($r.IdentityReference.Value -ne $sid.Value){throw 'Private path accessible to another SID'}
  if(($r.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl){$own=$true}
 }
}
if(-not $own){throw 'Private path owner lacks access'}
""".replace('PATH64', encoded_path).replace('CREATEACL', '$true' if create_acl else '$false')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16le')).decode()],
                            capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Windows private path SID/DACL validation failed')


def ensure_private_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        windows_private(path, create_acl=not existed)
    else:
        validate_private(path, directory=True)


def validate_private(path: Path, *, directory: bool = False) -> None:
    import stat
    info = path.lstat()
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or path.is_symlink():
        raise ValueError('Private path has invalid type')
    if os.name == 'nt':
        windows_private(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private path must be owned by this user with owner-only access')


def docker_host_path(value: str) -> Path:
    """Decode only Docker Desktop's documented drive-path spellings on Windows."""
    if os.name != 'nt':
        return Path(value)
    from pathlib import PureWindowsPath
    normalized = value.replace('\\', '/')
    for prefix in ('/run/desktop/mnt/host/', '/host_mnt/'):
        if normalized.startswith(prefix):
            tail = normalized[len(prefix):]
            drive, separator, rest = tail.partition('/')
            if len(drive) != 1 or not drive.isalpha() or not separator:
                raise ValueError('Unsupported Docker Desktop mount source')
            normalized = drive + ':/' + rest
            break
    parsed = PureWindowsPath(normalized)
    if not parsed.is_absolute() or not parsed.drive.endswith(':'):
        raise ValueError('Windows project must use an absolute local drive path')
    return Path(str(parsed))


def cli_entry_name() -> str:
    return 'agentbc.cmd' if os.name == 'nt' else 'agentbc'


def add_user_path(directory: Path) -> None:
    if os.name != 'nt':
        return
    import winreg
    value = str(directory.resolve())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        try:
            old, kind = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            old, kind = '', winreg.REG_EXPAND_SZ
        if value.casefold() not in {part.rstrip('\\').casefold() for part in old.split(';') if part}:
            winreg.SetValueEx(key, 'Path', 0, kind, old.rstrip(';') + (';' if old else '') + value)
    if value.casefold() not in {part.casefold() for part in os.environ.get('PATH', '').split(';')}:
        os.environ['PATH'] = value + ';' + os.environ.get('PATH', '')


def remove_owned_entry(root: Path) -> None:
    entry = Path.home() / '.local/bin' / cli_entry_name()
    if not entry.exists() and not entry.is_symlink():
        return
    if os.name == 'nt':
        managed = root / 'bin' / cli_entry_name()
        _reject_reparse(entry)
        if not managed.is_file() or entry.read_bytes() != managed.read_bytes() or b'AgentBC Docker host entry' not in entry.read_bytes():
            raise ValueError('CLI ownership changed; maintenance remains incomplete')
    elif not entry.is_symlink() or not entry.resolve().is_relative_to(root):
        raise ValueError('CLI ownership changed; maintenance remains incomplete')
    entry.unlink()
