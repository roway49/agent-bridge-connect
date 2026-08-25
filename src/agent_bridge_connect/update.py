"""Verified Alpha self-update and legacy cutover gate (PERM-103-005/UPD-103-001).

``agentbc update`` first checks the GitHub release manifest without writing
locally.  A newer Alpha is installed only after explicit ``y``/``yes``, a
strict legacy-permission gate, release/asset hash verification, staged smoke,
and current/new CLI-Runner-Skill identity checks.  The managed CLI link switches
atomically; an internal recovery path restores the old CLI, skills, and Runner
on failure.  Homebrew-owned links remain under Homebrew control.

The 1.0.3A cutover gate retains these rules:

- while any old-channel task is ``pending`` / ``running`` /
  ``input_required`` / ``needs_recovery``, an unconsumed one-shot grant
  exists, or a legacy permission marker is waiting, the preflight returns
  ``legacy_permission_cutover_blocked`` together with per-task evidence;
- only an explicitly cleared old version (every non-terminal task
  terminated or completed in the old version, all grants consumed or
  revoked, no pending markers) produces ``cutover-ready``.  The preflight
  records a durable board-scoped stamp so the supported path is auditable
  and repeatable.

A manual wheel/bundle install that bypasses the supported preflight enters
cutover maintenance mode via :func:`manual_bypass_install`; maintenance
permits ``doctor`` / ``status`` / ``report`` and explicit termination only
and blocks task creation and dispatch until the board is cleared and a
supported update completes.

This module never issues or consumes permission grants, never detects
permission markers outside the cutover gate, and never rewrites terminal
history or the original ``agentbc.permission`` extensions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__
from .config import DEFAULT_BOARD_ROOT
from .migration import (
    LEGACY_CUTOVER_BLOCKED,
    enter_maintenance_mode,
    exit_maintenance_mode,
    is_maintenance_mode,
    legacy_permission_cutover_blocked,
)
from .protocol import ABCError

CUTOVER_READY_STATE = "cutover-ready"
CUTOVER_BLOCKED_STATE = "legacy_permission_cutover_blocked"
MAINTENANCE_STATE = "legacy_permission_cutover_maintenance"
CUTOVER_READY_FILE = ".agentbc-cutover-ready"
DEFAULT_RELEASES_URL = (
    "https://api.github.com/repos/roway49/agent-bridge-connect/releases?per_page=20"
)
UPDATE_CHANNEL = "alpha"
MAX_UPDATE_RESPONSE_BYTES = 2 * 1024 * 1024
_PACKAGE_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?$"
)

_UPDATE_NOTE = (
    "create/dispatch blocked until the old channel is cleared and a "
    "supported update completes"
)

_TARGET_SKILL_PATH_QUERY = """\
import json
from pathlib import Path

from agent_bridge_connect import setup
from agent_bridge_connect.skill_packages import LEGACY_SKILL_FINGERPRINTS, MANIFEST_NAME


def _root(value):
    path = Path(value).expanduser()
    if path.name in {"SKILL.md", ".agentbc-skill.json"}:
        return path.parent
    return path


def _entry(platform, value):
    files = set(setup._current_skill_files(platform))
    files.update(LEGACY_SKILL_FINGERPRINTS[platform]["files"])
    files.add(MANIFEST_NAME)
    return {
        "root": str(_root(value)),
        "files": sorted(files),
    }


