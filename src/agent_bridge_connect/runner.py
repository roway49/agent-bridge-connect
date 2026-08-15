from __future__ import annotations

import hmac
import json
import os
import plistlib
import re
import shlex
import shutil
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .effective_permissions import (
    is_temporary_permission,
    resolve_effective_permission,
    validate_temporary_permission_context,
)
from .control import ApprovalControlPlane, ControlPlaneError, normalize_decision
from .execution_policy import (
    RESOURCE_EXTENSION_KEY,
    SESSION_EXTENSION_KEY,
    attach_execution_policy,
    build_resource_snapshot,
    build_session_snapshot,
    validate_resource_snapshot,
    validate_session_snapshot,
)
from .path_provider import find_binary
from .permission_modes import (
    PERMISSION_EXTENSION_KEY,
    assert_executor_permission_supported,
    permission_record_from_extensions,
    validate_permission_command,
)
from .permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    consume_permission_grant,
    permission_grant_from_extensions,
)
from .protocol import ABCError
from .session import SessionRecoveryRequired, control_root_for_task


MAX_REQUEST_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_MANAGED_FILE_BYTES = 10 * 1024
TERMINAL_STATES = {"completed", "failed", "cancelled"}
MANAGED_RECORD_NAME_RE = re.compile(r"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4,}-\d{3}-report\.md\Z")
LEGACY_RUNNER_LAUNCH_AGENT_LABEL = "com.agentbc.runner"

# --- Phase 2 (1.0.2A) legacy migration and Runner policy validation ---
# Integration merge point: Task 1 (1.0.2A resource configuration foundations,
# commit 4e60e4f) owns agent_bridge_connect.execution_policy; this section only
# consumes its public snapshot builders/validators and extension keys. The
# Runner never reads the user's current configuration for legacy backfill and
# never injects executor resource/session flags. Phase 3 validates the argv
# emitted by adapters against these frozen Phase 2 snapshots.
PHASE2_EXECUTOR_PROJECT_ROOT_KEY = "executor_project_root"
PHASE2_EXECUTION_EXTENSION_KEY = "agentbc.execution"
PHASE2_LEGACY_UNRECORDED_KEY = "legacy_unrecorded"
PHASE2_LEGACY_BACKFILLED_KEY = "legacy_snapshot_backfilled"
PHASE2_LEGACY_BACKFILLED_AT_KEY = "legacy_backfilled_at"
PHASE2_LEGACY_DEFAULT_LIMITS: dict[str, tuple[int | float, str]] = {
    "claude": (10.0, "legacy_default_10"),
    "hermes": (90, "legacy_default_90"),
}
PHASE2_LEGACY_RETENTION = False
PHASE2_AUDIT_EVENT_TYPE = "execution_policy_audit"
PHASE2_POLICY_EXTENSION_KEYS = (RESOURCE_EXTENSION_KEY, SESSION_EXTENSION_KEY)
PHASE6_INPUT_EXTENSION_KEY = "agentbc.input"
PHASE6_LINEAGE_EXTENSION_KEY = "agentbc.lineage"
PHASE6_AUTHORIZATION_EXTENSION_KEYS = (
    PERMISSION_GRANT_EXTENSION_KEY,
    PHASE6_INPUT_EXTENSION_KEY,
    PHASE6_LINEAGE_EXTENSION_KEY,
)
_EXECUTOR_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")

_EXECUTOR_COMMAND_RULES: dict[str, dict[str, Any]] = {
    "hermes": {
        "required_subcommand": "chat",
        "required_flags": {"-q"},
        "description": "Hermes chat -q (non-interactive headless mode)",
    },
    "codex": {
        "required_subcommand": "exec",
        "required_flags": {"--json"},
        "alternate_subcommand": "app-server",
        "alternate_flags": {"--stdio", "--listen"},
        "description": "Codex exec --json or app-server --stdio (structured control transport)",
    },
    "claude": {
        "required_subcommand": None,
        "required_flags": set(),
        "required_any_flags": {"-p", "--print"},
        "description": "Claude -p (headless print mode)",
    },
}


def _canonical_flag_values(command: list[str], flag: str) -> tuple[list[str], bool]:
    """Return separated flag values and whether a non-canonical equals form exists."""
    values: list[str] = []
    noncanonical = False
    for index, token in enumerate(command):
        if token == flag:
            values.append(command[index + 1] if index + 1 < len(command) else "")
        elif token.startswith(f"{flag}="):
            noncanonical = True
    return values, noncanonical


class RunnerError(RuntimeError):
    pass


def _phase2_legacy_ephemeral_project_path(workspace: dict[str, Any], task_id: str) -> str:
    """Return the canonical iteration-scoped ephemeral Claude Project path.

    The session UUID is stored separately in ``agentbc.session.session_id``.  It
    must never become another project-directory segment.
    """
    from .path_model import canonical_executor_project_root

    agentbc_root = str((workspace or {}).get("agentbc_root") or "").strip()
    task_code = str((workspace or {}).get("task_code") or "").strip()
    task_date = str((workspace or {}).get("task_date") or "").strip()
    if not agentbc_root or not task_code or not task_date or not str(task_id).strip():
        raise RunnerError(
            "invalid_execution_policy: legacy Claude task is missing canonical path fields"
        )
    return str(
        canonical_executor_project_root(agentbc_root, task_date, task_code, task_id)
    )


def _phase2_required_policy_keys(executor: str) -> tuple[str, ...]:
    normalized = str(executor or "").strip().lower()
    if normalized in PHASE2_LEGACY_DEFAULT_LIMITS:
        return PHASE2_POLICY_EXTENSION_KEYS
    if normalized == "codex":
        return (SESSION_EXTENSION_KEY,)
    return ()


def _phase2_legacy_default_snapshots(
    executor: str,
    task_id: str,
    workspace: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build the canonical fixed legacy snapshots without reading user configuration."""
    normalized = str(executor or "").strip().lower()
    if normalized not in {"claude", "hermes", "codex"}:
        raise RunnerError(
            f"invalid_execution_policy: no legacy defaults for executor {executor}"
        )
    resources = None
    default = PHASE2_LEGACY_DEFAULT_LIMITS.get(normalized)
    if default is not None:
        limit, source = default
        resources = build_resource_snapshot(executor, limit, source=source)
    if normalized == "claude":
        session = build_session_snapshot(
            executor,
            retain=PHASE2_LEGACY_RETENTION,
            session_id=str(uuid.uuid4()),
            session_state="pending",
            project_path=_phase2_legacy_ephemeral_project_path(workspace, task_id),
        )
    else:
        session = build_session_snapshot(
            executor,
            retain=PHASE2_LEGACY_RETENTION,
            session_state="pending",
        )
    return resources, session


def _phase2_packet_policy_mismatch(
    packet: dict[str, Any],
    persisted: dict[str, Any],
) -> str | None:
    """Compare a worker task packet against the authoritative disk snapshot.

    Returns a compact fail-closed reason when the packet is not fully consistent
    with the disk record, or None when it is:
    - ``missing:<key>``   packet omits a policy snapshot recorded on disk;
    - ``injected:<key>``  packet carries a policy snapshot absent from disk;
    - ``modified:<key>``  packet and disk disagree on a policy snapshot;
    - ``expired:<key>``   packet reflects an older task state than the disk.
    """
    packet_ext = packet.get("extensions")
    disk_ext = persisted.get("extensions")
    if not isinstance(packet_ext, dict):
        packet_ext = {}
    if not isinstance(disk_ext, dict):
        disk_ext = {}
    for key in PHASE2_POLICY_EXTENSION_KEYS:
        in_packet = key in packet_ext
        in_disk = key in disk_ext
        if in_packet and not in_disk:
            return f"injected:{key}"
        if not in_packet and in_disk:
            return f"missing:{key}"
        if in_packet and in_disk and packet_ext[key] != disk_ext[key]:
            return f"modified:{key}"
    packet_execution = packet_ext.get(PHASE2_EXECUTION_EXTENSION_KEY)
    disk_execution = disk_ext.get(PHASE2_EXECUTION_EXTENSION_KEY)
    if (
        isinstance(packet_execution, dict)
        and isinstance(disk_execution, dict)
        and packet_execution != disk_execution
    ):
        return f"expired:{PHASE2_EXECUTION_EXTENSION_KEY}"
    for key in ("status", "updated_at"):
        if key in packet and key in persisted and packet[key] != persisted[key]:
            return f"expired:{key}"
    return None


def _phase6_packet_authorization_mismatch(
    packet: dict[str, Any],
    persisted: dict[str, Any],
) -> str | None:
    """Compare grant/input/lineage state without interpreting grant fields."""
    packet_ext = packet.get("extensions")
    disk_ext = persisted.get("extensions")
    packet_ext = packet_ext if isinstance(packet_ext, dict) else {}
    disk_ext = disk_ext if isinstance(disk_ext, dict) else {}
    for key in PHASE6_AUTHORIZATION_EXTENSION_KEYS:
        in_packet = key in packet_ext
        in_disk = key in disk_ext
        if in_packet and not in_disk:
            return f"injected:{key}"
        if not in_packet and in_disk:
            return f"missing:{key}"
        if in_packet and packet_ext[key] != disk_ext[key]:
            return f"modified:{key}"
    if packet_ext.get(PERMISSION_EXTENSION_KEY) != disk_ext.get(
        PERMISSION_EXTENSION_KEY
    ):
        return f"modified:{PERMISSION_EXTENSION_KEY}"
    for key in ("id", "task_id", "assignee", "status", "updated_at"):
        packet_value = packet.get(key)
        disk_value = persisted.get("id") if key == "task_id" else persisted.get(key)
        if packet_value is not None and disk_value is not None and packet_value != disk_value:
            return f"expired:{key}"
    return None


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


def start_runner_background(
    *,
    config_path: str | Path | None = None,
    spool_root: str | Path | None = None,
    token_path: str | Path | None = None,
    state_root: str | Path | None = None,
    extra_roots: list[str | Path] | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Start one detached Runner and wait until its health endpoint is ready."""
    from .config import resolve_config_path

    legacy = cleanup_legacy_runner_launch_agent()
    if not legacy.get("ok"):
        return legacy

    spool = Path(spool_root or default_runner_spool()).expanduser()
    token = Path(token_path or (spool / "token")).expanduser()
    state = Path(state_root or default_runner_root()).expanduser()
    log_path = state / "runner.log"
    client = RunnerClient(spool_root=spool, token_path=token, timeout_s=0.5)
    try:
        health = client.health()
    except RunnerError as exc:
        if _is_runner_identity_conflict(exc):
            return {
                "ok": False,
                "status": "runner_identity_conflict",
                "log": str(log_path),
                "error": str(exc),
                "action": "Run `agentbc runner stop` before starting a replacement Runner.",
            }
        health = None
    if health and health.get("ok") and health.get("status") == "ready":
        return {
            "ok": True,
            "status": "already_running",
            "pid": health.get("pid"),
            "log": str(log_path),
            "health": health,
        }

    state.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "agent_bridge_connect.cli",
        "runner",
        "serve",
        "--spool",
        str(spool),
        "--token",
        str(token),
        "--state-root",
        str(state),
        "--config",
        str(resolve_config_path(config_path)),
    ]
    for root in extra_roots or []:
        command.extend(("--allow-root", str(Path(root).expanduser())))
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        )

    deadline = time.monotonic() + max(timeout_s, 0.1)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {
                "ok": False,
                "status": "start_failed",
                "pid": process.pid,
                "returncode": process.returncode,
                "log": str(log_path),
                "error": _tail_text(log_path),
            }
        try:
            health = client.health()
        except RunnerError as exc:
            if _is_runner_identity_conflict(exc):
                _terminate_spawned_process(process)
                return {
                    "ok": False,
                    "status": "runner_identity_conflict",
                    "pid": process.pid,
                    "log": str(log_path),
                    "error": str(exc),
                    "action": "Run `agentbc runner stop` before starting a replacement Runner.",
                }
            time.sleep(0.1)
            continue
        if health.get("ok") and health.get("status") == "ready":
            return {
                "ok": True,
                "status": "started",
                "pid": health.get("pid", process.pid),
                "log": str(log_path),
                "health": health,
            }
        time.sleep(0.1)

    _terminate_spawned_process(process)
    return {
        "ok": False,
        "status": "start_timeout",
        "pid": process.pid,
        "log": str(log_path),
        "error": _tail_text(log_path),
    }


