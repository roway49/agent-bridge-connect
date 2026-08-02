#!/usr/bin/env python3
"""Build provenance: identity generation, version mapping, and release validation.

Usage:
  python3 scripts/build_provenance.py tag-to-version v1.0.1A
  python3 scripts/build_provenance.py version-to-tag 1.0.1a1
  python3 scripts/build_provenance.py generate-build-info --repo-root . --build-source local
  python3 scripts/build_provenance.py generate-manifest --repo-root . --dist-dir dist
  python3 scripts/build_provenance.py validate --repo-root . --tag v1.0.1A
  python3 scripts/build_provenance.py validate-dists --repo-root . --dist-dir dist
  python3 scripts/build_provenance.py print-package-version --repo-root .
  python3 scripts/build_provenance.py print-product-version --repo-root .
  python3 scripts/build_provenance.py discover-dists --dist-dir dist
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0"

# ── version-mapping helpers ──────────────────────────────────────────────


def _parse_product_tag(tag: str) -> tuple[int, int, int, str]:
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)([A-Z])$", tag)
    if not m:
        raise ValueError(f"tag does not match vX.Y.ZS pattern (S uppercase): {tag!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).lower())


def tag_to_python_version(tag: str) -> str:
    """Convert release tag vX.Y.ZS to Python version X.Y.Zs1.

    >>> tag_to_python_version("v1.0.1A")
    '1.0.1a1'
    >>> tag_to_python_version("v2.3.4B")
    '2.3.4b1'
    """
    major, minor, patch, suffix = _parse_product_tag(tag)
    return f"{major}.{minor}.{patch}{suffix}1"


def _parse_python_pre(version: str) -> tuple[int, int, int, str]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)([a-z])1$", version)
    if not m:
        raise ValueError(f"version does not match X.Y.Zs1 pattern: {version!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).upper())


def python_to_tag_version(version: str) -> str:
    """Convert Python version X.Y.Zs1 to release tag vX.Y.ZS.

    >>> python_to_tag_version("1.0.1a1")
    'v1.0.1A'
    """
    major, minor, patch, suffix = _parse_python_pre(version)
    return f"v{major}.{minor}.{patch}{suffix}"


# ── package-metadata helpers ─────────────────────────────────────────────


def _find_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def get_package_version(repo_root: Path) -> str:
    init_path = repo_root / "src" / "agent_bridge_connect" / "__init__.py"
    content = init_path.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not m:
        raise ValueError(f"could not find __version__ in {init_path}")
    return m.group(1)


def get_commit_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo_root, check=True,
    )
    return result.stdout.strip()


def get_source_tree_sha256(repo_root: Path) -> str:
    """Compute a deterministic SHA-256 over all git-tracked file contents.

    Every tracked file is hashed individually, then the sorted sequence of
    ``path \\0 hexsha \\n`` entries is hashed to produce the final digest.
    Because the hash iterates over ``git ls-files`` sorted output it is
    independent of checkout order, platform newline conversion, and build
    directory noise.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True, cwd=repo_root, check=True,
    )
    files = sorted(
        p for p in result.stdout.decode(errors="replace").split("\0") if p
    )
    h = hashlib.sha256()
    for rel in files:
        fp = repo_root / rel
        if not fp.is_file():
            continue
        file_sha = hashlib.sha256(fp.read_bytes()).hexdigest()
        h.update(f"{rel}\0{file_sha}\n".encode())
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── build-info generation ────────────────────────────────────────────────


def generate_build_info(repo_root: Path, build_source: str = "local") -> dict:
    package_version = get_package_version(repo_root)
    commit_sha = get_commit_sha(repo_root)
    source_tree_sha256 = get_source_tree_sha256(repo_root)
    built_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "package_version": package_version,
        "commit_sha": commit_sha,
        "source_tree_sha256": source_tree_sha256,
        "build_source": build_source,
        "built_at_utc": built_at_utc,
    }


def write_build_info(repo_root: Path, build_source: str = "local") -> Path:
    info = generate_build_info(repo_root, build_source)
    target = repo_root / "src" / "agent_bridge_connect" / "_build_info.json"
    target.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return target


# ── release-manifest generation ──────────────────────────────────────────


