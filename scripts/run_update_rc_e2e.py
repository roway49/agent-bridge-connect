#!/usr/bin/env python3
"""Isolated two-version Update RC E2E driver for UPD-103-001.

This driver builds two deterministic test packages (1.0.2a1 and 1.0.3a1)
from explicit source inputs, serves a verified local HTTPS RC feed, and then
exercises the real ``agentbc update`` subprocess inside a freshly created
temporary HOME / install / bin / config / workspace / spool hierarchy.  It
never discovers or writes the real user installation, the default Runner
spool, or the user task board.

The driver accepts a clean update only when the CLI, Runner, and all managed
Skills advance together.  Fault scenarios accept only an exact rollback to
the old CLI, Runner, Skill bytes, configuration, and stable user data.

Scenarios
---------
- ``success``            clean target wheel; contract is 1.0.2a1 -> 1.0.3a1
                         with matching CLI / Runner / installed Skills.
- ``setup_refresh``      fault wheel makes the new CLI's ``setup --update``
                         exit non-zero; the update flow must roll back.
- ``runner_start``       fault wheel makes the new CLI's ``runner start``
                         exit non-zero; the update flow must roll back.
- ``post_identity``      clean target wheel used by compatibility tests that
                         expect a post-update identity failure and rollback.

Every fault contract requires the exact pre-update CLI link, the managed
Skill bytes/manifests, the old Runner identity, the config, and the stable
data to be restored with no second Runner.  The driver reports each clause
independently and never masks a failing clause.

Isolation and safety
--------------------
- The driver refuses non-temporary roots: every managed path must live under
  a fresh root below the system temp dir whose name starts with
  ``agentbc-update-rc-e2e-``.
- Explicit source/wheel inputs and explicit versions are required.
- ``--plan`` prints a redacted plan and exits without touching the filesystem.
- The public summary redacts the temporary root, the isolated HOME, and any
  token-like secret values passed by the caller.
- No test CA, feed, wheel, fault package, or temporary version ever enters
  formal release assets: all of them are created and consumed under the
  ephemeral isolation root.

Real two-version run (opt-in)
-----------------------------
The slow real run is gated behind an explicit environment gate.  Exact
invocation::

    AGENTBC_E2E_RUN_REAL=1 python3.12 scripts/run_update_rc_e2e.py \\
        --scenario post_identity \\
        --old-src /path/to/agentbc-checkout \\
        --new-src /path/to/agentbc-checkout \\
        --old-version 1.0.2a1 --new-version 1.0.3a1

Pass ``--plan`` to preview the same plan without executing.  Pre-built wheels
can be supplied with ``--old-wheel`` / ``--new-wheel`` instead of source
trees.  ``--keep`` preserves the isolation root for inspection (the Runner is
still stopped).  ``--out-evidence FILE.json`` writes the machine-readable
evidence; without it the evidence is printed to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
TASK_ID = "UPD-103-001"

GATE_ENV = "AGENTBC_E2E_RUN_REAL"
FAULT_ENV = "AGENTBC_E2E_FAULT"

DEFAULT_OLD_VERSION = "1.0.2a1"
DEFAULT_NEW_VERSION = "1.0.3a1"
OLD_TAG = "v1.0.2A"
NEW_TAG = "v1.0.3A"

SCENARIOS = ("success", "setup_refresh", "runner_start", "post_identity")
PLATFORMS = ("codex", "claude", "hermes")

TEMP_PREFIX = "agentbc-update-rc-e2e-"
_WHEEL_FILENAME = "agentbc-{version}-py3-none-any.whl"

# Sub-directories created under the isolation root.
_HOME_DIR = "home"
_INSTALL_DIR = "install"
_BIN_DIR = "bin"
_CONFIG_DIR = "config"
_WORKSPACE_DIR = "workspace"
_SPOOL_DIR = "spool"
_FEED_DIR = "feed"
_BUILD_DIR = "build"
_CERT_DIR = "certs"
_TMP_DIR = "tmp"

_PACKAGE_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?$"
)
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")

# Well-known real user locations that must never be accepted as an isolation
# root, even when a path segment happens to match.
_REAL_ROOT_MARKERS = (
    "Documents/AgentBC/workspace",
    ".agentbc-alpha",
    ".local/bin",
    "Library/LaunchAgents",
    "Library/Application Support",
    ".abc",
)


class IsolationError(RuntimeError):
    """The driver refused to operate on a non-temporary or real user root."""


class GateNotEnabled(RuntimeError):
    """The real two-version run requires the explicit ``AGENTBC_E2E_RUN_REAL`` gate."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------


def version_key(version: str) -> tuple[int, ...]:
    """Sortable package-version key mirroring ``update._version_key``."""
    matched = _PACKAGE_VERSION_RE.fullmatch(version)
    if matched is None:
        raise ValueError(f"unsupported package version: {version!r}")
    pre = matched.group("pre")
    pre_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[pre]
    return (
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
        pre_rank,
        int(matched.group("pre_n") or 0),
    )


def is_version_valid(version: str) -> bool:
    try:
        version_key(version)
        return True
    except ValueError:
        return False


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def hash_equal(left: str, right: str) -> bool:
    """Case-insensitive SHA-256 comparison that refuses malformed digests."""
    if not is_sha256(left) or not is_sha256(right):
        return False
    return left.lower() == right.lower()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_sha256(root: Path) -> str:
    """Deterministic hash over every regular file/symlink under ``root``.

    Symlink targets are recorded without following them so an isolated
    installation can be snapshotted without walking outside the root.
    """
    return dir_sha256_excluding(root, frozenset())


def dir_sha256_excluding(root: Path, excluded_names: Iterable[str]) -> str:
    """Hash a tree while skipping any path segment in ``excluded_names``.

    ``record`` (the board) and ``.agentbc-cutover-ready`` (the durable
    cutover stamp) are excluded when measuring stable user data so an update
    that records a stamp is not mistaken for data loss.
    """
    excluded = frozenset(excluded_names)
    digest = hashlib.sha256()
    root = root.expanduser()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts):
            continue
        relative_text = relative.as_posix()
        if path.is_symlink():
            digest.update(f"L\0{relative_text}\0{os.readlink(path)}\0".encode("utf-8"))
        elif path.is_file():
            digest.update(f"F\0{relative_text}\0".encode("utf-8"))
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False


def assert_temporary_root(root: Path) -> None:
    """Refuse any isolation root that is not a fresh temp directory."""
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise IsolationError(f"isolation root must be an existing directory: {resolved}")
    system_temp = Path(tempfile.gettempdir()).resolve()
    if not is_under(resolved, system_temp):
        raise IsolationError(
            f"refusing non-temporary isolation root: {resolved} "
            f"(must live under the system temp dir {system_temp})"
        )
    if not resolved.name.startswith(TEMP_PREFIX):
        raise IsolationError(
            f"refusing isolation root not created by this driver: {resolved} "
            f"(name must start with {TEMP_PREFIX!r})"
        )
    for marker in _REAL_ROOT_MARKERS:
        if marker in resolved.parts:
            raise IsolationError(
                f"refusing isolation root containing a real user location "
                f"({marker!r}): {resolved}"
            )


def assert_isolated_paths(root: Path, paths: Iterable[Path | str]) -> None:
    """Refuse any managed path that escapes the temporary isolation root."""
    resolved_root = Path(root).expanduser().resolve()
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not is_under(path, resolved_root):
            raise IsolationError(f"refusing non-temporary managed path: {path}")


