from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


MANIFEST_NAME = ".agentbc-skill.json"
MANIFEST_SCHEMA_VERSION = 1
PROTOCOL_VERSION = "1.0"
COMPLETION_VERSION = 1

# Exact package fingerprints from private/integration@33d3d08.  These hashes
# intentionally remain constants: recomputing them from current templates would
# make an old, unmodified installation indistinguishable from user content.
LEGACY_SKILL_FINGERPRINTS: dict[str, dict[str, Any]] = {
    "codex": {
        "template_sha256": "ef0f073ef911e5fb0f8092fdda57c204db5dc24e565d9b8367d35830974afea1",
        "files": {
            "SKILL.md": "e54131af13c0d1eeefcbf4c148f1cfa7f21e9dc882dcc2c2bed81d7955781ec7",
            "agents/openai.yaml": "cf2d6ac45a6e204e8c673a569c2aca0a681f63ba31675d2c01fcd2d64173c380",
        },
    },
    "claude": {
        "template_sha256": "aaaa1fb45b9ef6a9c91b4435a0a22c5aedbc5beb89413f7c45e0f2b310ccc36d",
        "files": {
            "SKILL.md": "795ba7a32d59b838d0e97bd5b290488af998a554b5dead58b95d913fd2bed91a",
        },
    },
    "hermes": {
        "template_sha256": "381b9926e7a26cfba2b45c03b3797bd0683de3a022ac2848c3de8954c972668a",
        "files": {
            "SKILL.md": "bf73694965f842567df49b5918586849ad8ce3012b2782abb0e4686de40656af",
            "references/agentbc-steps-yaml.md": "ec2d398a422e7ae4787b7321a66a29aa77e6442ad1ed27f5b835007bdf8880dc",
        },
    },
}

# Frozen AgentBC-managed package fingerprints for historically released
# manifest-era versions.  Like LEGACY_SKILL_FINGERPRINTS these hashes are
# constants, not recomputed values: an installation is recognized as an
# intact older managed package only when the installed manifest platform,
# package version, protocol, path set, per-file hashes, aggregate hash, and
# the on-disk file hashes all match a shipped release exactly.  The 1.0.2a1
# fingerprints below were computed from the published 1.0.2a1 sdist
# (the artifact Homebrew formula agentbc--1.0.2a1.tar.gz is built from).
MANAGED_SKILL_FINGERPRINTS: dict[str, dict[str, dict[str, Any]]] = {
    "1.0.2a1": {
        "codex": {
            "protocol_version": "1.0",
            "completion_version": 1,
            "template_sha256": "bd6eebf95d63963eeafa4986705e010b92c41dde45878dbfb83ebb0bfc0c290e",
            "files": {
                "SKILL.md": "a4793a20c5a9e079a3a26c31bdccca366be59f97b225f137bc90645d6f649ec0",
                "agents/openai.yaml": "8f6cf84d2091c1ea1e895ec06f9d2321daceee34e5707cd9df3fc8a9142ca21a",
                "references/agentbc-steps-yaml.md": "ec2d398a422e7ae4787b7321a66a29aa77e6442ad1ed27f5b835007bdf8880dc",
                "references/controller-contract.md": "370af89b528469368c75f49b9986f004951397b240a1d9721d6598057665c38d",
            },
        },
        "claude": {
            "protocol_version": "1.0",
            "completion_version": 1,
            "template_sha256": "891b95db10659920d5c3b9f5e013ddf5803c476458ce75e53d4e58a9a6b953b8",
            "files": {
                "SKILL.md": "793e1c2c2ff04785f917f97d39decef3789a14a27d679f4ddf2789e598baf1db",
                "references/agentbc-steps-yaml.md": "ec2d398a422e7ae4787b7321a66a29aa77e6442ad1ed27f5b835007bdf8880dc",
                "references/controller-contract.md": "370af89b528469368c75f49b9986f004951397b240a1d9721d6598057665c38d",
            },
        },
        "hermes": {
            "protocol_version": "1.0",
            "completion_version": 1,
            "template_sha256": "bcfce4bfdc406d36670155256b48bd2ab1c32c83415b588a10da722f8bba26e1",
            "files": {
                "SKILL.md": "108723eb662cd22e78a475e48512dc2beae21fb079378be92fc3a26388521654",
                "references/agentbc-steps-yaml.md": "ec2d398a422e7ae4787b7321a66a29aa77e6442ad1ed27f5b835007bdf8880dc",
                "references/controller-contract.md": "370af89b528469368c75f49b9986f004951397b240a1d9721d6598057665c38d",
            },
        },
    },
}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def aggregate_template_sha256(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[relative_path])
        digest.update(b"\0")
    return digest.hexdigest()