def stop_runner_background(
    *,
    spool_root: str | Path | None = None,
    state_root: str | Path | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Stop every verified AgentBC Runner process serving one spool."""
    legacy = cleanup_legacy_runner_launch_agent()
    if not legacy.get("ok"):
        return legacy

    spool = Path(spool_root or default_runner_spool()).expanduser()
    state = Path(state_root or default_runner_root()).expanduser()
    stable_pid_path = state / "runner.pid"
    spool_pid_path = spool / "runner.pid"
    include_stable_pid = state_root is not None or spool.resolve() == default_runner_spool().resolve()
    pid_paths = tuple(
        dict.fromkeys(
            (stable_pid_path, spool_pid_path) if include_stable_pid else (spool_pid_path,)
        )
    )
    recorded_pids = {
        pid
        for pid in (_read_runner_pid(path) for path in pid_paths)
        if pid is not None and _pid_is_alive(pid)
    }
    verified_pids = set(_discover_runner_pids(spool))
    health: dict[str, Any] | None = None
    health_error = ""
    try:
        health = RunnerClient(spool_root=spool, timeout_s=1.0).health()
    except RunnerError as exc:
        health_error = str(exc)
    if health and health.get("ok") and health.get("status") == "ready":
        health_pid = health.get("pid")
        if (
            isinstance(health_pid, int)
            and _pid_is_alive(health_pid)
            and (not recorded_pids or health_pid in recorded_pids or health_pid in verified_pids)
        ):
            verified_pids.add(health_pid)

    for pid in recorded_pids:
        if _pid_matches_runner_spool(pid, spool) or _pid_matches_runner_state(pid, state):
            verified_pids.add(pid)

    if not verified_pids:
        for path in pid_paths:
            pid = _read_runner_pid(path)
            if pid is None or not _pid_is_alive(pid):
                path.unlink(missing_ok=True)
        if recorded_pids:
            return {
                "ok": False,
                "status": "pid_not_agentbc_runner",
                "pids": sorted(recorded_pids),
                "health_pid": health.get("pid") if health else None,
                "error": health_error,
            }
        if _is_runner_identity_conflict(health_error):
            return {
                "ok": False,
                "status": "runner_identity_conflict_unresolved",
                "error": health_error,
            }
        return {"ok": True, "status": "not_running", "pids": []}

    stopped, forced, remaining = _terminate_runner_pids(verified_pids, timeout_s=timeout_s)
    if remaining:
        return {
            "ok": False,
            "status": "stop_timeout",
            "pids": sorted(verified_pids),
            "stopped_pids": stopped,
            "remaining_pids": remaining,
        }
    for path in dict.fromkeys((*pid_paths, stable_pid_path)):
        pid = _read_runner_pid(path)
        if pid is None or pid in stopped or not _pid_is_alive(pid):
            path.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "stopped_forced" if forced else ("stopped_orphans" if len(stopped) > 1 else "stopped"),
        "pids": stopped,
        "forced_pids": forced,
        "legacy_launch_agent": legacy,
    }


def cleanup_legacy_runner_launch_agent(
    launch_agent_path: str | Path | None = None,
) -> dict[str, Any]:
    """Remove the verified pre-Alpha launchd Runner so it cannot respawn."""
    if sys.platform != "darwin":
        return {"ok": True, "status": "not_applicable"}
    path = Path(
        launch_agent_path
        or Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_RUNNER_LAUNCH_AGENT_LABEL}.plist"
    ).expanduser()
    if not path.exists():
        return {"ok": True, "status": "not_present", "path": str(path)}
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        return {
            "ok": False,
            "status": "legacy_launch_agent_unverified",
            "path": str(path),
            "error": str(exc),
        }
    arguments = payload.get("ProgramArguments")
    owned = (
        payload.get("Label") == LEGACY_RUNNER_LAUNCH_AGENT_LABEL
        and isinstance(arguments, list)
        and any(Path(str(item)).name == "agentbc" for item in arguments)
        and "runner" in arguments
        and "serve" in arguments
    )
    if not owned:
        return {
            "ok": False,
            "status": "legacy_launch_agent_unverified",
            "path": str(path),
            "error": "refusing to remove a launch agent not owned by AgentBC",
        }
    launchctl = shutil.which("launchctl")
    if launchctl:
        subprocess.run(
            [launchctl, "remove", LEGACY_RUNNER_LAUNCH_AGENT_LABEL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    path.unlink()
    for log_path in (
        Path("/tmp/agentbc-runner.launchd.out.log"),
        Path("/tmp/agentbc-runner.launchd.err.log"),
    ):
        log_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "removed",
        "path": str(path),
        "label": LEGACY_RUNNER_LAUNCH_AGENT_LABEL,
    }


def _terminate_spawned_process(process: subprocess.Popen[Any]) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _is_runner_identity_conflict(error: BaseException | str) -> bool:
    message = str(error).strip().lower()
    return "runner authentication failed" in message or "runner identity conflict" in message


def _discover_runner_pids(spool_root: Path) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid != os.getpid() and _runner_command_matches_spool(parts[1], spool_root):
            pids.append(pid)
    return sorted(set(pids))


def _pid_matches_runner_spool(pid: int, spool_root: Path) -> bool:
    command = _runner_process_command(pid)
    return bool(command) and _runner_command_matches_spool(command, spool_root)


def _pid_matches_runner_state(pid: int, state_root: Path) -> bool:
    command = _runner_process_command(pid)
    return bool(command) and _runner_command_matches_state(command, state_root)


def _runner_process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _runner_command_matches_spool(command: str, spool_root: Path) -> bool:
    arguments = _runner_command_arguments(command)
    if arguments is None:
        return False
    requested_spool = _command_option(arguments, "--spool")
    command_spool = Path(requested_spool).expanduser() if requested_spool else default_runner_spool()
    return command_spool.resolve() == spool_root.expanduser().resolve()


def _runner_command_matches_state(command: str, state_root: Path) -> bool:
    arguments = _runner_command_arguments(command)
    if arguments is None:
        return False
    requested_state = _command_option(arguments, "--state-root")
    command_state = Path(requested_state).expanduser() if requested_state else default_runner_root()
    return command_state.resolve() == state_root.expanduser().resolve()


def _runner_command_arguments(command: str) -> list[str] | None:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    is_runner = False
    for index, value in enumerate(arguments):
        if value == "agent_bridge_connect.cli" and arguments[index + 1 : index + 3] == ["runner", "serve"]:
            is_runner = True
            break
        if Path(value).name == "agentbc" and arguments[index + 1 : index + 3] == ["runner", "serve"]:
            is_runner = True
            break
    if not is_runner:
        return None
    return arguments


def _command_option(arguments: list[str], option: str) -> str | None:
    for index, value in enumerate(arguments):
        if value == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(f"{option}="):
            return value.split("=", 1)[1]
    return None


def _terminate_runner_pids(
    pids: set[int],
    *,
    timeout_s: float,
) -> tuple[list[int], list[int], list[int]]:
    targets = sorted(pid for pid in pids if pid > 0 and pid != os.getpid())
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + max(timeout_s, 0.1)
    while time.monotonic() < deadline and any(_pid_is_alive(pid) for pid in targets):
        time.sleep(0.1)
    forced = [pid for pid in targets if _pid_is_alive(pid)]
    for pid in forced:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    force_deadline = time.monotonic() + 1.0
    while time.monotonic() < force_deadline and any(_pid_is_alive(pid) for pid in forced):
        time.sleep(0.05)
    remaining = [pid for pid in targets if _pid_is_alive(pid)]
    stopped = [pid for pid in targets if pid not in remaining]
    return stopped, [pid for pid in forced if pid not in remaining], remaining


def _tail_text(path: Path, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return str(exc)
    return text[-max_chars:]


class RunnerClient:
    def __init__(
        self,
        spool_root: str | Path | None = None,
        token_path: str | Path | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        self.spool_root = Path(spool_root or default_runner_spool()).expanduser()
        self.token_path = Path(token_path or (self.spool_root / "token")).expanduser()
        self.timeout_s = timeout_s

    def health(self) -> dict[str, Any]:
        return self._request({"op": "health"})

    def storage_status(self, paths: list[str | Path]) -> dict[str, Any]:
        """Ask the Runner to inspect storage access from its own process."""
        return self._request(
            {
                "op": "storage_status",
                "paths": [str(Path(path).expanduser()) for path in paths],
            }
        )

    def submit(
        self,
        executor: str,
        command: list[str],
        cwd: str | Path,
        task: dict[str, Any] | None = None,
        *,
        executor_run_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op": "submit",
            "executor": executor,
            "command": command,
            "cwd": str(cwd),
            "executor_run_id": executor_run_id or "",
        }
        if task is not None:
            payload["task"] = task
        return self._request(payload)

    def process_sample(self, patterns: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        return self._request({"op": "process_sample", "patterns": list(patterns or [])})

    def status(self, run_id: str) -> dict[str, Any]:
        return self._request({"op": "status", "run_id": run_id})

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._request({"op": "cancel", "run_id": run_id})

    def write_report(self, path: str | Path, content: str) -> dict[str, Any]:
        return self._request({"op": "write_report", "path": str(Path(path).expanduser()), "content": content})

    def agent_callback(
        self,
        task_id: str,
        board_root: str | Path,
        state: str,
        summary: str,
        *,
        report_file: str | Path | None = None,
        artifacts_dir: str | Path | None = None,
        executor_run_id: str | None = None,
        recovery_code: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "agent_callback",
                "task_id": task_id,
                "board_root": str(Path(board_root).expanduser()),
                "state": state,
                "summary": summary,
                "report_file": str(Path(report_file).expanduser()) if report_file else "",
                "artifacts_dir": str(Path(artifacts_dir).expanduser()) if artifacts_dir else "",
                "executor_run_id": executor_run_id or "",
                "recovery_code": recovery_code or "",
            }
        )

    def authorize_command(
        self,
        executor: str,
        command: list[str],
        cwd: str | Path,
        task: dict[str, Any],
        *,
        executor_run_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "authorize_command",
                "executor": executor,
                "command": command,
                "cwd": str(Path(cwd).expanduser()),
                "task": task,
                "executor_run_id": executor_run_id or "",
            }
        )

    def respond_approval(
        self,
        task_id: str,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        decision: str,
        *,
        board_root: str | Path | None = None,
        control_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Submit one exact accept/decline decision to the Runner control plane."""
        selected = normalize_decision(decision)
        return self._request(
            {
                "op": "respond_approval",
                "task_id": str(task_id),
                "executor_run_id": str(executor_run_id),
                "session_id": str(session_id),
                "request_id": str(request_id),
                "decision": selected,
                "board_root": str(Path(board_root).expanduser()) if board_root else "",
                "control_root": str(Path(control_root).expanduser()) if control_root else "",
            }
        )

    def control_status(
        self,
        task_id: str,
        executor_run_id: str,
        *,
        session_id: str | None = None,
        board_root: str | Path | None = None,
        control_root: str | Path | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "control_status",
                "task_id": str(task_id),
                "executor_run_id": str(executor_run_id),
                "session_id": str(session_id or ""),
                "board_root": str(Path(board_root).expanduser()) if board_root else "",
                "control_root": str(Path(control_root).expanduser()) if control_root else "",
            }
        )

    def control_events(
        self,
        task_id: str,
        executor_run_id: str,
        *,
        session_id: str | None = None,
        board_root: str | Path | None = None,
        control_root: str | Path | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "control_events",
                "task_id": str(task_id),
                "executor_run_id": str(executor_run_id),
                "session_id": str(session_id or ""),
                "board_root": str(Path(board_root).expanduser()) if board_root else "",
                "control_root": str(Path(control_root).expanduser()) if control_root else "",
            }
        )

    def show_task(self, task_id: str, board_root: str | Path) -> dict[str, Any]:
        return self._request(
            {
                "op": "show_task",
                "task_id": task_id,
                "board_root": str(Path(board_root).expanduser()),
            }
        )

    def dispatch_worker(
        self,
        task_id: str,
        executor: str,
        board_root: str | Path,
        config_path: str | Path | None,
        interval_s: float = 2.0,
        monitor: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "dispatch_worker",
                "task_id": task_id,
                "executor": executor,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "interval_s": interval_s,
                "monitor": monitor,
            }
        )

    def dispatch_task(
        self,
        task_id: str,
        board_root: str | Path,
        config_path: str | Path | None,
        interval_s: float = 2.0,
        monitor: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "dispatch_task",
                "task_id": task_id,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "interval_s": interval_s,
                "monitor": monitor,
            }
        )

    def respond_task(
        self,
        task_id: str,
        input_id: str,
        response_type: str,
        message: str,
        board_root: str | Path,
        config_path: str | Path | None,
        interval_s: float = 2.0,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "respond_task",
                "task_id": task_id,
                "input_id": input_id,
                "response_type": response_type,
                "message": message,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "interval_s": interval_s,
            }
        )

    def create_and_dispatch(
        self,
        title: str,
        assignee: str,
        steps: list[dict[str, Any]],
        board_root: str | Path,
        config_path: str | Path | None,
        session_id: str | None = None,
        source_platform: str | None = None,
        customer_dir: bool | None = None,
        customer_path: str | Path | None = None,
        images: list[str | Path] | None = None,
        interval_s: float = 2.0,
        monitor: bool = False,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "create_and_dispatch",
                "title": title,
                "assignee": assignee,
                "steps": steps,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "session_id": session_id,
                "source_platform": source_platform,
                "customer_dir": customer_dir,
                "customer_path": str(Path(customer_path).expanduser()) if customer_path else "",
                "images": [str(Path(image).expanduser()) for image in images or []],
                "interval_s": interval_s,
                "monitor": monitor,
                "permission_mode": permission_mode,
            }
        )

    def handoff_and_dispatch(
        self,
        source_task_id: str,
        target_assignee: str,
        message: str | None,
        board_root: str | Path,
        config_path: str | Path | None,
        interval_s: float = 2.0,
        monitor: bool = False,
        branch: bool = False,
        source_platform: str | None = None,
        images: list[str | Path] | None = None,
        session_id: str | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            {
                "op": "handoff_and_dispatch",
                "source_task_id": source_task_id,
                "target_assignee": target_assignee,
                "message": message,
                "branch": branch,
                "session_id": session_id,
                "source_platform": source_platform,
                "images": [str(Path(image).expanduser()) for image in images] if images is not None else None,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "interval_s": interval_s,
                "monitor": monitor,
                "permission_mode": permission_mode,
            }
        )

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RunnerError(f"runner token unavailable: {exc}") from exc
        requests_dir = self.spool_root / "requests"
        responses_dir = self.spool_root / "responses"
        if not requests_dir.is_dir() or not responses_dir.is_dir():
            raise RunnerError("runner spool is unavailable")
        request_id = uuid.uuid4().hex
        request = {
            **payload,
            "request_id": request_id,
            "token": token,
            "expires_at": time.time() + self.timeout_s,
        }
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise RunnerError("runner request exceeds size limit")
        request_path = requests_dir / f"{request_id}.json"
        temporary = requests_dir / f".{request_id}.tmp"
        temporary.write_bytes(encoded)
        os.chmod(temporary, 0o600)
        temporary.replace(request_path)
        response_path = responses_dir / f"{request_id}.json"
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    result = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RunnerError("runner returned an invalid response") from exc
                finally:
                    response_path.unlink(missing_ok=True)
                if not isinstance(result, dict) or not result.get("ok"):
                    message = result.get("error", "runner request failed") if isinstance(result, dict) else "runner request failed"
                    raise RunnerError(str(message))
                return result
            time.sleep(0.02)
        request_path.unlink(missing_ok=True)
        raise RunnerError("runner response timed out")


