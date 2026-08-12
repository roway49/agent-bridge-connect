from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import (
    ExecutorCapabilities,
    ExecutorLevel,
    PollResult,
    ProbeResult,
    SessionCleanupCapability,
    SessionCleanupRequest,
    SessionCleanupResult,
    StartResult,
)
from agent_bridge_connect.execution_contract import (
    detect_retryable_transport_failure,
    extract_callback_validation_from_events,
    route_executor_terminal,
    strip_callback_line,
)
from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.media import task_image_paths
from agent_bridge_connect.permission_modes import (
    assert_executor_permission_supported,
    permission_flags,
    permission_record_from_extensions,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import PromptPlatformExtras, build_prompt_contract
from agent_bridge_connect.runner import RunnerClient, RunnerError

from .base import CLIExecutorBase
from ..path_provider import find_binary

SAFETY_TIMEOUT_S = 24 * 60 * 60
SESSION_EXTENSION_KEY = "agentbc.session"
CODEX_CLEANUP_UNSUPPORTED_CODE = "codex_session_delete_unavailable"
CODEX_SESSION_DELETE_FAILED_CODE = "codex_session_delete_failed"
CODEX_SESSION_DELETE_INVALID_ID_CODE = "codex_session_delete_invalid_session_id"
_CODEX_FROZEN_HELP_FIXTURE = "codex_0.146.0_help.txt"
_CODEX_FROZEN_VERSION = "0.146.0"
_CODEX_CLEANUP_TIMEOUT_S = 60
_CODEX_SESSION_ABSENT_RE = re.compile(
    r"(?im)^(?:session|saved session).*(?:not found|does not exist)"
)


class CodexExecutor(CLIExecutorBase):
    """L2 Codex CLI adapter using blocking JSONL execution."""

    def __init__(self, timeout_s: int = SAFETY_TIMEOUT_S, command: str | None = None) -> None:
        super().__init__()
        self.timeout_s = timeout_s
        self._discovery = _discover_codex_binary(command)
        resolved = str(self._discovery.get("path") or "")
        self.agent_bin = Path(resolved).expanduser() if resolved else None
        self._last_run_id: str | None = None
        self._run_metadata: dict[str, dict[str, Any]] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}

    def probe(self) -> ProbeResult:
        if self.agent_bin is None:
            return ProbeResult(
                ok=False,
                message="codex unavailable",
                details={
                    "agent_bin": "",
                    "agent_bin_source": self._discovery.get("source") or "not_found",
                    "searched_paths": self._discovery.get("searched_paths") or [],
                    "manual_override": self._discovery.get("manual_override") or "",
                },
            )
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "--version"],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(
                ok=False,
                message=f"codex unavailable: {exc}",
                details={
                    "agent_bin": str(self.agent_bin),
                    "agent_bin_source": self._discovery.get("source") or "unknown",
                },
            )

        version = (completed.stdout or completed.stderr).strip()
        return ProbeResult(
            ok=completed.returncode == 0,
            message=version or f"codex exited with {completed.returncode}",
            details={
                "agent_bin": str(self.agent_bin),
                "agent_bin_source": self._discovery.get("source") or "unknown",
                "returncode": completed.returncode,
                "version": version,
            },
        )

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            structured_output=True,
            streaming_events=True,
            resume=True,
            cancel=False,
            input_required=False,
            model_selection=True,
            multimodal=True,
            image_input=True,
            image_generation=True,
            image_editing=True,
            parallelism=1,
            level=ExecutorLevel.L2,
        )

    def session_cleanup_capability(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupCapability:
        """Probe the discovered CLI help without reading any saved session data."""
        if request.retain is True:
            return SessionCleanupCapability("not_applicable", "retain")
        if self.agent_bin is None:
            return _codex_cleanup_unsupported()
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "delete", "--help"],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _codex_cleanup_unsupported()
        if completed.returncode != 0:
            return _codex_cleanup_unsupported()
        return _codex_session_cleanup_capability(
            f"{completed.stdout or ''}\n{completed.stderr or ''}"
        )

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        """Delete one exact official UUID through ``codex delete --force``."""
        if request.retain is True:
            return SessionCleanupResult("retained", "not_applicable", "retain")
        request_error = _codex_cleanup_request_error(request)
        if request_error:
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                request_error,
                False,
            )
        capability = self.session_cleanup_capability(request)
        if capability.capability != "supported":
            return SessionCleanupResult(
                "unsupported",
                "unsupported",
                "none",
                capability.error_code or CODEX_CLEANUP_UNSUPPORTED_CODE,
                False,
            )
        assert self.agent_bin is not None
        try:
            completed = subprocess.run(
                [str(self.agent_bin), "delete", "--force", request.session_id],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=_CODEX_CLEANUP_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SessionCleanupResult(
                "failed",
                "supported",
                "official_session_delete",
                CODEX_SESSION_DELETE_FAILED_CODE,
                True,
            )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode == 0 or _CODEX_SESSION_ABSENT_RE.search(output):
            return SessionCleanupResult(
                "succeeded",
                "supported",
                "official_session_delete",
            )
        return SessionCleanupResult(
            "failed",
            "supported",
            "official_session_delete",
            CODEX_SESSION_DELETE_FAILED_CODE,
            False,
        )

    def start(self, task_packet: dict) -> StartResult:
        steps = task_packet.get("steps") or []
        if not steps:
            return StartResult(ok=False, run_id="", message="no steps")
        if self.agent_bin is None:
            return StartResult(ok=False, run_id="", message="codex unavailable")

        workspace = task_packet.get("workspace") or {}
        root = Path(workspace.get("root", ".")).expanduser().resolve()
        if not root.is_dir():
            return StartResult(ok=False, run_id="", message=f"workspace not found: {root}")

        run_id = f"codex-{task_packet.get('task_id', 'unknown')}-{uuid.uuid4().hex[:8]}"
        self._task_packets[run_id] = dict(task_packet)
        self._start_run_lease(task_packet, run_id, "codex")
        prompt = _build_prompt(task_packet)
        try:
            permission = resolve_effective_permission(
                task_packet,
                "codex",
                run_id,
                trusted_runner_managed=(
                    task_packet.get("runner_authorization_required") is True
                ),
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"{exc.code}: {exc}")
        try:
            assert_executor_permission_supported(
                "codex", permission["effective_mode"], self.agent_bin
            )
            resumed, _ = _codex_resume_context(task_packet)
            command, prompt_input = self._build_command(
                task_packet,
                prompt,
                root,
                permission,
            )
        except ABCError as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=str(exc))

        try:
            if task_packet.get("runner_authorization_required") is True:
                RunnerClient().authorize_command(
                    "codex",
                    command,
                    root,
                    task_packet,
                    executor_run_id=run_id,
                )
            self._heartbeat_run(run_id)
            completed = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                input=prompt_input,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            events = _parse_jsonl(exc.stdout or "")
            self._store_metadata(run_id, root, events, returncode=None)
            self._mark_run_stale(run_id)
            timeout_result: dict[str, Any] = {
                "events": events,
                "reason": f"codex safety runtime exceeded after {self.timeout_s}s",
                "timeout_is_failure": False,
                "failure": {
                    "kind": "executor_timeout",
                    "layer": "executor",
                    "message": f"codex safety runtime exceeded after {self.timeout_s}s",
                    "retryable": True,
                },
                "extensions": self.get_extensions(),
            }
            execution_session = _execution_session_receipt(events, resumed=resumed)
            if execution_session is not None:
                timeout_result["execution_session"] = execution_session
            self._runs[run_id] = PollResult(
                status="needs_recovery",
                progress={"events_seen": len(events)},
                result=timeout_result,
            )
            return StartResult(ok=True, run_id=run_id, message="codex execution needs recovery")
        except (OSError, RunnerError) as exc:
            self._close_run_lease(run_id)
            return StartResult(ok=False, run_id="", message=f"failed to start codex: {exc}")

        self._heartbeat_run(run_id)
        events = _parse_jsonl(completed.stdout)
        summary = _extract_summary(events)
        validation = extract_callback_validation_from_events(
            events,
            task_packet,
            run_id,
        )
        terminal = route_executor_terminal(
            validation,
            completed.returncode,
            executor_name="codex",
            stderr=completed.stderr,
            runtime_failure=detect_retryable_transport_failure(completed.stdout, completed.stderr),
        )
        status = terminal.status
        result = {
            "events": events,
            "summary": summary,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "agent_callback": terminal.callback,
            "marker_valid": validation.valid,
            "marker_seen": validation.marker_seen,
            "failure": terminal.failure,
        }
        execution_session = _execution_session_receipt(events, resumed=resumed)
        if execution_session is not None:
            result["execution_session"] = execution_session
        self._store_metadata(run_id, root, events, returncode=completed.returncode)
        result["extensions"] = self.get_extensions()
        self._runs[run_id] = PollResult(
            status=status,
            progress={"events_seen": len(events)},
            result=result,
        )
        self._close_run_lease(run_id)
        return StartResult(ok=True, run_id=run_id, message=f"codex execution {status}")

    def _build_command(
        self,
        task_packet: dict[str, Any],
        prompt: str,
        root: Path,
        permission: dict[str, str] | None = None,
    ) -> tuple[list[str], str | None]:
        if self.agent_bin is None:
            raise RuntimeError("codex unavailable")
        selected = permission or permission_record_from_extensions(task_packet.get("extensions"))
        resumed, session_id = _codex_resume_context(task_packet)
        command = [str(self.agent_bin), "exec", "--json"]
        command.extend(permission_flags("codex", selected["effective_mode"]))
        command.append("--skip-git-repo-check")
        if selected["effective_mode"] == "safe":
            for writable_root in _codex_writable_roots(task_packet, root):
                command.extend(["--add-dir", str(writable_root)])
        if resumed:
            command.extend(["resume", session_id])
        images = task_image_paths(task_packet)
        prompt_input: str | None = None
        if images:
            command.append("--image")
            command.extend(str(image) for image in images)
            command.append("-")
            prompt_input = prompt
        else:
            command.append(prompt)
        return command, prompt_input

    def get_extensions(self) -> dict:
        """Return metadata suitable for storage at extensions.executor.codex."""
        metadata: dict[str, Any] = {
            "agent_bin": str(self.agent_bin) if self.agent_bin is not None else "",
            "agent_bin_source": self._discovery.get("source") or "not_found",
            "capability_level": self.capabilities().level,
            "last_run_id": self._last_run_id,
        }
        if self._last_run_id is not None:
            metadata["last_run"] = self._run_metadata[self._last_run_id]
        return {"executor": {"codex": metadata}}

    def _store_metadata(
        self,
        run_id: str,
        workspace: Path,
        events: list[dict[str, Any]],
        returncode: int | None,
    ) -> None:
        self._last_run_id = run_id
        self._run_metadata[run_id] = {
            "run_id": run_id,
            "workspace": str(workspace),
            "permission": permission_record_from_extensions(
                self._task_packets.get(run_id, {}).get("extensions")
            ),
            "writable_roots": [
                str(path) for path in _codex_writable_roots(self._task_packets.get(run_id, {}), workspace)
            ],
            "events_seen": len(events),
            "returncode": returncode,
        }


