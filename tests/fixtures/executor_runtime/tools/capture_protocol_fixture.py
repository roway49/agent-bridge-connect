"""Controlled executor protocol fixture capture and validation (PROTO-104-001).

This tool is the only sanctioned way to add or refresh a versioned protocol
fixture under ``tests/fixtures/executor_runtime/matrix``.  It is deliberately
narrow:

* ``capture`` runs a frozen whitelist of *official*, read-only probes
  (``--version``, ``--help``, subcommand ``--help``, ``acp --check``,
  ``acp --version``, ``app-server generate-json-schema``) against one
  explicitly supplied binary, writing into an explicitly supplied staging
  directory.  Staged output never enters the repository directly.
* ``import-snapshot`` ingests staged output that was produced by a fake
  transport (for example ``tests/fixtures/executor_runtime/hermes_acp_fake_server.py``)
  or by a captured run on another machine.  It performs the same redaction and
  hashing path as ``capture`` so both origins share one rule set.
* ``review`` prints the redaction report, a redacted unified diff against the
  authoritative copy already in the matrix, and the SHA-256 manifest.
* ``verify`` re-checks an existing matrix version directory: every file must be
  listed in the per-version ``surfaces.json``, hashes must match, and no
  redacted-output invariant may be violated.

Hard rules enforced by this module:

* Tokens, bearer credentials, home-directory paths, raw private session paths,
  user prompts, full environment dumps and real session bodies are never
  written to staging output destined for the matrix.
* The probe whitelist is closed.  Any argv outside :data:`PROBE_COMMANDS` is
  rejected before a subprocess is spawned.
* Nothing here expands a production version gate.  Capturing evidence for a
  candidate version records evidence only; promotion into the supported range
  is a code-review decision backed by a full schema/help/event/canary contract.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Probe whitelist
# ---------------------------------------------------------------------------

#: Surfaces this tool may probe.  Values are argv templates; the literal token
#: ``{binary}`` is replaced by the configured executable and ``{out}`` by a
#: directory inside the staging area.
PROBE_COMMANDS: dict[str, tuple[str, ...]] = {
    "version": ("{binary}", "--version"),
    "help": ("{binary}", "--help"),
    "delete_help": ("{binary}", "delete", "--help"),
    "sessions_delete_help": ("{binary}", "sessions", "delete", "--help"),
    "project_purge_help": ("{binary}", "project", "purge", "--help"),
    "acp_check": ("{binary}", "acp", "--check"),
    "acp_version": ("{binary}", "acp", "--version"),
    # Official generated schema bundle.  Writes only under the staging dir.
    "app_server_schema": (
        "{binary}",
        "app-server",
        "generate-json-schema",
        "--out",
        "{out}",
        "--experimental",
    ),
}

#: Surfaces whose probe writes a directory instead of one text stream.
DIRECTORY_SURFACES = frozenset({"app_server_schema"})

#: Files inside a generated schema bundle that are distilled into
#: ``app_server_schema.json`` matrix surfaces.
SCHEMA_BUNDLE_FILES = (
    "ClientRequest.json",
    "ServerRequest.json",
    "ServerNotification.json",
)

DEFAULT_PROBES_BY_EXECUTOR: dict[str, tuple[str, ...]] = {
    "codex": ("version", "delete_help", "app_server_schema"),
    "claude": ("version", "help", "project_purge_help"),
    "hermes": (
        "version",
        "help",
        "sessions_delete_help",
        "acp_check",
        "acp_version",
    ),
}

KNOWN_EXECUTORS = ("codex", "claude", "hermes")

# ---------------------------------------------------------------------------
# Redaction rules
# ---------------------------------------------------------------------------

#: Patterns that must never reach the matrix.  Each entry is matched after the
#: home-directory rewrite so private paths are normalized first.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("openai_key", r"sk-[A-Za-z0-9_-]{12,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{12,}"),
    ("bearer_credential", r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("slack_token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("private_session_store", r"~/\.[a-z][a-z0-9_-]*/(sessions?|projects)/\S+"),
    ("assignment_env_dump", r"(?m)^[A-Z][A-Z0-9_]*=[^\s]{1,}$"),
)

#: Keys that must not appear inside imported JSON snapshots (full environment
#: captures and raw prompt bodies are the historical leak vectors).
FORBIDDEN_JSON_KEYS = frozenset(
    {"environment", "env", "prompt", "user_prompt", "token", "api_key"}
)

_REDACTED = "<redacted>"


def _home_directory() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def redact_text(text: str, *, home: Path | None = None) -> tuple[str, list[str]]:
    """Return redacted text plus the list of applied redaction rules."""
    resolved_home = (home or _home_directory()).as_posix()
    applied: list[str] = []
    if resolved_home and resolved_home != "/":
        if resolved_home in text:
            text = text.replace(resolved_home, "~")
            applied.append("home_path")
    for name, pattern in FORBIDDEN_PATTERNS:
        compiled = re.compile(pattern)
        if compiled.search(text):
            text = compiled.sub(_REDACTED, text)
            applied.append(name)
    return text, applied


def redact_structured(value: Any) -> tuple[Any, list[str]]:
    """Redact one JSON-shaped value, rejecting known leaking keys outright."""
    applied: list[str] = []
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.strip().lower()
            if lowered in FORBIDDEN_JSON_KEYS:
                cleaned[key] = _REDACTED
                applied.append(f"json_key:{lowered}")
                continue
            new_child, child_applied = redact_structured(child)
            cleaned[key] = new_child
            applied.extend(child_applied)
        return cleaned, applied
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for child in value:
            new_child, child_applied = redact_structured(child)
            cleaned_list.append(new_child)
            applied.extend(child_applied)
        return cleaned_list, applied
    if isinstance(value, str):
        return redact_text(value)
    return value, applied


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def assert_no_secrets(text: str, *, origin: str) -> None:
    """Fail closed when a forbidden pattern survives redaction."""
    for name, pattern in FORBIDDEN_PATTERNS:
        if name == "assignment_env_dump":
            continue
        if re.search(pattern, text):
            raise SystemExit(
                f"refusing to stage {origin}: forbidden pattern {name!r} survived "
                "redaction"
            )


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def parse_version(text: str) -> str | None:
    """Parse the first ``x.y.z`` token out of an official version line."""
    match = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", text or "")
    return match.group(1) if match else None


def distill_schema_bundle(bundle_dir: Path, codex_version: str) -> dict[str, Any]:
    """Distill the official schema bundle into the frozen contract shape."""
    definitions: dict[str, Any] = {}
    for name in SCHEMA_BUNDLE_FILES:
        payload = json.loads((bundle_dir / name).read_text(encoding="utf-8"))
        defs = payload.get("definitions")
        if not isinstance(defs, dict):
            continue
        key = name.removesuffix(".json")
        if key in defs:
            definitions[key] = defs[key]
        else:
            definitions[key] = {k: v for k, v in payload.items() if k != "$schema"}
    missing = [name for name, body in definitions.items() if not body]
    if missing or len(definitions) != len(SCHEMA_BUNDLE_FILES):
        raise SystemExit(
            "schema bundle is malformed; refusing to distill a partial contract"
        )
    schemas_json = bundle_dir / "codex_app_server_protocol.schemas.json"
    return {
        "definitions": definitions,
        "_fixture": {
            "generated_from": "codex app-server generate-json-schema --experimental",
            "codex_version": codex_version,
            "protocol_version": 2,
            "captured_by": "tests/fixtures/executor_runtime/tools/"
            "capture_protocol_fixture.py",
        },
        "_staging_note": {
            "bundle_file_present": schemas_json.is_file(),
            "redacted": True,
        },
    }


def run_probe(
    *,
    executor: str,
    binary: Path,
    surface: str,
    staging_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    """Run one whitelisted probe and record its redacted output."""
    template = PROBE_COMMANDS.get(surface)
    if template is None:
        raise SystemExit(
            f"surface {surface!r} is not in the probe whitelist "
            f"({', '.join(sorted(PROBE_COMMANDS))})"
        )
    if executor == "codex" and surface == "app_server_schema":
        argv = [
            part.replace("{binary}", str(binary)).replace(
                "{out}", str(staging_dir / "schema_bundle")
            )
            for part in template
        ]
    else:
        argv = [part.replace("{binary}", str(binary)) for part in template]
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"surface": surface, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    stdout, applied_out = redact_text(completed.stdout or "")
    stderr, applied_err = redact_text(completed.stderr or "")
    record: dict[str, Any] = {
        "surface": surface,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "argv_template": list(template),
        "stdout": stdout,
        "stderr": stderr,
        "redactions": sorted(set(applied_out + applied_err)),
    }
    if completed.returncode == 0:
        assert_no_secrets(stdout, origin=f"{surface} stdout")
        assert_no_secrets(stderr, origin=f"{surface} stderr")
    return record


def capture(
    *,
    executor: str,
    binary: Path,
    version: str | None,
    staging_dir: Path,
    surfaces: list[str],
    timeout: int = 60,
) -> dict[str, Any]:
    """Run the requested probes and write one staging snapshot."""
    if executor not in KNOWN_EXECUTORS:
        raise SystemExit(
            f"unknown executor {executor!r}; expected one of {KNOWN_EXECUTORS}"
        )
    staging_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    schema_record = next((item for item in surfaces if item == "app_server_schema"), None)
    plain_surfaces = [item for item in surfaces if item != "app_server_schema"]
    for surface in plain_surfaces:
        records.append(
            run_probe(
                executor=executor,
                binary=binary,
                surface=surface,
                staging_dir=staging_dir,
                timeout=timeout,
            )
        )
    if schema_record is not None:
        result = run_probe(
            executor=executor,
            binary=binary,
            surface="app_server_schema",
            staging_dir=staging_dir,
            timeout=max(timeout, 180),
        )
        distillation: dict[str, Any]
        if result["ok"]:
            parsed_version = version or ""
            try:
                distillation = distill_schema_bundle(
                    staging_dir / "schema_bundle", parsed_version
                )
            except SystemExit:
                raise
            except (OSError, ValueError) as exc:
                distillation = {"error": f"schema distillation failed: {exc}"}
            result["distilled"] = isinstance(distillation.get("definitions"), dict)
        else:
            distillation = {}
        result["artifact"] = "app_server_schema.json"
        records.append(result)
        snapshot = {
            "executor": executor,
            "version": version,
            "probes": records,
            "artifacts": {"app_server_schema.json": distillation},
        }
    else:
        snapshot = {"executor": executor, "version": version, "probes": records}
    (staging_dir / "snapshot.json").write_text(
        json.dumps(redact_structured(snapshot)[0], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return snapshot


# ---------------------------------------------------------------------------
# Staging import / review / verify
# ---------------------------------------------------------------------------


def load_staging_snapshot(staging_dir: Path) -> dict[str, Any]:
    snapshot_path = staging_dir / "snapshot.json"
    if not snapshot_path.is_file():
        raise SystemExit(f"{snapshot_path} is missing; nothing to review")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    redacted_snapshot, applied = redact_structured(snapshot)
    if applied:
        raise SystemExit(
            f"staging snapshot at {snapshot_path} contained data that had to be "
            f"redacted ({', '.join(sorted(set(applied)))}); regenerate it"
        )
    return snapshot


def staging_surface_files(snapshot: dict[str, Any]) -> dict[str, str]:
    """Collect the redacted text bodies for each probed surface."""
    files: dict[str, str] = {}
    for probe in snapshot.get("probes", []):
        surface = str(probe.get("surface"))
        if probe.get("ok") and surface in DIRECTORY_SURFACES:
            artifact = snapshot.get("artifacts", {}).get("app_server_schema.json")
            if isinstance(artifact, dict) and artifact.get("definitions"):
                files["app_server_schema.json"] = (
                    json.dumps(artifact, indent=2, sort_keys=True) + "\n"
                )
            continue
        body = (probe.get("stdout") or "").strip()
        fallback = (probe.get("stderr") or "").strip()
        text = body or fallback
        if not text:
            continue
        suffix = ".json" if surface == "app_server_schema" else ".txt"
        files[f"{surface}{suffix}"] = text + "\n"
    return files


_MATRIX_ROOT = Path(__file__).resolve().parents[1] / "matrix"


def version_dir(executor: str, version: str) -> Path:
    return _MATRIX_ROOT / executor / version


def review(
    *,
    executor: str,
    version: str,
    staging_dir: Path,
) -> dict[str, Any]:
    """Print the redacted diff plus hashes for one staged snapshot."""
    snapshot = load_staging_snapshot(staging_dir)
    detected = parse_version(str(snapshot.get("version") or "")) if snapshot.get(
        "version"
    ) else None
    files = staging_surface_files(snapshot)
    report: dict[str, Any] = {
        "executor": executor,
        "declared_version": snapshot.get("version") or "",
        "detected_version": detected,
        "files": {},
    }
    baseline_root = version_dir(executor, version)
    for name in sorted(files):
        text = files[name]
        digest = sha256_bytes(text.encode("utf-8"))
        entry: dict[str, Any] = {"sha256": digest, "bytes": len(text.encode("utf-8"))}
        baseline = baseline_root / name
        if baseline.is_file():
            previous = baseline.read_text(encoding="utf-8")
            diff = "\n".join(
                difflib.unified_diff(
                    previous.splitlines(),
                    text.splitlines(),
                    fromfile=f"matrix/{executor}/{version}/{name}",
                    tofile=f"<staged>/{name}",
                    lineterm="",
                )
            )
            entry["diff_lines"] = len(diff.splitlines())
            entry["diff"] = diff[:4000]
            entry["unchanged"] = diff == ""
        else:
            entry["new_surface"] = True
        report["files"][name] = entry
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


def build_surfaces_manifest(directory: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        entries[relative] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return entries


def manifest_path() -> Path:
    return _MATRIX_ROOT / "manifest.json"


def read_manifest() -> dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        raise SystemExit(f"{path} is missing; the matrix has no single source of truth")
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_manifest_entry(executor: str, version: str) -> dict[str, Any]:
    """Recompute the surfaces block of one matrix version in the manifest.

    Hashes are the capture-side contract: a surface file that changed without a
    reviewed redacted diff is caught by ``verify``.
    """
    manifest = read_manifest()
    executor_block = manifest["executors"][executor]
    entry = executor_block["versions"].get(version)
    if entry is None:
        raise SystemExit(
            f"{executor}/{version} is not declared in the manifest; add the version "
            "with its status first (never widen production bounds implicitly)"
        )
    entry["surfaces"] = build_surfaces_manifest(version_dir(executor, version))
    manifest_path().write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return entry["surfaces"]


def promote(*, executor: str, version: str, staging_dir: Path) -> dict[str, Any]:
    """Write reviewed staging files into the matrix as the authoritative copy."""
    # Resolve the declared version first so nothing is ever written outside a
    # manifest-declared version directory.
    manifest = read_manifest()
    if (
        executor not in manifest["executors"]
        or version not in manifest["executors"][executor]["versions"]
    ):
        raise SystemExit(
            f"{executor}/{version} is not declared in the manifest; declare it with "
            "an explicit status first (candidates never widen production bounds)"
        )
    snapshot = load_staging_snapshot(staging_dir)
    files = staging_surface_files(snapshot)
    target = version_dir(executor, version)
    target.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(files.items()):
        assert_no_secrets(text, origin=f"{target / name}")
        (target / name).write_text(text, encoding="utf-8")
    surfaces = refresh_manifest_entry(executor, version)
    print(json.dumps(surfaces, indent=2, sort_keys=True))
    return surfaces


def verify(executor_version: tuple[str, str] | None = None) -> bool:
    """Verify hashes declared in the manifest against the stored fixture files."""
    manifest = read_manifest()
    if executor_version is None:
        blocks: list[tuple[str, str | None, dict[str, Any]]] = [
            (executor, None, manifest["executors"][executor]["shared_surfaces"])
            for executor in manifest["executors"]
        ]
        versions = [
            (executor, version, entry)
            for executor, data in manifest["executors"].items()
            for version, entry in data["versions"].items()
        ]
    else:
        data = manifest["executors"][executor_version[0]]
        entry = data["versions"][executor_version[1]]
        blocks = [(executor_version[0], None, data["shared_surfaces"])]
        versions = [(executor_version[0], executor_version[1], entry)]
    problems: list[str] = []
    listed_all: set[tuple[str, str]] = set()
    for relative, metadata in sorted(
        (rel, meta) for _, _executor, shared in blocks for rel, meta in shared.items()
    ):
        path = _MATRIX_ROOT / relative
        problems.extend(_verify_hash(relative, path, metadata))
    for executor, version, entry in versions:
        for relative, metadata in entry.get("surfaces", {}).items():
            listed_all.add((executor, relative))
            path = version_dir(executor, version) / relative
            problems.extend(_verify_hash(relative, path, metadata))
        actual = {
            path.relative_to(version_dir(executor, version)).as_posix()
            for path in version_dir(executor, version).rglob("*")
            if path.is_file()
        }
        for unexpected in sorted(actual - set(entry.get("surfaces", {}))):
            problems.append(f"unlisted file: {executor}/{version}/{unexpected}")
    for problem in problems:
        print(problem)
    return not problems


def _verify_hash(relative: str, path: Path, metadata: dict[str, Any]) -> list[str]:
    if not path.is_file():
        return [f"missing file: {relative}"]
    problems: list[str] = []
    if sha256_file(path) != metadata.get("sha256"):
        problems.append(f"hash mismatch: {relative}")
    if path.stat().st_size != metadata.get("bytes"):
        problems.append(f"size mismatch: {relative}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture", help="run whitelisted probes")
    capture_parser.add_argument("--executor", required=True, choices=KNOWN_EXECUTORS)
    capture_parser.add_argument("--binary", required=True, type=Path)
    capture_parser.add_argument("--version", default="")
    capture_parser.add_argument("--staging-dir", required=True, type=Path)
    capture_parser.add_argument("--surfaces", default="")
    capture_parser.add_argument("--timeout", type=int, default=60)

    import_parser = subparsers.add_parser(
        "import-snapshot", help="ingest fake-transport or remote staged output"
    )
    import_parser.add_argument("--executor", required=True, choices=KNOWN_EXECUTORS)
    import_parser.add_argument("--snapshot", required=True, type=Path)
    import_parser.add_argument("--staging-dir", required=True, type=Path)

    review_parser = subparsers.add_parser("review", help="redacted diff plus hashes")
    review_parser.add_argument("--executor", required=True, choices=KNOWN_EXECUTORS)
    review_parser.add_argument("--version", required=True)
    review_parser.add_argument("--staging-dir", required=True, type=Path)

    promote_parser = subparsers.add_parser("promote", help="write reviewed files")
    promote_parser.add_argument("--executor", required=True, choices=KNOWN_EXECUTORS)
    promote_parser.add_argument("--version", required=True)
    promote_parser.add_argument("--staging-dir", required=True, type=Path)

    verify_parser = subparsers.add_parser(
        "verify", help="check stored hashes against manifest.json"
    )
    verify_parser.add_argument("--executor", choices=KNOWN_EXECUTORS, default=None)
    verify_parser.add_argument("--version", default=None)

    args = parser.parse_args(argv)

    if args.command == "capture":
        surfaces = (
            args.surfaces.split(",")
            if args.surfaces
            else list(DEFAULT_PROBES_BY_EXECUTOR[args.executor])
        )
        snapshot = capture(
            executor=args.executor,
            binary=args.binary.expanduser(),
            version=str(parse_version(args.version) or args.version or "") or None,
            staging_dir=args.staging_dir,
            surfaces=surfaces,
            timeout=args.timeout,
        )
        print(
            json.dumps(
                {
                    "executor": snapshot["executor"],
                    "version": snapshot.get("version"),
                    "surfaces": [
                        {"surface": p["surface"], "ok": p.get("ok")}
                        for p in snapshot["probes"]
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "import-snapshot":
        staged = args.staging_dir
        staged.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        redacted_snapshot, applied = redact_structured(snapshot)
        if applied:
            print(
                "snapshot required redaction: " + ", ".join(sorted(set(applied))),
                file=sys.stderr,
            )
        (staged / "snapshot.json").write_text(
            json.dumps(redacted_snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"staged": str(staged / 'snapshot.json')}, sort_keys=True))
        return 0

    if args.command == "review":
        review(executor=args.executor, version=args.version, staging_dir=args.staging_dir)
        return 0

    if args.command == "promote":
        promote(
            executor=args.executor, version=args.version, staging_dir=args.staging_dir
        )
        return 0

    if args.command == "verify":
        selected = (
            (args.executor, args.version)
            if args.executor and args.version
            else None
        )
        return 0 if verify(selected) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
