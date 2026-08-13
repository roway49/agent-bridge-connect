"""Read-only build, configuration, Runner, storage, Skill, and Executor diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import (
    load_config,
    resolve_config_path,
    resolve_workspace_root,
    validate_config,
)
from .task_health import ACTIVE_STATUSES

# Public doctor contract v2.  Status is frozen to healthy|warning|unavailable and
# the CLI exit code follows 0|1|2.  unavailable means a core execution-chain
# dependency is unusable; warning means a non-core or partial capability problem.
SCHEMA_VERSION = 2
BUILD_INFO_SCHEMA_VERSION = 1
EXIT_CODE_BY_STATUS = {"healthy": 0, "warning": 1, "unavailable": 2}
_EXECUTOR_PLATFORMS = ("codex", "claude", "hermes")
_DEFAULT_AUTH_ENV = {
    "codex": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "hermes": "HERMES_API_KEY",
}
_MAX_PROBE_VERSION_LENGTH = 200

_BUILD_INFO_FIELDS = {
    "schema_version",
    "package_version",
    "commit_sha",
    "source_tree_sha256",
    "build_source",
    "built_at_utc",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SAFE_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_EXECUTOR_PROBE_STATES = frozenset({"ok", "failed", "skipped", "unavailable"})
_EXECUTOR_SOURCES = frozenset(
    {
        "bun",
        "chatgpt_desktop",
        "codex_desktop_legacy",
        "common_user_bin",
        "cursor_extension_bundled",
        "env_override",
        "extra_path",
        "macos_dir",
        "node_manager",
        "not_found",
        "npm",
        "path",
        "pnpm",
        "unavailable",
        "version_manager",
        "vscode_extension_bundled",
        "windsurf_extension_bundled",
        "yarn",
    }
)
_DISTRIBUTION_UNSET = object()


def build_doctor_report(
    *,
    config_path: str | Path | None = None,
    runner_health: Callable[[], dict[str, Any]] | None = None,
    runner_storage: Callable[[list[str]], dict[str, Any]] | None = None,
    module_path: str | Path | None = None,
    executable_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    distribution: Any = _DISTRIBUTION_UNSET,
    candidate_marker_paths: list[str | Path] | None = None,
    build_info_path: str | Path | None = None,
    board_root: str | Path | None = None,
    cleanup_tasks: list[dict[str, Any]] | None = None,
    now: str | None = None,
    skill_roots: dict[str, str | Path] | None = None,
    skill_current_files: dict[str, dict[str, bytes]] | None = None,
    executor_probe: Callable[[str], dict[str, Any]] | None = None,
    runner_spool_root: str | Path | None = None,
    runner_token_path: str | Path | None = None,
) -> dict[str, Any]:
    """Collect the stable public doctor contract without changing local state."""
    runner_storage_required = runner_storage is not None
    if runner_health is None:
        from .runner import RunnerClient

        spool = Path(runner_spool_root or _default_runner_spool()).expanduser()
        token = Path(runner_token_path or (spool / "token")).expanduser()
        runner_client = RunnerClient(spool_root=spool, token_path=token)
        runner_health = runner_client.health
        if runner_storage is None:
            runner_storage = runner_client.storage_status
            runner_storage_required = True
    current_module = _resolved_path(module_path or _package_module_path())
    current_executable = _resolved_path(executable_path or _cli_executable_path())
    current_python = _resolved_path(python_executable or sys.executable)
    checkout_root = _find_source_checkout(current_module)
    installed_distribution = (
        _installed_distribution()
        if distribution is _DISTRIBUTION_UNSET
        else distribution
    )
    marker_paths = (
        [Path(path).expanduser() for path in candidate_marker_paths]
        if candidate_marker_paths is not None
        else _default_candidate_marker_paths(current_module, current_executable)
    )
    install_source = detect_install_source(
        current_module,
        distribution=installed_distribution,
        candidate_marker_paths=marker_paths,
        source_checkout=checkout_root,
    )

    identity_path = (
        Path(build_info_path).expanduser()
        if build_info_path is not None
        else current_module.with_name("_build_info.json")
    )
    build_info, build_info_state = _read_build_info(identity_path)
    commit_sha = None
    source_tree_sha256 = None
    build_source = "unknown"
    if build_info is not None:
        commit_sha = str(build_info["commit_sha"]).lower()
        source_tree_sha256 = str(build_info["source_tree_sha256"]).lower()
        build_source = str(build_info["build_source"])
    elif checkout_root is not None:
        commit_sha = _git_commit_sha(checkout_root)
        source_tree_sha256 = _source_tree_sha256(checkout_root)
        build_source = "source_checkout"

    package, package_checks = _safe_collect(
        "package",
        lambda: _collect_package(
            build_info=build_info,
            build_info_state=build_info_state,
            package_version=__version__,
            install_source=install_source,
            commit_sha=commit_sha,
            source_tree_sha256=source_tree_sha256,
            build_source=build_source,
            current_module=current_module,
            current_executable=current_executable,
        ),
    )
    config, config_checks, loaded_config = _collect_config(config_path)
    runner, runner_checks = _safe_collect(
        "runner",
        lambda: _collect_runner(
            runner_health,
            current_python=current_python,
            current_module=current_module,
            spool_root=runner_spool_root,
            token_path=runner_token_path,
        ),
    )
    authoritative_storage = (
        runner_storage
        if runner.get("status") == "ready" and runner.get("identity") == "match"
        else None
    )
    cleanup = (
        build_session_cleanup_diagnostics(cleanup_tasks, now=now)
        if cleanup_tasks is not None
        else collect_session_cleanup_diagnostics(
            board_root or _doctor_board_root(loaded_config),
            now=now,
        )
    )
    effective_board_root = board_root or _doctor_board_root(loaded_config)
    storage, storage_checks = _safe_collect(
        "storage",
        lambda: _collect_storage(
            loaded_config,
            board_root=effective_board_root,
            runner_storage=authoritative_storage,
            runner_storage_required=runner_storage_required,
        ),
    )
    skills, skills_checks = _safe_collect(
        "skills",
        lambda: _collect_skills(
            skill_roots=skill_roots,
            skill_current_files=skill_current_files,
            package_version=__version__,
        ),
    )
    executors, executors_checks = _safe_collect(
        "executors",
        lambda: _collect_executors(loaded_config, probe_fn=executor_probe),
    )
    blockers, blockers_checks = _safe_collect(
        "blockers",
        lambda: _collect_blockers(effective_board_root, cleanup=cleanup),
    )
    checks = _build_checks(
        package_checks=package_checks,
        config_checks=config_checks,
        runner_checks=runner_checks,
        storage_checks=storage_checks,
        skills_checks=skills_checks,
        executors_checks=executors_checks,
        cleanup=cleanup,
        blockers_checks=blockers_checks,
    )
    status = _overall_status(checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": EXIT_CODE_BY_STATUS[status],
        "package": package,
        "config": config,
        "runner": runner,
        "storage": storage,
        "skills": skills,
        "executors": executors,
        "session_cleanup": cleanup,
        "blockers": blockers,
        "checks": checks,
    }


def detect_install_source(
    module_path: str | Path,
    *,
    distribution: Any | None,
    candidate_marker_paths: list[str | Path],
    source_checkout: Path | None = None,
) -> str:
    """Classify install source in the required precedence order."""
    if any(Path(path).expanduser().is_file() for path in candidate_marker_paths):
        return "candidate"

    if distribution is not None:
        direct_url = _read_direct_url(distribution)
        if direct_url is not None:
            directory = direct_url.get("dir_info")
            if isinstance(directory, dict) and directory.get("editable") is True:
                return "editable"
            return "direct_url"
        return "pypi"

    checkout = source_checkout or _find_source_checkout(_resolved_path(module_path))
    if checkout is not None:
        return "source_checkout"
    return "unknown"


def render_doctor_text(report: dict[str, Any]) -> str:
    """Render text strictly from the same public data returned as JSON."""
    lines = [
        f"AgentBC doctor: {str(report.get('status') or 'unknown').upper()}",
    ]
    if "schema_version" in report:
        lines.append(f"schema_version: {report['schema_version']}")
    if "exit_code" in report:
        lines.append(f"exit_code: {report['exit_code']}")
    package = report.get("package")
    if isinstance(package, dict):
        lines.extend(_render_package(package))
    config = report.get("config")
    if isinstance(config, dict):
        lines.extend(_render_config(config))
    runner = report.get("runner")
    if isinstance(runner, dict):
        lines.extend(_render_runner(runner))
    storage = report.get("storage")
    if isinstance(storage, dict):
        lines.extend(_render_storage(storage))
    skills = report.get("skills")
    if isinstance(skills, dict):
        lines.extend(_render_skills(skills))
    executors = report.get("executors")
    if isinstance(executors, dict):
        lines.extend(_render_executors(executors))
    cleanup = report.get("session_cleanup")
    if isinstance(cleanup, dict):
        lines.extend(_render_cleanup(cleanup))
    blockers = report.get("blockers")
    if isinstance(blockers, dict):
        lines.extend(_render_blockers(blockers))
    checks = report.get("checks")
    if isinstance(checks, list):
        lines.append("Checks:")
        for check in checks:
            lines.append(
                f"  [{str(check['status']).upper()}] {check['id']}: {check['message']}"
            )
    return "\n".join(lines)


def collect_session_cleanup_diagnostics(
    board_root: str | Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Read AgentBC task receipts without touching Executor session storage."""
    root = Path(board_root).expanduser().resolve()
    tasks = _read_task_records(root)
    return build_session_cleanup_diagnostics(tasks, now=now)


