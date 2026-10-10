"""Task step parsing and validation, independent of task lifecycle state."""
from __future__ import annotations

from typing import Any

from .protocol import ABCError, task_step_text


def validate_handoff_steps(steps: Any) -> list[dict[str, Any]]:
    """Strict new handoff input; existing stored tasks retain legacy semantics."""
    if not isinstance(steps, list) or not steps:
        raise ABCError("handoff_steps_invalid", "steps must be a non-empty list")
    result = []
    for index, step in enumerate(steps, 1):
        if (not isinstance(step, dict) or type(step.get("id")) is not int
                or step["id"] != index or not isinstance(step.get("description"), str)
                or not step["description"].strip()):
            raise ABCError("handoff_steps_invalid", "steps require consecutive integer IDs starting at 1 and non-empty descriptions")
        # User input cannot inject done status, receipts, or arbitrary record paths.
        if set(step) - {"id", "description", "record"}:
            raise ABCError("handoff_steps_invalid", "steps accept only id and description")
        if "record" in step and step["record"] != f"steps/{index:02d}.json":
            raise ABCError("handoff_steps_invalid", "step record must use its canonical path")
        result.append(_normalize_step({"id": index, "description": step["description"]}, index))
    return result


def _normalize_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ABCError("task_create_error", "each step must be a mapping")
    normalized = dict(step)
    description = task_step_text(normalized)
    if not description:
        raise ABCError(
            "task_create_error",
            f"step {index} must define a non-empty description or action",
        )
    # FLOW-104-001 / transport correction schema correction: canonical new tasks carry
    # integer step ids only.  A string id (e.g. ``"1"``) used to pass task
    # creation and then fail the matching final callback with
    # ``completion_marker_task_steps_invalid`` after the whole run had
    # executed.  The mismatch now fails BEFORE dispatch with a stable
    # ``task_create_error``; legacy tasks keep their stored ids (dual-read).
    declared_id = normalized.get("id")
    if declared_id is not None and (isinstance(declared_id, bool) or not isinstance(declared_id, int)):
        raise ABCError(
            "task_create_error",
            f"step {index} id must be an integer (got {type(declared_id).__name__}); "
            "string step ids are rejected before dispatch so the final "
            "callback can never diverge from the declared steps",
        )
    if declared_id is None:
        normalized["id"] = index
    normalized["description"] = description
    normalized.setdefault("record", f"steps/{index:02d}.json")
    return normalized


def _load_steps_text(text: str, *, structured_handoff: bool = False) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        yaml = None
    if yaml is not None:
        try:
            if structured_handoff:
                class UniqueStepLoader(yaml.SafeLoader):
                    pass

                def unique_mapping(loader, node):
                    result = {}
                    for key_node, value_node in node.value:
                        key = loader.construct_object(key_node)
                        if not isinstance(key, str) or key in result:
                            raise ABCError("handoff_steps_invalid", "Duplicate or non-text steps mapping key")
                        result[key] = loader.construct_object(value_node)
                    return result

                UniqueStepLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
                data = yaml.load(text, Loader=UniqueStepLoader)
            else:
                data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ABCError("task_create_error", "Invalid steps YAML") from exc
        yaml_steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(yaml_steps, list):
            raise ABCError("task_create_error", "steps file must contain a steps list")
        return validate_handoff_steps(yaml_steps) if structured_handoff else [_normalize_step(step, index) for index, step in enumerate(yaml_steps, 1)]

    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped == "steps:":
            index += 1
            continue
        if stripped.startswith("- "):
            if current is not None:
                steps.append(current)
            current = {}
            remainder = stripped[2:].strip()
            if remainder:
                if _is_block_scalar_key(remainder):
                    key = remainder.split(":", 1)[0].strip()
                    block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                    current[key] = block
                    continue
                _parse_step_key_value(current, remainder)
            index += 1
            continue
        if current is not None:
            if structured_handoff and stripped.split(":", 1)[0].strip() in current:
                raise ABCError("handoff_steps_invalid", "Duplicate steps mapping key")
            if _is_block_scalar_key(stripped):
                key = stripped.split(":", 1)[0].strip()
                block, index = _collect_block_scalar(lines, index + 1, _indent(raw_line))
                current[key] = block
                continue
            _parse_step_key_value(current, stripped)
        index += 1
    if current is not None:
        steps.append(current)
    if not steps:
        raise ABCError("task_create_error", "steps file must contain a steps list")
    return validate_handoff_steps(steps) if structured_handoff else steps


def _parse_step_key_value(step: dict[str, Any], text: str) -> None:
    if ":" not in text:
        return
    key, value = text.split(":", 1)
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    elif value.isdigit():
        step[key.strip()] = int(value)
        return
    step[key.strip()] = value


def _is_block_scalar_key(text: str) -> bool:
    if ":" not in text:
        return False
    _, value = text.split(":", 1)
    return value.strip() in {"|", "|-", "|+", ">", ">-", ">+"}


def _collect_block_scalar(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    collected: list[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        current_indent = _indent(raw)
        if stripped.startswith("- ") and current_indent <= parent_indent:
            break
        if stripped and current_indent <= parent_indent:
            break
        collected.append(stripped)
        index += 1
    return "\n".join(collected).strip(), index


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))
