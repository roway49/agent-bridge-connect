#!/usr/bin/env python3
"""Enforce the immutable repository-root file set.

Root directories may add or remove descendants. Root-level files and symlinks
must exactly match ``.github/repository-root-files.txt``. The manifest and this
checker are CODEOWNED so a structural exception requires owner review in a PR.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github" / "repository-root-files.txt"


def expected_root_files() -> set[str]:
    return {
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def working_tree_root_files() -> set[str]:
    return {
        child.name
        for child in ROOT.iterdir()
        if child.name != ".git" and (child.is_file() or child.is_symlink())
    }


def index_root_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: set[str] = set()
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        _, raw_path = item.split(b"\t", 1)
        path = raw_path.decode("utf-8")
        if "/" not in path:
            paths.add(path)
    return paths


def revision_root_files(revision: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-z", revision],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: set[str] = set()
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        _mode, object_type, _object_id = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if object_type != "tree" and "/" not in path:
            paths.add(path)
    return paths


def validate(actual: set[str], *, source: str) -> int:
    expected = expected_root_files()
    added = sorted(actual - expected)
    deleted = sorted(expected - actual)
    if not added and not deleted:
        print(f"repository root boundary ({source}): ok")
        return 0
    print(f"repository root boundary ({source}) failed:")
    for path in added:
        print(f"- unauthorized root file added: {path}")
    for path in deleted:
        print(f"- protected root file deleted: {path}")
    print("Root structural changes require owner review and a manifest update via PR.")
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=("working-tree", "index", "revision"),
        default="revision",
    )
    parser.add_argument("--revision", default="HEAD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source == "working-tree":
        actual = working_tree_root_files()
        source = "working tree"
    elif args.source == "index":
        actual = index_root_files()
        source = "index"
    else:
        actual = revision_root_files(args.revision)
        source = f"revision {args.revision}"
    return validate(actual, source=source)


if __name__ == "__main__":
    raise SystemExit(main())
