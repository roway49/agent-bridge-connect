"""PROTO-104-001 executor protocol fixture matrix contract tests.

These tests are the enforcement layer for the versioned fixture matrix under
``tests/fixtures/executor_runtime/matrix``:

* manifest integrity - every listed surface exists with the declared hash and
  no unlisted file hides in a version directory (single authoritative copy);
* production bounds agreement - the manifest never claims more than the
  production constants actually enforce;
* candidate isolation - candidate and pending-capture versions can never widen
  a production gate, no matter how new the PATH binary is;
* capability group agreement - the named Codex execution/cleanup groups match
  the fixtures, and the cleanup group is exactly
  ``thread/delete`` / ``thread/deleted`` / ``thread/read``;
* fail-closed contract completeness - critical event policies and early
  receipts are frozen for every recorded version.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from agent_bridge_connect.claude_path_capability import (
    CLAUDE_PATH_CAPABILITY_MAX_VERSION,
    CLAUDE_PATH_CAPABILITY_MIN_VERSION,
)
from agent_bridge_connect.codex_app_server import (
    CODEX_APP_SERVER_CAPABILITY_GROUPS,
    CODEX_APP_SERVER_CLEANUP_GROUP,
    CODEX_APP_SERVER_COLLABORATION_MARKERS,
    CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP,
    CODEX_APP_SERVER_COLLABORATION_LIFECYCLE,
    CODEX_APP_SERVER_CLIENT_METHODS,
    CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP,
    CODEX_APP_SERVER_NOTIFICATIONS,
    CODEX_APP_SERVER_REQUEST_METHODS,
    verify_capability_group,
)
from agent_bridge_connect.executors.codex import _CODEX_FROZEN_VERSION
from agent_bridge_connect.executors.hermes import _HERMES_FROZEN_VERSION
from agent_bridge_connect.hermes_acp import HERMES_ACP_PROTOCOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime"
MATRIX = FIXTURES / "matrix"
MANIFEST = MATRIX / "manifest.json"

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(str(text or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


class ManifestIntegrityTests(unittest.TestCase):
    """Every file in the matrix must be listed exactly once by the manifest."""

    def test_manifest_exists_with_expected_shape(self) -> None:
        data = load_manifest()
        self.assertEqual(data["schema_version"], "PROTO-104-001/1")
        self.assertEqual(
            set(data["executors"]), {"codex", "claude", "hermes"}
        )

    def test_required_versions_are_recorded(self) -> None:
        manifest = load_manifest()
        expected = {
            "codex": {"0.146.0", "0.147.0", "0.150.1"},
            "claude": {"2.1.226", "2.1.233"},
            "hermes": {"0.17.0", "0.20.1"},
        }
        for executor, versions in expected.items():
            with self.subTest(executor=executor):
                self.assertEqual(
                    set(manifest["executors"][executor]["versions"]), versions
                )

    def test_e52m003_live_probe_is_executor_level_evidence(self) -> None:
        # The live probe of the installed production binary is recorded as
        # executor-level evidence, not a matrix version: it adds no
        # capability and must never satisfy version surface requirements.
        manifest = load_manifest()
        probe = manifest["executors"]["claude"]["live_probe_e52m003"]
        self.assertTrue(probe["live_capture"])
        self.assertFalse(probe["help_contains_permission_prompt_tool"])
        self.assertIsNone(probe["stdio_control_live_capture"])
        self.assertEqual(probe["decision"], "permission_transport_unsupported")
        probe_dir = MATRIX / probe["surface_dir"]
        body = json.loads((probe_dir / "permission_control.json").read_text())
        self.assertTrue(body["captured_live"])
        self.assertFalse(body["probe"]["help_contains_permission_prompt_tool"])

    def test_declared_hashes_match_stored_files(self) -> None:
        manifest = load_manifest()
        checked = 0
        for executor, block in manifest["executors"].items():
            entries = [
                ("", relative, metadata)
                for relative, metadata in block["shared_surfaces"].items()
            ] + [
                (version, relative, metadata)
                for version, entry in block["versions"].items()
                for relative, metadata in entry["surfaces"].items()
            ]
            for version_label, relative, metadata in entries:
                directory = (
                    MATRIX if not version_label else MATRIX / executor / version_label
                )
                path = directory / relative
                with self.subTest(
                    executor=executor, version=version_label or "shared", surface=relative
                ):
                    self.assertTrue(path.is_file(), f"{path} is missing")
                    payload = path.read_bytes()
                    self.assertEqual(
                        hashlib.sha256(payload).hexdigest(), metadata["sha256"]
                    )
                    self.assertEqual(len(payload), metadata["bytes"])
                    checked += 1
        self.assertGreater(checked, 40)

    def test_no_unlisted_files_inside_version_directories(self) -> None:
        manifest = load_manifest()
        orphans: list[str] = []
        for executor, block in manifest["executors"].items():
            for version, entry in block["versions"].items():
                directory = MATRIX / executor / version
                for path in sorted(directory.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(directory).as_posix()
                    if relative not in entry["surfaces"]:
                        orphans.append(f"{executor}/{version}/{relative}")
            for shared_relative in block["shared_surfaces"]:
                if not (MATRIX / shared_relative).is_file():
                    orphans.append(shared_relative)
        self.assertEqual(orphans, [])

    def test_shared_surface_is_the_only_codex_summary_copy(self) -> None:
        summaries = list(MATRIX.glob("codex/**/app_server_v2_contract_summary.json"))
        flat_duplicates = list(FIXTURES.glob("codex_app_server_protocol.*"))
        self.assertEqual([path.name for path in summaries],
                         ["app_server_v2_contract_summary.json"])
        self.assertEqual(flat_duplicates, [])


class ProductionBoundAgreementTests(unittest.TestCase):
    """The manifest may only describe bounds that production actually enforces."""

    def test_codex_app_server_production_uses_protocol_detection(self) -> None:
        block = load_manifest()["executors"]["codex"]["production"]
        self.assertEqual(block["app_server_support"], "protocol_surface")
        self.assertNotIn("app_server_bounds", block)
        self.assertEqual(block["frozen_cli_version"], _CODEX_FROZEN_VERSION)

    def test_claude_path_capability_bounds_agree(self) -> None:
        block = load_manifest()["executors"]["claude"]["production"]
        self.assertEqual(
            parse_version(block["path_capability_bounds"]["min_inclusive"]),
            CLAUDE_PATH_CAPABILITY_MIN_VERSION,
        )
        self.assertEqual(
            parse_version(block["path_capability_bounds"]["max_exclusive"]),
            CLAUDE_PATH_CAPABILITY_MAX_VERSION,
        )

    def test_hermes_acp_and_frozen_cli_agree(self) -> None:
        block = load_manifest()["executors"]["hermes"]["production"]
        self.assertEqual(block["acp_protocol_version"], HERMES_ACP_PROTOCOL_VERSION)
        self.assertEqual(block["frozen_cli_version"], _HERMES_FROZEN_VERSION)

    def test_binding_constants_resolve_to_real_module_attributes(self) -> None:
        """Every manifest binding constant must name an importable attribute.

        A constant that only *looks* like a real gate makes the manifest's
        provenance claim unverifiable, so the pointer is exercised here.
        """
        import importlib

        manifest = load_manifest()
        for executor, block in manifest["executors"].items():
            for binding in block["production"]["binding_constants"]:
                with self.subTest(executor=executor, binding=binding):
                    module_name, _, attribute = binding.rpartition(".")
                    module = importlib.import_module(module_name)
                    self.assertTrue(hasattr(module, attribute))

    def test_supported_range_matches_statuses(self) -> None:
        manifest = load_manifest()
        for executor, block in manifest["executors"].items():
            supported = {
                version
                for version, entry in block["versions"].items()
                if entry["in_production_supported_range"]
            }
            with self.subTest(executor=executor):
                self.assertEqual(supported, {"codex": {"0.146.0", "0.147.0", "0.150.1"},
                                             "claude": {"2.1.226"},
                                             "hermes": {"0.17.0"}}[executor])


class CandidateIsolationTests(unittest.TestCase):
    """Candidates are evidence-only; nothing about them widens production."""

    def assert_outside(self, parsed, low, high) -> None:
        self.assertIsNotNone(parsed)
        self.assertFalse(low <= parsed <= high)

    def test_candidate_versions_are_flagged_and_blocked(self) -> None:
        manifest = load_manifest()
        for executor, block in manifest["executors"].items():
            for version, entry in block["versions"].items():
                if entry["status"] == "supported":
                    continue
                with self.subTest(executor=executor, version=version):
                    self.assertFalse(entry["in_production_supported_range"])
                    self.assertTrue(entry.get("promotion_blocker"))
                    version_surface = json.loads(
                        (MATRIX / executor / version / "version.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    status = version_surface.get("status")
                    self.assertIn(
                        status,
                        ("candidate", "pending_capture"),
                        f"{executor}/{version} must not look production-ready",
                    )

    def test_promoted_collaboration_binary_is_frozen_in_codex_gate(self) -> None:
        codex = load_manifest()["executors"]["codex"]
        self.assertIn("0.150.1", {
            version
            for version, entry in codex["versions"].items()
            if entry["in_production_supported_range"]
        })
        # The candidate is still fully captured as evidence.
        self.assertTrue((MATRIX / "codex" / "0.150.1" / "delete_help.txt").is_file())
        schema = json.loads(
            (MATRIX / "codex" / "0.150.1" / "app_server_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["_fixture"]["capture_status"], "supported_live_probe")

    def test_hermes_cleanup_freeze_text_stays_pinned(self) -> None:
        hermes = load_manifest()["executors"]["hermes"]
        self.assertEqual(hermes["production"]["frozen_cli_version"], "0.17.0")
        text = (MATRIX / "hermes" / "0.17.0" / "help.txt").read_text(encoding="utf-8")
        self.assertIn("Hermes Agent v0.17.0", text)

    def test_pending_capture_version_carries_no_live_capture_claim(self) -> None:
        claude = load_manifest()["executors"]["claude"]
        entry = claude["versions"]["2.1.233"]
        self.assertEqual(entry["evidence"]["live_capture"], False)
        body = json.loads(
            (MATRIX / "claude" / "2.1.233" / "version.json").read_text(encoding="utf-8")
        )
        self.assertEqual(body["captured_live"], False)


class CodexCapabilityGroupTests(unittest.TestCase):
    """Named execution/cleanup groups agree across code, fixtures and schema."""

    EXPECTED_CLEANUP = frozenset(
        {
            "thread/archive",
            "thread/archived",
            "thread/delete",
            "thread/deleted",
            "thread/read",
        }
    )
    EXPECTED_DESKTOP_VISIBILITY = frozenset({"thread/list"})

    def test_cleanup_group_membership_is_exact(self) -> None:
        definition = CODEX_APP_SERVER_CAPABILITY_GROUPS[CODEX_APP_SERVER_CLEANUP_GROUP]
        members = (
            frozenset(definition["client_methods"])
            | frozenset(definition["notifications"])
            | frozenset(definition["server_requests"])
        )
        self.assertEqual(members, self.EXPECTED_CLEANUP)
        self.assertEqual(CODEX_APP_SERVER_CAPABILITY_GROUPS, {
            "execution": {
                "client_methods": CODEX_APP_SERVER_CLIENT_METHODS,
                "server_requests": CODEX_APP_SERVER_REQUEST_METHODS,
                "notifications": CODEX_APP_SERVER_NOTIFICATIONS,
            },
            "cleanup": definition,
            "desktop_visibility": {
                "client_methods": self.EXPECTED_DESKTOP_VISIBILITY,
                "server_requests": frozenset(),
                "notifications": frozenset(),
            },
            CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP: {
                "client_methods": frozenset(),
                "server_requests": frozenset(),
                "notifications": CODEX_APP_SERVER_COLLABORATION_LIFECYCLE,
                "schema_markers": CODEX_APP_SERVER_COLLABORATION_MARKERS,
            },
        })

    def test_desktop_visibility_group_is_exact(self) -> None:
        definition = CODEX_APP_SERVER_CAPABILITY_GROUPS[
            CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP
        ]
        members = (
            frozenset(definition["client_methods"])
            | frozenset(definition["notifications"])
            | frozenset(definition["server_requests"])
        )
        self.assertEqual(members, self.EXPECTED_DESKTOP_VISIBILITY)

    def test_execution_group_matches_fixture_for_every_schema_evidence_version(
        self,
    ) -> None:
        for version in ("0.146.0", "0.147.0", "0.150.1"):
            schema = json.loads(
                (MATRIX / "codex" / version / "app_server_schema.json").read_text(
                    encoding="utf-8"
                )
            )
            for group in (
                "execution",
                CODEX_APP_SERVER_CLEANUP_GROUP,
                CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP,
            ):
                found_missing = verify_capability_group(schema, group)
                with self.subTest(version=version, group=group):
                    self.assertEqual(found_missing, [])

    def test_collaboration_group_is_fixture_evidence_only(self) -> None:
        for version in ("0.146.0", "0.147.0"):
            schema = json.loads(
                (MATRIX / "codex" / version / "app_server_schema.json").read_text(
                    encoding="utf-8"
                )
            )
            missing = verify_capability_group(
                schema,
                CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP,
            )
            with self.subTest(version=version):
                self.assertEqual(
                    set(missing),
                    set(CODEX_APP_SERVER_COLLABORATION_MARKERS),
                )
        candidate = json.loads(
            (MATRIX / "codex" / "0.150.1" / "app_server_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            verify_capability_group(
                candidate,
                CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP,
            ),
            [],
        )

    def test_unknown_capability_group_fails_closed(self) -> None:
        schema = json.loads(
            (MATRIX / "codex" / "0.146.0" / "app_server_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            verify_capability_group(schema, "teleportation"),
            ["unknown_capability_group:teleportation"],
        )


class FailClosedContractTests(unittest.TestCase):
    """Critical-event policy and early receipt are frozen for every version."""

    REQUIRED_POLICY_KEYS = (
        "unknown_critical_event_fails_closed",
        "missing_early_receipt_fails_closed",
        "incomplete_capability_group_fails_closed",
    )

    def test_every_recorded_version_freezes_fail_closed_policy(self) -> None:
        manifest = load_manifest()
        session_event_files = sorted(MATRIX.glob("*/*/session_events.json"))
        recorded = len(
            [1 for block in manifest["executors"].values() for _entry in block["versions"].values()]
        )
        self.assertEqual(len(session_event_files), recorded)
        for path in session_event_files:
            body = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(surface=str(path.relative_to(MATRIX))):
                for key in self.REQUIRED_POLICY_KEYS:
                    self.assertTrue(body["fail_closed"][key])
                self.assertTrue(body["early_receipt"]["required"])

    def test_early_receipt_extractors_are_named_per_executor(self) -> None:
        expectations = {
            "codex": "_extract_codex_session_id",
            "claude": "ClaudeExecutor._verify_init_receipt",
            "hermes": "execution_policy.extract_hermes_session_id",
        }
        for executor, needle in expectations.items():
            paths = sorted(MATRIX.glob(f"{executor}/*/session_events.json"))
            self.assertGreaterEqual(len(paths), 1)
            for path in paths:
                with self.subTest(surface=str(path.relative_to(MATRIX))):
                    text = path.read_text(encoding="utf-8")
                    if json.loads(text)["early_receipt"].get("extractor"):
                        self.assertIn(needle, text)
                    else:
                        self.assertIn("verifier", text)

    def test_decisions_surfaces_cover_approve_deny_timeout_transport_unsupported(
        self,
    ) -> None:
        required_top_level = {"approve", "deny", "timeout", "transport_lost", "unsupported"}
        decision_paths = sorted(MATRIX.glob("*/*/decisions.json"))
        self.assertGreaterEqual(len(decision_paths), 3)
        for path in decision_paths:
            body = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(surface=str(path.relative_to(MATRIX))):
                self.assertLessEqual(required_top_level, set(body))
                self.assertTrue(body["timeout"]["fail_closed"])
                self.assertTrue(body["transport_lost"]["fail_closed"])

    def test_codex_cleanup_group_names_the_archive_and_delete_members(self) -> None:
        expected = frozenset(
            {
                "thread/archive",
                "thread/archived",
                "thread/delete",
                "thread/deleted",
                "thread/read",
            }
        )
        body = json.loads(
            (MATRIX / "codex" / "0.146.0" / "app_server_cleanup.json").read_text(
                encoding="utf-8"
            )
        )
        members = set(body["exact_members"]["requests"]) | set(
            body["exact_members"]["notifications"]
        )
        self.assertEqual(members, expected)
        self.assertFalse(body["changes_default_production_behavior"])


class RedactionInvariantTests(unittest.TestCase):
    """Stored captures must stay free of private material."""

    FORBIDDEN_TEXT = [
        re.compile(r"/Users/[A-Za-z0-9._-]+"),
        re.compile(r"/home/[A-Za-z0-9._-]+"),
        re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ]

    def test_no_private_paths_or_credentials_inside_matrix(self) -> None:
        scanned = 0
        for path in sorted(MATRIX.rglob("*")):
            if not path.is_file() or path.suffix not in {".json", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in self.FORBIDDEN_TEXT:
                with self.subTest(file=str(path.relative_to(MATRIX)), pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))
            scanned += 1
        self.assertGreater(scanned, 20)


if __name__ == "__main__":
    unittest.main()