def build_skill_manifest(
    platform: str,
    package_version: str,
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    normalized = _normalize_files(files)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "platform": platform,
        "package_version": package_version,
        "protocol_version": PROTOCOL_VERSION,
        "completion_version": COMPLETION_VERSION,
        "template_sha256": aggregate_template_sha256(normalized),
        "files": {
            path: sha256_bytes(normalized[path])
            for path in sorted(normalized)
        },
    }


def serialize_skill_manifest(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def classify_skill_package(
    root: Path,
    *,
    platform: str,
    package_version: str,
    current_files: Mapping[str, bytes],
) -> dict[str, Any]:
    """Classify an installed package without mutating it."""
    root = root.expanduser()
    normalized = _normalize_files(current_files)
    expected_manifest = build_skill_manifest(platform, package_version, normalized)
    legacy = LEGACY_SKILL_FINGERPRINTS[platform]
    known_paths = set(normalized) | set(legacy["files"])
    observed = _observed_hashes(root, known_paths)
    manifest_path = root / MANIFEST_NAME

    if manifest_path.exists() or manifest_path.is_symlink():
        installed_manifest, error = _read_manifest(manifest_path)
        if error is not None or installed_manifest != expected_manifest:
            classification = (
                "managed_outdated"
                if error is None
                and installed_manifest is not None
                and _matches_managed_fingerprint(platform, installed_manifest, observed)
                else "modified"
            )
        elif any(observed.get(path) != digest for path, digest in installed_manifest["files"].items()):
            classification = (
                "partial"
                if all(
                    path not in observed or observed[path] == digest
                    for path, digest in installed_manifest["files"].items()
                )
                else "modified"
            )
        else:
            classification = "current"
        return _classification_result(
            classification,
            installed_manifest,
            expected_manifest,
            known_paths,
            error,
        )

    if not observed:
        classification = "missing"
    elif _matches_layout(observed, legacy["files"], complete=True):
        classification = "legacy"
    elif _matches_layout(observed, expected_manifest["files"], complete=False) or _matches_layout(
        observed, legacy["files"], complete=False
    ):
        classification = "partial"
    else:
        classification = "modified"
    return _classification_result(
        classification,
        None,
        expected_manifest,
        known_paths,
        None,
    )


def replace_managed_skill_package(
    root: Path,
    *,
    platform: str,
    files: Mapping[str, bytes],
    manifest: Mapping[str, Any],
) -> None:
    """Transactionally replace AgentBC-managed files while preserving extras."""
    root = root.expanduser()
    normalized = _normalize_files(files)
    expected_manifest = build_skill_manifest(
        platform,
        str(manifest.get("package_version") or ""),
        normalized,
    )
    if dict(manifest) != expected_manifest:
        raise ValueError("manifest does not match the managed Skill package")
    recognized_paths = set(normalized) | set(LEGACY_SKILL_FINGERPRINTS[platform]["files"])
    target_files = {**normalized, MANIFEST_NAME: serialize_skill_manifest(manifest)}
    touched_paths = recognized_paths | set(target_files)
    temporary_root = root.with_name(f".{root.name}.agentbc-tmp-{os.getpid()}")
    backup_root = root.with_name(f".{root.name}.agentbc-backup-{os.getpid()}")
    root_existed = root.exists() or root.is_symlink()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise OSError(f"skill package root is not a directory: {root}")
    _require_regular_managed_targets(root, touched_paths)
    _remove_path(temporary_root)
    _remove_path(backup_root)
    temporary_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    root.mkdir(parents=True, exist_ok=True)

    backed_up: set[str] = set()
    mutated = False
    try:
        for relative_path in sorted(touched_paths):
            source = root / relative_path
            if source.exists() or source.is_symlink():
                _copy_path(source, backup_root / relative_path)
                backed_up.add(relative_path)
        for relative_path, content in target_files.items():
            staged = temporary_root / relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)

        mutated = True
        for relative_path in sorted(touched_paths - set(target_files), reverse=True):
            _remove_path(root / relative_path)
        install_order = [
            path for path in sorted(target_files) if path != MANIFEST_NAME
        ] + [MANIFEST_NAME]
        for relative_path in install_order:
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            elif destination.is_symlink():
                destination.unlink()
            _install_staged_file(temporary_root / relative_path, destination)
    except BaseException:
        if mutated:
            for relative_path in sorted(touched_paths, reverse=True):
                _remove_path(root / relative_path)
            for relative_path in sorted(backed_up):
                _copy_path(backup_root / relative_path, root / relative_path)
            _prune_empty_directories(root)
        if not root_existed:
            try:
                root.rmdir()
            except OSError:
                pass
        raise
    finally:
        _remove_path(temporary_root)
        _remove_path(backup_root)


