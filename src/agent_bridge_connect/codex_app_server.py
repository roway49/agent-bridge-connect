"""Codex App Server capability/schema contract (PERM-103-009).

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
* best-effort version diagnostics that never decide protocol support, and
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
  surfaces are identical for the required method set.  Newer versions and
  forks are supported by default when the generated schema still exposes
  that protocol surface; AgentBC does not maintain a release allow-list.
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

# Historical fixture bounds retained as compatibility exports for tooling and
# old reports.  They are evidence metadata only and are never a production
# support gate.  Runtime support is determined mechanically from the generated
# App Server schema below.
CODEX_APP_SERVER_MIN_VERSION = (0, 146, 0)
CODEX_APP_SERVER_MAX_VERSION = (0, 150, 1)
CODEX_APP_SERVER_SUPPORTED_VERSIONS = frozenset(
    {(0, 146, 0), (0, 147, 0), (0, 150, 1)}
)
CODEX_APP_SERVER_REQUIRED_PROTOCOL = 2

# Frozen App Server surface for the AgentBC single-action chain.
CODEX_APP_SERVER_CLIENT_METHODS = frozenset(
    {"initialize", "thread/start", "thread/resume", "turn/start"}
)
CODEX_APP_SERVER_REQUEST_METHODS = frozenset(APPROVAL_METHODS)
CODEX_APP_SERVER_NOTIFICATIONS = frozenset({"item/completed", "turn/completed"})

# Named capability groups (PROTO-104-001).  The ``execution`` group is exactly
# the frozen single-action surface above and keeps driving production today.
# The ``cleanup`` group names the thread lifecycle surface SESSION will need for
# App Server based session deletion; exposing it as a named group does not
# change any production behavior until SESSION opts in.  Group membership is a
# closed set: extra schema methods are never added automatically, so an
# incomplete group always fails closed in the matrix contract tests.
CODEX_APP_SERVER_EXECUTION_GROUP = "execution"
CODEX_APP_SERVER_CLEANUP_GROUP = "cleanup"
CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP = "desktop_visibility"
CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP = "collaboration_spawn"
CODEX_APP_SERVER_COLLABORATION_MARKERS = frozenset(
    {"collabAgentToolCall", "spawnAgent", "receiverThreadId"}
)
CODEX_APP_SERVER_COLLABORATION_LIFECYCLE = frozenset(
    {"item/started", "item/completed"}
)
CODEX_APP_SERVER_CAPABILITY_GROUPS: dict[str, dict[str, frozenset[str]]] = {
    CODEX_APP_SERVER_EXECUTION_GROUP: {
        "client_methods": CODEX_APP_SERVER_CLIENT_METHODS,
        "server_requests": CODEX_APP_SERVER_REQUEST_METHODS,
        "notifications": CODEX_APP_SERVER_NOTIFICATIONS,
    },
    CODEX_APP_SERVER_CLEANUP_GROUP: {
        # SESSION-104-001: the archive members join the frozen delete surface.
        # ``thread/archive`` must be acknowledged before ``thread/delete`` is
        # sent; ``thread/archived`` stays advisory exactly like
        # ``thread/deleted``.  Extra schema members are never added, so the
        # closed set still fails closed on unknown versions.
        "client_methods": frozenset(
            {"thread/archive", "thread/delete", "thread/read"}
        ),
        "server_requests": frozenset(),
        "notifications": frozenset({"thread/archived", "thread/deleted"}),
    },
    CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP: {
        "client_methods": frozenset({"thread/list"}),
        "server_requests": frozenset(),
        "notifications": frozenset(),
    },
    CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP: {
        # The logical receiverThreadId marker maps to the plural
        # receiverThreadIds field used by the 0.150 candidate schema.  The
        # group is enabled only when the live generated schema exposes it.
        "client_methods": frozenset(),
        "server_requests": frozenset(),
        "notifications": CODEX_APP_SERVER_COLLABORATION_LIFECYCLE,
        "schema_markers": CODEX_APP_SERVER_COLLABORATION_MARKERS,
    },
}

# Versions whose generated schema bundle is distilled into the fixture matrix.
# Evidence only: listing a version here does not restrict runtime support.
CODEX_APP_SERVER_SCHEMA_EVIDENCE_VERSIONS = ("0.146.0", "0.147.0", "0.150.1")

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


def _schema_contains_value(schema: dict[str, Any], expected: str) -> bool:
    """Find one exact enum/value marker in a generated schema bundle."""
    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            if any(item == expected for item in value.get("enum", [])):
                return True
            return any(walk(child) for child in value.values())
        if isinstance(value, list):
            return any(walk(child) for child in value)
        return False

    return walk(schema)


def _schema_has_collaboration_marker(schema: dict[str, Any], marker: str) -> bool:
    """Verify a collaboration marker, including Codex's plural wire field."""
    if marker == "receiverThreadId":
        def has_receiver_key(value: Any) -> bool:
            if isinstance(value, dict):
                if "receiverThreadId" in value or "receiverThreadIds" in value:
                    return True
                return any(has_receiver_key(child) for child in value.values())
            if isinstance(value, list):
                return any(has_receiver_key(child) for child in value)
            return False

        return has_receiver_key(schema)
    return _schema_contains_value(schema, marker)


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


