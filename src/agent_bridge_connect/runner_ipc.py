"""Runner IPC transport: the client, the service and the single request router.

ARCH-104-001 Slice B: this module owns RunnerClient, RunnerService, request
authentication/expiry/serialization, response handling, and the one
``_dispatch_request`` routing chain.  The production flow stays exactly
``RunnerClient -> _dispatch_request -> one RunnerState handler``.

Runner state, process authorization/spawn/reap, task dispatch, maintenance and
``create_runner_service`` stay owned by ``agent_bridge_connect.runner``.  The
state is typed here through the internal ``RunnerOperations`` protocol so this
module never imports ``RunnerState`` and no import cycle is introduced.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .control import normalize_decision
from .codex_desktop_archive import (
    CodexDesktopArchiveBroker,
    CodexDesktopRouteContext,
    read_desktop_route_context,
)
from .protocol import ABCError
from .runner_contract import (
    MAX_REQUEST_BYTES,
    RUNNER_IDENTITY_REFRESH_INTERVAL_S,
    RUNNER_IPC_CHANNEL_RE,
    RunnerError,
    _pid_is_alive,
    _read_runner_pid,
    default_runner_spool,
)


class RunnerOperations(Protocol):
    """Internal structural type for the Runner state the IPC layer drives.

    ARCH-104-001 Slice B: ``runner_ipc`` must not import ``RunnerState`` (that
    would close an import cycle), so the router and the service are typed
    against the exact surface they consume instead.  ``RunnerState`` remains the
    only production implementation and the only handler per operation.
    """

    state_root: Path
    spool_root: Path
    allowed_executables: dict[str, Path]
    executable_sources: dict[str, str]
    desktop_archive_broker: CodexDesktopArchiveBroker

    def storage_status(self, paths: Any) -> dict[str, Any]: ...
    def submit(
        self,
        executor: str,
        command: list[str],
        cwd: str,
        task: dict[str, Any] | None,
        executor_run_id: str | None,
    ) -> dict[str, Any]: ...
    def authorize_command(
        self,
        executor: str,
        command: list[str],
        cwd: str,
        task: dict[str, Any] | None,
        executor_run_id: str | None,
    ) -> dict[str, Any]: ...
    def authorize_transport(
        self,
        executor: str,
        transport: str,
        cwd: str,
        task: dict[str, Any] | None,
        context: dict[str, Any] | None,
        executor_run_id: str | None,
    ) -> dict[str, Any]: ...
    def respond_approval(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def control_status(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def control_events(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def process_sample(self, patterns: list[str] | None) -> dict[str, Any]: ...
    def dispatch_worker(
        self,
        task_id: str,
        executor: str,
        board_root: str,
        config_path: str,
        interval_s: float,
        monitor: bool,
        resuming: bool,
    ) -> dict[str, Any]: ...
    def dispatch_task(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def respond_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def create_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def handoff_and_dispatch(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def register_desktop_route(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def acknowledge_desktop_archive(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, run_id: str) -> dict[str, Any]: ...
    def cancel(self, run_id: str) -> dict[str, Any]: ...
    def cancel_task_runs(self, task_id: str, board_root: str) -> dict[str, Any]: ...
    def write_report(self, path: str, content: str) -> dict[str, Any]: ...
    def terminal_delivery(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def agent_callback(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def show_task(self, task_id: str, board_root: str) -> dict[str, Any]: ...
    def maintain_waiting_inputs(self, *, now: str | None = None) -> list[dict[str, Any]]: ...
    def maintain_terminal_delivery(self, *, now: str | None = None) -> list[dict[str, Any]]: ...
    def maintain_session_cleanup(
        self, *, now: str | None = None
    ) -> list[dict[str, Any]]: ...


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
        self.channel = str(os.environ.get("AGENTBC_RUNNER_CHANNEL") or "").strip()
        if self.channel and not RUNNER_IPC_CHANNEL_RE.fullmatch(self.channel):
            raise RunnerError("runner IPC channel is invalid")

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
        self._try_register_current_desktop_route()
        return self._request({"op": "status", "run_id": run_id})

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._request({"op": "cancel", "run_id": run_id})

    def cancel_task_runs(
        self,
        task_id: str,
        board_root: str | Path,
    ) -> dict[str, Any]:
        """Cancel every live Runner run immutably bound to one exact task."""
        return self._request(
            {
                "op": "cancel_task_runs",
                "task_id": str(task_id),
                "board_root": str(Path(board_root).expanduser()),
            }
        )

    def write_report(self, path: str | Path, content: str) -> dict[str, Any]:
        return self._request({"op": "write_report", "path": str(Path(path).expanduser()), "content": content})

    def deliver_terminal(self, task_id: str, board_root: str | Path) -> dict[str, Any]:
        """Ask the Runner (the production delivery owner) to deliver one task now.

        FLOW-104-002: a contained worker finalizes the task and attempts the
        report/record/index stages, but must not write the board-level
        notification side channel.  It hands the remaining stages to the Runner,
        which owns every terminal delivery replay.
        """
        return self._request(
            {
                "op": "terminal_delivery",
                "task_id": task_id,
                "board_root": str(Path(board_root).expanduser()),
            }
        )

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

    def authorize_transport(
        self,
        executor: str,
        transport: str,
        cwd: str | Path,
        task: dict[str, Any],
        context: dict[str, Any],
        *,
        executor_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Authorize a structured executor transport without fabricating CLI argv."""
        return self._request(
            {
                "op": "authorize_transport",
                "executor": executor,
                "transport": transport,
                "cwd": str(Path(cwd).expanduser()),
                "task": task,
                "context": context,
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
        files: list[str | Path] | None = None,
        interval_s: float = 2.0,
        monitor: bool = False,
        permission_mode: str | None = None,
        collaboration_spawn: bool = False,
    ) -> dict[str, Any]:
        self._try_register_current_desktop_route(board_root=board_root)
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
                "files": [str(Path(item).expanduser()) for item in files or []],
                "interval_s": interval_s,
                "monitor": monitor,
                "permission_mode": permission_mode,
                "collaboration_spawn": bool(collaboration_spawn),
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
        files: list[str | Path] | None = None,
        session_id: str | None = None,
        permission_mode: str | None = None,
    ) -> dict[str, Any]:
        self._try_register_current_desktop_route(board_root=board_root)
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
                "files": [str(Path(item).expanduser()) for item in files] if files is not None else None,
                "board_root": str(Path(board_root).expanduser()),
                "config_path": str(Path(config_path).expanduser()) if config_path else "",
                "interval_s": interval_s,
                "monitor": monitor,
                "permission_mode": permission_mode,
            }
        )

    def register_desktop_route(
        self,
        context: CodexDesktopRouteContext,
        *,
        board_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Register transient current-Desktop context with the Runner."""
        return self._request(
            {
                "op": "register_desktop_route",
                "board_root": str(Path(board_root).expanduser()) if board_root else "",
                "context": {
                    "pipe_path": context.pipe_path,
                    "dispatcher_thread_id": context.dispatcher_thread_id,
                    "mcp_runtime": context.mcp_runtime,
                    "mcp_resource": context.mcp_resource,
                    "relay_socket": context.relay_socket,
                    "relay_token": context.relay_token,
                },
            }
        )

    def acknowledge_desktop_archive(
        self,
        task_id: str,
        session_id: str,
        board_root: str | Path,
    ) -> dict[str, Any]:
        """Continue exact-session cleanup after native Desktop archive ack."""
        return self._request(
            {
                "op": "acknowledge_desktop_archive",
                "task_id": str(task_id or ""),
                "session_id": str(session_id or ""),
                "board_root": str(Path(board_root).expanduser()),
            }
        )

    def _try_register_current_desktop_route(
        self,
        *,
        board_root: str | Path | None = None,
    ) -> None:
        """Best-effort route refresh; status must remain usable offline."""
        if os.environ.get("AGENTBC_SKIP_DESKTOP_ROUTE_REGISTER") == "1":
            return
        context = read_desktop_route_context()
        if context is None:
            return
        try:
            self.register_desktop_route(context, board_root=board_root)
        except Exception:
            return

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RunnerError(f"runner token unavailable: {exc}") from exc
        requests_dir = self.spool_root / "requests"
        responses_dir = self.spool_root / "responses"
        if self.channel:
            requests_dir = requests_dir / self.channel
            responses_dir = responses_dir / self.channel
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


class RunnerService:
    def __init__(
        self,
        spool_root: Path,
        token_path: Path,
        state: RunnerOperations,
        interval_s: float = 0.2,
    ) -> None:
        self.spool_root = spool_root.expanduser().resolve()
        self.token_path = token_path.expanduser().resolve()
        self.runner_state = state
        self.runner_state.spool_root = self.spool_root
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
        self._last_identity_refresh_at = 0.0
        if not self._refresh_identity_files():
            self._release_singleton_pid()
            raise RunnerError("runner identity files could not be refreshed")

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            if not self._identity_is_current():
                self._stop.set()
                break
            now = time.monotonic()
            if (
                now - self._last_identity_refresh_at
                >= RUNNER_IDENTITY_REFRESH_INTERVAL_S
                and not self._refresh_identity_files(now=now)
            ):
                self._stop.set()
                break
            handled = self.serve_once()
            if not handled:
                self._stop.wait(self.interval_s)

    def serve_once(self) -> bool:
        now = time.monotonic()
        if now - self._last_maintenance_at >= 60.0:
            self.runner_state.maintain_waiting_inputs()
            self.runner_state.maintain_terminal_delivery()
            self.runner_state.maintain_session_cleanup()
            self._last_maintenance_at = now
        handled = False
        for request_path in sorted(self.requests_dir.glob("**/*.json")):
            relative = request_path.relative_to(self.requests_dir)
            if len(relative.parts) == 1:
                channel = ""
            elif (
                len(relative.parts) == 2
                and RUNNER_IPC_CHANNEL_RE.fullmatch(relative.parts[0])
            ):
                channel = relative.parts[0]
            else:
                continue
            processing_dir = (
                self.processing_dir / channel if channel else self.processing_dir
            )
            processing_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            processing_path = processing_dir / request_path.name
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
            self._write_response(request_id, response, channel=channel)
            processing_path.unlink(missing_ok=True)
            if channel:
                try:
                    processing_dir.rmdir()
                except OSError:
                    pass
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

    def _refresh_identity_files(self, *, now: float | None = None) -> bool:
        """Keep active `/tmp` identity files out of age-based OS cleanup.

        The token value and pid contents remain unchanged.  Refreshing their
        timestamps (and the containing spool) is the Runner heartbeat that
        distinguishes a live IPC endpoint from abandoned temporary state.
        """
        paths = (self.spool_root, self.token_path, *self.pid_paths)
        try:
            for path in paths:
                os.utime(path, None)
        except OSError:
            return False
        self._last_identity_refresh_at = time.monotonic() if now is None else now
        return True

    def _write_response(
        self,
        request_id: str,
        response: dict[str, Any],
        *,
        channel: str = "",
    ) -> None:
        response_dir = self.responses_dir / channel if channel else self.responses_dir
        response_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = response_dir / f"{request_id}.json"
        temporary = response_dir / f".{request_id}.tmp"
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


def _dispatch_request(state: RunnerOperations, request: dict[str, Any]) -> dict[str, Any]:
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
            "desktop_archive_route": state.desktop_archive_broker.public_status(),
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
    if operation == "authorize_transport":
        task = request.get("task")
        context = request.get("context")
        return state.authorize_transport(
            str(request.get("executor") or ""),
            str(request.get("transport") or ""),
            str(request.get("cwd") or ""),
            task if isinstance(task, dict) else None,
            context if isinstance(context, dict) else None,
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
    if operation == "register_desktop_route":
        return state.register_desktop_route(request)
    if operation == "acknowledge_desktop_archive":
        return state.acknowledge_desktop_archive(request)
    if operation == "status":
        return state.status(str(request.get("run_id") or ""))
    if operation == "cancel":
        return state.cancel(str(request.get("run_id") or ""))
    if operation == "cancel_task_runs":
        return state.cancel_task_runs(
            str(request.get("task_id") or ""),
            str(request.get("board_root") or ""),
        )
    if operation == "write_report":
        return state.write_report(str(request.get("path") or ""), str(request.get("content") or ""))
    if operation == "terminal_delivery":
        return state.terminal_delivery(request)
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