def redact_text(
    text: str,
    *,
    root: Path,
    home: Path,
    secrets: Iterable[str] = (),
) -> str:
    """Replace private paths and token-like values with stable placeholders."""
    redacted = str(text)
    root_raw = str(Path(root).expanduser())
    root_resolved = str(Path(root).expanduser().resolve())
    home_raw = str(Path(home).expanduser())
    home_resolved = str(Path(home).expanduser().resolve())
    ordered: list[tuple[str, str]] = [
        (root_raw, "<TMPROOT>"),
        (root_resolved, "<TMPROOT>"),
        (home_raw, "<ISOLATED_HOME>"),
        (home_resolved, "<ISOLATED_HOME>"),
        (home_raw + "/", "<ISOLATED_HOME>/"),
        (home_resolved + "/", "<ISOLATED_HOME>/"),
    ]
    for secret in secrets:
        if secret:
            ordered.append((str(secret), "<REDACTED>"))
    ordered.sort(key=lambda pair: len(pair[0]), reverse=True)
    for value, placeholder in ordered:
        redacted = redacted.replace(value, placeholder)
    return redacted


def excerpt(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def fault_env_for(scenario: str) -> dict[str, str]:
    """Return the environment override that selects a fault scenario.

    ``success`` and ``post_identity`` use a clean target wheel and therefore
    inject no fault; ``setup_refresh`` and ``runner_start`` use a fault wheel
    that fails the matching subcommand of the new CLI.
    """
    if scenario in {"setup_refresh", "runner_start"}:
        return {FAULT_ENV: scenario}
    return {}


def fault_command_for(scenario: str) -> str:
    """Return the new-CLI subcommand the injected fault makes fail."""
    return {"setup_refresh": "setup", "runner_start": "runner"}.get(scenario, "")


def runner_pids_for_spool(ps_lines: Iterable[str], spool: Path) -> list[int]:
    """Filter ``ps -axo pid=,command=`` lines for one isolated spool path."""
    candidates = {
        str(Path(spool).expanduser()),
        str(Path(spool).expanduser().resolve()),
    }
    pids: list[int] = []
    for raw in ps_lines:
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        if any(needle in command for needle in candidates) and "runner" in command and "serve" in command:
            pids.append(pid)
    return sorted(set(pids))


def fault_patch_source() -> str:
    """Return the deterministic ``cli.main`` fault gate injected into fault wheels."""
    return (
        "    import os as _e2e_os\n"
        "    _e2e_fault = _e2e_os.environ.get('AGENTBC_E2E_FAULT', '')\n"
        "    _e2e_map = {'setup_refresh': 'setup', 'runner_start': 'runner'}\n"
        "    if _e2e_fault in _e2e_map and len(sys.argv) > 1 and sys.argv[1] == _e2e_map[_e2e_fault]:\n"
        "        print(f'fault_injected: {_e2e_fault}', file=sys.stderr)\n"
        "        return 3\n"
    )


# ---------------------------------------------------------------------------
# Evidence schema validation
# ---------------------------------------------------------------------------

_SOURCE_REQUIRED = ("old", "new", "commit_sha", "source_tree_sha256")
_PACKAGE_REQUIRED = ("path", "sha256", "size")
_ARTIFACT_KEYS = ("old_wheel", "new_wheel", "feed_manifest", "release_index", "ca_cert")
_CLI_LINK_KEYS = ("target", "readlink_before", "readlink_after", "restored")
_SKILL_PLATFORM_KEYS = (
    "root",
    "manifest_sha256_before",
    "manifest_sha256_after",
    "files_sha256_before",
    "files_sha256_after",
)
_RUNNER_KEYS = (
    "identity_before",
    "identity_after",
    "status_before",
    "status_after",
    "pid_before",
    "pid_after",
    "spool",
    "single_runner",
)
_STABLE_KEYS = (
    "config_sha256_before",
    "config_sha256_after",
    "workspace_sha256_before",
    "workspace_sha256_after",
    "board_sha256_before",
    "board_sha256_after",
    "workspace_data_sha256_before",
    "workspace_data_sha256_after",
    "board_data_sha256_before",
    "board_data_sha256_after",
)
_OUTCOME_KEYS = (
    "expected",
    "actual",
    "update_exit_code",
    "known_pre_fix_failure",
    "reason",
    "rollback_complete",
)
_DIAGNOSIS_KEYS = ("new_package_version", "diagnosed", "mismatched")
_TOP_LEVEL_KEYS = (
    "schema_version",
    "task_id",
    "scenario",
    "source",
    "artifacts",
    "commands",
    "cli_link",
    "skills",
    "runner",
    "stable_data",
    "outcome",
    "diagnosis",
    "contract",
)


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    """Return a list of schema violations for a machine-readable evidence doc."""
    errors: list[str] = []
    for key in _TOP_LEVEL_KEYS:
        if key not in evidence:
            errors.append(f"missing top-level key: {key}")
    if evidence.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if evidence.get("task_id") != TASK_ID:
        errors.append(f"task_id must be {TASK_ID}")
    scenario = evidence.get("scenario")
    if scenario not in SCENARIOS:
        errors.append(f"scenario must be one of {SCENARIOS}")

    source = evidence.get("source")
    if isinstance(source, dict):
        for key in _SOURCE_REQUIRED:
            if key not in source:
                errors.append(f"missing source key: {key}")
        if not isinstance(source.get("old"), dict) or not isinstance(source.get("new"), dict):
            errors.append("source.old and source.new must be objects")
    else:
        errors.append("source must be an object")

    artifacts = evidence.get("artifacts")
    if isinstance(artifacts, dict):
        for key in _ARTIFACT_KEYS:
            item = artifacts.get(key)
            if not isinstance(item, dict):
                errors.append(f"missing artifacts key: {key}")
                continue
            for field in _PACKAGE_REQUIRED:
                if field not in item:
                    errors.append(f"missing artifacts.{key}.{field}")
            if not is_sha256(item.get("sha256")):
                errors.append(f"artifacts.{key}.sha256 must be a sha256 hex digest")
    else:
        errors.append("artifacts must be an object")

    if not isinstance(evidence.get("commands"), list):
        errors.append("commands must be a list")
    else:
        for index, command in enumerate(evidence["commands"]):
            if not isinstance(command, dict) or "argv" not in command or "exit_code" not in command:
                errors.append(f"commands[{index}] must contain argv and exit_code")

    cli_link = evidence.get("cli_link")
    if isinstance(cli_link, dict):
        for key in _CLI_LINK_KEYS:
            if key not in cli_link:
                errors.append(f"missing cli_link key: {key}")
    else:
        errors.append("cli_link must be an object")

    skills = evidence.get("skills")
    if isinstance(skills, dict):
        for platform in PLATFORMS:
            entry = skills.get(platform)
            if not isinstance(entry, dict):
                errors.append(f"missing skills key: {platform}")
                continue
            for key in _SKILL_PLATFORM_KEYS:
                if key not in entry:
                    errors.append(f"missing skills.{platform}.{key}")
    else:
        errors.append("skills must be an object")

    runner = evidence.get("runner")
    if isinstance(runner, dict):
        for key in _RUNNER_KEYS:
            if key not in runner:
                errors.append(f"missing runner key: {key}")
    else:
        errors.append("runner must be an object")

    stable_data = evidence.get("stable_data")
    if isinstance(stable_data, dict):
        for key in _STABLE_KEYS:
            if key not in stable_data:
                errors.append(f"missing stable_data key: {key}")
    else:
        errors.append("stable_data must be an object")

    outcome = evidence.get("outcome")
    if isinstance(outcome, dict):
        for key in _OUTCOME_KEYS:
            if key not in outcome:
                errors.append(f"missing outcome key: {key}")
    else:
        errors.append("outcome must be an object")

    diagnosis = evidence.get("diagnosis")
    if isinstance(diagnosis, dict):
        for key in _DIAGNOSIS_KEYS:
            if key not in diagnosis:
                errors.append(f"missing diagnosis key: {key}")
    else:
        errors.append("diagnosis must be an object")
    return errors


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    scenario: str
    old_version: str
    new_version: str
    root: Path
    home: Path
    install_root: Path
    bin_dir: Path
    config_path: Path
    workspace_root: Path
    board_root: Path
    spool_root: Path
    state_root: Path
    feed_dir: Path
    build_dir: Path
    cert_dir: Path
    tmp_dir: Path
    old_src: Path | None = None
    new_src: Path | None = None
    old_wheel: Path | None = None
    new_wheel: Path | None = None
    python: Path = field(default_factory=lambda: Path(sys.executable))
    keep: bool = False
    out_evidence: Path | None = None

    @classmethod
    def derive(
        cls,
        *,
        root: Path,
        scenario: str,
        old_version: str,
        new_version: str,
        old_src: Path | None = None,
        new_src: Path | None = None,
        old_wheel: Path | None = None,
        new_wheel: Path | None = None,
        python: Path | None = None,
        keep: bool = False,
        out_evidence: Path | None = None,
    ) -> "Plan":
        resolved_root = Path(root).expanduser().resolve()
        home = resolved_root / _HOME_DIR
        install_root = resolved_root / _INSTALL_DIR
        bin_dir = resolved_root / _BIN_DIR
        workspace_root = resolved_root / _WORKSPACE_DIR
        return cls(
            scenario=scenario,
            old_version=old_version,
            new_version=new_version,
            root=resolved_root,
            home=home,
            install_root=install_root,
            bin_dir=bin_dir,
            config_path=resolved_root / _CONFIG_DIR / "config.toml",
            workspace_root=workspace_root,
            board_root=workspace_root / "record",
            spool_root=resolved_root / _SPOOL_DIR,
            state_root=home / ".abc" / "runner",
            feed_dir=resolved_root / _FEED_DIR,
            build_dir=resolved_root / _BUILD_DIR,
            cert_dir=resolved_root / _CERT_DIR,
            tmp_dir=resolved_root / _TMP_DIR,
            old_src=Path(old_src).expanduser() if old_src else None,
            new_src=Path(new_src).expanduser() if new_src else None,
            old_wheel=Path(old_wheel).expanduser() if old_wheel else None,
            new_wheel=Path(new_wheel).expanduser() if new_wheel else None,
            python=Path(python).expanduser() if python else Path(sys.executable),
            keep=keep,
            out_evidence=Path(out_evidence).expanduser() if out_evidence else None,
        )


def validate_plan(plan: Plan) -> list[str]:
    """Validate a fully derived plan without touching the filesystem."""
    errors: list[str] = []
    if plan.scenario not in SCENARIOS:
        errors.append(f"scenario must be one of {SCENARIOS}")
    for label, version in (("old", plan.old_version), ("new", plan.new_version)):
        if not is_version_valid(version):
            errors.append(f"{label} version is not a supported package version: {version!r}")
    if version_key(plan.new_version) <= version_key(plan.old_version):
        errors.append("new version must sort strictly after the old version")
    if (plan.old_src is None) == (plan.old_wheel is None):
        errors.append("provide exactly one of --old-src or --old-wheel")
    if (plan.new_src is None) == (plan.new_wheel is None):
        errors.append("provide exactly one of --new-src or --new-wheel")
    if plan.old_src is not None and not plan.old_src.is_dir():
        errors.append(f"old source is not a directory: {plan.old_src}")
    if plan.new_src is not None and not plan.new_src.is_dir():
        errors.append(f"new source is not a directory: {plan.new_src}")
    if plan.old_wheel is not None and not plan.old_wheel.is_file():
        errors.append(f"old wheel is not a file: {plan.old_wheel}")
    if plan.new_wheel is not None and not plan.new_wheel.is_file():
        errors.append(f"new wheel is not a file: {plan.new_wheel}")
    return errors


def build_plan(args: argparse.Namespace) -> Plan:
    """Create the fresh isolation root and derive every managed path from it."""
    root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    plan = Plan.derive(
        root=root,
        scenario=args.scenario,
        old_version=args.old_version,
        new_version=args.new_version,
        old_src=args.old_src,
        new_src=args.new_src,
        old_wheel=args.old_wheel,
        new_wheel=args.new_wheel,
        python=args.python,
        keep=args.keep,
        out_evidence=args.out_evidence,
    )
    errors = validate_plan(plan)
    if errors:
        shutil.rmtree(plan.root, ignore_errors=True)
        raise IsolationError("; ".join(errors))
    return plan


def plan_text(plan: Plan) -> str:
    """Human-readable, redacted plan used by ``--plan`` and the summary."""
    lines = [
        "AgentBC Update RC E2E plan",
        f"  scenario:            {plan.scenario}",
        f"  old version:         {plan.old_version} -> new version: {plan.new_version}",
        f"  old source/wheel:    {plan.old_src or plan.old_wheel or '-'}",
        f"  new source/wheel:    {plan.new_src or plan.new_wheel or '-'}",
        "  isolation root:      <TMPROOT>",
        f"  isolated HOME:       <TMPROOT>/{_HOME_DIR}",
        f"  install root:        <TMPROOT>/{_INSTALL_DIR}",
        f"  bin dir:             <TMPROOT>/{_BIN_DIR}",
        f"  config:              <TMPROOT>/{_CONFIG_DIR}/config.toml",
        f"  workspace/board:     <TMPROOT>/{_WORKSPACE_DIR}/record",
        f"  runner spool:        <TMPROOT>/{_SPOOL_DIR}",
        f"  fault injection:     {fault_command_for(plan.scenario) or 'none'}",
        f"  keep root:           {plan.keep}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command construction (unit-tested)
# ---------------------------------------------------------------------------


def build_update_argv(plan: Plan) -> list[str]:
    """The real ``agentbc update`` command with explicit isolated roots."""
    return [
        str(plan.bin_dir / "agentbc"),
        "update",
        "--root",
        str(plan.board_root),
        "--config",
        str(plan.config_path),
    ]


def build_doctor_argv(cli: Path) -> list[str]:
    return [str(cli), "doctor", "--json"]


def build_setup_argv(cli: Path) -> list[str]:
    return [str(cli), "setup", "--non-interactive"]


def build_runner_stop_argv(plan: Plan) -> list[str]:
    """Stop only the isolated spool's Runner, never the default spool."""
    return [str(plan.bin_dir / "agentbc"), "runner", "stop", "--spool", str(plan.spool_root)]


def build_runner_status_argv(plan: Plan) -> list[str]:
    return [str(plan.bin_dir / "agentbc"), "runner", "status", "--spool", str(plan.spool_root)]


def build_env(plan: Plan, *, fault: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal, fully isolated environment for every AgentBC subprocess."""
    environment: dict[str, str] = {
        "HOME": str(plan.home),
        "PATH": f"{plan.bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin{os.pathsep}/usr/sbin{os.pathsep}/sbin",
        "TMPDIR": str(plan.tmp_dir),
        "AGENTBC_CONFIG_PATH": str(plan.config_path),
        "AGENTBC_ALPHA_HOME": str(plan.install_root),
        "AGENTBC_BIN_DIR": str(plan.bin_dir),
        "AGENTBC_RUNNER_SPOOL": str(plan.spool_root),
        "AGENTBC_CODEX_BIN": str(plan.bin_dir / "codex"),
        "AGENTBC_CLAUDE_BIN": str(plan.bin_dir / "claude"),
        "AGENTBC_HERMES_BIN": str(plan.bin_dir / "hermes"),
        "SSL_CERT_FILE": str(plan.cert_dir / "ca.pem"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if fault:
        environment[FAULT_ENV] = fault
    if extra:
        environment.update(extra)
    return environment


# ---------------------------------------------------------------------------
# Isolated hierarchy
# ---------------------------------------------------------------------------


def prepare_hierarchy(plan: Plan) -> None:
    """Create every managed directory under the temporary isolation root."""
    for path in (
        plan.root,
        plan.home,
        plan.install_root,
        plan.bin_dir,
        plan.config_path.parent,
        plan.workspace_root,
        plan.spool_root,
        plan.state_root,
        plan.feed_dir,
        plan.build_dir,
        plan.cert_dir,
        plan.tmp_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    assert_isolated_paths(
        plan.root,
        (
            plan.home,
            plan.install_root,
            plan.bin_dir,
            plan.config_path,
            plan.workspace_root,
            plan.spool_root,
            plan.state_root,
            plan.feed_dir,
            plan.build_dir,
            plan.cert_dir,
        ),
    )


def _stub_script(kind: str) -> str:
    if kind == "codex":
        return (
            "#!/bin/sh\n"
            "# isolated AgentBC RC E2E stub; never reaches the real Codex CLI\n"
            "case \"$1\" in\n"
            "  --version) echo 'codex 0.44.0'; exit 0 ;;\n"
            "  --help) echo 'codex --json --sandbox --model --cd read-only workspace-write danger-full-access'; exit 0 ;;\n"
            "  exec) echo 'codex exec --json'; exit 0 ;;\n"
            "  login) exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
    if kind == "claude":
        return (
            "#!/bin/sh\n"
            "# isolated AgentBC RC E2E stub; never reaches the real Claude Code\n"
            "case \"$1\" in\n"
            "  --version) echo 'claude 1.0.0'; exit 0 ;;\n"
            "  --help) echo 'claude -p, --print --safe-mode --dangerously-skip-permissions --output-format'; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
    # hermes
    return (
        "#!/bin/sh\n"
        "# isolated AgentBC RC E2E stub; never reaches the real Hermes runtime\n"
        "case \"$1\" in\n"
        "  --version) echo 'hermes 0.9.0'; exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )


def write_stub_binaries(plan: Plan) -> None:
    """Install deterministic codex/claude/hermes stubs into the isolated bin dir."""
    for kind in ("codex", "claude", "hermes"):
        path = plan.bin_dir / kind
        path.write_text(_stub_script(kind), encoding="utf-8")
        path.chmod(0o755)


# ---------------------------------------------------------------------------
# Test package building and RC feed
# ---------------------------------------------------------------------------


def git_identity(source: Path) -> tuple[str, str]:
    """Return (commit_sha, source_tree_sha256) for an explicit source input.

    Falls back to a deterministic zero digest when the source tree is not a
    git checkout so the driver still emits a stable source identity.
    """
    source = Path(source).expanduser().resolve()
    commit = ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if completed.returncode == 0:
            commit = completed.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        commit = "0" * 40
    tree = _source_tree_sha256(source)
    return commit, tree


def _source_tree_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-z"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        if completed.returncode != 0:
            raise OSError("git ls-files failed")
        files = sorted(p for p in completed.stdout.decode(errors="replace").split("\0") if p)
        for relative in files:
            path = source / relative
            if not path.is_file():
                continue
            digest.update(f"{relative}\0{sha256_file(path)}\n".encode("utf-8"))
    except (OSError, subprocess.SubprocessError):
        if not source.is_dir():
            return digest.hexdigest()
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                digest.update(f"{relative}\0{sha256_file(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def copy_source_tree(source: Path, destination: Path) -> None:
    """Copy an explicit source input without build noise or a .git directory."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    ignores = shutil.ignore_patterns(
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "build",
        "dist",
        "*.egg-info",
        "__pycache__",
        "*.pyc",
        ".mypy_cache",
        ".coverage",
        "htmlcov",
        "runtime",
        "workspace",
        "state",
        "logs",
        "work",
    )
    shutil.copytree(source, destination, ignore=ignores, symlinks=False, dirs_exist_ok=True)


def patch_version(source: Path, version: str) -> None:
    """Pin ``__version__`` and the pyproject version for a test package."""
    init_path = source / "src" / "agent_bridge_connect" / "__init__.py"
    content = init_path.read_text(encoding="utf-8")
    patched = re.sub(
        r'__version__\s*=\s*["\'][^"\']*["\']',
        f'__version__ = "{version}"',
        content,
        count=1,
    )
    init_path.write_text(patched, encoding="utf-8")
    pyproject = source / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        text = re.sub(r'(?m)^version\s*=\s*["\'][^"\']*["\']', f'version = "{version}"', text, count=1)
        pyproject.write_text(text, encoding="utf-8")


def write_build_info(source: Path, version: str, commit: str, tree: str) -> None:
    """Write packaged provenance so the installed CLI reports a valid identity."""
    payload = {
        "schema_version": 1,
        "package_version": version,
        "commit_sha": commit,
        "source_tree_sha256": tree,
        "build_source": "agentbc-update-rc-e2e",
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    target = source / "src" / "agent_bridge_connect" / "_build_info.json"
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def inject_fault(source: Path, scenario: str) -> None:
    """Patch the copied new source so the new CLI fails the fault subcommand."""
    if scenario not in {"setup_refresh", "runner_start"}:
        return
    cli_path = source / "src" / "agent_bridge_connect" / "cli.py"
    content = cli_path.read_text(encoding="utf-8")
    marker = "def main(argv: list[str] | None = None) -> int:\n"
    if marker not in content:
        raise IsolationError(f"could not locate cli.main to inject fault: {cli_path}")
    content = content.replace(marker, marker + fault_patch_source(), 1)
    cli_path.write_text(content, encoding="utf-8")


def ensure_build_venv(plan: Plan, logger: "CommandLogger") -> Path:
    """Create one build venv with setuptools+wheel for reproducible wheel builds."""
    venv_python = plan.build_dir / "venv" / "bin" / "python"
    if venv_python.is_file():
        return venv_python
    logger.run([str(plan.python), "-m", "venv", str(plan.build_dir / "venv")], check=True)
    logger.run(
        [str(venv_python), "-m", "pip", "install", "-q", "setuptools", "wheel"],
        check=True,
    )
    return venv_python


def build_wheel(
    plan: Plan,
    source: Path,
    version: str,
    *,
    fault: str,
    logger: "CommandLogger",
) -> dict[str, Any]:
    """Build an isolated test wheel from an explicit source input."""
    copy_dir = plan.build_dir / f"src-{version}"
    shutil.rmtree(copy_dir, ignore_errors=True)
    copy_source_tree(source, copy_dir)
    inject_fault(copy_dir, fault)
    patch_version(copy_dir, version)
    commit, tree = git_identity(source)
    write_build_info(copy_dir, version, commit, tree)
    wheel_dir = plan.build_dir / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    build_python = ensure_build_venv(plan, logger)
    logger.run(
        [
            str(build_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheel_dir),
            str(copy_dir),
        ],
        check=True,
    )
    wheel = wheel_dir / _WHEEL_FILENAME.format(version=version)
    if not wheel.is_file():
        raise IsolationError(f"expected wheel was not produced: {wheel}")
    return {
        "path": wheel,
        "filename": wheel.name,
        "sha256": sha256_file(wheel),
        "size": wheel.stat().st_size,
        "commit_sha": commit,
        "source_tree_sha256": tree,
    }


def read_wheel_build_info(wheel: Path) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            if "agent_bridge_connect/_build_info.json" not in archive.namelist():
                return None
            payload = json.loads(archive.read("agent_bridge_connect/_build_info.json"))
        return payload if isinstance(payload, dict) else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def make_tls(cert_dir: Path) -> tuple[Path, Path, Path]:
    """Generate a throwaway test CA + server certificate via ``openssl``."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_key = cert_dir / "ca.key"
    ca_cert = cert_dir / "ca.pem"
    server_key = cert_dir / "server.key"
    server_csr = cert_dir / "server.csr"
    server_cert = cert_dir / "server.pem"
    ca_config = cert_dir / "ca.cnf"
    ca_config.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "prompt=no\n"
        "x509_extensions=v3_ca\n"
        "[dn]\n"
        "CN=AgentBC RC Test CA\n"
        "[v3_ca]\n"
        "basicConstraints=critical,CA:TRUE,pathlen:0\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always,issuer\n",
        encoding="utf-8",
    )
    san_file = cert_dir / "san.cnf"
    san_file.write_text(
        "[v3_server]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    for path in (ca_key, ca_cert, server_key, server_csr, server_cert):
        path.unlink(missing_ok=True)
    _run_or_raise(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(ca_key), "-out", str(ca_cert),
            "-days", "2", "-nodes", "-config", str(ca_config),
        ]
    )
    _run_or_raise(
        [
            "openssl", "req", "-newkey", "rsa:2048",
            "-keyout", str(server_key), "-out", str(server_csr),
            "-nodes", "-subj", "/CN=localhost",
        ]
    )
    _run_or_raise(
        [
            "openssl", "x509", "-req",
            "-in", str(server_csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(server_cert),
            "-days", "2", "-extfile", str(san_file), "-extensions", "v3_server",
        ]
    )
    return ca_cert, server_cert, server_key


def _run_or_raise(argv: list[str]) -> None:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        raise IsolationError(f"command failed ({completed.returncode}): {argv}\n{completed.stderr}")


class _FeedHandler(SimpleHTTPRequestHandler):
    """Serve only the RC feed directory over the local test HTTPS server."""

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        return


def start_feed_server(
    feed_dir: Path,
    cert: Path,
    key: Path,
) -> tuple[ThreadingHTTPServer, str]:
    """Start a local HTTPS server and return (server, base_url)."""
    handler = partial(_FeedHandler, directory=str(feed_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"https://{host}:{port}"
    return server, base_url


def write_feed(plan: Plan, base_url: str, wheel: dict[str, Any]) -> dict[str, Any]:
    """Write the verified local RC feed consumed by ``agentbc update``."""
    manifest = {
        "schema_version": 1,
        "tag": NEW_TAG,
        "package_version": plan.new_version,
        "commit_sha": wheel["commit_sha"],
        "source_tree_sha256": wheel["source_tree_sha256"],
        "artifacts": [
            {
                "filename": wheel["filename"],
                "size": wheel["size"],
                "sha256": wheel["sha256"],
            }
        ],
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    wheel_url = f"{base_url}/{wheel['filename']}"
    manifest_url = f"{base_url}/release-manifest.json"
    releases = [
        {
            "tag_name": NEW_TAG,
            "draft": False,
            "html_url": f"{base_url}/release",
            "body": "Isolated two-version RC E2E test release (UPD-103-001)",
            "assets": [
                {
                    "name": "release-manifest.json",
                    "browser_download_url": manifest_url,
                    "digest": f"sha256:{manifest_sha}",
                },
                {
                    "name": wheel["filename"],
                    "browser_download_url": wheel_url,
                    "digest": f"sha256:{wheel['sha256']}",
                },
            ],
        }
    ]
    plan.feed_dir.mkdir(parents=True, exist_ok=True)
    (plan.feed_dir / "releases.json").write_bytes(json.dumps(releases, indent=2).encode("utf-8"))
    (plan.feed_dir / "release-manifest.json").write_bytes(manifest_bytes)
    shutil.copy2(wheel["path"], plan.feed_dir / wheel["filename"])
    return {
        "base_url": base_url,
        "manifest_sha256": manifest_sha,
        "release_index_sha256": sha256_file(plan.feed_dir / "releases.json"),
    }


# ---------------------------------------------------------------------------
# Command runner + snapshots
# ---------------------------------------------------------------------------


class CommandLogger:
    """Run subprocesses and keep a machine-readable record of every command."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def run(
        self,
        argv: list[Any],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: float = 180,
        check: bool = False,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(argument) for argument in argv],
                input=input,
                text=True,
                capture_output=True,
                env=env,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
            returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        except OSError as exc:
            returncode = -2
            stdout = ""
            stderr = str(exc)
        self.records.append(
            {
                "argv": [str(argument) for argument in argv],
                "exit_code": returncode,
                "duration_ms": int(round((time.monotonic() - started) * 1000)),
                "stdout_excerpt": excerpt(stdout),
                "stderr_excerpt": excerpt(stderr),
            }
        )
        if check and returncode != 0:
            raise RuntimeError(
                f"command failed (exit {returncode}): {' '.join(str(a) for a in argv)}\n"
                f"{excerpt(stderr, 800)}"
            )
        return {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": stderr}


def snapshot_skill_hashes(plan: Plan) -> dict[str, dict[str, str]]:
    """Capture three-platform Skill bytes and manifest digests under isolated HOME."""
    roots = {
        "codex": plan.home / ".codex" / "skills" / "agentbc",
        "claude": plan.home / ".claude" / "skills" / "agentbc",
        "hermes": plan.home / ".hermes" / "skills" / "agentbc",
    }
    snapshots: dict[str, dict[str, str]] = {}
    for platform in PLATFORMS:
        root = roots[platform]
        manifest_path = root / ".agentbc-skill.json"
        manifest_sha = sha256_file(manifest_path) if manifest_path.is_file() else ""
        template_sha = ""
        if manifest_path.is_file():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                template_sha = str(payload.get("template_sha256") or "")
            except (OSError, ValueError):
                template_sha = ""
        snapshots[platform] = {
            "root": str(root),
            "manifest_sha256": manifest_sha,
            "template_sha256": template_sha,
            "files_sha256": dir_sha256(root),
        }
    return snapshots


def snapshot_stable_data(plan: Plan) -> dict[str, str]:
    config = sha256_file(plan.config_path) if plan.config_path.is_file() else ""
    return {
        "config_sha256": config,
        "workspace_sha256": dir_sha256(plan.workspace_root),
        "board_sha256": dir_sha256(plan.board_root),
        "workspace_data_sha256": dir_sha256_excluding(plan.workspace_root, ("record",)),
        "board_data_sha256": dir_sha256_excluding(plan.board_root, (".agentbc-cutover-ready",)),
    }


def parse_doctor_report(text: str) -> dict[str, Any]:
    try:
        report = json.loads(text)
        return report if isinstance(report, dict) else {}
    except (ValueError, TypeError):
        return {}


def snapshot_runner(plan: Plan, doctor_text: str) -> dict[str, Any]:
    report = parse_doctor_report(doctor_text)
    runner = report.get("runner") if isinstance(report.get("runner"), dict) else {}
    return {
        "identity": str(runner.get("identity") or "unavailable"),
        "status": str(runner.get("status") or "unavailable"),
        "pid": runner.get("pid"),
        "python_executable": str(runner.get("python_executable") or ""),
        "module_path": str(runner.get("module_path") or ""),
        "spool": str(plan.spool_root),
    }


def snapshot_cli_link(plan: Plan) -> str:
    target = plan.bin_dir / "agentbc"
    if not target.is_symlink():
        return ""
    try:
        return str(Path(os.readlink(target)).expanduser())
    except OSError:
        return ""


def diagnose_new_cli_skill_mismatch(
    plan: Plan,
    new_wheel: dict[str, Any],
    logger: CommandLogger,
) -> dict[str, Any]:
    """Determine whether the new CLI sees the old installed Skills as drifted.

    This is the documented pre-fix failure root cause: after a real
    1.0.2a1 -> 1.0.3a1 update the new ``setup --update`` leaves the
    three-platform Skills classified as ``modified``/``1.0.2a1``.  The
    diagnosis installs the new wheel into a throwaway venv and runs its
    ``doctor --json`` against the isolated old-Skill state, without touching
    the real installation.
    """
    venv = plan.build_dir / "diagnose-venv"
    venv_python = venv / "bin" / "python"
    if not venv_python.is_file():
        logger.run([str(plan.python), "-m", "venv", str(venv)], check=True)
        logger.run(
            [str(venv_python), "-m", "pip", "install", "--no-deps", str(new_wheel["path"])],
            check=True,
        )
    cli = venv / "bin" / "agentbc"
    doctor = logger.run(build_doctor_argv(cli), env=build_env(plan), timeout=60)
    report = parse_doctor_report(doctor["stdout"])
    skills = report.get("skills") if isinstance(report.get("skills"), dict) else {}
    mismatched: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        entry = skills.get(platform)
        if not isinstance(entry, dict):
            mismatched.append({"platform": platform, "classification": "unavailable", "up_to_date": False, "package_version": ""})
            continue
        if entry.get("up_to_date") is not True:
            mismatched.append(
                {
                    "platform": platform,
                    "classification": str(entry.get("classification") or ""),
                    "up_to_date": bool(entry.get("up_to_date")),
                    "package_version": str(entry.get("package_version") or ""),
                }
            )
    return {
        "new_package_version": plan.new_version,
        "diagnosed": bool(mismatched),
        "mismatched": mismatched,
    }


def install_old_release(plan: Plan, wheel: dict[str, Any], logger: CommandLogger) -> dict[str, Any]:
    """Install the 1.0.2a1 test package into the isolated hierarchy."""
    venv = plan.install_root / "venv"
    venv_python = venv / "bin" / "python"
    logger.run([str(plan.python), "-m", "venv", str(venv)], check=True)
    logger.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel["path"])],
        check=True,
    )
    plan.bin_dir.mkdir(parents=True, exist_ok=True)
    target = plan.bin_dir / "agentbc"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(venv / "bin" / "agentbc")
    # Seed the isolated workspace/board roots so setup keeps the update and
    # the driver on the same board; otherwise setup would default to
    # ``~/Documents/AgentBC/workspace`` inside the isolated HOME.
    plan.config_path.parent.mkdir(parents=True, exist_ok=True)
    if not plan.config_path.is_file():
        plan.config_path.write_text(
            f"workspace_root = '{plan.workspace_root}'\n"
            f"board_root = '{plan.board_root}'\n",
            encoding="utf-8",
        )
    environment = build_env(plan)
    setup = logger.run(build_setup_argv(target), env=environment, timeout=180, check=True)
    doctor = logger.run(build_doctor_argv(target), env=environment, timeout=60)
    report = parse_doctor_report(doctor["stdout"])
    package = report.get("package") if isinstance(report.get("package"), dict) else {}
    if str(package.get("version") or "") != plan.old_version:
        raise IsolationError(
            f"old test package identity is wrong: expected {plan.old_version}, "
            f"doctor reported {package.get('version')!r}"
        )
    return {
        "cli": str(target),
        "venv": str(venv),
        "setup": setup,
        "doctor": doctor,
        "package_version": str(package.get("version") or ""),
    }