def build_session_cleanup_diagnostics(
    tasks: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Build structured cleanup health data consumed by doctor text and JSON."""
    from .execution_policy import SESSION_EXTENSION_KEY, session_cleanup_view

    current = _parse_timestamp(now) if now else None
    current = current or datetime.now(timezone.utc)
    diagnostics: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        extensions = task.get("extensions")
        session = (
            extensions.get(SESSION_EXTENSION_KEY)
            if isinstance(extensions, dict)
            else None
        )
        if not isinstance(session, dict) or "cleanup" not in session:
            continue
        cleanup = session_cleanup_view(session.get("cleanup"))
        state = cleanup["state"]
        status = "healthy"
        executor = _safe_label(session.get("executor"), "unknown")
        if executor not in {"claude", "codex", "hermes"}:
            executor = "unknown"
        if state == "unsupported":
            status = "warning"
            message = (
                f"The current {executor} Executor has no official exact-session "
                "deletion capability; the terminal task is unchanged. Upgrade the "
                "Executor when an official capability is available."
            )
        elif state == "failed":
            status = "warning"
            message = (
                f"Cleanup failed with error_code={cleanup['error_code'] or 'unknown'}; "
                f"retryable={str(bool(cleanup['retryable'])).lower()}. AgentBC will "
                "use its bounded retry path when retryable."
            )
        elif state == "pending" and _pending_is_stale(
            session.get("cleanup"), current
        ):
            status = "warning"
            message = (
                "Cleanup has remained pending for more than five minutes; check "
                "AgentBC Runner health without inspecting Executor session stores."
            )
        elif state == "retained":
            message = "Executor session retention was requested; no cleanup is needed."
        elif state == "succeeded":
            message = "Executor temporary-session cleanup succeeded."
        elif state == "pending":
            message = "Executor temporary-session cleanup is pending."
        else:
            message = "No executor temporary-session cleanup warning is present."
        diagnostics.append(
            {
                "task_id": _safe_label(task.get("id"), "unknown"),
                "executor": executor,
                **cleanup,
                "status": status,
                "message": message,
            }
        )
    diagnostics.sort(key=lambda item: (item["task_id"], item["executor"]))
    warnings = sum(item["status"] == "warning" for item in diagnostics)
    return {
        "status": "warning" if warnings else "healthy",
        "warnings": warnings,
        "diagnostics": diagnostics,
    }


def _render_package(package: dict[str, Any]) -> list[str]:
    lines = ["Package:"]
    for field in (
        "version",
        "commit_sha",
        "source_tree_sha256",
        "build_source",
        "module_path",
        "executable_path",
        "install_source",
    ):
        lines.append(f"  {field}: {_text_value(package.get(field))}")
    if "status" in package:
        lines.append(f"  status: {_text_value(package.get('status'))}")
    if "reason" in package:
        lines.append(f"  reason: {_text_value(package.get('reason'))}")
    if "remediation" in package:
        lines.append(f"  remediation: {_text_value(package.get('remediation'))}")
    return lines


def _render_config(config: dict[str, Any]) -> list[str]:
    lines = ["Config:"]
    for field in ("path", "exists", "workspace_root", "board_root"):
        if field not in config:
            continue
        value = config[field]
        rendered = _bool_text(value) if isinstance(value, bool) else _text_value(value)
        lines.append(f"  {field}: {rendered}")
    for field in ("status", "reason", "remediation"):
        if field in config:
            lines.append(f"  {field}: {_text_value(config.get(field))}")
    return lines


def _render_runner(runner: dict[str, Any]) -> list[str]:
    lines = ["Runner:"]
    for field in ("status", "pid", "python_executable", "module_path", "identity"):
        if field in runner:
            lines.append(f"  {field}: {_text_value(runner.get(field))}")
    executors = runner.get("executors")
    if isinstance(executors, list):
        lines.append(f"  executors: {', '.join(executors) or '-'}")
    token_file = runner.get("token_file")
    if isinstance(token_file, dict):
        lines.append("  token_file:")
        for field in ("path", "exists", "is_file", "readable", "bytes"):
            if field in token_file:
                value = token_file[field]
                rendered = (
                    _bool_text(value)
                    if isinstance(value, bool)
                    else _text_value(value)
                )
                lines.append(f"    {field}: {rendered}")
    spool = runner.get("spool")
    if isinstance(spool, dict):
        lines.append("  spool:")
        for field in (
            "root",
            "exists",
            "requests_exists",
            "responses_exists",
            "processing_exists",
            "pid_file",
            "pid_file_exists",
        ):
            if field in spool:
                value = spool[field]
                rendered = (
                    _bool_text(value)
                    if isinstance(value, bool)
                    else _text_value(value)
                )
                lines.append(f"    {field}: {rendered}")
    for field in ("reason", "remediation"):
        if field in runner:
            lines.append(f"  {field}: {_text_value(runner.get(field))}")
    return lines


def _render_storage(storage: dict[str, Any]) -> list[str]:
    lines = ["Storage:"]
    for name in ("workspace", "report", "record"):
        info = storage.get(name)
        if not isinstance(info, dict):
            continue
        flags = " ".join(
            f"{key}={_bool_text(info.get(key))}"
            for key in ("exists", "is_dir", "readable", "writable")
        )
        lines.append(
            f"  {name}: {_text_value(info.get('path'))} {flags} "
            f"[{str(info.get('status') or 'unknown').upper()}]"
        )
        if info.get("reason"):
            lines.append(f"    reason: {info['reason']}")
        if info.get("remediation"):
            lines.append(f"    remediation: {info['remediation']}")
    lines.append(f"  status: {_text_value(storage.get('status'))}")
    if storage.get("reason"):
        lines.append(f"  reason: {storage['reason']}")
    if storage.get("remediation"):
        lines.append(f"  remediation: {storage['remediation']}")
    return lines


def _render_skills(skills: dict[str, Any]) -> list[str]:
    lines = ["Skills:"]
    for platform in _EXECUTOR_PLATFORMS:
        entry = skills.get(platform)
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"  {platform}: {_text_value(entry.get('classification'))} "
            f"(package={_text_value(entry.get('package_version'))} "
            f"protocol={_text_value(entry.get('protocol_version'))} "
            f"completion={_text_value(entry.get('completion_version'))} "
            f"hash={_text_value(entry.get('template_sha256'))}) "
            f"[{str(entry.get('status') or 'unknown').upper()}]"
        )
        if entry.get("reason"):
            lines.append(f"    reason: {entry['reason']}")
        if entry.get("remediation"):
            lines.append(f"    remediation: {entry['remediation']}")
    lines.append(f"  status: {_text_value(skills.get('status'))}")
    lines.append(f"  warnings: {_text_value(skills.get('warnings'))}")
    return lines


def _render_executors(executors: dict[str, Any]) -> list[str]:
    lines = ["Executors:"]
    for platform in _EXECUTOR_PLATFORMS:
        entry = executors.get(platform)
        if not isinstance(entry, dict):
            continue
        capability = entry.get("capability") if isinstance(entry.get("capability"), dict) else {}
        auth = entry.get("auth") if isinstance(entry.get("auth"), dict) else {}
        lines.append(
            f"  {platform}: configured={_bool_text(entry.get('configured'))} "
            f"resolved={_bool_text(entry.get('resolved'))} "
            f"source={_text_value(entry.get('source'))} "
            f"version={_text_value(entry.get('version'))} "
            f"probe={_text_value(entry.get('probe'))} "
            f"auth={_text_value(auth.get('key_env'))} "
            f"capability=level{_text_value(capability.get('level'))} "
            f"[{str(entry.get('status') or 'unknown').upper()}]"
        )
        if entry.get("reason"):
            lines.append(f"    reason: {entry['reason']}")
        if entry.get("remediation"):
            lines.append(f"    remediation: {entry['remediation']}")
    lines.append(f"  status: {_text_value(executors.get('status'))}")
    lines.append(f"  warnings: {_text_value(executors.get('warnings'))}")
    return lines


def _render_cleanup(cleanup: dict[str, Any]) -> list[str]:
    lines = ["Session cleanup:"]
    lines.append(f"  status: {_text_value(cleanup.get('status'))}")
    lines.append(f"  warnings: {_text_value(cleanup.get('warnings'))}")
    for diagnostic in cleanup.get("diagnostics", []):
        lines.append(
            "  "
            f"[{str(diagnostic.get('status', '')).upper()}] "
            f"{_text_value(diagnostic.get('task_id'))} "
            f"({_text_value(diagnostic.get('executor'))}): "
            f"capability={_text_value(diagnostic.get('capability'))} "
            f"state={_text_value(diagnostic.get('state'))} "
            f"attempts={_text_value(diagnostic.get('attempts'))} "
            f"error_code={_text_value(diagnostic.get('error_code'))} "
            f"retryable={_bool_text(diagnostic.get('retryable'))} - "
            f"{_text_value(diagnostic.get('message'))}"
        )
    return lines


def _render_blockers(blockers: dict[str, Any]) -> list[str]:
    lines = ["Blockers:"]
    lines.append(f"  status: {_text_value(blockers.get('status'))}")
    lines.append(f"  count: {_text_value(blockers.get('count'))}")
    for item in blockers.get("items", []):
        lines.append(
            f"  [{str(item.get('type') or 'unknown').upper()}] "
            f"{_text_value(item.get('task_id'))} "
            f"({_text_value(item.get('executor'))}) "
            f"kind={_text_value(item.get('kind'))} "
            f"state={_text_value(item.get('state'))}"
        )
    return lines


def _collect_package(
    *,
    build_info: dict[str, Any] | None,
    build_info_state: str,
    package_version: str,
    install_source: str,
    commit_sha: str | None,
    source_tree_sha256: str | None,
    build_source: str,
    current_module: Path,
    current_executable: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if build_info_state == "invalid":
        identity_status = "unavailable"
        identity_reason = "Packaged build identity is invalid; provenance cannot be trusted."
        identity_remediation = "Reinstall the AgentBC package from a trusted source."
    elif build_info_state == "valid":
        if str(build_info.get("package_version")) == package_version:
            identity_status = "healthy"
            identity_reason = "Packaged build identity is valid."
            identity_remediation = ""
        else:
            identity_status = "unavailable"
            identity_reason = (
                "Packaged build identity version does not match the runtime package."
            )
            identity_remediation = "Reinstall the matching AgentBC package version."
    else:
        identity_status = "warning"
        identity_reason = (
            "Packaged build identity is unavailable; safe runtime fallbacks are in use."
        )
        identity_remediation = (
            "Install a packaged AgentBC release so provenance is complete."
        )

    if install_source == "unknown":
        source_status = "warning"
        source_reason = "Install source could not be determined."
        source_remediation = (
            "Install AgentBC through pip, an editable install, or a candidate build."
        )
    else:
        source_status = "healthy"
        source_reason = f"Install source classified as {install_source}."
        source_remediation = ""

    if identity_status == "unavailable" or source_status == "unavailable":
        status = "unavailable"
    elif identity_status == "warning" or source_status == "warning":
        status = "warning"
    else:
        status = "healthy"
    if status == identity_status:
        reason, remediation = identity_reason, identity_remediation
    else:
        reason, remediation = source_reason, source_remediation

    section = {
        "version": package_version,
        "commit_sha": commit_sha,
        "source_tree_sha256": source_tree_sha256,
        "build_source": build_source,
        "module_path": str(current_module),
        "executable_path": str(current_executable),
        "install_source": install_source,
        "status": status,
        "reason": reason,
        "remediation": remediation,
    }
    checks = [
        {
            "id": "package.build_identity",
            "status": identity_status,
            "message": identity_reason,
        },
        {
            "id": "package.install_source",
            "status": source_status,
            "message": source_reason,
        },
    ]
    return section, checks


def _collect_config(
    config_path: str | Path | None,
) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, Any]]:
    path = _resolved_path(resolve_config_path(config_path))
    exists = path.is_file()
    loaded: dict[str, Any] = {}
    error: str | None = None
    if exists:
        try:
            loaded = load_config(path)
            validation_errors = validate_config(loaded)
        except (OSError, ValueError, TypeError):
            validation_errors = []
            error = "parse_error"
        if validation_errors:
            error = "invalid_config"
    workspace_root = _resolved_path(resolve_workspace_root(loaded))
    board_root = _doctor_board_root(loaded)
    if error is not None:
        status = "unavailable"
        reason = "Configuration is unreadable or invalid."
        remediation = "Fix or remove the invalid config file, then run agentbc setup."
        check_status = "unavailable"
    elif exists:
        status = "healthy"
        reason = "Configuration loaded successfully."
        remediation = ""
        check_status = "healthy"
    else:
        status = "warning"
        reason = "Configuration file is absent; defaults are in use."
        remediation = "Run agentbc setup to create a configuration."
        check_status = "warning"
    return (
        {
            "path": str(path),
            "exists": exists,
            "workspace_root": str(workspace_root),
            "board_root": str(board_root),
            "status": status,
            "reason": reason,
            "remediation": remediation,
        },
        [{"id": "config.load", "status": check_status, "message": reason}],
        loaded,
    )


def _collect_runner(
    runner_health: Callable[[], dict[str, Any]] | None,
    *,
    current_python: Path,
    current_module: Path,
    spool_root: str | Path | None,
    token_path: str | Path | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    from .runner import RunnerClient

    spool = Path(spool_root or _default_runner_spool()).expanduser()
    token = Path(token_path or (spool / "token")).expanduser()
    if runner_health is None:
        runner_health = RunnerClient(spool_root=spool, token_path=token).health
    try:
        health = runner_health()
    except Exception:  # noqa: BLE001 - doctor reports unavailability without leaking details.
        health = None
    token_file = _token_file_metadata(token)
    spool_status = _spool_status(spool)
    if (
        not isinstance(health, dict)
        or not health.get("ok")
        or health.get("status") != "ready"
    ):
        return (
            {
                "status": "unavailable",
                "pid": None,
                "python_executable": None,
                "module_path": None,
                "executors": [],
                "identity": "unavailable",
                "token_file": token_file,
                "spool": spool_status,
                "reason": "Runner is unavailable; no changes were attempted.",
                "remediation": "Start the AgentBC Runner (agentbc runner start) and re-run doctor.",
            },
            [
                {
                    "id": "runner.availability",
                    "status": "unavailable",
                    "message": "Runner is unavailable; no changes were attempted.",
                },
                {
                    "id": "runner.identity",
                    "status": "unavailable",
                    "message": "Runner identity could not be compared.",
                },
            ],
        )
    pid = health.get("pid")
    raw_executors = health.get("executors")
    executors = (
        sorted(
            value
            for value in raw_executors
            if isinstance(value, str) and _SAFE_NAME.fullmatch(value)
        )
        if isinstance(raw_executors, list)
        else []
    )
    python_path = _public_identity_path(health.get("python_executable"))
    module_path = _public_identity_path(health.get("module_path"))
    if python_path is None or module_path is None:
        identity = "incomplete"
        reason = "Runner did not provide complete runtime identity."
        remediation = "Restart the AgentBC Runner so it reports full runtime identity."
        identity_check = {
            "id": "runner.identity",
            "status": "unavailable",
            "message": reason,
        }
    else:
        drift: list[str] = []
        if _resolved_path(python_path) != current_python:
            drift.append("Python interpreter")
        if _resolved_path(module_path) != current_module:
            drift.append("AgentBC module")
        if drift:
            identity = "drift"
            reason = f"CLI/Runner drift detected for {' and '.join(drift)}."
            remediation = (
                "Reinstall or restart AgentBC so the CLI and Runner run the same "
                "Python interpreter and module."
            )
            identity_check = {
                "id": "runner.identity",
                "status": "unavailable",
                "message": reason,
            }
        else:
            identity = "match"
            reason = "CLI and Runner identities match."
            remediation = ""
            identity_check = {
                "id": "runner.identity",
                "status": "healthy",
                "message": reason,
            }
    return (
        {
            "status": "ready",
            "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "python_executable": python_path,
            "module_path": module_path,
            "executors": executors,
            "identity": identity,
            "token_file": token_file,
            "spool": spool_status,
            "reason": reason,
            "remediation": remediation,
        },
        [
            {
                "id": "runner.availability",
                "status": "healthy",
                "message": "Runner is ready.",
            },
            identity_check,
        ],
    )


def _collect_storage(
    config: dict[str, Any],
    *,
    board_root: str | Path,
    runner_storage: Callable[[list[str]], dict[str, Any]] | None = None,
    runner_storage_required: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    workspace_root = _resolved_path(resolve_workspace_root(config))
    record_root = _resolved_path(board_root)
    report_root = workspace_root / "tasks" / "report"
    paths = [workspace_root, report_root, record_root]
    verified = (
        _runner_storage_permissions(paths, runner_storage)
        if runner_storage is not None
        else None
    )
    if verified is not None:
        workspace, report, record = verified
        _apply_storage_severity(workspace, required_write=True, required_read=False)
        _apply_storage_severity(report, required_write=True, required_read=False)
        _apply_storage_severity(record, required_write=True, required_read=True)
    elif runner_storage_required:
        workspace, report, record = [
            _unverified_storage_permissions(path) for path in paths
        ]
    else:
        workspace = _path_permissions(workspace_root)
        report = _path_permissions(report_root)
        record = _path_permissions(record_root)
        _apply_storage_severity(workspace, required_write=True, required_read=False)
        _apply_storage_severity(report, required_write=True, required_read=False)
        _apply_storage_severity(record, required_write=True, required_read=True)

    statuses = (workspace["status"], report["status"], record["status"])
    if "unavailable" in statuses:
        status = "unavailable"
        reason = "A required workspace/report/record path is unusable."
        remediation = "Fix the path or its permissions, then re-run doctor."
    elif "warning" in statuses:
        status = "warning"
        reason = "A workspace/report/record path needs attention."
        remediation = "Review the reported paths and run agentbc setup or agentbc init."
    else:
        status = "healthy"
        reason = "Workspace, report, and record paths are available."
        remediation = ""
    return (
        {
            "workspace": workspace,
            "report": report,
            "record": record,
            "status": status,
            "reason": reason,
            "remediation": remediation,
        },
        [
            {
                "id": "storage.workspace",
                "status": workspace["status"],
                "message": workspace["reason"],
            },
            {
                "id": "storage.report",
                "status": report["status"],
                "message": report["reason"],
            },
            {
                "id": "storage.record",
                "status": record["status"],
                "message": record["reason"],
            },
        ],
    )


def _collect_skills(
    *,
    skill_roots: dict[str, str | Path] | None,
    skill_current_files: dict[str, dict[str, bytes]] | None,
    package_version: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    roots = skill_roots or _default_skill_roots()
    current_files = skill_current_files or _default_skill_current_files()
    entries: dict[str, Any] = {}
    checks: list[dict[str, str]] = []
    warnings = 0
    for platform in _EXECUTOR_PLATFORMS:
        entry, check = _collect_skill_entry(
            platform,
            roots,
            current_files,
            package_version,
        )
        entries[platform] = entry
        warnings += 1 if entry["status"] == "warning" else 0
        checks.append(check)
    return (
        {
            **entries,
            "status": "warning" if warnings else "healthy",
            "warnings": warnings,
        },
        checks,
    )


def _collect_skill_entry(
    platform: str,
    roots: dict[str, str | Path],
    current_files: dict[str, dict[str, bytes]],
    package_version: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    from .skill_packages import classify_skill_package

    root = Path(roots[platform]).expanduser()
    files = current_files.get(platform, {})
    try:
        state = classify_skill_package(
            root,
            platform=platform,
            package_version=package_version,
            current_files=files,
        )
    except Exception:  # noqa: BLE001 - a Skill collector must never crash doctor.
        state = None
    if not isinstance(state, dict):
        return (
            {
                "platform": platform,
                "root": str(root),
                "classification": "unavailable",
                "installed": False,
                "up_to_date": False,
                "package_version": package_version,
                "protocol_version": "",
                "completion_version": None,
                "template_sha256": "",
                "status": "warning",
                "reason": f"The {platform} Skill package could not be classified.",
                "remediation": (
                    f"Run agentbc setup --update to refresh the {platform} Skill package."
                ),
            },
            {
                "id": f"skills.{platform}",
                "status": "warning",
                "message": f"The {platform} Skill package could not be classified.",
            },
        )
    classification = str(state.get("classification") or "missing")
    installed_manifest = state.get("manifest")
    expected_manifest = state.get("expected_manifest")
    manifest = installed_manifest if isinstance(installed_manifest, dict) else expected_manifest
    manifest = manifest if isinstance(manifest, dict) else {}
    if classification == "current":
        status = "healthy"
        reason = f"The {platform} Skill package is current."
        remediation = ""
    else:
        status = "warning"
        reason = (
            f"The {platform} Skill package is {classification}; "
            "AgentBC-managed files were not modified."
        )
        remediation = (
            f"Run agentbc setup --update to refresh the {platform} Skill package."
        )
    completion_version = manifest.get("completion_version")
    return (
        {
            "platform": platform,
            "root": str(root),
            "classification": classification,
            "installed": bool(state.get("installed")),
            "up_to_date": bool(state.get("up_to_date")),
            "package_version": str(manifest.get("package_version") or package_version),
            "protocol_version": str(manifest.get("protocol_version") or ""),
            "completion_version": (
                completion_version
                if isinstance(completion_version, int)
                else None
            ),
            "template_sha256": str(manifest.get("template_sha256") or ""),
            "status": status,
            "reason": reason,
            "remediation": remediation,
        },
        {
            "id": f"skills.{platform}",
            "status": status,
            "message": reason,
        },
    )


def _collect_executors(
    config: dict[str, Any],
    *,
    probe_fn: Callable[[str], dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    entries: dict[str, Any] = {}
    checks: list[dict[str, str]] = []
    warnings = 0
    for platform in _EXECUTOR_PLATFORMS:
        entry, check = _collect_executor_entry(platform, config, probe_fn)
        entries[platform] = entry
        warnings += 1 if entry["status"] == "warning" else 0
        checks.append(check)
    return (
        {
            **entries,
            "status": "warning" if warnings else "healthy",
            "warnings": warnings,
        },
        checks,
    )


def _collect_executor_entry(
    platform: str,
    config: dict[str, Any],
    probe_fn: Callable[[str], dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    executors_table = config.get("executors") if isinstance(config, dict) else None
    configured = isinstance(executors_table, dict) and platform in executors_table
    raw_config = (
        executors_table.get(platform) if configured else None
    )
    executor_config = dict(raw_config) if isinstance(raw_config, dict) else {}
    command = executor_config.get("command")
    command = command if isinstance(command, str) and command.strip() else None
    if probe_fn is not None:
        try:
            probe = probe_fn(platform)
        except Exception:  # noqa: BLE001 - a probe failure must never crash doctor.
            probe = None
    else:
        try:
            probe = _default_executor_probe(platform, executor_config)
        except Exception:  # noqa: BLE001 - a probe failure must never crash doctor.
            probe = None
    public_probe = _public_executor_probe(probe, platform, executor_config)
    resolved = public_probe["resolved"]
    source = public_probe["source"]
    version = public_probe["version"]
    probe_state = public_probe["probe"]
    auth = public_probe["auth"]
    capability = public_probe["capability"]
    if not configured:
        status = "healthy"
        reason = f"The {platform} executor is not configured."
        remediation = ""
    elif not resolved:
        status = "warning"
        reason = f"The {platform} executor is configured but its command could not be resolved."
        remediation = (
            f"Install the {platform} CLI or set AGENTBC_{platform.upper()}_BIN, "
            "then run agentbc setup."
        )
    elif probe_state in ("failed", "unavailable"):
        status = "warning"
        reason = f"The {platform} executor is resolved but its probe failed."
        remediation = f"Verify the {platform} installation and run agentbc setup."
    elif probe_state == "ok":
        status = "healthy"
        reason = f"The {platform} executor is configured and available."
        remediation = ""
    else:
        status = "warning"
        reason = f"The {platform} executor probe was not completed."
        remediation = "Run agentbc setup to refresh executor discovery."
    return (
        {
            "platform": platform,
            "configured": configured,
            "command": command,
            "resolved": resolved,
            "source": source,
            "version": version,
            "probe": probe_state,
            "auth": auth,
            "capability": capability,
            "status": status,
            "reason": reason,
            "remediation": remediation,
        },
        {
            "id": f"executors.{platform}",
            "status": status,
            "message": reason,
        },
    )


def _collect_blockers(
    board_root: str | Path,
    *,
    cleanup: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    from .permission_grants import (
        PERMISSION_GRANT_EXTENSION_KEY,
        permission_grant_public_projection,
    )
    from .protocol import ABCError

    from .execution_policy import is_resource_decision_request

    tasks = _read_task_records(board_root)
    items: list[dict[str, Any]] = []
    for task in tasks:
        task_id = _safe_label(task.get("id"), "unknown")
        status = str(task.get("status") or "")
        if status not in _BLOCKER_TASK_STATUSES:
            continue
        executor = _safe_label(task.get("assignee"), "unknown")
        extensions = task.get("extensions")
        extensions = extensions if isinstance(extensions, dict) else {}
        if status == "needs_recovery":
            items.append(
                {
                    "task_id": task_id,
                    "type": "needs_recovery",
                    "executor": executor,
                    "kind": "",
                    "state": "needs_recovery",
                }
            )
        if status == "input_required":
            request = extensions.get("agentbc.input")
            if is_resource_decision_request(request):
                items.append(
                    {
                        "task_id": task_id,
                        "type": "resource",
                        "executor": executor,
                        "kind": "resource_limit",
                        "state": _safe_label(request.get("status"), "waiting"),
                    }
                )
            elif isinstance(request, dict):
                items.append(
                    {
                        "task_id": task_id,
                        "type": "input",
                        "executor": executor,
                        "kind": _safe_label(
                            request.get("kind") or request.get("type"), "input"
                        ),
                        "state": _safe_label(request.get("status"), "waiting"),
                    }
                )
        grant = extensions.get(PERMISSION_GRANT_EXTENSION_KEY)
        if grant is not None:
            try:
                projection = permission_grant_public_projection(grant)
            except ABCError:
                projection = None
            if projection and projection.get("active"):
                items.append(
                    {
                        "task_id": task_id,
                        "type": "permission",
                        "executor": executor,
                        "kind": "permission_grant",
                        "state": _safe_label(projection.get("state"), "issued"),
                    }
                )
    for diagnostic in cleanup.get("diagnostics", []):
        if diagnostic.get("status") == "warning":
            items.append(
                {
                    "task_id": _safe_label(diagnostic.get("task_id"), "unknown"),
                    "type": "cleanup",
                    "executor": _safe_label(diagnostic.get("executor"), "unknown"),
                    "kind": _safe_label(diagnostic.get("capability"), ""),
                    "state": _safe_label(diagnostic.get("state"), ""),
                }
            )
    items.sort(key=lambda item: (item["type"], item["task_id"], item["executor"]))
    count = len(items)
    return (
        {
            "status": "warning" if count else "healthy",
            "count": count,
            "items": items,
        },
        [
            {
                "id": "blockers.active",
                "status": "warning" if count else "healthy",
                "message": (
                    f"{count} active blocker(s) require attention."
                    if count
                    else "No active blockers were found."
                ),
            }
        ],
    )


def _build_checks(
    *,
    package_checks: list[dict[str, str]],
    config_checks: list[dict[str, str]],
    runner_checks: list[dict[str, str]],
    storage_checks: list[dict[str, str]],
    skills_checks: list[dict[str, str]],
    executors_checks: list[dict[str, str]],
    cleanup: dict[str, Any],
    blockers_checks: list[dict[str, str]],
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = [
        *package_checks,
        *config_checks,
        *runner_checks,
        *storage_checks,
        *skills_checks,
        *executors_checks,
    ]
    if cleanup["warnings"]:
        checks.append(
            {
                "id": "session.cleanup",
                "status": "warning",
                "message": (
                    f"{cleanup['warnings']} executor session cleanup warning(s) "
                    "require attention."
                ),
            }
        )
    else:
        checks.append(
            {
                "id": "session.cleanup",
                "status": "healthy",
                "message": "No executor session cleanup warnings were found.",
            }
        )
    checks.extend(blockers_checks)
    checks.sort(key=lambda item: item["id"])
    return checks


def _apply_storage_severity(
    info: dict[str, Any],
    *,
    required_write: bool,
    required_read: bool,
) -> None:
    if not info["exists"]:
        if required_write and not info["writable"]:
            info["status"] = "unavailable"
            info["reason"] = f"{info['path']} is missing and cannot be created."
            info["remediation"] = (
                "Restore write permission on its parent directory and re-run doctor."
            )
        else:
            info["status"] = "warning"
            info["reason"] = f"{info['path']} does not exist yet."
            info["remediation"] = (
                "Run agentbc setup or agentbc init to create the required path."
            )
        return
    if not info["is_dir"]:
        info["status"] = "unavailable"
        info["reason"] = f"{info['path']} exists but is not a directory."
        info["remediation"] = "Fix the configured path so it is a directory."
        return
    if required_read and not info["readable"]:
        info["status"] = "unavailable"
        info["reason"] = f"{info['path']} is not readable."
        info["remediation"] = "Restore read permission and re-run doctor."
        return
    if required_write and not info["writable"]:
        info["status"] = "unavailable"
        info["reason"] = f"{info['path']} is not writable."
        info["remediation"] = "Restore write permission and re-run doctor."
        return
    if not info["readable"]:
        info["status"] = "warning"
        info["reason"] = f"{info['path']} is not readable."
        info["remediation"] = "Restore read permission and re-run doctor."
        return
    info["status"] = "healthy"
    info["reason"] = f"{info['path']} is available."
    info["remediation"] = ""


def _path_permissions(path: Path) -> dict[str, Any]:
    exists = path.exists()
    is_dir = path.is_dir() if exists else False
    readable = os.access(path, os.R_OK) if exists else False
    writable = os.access(path, os.W_OK) if exists else _write_capable(path)
    return {
        "path": str(path),
        "exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
    }


def _runner_storage_permissions(
    paths: list[Path],
    probe: Callable[[list[str]], dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Validate the Runner's storage reply without exposing probe failures."""
    expected = [str(path) for path in paths]
    requested = list(dict.fromkeys(expected))
    try:
        response = probe(requested)
    except Exception:  # noqa: BLE001 - doctor emits a stable sanitized failure.
        return None
    if (
        not isinstance(response, dict)
        or response.get("ok") is not True
        or response.get("status") != "ready"
        or not isinstance(response.get("paths"), list)
    ):
        return None
    rows = response["paths"]
    if len(rows) != len(requested):
        return None
    by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "exists",
            "is_dir",
            "readable",
            "writable",
        }:
            return None
        path = row.get("path")
        if not isinstance(path, str) or path not in requested or path in by_path:
            return None
        if any(
            not isinstance(row.get(field), bool)
            for field in ("exists", "is_dir", "readable", "writable")
        ):
            return None
        by_path[path] = dict(row)
    if set(by_path) != set(requested):
        return None
    return [by_path[path] for path in expected]


