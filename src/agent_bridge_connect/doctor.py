"""Read-only build, configuration, Runner, storage, Skill, and Executor diagnostics.

ARCH-104-001 Slice C: this module keeps the stable public doctor surface --
collector ordering, aggregation, overall status, and JSON/text rendering.
Every read-only collector lives in ``doctor_collectors`` (the single
read-only collection owner) and is imported directly from there; the
import bindings below also serve as compatibility aliases for the names
that were previously defined in this module.
"""

from __future__ import annotations

import os  # noqa: F401  -- kept so test patches of doctor.os.access keep working (os is a process-wide singleton)
import sys  # noqa: F401  -- sys.executable fallback inside build_doctor_report
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__



from .doctor_collectors import (
    BUILD_INFO_SCHEMA_VERSION,
    _BLOCKER_TASK_STATUSES as _BLOCKER_TASK_STATUSES,
    _EXECUTOR_PLATFORMS,
    _apply_storage_severity as _apply_storage_severity,
    _auxiliary_cleanup_diagnostics as _auxiliary_cleanup_diagnostics,
    _claude_sdk_capability_projection as _claude_sdk_capability_projection,
    _cli_executable_path,
    _collect_blockers,
    _collect_claude_sdk_capability as _collect_claude_sdk_capability,
    _collect_config,
    _collect_executor_entry as _collect_executor_entry,
    _collect_executors,
    _collect_package,
    _collect_permission_runtime,
    _collect_runner,
    _collect_skill_entry as _collect_skill_entry,
    _collect_skills,
    _collect_storage,
    _default_auth as _default_auth,
    _default_candidate_marker_paths,
    _default_capability as _default_capability,
    _default_executor_probe as _default_executor_probe,
    _default_runner_spool,
    _default_skill_current_files as _default_skill_current_files,
    _default_skill_roots as _default_skill_roots,
    _doctor_board_root,
    _find_source_checkout,
    _git_commit_sha,
    _installed_distribution,
    _one_auxiliary_diagnostic as _one_auxiliary_diagnostic,
    _package_module_path,
    _parse_timestamp as _parse_timestamp,
    _path_permissions as _path_permissions,
    _pending_is_stale as _pending_is_stale,
    _public_executor_probe as _public_executor_probe,
    _public_identity_path as _public_identity_path,
    _read_build_info,
    _read_direct_url as _read_direct_url,
    _read_task_records as _read_task_records,
    _resolved_path,
    _runner_storage_permissions as _runner_storage_permissions,
    _safe_label as _safe_label,
    _source_tree_sha256,
    _spool_status as _spool_status,
    _token_file_metadata as _token_file_metadata,
    _unverified_storage_permissions as _unverified_storage_permissions,
    _write_capable as _write_capable,
    build_session_cleanup_diagnostics,
    collect_session_cleanup_diagnostics,
    detect_install_source,
)


__all__ = [
    "BUILD_INFO_SCHEMA_VERSION",
    "EXIT_CODE_BY_STATUS",
    "SCHEMA_VERSION",
    "build_doctor_report",
    "build_session_cleanup_diagnostics",
    "collect_session_cleanup_diagnostics",
    "detect_install_source",
    "render_doctor_text",
]



# Public doctor contract v2.  Status is frozen to healthy|warning|unavailable and
# the CLI exit code follows 0|1|2.  unavailable means a core execution-chain
# dependency is unusable; warning means a non-core or partial capability problem.


SCHEMA_VERSION = 2


EXIT_CODE_BY_STATUS = {"healthy": 0, "warning": 1, "unavailable": 2}


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
    permission_runtime, permission_runtime_checks = _safe_collect(
        "permission_runtime",
        lambda: _collect_permission_runtime(loaded_config),
    )
    checks = _build_checks(
        package_checks=package_checks,
        config_checks=config_checks,
        runner_checks=runner_checks,
        storage_checks=storage_checks,
        skills_checks=skills_checks,
        executors_checks=executors_checks,
        cleanup=cleanup,
        permission_runtime_checks=permission_runtime_checks,
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
        "permission_runtime": permission_runtime,
        "checks": checks,
    }


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
        strategy = diagnostic.get("strategy")
        verification = diagnostic.get("verification")
        commands = diagnostic.get("commands")
        detail = ""
        if strategy:
            detail += f" strategy={_text_value(strategy)}"
        if isinstance(verification, dict):
            cli = verification.get("cli") if isinstance(verification.get("cli"), dict) else {}
            desktop_backend = verification.get("desktop_backend") if isinstance(verification.get("desktop_backend"), dict) else {}
            desktop_live = verification.get("desktop_live") if isinstance(verification.get("desktop_live"), dict) else {}
            desktop = verification.get("desktop") if isinstance(verification.get("desktop"), dict) else {}
            detail += (
                f" cli={_text_value(cli.get('status'))}"
                f" desktop_backend={_text_value(desktop_backend.get('status'))}"
                f" desktop_live={_text_value(desktop_live.get('status'))}"
                f" desktop={_text_value(desktop.get('status'))}"
            )
        if isinstance(commands, dict) and commands:
            archive = commands.get("archive") if isinstance(commands.get("archive"), dict) else {}
            desktop_archive = commands.get("desktop_archive") if isinstance(commands.get("desktop_archive"), dict) else {}
            app_server_archive = commands.get("app_server_archive") if isinstance(commands.get("app_server_archive"), dict) else {}
            delete = commands.get("delete") if isinstance(commands.get("delete"), dict) else {}
            detail += (
                f" archive={_text_value(archive.get('status'))}"
                f" desktop_archive={_text_value(desktop_archive.get('status'))}"
                f" app_server_archive={_text_value(app_server_archive.get('status'))}"
                f" delete={_text_value(delete.get('status'))}"
            )
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
            f"{_text_value(diagnostic.get('message'))}{detail}"
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
    permission_runtime_checks: list[dict[str, str]],
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = [
        *package_checks,
        *config_checks,
        *runner_checks,
        *storage_checks,
        *skills_checks,
        *executors_checks,
        *permission_runtime_checks,
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


def _overall_status(checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if "unavailable" in statuses:
        return "unavailable"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _text_value(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def _bool_text(value: Any) -> str:
    return str(bool(value)).lower()