# ---------------------------------------------------------------------------
# Evidence + contract evaluation
# ---------------------------------------------------------------------------


def collect_evidence(
    plan: Plan,
    *,
    old_wheel: dict[str, Any],
    new_wheel: dict[str, Any],
    feed_info: dict[str, Any],
    tls_info: dict[str, Any],
    logger: CommandLogger,
    doctor_before: dict[str, Any],
    doctor_after: dict[str, Any],
    update_result: dict[str, Any],
    skills_before: dict[str, dict[str, str]],
    skills_after: dict[str, dict[str, str]],
    stable_before: dict[str, str],
    stable_after: dict[str, str],
    runner_before: dict[str, Any],
    runner_after: dict[str, Any],
    cli_before: str,
    cli_after: str,
    rollback_complete: bool,
    actual_state: str,
    reason: str,
    diagnosis: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the machine-readable evidence document."""
    update_exit_code = int(update_result.get("returncode", -1))
    # The product swallows the specific ABCError code on rollback, so the
    # known pre-fix failure is diagnosed directly: the new CLI classifies the
    # old installed Skills as not current AND the update did not complete.
    known_failure = known_pre_fix_failure(diagnosis, actual_state, plan.scenario)
    skills_section: dict[str, Any] = {}
    for platform in PLATFORMS:
        before = skills_before.get(platform, {})
        after = skills_after.get(platform, {})
        skills_section[platform] = {
            "root": str(before.get("root") or ""),
            "manifest_sha256_before": str(before.get("manifest_sha256") or ""),
            "manifest_sha256_after": str(after.get("manifest_sha256") or ""),
            "template_sha256_before": str(before.get("template_sha256") or ""),
            "template_sha256_after": str(after.get("template_sha256") or ""),
            "files_sha256_before": str(before.get("files_sha256") or ""),
            "files_sha256_after": str(after.get("files_sha256") or ""),
        }
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": TASK_ID,
        "scenario": plan.scenario,
        "source": {
            "old": {
                "version": plan.old_version,
                "commit_sha": old_wheel.get("commit_sha", ""),
                "source_tree_sha256": old_wheel.get("source_tree_sha256", ""),
            },
            "new": {
                "version": plan.new_version,
                "commit_sha": new_wheel.get("commit_sha", ""),
                "source_tree_sha256": new_wheel.get("source_tree_sha256", ""),
            },
            "commit_sha": str(new_wheel.get("commit_sha") or old_wheel.get("commit_sha") or ""),
            "source_tree_sha256": str(
                new_wheel.get("source_tree_sha256") or old_wheel.get("source_tree_sha256") or ""
            ),
        },
        "artifacts": {
            "old_wheel": {
                "path": str(old_wheel["path"]),
                "sha256": old_wheel["sha256"],
                "size": int(old_wheel["size"]),
            },
            "new_wheel": {
                "path": str(new_wheel["path"]),
                "sha256": new_wheel["sha256"],
                "size": int(new_wheel["size"]),
            },
            "feed_manifest": {
                "path": str(plan.feed_dir / "release-manifest.json"),
                "sha256": feed_info["manifest_sha256"],
                "size": int((plan.feed_dir / "release-manifest.json").stat().st_size),
            },
            "release_index": {
                "path": str(plan.feed_dir / "releases.json"),
                "sha256": feed_info["release_index_sha256"],
                "size": int((plan.feed_dir / "releases.json").stat().st_size),
            },
            "ca_cert": {
                "path": str(tls_info["ca_cert"]),
                "sha256": tls_info["ca_cert_sha256"],
                "size": int(tls_info["ca_cert_size"]),
            },
        },
        "commands": logger.records,
        "cli_link": {
            "target": str(plan.bin_dir / "agentbc"),
            "readlink_before": cli_before,
            "readlink_after": cli_after,
            "restored": bool(cli_before and cli_after == cli_before),
        },
        "skills": skills_section,
        "runner": {
            "identity_before": str(runner_before.get("identity") or ""),
            "identity_after": str(runner_after.get("identity") or ""),
            "status_before": str(runner_before.get("status") or ""),
            "status_after": str(runner_after.get("status") or ""),
            "pid_before": runner_before.get("pid"),
            "pid_after": runner_after.get("pid"),
            "spool": str(plan.spool_root),
            "single_runner": bool(runner_after.get("single_runner", True)),
        },
        "stable_data": {
            "config_sha256_before": stable_before.get("config_sha256", ""),
            "config_sha256_after": stable_after.get("config_sha256", ""),
            "workspace_sha256_before": stable_before.get("workspace_sha256", ""),
            "workspace_sha256_after": stable_after.get("workspace_sha256", ""),
            "board_sha256_before": stable_before.get("board_sha256", ""),
            "board_sha256_after": stable_after.get("board_sha256", ""),
            "workspace_data_sha256_before": stable_before.get("workspace_data_sha256", ""),
            "workspace_data_sha256_after": stable_after.get("workspace_data_sha256", ""),
            "board_data_sha256_before": stable_before.get("board_data_sha256", ""),
            "board_data_sha256_after": stable_after.get("board_data_sha256", ""),
        },
        "outcome": {
            "expected": plan.scenario,
            "actual": actual_state,
            "update_exit_code": update_exit_code,
            "known_pre_fix_failure": bool(known_failure),
            "reason": reason,
            "rollback_complete": bool(rollback_complete),
        },
        "diagnosis": diagnosis,
        "contract": {},
    }
    validation = validate_evidence(evidence)
    if validation:
        evidence["contract"]["validation_errors"] = validation
    evidence["contract"].update(
        evaluate_outcome(plan.scenario, evidence)
    )
    return evidence


def known_pre_fix_failure(diagnosis: dict[str, Any], actual_state: str, scenario: str) -> bool:
    """True when the new CLI cannot accept the old Skills and the update failed.

    This is the documented pre-fix failure for the ``success`` and
    ``post_identity`` scenarios: the new CLI classifies the old installed
    Skills as not current and the update did not complete.  Fault scenarios
    are excluded because their failure is the injected fault, not the skill
    mismatch.
    """
    return bool(
        diagnosis.get("diagnosed")
        and actual_state in {"update_error", "unknown"}
        and scenario in {"success", "post_identity"}
    )


def evaluate_outcome(scenario: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a scenario contract and return per-clause pass/fail verdicts."""
    outcome = evidence.get("outcome") or {}
    cli_link = evidence.get("cli_link") or {}
    skills = evidence.get("skills") or {}
    runner = evidence.get("runner") or {}
    stable_data = evidence.get("stable_data") or {}
    verdict: dict[str, Any] = {"passes": [], "failures": []}

    def check(name: str, ok: bool) -> bool:
        (verdict["passes"] if ok else verdict["failures"]).append(name)
        return ok

    skills_restored = all(
        str(skills.get(platform, {}).get("files_sha256_before") or "")
        == str(skills.get(platform, {}).get("files_sha256_after") or "")
        for platform in PLATFORMS
    )
    runner_restored = (
        str(runner.get("identity_after") or "") == "match"
        and str(runner.get("status_after") or "") == "ready"
    )
    no_second_runner = bool(runner.get("single_runner", True))
    config_stable = (
        str(stable_data.get("config_sha256_before") or "")
        == str(stable_data.get("config_sha256_after") or "")
    )
    workspace_data_stable = (
        str(stable_data.get("workspace_data_sha256_before") or "")
        == str(stable_data.get("workspace_data_sha256_after") or "")
    )
    board_data_stable = (
        str(stable_data.get("board_data_sha256_before") or "")
        == str(stable_data.get("board_data_sha256_after") or "")
    )

    if scenario == "success":
        update_ok = int(outcome.get("update_exit_code", -1)) == 0 and str(
            outcome.get("actual") or ""
        ) in {"updated", "current"}
        check("update_succeeded", update_ok)
        check("cli_advanced", str(cli_link.get("readlink_after") or "") != str(cli_link.get("readlink_before") or ""))
        check("runner_matches_new", runner_restored)
        check("skills_refreshed", not skills_restored)
        known_failure = bool(
            outcome.get("known_pre_fix_failure")
            or (not update_ok and "skill" in str(outcome.get("reason") or "").lower())
        )
    else:
        check("cli_link_restored", bool(cli_link.get("restored")))
        check("skills_restored", skills_restored)
        check("runner_restored", runner_restored)
        check("no_second_runner", no_second_runner)
        check("config_stable", config_stable)
        check("workspace_data_stable", workspace_data_stable)
        check("board_data_stable", board_data_stable)
        known_failure = bool(outcome.get("known_pre_fix_failure") or not skills_restored)

    verdict["ok"] = not verdict["failures"]
    verdict["known_pre_fix_failure"] = bool(known_failure)
    return verdict


# ---------------------------------------------------------------------------
# Real run
# ---------------------------------------------------------------------------


def cleanup_runner(plan: Plan, logger: CommandLogger) -> None:
    """Stop only the isolated spool's Runner and return after it is gone."""
    cli = plan.bin_dir / "agentbc"
    if not (cli.exists() or cli.is_symlink()):
        cli = plan.install_root / "venv" / "bin" / "agentbc"
    if cli.is_file() or cli.is_symlink():
        logger.run(build_runner_stop_argv(plan), env=build_env(plan), timeout=60)
    # ``runner stop`` can miss a Runner whose venv was deleted mid-rollback;
    # scan the process table for the isolated spool as a final guarantee.
    _terminate_spool_pids(plan)


def _terminate_spool_pids(plan: Plan) -> None:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for pid in runner_pids_for_spool(completed.stdout.splitlines(), plan.spool_root):
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def real_run(plan: Plan, logger: CommandLogger | None = None) -> dict[str, Any]:
    """Execute the gated two-version E2E and return the evidence document."""
    if os.environ.get(GATE_ENV) != "1":
        raise GateNotEnabled(
            f"the real two-version run is opt-in; set {GATE_ENV}=1 to enable it"
        )
    logger = logger or CommandLogger()
    assert_temporary_root(plan.root)
    prepare_hierarchy(plan)
    write_stub_binaries(plan)
    fault_name = plan.scenario if plan.scenario in {"setup_refresh", "runner_start"} else ""

    if plan.old_wheel is not None:
        old_wheel = {
            "path": plan.old_wheel,
            "filename": plan.old_wheel.name,
            "sha256": sha256_file(plan.old_wheel),
            "size": plan.old_wheel.stat().st_size,
        }
        build_info = read_wheel_build_info(plan.old_wheel) or {}
        old_wheel["commit_sha"] = str(build_info.get("commit_sha") or "")
        old_wheel["source_tree_sha256"] = str(build_info.get("source_tree_sha256") or "")
    else:
        assert plan.old_src is not None
        old_wheel = build_wheel(plan, plan.old_src, plan.old_version, fault="", logger=logger)

    if plan.new_wheel is not None:
        # A pre-built wheel is used verbatim; when the scenario needs a fault,
        # the caller must supply a fault wheel.  The fault environment is still
        # exported so a fault wheel triggers exactly like a source build.
        new_wheel = {
            "path": plan.new_wheel,
            "filename": plan.new_wheel.name,
            "sha256": sha256_file(plan.new_wheel),
            "size": plan.new_wheel.stat().st_size,
        }
        build_info = read_wheel_build_info(plan.new_wheel) or {}
        new_wheel["commit_sha"] = str(build_info.get("commit_sha") or "")
        new_wheel["source_tree_sha256"] = str(build_info.get("source_tree_sha256") or "")
    else:
        assert plan.new_src is not None
        new_wheel = build_wheel(
            plan, plan.new_src, plan.new_version, fault=fault_name, logger=logger
        )

    ca_cert, server_cert, server_key = make_tls(plan.cert_dir)
    server: ThreadingHTTPServer | None = None
    base_url = ""
    try:
        server, base_url = start_feed_server(plan.feed_dir, server_cert, server_key)
        feed_info = write_feed(plan, base_url, new_wheel)
        tls_info = {
            "ca_cert": ca_cert,
            "ca_cert_sha256": sha256_file(ca_cert),
            "ca_cert_size": ca_cert.stat().st_size,
        }

        installed = install_old_release(plan, old_wheel, logger)
        cli_target = Path(installed["cli"])

        environment = build_env(plan, fault=fault_env_for(plan.scenario).get(FAULT_ENV, ""))
        environment["AGENTBC_UPDATE_INDEX_URL"] = f"{base_url}/releases.json"

        doctor_before_result = logger.run(
            build_doctor_argv(cli_target), env=build_env(plan), timeout=60
        )
        skills_before = snapshot_skill_hashes(plan)
        stable_before = snapshot_stable_data(plan)
        runner_before = snapshot_runner(plan, doctor_before_result["stdout"])
        cli_before = snapshot_cli_link(plan)

        # Diagnose the documented pre-fix failure root cause against the
        # isolated old-Skill state before the real update runs.
        diagnosis = diagnose_new_cli_skill_mismatch(plan, new_wheel, logger)

        update_result = logger.run(
            build_update_argv(plan),
            env=environment,
            input="y\n",
            timeout=600,
            check=False,
        )
        update_text = f"{update_result['stdout']}\n{update_result['stderr']}"

        doctor_after_result = logger.run(
            build_doctor_argv(cli_target), env=build_env(plan), timeout=60
        )
        skills_after = snapshot_skill_hashes(plan)
        stable_after = snapshot_stable_data(plan)
        runner_after = snapshot_runner(plan, doctor_after_result["stdout"])
        cli_after = snapshot_cli_link(plan)

        # Count isolated Runner processes after the scenario.
        try:
            ps = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            isolated_pids = runner_pids_for_spool(ps.stdout.splitlines(), plan.spool_root)
        except (OSError, subprocess.SubprocessError):
            isolated_pids = []
        runner_after["single_runner"] = len(isolated_pids) <= 1

        if "update_error:" in update_text:
            actual_state = "update_error"
        elif '"updated": true' in update_text or '"state": "updated"' in update_text:
            actual_state = "updated"
        elif '"state": "update_declined"' in update_text:
            actual_state = "update_declined"
        else:
            actual_state = "unknown"
        reason = excerpt(update_text, 600)
        rollback_complete = bool(
            "previous CLI, skills, and Runner were restored" in update_text
            or "update_rollback_incomplete" in update_text
        )

        evidence = collect_evidence(
            plan,
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            feed_info=feed_info,
            tls_info=tls_info,
            logger=logger,
            doctor_before=doctor_before_result,
            doctor_after=doctor_after_result,
            update_result=update_result,
            skills_before=skills_before,
            skills_after=skills_after,
            stable_before=stable_before,
            stable_after=stable_after,
            runner_before=runner_before,
            runner_after=runner_after,
            cli_before=cli_before,
            cli_after=cli_after,
            rollback_complete=rollback_complete,
            actual_state=actual_state,
            reason=reason,
            diagnosis=diagnosis,
        )
        return evidence
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_update_rc_e2e.py",
        description="Isolated two-version AgentBC Update RC E2E driver (UPD-103-001).",
    )
    parser.add_argument(
        "--scenario",
        default="success",
        choices=SCENARIOS,
        help="scenario to run (default: success)",
    )
    parser.add_argument("--old-src", type=_positive_path, help="source tree for the 1.0.2a1 test package")
    parser.add_argument("--new-src", type=_positive_path, help="source tree for the 1.0.3a1 test package")
    parser.add_argument("--old-wheel", type=_positive_path, help="pre-built 1.0.2a1 wheel")
    parser.add_argument("--new-wheel", type=_positive_path, help="pre-built 1.0.3a1 wheel")
    parser.add_argument(
        "--old-version",
        default=DEFAULT_OLD_VERSION,
        help=f"old package version (default: {DEFAULT_OLD_VERSION})",
    )
    parser.add_argument(
        "--new-version",
        default=DEFAULT_NEW_VERSION,
        help=f"new package version (default: {DEFAULT_NEW_VERSION})",
    )
    parser.add_argument("--python", type=_positive_path, default=sys.executable, help="base python for venvs")
    parser.add_argument("--plan", action="store_true", help="print the redacted plan and exit")
    parser.add_argument("--keep", action="store_true", help="keep the isolation root after the run")
    parser.add_argument("--out-evidence", type=_positive_path, help="write machine-readable evidence JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    plan = build_plan(args)
    if args.plan:
        print(plan_text(plan))
        shutil.rmtree(plan.root, ignore_errors=True)
        return 0
    try:
        evidence = real_run(plan)
    except GateNotEnabled as exc:
        print(f"e2e_gate_required: {exc}", file=sys.stderr)
        shutil.rmtree(plan.root, ignore_errors=True)
        return 3
    except (IsolationError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"e2e_error: {exc}", file=sys.stderr)
        cleanup_runner(plan, CommandLogger())
        if not plan.keep:
            shutil.rmtree(plan.root, ignore_errors=True)
        return 2

    try:
        cleanup_runner(plan, CommandLogger())
    finally:
        if not plan.keep:
            shutil.rmtree(plan.root, ignore_errors=True)

    secret_values = [
        os.environ.get("AGENTBC_RUNNER_TOKEN", ""),
    ]
    raw_document = json.dumps(evidence, indent=2, ensure_ascii=False)
    public = redact_text(raw_document, root=plan.root, home=plan.home, secrets=secret_values)
    if plan.out_evidence is not None:
        # The evidence file keeps the full machine-readable document (artifact
        # hashes, commands, exit codes, identities, stable-data hashes); the
        # stdout view and summary are always redacted.
        plan.out_evidence.parent.mkdir(parents=True, exist_ok=True)
        plan.out_evidence.write_text(raw_document + "\n", encoding="utf-8")
        print(f"evidence: {plan.out_evidence}")
    else:
        print(public)
    print("summary: " + redact_text(_summary_line(evidence), root=plan.root, home=plan.home, secrets=secret_values))
    return 0 if evidence.get("contract", {}).get("ok") else 1


def _summary_line(evidence: dict[str, Any]) -> str:
    contract = evidence.get("contract") or {}
    outcome = evidence.get("outcome") or {}
    passes = ", ".join(contract.get("passes") or []) or "none"
    failures = ", ".join(contract.get("failures") or []) or "none"
    return (
        f"scenario={evidence.get('scenario')} "
        f"actual={outcome.get('actual')} "
        f"exit={outcome.get('update_exit_code')} "
        f"known_pre_fix_failure={outcome.get('known_pre_fix_failure')} "
        f"passes=[{passes}] failures=[{failures}] "
        f"known_failure={contract.get('known_pre_fix_failure')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
