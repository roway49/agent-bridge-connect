"""Frozen Codex App Server capability/schema contract (PERM-103-009).

This narrow module is the single place where AgentBC freezes which Codex
App Server surfaces it may drive in production.  It owns:

* the canonical App Server transport value (``app-server``) and its
  accepted aliases for backward compatibility,
* the frozen client methods / server request methods / server
  notifications required for a single-action approval chain,
* the schema-contract verification against the official generated
  ``codex app-server generate-json-schema --experimental`` output
  (both the Runner-pinned ``0.146.0`` and the local ``0.147.0``
  surfaces),
* the version gate that fails closed on unknown / too-old Codex
  versions, and
* the executable probe that only ever reads official CLI help/schema
  output and never scans Codex private session storage.

Design rules (from the ``PERM-103-009`` production-chain freeze):

* ``app-server`` is the only canonical transport value that enables the
  same-process single-action approval chain.  ``inherit`` never adds an
  AgentBC override; ``safe`` may select ``app-server`` only when the
  full chain (thread/start -> official receipt -> turn/start ->
  requestApproval -> accept/decline -> same session) is verified on the
  configured executable.  ``full`` and the existing CLI continuation
  fallback remain unchanged.
* Every verification result is bounded and never persists raw CLI
  output; only the frozen method names, the parsed version and a short
  summary may be recorded.
* The contract mirrors the generated schema both for the Runner-pinned
  ``0.146.0`` release and the locally installed ``0.147.0``.  The two
  surfaces are identical for the frozen method set, so the same probe
  works for both; the version gate keeps the contract pinned to known
  releases and fails closed for anything unknown.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .control import APPROVAL_METHODS
from .protocol import ABCError

# Canonical App Server transport value.  Only this value enables the
# same-process single-action approval chain in production.  The executor
# accepts the legacy aliases for backward compatibility but the registry and
# Runner only accept the canonical value.
CODEX_APP_SERVER_TRANSPORT = "app-server"
CODEX_APP_SERVER_TRANSPORT_ALIASES = frozenset(
    {"app-server", "app_server", "stdio", "codex-app-server"}
)

# Version gate: the frozen minimum and maximum surfaces.  The Runner-pinned
# release is 0.146.0 and the locally installed surface is 0.147.0.  Any other
# version is rejected until a new schema fixture and probe evidence exist.
CODEX_APP_SERVER_MIN_VERSION = (0, 146, 0)
CODEX_APP_SERVER_MAX_VERSION = (0, 147, 0)
CODEX_APP_SERVER_REQUIRED_PROTOCOL = 2

# Frozen App Server surface for the AgentBC single-action chain.
CODEX_APP_SERVER_CLIENT_METHODS = frozenset(
    {"initialize", "thread/start", "thread/resume", "turn/start"}
)
CODEX_APP_SERVER_REQUEST_METHODS = frozenset(APPROVAL_METHODS)
CODEX_APP_SERVER_NOTIFICATIONS = frozenset({"item/completed", "turn/completed"})

# Schema-contract evidence, in the same order the generated bundle is checked.
CODEX_APP_SERVER_SCHEMA_METHODS = frozenset(
    {
        # v2 protocol schema (both versions)
        "thread/start",
        "thread/resume",
        "turn/start",
        # non-v2 server request schema (both versions)
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        # v2 notifications (both versions)
        "item/completed",
        "turn/completed",
    }
)

# Version strings are of the form ``codex-cli 0.146.0`` or ``0.147.0``.
_VERSION_RE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


def parse_codex_version(output: str) -> tuple[int, int, int] | None:
    """Parse a ``codex --version`` line into a comparable triple."""
    text = str(output or "").strip()
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    try:
        return (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
        )
    except (TypeError, ValueError):
        return None


def _version_key(version: str) -> tuple[int, int, int] | None:
    parsed = parse_codex_version(version)
    if parsed is None:
        return None
    return parsed


def _first_line(text: str, *, limit: int = 160) -> str:
    line = text.splitlines()[0] if text else ""
    if len(line) > limit:
        return f"{line[:limit]}..."
    return line


def _extract_method_names(schema: dict[str, Any]) -> set[str]:
    """Extract the frozen method names from one generated schema bundle."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if (
                isinstance(value.get("title"), str)
                and "Method" in value["title"]
                and isinstance(value.get("enum"), list)
            ):
                found.update(str(item) for item in value["enum"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    return found


def _schema_has_request_method(schema: dict[str, Any], method: str) -> bool:
    """Return True only when one ServerRequest definition lists the method."""
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict):
        return False
    for name, value in definitions.items():
        if not isinstance(name, str) or not name.startswith("ServerRequest"):
            continue
        methods = _extract_method_names(value)
        if method in methods:
            return True
    return False


def _schema_has_notification(schema: dict[str, Any], method: str) -> bool:
    """Return True only when one ServerNotification definition lists the method."""
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict):
        return False
    for name, value in definitions.items():
        if not isinstance(name, str) or not name.startswith("ServerNotification"):
            continue
        methods = _extract_method_names(value)
        if method in methods:
            return True
    return False


