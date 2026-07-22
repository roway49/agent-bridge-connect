from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


FINAL_CALLBACK_PREFIX = "AGENTBC_FINAL_CALLBACK:"
AGENT_FINAL_STATES = frozenset({"completed", "input_required", "cancelled"})


def build_agent_callback(
    task_packet: dict[str, Any],
    final_state: str,
    summary: str,
    executor_run_id: str,
    *,
    finished_at: str | None = None,
    callback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    payload = dict(callback or {})
    payload.setdefault("task_id", str(task_packet.get("task_id") or ""))
    payload.setdefault("final_state", final_state)
    payload.setdefault("report_file", str(workspace.get("report_file") or ""))
    payload.setdefault("artifacts_dir", str(workspace.get("artifacts_dir") or ""))
    payload.setdefault("summary", summary.strip())
    payload["executor_run_id"] = executor_run_id
    payload.setdefault("finished_at", finished_at or _utc_now())
    return payload


def extract_callback_from_output(
    output: str,
    task_packet: dict[str, Any],
    executor_run_id: str,
    *,
    final_state: str = "completed",
    summary_fallback: str = "",
) -> dict[str, Any] | None:
    callback = _parse_callback_payload(output)
    if callback is None:
        return None
    return build_agent_callback(
        task_packet,
        str(callback.get("final_state") or final_state),
        str(callback.get("summary") or summary_fallback),
        executor_run_id,
        callback=callback,
    )


def extract_callback_from_events(
    events: list[dict[str, Any]],
    task_packet: dict[str, Any],
    executor_run_id: str,
    *,
    final_state: str = "completed",
    summary_fallback: str = "",
) -> dict[str, Any] | None:
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
    for text in reversed(texts):
        callback = extract_callback_from_output(
            text,
            task_packet,
            executor_run_id,
            final_state=final_state,
            summary_fallback=summary_fallback,
        )
        if callback is not None:
            return callback
    return None


def strip_callback_line(text: str) -> str:
    cleaned = [
        line for line in text.splitlines()
        if not line.strip().startswith(FINAL_CALLBACK_PREFIX)
    ]
    return "\n".join(cleaned).strip()


def is_valid_agent_final_state(value: Any) -> bool:
    return isinstance(value, str) and value in AGENT_FINAL_STATES


def _parse_callback_payload(output: str) -> dict[str, Any] | None:
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line.startswith(FINAL_CALLBACK_PREFIX):
            continue
        payload = line.removeprefix(FINAL_CALLBACK_PREFIX).strip()
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
