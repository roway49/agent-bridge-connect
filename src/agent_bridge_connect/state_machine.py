from __future__ import annotations

from .protocol import ABCError, STATES, TRANSITIONS


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, [])


def validate_transition(from_state: str, to_state: str) -> None:
    if from_state not in STATES:
        raise ABCError("unknown_state", f"Unknown source state: {from_state}", {"state": from_state})
    if to_state not in STATES:
        raise ABCError("unknown_state", f"Unknown target state: {to_state}", {"state": to_state})
    if not can_transition(from_state, to_state):
        raise ABCError(
            "invalid_transition",
            f"Invalid state transition: {from_state} -> {to_state}",
            {"from_state": from_state, "to_state": to_state},
        )


def get_valid_transitions(state: str) -> list[str]:
    if state not in STATES:
        raise ABCError("unknown_state", f"Unknown state: {state}", {"state": state})
    return list(TRANSITIONS[state])