def _discover_codex_binary(command: str | None) -> dict[str, Any]:
    configured = command.strip() if isinstance(command, str) else ""
    return find_binary("codex", extra_paths=[configured] if configured else None)


_FUZZY_SELECTOR_MARKERS = ("--last", "picker")
_GLOBAL_PURGE_MARKERS = ("prune", "purge", "delete old", "delete all")
_CODEX_DELETE_USAGE_RE = re.compile(
    r"^usage:\s+codex\s+delete\b",
    re.IGNORECASE | re.MULTILINE,
)


def _frozen_help_fixture_text(fixture_name: str) -> str:
    """Read a frozen CLI help fixture from the source checkout.

    The frozen fixture is the version-pinned evidence the cleanup capability
    probe is based on. Returns an empty string when the fixture cannot be
    resolved so callers fail closed; the fixture path itself is never exposed
    in any result.
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "tests" / "fixtures" / "executor_runtime" / fixture_name
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return ""


def _codex_has_exact_session_delete_entry(help_text: str) -> bool:
    """Return True only for an official delete entry accepting an exact session ID.

    Codex may also accept names interactively, but ``--force`` explicitly
    requires SESSION to be a UUID. AgentBC validates the official UUID before
    invoking that noninteractive form, making the deletion exact.
    """
    if not help_text or _CODEX_DELETE_USAGE_RE.search(help_text) is None:
        return False
    lowered = help_text.lower()
    if any(marker in lowered for marker in _FUZZY_SELECTOR_MARKERS):
        return False
    if any(marker in lowered for marker in _GLOBAL_PURGE_MARKERS):
        return False
    force_uuid = (
        "session id (uuid) or session name" in lowered
        and "--force" in lowered
        and "session must be a uuid" in lowered
    )
    exact_positional = re.search(
        r"^\s+session_id\b.*session id to delete",
        help_text,
        re.IGNORECASE | re.MULTILINE,
    ) is not None
    return force_uuid or exact_positional


def _codex_session_cleanup_capability(help_text: str) -> SessionCleanupCapability:
    """Derive the Codex cleanup capability from frozen help fixture text."""
    if _codex_has_exact_session_delete_entry(help_text):
        return SessionCleanupCapability(
            capability="supported",
            strategy="official_session_delete",
            error_code="",
        )
    return SessionCleanupCapability(
        capability="unsupported",
        strategy="none",
        error_code=CODEX_CLEANUP_UNSUPPORTED_CODE,
    )


def _codex_cleanup_unsupported() -> SessionCleanupCapability:
    return SessionCleanupCapability(
        capability="unsupported",
        strategy="none",
        error_code=CODEX_CLEANUP_UNSUPPORTED_CODE,
    )


def _codex_cleanup_request_error(request: SessionCleanupRequest) -> str:
    if str(request.executor or "").strip().lower() != "codex":
        return "codex_cleanup_executor_mismatch"
    if request.retain is not False or request.project_mode != "none":
        return "codex_cleanup_mode_invalid"
    if request.strategy != "official_session_delete":
        return "codex_cleanup_strategy_mismatch"
    session_id = str(request.session_id or "").strip()
    try:
        parsed = uuid.UUID(session_id)
    except (AttributeError, ValueError):
        return CODEX_SESSION_DELETE_INVALID_ID_CODE
    if str(parsed) != session_id.lower():
        return CODEX_SESSION_DELETE_INVALID_ID_CODE
    return ""


def _codex_writable_roots(task_packet: dict[str, Any], workspace_root: Path) -> list[Path]:
    """Return only task deliverable and compact runtime-state write roots."""
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
    candidates: list[str | Path | None] = [
        workspace_root,
        workspace.get("project_root"),
        workspace.get("root"),
        workspace.get("artifact_root"),
        workspace.get("artifacts_dir"),
        task_board.get("root"),
    ]

    roots: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        if not value:
            continue
        path = Path(str(value)).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _build_prompt(task_packet: dict[str, Any]) -> str:
    """Build the Codex prompt: shared contract plus Codex platform notes."""
    return build_prompt_contract(
        task_packet,
        PromptPlatformExtras(
            opening="You are executing a structured task.",
            image_note="Image inputs are attached through the native Codex CLI image interface:",
            image_inputs=tuple(str(image) for image in task_image_paths(task_packet)),
            image_rule=(
                "For image generation or image editing work, use the native image-generation "
                "capability and save the final bitmap deliverables under the Artifact root; do not "
                "return only prose or preview links."
            ),
            summary_line="After completing all steps, write a summary of what you did.",
        ),
    )


def _parse_jsonl(output: str | bytes) -> list[dict[str, Any]]:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for sequence, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"type": "unparsed_output", "text": line}
        if not isinstance(payload, dict):
            payload = {"type": "codex_output", "value": payload}
        events.append(
            {
                "event_type": str(payload.get("type") or "codex_event"),
                "source": "codex",
                "sequence": sequence,
                "payload": payload,
            }
        )
    return events


def _codex_resume_context(task_packet: dict[str, Any]) -> tuple[bool, str]:
    """Return the explicit resume decision frozen into the task session snapshot."""
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict) or SESSION_EXTENSION_KEY not in extensions:
        return False, ""
    session = extensions.get(SESSION_EXTENSION_KEY)
    if not isinstance(session, dict):
        raise ABCError("invalid_executor_session", "agentbc.session must be an object")
    if str(session.get("executor") or "").strip().lower() != "codex":
        raise ABCError(
            "invalid_executor_session",
            "agentbc.session.executor must be codex",
        )
    run_ids = session.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        raise ABCError(
            "invalid_executor_session",
            "agentbc.session.run_ids must contain unique non-empty strings",
        )
    if not run_ids:
        return False, ""
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ABCError(
            "missing_executor_session_id",
            "Codex resume requires an explicit task session ID",
        )
    return True, session_id.strip()


def _extract_codex_session_id(events: list[dict[str, Any]]) -> str:
    """Extract a session ID only from one well-formed ``thread.started`` event."""
    receipts: list[str] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "thread.started":
            continue
        thread_id = payload.get("thread_id")
        if (
            isinstance(thread_id, str)
            and thread_id
            and thread_id == thread_id.strip()
            and not any(character.isspace() for character in thread_id)
        ):
            receipts.append(thread_id)
        else:
            return ""
    return receipts[0] if len(receipts) == 1 else ""


def _execution_session_receipt(
    events: list[dict[str, Any]],
    *,
    resumed: bool,
) -> dict[str, Any] | None:
    session_id = _extract_codex_session_id(events)
    if not session_id:
        return None
    return {
        "version": 1,
        "executor": "codex",
        "session_id": session_id,
        "resumed": resumed,
        "persistence": "persistent",
        "source": "jsonl_thread_started",
    }


def _extract_summary(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        payload = event.get("payload") or {}
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return strip_callback_line(str(item.get("text") or ""))
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            return strip_callback_line(str(payload.get("text") or ""))
    return ""
