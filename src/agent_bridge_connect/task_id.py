from __future__ import annotations

import re
import secrets
from collections.abc import Iterable


DEFAULT_TASK_CODE_LENGTH = 4
TASK_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
TASK_CODE_RE = re.compile(
    rf"^(?P<code>[{TASK_CODE_ALPHABET}]{{{DEFAULT_TASK_CODE_LENGTH},}})$",
    re.IGNORECASE,
)
TASK_ITERATION_RE = re.compile(
    rf"^(?P<code>[{TASK_CODE_ALPHABET}]{{{DEFAULT_TASK_CODE_LENGTH},}})-(?P<iteration>\d{{3}})$",
    re.IGNORECASE,
)
TASK_LIKE_ID_RE = re.compile(
    rf"^(?:[{TASK_CODE_ALPHABET}]{{{DEFAULT_TASK_CODE_LENGTH},}}(?:-\d{{3}})?|T-\d+(?:-[A-Za-z0-9]+)?)$",
    re.IGNORECASE,
)

# Backwards-compatible names for older internal imports/tests. New code should
# use task-code terminology.
DEFAULT_CHAIN_TOKEN_LENGTH = DEFAULT_TASK_CODE_LENGTH
CHAIN_TOKEN_ALPHABET = TASK_CODE_ALPHABET
TASK_ID_RE = TASK_ITERATION_RE


def format_task_id(task_code: str, iteration: int | str) -> str:
    return f"{normalize_task_code(task_code)}-{int(iteration):03d}"


def split_task_ref(task_ref: str) -> tuple[str, str | None]:
    value = str(task_ref).strip().upper()
    match = TASK_ITERATION_RE.fullmatch(value)
    if match:
        return match.group("code").upper(), match.group("iteration")
    match = TASK_CODE_RE.fullmatch(value)
    if match:
        return match.group("code").upper(), None
    raise ValueError(f"invalid AgentBC task id: {task_ref}")


def generate_task_code(length: int = DEFAULT_TASK_CODE_LENGTH) -> str:
    return "".join(secrets.choice(TASK_CODE_ALPHABET) for _ in range(length))


def allocate_task_code(existing_codes: Iterable[str], min_length: int = DEFAULT_TASK_CODE_LENGTH) -> str:
    existing = _valid_codes(existing_codes)
    length = token_length_for_available_capacity(existing, min_length=min_length)
    existing_at_length = {code for code in existing if len(code) == length}
    for _ in range(100):
        code = generate_task_code(length)
        if code not in existing_at_length:
            return code
    raise RuntimeError("could not allocate a unique AgentBC task code")


def token_capacity(length: int) -> int:
    return len(TASK_CODE_ALPHABET) ** length


def available_token_count(existing_codes: Iterable[str], length: int) -> int:
    used = {code for code in _valid_codes(existing_codes) if len(code) == length}
    return token_capacity(length) - len(used)


def token_length_for_available_capacity(
    existing_codes: Iterable[str],
    min_length: int = DEFAULT_TASK_CODE_LENGTH,
) -> int:
    existing = _valid_codes(existing_codes)
    length = max(int(min_length), 1)
    while available_token_count(existing, length) <= 0:
        length += 1
    return length


def normalize_task_code(code: str) -> str:
    normalized = str(code).strip().upper()
    if not normalized:
        raise ValueError("task code is required")
    if any(char not in TASK_CODE_ALPHABET for char in normalized):
        raise ValueError(f"invalid task code: {code}")
    return normalized


def task_iteration(task_id: str) -> int | None:
    match = TASK_ITERATION_RE.fullmatch(str(task_id).strip().upper())
    if match:
        return int(match.group("iteration"))
    return None


def is_task_like(value: str) -> bool:
    return bool(TASK_LIKE_ID_RE.fullmatch(str(value).strip()))


# Compatibility wrappers.
def allocate_chain_token(existing_tokens: Iterable[str], min_length: int = DEFAULT_TASK_CODE_LENGTH) -> str:
    return allocate_task_code(existing_tokens, min_length=min_length)


def generate_chain_token(length: int = DEFAULT_TASK_CODE_LENGTH) -> str:
    return generate_task_code(length)


def normalize_chain_token(token: str) -> str:
    return normalize_task_code(token)


def task_sequence(task_id: str) -> int | None:
    return task_iteration(task_id)


def _valid_codes(values: Iterable[str]) -> set[str]:
    codes: set[str] = set()
    for value in values:
        if not str(value).strip():
            continue
        try:
            code = normalize_task_code(str(value))
        except ValueError:
            continue
        codes.add(code)
    return codes