class RunnerState:
    def __init__(
        self,
        state_root: Path,
        allowed_roots: list[Path],
        allowed_executables: dict[str, Path],
        executable_sources: dict[str, str] | None = None,
        enable_task_dashboard: bool = False,
    ) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.allowed_roots = [root.expanduser().resolve() for root in allowed_roots]
        self.allowed_executables = {
            name: path.expanduser().resolve()
            for name, path in allowed_executables.items()
        }
        self.executable_sources = dict(executable_sources or {})
        self.enable_task_dashboard = bool(enable_task_dashboard)
        self.runs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        from .config import DEFAULT_BOARD_ROOT

        self.known_boards: set[Path] = {DEFAULT_BOARD_ROOT.expanduser().resolve()}
        (self.state_root / "runs").mkdir(parents=True, exist_ok=True)

    def storage_status(self, paths: Any) -> dict[str, Any]:
        """Return read-only path diagnostics from the authoritative Runner.

        The operation is deliberately limited to configured Runner roots and
        never creates a path.  Doctor can therefore distinguish a sandboxed
        controller's permissions from the process that actually performs task
        and report writes without exposing the Runner's root policy.
        """
        if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
            raise RunnerError("runner storage probe requires between 1 and 8 paths")
        resolved: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip():
                raise RunnerError("runner storage probe paths must be non-empty strings")
            path = Path(raw).expanduser().resolve()
            if not any(_is_within(path, root) for root in self.allowed_roots):
                raise RunnerError("runner storage probe path is outside allowed roots")
            key = str(path)
            if key in seen:
                raise RunnerError("runner storage probe paths must be unique")
            seen.add(key)
            resolved.append(path)

        status: list[dict[str, Any]] = []
        for path in resolved:
            exists = path.exists()
            status.append(
                {
                    "path": str(path),
                    "exists": exists,
                    "is_dir": path.is_dir() if exists else False,
                    "readable": os.access(path, os.R_OK) if exists else False,
                    "writable": (
                        os.access(path, os.W_OK)
                        if exists
                        else _write_capable_from_process(path)
                    ),
                }
            )
        return {"ok": True, "status": "ready", "paths": status}

    def submit(
        self,
        executor: str,
        command: list[str],
        cwd: str,
        task: dict[str, Any] | None = None,
        executor_run_id: str | None = None,
    ) -> dict[str, Any]:
        work_dir = Path(cwd).expanduser().resolve()
        with self.lock:
            self._authorize_executor_run(
                executor,
                command,
                work_dir,
                task,
                executor_run_id or "",
            )
            return self._spawn_process(
                executor,
                command,
                work_dir,
                f"runner-{executor}",
                run_id=executor_run_id or None,
            )

    def authorize_command(
        self,
        executor: str,
        command: list[str],
        cwd: str,
        task: dict[str, Any] | None,
        executor_run_id: str | None = None,
    ) -> dict[str, Any]:
        work_dir = Path(cwd).expanduser().resolve()
        with self.lock:
            permission = self._authorize_executor_run(
                executor,
                command,
                work_dir,
                task,
                executor_run_id or "",
            )
        return {
            "ok": True,
            "executor": executor,
            "executor_run_id": executor_run_id or "",
            "authorized": True,
            "effective_permission_mode": permission["effective_mode"],
        }

    def _control_plane_from_request(self, request: dict[str, Any]) -> ApprovalControlPlane:
        task_id = str(request.get("task_id") or "").strip()
        executor_run_id = str(request.get("executor_run_id") or "").strip()
        session_id = str(request.get("session_id") or "").strip()
        if not task_id or not executor_run_id or not session_id:
            raise RunnerError("approval control requires task, run, and session IDs")
        board_value = str(request.get("board_root") or "").strip()
        explicit_value = str(request.get("control_root") or "").strip()
        try:
            if explicit_value:
                root = control_root_for_task(
                    task_id,
                    board_root=board_value or None,
                    explicit_root=explicit_value,
                )
            elif board_value:
                root = control_root_for_task(task_id, board_root=board_value)
            else:
                candidates = [
                    control_root_for_task(task_id, board_root=board)
                    for board in sorted(self.known_boards, key=str)
                    if control_root_for_task(task_id, board_root=board).is_dir()
                ]
                if len(candidates) != 1:
                    raise ValueError(
                        "task board is not uniquely known; respond_approval requires an exact control root"
                    )
                root = candidates[0]
            if board_value:
                board = Path(board_value).expanduser().resolve()
                if not any(_is_within(board, allowed_root) for allowed_root in self.allowed_roots):
                    raise ValueError("task board is outside allowed roots")
                if not _is_within(root, board):
                    raise ValueError("control root is outside the task board")
                if root != control_root_for_task(task_id, board_root=board):
                    raise ValueError("control root is not the canonical task control directory")
            elif root.parent.name != ".agentbc-control":
                raise ValueError("control root is not the canonical task control directory")
            if not any(_is_within(root, allowed_root) for allowed_root in self.allowed_roots):
                raise ValueError("control root is outside allowed roots")
            return ApprovalControlPlane(
                root,
                task_id=task_id,
                executor_run_id=executor_run_id,
                session_id=session_id,
                executor=str(request.get("executor") or "codex"),
                create=False,
            )
        except (ValueError, ControlPlaneError) as exc:
            raise RunnerError(f"approval_control_invalid: {exc}") from exc

    def respond_approval(self, request: dict[str, Any]) -> dict[str, Any]:
        """Atomically record one Core/Task 3 single-action decision."""
        try:
            plane = self._control_plane_from_request(request)
            response = plane.respond_approval(
                str(request.get("task_id") or ""),
                str(request.get("executor_run_id") or ""),
                str(request.get("session_id") or ""),
                str(request.get("request_id") or ""),
                str(request.get("decision") or ""),
            )
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            raise RunnerError(f"{getattr(exc, 'code', 'approval_control_error')}: {exc}") from exc
        return response

    def control_status(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            plane = self._control_plane_from_request(request)
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            raise RunnerError(f"{getattr(exc, 'code', 'approval_control_error')}: {exc}") from exc
        return {"ok": True, "control": plane.status()}

    def control_events(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            plane = self._control_plane_from_request(request)
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            raise RunnerError(f"{getattr(exc, 'code', 'approval_control_error')}: {exc}") from exc
        return {"ok": True, "events": plane.events()}

    def _phase2_structure_errors(
        self,
        extensions: dict[str, Any] | None,
        executor: str,
    ) -> list[str]:
        """Structural policy errors for the persisted snapshots (Task 1 validators)."""
        extensions = extensions if isinstance(extensions, dict) else {}
        errors: list[str] = []
        if RESOURCE_EXTENSION_KEY in extensions:
            errors.extend(
                validate_resource_snapshot(extensions[RESOURCE_EXTENSION_KEY], executor=executor)
            )
        if SESSION_EXTENSION_KEY in extensions:
            errors.extend(
                validate_session_snapshot(extensions[SESSION_EXTENSION_KEY], executor=executor)
            )
        return errors

    def _phase2_append_audit(
        self,
        board: Path,
        task_id: str,
        executor: str,
        outcome: str,
        reason: str,
    ) -> None:
        """Record a sanitized execution_policy_audit event (no paths, content, or secrets)."""
        from .task_store import TaskStore

        try:
            TaskStore(board).append_event(
                task_id,
                {
                    "event_type": PHASE2_AUDIT_EVENT_TYPE,
                    "task_id": task_id,
                    "executor": str(executor or "").strip().lower(),
                    "outcome": outcome,
                    "reason": reason,
                    "created_at": _utc_now(),
                },
            )
        except (ABCError, OSError, ValueError):
            # Auditing must never take down dispatch or authorization.
            pass

    def _enforce_phase2_dispatch_policy(
        self,
        service,
        task: dict[str, Any],
        executor: str,
        board: Path,
    ) -> dict[str, Any]:
        """Enforce the Phase 2 execution-policy contract at dispatch time.

        Native tasks (``workspace.executor_project_root`` present) fail closed
        with ``invalid_execution_policy`` when required snapshots are missing or
        corrupt; they are never treated as legacy. Legacy tasks missing snapshots
        are backfilled exactly once inside the Runner lock with the canonical
        fixed defaults (never the user's current configuration). Terminal legacy
        tasks are not rewritten; their public state is marked
        ``legacy_unrecorded`` through the normal metadata path.
        """
        from .task_store import TaskStore

        task_id = str(task.get("id") or task.get("task_id") or "").strip()
        workspace = task.get("workspace")
        extensions = task.get("extensions")
        if not isinstance(workspace, dict):
            workspace = {}
        if not isinstance(extensions, dict):
            extensions = {}
        native = bool(workspace.get(PHASE2_EXECUTOR_PROJECT_ROOT_KEY))
        terminal = str(task.get("status") or "").strip().lower() in TERMINAL_STATES
        required_keys = _phase2_required_policy_keys(executor)
        missing = [key for key in required_keys if key not in extensions]
        if native:
            errors = [f"missing {key}" for key in missing]
            errors.extend(self._phase2_structure_errors(extensions, executor))
            if errors:
                reason = f"invalid_execution_policy: {'; '.join(errors)}"
                self._phase2_append_audit(board, task_id, executor, "fail", reason)
                raise RunnerError(reason)
            return task
        if terminal:
            execution = extensions.get(PHASE2_EXECUTION_EXTENSION_KEY)
            if not (
                isinstance(execution, dict) and execution.get(PHASE2_LEGACY_UNRECORDED_KEY) is True
            ):
                service.update_execution_metadata(task_id, {PHASE2_LEGACY_UNRECORDED_KEY: True})
            return task
        refreshed = task
        if missing:
            with self.lock:
                store = TaskStore(board)
                persisted = store.read_task(task_id)
                persisted_extensions = persisted.get("extensions")
                persisted_workspace = persisted.get("workspace")
                if not isinstance(persisted_extensions, dict):
                    persisted_extensions = {}
                if not isinstance(persisted_workspace, dict):
                    persisted_workspace = {}
                persisted_required = _phase2_required_policy_keys(executor)
                persisted_missing = [
                    key for key in persisted_required if key not in persisted_extensions
                ]
                persisted_errors = self._phase2_structure_errors(
                    persisted_extensions, executor
                )
                if persisted_errors:
                    reason = f"invalid_execution_policy: {'; '.join(persisted_errors)}"
                    self._phase2_append_audit(
                        board, task_id, executor, "fail", reason
                    )
                    raise RunnerError(reason)
                if not persisted_missing:
                    return persisted
                resources, session = _phase2_legacy_default_snapshots(
                    executor,
                    task_id,
                    persisted_workspace,
                )
                updated_extensions = attach_execution_policy(
                    persisted_extensions,
                    resources=(
                        resources
                        if RESOURCE_EXTENSION_KEY in persisted_missing
                        else None
                    ),
                    session=(
                        session if SESSION_EXTENSION_KEY in persisted_missing else None
                    ),
                )
                execution = dict(persisted_extensions.get(PHASE2_EXECUTION_EXTENSION_KEY) or {})
                execution[PHASE2_LEGACY_BACKFILLED_KEY] = True
                execution[PHASE2_LEGACY_BACKFILLED_AT_KEY] = _utc_now()
                updated_extensions[PHASE2_EXECUTION_EXTENSION_KEY] = execution
                now = _utc_now()
                store.write_task(
                    task_id,
                    {**persisted, "extensions": updated_extensions, "updated_at": now},
                )
                self._phase2_append_audit(
                    board,
                    task_id,
                    executor,
                    "backfilled",
                    "legacy_snapshot_backfilled",
                )
                refreshed = store.read_task(task_id)
        refreshed_extensions = refreshed.get("extensions")
        if not isinstance(refreshed_extensions, dict):
            refreshed_extensions = {}
        errors = [
            f"missing {key}"
            for key in _phase2_required_policy_keys(executor)
            if key not in refreshed_extensions
        ]
        errors.extend(self._phase2_structure_errors(refreshed_extensions, executor))
        if errors:
            reason = f"invalid_execution_policy: {'; '.join(errors)}"
            self._phase2_append_audit(board, task_id, executor, "fail", reason)
            raise RunnerError(reason)
        return refreshed

    def _enforce_phase2_authorization(
        self,
        executor: str,
        task: dict[str, Any] | None,
    ) -> None:
        """Validate the worker task packet against the authoritative disk snapshot.

        Fails closed on missing/injected/modified/expired policy content and on
        invalid or missing disk snapshots (Phase 2 native tasks never fall back
        to legacy auto-fix here). Every failure is recorded as a sanitized
        ``execution_policy_audit`` event.
        """
        from .task_store import TaskStore

        if not isinstance(task, dict):
            return
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        task_board = task.get("task_board") if isinstance(task.get("task_board"), dict) else {}
        board_value = str(task_board.get("root") or "").strip()
        if not task_id or not board_value:
            # Identity is already enforced by _persisted_permission_authorization.
            return
        board = Path(board_value).expanduser().resolve()
        self.known_boards.add(board)
        from .config import DEFAULT_BOARD_ROOT

        default_board = DEFAULT_BOARD_ROOT.expanduser().resolve()
        if board != default_board and not any(_is_within(board, root) for root in self.allowed_roots):
            raise RunnerError("task board is outside allowed roots")
        persisted = TaskStore(board).read_task(task_id)
        mismatch = _phase2_packet_policy_mismatch(task, persisted)
        if mismatch is not None:
            self._phase2_append_audit(board, task_id, executor, "fail", mismatch)
            raise RunnerError(f"execution_policy_mismatch: {mismatch}")
        persisted_extensions = persisted.get("extensions")
        if not isinstance(persisted_extensions, dict):
            persisted_extensions = {}
        errors = [
            f"missing {key}"
            for key in _phase2_required_policy_keys(executor)
            if key not in persisted_extensions
        ]
        errors.extend(self._phase2_structure_errors(persisted_extensions, executor))
        if errors:
            reason = f"invalid_execution_policy: {'; '.join(errors)}"
            self._phase2_append_audit(board, task_id, executor, "fail", reason)
            raise RunnerError(reason)

    def dispatch_worker(
        self,
        task_id: str,
        executor: str,
        board_root: str,
        config_path: str,
        interval_s: float,
        monitor: bool,
        resuming: bool = False,
    ) -> dict[str, Any]:
        if executor not in self.allowed_executables:
            raise RunnerError(f"runner does not allow executor: {executor}")
        board = Path(board_root).expanduser().resolve()
        from .config import DEFAULT_BOARD_ROOT, load_config

        default_board = DEFAULT_BOARD_ROOT.expanduser().resolve()
        if board != default_board and not any(_is_within(board, root) for root in self.allowed_roots):
            raise RunnerError(f"task board is outside allowed roots: {board}")
        from .service import TaskService
        from .task_store import TaskStore

        config = Path(config_path).expanduser().resolve() if config_path else None
        loaded_config = load_config(config)
        service = TaskService(board, config=loaded_config)
        try:
            task_model = service.ensure_task_permission(task_id)
        except Exception as exc:
            raise RunnerError(f"runner task unavailable: {task_id}") from exc
        task = task_model.to_dict()
        task_id = task_model.id
        task = self._enforce_phase2_dispatch_policy(service, task, executor, board)
        task_model = service.get_task(task_id)
        self._validate_task_path_plan(task)
        if task.get("assignee") != executor:
            raise RunnerError(f"task {task_id} is not assigned to {executor}")
        execution = dict((task.get("extensions") or {}).get("agentbc.execution") or {})
        is_resuming = task.get("status") == "running" and execution.get("internal_status") == "resuming"
        if task.get("status") != "pending" and not (resuming and is_resuming):
            raise RunnerError(f"task {task_id} is not pending")
        workspace = Path(str((task.get("workspace") or {}).get("project_root") or (task.get("workspace") or {}).get("root") or "")).expanduser().resolve()
        if bool((task.get("workspace") or {}).get("customer_dir")) and not workspace.is_dir():
            raise RunnerError(f"task project root does not exist: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        if config is not None:
            allowed_config_root = (Path.home() / ".abc").resolve()
            if not _is_within(config, allowed_config_root):
                raise RunnerError("worker config is outside ~/.abc")
        self._validate_executor_config(executor, config)
        permission = permission_record_from_extensions(task_model.extensions, allow_legacy=False)
        try:
            assert_executor_permission_supported(
                executor,
                permission["effective_mode"],
                self.allowed_executables.get(executor),
            )
        except ABCError as exc:
            raise RunnerError(f"{exc.code}: {exc}") from exc
        TaskStore(board).append_event(
            task_id,
            {
                "event_type": "permission_audit",
                "task_id": task_id,
                "executor": executor,
                "requested_mode": permission["requested_mode"],
                "effective_mode": permission["effective_mode"],
                "selection_source": permission["selection_source"],
                "created_at": _utc_now(),
            },
        )
        command = [
            sys.executable,
            "-m",
            "agent_bridge_connect.cli",
            "worker",
            "run",
            "--root",
            str(board),
            "--executor",
            executor,
            "--once",
            "--interval",
            str(max(interval_s, 0.1)),
            "--task-id",
            task_id,
            "--runner-authorize",
        ]
        if config is not None:
            command.extend(["--config", str(config)])
        result = self._spawn_process(f"worker:{executor}", command, workspace, "runner-worker")
        service.update_execution_metadata(
            task_id,
            {
                "worker_run_id": result["run_id"],
                "worker_pid": result["pid"],
                "dispatch_status": "accepted",
            },
        )
        TaskStore(board).append_event(
            task_id,
            {
                "event_type": "worker_dispatched",
                "task_id": task_id,
                "executor_id": executor,
                "worker_run_id": result["run_id"],
                "created_at": _utc_now(),
            },
        )
        monitor_result = self._open_task_monitor(task_id, board) if monitor else {"status": "disabled"}
        service.update_execution_metadata(
            task_id,
            {
                "monitor_status": monitor_result["status"],
                "monitor_message": monitor_result.get("message"),
            },
        )
        return {
            **result,
            "task_id": task_id,
            "dispatch_status": "accepted",
            "monitor_status": monitor_result["status"],
        }

    def create_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        from .config import load_config, resolve_workspace_root
        from .path_model import derive_customer_path_plan
        from .service import TaskService

        assignee = str(request.get("assignee") or "")
        if assignee not in self.allowed_executables:
            raise RunnerError(f"runner does not allow executor: {assignee}")
        board = self._atomic_board(str(request.get("board_root") or ""))
        config = self._atomic_config(str(request.get("config_path") or ""))
        loaded_config = load_config(config)
        self._validate_executor_config(assignee, config, loaded_config)
        try:
            customer_dir, customer_path_value = derive_customer_path_plan(
                request.get("customer_path"),
                request.get("customer_dir") if isinstance(request.get("customer_dir"), bool) else None,
            )
        except ABCError as exc:
            raise RunnerError(str(exc)) from exc
        customer_path = str(customer_path_value or "")
        project_root = Path(customer_path).expanduser().resolve() if customer_dir else resolve_workspace_root(loaded_config)
        if customer_dir:
            if not project_root.is_dir():
                raise RunnerError(f"customer_path does not exist: {project_root}")
        else:
            self._atomic_workspace(project_root, require_existing=False)
        service = TaskService(board, config=loaded_config)
        task = service.create_task(
            title=str(request.get("title") or ""),
            assignee=assignee,
            steps=request.get("steps") or [],
            session_id=request.get("session_id"),
            source_platform=request.get("source_platform"),
            customer_dir=customer_dir,
            customer_path=customer_path or None,
            images=request.get("images") or [],
            permission_mode=request.get("permission_mode"),
        )
        return self._atomic_dispatch_task(service, task, config, request)

    def dispatch_task(self, request: dict[str, Any]) -> dict[str, Any]:
        from .config import load_config
        from .execution_policy import execution_policy_view
        from .service import TaskService

        board = self._atomic_board(str(request.get("board_root") or ""))
        config = self._atomic_config(str(request.get("config_path") or ""))
        task_id = str(request.get("task_id") or "")
        loaded_config = load_config(config)
        service = TaskService(board, config=loaded_config)
        try:
            task_model = service.get_task(task_id)
            task = task_model.to_dict()
            task_id = task_model.id
        except Exception as exc:
            raise RunnerError(f"runner task unavailable: {task_id}") from exc
        self._validate_task_path_plan(task)
        executor = str(task.get("assignee") or "")
        self._validate_executor_config(executor, config, loaded_config)
        if str(task.get("status") or "") in {"needs_recovery", "failed"}:
            service.requeue_task(task_id)
        dispatched = self.dispatch_worker(
            task_id,
            executor,
            str(board),
            str(config) if config else "",
            float(request.get("interval_s") or 2.0),
            bool(request.get("monitor", False)),
        )
        self._ensure_task_list_dashboard(board, task_id=task_id)
        return {
            **dispatched,
            "execution_policy": execution_policy_view(task_model.extensions),
        }

    def respond_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        """Record an input answer and launch the same task under one Runner lock."""
        from .config import load_config
        from .notifications import notify_terminal
        from .reports import write_report_files
        from .service import TaskService

        board = self._atomic_board(str(request.get("board_root") or ""))
        config = self._atomic_config(str(request.get("config_path") or ""))
        task_id = str(request.get("task_id") or "")
        with self.lock:
            service = TaskService(board, config=load_config(config))
            expired = service.expire_waiting_inputs()
            if any(item.get("task_id") == task_id for item in expired):
                expired_task = service.get_task(task_id)
                timed_out_permission = (
                    expired_task.status == "failed"
                    and bool(expired_task.errors)
                    and expired_task.errors[-1].get("code")
                    == "permission_denied_by_timeout"
                )
                event_type = "task.failed" if timed_out_permission else "task.recovery_required"
                level = "error" if timed_out_permission else "warning"
                message = (
                    "Permission request timed out and was automatically denied"
                    if timed_out_permission
                    else "Input response deadline expired"
                )
                write_report_files(task_id, board)
                notify_terminal(
                    service,
                    task_id,
                    event_type,
                    level,
                    message,
                )
                self._refresh_task_list_dashboard(board)
                raise RunnerError(f"input deadline expired for task {task_id}")
            try:
                result = service.respond_to_input(
                    task_id,
                    str(request.get("input_id") or ""),
                    response_type=str(request.get("response_type") or ""),
                    message=str(request.get("message") or ""),
                )
            except ABCError as exc:
                raise RunnerError(f"{exc.code}: {exc}") from exc
            if not result.get("dispatch_required"):
                if result.get("resource_terminated"):
                    failure = result.get("failure") or {}
                    failure_message = str(
                        failure.get("message")
                        or "Task terminated after executor resource exhaustion"
                    )
                    write_report_files(task_id, board)
                    notify_terminal(
                        service,
                        task_id,
                        "task.failed",
                        "error",
                        failure_message,
                    )
                    self._refresh_task_list_dashboard(board)
                return result
            task = service.get_task(task_id)
            try:
                self._validate_task_path_plan(task.to_dict())
                dispatched = self.dispatch_worker(
                    task.id,
                    task.assignee,
                    str(board),
                    str(config) if config else "",
                    float(request.get("interval_s") or 2.0),
                    False,
                    resuming=True,
                )
            except RunnerError as exc:
                task_extensions = task.extensions if isinstance(task.extensions, dict) else {}
                if PERMISSION_GRANT_EXTENSION_KEY in task_extensions:
                    revoke = getattr(service, "revoke_permission_grant", None)
                    if not callable(revoke):
                        raise RunnerError(
                            "permission_grant_revoke_unavailable: Core revoke helper is required"
                        ) from exc
                    try:
                        revoke(task.id, "dispatch_failed")
                    except ABCError as revoke_exc:
                        raise RunnerError(
                            f"{revoke_exc.code}: {revoke_exc}"
                        ) from revoke_exc
                service.mark_task_needs_recovery(
                    task.id,
                    "input_resume_dispatch_failed",
                    str(exc),
                    {
                        "input_id": result.get("input_id", ""),
                        "executor": task.assignee,
                        "phase": "resume_dispatch",
                    },
                )
                write_report_files(task.id, board)
                notify_terminal(
                    service,
                    task.id,
                    "task.recovery_required",
                    "warning",
                    f"Resume dispatch failed: {exc}",
                )
                self._refresh_task_list_dashboard(board)
                raise
            self._ensure_task_list_dashboard(board, task_id=task.id)
            return {
                **result,
                **dispatched,
                "task_id": task.id,
                "status": "running",
                "same_task": True,
            }

    def maintain_waiting_inputs(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Expire durable input waits only from Runner-owned maintenance."""
        from .notifications import notify_terminal
        from .reports import write_report_files
        from .service import TaskService

        expired: list[dict[str, Any]] = []
        with self.lock:
            for board in sorted(self.known_boards, key=str):
                try:
                    service = TaskService(board)
                    items = service.expire_waiting_inputs(now=now)
                except (ABCError, OSError, ValueError):
                    continue
                for item in items:
                    task_id = str(item.get("task_id") or "")
                    try:
                        expired_task = service.get_task(task_id)
                        timed_out_permission = (
                            expired_task.status == "failed"
                            and bool(expired_task.errors)
                            and expired_task.errors[-1].get("code")
                            == "permission_denied_by_timeout"
                        )
                        write_report_files(task_id, board)
                        notify_terminal(
                            service,
                            task_id,
                            "task.failed" if timed_out_permission else "task.recovery_required",
                            "error" if timed_out_permission else "warning",
                            (
                                "Permission request timed out and was automatically denied"
                                if timed_out_permission
                                else "Input response deadline expired"
                            ),
                        )
                        self._refresh_task_list_dashboard(board)
                    except (ABCError, OSError, ValueError):
                        pass
                    expired.append(item)
        return expired

    def maintain_session_cleanup(self, *, now: str | None = None) -> list[dict[str, Any]]:
        """Runner-owned cleanup maintenance for terminal executor sessions.

        Scans every known board for terminal sessions that need a cleanup pass:
        eligible ``not_requested`` sessions, due ``failed`` retries, and
        ``pending`` receipts left over from a crashed process.  The coordinator
        re-reads each authoritative snapshot from disk under a per-task lock and
        never mutates non-eligible tasks.
        """
        from .session_cleanup import SessionCleanupCoordinator

        processed: list[dict[str, Any]] = []
        with self.lock:
            boards = tuple(sorted(self.known_boards, key=str))
        for board in boards:
            try:
                coordinator = SessionCleanupCoordinator(board)
                processed.extend(coordinator.maintain_board(now=now))
            except (ABCError, OSError, ValueError):
                continue
        return processed

    def handoff_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        from .config import load_config
        from .service import TaskService

        target = str(request.get("target_assignee") or "")
        if target not in self.allowed_executables:
            raise RunnerError(f"runner does not allow executor: {target}")
        board = self._atomic_board(str(request.get("board_root") or ""))
        config = self._atomic_config(str(request.get("config_path") or ""))
        service = TaskService(board, config=load_config(config))
        self._validate_executor_config(target, config, service.config)
        source_id = str(request.get("source_task_id") or "")
        source = service.get_task(source_id)
        self._validate_task_path_plan(source.to_dict())
        task = service.handoff_task(
            source_id,
            target,
            request.get("message"),
            branch=bool(request.get("branch", False)),
            session_id=request.get("session_id"),
            source_platform=request.get("source_platform"),
            images=request.get("images"),
            permission_mode=request.get("permission_mode"),
        )
        return self._atomic_dispatch_task(service, task, config, request)

    def _atomic_dispatch_task(
        self,
        service,
        task,
        config: Path | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        from .execution_policy import execution_policy_view, public_workspace_view
        from .reports import write_report_files

        try:
            dispatched = self.dispatch_worker(
                task.id,
                task.assignee,
                str(service.board_root),
                str(config) if config else "",
                float(request.get("interval_s") or 2.0),
                bool(request.get("monitor", False)),
            )
        except RunnerError as exc:
            service.mark_task_needs_recovery(
                task.id,
                "runner_dispatch_failed",
                str(exc),
                {"executor": task.assignee},
            )
            write_report_files(task.id, service.board_root)
            self._refresh_task_list_dashboard(service.board_root)
            raise
        self._ensure_task_list_dashboard(service.board_root, task_id=task.id)
        return {
            **dispatched,
            "task_id": task.id,
            "assignee": task.assignee,
            "workspace": public_workspace_view(task.workspace),
            "execution_policy": execution_policy_view(task.extensions),
        }

    def _validate_task_path_plan(self, task: dict[str, Any]) -> None:
        from .media import normalize_image_inputs, task_image_paths
        from .path_model import validate_path_plan_workspace

        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        try:
            validate_path_plan_workspace(workspace)
        except Exception as exc:
            raise RunnerError(str(exc)) from exc
        project_root = Path(str(workspace.get("project_root") or workspace.get("root") or "")).expanduser().resolve()
        artifact_root = Path(str(workspace.get("artifact_root") or workspace.get("artifacts_dir") or "")).expanduser().resolve()
        report_root = Path(str(workspace.get("report_root") or workspace.get("output_dir") or "")).expanduser().resolve()
        agentbc_root = Path(str(workspace.get("agentbc_root") or "")).expanduser().resolve()
        if not any(_is_within(agentbc_root, root) for root in self.allowed_roots):
            raise RunnerError(f"task AgentBC root is outside allowed roots: {agentbc_root}")
        path_roots = [agentbc_root]
        if bool(workspace.get("customer_dir")):
            customer_root = Path(str(workspace.get("customer_path") or "")).expanduser().resolve()
            if not customer_root.is_dir():
                raise RunnerError(f"task customer_path does not exist: {customer_root}")
            path_roots.append(customer_root)
            if not _is_within(project_root, customer_root):
                raise RunnerError(f"task project root is outside customer_path: {project_root}")
            if not _is_within(artifact_root, customer_root):
                raise RunnerError(f"task artifact root is outside customer_path: {artifact_root}")
        else:
            if not _is_within(project_root, agentbc_root):
                raise RunnerError(f"task project root is outside AgentBC workspace: {project_root}")
            if not _is_within(artifact_root, agentbc_root):
                raise RunnerError(f"task artifact root is outside AgentBC workspace: {artifact_root}")
        if not any(_is_within(project_root, root) for root in path_roots):
            raise RunnerError(f"task project root is outside task allowed roots: {project_root}")
        if not any(_is_within(artifact_root, root) for root in path_roots):
            raise RunnerError(f"task artifact root is outside task allowed roots: {artifact_root}")
        if not _is_within(report_root, agentbc_root):
            raise RunnerError(f"task report directory is outside AgentBC workspace: {report_root}")
        try:
            normalize_image_inputs(task_image_paths(task), allowed_roots=path_roots)
        except ABCError as exc:
            raise RunnerError(str(exc)) from exc

    def _task_scoped_allowed_roots(self, task: dict[str, Any]) -> list[Path]:
        self._validate_task_path_plan(task)
        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        roots: list[Path] = []
        for key in ("agentbc_root", "project_root", "root", "artifact_root", "artifacts_dir", "report_root", "output_dir"):
            value = str(workspace.get(key) or "").strip()
            if value:
                roots.append(Path(value).expanduser().resolve())
        if bool(workspace.get("customer_dir")):
            customer_path = str(workspace.get("customer_path") or "").strip()
            if customer_path:
                roots.append(Path(customer_path).expanduser().resolve())
        for key in ("task_file", "report_file"):
            value = str(workspace.get(key) or "").strip()
            if value:
                roots.append(Path(value).expanduser().resolve().parent)
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            text = str(root)
            if text not in seen:
                seen.add(text)
                unique.append(root)
        return unique

    def _atomic_board(self, value: str) -> Path:
        board = Path(value).expanduser().resolve()
        from .config import DEFAULT_BOARD_ROOT

        default_board = DEFAULT_BOARD_ROOT.expanduser().resolve()
        if board != default_board and not any(_is_within(board, root) for root in self.allowed_roots):
            raise RunnerError(f"task board is outside allowed roots: {board}")
        self.known_boards.add(board)
        return board

    def _atomic_config(self, value: str) -> Path | None:
        config = Path(value).expanduser().resolve() if value else None
        if config is not None and not _is_within(config, (Path.home() / ".abc").resolve()):
            raise RunnerError("worker config is outside ~/.abc")
        return config

    def _atomic_workspace(self, workspace: Path, require_existing: bool = True) -> None:
        if require_existing and not workspace.is_dir():
            raise RunnerError(
                f"task workspace does not exist or is outside allowed roots: {workspace}. "
                "Do not copy the project into the AgentBC workspace; ask the user to add this path to Runner allowed_roots or choose the correct existing project path."
            )
        if not any(_is_within(workspace, root) for root in self.allowed_roots):
            raise RunnerError(
                f"task workspace is outside allowed roots: {workspace}. "
                "Do not copy the project into the AgentBC workspace; ask the user to add this path to Runner allowed_roots or choose the correct existing project path."
            )

    def _validate_executor_config(
        self,
        executor: str,
        config_path: Path | None,
        config: dict[str, Any] | None = None,
    ) -> None:
        from .config import get_executor_config, load_config

        if self.executable_sources.get(executor) not in {"config", "explicit"}:
            return
        loaded = config if config is not None else load_config(config_path)
        configured = get_executor_config(loaded, executor).get("command")
        if not isinstance(configured, str) or not configured.strip():
            return
        configured_path = Path(configured).expanduser()
        if not configured_path.is_file():
            return
        expected = self.allowed_executables.get(executor)
        actual = configured_path.resolve()
        if expected is None or actual != expected:
            raise RunnerError(
                f"runner_config_stale: {executor} command is {actual}; "
                f"Runner allowlist is {expected or 'missing'}. Restart Runner."
            )

    def _open_task_monitor(self, task_id: str, board: Path) -> dict[str, str]:
        cli = (
            f"{shlex.quote(sys.executable)} -m agent_bridge_connect.cli "
            f"task logs {shlex.quote(task_id)} --root {shlex.quote(str(board))} --follow"
        )
        command = (
            f"printf '\\033]0;AgentBC {task_id}\\007'; "
            f"{cli}; exit"
        )
        script = (
            "on run argv\n"
            "  tell application \"Terminal\"\n"
            "    activate\n"
            "    do script (item 1 of argv)\n"
            "  end tell\n"
            "end run\n"
        )
        try:
            subprocess.Popen(
                [
                    "/usr/bin/osascript",
                    "-e",
                    script,
                    command,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return {"status": "failed", "message": str(exc)}
        return {"status": "opened"}

    def _ensure_task_list_dashboard(self, board: Path, *, task_id: str | None = None) -> dict[str, str]:
        from .task_health import (
            dashboard_is_active,
            dashboard_protocol_matches,
            mark_dashboard_active,
            mark_dashboard_closed,
            register_dashboard_task,
            request_dashboard_refresh,
            stop_dashboard_process,
        )

        if not self.enable_task_dashboard:
            return {"status": "disabled"}
        with self.lock:
            active = dashboard_is_active(board)
            if active and not dashboard_protocol_matches(board):
                stop_dashboard_process(board)
                active = False
            if task_id:
                register_dashboard_task(board, task_id, reset=not active)
            request_dashboard_refresh(board)
            if active:
                return {"status": "refreshed"}
            # Terminal opens asynchronously. Reserve the dashboard before spawning it so
            # a second dispatch cannot open another window and reset the shared cohort.
            mark_dashboard_active(board, pid=os.getpid())
            opened = self._open_task_list_dashboard(board, task_id=task_id)
            if opened.get("status") == "failed":
                mark_dashboard_closed(board)
            return opened

    def _refresh_task_list_dashboard(self, board: Path) -> None:
        from .task_health import request_dashboard_refresh

        request_dashboard_refresh(board)

    def _open_task_list_dashboard(self, board: Path, *, task_id: str | None = None) -> dict[str, str]:
        task_arg = f" --watch-task-id {shlex.quote(str(task_id))}" if task_id else " --current"
        cli = (
            f"{shlex.quote(sys.executable)} -m agent_bridge_connect.cli "
            f"task list --root {shlex.quote(str(board))}{task_arg} --watch "
            "--interval 20 --auto-exit-when-idle --idle-grace 0"
        )
        close_script = (
            'sleep 0.8; /usr/bin/osascript -e "tell application \\"Terminal\\" '
            'to close window id $AGENTBC_WINDOW_ID"'
        )
        command = (
            "printf '\\033]0;AgentBC Task List\\007'; "
            "__agentbc_window_id=$(osascript -e 'tell application \"Terminal\" to id of front window' 2>/dev/null); "
            f"{cli}; "
            "__agentbc_status=$?; "
            "if [ -n \"$__agentbc_window_id\" ]; then "
            f"AGENTBC_WINDOW_ID=\"$__agentbc_window_id\" /usr/bin/nohup /bin/sh -c {shlex.quote(close_script)} >/dev/null 2>&1 </dev/null & "
            "disown >/dev/null 2>&1 || true; "
            "fi; "
            "exit $__agentbc_status"
        )
        script = (
            "on run argv\n"
            "  tell application \"Terminal\"\n"
            "    activate\n"
            "    do script (item 1 of argv)\n"
            "  end tell\n"
            "end run\n"
        )
        try:
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", script, command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return {"status": "failed", "message": str(exc)}
        return {"status": "opened"}

    def _spawn_process(
        self,
        executor: str,
        command: list[str],
        work_dir: Path,
        run_prefix: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or f"{run_prefix}-{uuid.uuid4().hex[:12]}"
        if not _EXECUTOR_RUN_ID_RE.fullmatch(run_id):
            raise RunnerError("runner run id is invalid")
        if run_id in self.runs:
            raise RunnerError(f"runner run id already exists: {run_id}")
        run_dir = self.state_root / "runs" / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RunnerError(f"runner run id already exists: {run_id}") from exc
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        stdout_file = stdout_path.open("wb")
        stderr_file = stderr_path.open("wb")
        try:
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except Exception:
            stdout_file.close()
            stderr_file.close()
            raise
        record: dict[str, Any] = {
            "run_id": run_id,
            "executor": executor,
            "command": list(command),
            "cwd": str(work_dir),
            "pid": process.pid,
            "status": "running",
            "returncode": None,
            "started_at": _utc_now(),
            "ended_at": None,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "cancel_requested": False,
            "process": process,
        }
        with self.lock:
            self.runs[run_id] = record
            self._write_metadata(record)
        threading.Thread(
            target=self._wait_for_process,
            args=(run_id, process, stdout_file, stderr_file),
            daemon=True,
        ).start()
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.runs.get(run_id)
            if record is None:
                raise RunnerError(f"unknown runner run: {run_id}")
            return self._public_record(record)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.runs.get(run_id)
            if record is None:
                raise RunnerError(f"unknown runner run: {run_id}")
            if record["status"] in TERMINAL_STATES:
                return self._public_record(record)
            record["cancel_requested"] = True
            record["status"] = "cancelling"
            process = record["process"]
            self._write_metadata(record)
        process_group = None
        try:
            process_group = os.getpgid(process.pid)
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 1.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process_group is not None:
            try:
                os.killpg(process_group, 0)
            except (ProcessLookupError, PermissionError):
                pass
            else:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        return self.status(run_id)

    def write_report(self, path: str, content: str) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not MANAGED_RECORD_NAME_RE.fullmatch(target.name):
            raise RunnerError("runner managed writes only allow TASKCODE-001-report.md-style files")
        if not any(_is_within(target, root) for root in self.allowed_roots):
            raise RunnerError(f"runner report path is outside allowed roots: {target}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_MANAGED_FILE_BYTES:
            raise RunnerError("runner report exceeds the 10KB size limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        return {"ok": True, "path": str(target), "bytes": len(encoded)}

    def agent_callback(self, request: dict[str, Any]) -> dict[str, Any]:
        from .service import TaskService

        task_id = str(request.get("task_id") or "")
        board = self._atomic_board(str(request.get("board_root") or ""))
        service = TaskService(board)
        try:
            task = service.get_task(task_id)
        except Exception as exc:
            raise RunnerError(f"runner task unavailable: {task_id}") from exc
        self._validate_task_path_plan(task.to_dict())
        try:
            recorded = service.record_agent_callback(
                task.id,
                {
                    "final_state": str(request.get("state") or ""),
                    "summary": str(request.get("summary") or ""),
                    "report_file": str(request.get("report_file") or ""),
                    "artifacts_dir": str(request.get("artifacts_dir") or ""),
                    "executor_run_id": str(request.get("executor_run_id") or ""),
                },
            )
        except ABCError as exc:
            raise RunnerError(str(exc)) from exc
        self._refresh_task_list_dashboard(board)
        current = service.get_task(task.id)
        return {
            "ok": True,
            "task_id": current.id,
            "status": current.status,
            "event_type": "task.agent_callback_recorded",
            "recorded": recorded,
            "notified": False,
            "report_file": (current.workspace or {}).get("report_file", ""),
        }

    def show_task(self, task_id: str, board_root: str) -> dict[str, Any]:
        board = self._atomic_board(board_root)
        from .service import TaskService

        try:
            task = TaskService(board).get_task(task_id)
        except Exception as exc:
            raise RunnerError(f"runner task unavailable: {task_id}") from exc
        result = self._open_task_monitor(task.id, board)
        return {"task_id": task.id, "monitor_status": result["status"], "monitor_message": result.get("message")}

    def _validate_request(
        self,
        executor: str,
        command: list[str],
        cwd: Path,
        task: dict[str, Any] | None = None,
        *,
        persisted_task: dict[str, Any] | None = None,
        permission: dict[str, Any] | None = None,
    ) -> None:
        rules = _EXECUTOR_COMMAND_RULES.get(executor)
        if rules is None:
            raise RunnerError(f"unsupported runner executor: {executor}")
        if not command or not all(isinstance(item, str) for item in command):
            raise RunnerError("runner command must be a non-empty string list")
        expected = self.allowed_executables.get(executor)
        if expected is None or Path(command[0]).expanduser().resolve() != expected:
            raise RunnerError("runner executable is not allowlisted")
        if persisted_task is None or permission is None:
            persisted_task, permission = self._persisted_permission_authorization(
                executor, task
            )
        allowed_roots = list(self.allowed_roots)
        allowed_roots.extend(self._task_scoped_allowed_roots(persisted_task))
        if not cwd.is_dir() or not any(_is_within(cwd, root) for root in allowed_roots):
            raise RunnerError(f"runner cwd is outside allowed roots: {cwd}")
        codex_app_server = (
            executor == "codex"
            and len(command) >= 2
            and command[1] == "app-server"
        )
        if codex_app_server:
            if command.count("app-server") != 1:
                raise RunnerError("codex App Server command must contain one app-server subcommand")
            if "--stdio" not in command and not any(
                token == "stdio://" or token.startswith("stdio://")
                for token in command
            ):
                raise RunnerError("codex App Server requires stdio transport")
            if any(token in {"--last", "--continue", "resume", "--ephemeral"} for token in command):
                raise RunnerError(
                    "runner_session_argument_mismatch: Codex App Server requires explicit RPC session IDs"
                )
            return
        required_subcommand = rules.get("required_subcommand")
        if required_subcommand and required_subcommand not in command:
            raise RunnerError(
                f"{executor} runner requires '{required_subcommand}' subcommand"
            )
        required_flags: set[str] = rules["required_flags"]
        missing = sorted(required_flags - set(command))
        if missing:
            raise RunnerError(
                f"{executor} runner requires flags: {', '.join(missing)}"
            )
        required_any: set[str] = rules.get("required_any_flags", set())
        if required_any and not (required_any & set(command)):
            raise RunnerError(
                f"{executor} runner requires one of: {', '.join(sorted(required_any))}"
            )
        try:
            validate_permission_command(executor, command, permission)
        except ABCError as exc:
            raise RunnerError(f"{exc.code}: {exc}") from exc

    def _authorize_executor_run(
        self,
        executor: str,
        command: list[str],
        cwd: Path,
        task: dict[str, Any] | None,
        executor_run_id: str,
    ) -> dict[str, Any]:
        """Authorize one run and atomically consume any temporary grant.

        Callers hold ``self.lock`` across this method.  Packet/disk matching,
        installed full-capability verification, target binding and durable
        consumption happen before canonical argv validation and before spawn.
        """
        if not isinstance(task, dict):
            raise RunnerError(
                "unsupported_permission_mode: missing persisted task permission authorization"
            )
        persisted, _base_permission = self._persisted_permission_authorization(
            executor, task
        )
        mismatch = _phase6_packet_authorization_mismatch(task, persisted)
        if mismatch is not None:
            raise RunnerError(f"permission_authorization_mismatch: {mismatch}")
        self._enforce_phase2_authorization(executor, task)

        task_id = str(persisted.get("id") or "").strip()
        task_board = task.get("task_board")
        board_value = (
            str(task_board.get("root") or "").strip()
            if isinstance(task_board, dict)
            else ""
        )
        if not task_id or not board_value:
            raise RunnerError(
                "permission_authorization_invalid: task identity is required"
            )
        board = Path(board_value).expanduser().resolve()
        from .service import TaskService
        from .task_store import TaskStore

        chain = TaskService(board).resolve_chain(task_id)
        if not chain.requested_is_head or len(chain.head_task_ids) != 1:
            raise RunnerError(
                "permission_authorization_mismatch: task is not the unique chain head"
            )
        extensions = dict(persisted.get("extensions") or {})
        try:
            grant = permission_grant_from_extensions(extensions)
        except ABCError as exc:
            raise RunnerError(f"{exc.code}: {exc}") from exc

        if grant is not None and grant["state"]["status"] == "issued":
            if task.get("runner_authorization_required") is not True:
                raise RunnerError(
                    "permission_grant_runner_context_required: issued grants require "
                    "an explicit Runner-managed worker packet"
                )
            if not _EXECUTOR_RUN_ID_RE.fullmatch(executor_run_id):
                raise RunnerError(
                    "permission_grant_target_invalid: executor_run_id must be an opaque identifier"
                )
            try:
                validated_grant = validate_temporary_permission_context(
                    persisted,
                    executor,
                    executor_run_id,
                    expected_status="issued",
                )
                assert_executor_permission_supported(
                    executor,
                    "full",
                    self.allowed_executables.get(executor),
                )
            except ABCError as exc:
                raise RunnerError(f"{exc.code}: {exc}") from exc
            try:
                binding = validated_grant["binding"]
                consumed = consume_permission_grant(
                    validated_grant,
                    executor_run_id,
                    executor=executor,
                    task_id=task_id,
                    input_id=str(binding.get("input_id") or ""),
                    session_id=str(binding.get("session_id") or ""),
                    source_run_id=str(binding.get("source_run_id") or ""),
                )
            except ABCError as exc:
                raise RunnerError(f"{exc.code}: {exc}") from exc
            extensions[PERMISSION_GRANT_EXTENSION_KEY] = consumed
            persisted = {**persisted, "extensions": extensions, "updated_at": _utc_now()}
            store = TaskStore(board)
            store.write_task(task_id, persisted)
            store.append_event(
                task_id,
                {
                    "event_type": "permission_grant_consumed",
                    "task_id": task_id,
                    "executor": executor,
                    "executor_run_id": executor_run_id,
                    "created_at": consumed["audit"]["consumed_at"],
                },
            )

            try:
                effective = resolve_effective_permission(
                    persisted,
                    executor,
                    executor_run_id,
                    trusted_runner_managed=True,
                )
            except ABCError as exc:
                raise RunnerError(f"{exc.code}: {exc}") from exc
            if (
                not is_temporary_permission(effective)
                or effective.get("grant_status") != "consumed"
            ):
                raise RunnerError(
                    "permission_authorization_invalid: consumed grant did not resolve "
                    "inside trusted Runner context"
                )
        else:
            try:
                effective = resolve_effective_permission(
                    persisted,
                    executor,
                    executor_run_id,
                )
            except ABCError as exc:
                raise RunnerError(f"{exc.code}: {exc}") from exc

        self._validate_request(
            executor,
            command,
            cwd,
            task,
            persisted_task=persisted,
            permission=effective,
        )
        try:
            self._validate_phase3_execution_command(
                executor,
                command,
                cwd,
                persisted,
            )
        except RunnerError as exc:
            self._phase2_append_audit(
                board,
                task_id,
                executor,
                "fail",
                str(exc).split(":", 1)[0],
            )
            raise
        return effective

    def _enforce_phase3_authorization(
        self,
        executor: str,
        command: list[str],
        cwd: Path,
        task: dict[str, Any] | None,
    ) -> None:
        persisted_task, _permission = self._persisted_permission_authorization(executor, task)
        try:
            self._validate_phase3_execution_command(executor, command, cwd, persisted_task)
        except RunnerError as exc:
            task_id = str(persisted_task.get("id") or persisted_task.get("task_id") or "")
            task_board = task.get("task_board") if isinstance(task, dict) else None
            board_value = str(task_board.get("root") or "") if isinstance(task_board, dict) else ""
            if task_id and board_value:
                self._phase2_append_audit(
                    Path(board_value).expanduser().resolve(),
                    task_id,
                    executor,
                    "fail",
                    str(exc).split(":", 1)[0],
                )
            raise

    def _validate_phase3_execution_command(
        self,
        executor: str,
        command: list[str],
        cwd: Path,
        persisted_task: dict[str, Any],
    ) -> None:
        """Match resource/session argv to the authoritative Phase 2 snapshots."""
        extensions = persisted_task.get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        session = extensions.get(SESSION_EXTENSION_KEY)
        session_errors = validate_session_snapshot(session, executor=executor)
        if session_errors:
            raise RunnerError(
                f"runner_session_argument_mismatch: {'; '.join(session_errors)}"
            )
        session_id = str(session.get("session_id") or "").strip()
        resumed = bool(session.get("run_ids") or [])

        if executor in {"claude", "hermes"}:
            resources = extensions.get(RESOURCE_EXTENSION_KEY)
            resource_errors = validate_resource_snapshot(resources, executor=executor)
            if resource_errors:
                raise RunnerError(
                    f"runner_resource_argument_mismatch: {'; '.join(resource_errors)}"
                )
            flag = "--max-budget-usd" if executor == "claude" else "--max-turns"
            values, noncanonical = _canonical_flag_values(command, flag)
            current_limit = resources.get("current_limit")
            expected_value = (
                str(float(current_limit)) if executor == "claude" else str(int(current_limit))
            )
            if noncanonical or values != [expected_value]:
                raise RunnerError(
                    f"runner_resource_argument_mismatch: {flag} must appear once with the frozen task value"
                )

        if executor == "codex" and len(command) >= 2 and command[1] == "app-server":
            if any(token in {"--last", "--continue", "resume", "--ephemeral", "--session-id", "--resume"} for token in command):
                raise RunnerError(
                    "runner_session_argument_mismatch: Codex App Server does not accept ambiguous CLI continuation"
                )
            if "--stdio" not in command and not any(
                token == "stdio://" or token.startswith("stdio://")
                for token in command
            ):
                raise RunnerError(
                    "runner_session_argument_mismatch: Codex App Server must use stdio"
                )
            return

        resume_values, resume_noncanonical = _canonical_flag_values(command, "--resume")
        session_values, session_noncanonical = _canonical_flag_values(command, "--session-id")
        if resume_noncanonical or session_noncanonical:
            raise RunnerError(
                "runner_session_argument_mismatch: session flags require separated canonical values"
            )

        if executor == "claude":
            if "--no-session-persistence" in command:
                raise RunnerError(
                    "runner_session_argument_mismatch: Claude session persistence cannot be disabled"
                )
            expected_project = Path(str(session.get("project_path") or "")).expanduser().resolve()
            if cwd != expected_project:
                raise RunnerError(
                    "runner_executor_cwd_mismatch: Claude cwd does not match the frozen session project"
                )
            if resumed:
                valid = resume_values == [session_id] and not session_values and bool(session_id)
            else:
                valid = session_values == [session_id] and not resume_values and bool(session_id)
            if not valid:
                raise RunnerError(
                    "runner_session_argument_mismatch: Claude fresh/resume flags do not match task history"
                )
            return

        if executor == "hermes":
            if "--continue" in command or "-c" in command:
                raise RunnerError(
                    "runner_session_argument_mismatch: Hermes ambiguous continuation is forbidden"
                )
            if session_values:
                raise RunnerError(
                    "runner_session_argument_mismatch: Hermes does not accept a preassigned session ID"
                )
            if resumed:
                valid = resume_values == [session_id] and bool(session_id)
            else:
                valid = not resume_values
            if not valid:
                raise RunnerError(
                    "runner_session_argument_mismatch: Hermes resume flag does not match task history"
                )
            return

        if executor == "codex":
            if "--ephemeral" in command or "--last" in command:
                raise RunnerError(
                    "runner_session_argument_mismatch: Codex requires persistent explicit-ID sessions"
                )
            resume_positions = [
                index for index, token in enumerate(command[2:], 2) if token == "resume"
            ]
            if resumed:
                valid = (
                    len(resume_positions) == 1
                    and bool(session_id)
                    and resume_positions[0] + 1 < len(command)
                    and command[resume_positions[0] + 1] == session_id
                )
            else:
                valid = not resume_positions
            if not valid:
                raise RunnerError(
                    "runner_session_argument_mismatch: Codex resume command does not match task history"
                )

    def _persisted_permission_authorization(
        self,
        executor: str,
        task: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not isinstance(task, dict):
            raise RunnerError(
                "unsupported_permission_mode: missing persisted task permission authorization"
            )
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        task_board = task.get("task_board") if isinstance(task.get("task_board"), dict) else {}
        board_value = str(task_board.get("root") or "").strip()
        if not task_id or not board_value:
            raise RunnerError(
                "unsupported_permission_mode: task id and task board are required for authorization"
            )
        board = Path(board_value).expanduser().resolve()
        from .config import DEFAULT_BOARD_ROOT
        from .task_store import TaskStore

        default_board = DEFAULT_BOARD_ROOT.expanduser().resolve()
        if board != default_board and not any(_is_within(board, root) for root in self.allowed_roots):
            raise RunnerError("task board is outside allowed roots")
        try:
            persisted = TaskStore(board).read_task(task_id)
        except ABCError as exc:
            raise RunnerError(
                f"unsupported_permission_mode: persisted task authorization unavailable: {task_id}"
            ) from exc
        if str(persisted.get("assignee") or "") != executor:
            raise RunnerError(
                "unsupported_permission_mode: persisted task executor does not match command executor"
            )
        try:
            supplied_permission = permission_record_from_extensions(
                task.get("extensions") if isinstance(task.get("extensions"), dict) else {},
                allow_legacy=False,
            )
            persisted_permission = permission_record_from_extensions(
                persisted.get("extensions") if isinstance(persisted.get("extensions"), dict) else {},
                allow_legacy=False,
            )
        except ABCError as exc:
            raise RunnerError(f"{exc.code}: {exc}") from exc
        if supplied_permission != persisted_permission:
            raise RunnerError(
                "unsupported_permission_mode: stale or command-injected permission authorization"
            )
        self._validate_task_path_plan(persisted)
        return persisted, persisted_permission

    def process_sample(self, patterns: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        patterns = list(patterns or ["agentbc", "hermes", "codex", "claude"])
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,pcpu=,pmem=,rss=,state=,etime=,command="],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - sampling must never break Runner requests.
            return {
                "ok": False,
                "source": "runner_ps",
                "patterns": patterns,
                "count": 0,
                "cpu_sum": 0.0,
                "rss_kb_sum": 0,
                "rss_mb_sum": 0.0,
                "groups": {},
                "top": [],
                "ps_ok": False,
                "ps_error": str(exc),
            }
        rows: list[dict[str, Any]] = []
        if result.returncode == 0:
            lowered = tuple(pattern.lower() for pattern in patterns)
            for line in result.stdout.splitlines():
                row = _parse_ps_sample_line(line)
                if not row:
                    continue
                command = row["command"].lower()
                if any(pattern in command for pattern in lowered) or "agent_bridge_connect" in command:
                    row["group"] = _classify_sample_process(row["command"])
                    rows.append(row)
        rows.sort(key=lambda item: float(item.get("pcpu") or 0.0), reverse=True)
        rss_sum = sum(int(row.get("rss_kb") or 0) for row in rows)
        cpu_sum = sum(float(row.get("pcpu") or 0.0) for row in rows)
        return {
            "ok": result.returncode == 0,
            "source": "runner_ps",
            "patterns": patterns,
            "count": len(rows),
            "cpu_sum": round(cpu_sum, 2),
            "rss_kb_sum": rss_sum,
            "rss_mb_sum": round(rss_sum / 1024, 1),
            "groups": _summarize_sample_process_groups(rows),
            "top": rows[:10],
            "ps_ok": result.returncode == 0,
            "ps_error": result.stderr.strip(),
        }

    def _wait_for_process(self, run_id, process, stdout_file, stderr_file) -> None:
        returncode = process.wait()
        stdout_file.close()
        stderr_file.close()
        with self.lock:
            record = self.runs[run_id]
            record["returncode"] = returncode
            record["ended_at"] = _utc_now()
            record["status"] = (
                "cancelled"
                if record["cancel_requested"]
                else "completed" if returncode == 0 else "failed"
            )
            self._write_metadata(record)

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        stdout, stdout_truncated = _read_output(Path(record["stdout_path"]))
        stderr, stderr_truncated = _read_output(Path(record["stderr_path"]))
        return {
            "ok": True,
            "run_id": record["run_id"],
            "executor": record["executor"],
            "cwd": record["cwd"],
            "pid": record["pid"],
            "status": record["status"],
            "returncode": record["returncode"],
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": stdout_truncated or stderr_truncated,
        }

    def _write_metadata(self, record: dict[str, Any]) -> None:
        public = {key: value for key, value in record.items() if key != "process"}
        path = self.state_root / "runs" / record["run_id"] / "run.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(public, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


class RunnerService:
    def __init__(
        self,
        spool_root: Path,
        token_path: Path,
        state: RunnerState,
        interval_s: float = 0.2,
    ) -> None:
        self.spool_root = spool_root.expanduser().resolve()
        self.token_path = token_path.expanduser().resolve()
        self.runner_state = state
        self.interval_s = max(interval_s, 0.01)
        self.requests_dir = self.spool_root / "requests"
        self.responses_dir = self.spool_root / "responses"
        self.processing_dir = self.spool_root / "processing"
        for path in (self.spool_root, self.requests_dir, self.responses_dir, self.processing_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        self.pid_paths = tuple(
            dict.fromkeys(
                (
                    self.runner_state.state_root / "runner.pid",
                    self.spool_root / "runner.pid",
                )
            )
        )
        self.pid_path = self.spool_root / "runner.pid"
        self._owned_pid_paths: list[Path] = []
        try:
            self._acquire_singleton_pid()
            self.runner_token = _load_or_create_token(self.token_path)
        except Exception:
            self._release_singleton_pid()
            raise
        self._stop = threading.Event()
        self._last_maintenance_at = 0.0

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            if not self._identity_is_current():
                self._stop.set()
                break
            handled = self.serve_once()
            if not handled:
                self._stop.wait(self.interval_s)

    def serve_once(self) -> bool:
        now = time.monotonic()
        if now - self._last_maintenance_at >= 60.0:
            self.runner_state.maintain_waiting_inputs()
            self.runner_state.maintain_session_cleanup()
            self._last_maintenance_at = now
        handled = False
        for request_path in sorted(self.requests_dir.glob("*.json")):
            processing_path = self.processing_dir / request_path.name
            try:
                request_path.replace(processing_path)
            except FileNotFoundError:
                continue
            handled = True
            request_id = processing_path.stem
            try:
                request = json.loads(processing_path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise RunnerError("runner request must be an object")
                if float(request.get("expires_at") or 0) < time.time():
                    raise RunnerError("runner request expired")
                if not hmac.compare_digest(
                    str(request.get("token") or ""),
                    self.runner_token,
                ):
                    raise RunnerError(f"runner authentication failed (runner pid {os.getpid()})")
                response = _dispatch_request(self.runner_state, request)
            except (ABCError, RunnerError, OSError, ValueError, json.JSONDecodeError) as exc:
                response = {"ok": False, "error": str(exc)}
            self._write_response(request_id, response)
            processing_path.unlink(missing_ok=True)
        return handled

    def shutdown(self) -> None:
        self._stop.set()
        self._release_singleton_pid()

    def _release_singleton_pid(self) -> None:
        for path in self._owned_pid_paths:
            try:
                current = path.read_text(encoding="utf-8").strip()
            except OSError:
                current = ""
            if current == str(os.getpid()):
                path.unlink(missing_ok=True)
        self._owned_pid_paths.clear()

    def _identity_is_current(self) -> bool:
        pid_text = str(os.getpid())
        for path in self.pid_paths:
            try:
                if path.read_text(encoding="utf-8").strip() != pid_text:
                    return False
            except OSError:
                return False
        try:
            current_token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return bool(current_token) and hmac.compare_digest(current_token, self.runner_token)

    def _write_response(self, request_id: str, response: dict[str, Any]) -> None:
        path = self.responses_dir / f"{request_id}.json"
        temporary = self.responses_dir / f".{request_id}.tmp"
        temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _acquire_singleton_pid(self) -> None:
        pid_text = str(os.getpid())
        for path in self.pid_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    existing = _read_runner_pid(path)
                    if existing is not None and _pid_is_alive(existing):
                        raise RunnerError(
                            f"runner already running for state {self.runner_state.state_root}: pid {existing}"
                        )
                    path.unlink(missing_ok=True)
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(pid_text + "\n")
                self._owned_pid_paths.append(path)
                break


def create_runner_service(
    spool_root: str | Path | None = None,
    token_path: str | Path | None = None,
    state_root: str | Path | None = None,
    allowed_roots: list[str | Path] | None = None,
    hermes_command: str | Path | None = None,
    codex_command: str | Path | None = None,
    config: dict[str, Any] | None = None,
    interval_s: float = 0.2,
) -> RunnerService:
    from .config import get_executor_config, load_config, resolve_runner_allowed_roots

    allowed: dict[str, Path] = {}
    sources: dict[str, str] = {}
    loaded_config = config if config is not None else load_config()
    for name, cmd in (("hermes", hermes_command), ("codex", codex_command), ("claude", None)):
        if cmd is not None:
            resolved = _resolve_runner_executable(name, str(cmd), "explicit")
        else:
            configured = get_executor_config(loaded_config, name).get("command")
            if isinstance(configured, str) and configured.strip():
                resolved = _resolve_runner_executable(name, configured, "config")
            else:
                resolved = _resolve_runner_executable(name, None, "discovery")
        if resolved is not None:
            path, source = resolved
            allowed[name] = path
            sources[name] = source
    if not allowed:
        raise RunnerError("No executor executables found for runner")
    roots = (
        [Path(root) for root in allowed_roots]
        if allowed_roots is not None
        else resolve_runner_allowed_roots(loaded_config)
    )
    state = RunnerState(
        Path(state_root or default_runner_root()),
        roots,
        allowed,
        sources,
        enable_task_dashboard=True,
    )
    spool = Path(spool_root or default_runner_spool())
    token = Path(token_path or (spool / "token"))
    return RunnerService(spool, token, state, interval_s=interval_s)


def _resolve_runner_executable(
    name: str,
    configured_command: str | None,
    configured_source: str,
) -> tuple[Path, str] | None:
    configured = configured_command.strip() if isinstance(configured_command, str) else ""
    discovery = find_binary(name, extra_paths=[configured] if configured else None)
    if not discovery.get("found"):
        return None
    resolved = Path(str(discovery["path"]))
    if configured:
        requested = Path(configured).expanduser()
        if requested.is_file() and resolved == requested.resolve():
            return resolved, configured_source
    return resolved, str(discovery.get("source") or "discovery")


def _dispatch_request(state: RunnerState, request: dict[str, Any]) -> dict[str, Any]:
    operation = str(request.get("op") or "")
    if operation == "health":
        return {
            "ok": True,
            "status": "ready",
            "pid": os.getpid(),
            "python_executable": str(Path(sys.executable).expanduser().resolve()),
            "module_path": str(Path(__file__).with_name("__init__.py").resolve()),
            "executors": sorted(state.allowed_executables),
            "executor_commands": {
                name: {
                    "path": str(path),
                    "source": state.executable_sources.get(name, "unknown"),
                }
                for name, path in sorted(state.allowed_executables.items())
            },
            "path_policy": {
                "agent_input": "customer_path",
                "default_customer_path": "default path",
                "authorization": "runner_task_scoped",
            },
            "atomic_dispatch": True,
        }
    if operation == "storage_status":
        return state.storage_status(request.get("paths"))
    if operation == "submit":
        task = request.get("task")
        return state.submit(
            str(request.get("executor") or ""),
            request.get("command") or [],
            str(request.get("cwd") or ""),
            task if isinstance(task, dict) else None,
            str(request.get("executor_run_id") or "") or None,
        )
    if operation == "authorize_command":
        task = request.get("task")
        return state.authorize_command(
            str(request.get("executor") or ""),
            request.get("command") or [],
            str(request.get("cwd") or ""),
            task if isinstance(task, dict) else None,
            str(request.get("executor_run_id") or "") or None,
        )
    if operation == "respond_approval":
        return state.respond_approval(request)
    if operation == "control_status":
        return state.control_status(request)
    if operation == "control_events":
        return state.control_events(request)
    if operation == "process_sample":
        patterns = request.get("patterns")
        return state.process_sample(patterns if isinstance(patterns, list) else None)
    if operation == "dispatch_worker":
        return state.dispatch_worker(
            str(request.get("task_id") or ""),
            str(request.get("executor") or ""),
            str(request.get("board_root") or ""),
            str(request.get("config_path") or ""),
            float(request.get("interval_s") or 2.0),
            bool(request.get("monitor", False)),
            bool(request.get("resuming", False)),
        )
    if operation == "dispatch_task":
        return state.dispatch_task(request)
    if operation == "respond_task":
        return state.respond_and_dispatch(request)
    if operation == "create_and_dispatch":
        return state.create_and_dispatch(request)
    if operation == "handoff_and_dispatch":
        return state.handoff_and_dispatch(request)
    if operation == "status":
        return state.status(str(request.get("run_id") or ""))
    if operation == "cancel":
        return state.cancel(str(request.get("run_id") or ""))
    if operation == "write_report":
        return state.write_report(str(request.get("path") or ""), str(request.get("content") or ""))
    if operation == "agent_callback":
        return state.agent_callback(request)
    if operation == "show_task":
        return state.show_task(str(request.get("task_id") or ""), str(request.get("board_root") or ""))
    raise RunnerError(f"unknown runner operation: {operation}")


def _load_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


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


def _parse_ps_sample_line(line: str) -> dict[str, Any] | None:
    parts = line.strip().split(None, 6)
    if len(parts) < 7:
        return None
    pid, ppid, pcpu, pmem, rss, state, etime_cmd = parts
    etime_parts = etime_cmd.split(None, 1)
    if len(etime_parts) != 2:
        return None
    etime, command = etime_parts
    try:
        return {
            "pid": int(pid),
            "ppid": int(ppid),
            "pcpu": float(pcpu),
            "pmem": float(pmem),
            "rss_kb": int(rss),
            "state": state,
            "etime": etime,
            "command": command,
        }
    except ValueError:
        return None


def _classify_sample_process(command: str) -> str:
    lowered = command.lower()
    if "agent_bridge_connect.cli task list" in lowered:
        return "agentbc_task_list"
    if "agent_bridge_connect.cli worker run" in lowered:
        return "agentbc_worker"
    if "agentbc runner serve" in lowered or "agent_bridge_connect.cli runner serve" in lowered:
        return "agentbc_runner"
    if "codex.app" in lowered or "com.openai.codex" in lowered:
        return "codex_gui"
    if "codex exec" in lowered or lowered.endswith("/codex") or "resources/codex" in lowered:
        return "codex_cli"
    if "hermes_cli.main dashboard" in lowered or "hermes.app" in lowered:
        return "hermes_gui"
    if "/hermes" in lowered or " hermes " in lowered:
        return "hermes_cli"
    if "/claude" in lowered or " claude " in lowered:
        return "claude_cli"
    if "agent_bridge_connect" in lowered or "agentbc" in lowered:
        return "agentbc_other"
    return "matched_other"


def _summarize_sample_process_groups(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = str(row.get("group") or "matched_other")
        current = groups.setdefault(group, {"count": 0, "cpu_sum": 0.0, "rss_kb_sum": 0})
        current["count"] += 1
        current["cpu_sum"] += float(row.get("pcpu") or 0.0)
        current["rss_kb_sum"] += int(row.get("rss_kb") or 0)
    for data in groups.values():
        data["cpu_sum"] = round(float(data["cpu_sum"]), 2)
        data["rss_mb_sum"] = round(int(data["rss_kb_sum"]) / 1024, 1)
    return dict(sorted(groups.items()))


def _read_output(path: Path) -> tuple[str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_OUTPUT_BYTES:
                handle.seek(size - MAX_OUTPUT_BYTES)
            payload = handle.read(MAX_OUTPUT_BYTES)
    except OSError:
        return "", False
    return payload.decode("utf-8", errors="replace"), size > MAX_OUTPUT_BYTES


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_capable_from_process(path: Path) -> bool:
    """Return whether this process can create a missing descendant path."""
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
