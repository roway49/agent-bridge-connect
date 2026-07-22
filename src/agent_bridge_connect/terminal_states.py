from __future__ import annotations


TASK_TERMINAL_STATES = frozenset({"completed", "needs_recovery", "failed"})


def terminal_status_label(status: object) -> str:
    value = str(status or "").strip()
    return value if value in TASK_TERMINAL_STATES else ""