def _unverified_storage_permissions(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": False,
        "is_dir": False,
        "readable": False,
        "writable": False,
        "status": "unavailable",
        "reason": "Runner could not verify storage access.",
        "remediation": "Restart the AgentBC Runner and re-run doctor.",
    }


def _write_capable(path: Path) -> bool:
    """Report whether a missing path could be created via an existing ancestor."""
    current = Path(path)
    while not current.exists() and current.parent != current:
        current = current.parent
    return os.access(current, os.W_OK)


def _token_file_metadata(token_path: Path) -> dict[str, Any]:
    path = Path(token_path).expanduser()
    exists = path.exists()
    is_file = path.is_file() if exists else False
    readable = os.access(path, os.R_OK) if exists else False
    size = path.stat().st_size if exists and is_file else None
    return {
        "path": str(path),
        "exists": exists,
        "is_file": is_file,
        "readable": readable,
        "bytes": size,
    }


def _spool_status(spool_root: Path) -> dict[str, Any]:
    root = Path(spool_root).expanduser()
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "requests_exists": (root / "requests").is_dir(),
        "responses_exists": (root / "responses").is_dir(),
        "processing_exists": (root / "processing").is_dir(),
        "pid_file": str(root / "runner.pid"),
        "pid_file_exists": (root / "runner.pid").exists(),
    }