def remove_managed_skill_package(
    root: Path,
    *,
    platform: str,
    package_version: str,
    current_files: Mapping[str, bytes],
) -> dict[str, Any]:
    """Remove only recognized or manifest-declared AgentBC package files."""
    state = classify_skill_package(
        root,
        platform=platform,
        package_version=package_version,
        current_files=current_files,
    )
    manifest_path = root / MANIFEST_NAME
    installed_manifest, error = _read_manifest(manifest_path)
    recognized_paths = set(_normalize_files(current_files)) | set(
        LEGACY_SKILL_FINGERPRINTS[platform]["files"]
    )
    if installed_manifest is not None and error is None and installed_manifest.get("platform") == platform:
        paths = set(installed_manifest["files"]) & recognized_paths
        remove_manifest = True
    elif state["classification"] in {"legacy", "partial", "current"}:
        paths = set(state["managed_paths"])
        remove_manifest = manifest_path.exists() or manifest_path.is_symlink()
    else:
        paths = set()
        remove_manifest = False

    targets = set(paths)
    if remove_manifest:
        targets.add(MANIFEST_NAME)
    _require_regular_managed_targets(root, targets)

    changed = False
    for relative_path in sorted(paths, reverse=True):
        path = root / relative_path
        if path.exists() or path.is_symlink():
            _remove_path(path)
            changed = True
    if remove_manifest and (manifest_path.exists() or manifest_path.is_symlink()):
        _remove_path(manifest_path)
        changed = True
    _prune_empty_directories(root)
    try:
        root.rmdir()
    except OSError:
        pass
    return {**state, "removed": changed, "changed": changed}


def _matches_managed_fingerprint(
    platform: str,
    manifest: Mapping[str, Any],
    observed: Mapping[str, str | None],
) -> bool:
    """True only for an exact, unmodified, AgentBC-known historical release.

    Every field of the installed manifest (platform, package version,
    protocol, completion, aggregate hash, path set, and per-file hashes) and
    every observed on-disk file hash must match the frozen fingerprint.
    Forged, unknown, user-edited, symlinked, or non-regular installations
    therefore remain ``modified`` and are never silently overwritten.
    """
    version = manifest.get("package_version")
    fingerprints = MANAGED_SKILL_FINGERPRINTS.get(
        str(version) if isinstance(version, str) else ""
    )
    if not fingerprints:
        return False
    fingerprint = fingerprints.get(platform)
    if not fingerprint:
        return False
    if manifest.get("platform") != platform:
        return False
    if manifest.get("protocol_version") != fingerprint.get("protocol_version"):
        return False
    if manifest.get("completion_version") != fingerprint.get("completion_version"):
        return False
    if manifest.get("template_sha256") != fingerprint.get("template_sha256"):
        return False
    declared = manifest.get("files")
    if not isinstance(declared, dict) or declared != fingerprint["files"]:
        return False
    return all(
        observed.get(path) == digest for path, digest in fingerprint["files"].items()
    )


