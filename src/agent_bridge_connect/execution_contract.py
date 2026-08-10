from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


FINAL_CALLBACK_PREFIX = "AGENTBC_FINAL_CALLBACK:"
FINAL_CALLBACK_VERSION = 1
AGENT_FINAL_STATES = frozenset({"completed", "input_required", "cancelled"})
STEP_RESULT_STATUSES = frozenset({"done", "failed", "blocked", "pending"})
CHOICE_INPUT_TYPE = "choice"
CHOICE_OPTION_COUNT = 2
MAX_CHOICE_OPTION_LENGTH = 48
MAX_CHOICE_REASON_LENGTH = 240
MAX_CHOICE_DESCRIPTION_LENGTH = 160


@dataclass(frozen=True)
class CallbackValidation:
    marker_seen: bool
    valid: bool
    callback: dict[str, Any] | None = None
    code: str = ""
    message: str = ""


@dataclass(frozen=True)
class ExecutorTerminalResult:
    status: str
    callback: dict[str, Any] | None
    failure: dict[str, Any] | None
    resource_exhaustion: dict[str, Any] | None = None


def build_agent_callback(
    task_packet: dict[str, Any],
    final_state: str,
    summary: str,
    executor_run_id: str,
    *,
    finished_at: str | None = None,
    callback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach Core metadata without filling any required flow declaration."""
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    payload = dict(callback or {})
    payload["executor_run_id"] = executor_run_id
    payload.setdefault("report_file", str(workspace.get("report_file") or ""))
    payload.setdefault("artifacts_dir", str(workspace.get("artifacts_dir") or ""))
    payload.setdefault("finished_at", finished_at or _utc_now())
    return payload


def validate_callback_payload(
    callback: Any,
    task_id: str,
    declared_steps: list[dict[str, Any]],
) -> CallbackValidation:
    if not isinstance(callback, dict):
        return _invalid("completion_marker_invalid_payload", "Final marker payload must be a JSON object")

    missing = [
        field
        for field in ("version", "task_id", "final_state", "summary", "step_results")
        if field not in callback
    ]
    if missing:
        return _invalid(
            "completion_marker_missing_fields",
            f"Final marker is missing required fields: {', '.join(missing)}",
        )

    version = callback.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != FINAL_CALLBACK_VERSION:
        return _invalid(
            "completion_marker_version_invalid",
            f"Final marker version must be {FINAL_CALLBACK_VERSION}",
        )
    if callback.get("task_id") != task_id:
        return _invalid("completion_marker_task_mismatch", "Final marker task_id does not match the running task")

    final_state = callback.get("final_state")
    if final_state not in AGENT_FINAL_STATES:
        return _invalid("completion_marker_state_invalid", f"Unsupported final marker state: {final_state}")
    summary = callback.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return _invalid("completion_marker_summary_missing", "Final marker summary must be a non-empty string")

    declared_ids: list[int] = []
    for index, step in enumerate(declared_steps, 1):
        step_id = step.get("id", index) if isinstance(step, dict) else index
        if isinstance(step_id, bool) or not isinstance(step_id, int):
            return _invalid("completion_marker_task_steps_invalid", "Declared task step ids must be integers")
        declared_ids.append(step_id)
    if len(declared_ids) != len(set(declared_ids)):
        return _invalid("completion_marker_task_steps_invalid", "Declared task step ids must be unique")

    step_results = callback.get("step_results")
    if not isinstance(step_results, list):
        return _invalid("completion_marker_steps_invalid", "Final marker step_results must be a list")
    seen_ids: set[int] = set()
    normalized_results: list[dict[str, Any]] = []
    for item in step_results:
        if not isinstance(item, dict):
            return _invalid("completion_marker_steps_invalid", "Each step result must be an object")
        step_id = item.get("id")
        if isinstance(step_id, bool) or not isinstance(step_id, int):
            return _invalid("completion_marker_steps_invalid", "Each step result id must be an integer")
        if step_id in seen_ids:
            return _invalid("completion_marker_step_duplicate", f"Step {step_id} appears more than once")
        if step_id not in declared_ids:
            return _invalid("completion_marker_step_unknown", f"Step {step_id} is not declared by the task")
        status = item.get("status")
        if status not in STEP_RESULT_STATUSES:
            return _invalid("completion_marker_step_status_invalid", f"Step {step_id} has invalid status: {status}")
        seen_ids.add(step_id)
        normalized_results.append(dict(item))

    declared_set = set(declared_ids)
    if final_state == "completed":
        missing_ids = sorted(declared_set - seen_ids)
        if missing_ids:
            return _invalid(
                "completion_marker_steps_missing",
                f"Completed marker is missing declared steps: {', '.join(map(str, missing_ids))}",
            )
        incomplete = sorted(
            item["id"] for item in normalized_results if item.get("status") != "done"
        )
        if incomplete:
            return _invalid(
                "completion_marker_steps_incomplete",
                f"Completed marker contains non-done steps: {', '.join(map(str, incomplete))}",
            )
    elif final_state == "input_required":
        if not any(item.get("status") == "blocked" for item in normalized_results):
            return _invalid(
                "completion_marker_input_step_missing",
                "input_required marker must identify at least one blocked step",
            )
        input_details = callback.get("input")
        if isinstance(input_details, dict) and str(input_details.get("type") or "").strip().lower() == CHOICE_INPUT_TYPE:
            reason = input_details.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return _invalid(
                    "completion_marker_choice_reason_invalid",
                    "choice input must explain why the user's decision is required",
                )
            clean_reason = reason.strip()
            if len(clean_reason) > MAX_CHOICE_REASON_LENGTH:
                return _invalid(
                    "completion_marker_choice_reason_invalid",
                    f"choice input reason must be at most {MAX_CHOICE_REASON_LENGTH} characters",
                )
            raw_options = input_details.get("options")
            if not isinstance(raw_options, list) or len(raw_options) != CHOICE_OPTION_COUNT:
                return _invalid(
                    "completion_marker_choice_options_invalid",
                    "choice input must declare exactly two options",
                )
            options: list[dict[str, str]] = []
            for option in raw_options:
                if not isinstance(option, dict):
                    return _invalid(
                        "completion_marker_choice_options_invalid",
                        "choice input options must contain label and description objects",
                    )
                label = option.get("label")
                description = option.get("description")
                if not isinstance(label, str) or not label.strip():
                    return _invalid(
                        "completion_marker_choice_options_invalid",
                        "choice input option labels must be non-empty strings",
                    )
                if not isinstance(description, str) or not description.strip():
                    return _invalid(
                        "completion_marker_choice_description_invalid",
                        "choice input options must include non-empty descriptions",
                    )
                clean_label = label.strip()
                clean_description = description.strip()
                if len(clean_label) > MAX_CHOICE_OPTION_LENGTH:
                    return _invalid(
                        "completion_marker_choice_options_invalid",
                        f"choice input option labels must be at most {MAX_CHOICE_OPTION_LENGTH} characters",
                    )
                if len(clean_description) > MAX_CHOICE_DESCRIPTION_LENGTH:
                    return _invalid(
                        "completion_marker_choice_description_invalid",
                        f"choice input option descriptions must be at most {MAX_CHOICE_DESCRIPTION_LENGTH} characters",
                    )
                options.append({"label": clean_label, "description": clean_description})
            if len({option["label"] for option in options}) != CHOICE_OPTION_COUNT:
                return _invalid(
                    "completion_marker_choice_options_invalid",
                    "choice input option labels must be distinct",
                )
            callback = dict(callback)
            callback["input"] = {
                **input_details,
                "type": CHOICE_INPUT_TYPE,
                "reason": clean_reason,
                "options": options,
            }

    normalized = dict(callback)
    normalized["summary"] = summary.strip()
    normalized["step_results"] = normalized_results
    return CallbackValidation(marker_seen=True, valid=True, callback=normalized)


def extract_callback_validation_from_output(
    output: str,
    task_packet: dict[str, Any],
    executor_run_id: str,
) -> CallbackValidation:
    marker_lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(FINAL_CALLBACK_PREFIX)
    ]
    if not marker_lines:
        return CallbackValidation(
            marker_seen=False,
            valid=False,
            code="completion_marker_missing",
            message="Executor exited without AGENTBC_FINAL_CALLBACK",
        )
    if len(marker_lines) != 1:
        return _invalid("completion_marker_duplicate", "Executor emitted more than one final marker")
    raw_payload = marker_lines[0].removeprefix(FINAL_CALLBACK_PREFIX).strip()
    if not raw_payload:
        return _invalid("completion_marker_json_invalid", "Final marker JSON payload is empty")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        return _invalid("completion_marker_json_invalid", f"Final marker contains invalid JSON: {exc.msg}")

    validation = validate_callback_payload(
        payload,
        str(task_packet.get("task_id") or ""),
        list(task_packet.get("steps") or []),
    )
    if not validation.valid or validation.callback is None:
        return validation
    return CallbackValidation(
        marker_seen=True,
        valid=True,
        callback=build_agent_callback(
            task_packet,
            str(validation.callback.get("final_state") or ""),
            str(validation.callback.get("summary") or ""),
            executor_run_id,
            callback=validation.callback,
        ),
    )


def extract_callback_validation_from_events(
    events: list[dict[str, Any]],
    task_packet: dict[str, Any],
    executor_run_id: str,
) -> CallbackValidation:
    texts: list[str] = []
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict):
            item = payload.get("item")
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return extract_callback_validation_from_output(
        "\n".join(texts), task_packet, executor_run_id
    )


def route_executor_terminal(
    validation: CallbackValidation,
    returncode: int,
    *,
    executor_name: str,
    stderr: str = "",
    runtime_failure: dict[str, Any] | None = None,
    resource_exhaustion: dict[str, Any] | None = None,
) -> ExecutorTerminalResult:
    """Route only declared flow state plus explicit process/transport evidence.

    Terminal-state priority is fixed: a valid agent callback wins, then a
    retryable transport/infrastructure failure keeps ``needs_recovery``, then
    a confirmed resource exhaustion becomes a system ``input_required`` wait,
    and only then does a strict marker failure apply.
    """
    if returncode == 0 and validation.valid and validation.callback is not None:
        return ExecutorTerminalResult(
            status=str(validation.callback["final_state"]),
            callback=validation.callback,
            failure=None,
        )
    if isinstance(runtime_failure, dict) and runtime_failure.get("retryable") is True:
        return ExecutorTerminalResult("needs_recovery", None, dict(runtime_failure))
    if isinstance(resource_exhaustion, dict) and resource_exhaustion.get("detected") is True:
        return _route_resource_exhaustion(resource_exhaustion)
    if returncode == 0:
        return ExecutorTerminalResult(
            "failed",
            None,
            {
                "kind": validation.code or "completion_marker_invalid",
                "layer": "flow_contract",
                "message": validation.message or "Executor final marker is invalid",
                "retryable": False,
            },
        )
    failure = dict(runtime_failure or {})
    failure.setdefault("kind", "executor_exit_nonzero")
    failure.setdefault("layer", "executor")
    failure.setdefault(
        "message",
        stderr.strip() or f"{executor_name} exited with {returncode}",
    )
    failure["retryable"] = False
    return ExecutorTerminalResult("failed", None, failure)


def build_resource_exhaustion(
    executor: str,
    resource: str,
    *,
    used: int | float | None,
    limit: int | float | None,
    source: str,
    snapshot_limit: int | float | None = None,
) -> dict[str, Any]:
    """Build the structured resource-exhaustion receipt for adapter output.

    ``limit_matches_snapshot`` is ``None`` when the snapshot is unavailable or
    the exhausted run did not report a concrete limit (so no mismatch evidence
    exists); only an explicit ``False`` forces ``needs_recovery``.
    """
    matches: bool | None = None
    if limit is not None and snapshot_limit is not None:
        matches = _resource_limits_equal(limit, snapshot_limit)
    return {
        "detected": True,
        "executor": str(executor or "").strip().lower(),
        "resource": str(resource or "").strip(),
        "used": used,
        "limit": limit,
        "source": str(source or "").strip(),
        "limit_matches_snapshot": matches,
    }


def resource_snapshot_limit(task_packet: Any, executor: str) -> int | float | None:
    """Return the frozen task resource limit for one executor, if present."""
    if not isinstance(task_packet, dict):
        return None
    extensions = task_packet.get("extensions")
    if not isinstance(extensions, dict):
        return None
    resources = extensions.get("agentbc.resources")
    if not isinstance(resources, dict):
        return None
    if str(resources.get("executor") or "").strip().lower() != str(executor).strip().lower():
        return None
    value = resources.get("current_limit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _route_resource_exhaustion(
    resource_exhaustion: dict[str, Any],
) -> ExecutorTerminalResult:
    payload = dict(resource_exhaustion)
    if payload.get("limit_matches_snapshot") is False:
        return ExecutorTerminalResult(
            "needs_recovery",
            None,
            {
                "kind": "resource_exhaustion_receipt_mismatch",
                "layer": "flow_contract",
                "message": (
                    "Executor resource exhaustion limit does not match the task "
                    "snapshot; refusing to auto-scale the resource"
                ),
                "retryable": False,
                "resource_exhaustion": payload,
            },
            resource_exhaustion=payload,
        )
    return ExecutorTerminalResult(
        "input_required",
        None,
        {
            "kind": "resource_limit_exhausted",
            "layer": "executor",
            "message": (
                f"{payload.get('executor') or 'executor'} resource "
                f"({payload.get('resource') or 'resource'}) limit exhausted"
            ),
            "retryable": False,
            "resource_exhaustion": payload,
        },
        resource_exhaustion=payload,
    )


def _resource_limits_equal(observed: int | float, expected: int | float) -> bool:
    if isinstance(observed, bool) or isinstance(expected, bool):
        return False
    try:
        left = float(observed)
        right = float(expected)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(left) or not math.isfinite(right):
        return False
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def detect_retryable_transport_failure(stdout: str, stderr: str = "") -> dict[str, Any] | None:
    prefixes = (
        "api call failed after",
        "connection error",
        "connection reset",
        "network is unreachable",
        "service unavailable",
        "rate limit exceeded",
        "quota exceeded",
    )
    for raw_line in f"{stdout}\n{stderr}".splitlines():
        line = raw_line.strip().lower()
        if line.startswith(prefixes) or "apiconnectionerror" in line:
            return {
                "kind": "executor_transport_failure",
                "layer": "transport",
                "message": raw_line.strip(),
                "retryable": True,
            }
    return None


def extract_callback_from_output(
    output: str,
    task_packet: dict[str, Any],
    executor_run_id: str,
    **_: Any,
) -> dict[str, Any] | None:
    """Compatibility wrapper. Invalid markers are intentionally never callbacks."""
    return extract_callback_validation_from_output(
        output, task_packet, executor_run_id
    ).callback


def extract_callback_from_events(
    events: list[dict[str, Any]],
    task_packet: dict[str, Any],
    executor_run_id: str,
    **_: Any,
) -> dict[str, Any] | None:
    """Compatibility wrapper. Invalid markers are intentionally never callbacks."""
    return extract_callback_validation_from_events(
        events, task_packet, executor_run_id
    ).callback


def strip_callback_line(text: str) -> str:
    cleaned = [
        line for line in text.splitlines()
        if not line.strip().startswith(FINAL_CALLBACK_PREFIX)
    ]
    return "\n".join(cleaned).strip()


def is_valid_agent_final_state(value: Any) -> bool:
    return isinstance(value, str) and value in AGENT_FINAL_STATES


def _invalid(code: str, message: str) -> CallbackValidation:
    return CallbackValidation(marker_seen=True, valid=False, code=code, message=message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
