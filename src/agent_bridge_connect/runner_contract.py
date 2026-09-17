"""Frozen Runner contracts shared by the state layer and the IPC layer.

ARCH-104-001 Slice B: this module owns the pieces both Runner layers must agree
on byte-for-byte -- the Runner error type, the IPC size and channel constants,
and the default Runner path contracts.  ``agent_bridge_connect.runner`` (state,
process and task-dispatch ownership) and ``agent_bridge_connect.runner_ipc``
(client, service and the single request router) both import this module, so it
must not import either of them.

``<spool>/runner.pid`` is part of the Runner IPC endpoint contract: the service
publishes its pid there and every process-management caller verifies liveness
against that same file, so the reader and the liveness probe live here to keep
exactly one definition of that contract.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


MAX_REQUEST_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
RUNNER_IDENTITY_REFRESH_INTERVAL_S = 30.0
RUNNER_IPC_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RunnerError(RuntimeError):
    pass


def default_runner_root() -> Path:
    return Path.home() / ".abc" / "runner"


def default_runner_spool() -> Path:
    override = os.environ.get("AGENTBC_RUNNER_SPOOL")
    if override:
        return Path(override).expanduser()
    return Path("/tmp") / f"agentbc-runner-v2-{os.getuid()}"


def default_runner_token() -> Path:
    return default_runner_spool() / "token"


def default_runner_log() -> Path:
    return default_runner_root() / "runner.log"


def _read_runner_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
