#!/usr/bin/env python3
"""Fail closed when a public release candidate contains internal material."""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELF = "scripts/check_public_release.py"

FORBIDDEN_PATHS = (
    "AGENTBC_*_DEVELOPMENT_CHECKLIST.md",
    "AGENTBC_*_DEVELOPMENT_HANDBOOK.md",
    "AGENTBC_DUAL_MACHINE_GIT_WORKFLOW.md",
    "*_EVIDENCE.md",
    "*_IMPLEMENTATION_NOTES.md",
    "*-baseline.md",
    "scripts/live_probe_*.py",
    "scripts/*canary*.sh",
    "tests/REGRESSION_MIGRATION.md",
    "tests/fixtures/executor_runtime/matrix/README.md",
    "tests/**/live_probe*/**",
    "tests/**/*probe_evidence*",
)

FORBIDDEN_CONTENT = (
    ("private_user_path", re.compile(r"/Users/(?:wangroway|rowaywang)(?:/|$)")),
    ("private_workspace", re.compile(r"AgentBC_Temp")),
    ("private_branch", re.compile(r"private/integration")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_fine_grained_token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("api_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


def tracked_files(revision: str | None = None) -> list[str]:
    command = ["git", "ls-files", "-z"]
    if revision:
        command = ["git", "ls-tree", "-r", "-z", "--name-only", revision]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def payload_for(relative: str, revision: str | None = None) -> bytes:
    if revision is None:
        return (ROOT / relative).read_bytes()
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    problems: list[str] = []
    for relative in tracked_files(args.revision):
        if any(fnmatch.fnmatch(relative, pattern) for pattern in FORBIDDEN_PATHS):
            problems.append(f"forbidden public path: {relative}")
            continue
        if relative == SELF:
            continue
        payload = payload_for(relative, args.revision)
        if b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        for label, pattern in FORBIDDEN_CONTENT:
            if pattern.search(text):
                problems.append(f"forbidden {label}: {relative}")
    if problems:
        print("public release boundary failed:")
        for problem in sorted(problems):
            print(f"- {problem}")
        return 1
    source = f"revision {args.revision}" if args.revision else "index"
    print(f"public release boundary ({source}): ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