def verify_capability_group(
    schema: dict[str, Any],
    group: str,
) -> list[str]:
    """Return the members of one named capability group missing from a schema.

    Fail-closed: a method that cannot be located in the generated bundle is
    reported as missing instead of being assumed present.  Used by the fixture
    matrix contract tests; it never changes production probing.
    """
    definition = CODEX_APP_SERVER_CAPABILITY_GROUPS.get(group)
    if definition is None:
        return [f"unknown_capability_group:{group}"]
    missing: list[str] = []
    for method in sorted(definition.get("client_methods", ())):
        if not _schema_has_client_method(schema, method):
            missing.append(method)
    for method in sorted(definition.get("server_requests", ())):
        if not _schema_has_request_method(schema, method):
            missing.append(method)
    for method in sorted(definition.get("notifications", ())):
        if not _schema_has_notification(schema, method):
            missing.append(method)
    if group == CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP:
        for marker in sorted(definition.get("schema_markers", ())):
            if not _schema_has_collaboration_marker(schema, marker):
                missing.append(marker)
    return missing


def codex_collaboration_spawn_fixture_contract(
    version: str,
    *,
    fixture_root: str | Path | None = None,
) -> dict[str, Any]:
    """Check the frozen schema fixture for the collaboration spawn group."""
    normalized = str(version or "").strip()
    if fixture_root is not None:
        root = Path(fixture_root).expanduser()
    else:
        packaged_root = Path(__file__).resolve().parent / "protocol_fixtures" / "codex"
        source_root = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "fixtures"
            / "executor_runtime"
            / "matrix"
            / "codex"
        )
        root = packaged_root if (packaged_root / normalized).is_dir() else source_root
    bundle_path = root / normalized / "app_server_schema.json"
    result: dict[str, Any] = {
        "ok": False,
        "version": normalized,
        "source": "fixture",
        "schema_path": str(bundle_path),
        "missing": [],
        "reason": "",
    }
    try:
        schema = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        result["reason"] = "collaboration fixture is unavailable"
        return result
    if not isinstance(schema, dict):
        result["reason"] = "collaboration fixture is malformed"
        return result
    missing = verify_capability_group(
        schema,
        CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP,
    )
    result["missing"] = missing
    if missing:
        result["reason"] = "collaboration fixture is missing: " + ", ".join(missing)
        return result
    result["ok"] = True
    result["evidence"] = ["fixture_schema_markers_verified", "fixture_item_lifecycle_verified"]
    return result


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
    Version text is diagnostic only. Missing required methods or a malformed
    schema bundle fail closed with ``codex_app_server_capability_unsupported``.
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
    result["evidence"] = [
        "protocol_surface_default_compatible",
        "schema_methods_verified",
    ]
    result["schema_summary"] = _first_line(
        str(bundle.get("title") or "CodexAppServerProtocol")
    )
    return result


