"""Docker-only Runner singleton: kernel lock, not a PID from an older namespace."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys


def claim_runtime(state: Path, spool: Path) -> int:
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / 'container.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Once the exclusive kernel lock is held, a PID from a previous
        # container namespace is stale even if that integer is alive here.
        (state / 'runner.pid').unlink(missing_ok=True)
        (spool / 'runner.pid').unlink(missing_ok=True)
        os.set_inheritable(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def main() -> None:
    if sys.platform != 'linux' or sys.argv[1:3] != ['runner', 'serve']:
        raise SystemExit('This entry is reserved for the managed Linux Docker Runner')
    claim_runtime(Path('/home/agentbc/.abc/runner'), Path('/run/agentbc'))
    # Older host updaters generate Compose without the new environment field.
    # Establish the store boundary in the new image's managed entry as well.
    os.environ['AGENTBC_SESSION_STORE'] = 'isolated_container'
    os.execv('/usr/local/bin/agentbc', ['agentbc', *sys.argv[1:]])


if __name__ == '__main__':
    main()