def _schema_has_client_method(schema: dict[str, Any], method: str) -> bool:
    """Return True when a ClientRequest definition lists the method."""
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict):
        return False
    for name, value in definitions.items():
        if not isinstance(name, str) or not name.startswith("ClientRequest"):
            continue
        methods = _extract_method_names(value)
        if method in methods:
            return True
    return False


def _schema_matches_contract(schema: dict[str, Any]) -> list[str]:
    """Return missing frozen surface names as a fail-closed reason list."""
    missing: list[str] = []
    for method in ("initialize", "thread/start", "thread/resume", "turn/start"):
        if not _schema_has_client_method(schema, method):
            missing.append(method)
    for method in CODEX_APP_SERVER_REQUEST_METHODS:
        if not _schema_has_request_method(schema, method):
            missing.append(method)
    for method in CODEX_APP_SERVER_NOTIFICATIONS:
        if not _schema_has_notification(schema, method):
            missing.append(method)
    return missing


def _read_bundle_directory(bundle_dir: str | Path) -> dict[str, Any] | None:
    """Load the generated ``codex_app_server_protocol.schemas.json`` bundle."""
    bundle = Path(bundle_dir).expanduser() / "codex_app_server_protocol.schemas.json"
    if not bundle.is_file():
        return None
    try:
        value = json.loads(bundle.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def codex_app_server_contract(
    executable: str | Path,
    *,
    version_output: str = "",
    schema_bundle: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Verify one Codex executable against the frozen App Server contract.

    The verification is executed with only official CLI help/schema output;
    it never scans private session storage and never reads user config.
    Unknown versions, missing frozen methods, or a malformed schema bundle
    all fail closed with ``codex_app_server_capability_unsupported``.
    """
    executable_path = Path(executable).expanduser()
    result: dict[str, Any] = {
        "ok": False,
        "transport": CODEX_APP_SERVER_TRANSPORT,
        "protocol_version": CODEX_APP_SERVER_REQUIRED_PROTOCOL,
        "version": "",
        "version_parsed": None,
        "schema_missing": [],
        "evidence": [],
        "reason": "",
        "returncode": None,
        "schema_summary": "",
    }

    version = str(version_output or "").strip()
    if not version:
        try:
            completed = subprocess.run(
                [str(executable_path), "--version"],
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["reason"] = f"codex --version unavailable: {exc}"
            return result
        result["returncode"] = completed.returncode
        version = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0 or not version:
            result["reason"] = "codex --version failed"
            return result
    result["version"] = _first_line(version)
    parsed = _version_key(version)
    result["version_parsed"] = parsed
    if parsed is None:
        result["reason"] = f"codex version is not parseable: {result['version']}"
        return result
    if not (
        CODEX_APP_SERVER_MIN_VERSION <= parsed <= CODEX_APP_SERVER_MAX_VERSION
    ):
        result["reason"] = (
            f"codex version {'.'.join(str(part) for part in parsed)} is outside the "
            f"frozen App Server surface "
            f"({'.'.join(str(part) for part in CODEX_APP_SERVER_MIN_VERSION)}-"
            f"{'.'.join(str(part) for part in CODEX_APP_SERVER_MAX_VERSION)})"
        )
        return result

    bundle = schema_bundle
    if bundle is None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agentbc-codex-schema-") as schema_dir:
            try:
                completed = subprocess.run(
                    [
                        str(executable_path),
                        "app-server",
                        "generate-json-schema",
                        "--out",
                        schema_dir,
                        "--experimental",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["reason"] = f"codex app-server schema generation unavailable: {exc}"
                return result
            if completed.returncode != 0:
                result["reason"] = "codex app-server schema generation failed"
                return result
            bundle = _read_bundle_directory(schema_dir)
    if not isinstance(bundle, dict):
        result["reason"] = "codex app-server generated schema is malformed"
        return result

    missing = _schema_matches_contract(bundle)
    result["schema_missing"] = missing
    if missing:
        result["reason"] = (
            "codex app-server schema is missing frozen surface methods: "
            + ", ".join(sorted(missing))
        )
        return result
    result["ok"] = True
    result["evidence"] = ["version_gate", "schema_methods_verified"]
    result["schema_summary"] = _first_line(
        str(bundle.get("title") or "CodexAppServerProtocol")
    )
    return result


def assert_codex_app_server_capability(
    executable: str | Path | None,
    *,
    transport: str | None = None,
) -> dict[str, Any]:
    """Assert the configured executable can drive the App Server chain.

    Raises ``permission_capability_unsupported`` when the transport is not the
    canonical ``app-server``, the executable is unavailable, or the frozen
    schema/version contract fails.  ``inherit`` never probes the App Server
    surface because it adds no AgentBC override.
    """
    selected = str(transport or "").strip().lower()
    if selected not in CODEX_APP_SERVER_TRANSPORT_ALIASES:
        raise ABCError(
            "permission_capability_unsupported",
            f"Transport {transport!r} cannot express the Codex App Server chain.",
            {"executor": "codex", "transport": transport},
        )
    if executable is None:
        raise ABCError(
            "permission_capability_unsupported",
            "Codex App Server capability requires an executable.",
            {"executor": "codex", "transport": CODEX_APP_SERVER_TRANSPORT},
        )
    probe = codex_app_server_contract(executable)
    if not probe["ok"]:
        raise ABCError(
            "permission_capability_unsupported",
            (
                f"Codex App Server single-action chain is unavailable: "
                f"{probe['reason']}"
            ),
            {
                "executor": "codex",
                "transport": CODEX_APP_SERVER_TRANSPORT,
                "reason": probe["reason"],
                "version": probe["version"],
                "version_parsed": probe["version_parsed"],
                "schema_missing": probe["schema_missing"],
                "returncode": probe["returncode"],
            },
        )
    return probe


__all__ = [
    "CODEX_APP_SERVER_CLIENT_METHODS",
    "CODEX_APP_SERVER_MAX_VERSION",
    "CODEX_APP_SERVER_MIN_VERSION",
    "CODEX_APP_SERVER_NOTIFICATIONS",
    "CODEX_APP_SERVER_REQUIRED_PROTOCOL",
    "CODEX_APP_SERVER_REQUEST_METHODS",
    "CODEX_APP_SERVER_TRANSPORT",
    "CODEX_APP_SERVER_TRANSPORT_ALIASES",
    "assert_codex_app_server_capability",
    "codex_app_server_contract",
    "parse_codex_version",
]