def _classification_result(
    classification: str,
    installed_manifest: dict[str, Any] | None,
    expected_manifest: dict[str, Any],
    known_paths: set[str],
    manifest_error: str | None,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "installed": classification != "missing",
        "up_to_date": classification == "current",
        "manifest": installed_manifest,
        "expected_manifest": expected_manifest,
        "manifest_error": manifest_error,
        "managed_paths": sorted(known_paths),
    }


def _normalize_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for relative_path, content in files.items():
        path = _validate_relative_path(relative_path)
        if path == MANIFEST_NAME:
            raise ValueError("manifest must not be included in managed files")
        if not isinstance(content, bytes):
            raise TypeError(f"skill content must be bytes: {path}")
        normalized[path] = content
    return normalized


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid managed path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid managed path: {value!r}")
    return value


def _observed_hashes(root: Path, paths: set[str]) -> dict[str, str | None]:
    observed: dict[str, str | None] = {}
    for relative_path in paths:
        path = root / relative_path
        if not (path.exists() or path.is_symlink()):
            continue
        if path.is_symlink() or not path.is_file():
            observed[relative_path] = None
            continue
        try:
            observed[relative_path] = sha256_bytes(path.read_bytes())
        except OSError:
            observed[relative_path] = None
    return observed


def _matches_layout(
    observed: Mapping[str, str | None],
    layout: Mapping[str, str],
    *,
    complete: bool,
) -> bool:
    if complete and set(observed) != set(layout):
        return False
    return bool(observed) and set(observed) <= set(layout) and all(
        observed[path] == layout[path] for path in observed
    )


def _require_regular_managed_targets(root: Path, relative_paths: set[str]) -> None:
    """Fail before mutation when a managed path could escape or remove a directory."""
    for relative_path in sorted(relative_paths):
        normalized = _validate_relative_path(relative_path)
        current = root
        for component in PurePosixPath(normalized).parts[:-1]:
            current = current / component
            if current.is_symlink():
                raise OSError(f"managed path parent is a symlink: {normalized}")
            if current.exists() and not current.is_dir():
                raise OSError(f"managed path parent is not a directory: {normalized}")
        target = root / normalized
        if target.is_symlink():
            raise OSError(f"managed path is a symlink: {normalized}")
        if target.exists() and not target.is_file():
            raise OSError(f"managed path is not a regular file: {normalized}")


def _read_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not (path.exists() or path.is_symlink()):
        return None, "missing"
    if path.is_symlink() or not path.is_file():
        return None, "manifest_not_regular_file"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid_json"
    required = {
        "schema_version",
        "platform",
        "package_version",
        "protocol_version",
        "completion_version",
        "template_sha256",
        "files",
    }
    if not isinstance(value, dict) or set(value) != required:
        return None, "invalid_schema"
    if type(value.get("schema_version")) is not int or value["schema_version"] != MANIFEST_SCHEMA_VERSION:
        return None, "unsupported_schema_version"
    if not isinstance(value.get("platform"), str) or not value["platform"]:
        return None, "invalid_platform"
    if not isinstance(value.get("package_version"), str) or not value["package_version"]:
        return None, "invalid_package_version"
    if value.get("protocol_version") != PROTOCOL_VERSION:
        return None, "invalid_protocol_version"
    if type(value.get("completion_version")) is not int or value["completion_version"] != COMPLETION_VERSION:
        return None, "invalid_completion_version"
    if not _is_sha256(value.get("template_sha256")):
        return None, "invalid_template_sha256"
    files = value.get("files")
    if not isinstance(files, dict) or not files:
        return None, "invalid_files"
    try:
        paths = [_validate_relative_path(path) for path in files]
    except ValueError:
        return None, "invalid_file_path"
    if MANIFEST_NAME in paths or any(not _is_sha256(digest) for digest in files.values()):
        return None, "invalid_file_hash"
    ordered_files = {path: files[path] for path in sorted(files)}
    return {**value, "files": ordered_files}, None


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _install_staged_file(source: Path, destination: Path) -> None:
    source.replace(destination)


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _prune_empty_directories(root: Path) -> None:
    if not root.is_dir():
        return
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