def codex_collaboration_spawn_contract(
    executable: str | Path,
    *,
    version_output: str = "",
    schema_bundle: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Probe the live collaboration-spawn surface mechanically.

    Collaboration is a separate capability from the ordinary App Server
    execution contract. Runtime support follows the generated schema surface,
    not a Codex version allow-list. Frozen fixtures remain regression evidence
    only and never gate a compatible newer release or fork.
    """
    base = codex_app_server_contract(
        executable,
        version_output=version_output,
        schema_bundle=schema_bundle,
        timeout=timeout,
    )
    result: dict[str, Any] = {
        "ok": False,
        "transport": CODEX_APP_SERVER_TRANSPORT,
        "version": base.get("version", ""),
        "version_parsed": base.get("version_parsed"),
        "missing": [],
        "reason": "",
        "live": base,
    }
    if not base.get("ok"):
        result["reason"] = str(base.get("reason") or "App Server live probe failed")
        return result
    live_schema = schema_bundle
    if live_schema is None:
        # codex_app_server_contract intentionally keeps the schema ephemeral.
        # Re-run the official bounded probe here only when the caller did not
        # supply the already parsed bundle; no private storage is touched.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="agentbc-codex-collab-schema-") as schema_dir:
            try:
                completed = subprocess.run(
                    [
                        str(Path(executable).expanduser()),
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
                result["reason"] = f"collaboration live schema probe unavailable: {exc}"
                return result
            if completed.returncode != 0:
                result["reason"] = "collaboration live schema probe failed"
                return result
            live_schema = _read_bundle_directory(schema_dir)
    if not isinstance(live_schema, dict):
        result["reason"] = "collaboration live schema is malformed"
        return result
    missing = verify_capability_group(
        live_schema,
        CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP,
    )
    result["missing"] = missing
    if missing:
        result["reason"] = "collaboration live probe is missing: " + ", ".join(missing)
        return result
    result["ok"] = True
    result["evidence"] = ["live_schema_markers_verified", "live_item_lifecycle_verified"]
    return result


def assert_codex_collaboration_spawn_capability(
    executable: str | Path | None,
    *,
    transport: str | None = None,
) -> dict[str, Any]:
    """Require live protocol proof before enabling collaboration."""
    selected = str(transport or "").strip().lower()
    if selected not in CODEX_APP_SERVER_TRANSPORT_ALIASES:
        raise ABCError(
            "codex_collaboration_spawn_unsupported",
            "Collaboration spawn requires the Codex App Server transport.",
        )
    if executable is None:
        raise ABCError(
            "codex_collaboration_spawn_unsupported",
            "Collaboration spawn requires a Codex executable.",
        )
    live = codex_collaboration_spawn_contract(executable)
    if not live.get("ok"):
        raise ABCError(
            "codex_collaboration_spawn_unsupported",
            str(live.get("reason") or "Codex collaboration live probe failed"),
            {"live": live},
        )
    parsed_version = live.get("version_parsed")
    version = (
        ".".join(str(part) for part in parsed_version)
        if isinstance(parsed_version, (tuple, list)) and len(parsed_version) == 3
        else str(live.get("version") or "").splitlines()[0]
    )
    fixture = codex_collaboration_spawn_fixture_contract(version)
    return {
        "enabled": True,
        "verification_source": "live_schema",
        "fixture": fixture,
        "live": live,
    }


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
    "CODEX_APP_SERVER_CAPABILITY_GROUPS",
    "CODEX_APP_SERVER_CLEANUP_GROUP",
    "CODEX_APP_SERVER_CLIENT_METHODS",
    "CODEX_APP_SERVER_COLLABORATION_LIFECYCLE",
    "CODEX_APP_SERVER_COLLABORATION_MARKERS",
    "CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP",
    "CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP",
    "CODEX_APP_SERVER_EXECUTION_GROUP",
    "CODEX_APP_SERVER_MAX_VERSION",
    "CODEX_APP_SERVER_MIN_VERSION",
    "CODEX_APP_SERVER_NOTIFICATIONS",
    "CODEX_APP_SERVER_SUPPORTED_VERSIONS",
    "CODEX_APP_SERVER_REQUIRED_PROTOCOL",
    "CODEX_APP_SERVER_REQUEST_METHODS",
    "CODEX_APP_SERVER_SCHEMA_EVIDENCE_VERSIONS",
    "CODEX_APP_SERVER_TRANSPORT",
    "CODEX_APP_SERVER_TRANSPORT_ALIASES",
    "assert_codex_app_server_capability",
    "assert_codex_collaboration_spawn_capability",
    "codex_collaboration_spawn_contract",
    "codex_collaboration_spawn_fixture_contract",
    "codex_app_server_contract",
    "parse_codex_version",
    "verify_capability_group",
]
