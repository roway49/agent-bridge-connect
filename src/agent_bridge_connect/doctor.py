"""Read-only build, configuration, and Runner identity diagnostics."""

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


SCHEMA_VERSION = 1
BUILD_INFO_SCHEMA_VERSION = 1
_BUILD_INFO_FIELDS = {
    "schema_version",
    "package_version",
    "commit_sha",
    "source_tree_sha256",
    "build_source",
    "built_at_utc",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DISTRIBUTION_UNSET = object()


def build_doctor_report(
    *,
    config_path: str | Path | None = None,
    runner_health: Callable[[], dict[str, Any]] | None = None,
    module_path: str | Path | None = None,
    executable_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    distribution: Any = _DISTRIBUTION_UNSET,
    candidate_marker_paths: list[str | Path] | None = None,
    build_info_path: str | Path | None = None,
    board_root: str | Path | None = None,
    cleanup_tasks: list[dict[str, Any]] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Collect the stable public doctor contract without changing local state."""
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

    config, config_error, loaded_config = _collect_config(config_path)
    runner = _collect_runner(runner_health)
    cleanup = (
        build_session_cleanup_diagnostics(cleanup_tasks, now=now)
        if cleanup_tasks is not None
        else collect_session_cleanup_diagnostics(
            board_root or _doctor_board_root(loaded_config),
            now=now,
        )
    )
    checks = _build_checks(
        build_info=build_info,
        build_info_state=build_info_state,
        package_version=__version__,
        install_source=install_source,
        config=config,
        config_error=config_error,
        runner=runner,
        cleanup=cleanup,
        current_python=current_python,
        current_module=current_module,
    )
    status = _overall_status(checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status != "error",
        "status": status,
        "package": {
            "version": __version__,
            "commit_sha": commit_sha,
            "source_tree_sha256": source_tree_sha256,
            "build_source": build_source,
            "module_path": str(current_module),
            "executable_path": str(current_executable),
            "install_source": install_source,
        },
        "config": config,
        "runner": runner,
        "session_cleanup": cleanup,
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
    package = report["package"]
    config = report["config"]
    runner = report["runner"]
    cleanup = report["session_cleanup"]
    executor_text = ", ".join(runner["executors"]) or "-"
    lines = [
        f"AgentBC doctor: {str(report['status']).upper()}",
        f"ok: {str(bool(report['ok'])).lower()}",
        f"schema_version: {report['schema_version']}",
        "Package:",
        f"  version: {_text_value(package['version'])}",
        f"  commit_sha: {_text_value(package['commit_sha'])}",
        f"  source_tree_sha256: {_text_value(package['source_tree_sha256'])}",
        f"  build_source: {_text_value(package['build_source'])}",
        f"  module_path: {_text_value(package['module_path'])}",
        f"  executable_path: {_text_value(package['executable_path'])}",
        f"  install_source: {_text_value(package['install_source'])}",
        "Config:",
        f"  path: {_text_value(config['path'])}",
        f"  exists: {str(bool(config['exists'])).lower()}",
        f"  workspace_root: {_text_value(config['workspace_root'])}",
        "Runner:",
        f"  status: {_text_value(runner['status'])}",
        f"  pid: {_text_value(runner['pid'])}",
        f"  python_executable: {_text_value(runner['python_executable'])}",
        f"  module_path: {_text_value(runner['module_path'])}",
        f"  executors: {executor_text}",
        "Session cleanup:",
        f"  status: {_text_value(cleanup['status'])}",
        f"  warnings: {cleanup['warnings']}",
    ]
    for diagnostic in cleanup["diagnostics"]:
        lines.append(
            "  "
            f"[{str(diagnostic['status']).upper()}] "
            f"{diagnostic['task_id']} ({diagnostic['executor']}): "
            f"capability={diagnostic['capability']} "
            f"state={diagnostic['state']} "
            f"attempts={diagnostic['attempts']} "
            f"error_code={diagnostic['error_code'] or '-'} "
            f"retryable={str(bool(diagnostic['retryable'])).lower()} - "
            f"{diagnostic['message']}"
        )
    lines.extend([
        "Checks:",
    ])
    for check in report["checks"]:
        lines.append(
            f"  [{str(check['status']).upper()}] {check['id']}: {check['message']}"
        )
    return "\n".join(lines)


def _collect_config(
    config_path: str | Path | None,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
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
    return (
        {
            "path": str(path),
            "exists": exists,
            "workspace_root": str(workspace_root),
        },
        error,
        loaded,
    )


def collect_session_cleanup_diagnostics(
    board_root: str | Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Read AgentBC task receipts without touching Executor session storage."""
    root = Path(board_root).expanduser().resolve()
    if not root.is_dir():
        return build_session_cleanup_diagnostics([], now=now)
    try:
        from .task_store import TaskStore

        tasks = TaskStore(root).list_tasks()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        tasks = []
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


def _doctor_board_root(config: dict[str, Any]) -> Path:
    configured = config.get("board_root") if isinstance(config, dict) else None
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return resolve_workspace_root(config) / "record"


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


def _collect_runner(
    runner_health: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any]:
    if runner_health is None:
        from .runner import RunnerClient

        runner_health = RunnerClient().health
    try:
        health = runner_health()
    except Exception:  # noqa: BLE001 - doctor reports unavailability without leaking details.
        health = None
    if (
        not isinstance(health, dict)
        or not health.get("ok")
        or health.get("status") != "ready"
    ):
        return {
            "status": "unavailable",
            "pid": None,
            "python_executable": None,
            "module_path": None,
            "executors": [],
        }
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
    return {
        "status": "ready",
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "python_executable": python_path,
        "module_path": module_path,
        "executors": executors,
    }


def _build_checks(
    *,
    build_info: dict[str, Any] | None,
    build_info_state: str,
    package_version: str,
    install_source: str,
    config: dict[str, Any],
    config_error: str | None,
    runner: dict[str, Any],
    cleanup: dict[str, Any],
    current_python: Path,
    current_module: Path,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if build_info_state == "valid":
        if str(build_info.get("package_version")) == package_version:
            checks.append(
                _check(
                    "package.build_identity",
                    "healthy",
                    "Packaged build identity is valid.",
                )
            )
        else:
            checks.append(
                _check(
                    "package.build_identity",
                    "error",
                    "Packaged build identity version does not match the runtime package.",
                )
            )
    elif build_info_state == "invalid":
        checks.append(
            _check(
                "package.build_identity",
                "error",
                "Packaged build identity is invalid; provenance cannot be trusted.",
            )
        )
    else:
        checks.append(
            _check(
                "package.build_identity",
                "warning",
                "Packaged build identity is unavailable; safe runtime fallbacks are in use.",
            )
        )

    if install_source == "unknown":
        checks.append(
            _check(
                "package.install_source",
                "warning",
                "Install source could not be determined.",
            )
        )
    else:
        checks.append(
            _check(
                "package.install_source",
                "healthy",
                f"Install source classified as {install_source}.",
            )
        )

    if config_error is not None:
        checks.append(
            _check("config.load", "error", "Configuration is unreadable or invalid.")
        )
    elif config["exists"]:
        checks.append(
            _check("config.load", "healthy", "Configuration loaded successfully.")
        )
    else:
        checks.append(
            _check(
                "config.load",
                "warning",
                "Configuration file is absent; defaults are in use.",
            )
        )

    workspace = Path(config["workspace_root"])
    if workspace.exists() and not workspace.is_dir():
        checks.append(
            _check(
                "config.workspace",
                "error",
                "Configured workspace root is not a directory.",
            )
        )
    elif workspace.is_dir():
        checks.append(
            _check("config.workspace", "healthy", "Workspace root is available.")
        )
    else:
        checks.append(
            _check("config.workspace", "warning", "Workspace root does not exist yet.")
        )

    if runner["status"] != "ready":
        checks.append(
            _check(
                "runner.availability",
                "warning",
                "Runner is unavailable; no changes were attempted.",
            )
        )
        checks.append(
            _check(
                "runner.identity", "warning", "Runner identity could not be compared."
            )
        )
    else:
        checks.append(_check("runner.availability", "healthy", "Runner is ready."))
        runner_python = runner["python_executable"]
        runner_module = runner["module_path"]
        if runner_python is None or runner_module is None:
            checks.append(
                _check(
                    "runner.identity",
                    "error",
                    "Runner did not provide complete runtime identity.",
                )
            )
        else:
            drift: list[str] = []
            if _resolved_path(runner_python) != current_python:
                drift.append("Python interpreter")
            if _resolved_path(runner_module) != current_module:
                drift.append("AgentBC module")
            if drift:
                checks.append(
                    _check(
                        "runner.identity",
                        "error",
                        f"CLI/Runner drift detected for {' and '.join(drift)}.",
                    )
                )
            else:
                checks.append(
                    _check(
                        "runner.identity", "healthy", "CLI and Runner identities match."
                    )
                )
    if cleanup["warnings"]:
        checks.append(
            _check(
                "session.cleanup",
                "warning",
                f"{cleanup['warnings']} executor session cleanup warning(s) require attention.",
            )
        )
    else:
        checks.append(
            _check(
                "session.cleanup",
                "healthy",
                "No executor session cleanup warnings were found.",
            )
        )
    return checks


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


def _check(check_id: str, status: str, message: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "message": message}


def _overall_status(checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _text_value(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)