def _read_task_records(board_root: str | Path) -> list[dict[str, Any]]:
    """Read task receipts read-only; never creates or rewrites any directory."""
    root = Path(board_root).expanduser().resolve()
    if not root.is_dir():
        return []
    tasks: list[dict[str, Any]] = []
    try:
        for path in root.glob("*/*/task.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(data, dict):
                tasks.append(data)
    except OSError:
        return []
    return tasks


def _default_executor_probe(
    platform: str,
    executor_config: dict[str, Any],
) -> dict[str, Any]:
    """Read-only binary discovery and --version probe; never starts an Executor."""
    from .executor_registry import get_executor
    from .path_provider import find_binary

    configured_command = executor_config.get("command")
    extra = (
        [configured_command]
        if isinstance(configured_command, str) and configured_command.strip()
        else None
    )
    try:
        discovery = find_binary(platform, extra_paths=extra)
    except Exception:  # noqa: BLE001 - discovery failure is reported, not raised.
        discovery = {"found": False, "source": "not_found"}
    resolved = bool(discovery.get("found"))
    source = str(discovery.get("source") or "not_found")
    probe_state = "skipped"
    version = None
    if resolved:
        binary = Path(str(discovery.get("path") or "")).expanduser()
        probe_state = "failed"
        if binary.is_file():
            try:
                completed = subprocess.run(
                    [str(binary), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if completed.returncode == 0:
                    probe_state = "ok"
                    version = (completed.stdout or completed.stderr or "").strip()
                    if len(version) > _MAX_PROBE_VERSION_LENGTH:
                        version = version[:_MAX_PROBE_VERSION_LENGTH]
                    version = version or None
            except (OSError, subprocess.SubprocessError):
                probe_state = "failed"
    capability = _default_capability()
    if executor_config:
        try:
            executor = get_executor(platform, executor_config)
            caps = executor.capabilities()
            capability = {
                "level": int(caps.level),
                "structured_output": bool(caps.structured_output),
                "resume": bool(caps.resume),
                "cancel": bool(caps.cancel),
                "input_required": bool(caps.input_required),
            }
        except Exception:  # noqa: BLE001 - capability failure is reported, not raised.
            capability = _default_capability()
    auth = _default_auth(platform, executor_config)
    return {
        "resolved": resolved,
        "source": source,
        "version": version,
        "probe": probe_state,
        "auth": auth,
        "capability": capability,
    }


def _default_capability() -> dict[str, Any]:
    return {
        "level": 0,
        "structured_output": False,
        "resume": False,
        "cancel": False,
        "input_required": False,
    }


def _public_executor_probe(
    probe: Any,
    platform: str,
    executor_config: dict[str, Any],
) -> dict[str, Any]:
    """Project an Executor probe onto the fixed Doctor v2 public contract."""
    if not isinstance(probe, dict):
        return {
            "resolved": False,
            "source": "unavailable",
            "version": None,
            "probe": "unavailable",
            "auth": _default_auth(platform, executor_config),
            "capability": _default_capability(),
        }

    source = probe.get("source")
    source = source if type(source) is str and source in _EXECUTOR_SOURCES else "unavailable"
    probe_state = probe.get("probe")
    probe_state = (
        probe_state
        if type(probe_state) is str and probe_state in _EXECUTOR_PROBE_STATES
        else "unavailable"
    )
    raw_version = probe.get("version")
    version = None
    if type(raw_version) is str:
        lines = [line.strip() for line in raw_version.splitlines() if line.strip()]
        if len(lines) == 1 and lines[0].isprintable():
            version = lines[0][:_MAX_PROBE_VERSION_LENGTH]

    raw_auth = probe.get("auth")
    fallback_auth = _default_auth(platform, executor_config)
    if isinstance(raw_auth, dict):
        key_env = raw_auth.get("key_env")
        if type(key_env) is not str or not _SAFE_ENV_NAME.fullmatch(key_env):
            key_env = fallback_auth["key_env"]
        auth = {
            "key_env": key_env,
            "configured": raw_auth.get("configured") is True,
            "present": raw_auth.get("present") is True,
        }
    else:
        auth = fallback_auth

    raw_capability = probe.get("capability")
    capability = _default_capability()
    if isinstance(raw_capability, dict):
        level = raw_capability.get("level")
        capability = {
            "level": level if type(level) is int and 0 <= level <= 4 else 0,
            "structured_output": raw_capability.get("structured_output") is True,
            "resume": raw_capability.get("resume") is True,
            "cancel": raw_capability.get("cancel") is True,
            "input_required": raw_capability.get("input_required") is True,
        }

    return {
        "resolved": probe.get("resolved") is True,
        "source": source,
        "version": version,
        "probe": probe_state,
        "auth": auth,
        "capability": capability,
    }


def _default_auth(platform: str, executor_config: dict[str, Any]) -> dict[str, Any]:
    configured_env = (executor_config or {}).get("api_key_env")
    if type(configured_env) is not str or not _SAFE_ENV_NAME.fullmatch(configured_env):
        configured_env = None
    key_env = (
        configured_env
        if configured_env
        else _DEFAULT_AUTH_ENV.get(platform, "")
    )
    present = bool(os.environ.get(key_env)) if key_env else False
    return {
        "key_env": key_env,
        "configured": bool(key_env),
        "present": present,
    }


def _default_skill_roots() -> dict[str, Path]:
    from .setup import _claude_skill_path, _codex_skill_root, _hermes_skill_destinations

    return {
        "codex": _codex_skill_root(),
        "claude": _claude_skill_path().parent,
        "hermes": _hermes_skill_destinations(all_profiles=True)[0].parent,
    }


def _default_skill_current_files() -> dict[str, dict[str, bytes]]:
    from .setup import _current_skill_files

    return {platform: _current_skill_files(platform) for platform in _EXECUTOR_PLATFORMS}


def _default_runner_spool() -> Path:
    from .runner import default_runner_spool

    return default_runner_spool()


def _doctor_board_root(config: dict[str, Any]) -> Path:
    configured = config.get("board_root") if isinstance(config, dict) else None
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return resolve_workspace_root(config) / "record"


def _safe_collect(
    name: str,
    collector: Callable[[], tuple[dict[str, Any], list[dict[str, str]]]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        return collector()
    except Exception:  # noqa: BLE001 - collector isolation safety net.
        return (
            {
                "status": "warning",
                "reason": (
                    f"The {name} collector failed to complete; the failure was contained."
                ),
                "remediation": "Re-run doctor and check AgentBC logs for the failure.",
            },
            [
                {
                    "id": f"{name}.collector",
                    "status": "warning",
                    "message": (
                        f"The {name} collector failed without emitting a stable diagnostic."
                    ),
                }
            ],
        )


def _pending_is_stale(receipt: Any, now: datetime) -> bool:
    if not isinstance(receipt, dict):
        return False
    timestamp = receipt.get("last_attempt_at") or receipt.get("requested_at")
    occurred = _parse_timestamp(timestamp)
    return occurred is not None and (now - occurred).total_seconds() > 300


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_label(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if _SAFE_NAME.fullmatch(text) else default


def _overall_status(checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if "unavailable" in statuses:
        return "unavailable"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _read_build_info(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None, "invalid"
    if not isinstance(value, dict) or not _BUILD_INFO_FIELDS.issubset(value):
        return None, "invalid"
    if value.get("schema_version") != BUILD_INFO_SCHEMA_VERSION:
        return None, "invalid"
    if not isinstance(value.get("package_version"), str):
        return None, "invalid"
    if not isinstance(value.get("commit_sha"), str) or not _COMMIT_SHA.fullmatch(
        value["commit_sha"]
    ):
        return None, "invalid"
    if not isinstance(value.get("source_tree_sha256"), str) or not _SHA256.fullmatch(
        value["source_tree_sha256"]
    ):
        return None, "invalid"
    build_source = value.get("build_source")
    if not isinstance(build_source, str) or not _SAFE_NAME.fullmatch(build_source):
        return None, "invalid"
    if not isinstance(value.get("built_at_utc"), str) or not value["built_at_utc"]:
        return None, "invalid"
    return value, "valid"


def _read_direct_url(distribution: Any) -> dict[str, Any] | None:
    try:
        raw = distribution.read_text("direct_url.json")
        value = json.loads(raw) if raw else None
    except (AttributeError, OSError, TypeError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("url"), str):
        return None
    return value


def _installed_distribution() -> Any | None:
    try:
        return metadata.distribution("agentbc")
    except metadata.PackageNotFoundError:
        return None


def _default_candidate_marker_paths(
    module_path: Path, executable_path: Path
) -> list[Path]:
    paths = [
        Path(sys.prefix) / ".agentbc-candidate",
        Path(sys.prefix) / "agentbc-candidate.json",
        module_path.with_name("_candidate.json"),
        executable_path.parent.parent / ".agentbc-candidate",
        executable_path.parent.parent.parent / ".agentbc-candidate",
    ]
    configured = os.environ.get("AGENTBC_CANDIDATE_MARKER")
    if configured:
        paths.insert(0, Path(configured).expanduser())
    return list(dict.fromkeys(paths))


def _find_source_checkout(module_path: Path) -> Path | None:
    start = module_path.parent if module_path.suffix else module_path
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists():
            return candidate
    return None


def _git_commit_sha(checkout_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout_root), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return (
        value.lower()
        if result.returncode == 0 and _COMMIT_SHA.fullmatch(value)
        else None
    )


def _source_tree_sha256(checkout_root: Path) -> str | None:
    """Hash tracked working-tree paths and bytes without changing the checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout_root), "ls-files", "-z"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    digest = hashlib.sha256()
    try:
        entries = sorted(value for value in result.stdout.split(b"\0") if value)
        for raw_path in entries:
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            path = checkout_root / relative
            content = (
                os.readlink(path).encode("utf-8", errors="surrogateescape")
                if path.is_symlink()
                else path.read_bytes()
            )
            digest.update(raw_path)
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def _package_module_path() -> Path:
    return Path(__file__).with_name("__init__.py")


def _cli_executable_path() -> Path:
    raw = Path(sys.argv[0]).expanduser()
    if raw.is_file():
        return raw
    discovered = shutil.which("agentbc")
    return Path(discovered) if discovered else Path(sys.executable)


def _public_identity_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return str(_resolved_path(value))


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _text_value(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def _bool_text(value: Any) -> str:
    return str(bool(value)).lower()


# Blocker detection includes active task states plus the needs_recovery terminal.
_BLOCKER_TASK_STATUSES = frozenset(ACTIVE_STATUSES) | {"needs_recovery"}