print(
    json.dumps(
        {
            "codex": _entry("codex", setup._codex_skill_root()),
            "claude": _entry("claude", setup._claude_skill_path()),
            "hermes": [
                _entry("hermes", value)
                for value in setup._hermes_skill_destinations(all_profiles=True)
            ],
        },
        sort_keys=True,
    )
)
"""


def update_preflight(service: Any) -> dict[str, Any]:
    """Run the supported ``agentbc update`` preflight.

    The gate scans every board task and blocks with
    ``legacy_permission_cutover_blocked`` and per-task evidence while any
    old-channel activity remains.  When the old version is explicitly
    cleared the preflight records the durable ``cutover-ready`` stamp and
    exits cutover maintenance mode if a manual bypass had entered it.
    """
    maintenance_active = is_maintenance_mode(service)
    gate = legacy_permission_cutover_blocked(service)
    if gate["blocked"]:
        return {
            "state": CUTOVER_BLOCKED_STATE,
            "code": LEGACY_CUTOVER_BLOCKED,
            "cutover_ready": False,
            "blockers": gate["blockers"],
            "maintenance_active": maintenance_active,
        }
    stamp = record_cutover_ready(service)
    if maintenance_active:
        exit_maintenance_mode(service)
    return {
        "state": CUTOVER_READY_STATE,
        "code": "",
        "cutover_ready": True,
        "blockers": [],
        "maintenance_active": False,
        "stamp": stamp,
    }


def check_for_update(
    *,
    releases_url: str | None = None,
    fetch_bytes: Any | None = None,
) -> dict[str, Any]:
    """Resolve and verify the newest Alpha release without writing locally."""
    fetch = fetch_bytes or _fetch_bytes
    url = releases_url or os.environ.get("AGENTBC_UPDATE_INDEX_URL") or DEFAULT_RELEASES_URL
    try:
        releases = json.loads(fetch(url).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ABCError("update_index_invalid", "Unable to read the release index") from exc
    if not isinstance(releases, list):
        raise ABCError("update_index_invalid", "Release index must be a list")

    candidates: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = str(release.get("tag_name") or "")
        package_version = _tag_to_package_version(tag)
        if package_version is None:
            continue
        candidates.append((_version_key(package_version), release))
    if not candidates:
        raise ABCError("update_release_unavailable", "No verifiable Alpha release was found")
    _key, release = max(candidates, key=lambda item: item[0])
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    manifest_asset = _asset_named(assets, "release-manifest.json")
    manifest_bytes = fetch(str(manifest_asset["browser_download_url"]))
    _verify_asset_digest(manifest_asset, manifest_bytes, "release manifest")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ABCError("update_manifest_invalid", "Release manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ABCError("update_manifest_invalid", "Release manifest must be an object")
    latest = str(manifest.get("package_version") or "")
    if _version_key(latest) != _key or str(manifest.get("tag") or "") != str(release.get("tag_name") or ""):
        raise ABCError("update_manifest_invalid", "Release tag and package version do not match")
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    wheels = [item for item in artifacts if isinstance(item, dict) and str(item.get("filename") or "").endswith(".whl")]
    if len(wheels) != 1:
        raise ABCError("update_manifest_invalid", "Release manifest must declare exactly one wheel")
    wheel_record = wheels[0]
    wheel_asset = _asset_named(assets, str(wheel_record.get("filename") or ""))
    expected_sha = str(wheel_record.get("sha256") or "").lower()
    asset_digest = str(wheel_asset.get("digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or asset_digest != f"sha256:{expected_sha}":
        raise ABCError("update_manifest_invalid", "Wheel digest is not consistently pinned")
    current_key = _version_key(__version__)
    return {
        "state": "update_available" if _key > current_key else "current",
        "code": "",
        "channel": UPDATE_CHANNEL,
        "current": __version__,
        "latest": latest,
        "update_available": _key > current_key,
        "source": "github_release_manifest",
        "release_url": str(release.get("html_url") or ""),
        "summary": _bounded_summary(str(release.get("body") or "")),
        "wheel": {
            "filename": str(wheel_record["filename"]),
            "sha256": expected_sha,
            "url": str(wheel_asset["browser_download_url"]),
        },
    }


def run_update_flow(
    service: Any | None,
    *,
    service_factory: Callable[[], Any] | None = None,
    input_fn: Any | None = None,
    output_fn: Any | None = None,
    checker: Any | None = None,
    installer: Any | None = None,
) -> dict[str, Any]:
    """Check, confirm, preflight and atomically switch a managed install."""
    read_input = input_fn or input
    write_output = output_fn or print
    strategy = _local_install_strategy() if installer is None else None
    if strategy is not None and strategy["method"] == "homebrew":
        write_output("Homebrew-managed install: run `brew upgrade agentbc`.")
        return {
            "state": "homebrew_update_required",
            "code": "",
            "channel": UPDATE_CHANNEL,
            "current": __version__,
            "source": "homebrew",
            "updated": False,
            "upgrade_command": "brew upgrade agentbc",
        }
    available = (checker or check_for_update)()
    write_output(
        f"AgentBC update: current={available['current']} latest={available['latest']} "
        f"channel={available['channel']} source={available['source']}"
    )
    if not available["update_available"]:
        return available
    if available.get("summary"):
        write_output(f"Summary: {available['summary']}")
    if installer is None:
        assert strategy is not None
        if strategy["method"] != "managed":
            raise ABCError("update_install_unsupported", str(strategy["reason"]))
    try:
        answer = str(read_input("Upgrade? [y/N] ") or "").strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        return {**available, "state": "update_declined", "updated": False}
    if service is None:
        if service_factory is None:
            raise ValueError("service or service_factory is required for update preflight")
        service = service_factory()
    preflight = update_preflight(service)
    if not preflight["cutover_ready"]:
        return preflight
    apply_update = installer or install_verified_release
    installed = apply_update(available)
    return {
        **available,
        "state": "updated",
        "updated": True,
        "preflight": preflight,
        "install": installed,
    }


def install_verified_release(release: dict[str, Any]) -> dict[str, Any]:
    """Install and cut over the managed CLI as one CLI/Skill/Runner transaction."""
    wheel = release["wheel"]
    strategy = _local_install_strategy()
    if strategy["method"] == "homebrew":
        raise ABCError(
            "update_homebrew_managed",
            "Homebrew-managed AgentBC must be upgraded with `brew upgrade agentbc`",
        )
    if strategy["method"] != "managed":
        raise ABCError(
            "update_install_unsupported",
            str(strategy["reason"]),
        )
    install_root = strategy["install_root"]
    bin_dir = strategy["bin_dir"]
    target = strategy["target"]
    current_identity = _require_current_install_identity(target, release)
    old_link = os.readlink(target) if target.is_symlink() else ""
    old_version = str(release.get("current") or "")
    if isinstance(current_identity, dict):
        package = current_identity.get("package")
        if isinstance(package, dict) and package.get("version"):
            old_version = str(package["version"])
    version = str(release["latest"])
    transaction = uuid.uuid4().hex[:12]
    new_venv = install_root / "versions" / f"{version}-{transaction}"
    base_python = getattr(sys, "_base_executable", None) or sys.executable
    try:
        with tempfile.TemporaryDirectory(prefix="agentbc-update-") as temporary:
            wheel_path = Path(temporary) / str(wheel["filename"])
            wheel_bytes = _fetch_bytes(str(wheel["url"]))
            if hashlib.sha256(wheel_bytes).hexdigest() != str(wheel["sha256"]):
                raise ABCError("update_wheel_hash_mismatch", "Downloaded wheel hash does not match manifest")
            wheel_path.write_bytes(wheel_bytes)
            new_venv.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([base_python, "-m", "venv", str(new_venv)], check=True)
            new_python = new_venv / "bin" / "python"
            new_cli = new_venv / "bin" / "agentbc"
            subprocess.run(
                [str(new_python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
                check=True,
                env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            )
            smoke = subprocess.run([str(new_cli), "--version"], text=True, capture_output=True, check=True)
            if version not in smoke.stdout:
                raise ABCError("update_smoke_failed", "Staged CLI reported the wrong version")
    except ABCError:
        shutil.rmtree(new_venv, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(new_venv, ignore_errors=True)
        raise ABCError("update_stage_failed", "Staged install failed; active CLI was not changed") from exc
    bin_dir.mkdir(parents=True, exist_ok=True)
    temporary_link = bin_dir / f".agentbc-update-{transaction}"
    snapshot: dict[str, Any] | None = None
    transaction_started = False
    try:
        target_skill_specs = _target_managed_skill_specs(new_python)
        snapshot = _capture_skill_snapshot(
            current_identity,
            target_skill_specs=target_skill_specs,
        )
        if temporary_link.exists() or temporary_link.is_symlink():
            raise ABCError(
                "update_transaction_unsafe",
                f"Refusing to reuse an existing transaction link: {temporary_link}",
            )
        temporary_link.symlink_to(new_cli)
        from .runner import stop_runner_background

        # From this point on a Runner stop may have happened even if the stop
        # helper reports an error, so every exception must enter rollback.
        transaction_started = True
        stopped = stop_runner_background()
        if not stopped.get("ok"):
            raise ABCError("update_runner_stop_failed", "The current Runner could not be stopped safely")
        os.replace(temporary_link, target)
        subprocess.run(
            [str(new_cli), "setup", "--update", "--non-interactive"],
            check=True,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            [str(new_cli), "runner", "start"],
            check=True,
            text=True,
            capture_output=True,
        )
        _require_post_update_identity(new_cli, version)
    except Exception as exc:
        if temporary_link.is_symlink():
            temporary_link.unlink()
        if not transaction_started or snapshot is None:
            shutil.rmtree(new_venv, ignore_errors=True)
            if isinstance(exc, ABCError):
                raise
            raise ABCError(
                "update_transaction_prepare_failed",
                "Update transaction could not be prepared; active CLI was not changed",
            ) from exc
        rollback_ok, _rollback_reason = _rollback_update_transaction(
            target=target,
            bin_dir=bin_dir,
            old_link=old_link,
            old_version=old_version,
            snapshot=snapshot,
            transaction=transaction,
        )
        shutil.rmtree(new_venv, ignore_errors=True)
        if rollback_ok:
            raise ABCError(
                "update_install_failed",
                "Update failed; previous CLI, skills, and Runner were restored",
            ) from exc
        raise ABCError(
            "update_rollback_incomplete",
            "Update failed and automatic recovery was incomplete; run `agentbc doctor --json`",
        ) from exc
    return {
        "version": version,
        "cli": str(target),
        "venv": str(new_venv),
        "runner_refreshed": True,
        "previous_preserved": bool(old_link),
    }


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "agentbc-update"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(MAX_UPDATE_RESPONSE_BYTES + 1)
    if len(payload) > MAX_UPDATE_RESPONSE_BYTES:
        raise ABCError("update_response_too_large", "Update response exceeds the size limit")
    return payload


def _require_current_install_identity(target: Path, release: dict[str, Any]) -> dict[str, Any]:
    report = _doctor_report(target)
    package = report.get("package") if isinstance(report.get("package"), dict) else {}
    runner = report.get("runner") if isinstance(report.get("runner"), dict) else {}
    if package.get("version") != release.get("current") or package.get("status") == "unavailable":
        raise ABCError(
            "update_identity_mismatch",
            "Current CLI package identity is not valid for this update",
        )
    if runner.get("status") != "ready" or runner.get("identity") != "match":
        raise ABCError(
            "update_identity_mismatch",
            "Current CLI and Runner identities must match before updating",
        )
    drifted = _drifted_skill_identities(report)
    if drifted:
        raise ABCError(
            "update_skill_identity_mismatch",
            "Installed managed skills are not current for the active CLI: "
            + ", ".join(drifted),
        )
    return report


def _require_post_update_identity(target: Path, version: str) -> None:
    report = _doctor_report(target)
    package = report.get("package") if isinstance(report.get("package"), dict) else {}
    runner = report.get("runner") if isinstance(report.get("runner"), dict) else {}
    if package.get("version") != version or package.get("status") == "unavailable":
        raise ABCError("update_identity_mismatch", "Updated CLI package identity is invalid")
    if runner.get("status") != "ready" or runner.get("identity") != "match":
        raise ABCError("update_identity_mismatch", "Updated CLI and Runner identities do not match")
    drifted = _drifted_skill_identities(report)
    if drifted:
        raise ABCError(
            "update_skill_identity_mismatch",
            "Updated managed skills are not version-matched: " + ", ".join(drifted),
        )


def _drifted_skill_identities(report: dict[str, Any]) -> list[str]:
    """Return every reported installed Skill that is not current.

    Doctor versions may report Hermes profiles in addition to the aggregate
    platform entry.  Walking the small identity tree keeps the update gate
    compatible with both shapes without changing the doctor schema.
    """
    skills = report.get("skills") if isinstance(report.get("skills"), dict) else {}
    drifted: list[str] = []
    for path, entry in _iter_skill_identity_entries(skills):
        if entry.get("installed") is True and entry.get("up_to_date") is not True:
            drifted.append(path)
    return drifted


def _iter_skill_identity_entries(value: Any, path: str = "skills"):
    if isinstance(value, dict):
        if "installed" in value or "up_to_date" in value:
            yield path, value
        for key, child in value.items():
            if isinstance(child, (dict, list, tuple)):
                child_path = f"{path}.{key}"
                yield from _iter_skill_identity_entries(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_skill_identity_entries(child, f"{path}[{index}]")


def _capture_skill_snapshot(
    report: Any,
    *,
    target_skill_specs: list[tuple[str, Path, list[str]]] | None = None,
) -> dict[str, Any]:
    """Capture only AgentBC-managed Skill files before the CLI cutover."""
    try:
        specs = _managed_skill_specs(report)
        target_paths: dict[tuple[str, str], set[str]] = {}
        seen = {(platform, str(root)) for platform, root in specs}
        for platform, root, paths in target_skill_specs or []:
            if platform not in {"codex", "claude", "hermes"}:
                raise ABCError("update_skill_snapshot_unsafe", "Unknown target Skill platform")
            root = _absolute_skill_root(root)
            _assert_safe_skill_root(root)
            normalized = {
                _validate_managed_relative_path(path)
                for path in paths
            }
            key = (platform, str(root))
            target_paths.setdefault(key, set()).update(normalized)
            if key not in seen:
                seen.add(key)
                specs.append((platform, root))
        entries: list[dict[str, Any]] = []
        for platform, root in specs:
            _assert_safe_skill_root(root)
            paths = set(_managed_skill_paths(platform))
            paths.update(target_paths.get((platform, str(root)), set()))
            paths.update(_manifest_declared_paths(root, platform))
            files: dict[str, bytes | None] = {}
            for relative_path in sorted(paths):
                path = _safe_skill_path(root, relative_path)
                if path.exists():
                    try:
                        files[relative_path] = path.read_bytes()
                    except OSError as exc:
                        raise ABCError(
                            "update_skill_snapshot_failed",
                            f"Unable to snapshot managed Skill file: {path}",
                        ) from exc
                else:
                    files[relative_path] = None
            entries.append(
                {
                    "platform": platform,
                    "root": str(root),
                    "files": files,
                }
            )
        return {"entries": entries}
    except ABCError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ABCError(
            "update_skill_snapshot_unsafe",
            "Managed Skill paths could not be snapshotted safely",
        ) from exc


def _target_managed_skill_specs(target_python: Path) -> list[tuple[str, Path, list[str]]]:
    """Read the staged release's complete managed Skill path set before setup."""
    try:
        completed = subprocess.run(
            [str(target_python), "-c", _TARGET_SKILL_PATH_QUERY],
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict) or set(payload) != {"codex", "claude", "hermes"}:
            raise ValueError("target Skill path query returned an incomplete platform set")

        specs: list[tuple[str, Path, list[str]]] = []
        for platform in ("codex", "claude"):
            values = payload[platform]
            if not isinstance(values, dict):
                raise ValueError("target Skill path query returned an invalid platform entry")
            entries = [values]
            for entry in entries:
                root_value = entry.get("root")
                files = entry.get("files")
                if not isinstance(root_value, str) or not isinstance(files, list) or not files:
                    raise ValueError("target Skill path query returned an invalid path set")
                root = _absolute_skill_root(root_value)
                if any(part in {".", ".."} for part in root.parts):
                    raise ValueError("target Skill path query returned an unsafe root")
                normalized = sorted(
                    {_validate_managed_relative_path(path) for path in files}
                )
                _assert_safe_skill_root(root)
                specs.append((platform, root, normalized))

        hermes_values = payload["hermes"]
        if not isinstance(hermes_values, list) or not hermes_values:
            raise ValueError("target Skill path query returned no Hermes profiles")
        for entry in hermes_values:
            if not isinstance(entry, dict):
                raise ValueError("target Skill path query returned an invalid Hermes profile")
            root_value = entry.get("root")
            files = entry.get("files")
            if not isinstance(root_value, str) or not isinstance(files, list) or not files:
                raise ValueError("target Skill path query returned an invalid profile path set")
            root = _absolute_skill_root(root_value)
            if any(part in {".", ".."} for part in root.parts):
                raise ValueError("target Skill path query returned an unsafe profile root")
            normalized = sorted(
                {_validate_managed_relative_path(path) for path in files}
            )
            _assert_safe_skill_root(root)
            specs.append(("hermes", root, normalized))
        return specs
    except ABCError as exc:
        raise ABCError(
            "update_skill_snapshot_unsafe",
            "Target-version managed Skill paths could not be determined safely",
        ) from exc
    except (OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError) as exc:
        raise ABCError(
            "update_skill_snapshot_unsafe",
            "Target-version managed Skill paths could not be determined safely",
        ) from exc