def discover_dists(dist_dir: Path) -> tuple[Path, Path]:
    """Return ``(wheel_path, sdist_path)`` — exactly one of each."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1:
        raise ValueError(
            f"expected exactly 1 wheel, found {len(wheels)}: "
            f"{[w.name for w in wheels]}"
        )
    if len(sdists) != 1:
        raise ValueError(
            f"expected exactly 1 sdist, found {len(sdists)}: "
            f"{[s.name for s in sdists]}"
        )
    return wheels[0], sdists[0]


def generate_release_manifest(
    repo_root: Path,
    dist_dir: Path,
    tag: str | None = None,
) -> dict:
    package_version = get_package_version(repo_root)
    commit_sha = get_commit_sha(repo_root)
    source_tree_sha256 = get_source_tree_sha256(repo_root)
    if tag is None:
        tag = python_to_tag_version(package_version)
    wheel_path, sdist_path = discover_dists(dist_dir)
    artifacts: list[dict] = []
    for art in (wheel_path, sdist_path):
        artifacts.append({
            "filename": art.name,
            "size": art.stat().st_size,
            "sha256": file_sha256(art),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "tag": tag,
        "package_version": package_version,
        "commit_sha": commit_sha,
        "source_tree_sha256": source_tree_sha256,
        "artifacts": artifacts,
    }


def write_release_manifest(
    repo_root: Path,
    dist_dir: Path,
    tag: str | None = None,
) -> Path:
    manifest = generate_release_manifest(repo_root, dist_dir, tag)
    target = dist_dir / "release-manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


# ── provenance validation ────────────────────────────────────────────────


def validate_provenance(repo_root: Path, tag: str | None = None) -> dict:
    """Check tag ↔ package-version ↔ commit consistency.

    When *tag* is supplied the check is strict: HEAD must carry the tag, the
    mapped Python version must equal ``__version__``, and the working tree
    must be clean.
    """
    package_version = get_package_version(repo_root)
    commit_sha = get_commit_sha(repo_root)
    errors: list[str] = []
    warnings: list[str] = []

    if tag:
        try:
            mapped = tag_to_python_version(tag)
            if mapped != package_version:
                errors.append(
                    f"tag {tag!r} maps to version {mapped!r}, "
                    f"but package version is {package_version!r}"
                )
        except ValueError as exc:
            errors.append(str(exc))

        result = subprocess.run(
            ["git", "tag", "--points-at", "HEAD"],
            capture_output=True, text=True, cwd=repo_root,
        )
        tags_at_head = [
            t for t in result.stdout.strip().split("\n") if t
        ]
        if tag not in tags_at_head:
            errors.append(
                f"HEAD ({commit_sha[:8]}) is not tagged with {tag!r}; "
                f"tags at HEAD: {tags_at_head}"
            )

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.stdout.strip():
        errors.append("working tree is not clean")

    info_path = repo_root / "src" / "agent_bridge_connect" / "_build_info.json"
    if not info_path.exists():
        warnings.append("_build_info.json not found; run generate-build-info first")

    return {
        "valid": len(errors) == 0,
        "tag": tag,
        "package_version": package_version,
        "commit_sha": commit_sha,
        "errors": errors,
        "warnings": warnings,
    }


def validate_dist_filenames(dist_dir: Path, package_version: str) -> dict:
    try:
        wheel, sdist = discover_dists(dist_dir)
    except ValueError as exc:
        return {"valid": False, "errors": [str(exc)]}

    errors: list[str] = []
    expected_wheel_prefix = f"agentbc-{package_version}-"
    if not wheel.name.startswith(expected_wheel_prefix):
        errors.append(
            f"wheel {wheel.name!r} does not start with "
            f"expected prefix {expected_wheel_prefix!r}"
        )
    expected_sdist = f"agentbc-{package_version}.tar.gz"
    if sdist.name != expected_sdist:
        errors.append(
            f"sdist {sdist.name!r} does not match expected {expected_sdist!r}"
        )
    return {"valid": len(errors) == 0, "errors": errors}


# ── CLI ──────────────────────────────────────────────────────────────────


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Build provenance — identity, manifest, and validation",
    )
    sp = parser.add_subparsers(dest="command", required=True)

    p = sp.add_parser("tag-to-version", help="v1.0.1A → 1.0.1a1")
    p.add_argument("tag")

    p = sp.add_parser("version-to-tag", help="1.0.1a1 → v1.0.1A")
    p.add_argument("version")

    p = sp.add_parser("print-package-version", help="print __version__")
    p.add_argument("--repo-root", default=".")

    p = sp.add_parser("print-product-version", help="print version as vX.Y.ZS")
    p.add_argument("--repo-root", default=".")

    p = sp.add_parser("generate-build-info")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--build-source", default="local")

    p = sp.add_parser("generate-manifest")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--dist-dir", default="dist")
    p.add_argument("--tag", default=None)

    p = sp.add_parser("validate")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--tag", default=None)

    p = sp.add_parser("validate-dists")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--dist-dir", default="dist")

    p = sp.add_parser("discover-dists")
    p.add_argument("--dist-dir", default="dist")

    args = parser.parse_args()

    if args.command == "tag-to-version":
        print(tag_to_python_version(args.tag))

    elif args.command == "version-to-tag":
        print(python_to_tag_version(args.version))

    elif args.command == "print-package-version":
        print(get_package_version(Path(args.repo_root).resolve()))

    elif args.command == "print-product-version":
        pv = get_package_version(Path(args.repo_root).resolve())
        print(python_to_tag_version(pv))

    elif args.command == "generate-build-info":
        rr = Path(args.repo_root).resolve()
        out = write_build_info(rr, args.build_source)
        print(f"_build_info.json written to {out}")

    elif args.command == "generate-manifest":
        rr = Path(args.repo_root).resolve()
        dd = Path(args.dist_dir).resolve()
        out = write_release_manifest(rr, dd, args.tag)
        print(f"release-manifest.json written to {out}")

    elif args.command == "validate":
        rr = Path(args.repo_root).resolve()
        result = validate_provenance(rr, args.tag)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if not result["valid"]:
            sys.exit(1)

    elif args.command == "validate-dists":
        rr = Path(args.repo_root).resolve()
        dd = Path(args.dist_dir).resolve()
        pv = get_package_version(rr)
        result = validate_dist_filenames(dd, pv)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if not result["valid"]:
            sys.exit(1)

    elif args.command == "discover-dists":
        dd = Path(args.dist_dir).resolve()
        whl, sdist = discover_dists(dd)
        print(f"wheel={whl}")
        print(f"sdist={sdist}")


if __name__ == "__main__":
    _main()
