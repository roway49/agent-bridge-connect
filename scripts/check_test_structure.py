#!/usr/bin/env python3
"""Enforce the public AgentBC test-suite maintenance contract."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


LAYERS = ("unit", "contracts", "integration", "release")
FORBIDDEN_NAME = re.compile(
    r"(?:phase\d|day\d|task\d|perm\d|flow\d|session_?\d|arch\d|proto\d|pkg\d|upd\d)",
    re.IGNORECASE,
)
SUBPROCESS_CALLS = {"run", "Popen", "check_call", "check_output", "call"}
SLEEP_CALLS = {"time.sleep", "asyncio.sleep"}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _decorator_name(node: ast.AST) -> str:
    return _call_name(node.func if isinstance(node, ast.Call) else node)


def _string_literals(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def check_file(path: Path, *, max_lines: int) -> list[str]:
    failures: list[str] = []
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    if len(lines) > max_lines:
        failures.append(f"{path}: {len(lines)} lines exceeds {max_lines}")
    if FORBIDDEN_NAME.search(path.stem):
        failures.append(f"{path}: historical phase/day/ticket naming is forbidden")

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: cannot parse: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if _decorator_name(decorator) in {"skip", "unittest.skip"}:
                    failures.append(
                        f"{path}:{decorator.lineno}: permanent unittest.skip is forbidden"
                    )
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name.split(".")[-1] in SUBPROCESS_CALLS and "subprocess" in name:
            if any(value == "python3" for value in _string_literals(node)):
                failures.append(
                    f"{path}:{node.lineno}: use sys.executable, not bare python3"
                )
        if path.parent.name in {"unit", "contracts"} and name in SLEEP_CALLS:
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, (int, float)) and value > 0.05:
                    failures.append(
                        f"{path}:{node.lineno}: unit/contract sleep {value}s exceeds 0.05s"
                    )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests-root", type=Path, default=Path("tests"))
    # Capacity budget, not a correctness bound.  It stays at the original 75:
    # the OpenCode cases live in their responsibility-matching modules rather
    # than in extra top-level files, so no budget exception is needed.
    parser.add_argument("--max-files", type=int, default=75)
    parser.add_argument("--max-lines", type=int, default=45_000)
    parser.add_argument("--max-file-lines", type=int, default=1_000)
    args = parser.parse_args(argv)

    root = args.tests_root
    failures: list[str] = []
    files: list[Path] = []
    top_level = sorted(root.glob("test_*.py"))
    if top_level:
        failures.append("top-level test modules are forbidden: " + ", ".join(map(str, top_level)))
    for layer in LAYERS:
        layer_root = root / layer
        layer_files = sorted(layer_root.glob("test_*.py"))
        if not (layer_root / "__init__.py").is_file() or not layer_files:
            failures.append(f"{layer_root}: layer must be a discoverable package with tests")
        files.extend(layer_files)

    if len(files) > args.max_files:
        failures.append(f"formal test files: {len(files)} exceeds {args.max_files}")
    total_lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    if total_lines > args.max_lines:
        failures.append(f"formal test lines: {total_lines} exceeds {args.max_lines}")
    for path in files:
        failures.extend(check_file(path, max_lines=args.max_file_lines))

    if failures:
        print("test structure check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(
        "test structure: ok "
        f"({len(files)} files, {total_lines} lines, layers={','.join(LAYERS)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