def _managed_skill_specs(report: Any) -> list[tuple[str, Path]]:
    from .setup import (
        _claude_skill_path,
        _codex_skill_root,
        _hermes_skill_destinations,
    )

    specs: list[tuple[str, Path]] = [
        ("codex", _skill_root(_codex_skill_root())),
        ("claude", _skill_root(_claude_skill_path())),
    ]
    specs.extend(
        ("hermes", _skill_root(path))
        for path in _hermes_skill_destinations(all_profiles=True)
    )

    skills = report.get("skills") if isinstance(report, dict) else None
    if isinstance(skills, dict):
        for platform in ("codex", "claude", "hermes"):
            if platform in skills:
                for value in _reported_skill_paths(skills[platform]):
                    specs.append((platform, _skill_root(value)))

    unique: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for platform, root in specs:
        absolute = _absolute_skill_root(root)
        key = (platform, str(absolute))
        if key not in seen:
            seen.add(key)
            unique.append((platform, absolute))
    return unique


def _absolute_skill_root(value: str | Path) -> Path:
    root = _skill_root(value).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.absolute()


def _reported_skill_paths(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key in ("root", "path", "skill_path"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                yield candidate
        for key in ("paths", "profiles", "roots", "profile_paths"):
            child = value.get(key)
            if isinstance(child, (dict, list, tuple)):
                yield from _reported_skill_paths(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _reported_skill_paths(child)


def _skill_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if root.name in {"SKILL.md", ".agentbc-skill.json"}:
        root = root.parent
    return root


def _managed_skill_paths(platform: str) -> list[str]:
    from .setup import _current_skill_files
    from .skill_packages import LEGACY_SKILL_FINGERPRINTS, MANIFEST_NAME

    paths = set(_current_skill_files(platform))
    paths.update(LEGACY_SKILL_FINGERPRINTS[platform]["files"])
    paths.add(MANIFEST_NAME)
    return sorted(_validate_managed_relative_path(path) for path in paths)


def _validate_managed_relative_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ABCError("update_skill_snapshot_unsafe", f"Unsafe managed Skill path: {relative_path!r}")
    return relative_path


def _assert_safe_skill_root(root: Path) -> None:
    if root.is_symlink():
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill root is a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill root is not a directory: {root}")


def _safe_skill_path(root: Path, relative_path: str) -> Path:
    _validate_managed_relative_path(relative_path)
    _assert_safe_skill_root(root)
    current = root
    parts = PurePosixPath(relative_path).parts
    for component in parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill parent is a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ABCError(
                "update_skill_snapshot_unsafe",
                f"Managed Skill parent is not a directory: {current}",
            )
    path = root / relative_path
    if path.is_symlink():
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill path is a symlink: {path}")
    if path.exists() and not path.is_file():
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill path is not a file: {path}")
    return path


def _manifest_declared_paths(root: Path, platform: str) -> list[str]:
    from .skill_packages import MANIFEST_NAME

    manifest_path = _safe_skill_path(root, MANIFEST_NAME)
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill manifest is unreadable: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("platform") != platform:
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill manifest has the wrong platform: {manifest_path}")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ABCError("update_skill_snapshot_unsafe", f"Managed Skill manifest has invalid files: {manifest_path}")
    return [_validate_managed_relative_path(path) for path in files]


def _augment_snapshot_from_current_manifests(snapshot: dict[str, Any]) -> None:
    """Include valid target-manifest paths so newly introduced managed files are removed."""
    from .skill_packages import MANIFEST_NAME

    for entry in snapshot.get("entries", []):
        root = Path(entry["root"])
        platform = str(entry["platform"])
        paths = entry["files"]
        _assert_safe_skill_root(root)
        manifest_path = _safe_skill_path(root, MANIFEST_NAME)
        if not manifest_path.exists():
            continue
        declared = _manifest_declared_paths(root, platform)
        for relative_path in declared:
            _safe_skill_path(root, relative_path)
            paths.setdefault(relative_path, None)


def _rollback_update_transaction(
    *,
    target: Path,
    bin_dir: Path,
    old_link: str,
    old_version: str,
    snapshot: dict[str, Any],
    transaction: str,
) -> tuple[bool, str]:
    """Stop the target Runner, restore bytes/link, then prove the old identity."""
    try:
        from .runner import stop_runner_background

        stopped = stop_runner_background()
        if not stopped.get("ok"):
            return False, "target Runner could not be stopped during rollback"
        if not old_link:
            return False, "the previous managed CLI link is unavailable"
        _augment_snapshot_from_current_manifests(snapshot)
        _restore_cli_link(target, bin_dir, old_link, transaction)
        _restore_skill_snapshot(snapshot, transaction)
        old_cli = target.resolve(strict=False)
        restored_runner = subprocess.run(
            [str(old_cli), "runner", "start"],
            check=False,
            text=True,
            capture_output=True,
        )
        if getattr(restored_runner, "returncode", None) != 0:
            return False, "the previous Runner did not start successfully"
        _verify_cli_link(target, old_link)
        _verify_skill_snapshot(snapshot)
        _require_post_update_identity(old_cli, old_version)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - rollback must fail closed.
        return False, str(exc)


def _restore_cli_link(target: Path, bin_dir: Path, old_link: str, transaction: str) -> None:
    if target.exists() and not target.is_symlink():
        raise ABCError("update_rollback_incomplete", f"Managed CLI target is not a symlink: {target}")
    rollback_link = bin_dir / f".agentbc-rollback-{transaction}"
    if rollback_link.exists() or rollback_link.is_symlink():
        raise ABCError("update_rollback_incomplete", f"Rollback link already exists: {rollback_link}")
    rollback_link.symlink_to(old_link)
    try:
        os.replace(rollback_link, target)
    finally:
        rollback_link.unlink(missing_ok=True)


def _verify_cli_link(target: Path, old_link: str) -> None:
    if not target.is_symlink() or os.readlink(target) != old_link:
        raise ABCError("update_rollback_incomplete", "The previous managed CLI link was not restored exactly")


def _restore_skill_snapshot(snapshot: dict[str, Any], transaction: str) -> None:
    for entry in snapshot.get("entries", []):
        root = Path(entry["root"])
        _assert_safe_skill_root(root)
        files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
        for relative_path in sorted(files):
            _safe_skill_path(root, relative_path)
        for relative_path in sorted(files):
            expected = files[relative_path]
            path = _safe_skill_path(root, relative_path)
            if expected is None:
                if path.exists() or path.is_symlink():
                    # ``_safe_skill_path`` has already rejected symlinks and
                    # non-files, so this removes only a regular managed file
                    # that did not exist in the pre-cutover snapshot.
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.agentbc-restore-{transaction}")
            if temporary.exists() or temporary.is_symlink():
                raise ABCError("update_rollback_incomplete", f"Rollback file already exists: {temporary}")
            try:
                temporary.write_bytes(expected)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        _prune_empty_skill_directories(root, files)


def _prune_empty_skill_directories(root: Path, files: dict[str, bytes | None]) -> None:
    parents: set[Path] = set()
    for relative_path in files:
        parent = (root / relative_path).parent
        while parent != root and _is_relative_to(parent, root):
            parents.add(parent)
            parent = parent.parent
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _verify_skill_snapshot(snapshot: dict[str, Any]) -> None:
    for entry in snapshot.get("entries", []):
        root = Path(entry["root"])
        _assert_safe_skill_root(root)
        files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
        for relative_path, expected in files.items():
            path = _safe_skill_path(root, relative_path)
            if expected is None:
                if path.exists() or path.is_symlink():
                    raise ABCError("update_rollback_incomplete", f"Introduced managed Skill path remains: {path}")
                continue
            if not path.exists() or path.read_bytes() != expected:
                raise ABCError("update_rollback_incomplete", f"Managed Skill bytes were not restored: {path}")


def _doctor_report(target: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(target), "doctor", "--json"],
            check=False,
            text=True,
            capture_output=True,
        )
        report = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise ABCError("update_identity_unavailable", "Unable to verify AgentBC identity") from exc
    if not isinstance(report, dict):
        raise ABCError("update_identity_unavailable", "AgentBC doctor returned an invalid report")
    return report


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_homebrew_cellar_path(path: Path) -> bool:
    parts = path.parts
    return any(
        parts[index] == "Cellar" and parts[index + 1] == "agentbc"
        for index in range(len(parts) - 1)
    )


def _local_install_strategy() -> dict[str, Any]:
    install_root = Path(
        os.environ.get("AGENTBC_ALPHA_HOME", Path.home() / ".agentbc-alpha")
    ).expanduser().resolve()
    bin_dir = Path(
        os.environ.get("AGENTBC_BIN_DIR", Path.home() / ".local" / "bin")
    ).expanduser().resolve()
    target = bin_dir / "agentbc"
    invoked = _invoked_cli_path()
    common = {"install_root": install_root, "bin_dir": bin_dir, "target": target}
    runtime_python = Path(sys.executable).expanduser().resolve(strict=False)
    if _is_homebrew_cellar_path(runtime_python):
        return {
            **common,
            "method": "homebrew",
            "reason": "Homebrew owns the running Python environment",
        }
    if invoked is not None and _is_homebrew_cellar_path(invoked.resolve(strict=False)):
        return {**common, "method": "homebrew", "reason": "Homebrew owns the running CLI"}
    if target.exists() and not target.is_symlink():
        return {
            **common,
            "method": "unsupported",
            "reason": f"Refusing to replace non-symlink CLI: {target}",
        }
    if not target.is_symlink():
        return {
            **common,
            "method": "unsupported",
            "reason": (
                "Self-update requires an AgentBC-managed CLI link; "
                "reinstall the Alpha bundle first"
            ),
        }
    current_target = target.resolve(strict=False)
    if _is_homebrew_cellar_path(current_target):
        return {**common, "method": "homebrew", "reason": "Homebrew owns the CLI link"}
    if not _is_relative_to(current_target, install_root):
        return {
            **common,
            "method": "unsupported",
            "reason": f"Refusing to replace CLI link outside the AgentBC install root: {target}",
        }
    if invoked is not None and invoked.resolve(strict=False) != current_target:
        return {
            **common,
            "method": "unsupported",
            "reason": "The running AgentBC CLI is not the managed update target",
        }
    return {**common, "method": "managed", "reason": ""}


def _invoked_cli_path() -> Path | None:
    raw = Path(sys.argv[0]).expanduser()
    if raw.name != "agentbc":
        return None
    if raw.is_absolute():
        return raw
    resolved = shutil.which(str(raw))
    return Path(resolved) if resolved else None


def _asset_named(assets: list[Any], name: str) -> dict[str, Any]:
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or not str(matches[0].get("browser_download_url") or "").startswith("https://"):
        raise ABCError("update_manifest_invalid", f"Release asset is missing or ambiguous: {name}")
    return matches[0]


def _verify_asset_digest(asset: dict[str, Any], payload: bytes, label: str) -> None:
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or hashlib.sha256(payload).hexdigest() != digest.removeprefix("sha256:"):
        raise ABCError("update_manifest_hash_mismatch", f"{label} hash does not match release metadata")


def _version_key(value: str) -> tuple[int, ...]:
    matched = _PACKAGE_VERSION_RE.fullmatch(value)
    if matched is None:
        raise ABCError("update_version_invalid", f"Unsupported package version: {value}")
    pre = matched.group("pre")
    pre_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[pre]
    return (
        int(matched.group("major")), int(matched.group("minor")), int(matched.group("patch")),
        pre_rank, int(matched.group("pre_n") or 0),
    )


def _tag_to_package_version(tag: str) -> str | None:
    matched = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)A", tag)
    if matched is None:
        return None
    major, minor, patch = matched.groups()
    return f"{major}.{minor}.{patch}a1"


def _bounded_summary(value: str) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= 240 else compact[:239] + "…"


def record_cutover_ready(service: Any) -> dict[str, Any]:
    """Persist the durable board-scoped ``cutover-ready`` stamp.

    The stamp records the installed version and the moment the explicitly
    cleared old version passed the supported preflight.  It never touches
    task records, terminal history, or the global config.
    """
    board = _board_root(service)
    board.mkdir(parents=True, exist_ok=True)
    stamp = {
        "state": CUTOVER_READY_STATE,
        "installed_version": __version__,
        "cutover_ready": True,
        "cleared_at": _utc_now(),
    }
    stamp_path = board / CUTOVER_READY_FILE
    stamp_path.write_text(
        json.dumps(stamp, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dict(stamp)


def cutover_ready_stamp(service: Any) -> dict[str, Any] | None:
    """Return the durable stamp when the supported cutover already ran."""
    board = _board_root(service)
    stamp_path = board / CUTOVER_READY_FILE
    if not stamp_path.is_file():
        return None
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("cutover_ready") is not True:
        return None
    return payload


def manual_bypass_install(
    service: Any,
    reason: str = "manual wheel/bundle bypass",
) -> dict[str, Any]:
    """Enter cutover maintenance mode for a manual install that skipped preflight.

    A hand-installed wheel or bundle is not a supported update: the board
    drops into maintenance mode which permits ``doctor`` / ``status`` /
    ``report`` and explicit termination only and blocks create/dispatch
    until the old channel is cleared and a supported update completes.
    """
    view = enter_maintenance_mode(service, reason=reason)
    return {
        "state": MAINTENANCE_STATE,
        "code": MAINTENANCE_STATE,
        "maintenance": view,
        "note": _UPDATE_NOTE,
    }


def assert_update_maintenance_allowed(service: Any, command: str) -> None:
    """Fail closed inside maintenance: raise for anything but the allowed set.

    Used by new-task / new-runtime entry points that must not mutate the
    board while the cutover is unresolved.
    """
    from .migration import assert_maintenance_command_allowed

    assert_maintenance_command_allowed(service, command)


def _board_root(service: Any) -> Path:
    board = Path(getattr(service, "board_root", "") or DEFAULT_BOARD_ROOT)
    return board.expanduser().resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
