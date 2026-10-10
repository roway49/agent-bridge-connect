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
import os
import tempfile
from unittest import mock as _oc_mock
from agent_bridge_connect.executors.opencode import OpenCodeExecutor
from tests.support.opencode_fakes import (jsonl, make_fake_opencode, make_task_packet, step_finish_event, text_event, tool_use_event)
import json
import re
import unittest
from pathlib import Path
from agent_bridge_connect.claude_path_capability import CLAUDE_PATH_CAPABILITY_MAX_VERSION, CLAUDE_PATH_CAPABILITY_MIN_VERSION
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
import json as _executor_fixture_json
import unittest as _executor_fixture_unittest
from pathlib import Path as _executor_fixture_Path
from agent_bridge_connect.execution_policy import extract_hermes_session_id as _executor_fixture_extract_hermes_session_id
from agent_bridge_connect.executors.hermes import _iteration_budget_diagnostics as _executor_fixture__iteration_budget_diagnostics
import json as _executor_protocol_json
import re as _executor_protocol_re
import unittest as _executor_protocol_unittest
from pathlib import Path as _executor_protocol_Path
from agent_bridge_connect.codex_app_server import codex_app_server_contract as _executor_protocol_codex_app_server_contract
from agent_bridge_connect.execution_policy import extract_hermes_session_id as _executor_protocol_extract_hermes_session_id
from agent_bridge_connect.executors.codex import _codex_has_exact_session_delete_entry as _executor_protocol__codex_has_exact_session_delete_entry, _extract_codex_session_id as _executor_protocol__extract_codex_session_id
from agent_bridge_connect.executors.hermes import _hermes_has_exact_session_delete_entry as _executor_protocol__hermes_has_exact_session_delete_entry, _iteration_budget_diagnostics as _executor_protocol__iteration_budget_diagnostics

FIXTURES = Path(__file__).parents[1] / "fixtures" / "executor_runtime"
MATRIX = FIXTURES / "matrix"
MANIFEST = MATRIX / "manifest.json"
VERSION_RE = re.compile("^(\\d+)\\.(\\d+)\\.(\\d+)$")


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.match(str(text or "").strip())
    if match is None:
        return None
    return tuple((int(part) for part in match.groups()))


class ManifestIntegrityTests(unittest.TestCase):
    """Every file in the matrix must be listed exactly once by the manifest."""

    def test_manifest_exists_with_expected_shape(self) -> None:
        data = load_manifest()
        self.assertEqual(data["schema_version"], "PROTO-104-001/1")
        self.assertEqual(set(data["executors"]), {"codex", "claude", "hermes"})

    def test_required_versions_are_recorded(self) -> None:
        manifest = load_manifest()
        expected = {"codex": {"0.146.0", "0.147.0", "0.150.1"}, "claude": {"2.1.226", "2.1.233"}, "hermes": {"0.17.0", "0.20.1"}}
        for executor, versions in expected.items():
            with self.subTest(executor=executor):
                self.assertEqual(set(manifest["executors"][executor]["versions"]), versions)

    def test_captured_cli_is_executor_level_evidence(self) -> None:
        manifest = load_manifest()
        probe = manifest["executors"]["claude"]["captured_cli_2_1_247"]
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
            entries = [("", relative, metadata) for (relative, metadata) in block["shared_surfaces"].items()] + [(version, relative, metadata) for (version, entry) in block["versions"].items() for (relative, metadata) in entry["surfaces"].items()]
            for version_label, relative, metadata in entries:
                directory = MATRIX if not version_label else MATRIX / executor / version_label
                path = directory / relative
                with self.subTest(executor=executor, version=version_label or "shared", surface=relative):
                    self.assertTrue(path.is_file(), f"{path} is missing")
                    payload = path.read_bytes()
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), metadata["sha256"])
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
        self.assertEqual([path.name for path in summaries], ["app_server_v2_contract_summary.json"])
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
        self.assertEqual(parse_version(block["path_capability_bounds"]["min_inclusive"]), CLAUDE_PATH_CAPABILITY_MIN_VERSION)
        self.assertEqual(parse_version(block["path_capability_bounds"]["max_exclusive"]), CLAUDE_PATH_CAPABILITY_MAX_VERSION)

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
                    (module_name, _, attribute) = binding.rpartition(".")
                    module = importlib.import_module(module_name)
                    self.assertTrue(hasattr(module, attribute))

    def test_supported_range_matches_statuses(self) -> None:
        manifest = load_manifest()
        for executor, block in manifest["executors"].items():
            supported = {version for (version, entry) in block["versions"].items() if entry["in_production_supported_range"]}
            with self.subTest(executor=executor):
                self.assertEqual(supported, {"codex": {"0.146.0", "0.147.0", "0.150.1"}, "claude": {"2.1.226"}, "hermes": {"0.17.0"}}[executor])


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
                    version_surface = json.loads((MATRIX / executor / version / "version.json").read_text(encoding="utf-8"))
                    status = version_surface.get("status")
                    self.assertIn(status, ("candidate", "pending_capture"), f"{executor}/{version} must not look production-ready")

    def test_promoted_collaboration_binary_is_frozen_in_codex_gate(self) -> None:
        codex = load_manifest()["executors"]["codex"]
        self.assertIn("0.150.1", {version for (version, entry) in codex["versions"].items() if entry["in_production_supported_range"]})
        self.assertTrue((MATRIX / "codex" / "0.150.1" / "delete_help.txt").is_file())
        schema = json.loads((MATRIX / "codex" / "0.150.1" / "app_server_schema.json").read_text(encoding="utf-8"))
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
        body = json.loads((MATRIX / "claude" / "2.1.233" / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(body["captured_live"], False)


class CodexCapabilityGroupTests(unittest.TestCase):
    """Named execution/cleanup groups agree across code, fixtures and schema."""

    EXPECTED_CLEANUP = frozenset({"thread/archive", "thread/archived", "thread/delete", "thread/deleted", "thread/read"})
    EXPECTED_DESKTOP_VISIBILITY = frozenset({"thread/list"})

    def test_cleanup_group_membership_is_exact(self) -> None:
        definition = CODEX_APP_SERVER_CAPABILITY_GROUPS[CODEX_APP_SERVER_CLEANUP_GROUP]
        members = frozenset(definition["client_methods"]) | frozenset(definition["notifications"]) | frozenset(definition["server_requests"])
        self.assertEqual(members, self.EXPECTED_CLEANUP)
        self.assertEqual(
            CODEX_APP_SERVER_CAPABILITY_GROUPS,
            {
                "execution": {"client_methods": CODEX_APP_SERVER_CLIENT_METHODS, "server_requests": CODEX_APP_SERVER_REQUEST_METHODS, "notifications": CODEX_APP_SERVER_NOTIFICATIONS},
                "cleanup": definition,
                "desktop_visibility": {"client_methods": self.EXPECTED_DESKTOP_VISIBILITY, "server_requests": frozenset(), "notifications": frozenset()},
                CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP: {"client_methods": frozenset(), "server_requests": frozenset(), "notifications": CODEX_APP_SERVER_COLLABORATION_LIFECYCLE, "schema_markers": CODEX_APP_SERVER_COLLABORATION_MARKERS},
            },
        )

    def test_desktop_visibility_group_is_exact(self) -> None:
        definition = CODEX_APP_SERVER_CAPABILITY_GROUPS[CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP]
        members = frozenset(definition["client_methods"]) | frozenset(definition["notifications"]) | frozenset(definition["server_requests"])
        self.assertEqual(members, self.EXPECTED_DESKTOP_VISIBILITY)

    def test_execution_group_matches_fixture_for_every_schema_evidence_version(self) -> None:
        for version in ("0.146.0", "0.147.0", "0.150.1"):
            schema = json.loads((MATRIX / "codex" / version / "app_server_schema.json").read_text(encoding="utf-8"))
            for group in ("execution", CODEX_APP_SERVER_CLEANUP_GROUP, CODEX_APP_SERVER_DESKTOP_VISIBILITY_GROUP):
                found_missing = verify_capability_group(schema, group)
                with self.subTest(version=version, group=group):
                    self.assertEqual(found_missing, [])

    def test_collaboration_group_is_fixture_evidence_only(self) -> None:
        for version in ("0.146.0", "0.147.0"):
            schema = json.loads((MATRIX / "codex" / version / "app_server_schema.json").read_text(encoding="utf-8"))
            missing = verify_capability_group(schema, CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP)
            with self.subTest(version=version):
                self.assertEqual(set(missing), set(CODEX_APP_SERVER_COLLABORATION_MARKERS))
        candidate = json.loads((MATRIX / "codex" / "0.150.1" / "app_server_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(verify_capability_group(candidate, CODEX_APP_SERVER_COLLABORATION_SPAWN_GROUP), [])

    def test_unknown_capability_group_fails_closed(self) -> None:
        schema = json.loads((MATRIX / "codex" / "0.146.0" / "app_server_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(verify_capability_group(schema, "teleportation"), ["unknown_capability_group:teleportation"])


class FailClosedContractTests(unittest.TestCase):
    """Critical-event policy and early receipt are frozen for every version."""

    REQUIRED_POLICY_KEYS = ("unknown_critical_event_fails_closed", "missing_early_receipt_fails_closed", "incomplete_capability_group_fails_closed")

    def test_every_recorded_version_freezes_fail_closed_policy(self) -> None:
        manifest = load_manifest()
        session_event_files = sorted(MATRIX.glob("*/*/session_events.json"))
        recorded = len([1 for block in manifest["executors"].values() for _entry in block["versions"].values()])
        self.assertEqual(len(session_event_files), recorded)
        for path in session_event_files:
            body = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(surface=str(path.relative_to(MATRIX))):
                for key in self.REQUIRED_POLICY_KEYS:
                    self.assertTrue(body["fail_closed"][key])
                self.assertTrue(body["early_receipt"]["required"])

    def test_early_receipt_extractors_are_named_per_executor(self) -> None:
        expectations = {"codex": "_extract_codex_session_id", "claude": "ClaudeExecutor._verify_init_receipt", "hermes": "execution_policy.extract_hermes_session_id"}
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

    def test_decisions_surfaces_cover_approve_deny_timeout_transport_unsupported(self) -> None:
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
        expected = frozenset({"thread/archive", "thread/archived", "thread/delete", "thread/deleted", "thread/read"})
        body = json.loads((MATRIX / "codex" / "0.146.0" / "app_server_cleanup.json").read_text(encoding="utf-8"))
        members = set(body["exact_members"]["requests"]) | set(body["exact_members"]["notifications"])
        self.assertEqual(members, expected)
        self.assertFalse(body["changes_default_production_behavior"])


class RedactionInvariantTests(unittest.TestCase):
    """Stored captures must stay free of private material."""

    FORBIDDEN_TEXT = [re.compile("/Users/[A-Za-z0-9._-]+"), re.compile("/home/[A-Za-z0-9._-]+"), re.compile("(?i)bearer\\s+[A-Za-z0-9._~+/-]{16,}"), re.compile("\\bsk-[A-Za-z0-9_-]{20,}\\b"), re.compile("gh[pousr]_[A-Za-z0-9]{20,}")]

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


_executor_fixture_FIXTURES = _executor_fixture_Path(__file__).parents[1] / "fixtures" / "executor_runtime"
_executor_fixture_MATRIX = _executor_fixture_FIXTURES / "matrix"


class _executor_fixture_ExecutorContractFixtureTests(_executor_fixture_unittest.TestCase):
    def test_claude_2_1_226_capability_snapshot(self) -> None:
        text = (_executor_fixture_MATRIX / "claude" / "2.1.226" / "help.txt").read_text(encoding="utf-8")
        for flag in ("--max-budget-usd", "--no-session-persistence", "--resume", "--session-id", "project purge", "--yes"):
            self.assertIn(flag, text)

    def test_claude_budget_exhaustion_snapshot_is_not_mislabeled_live(self) -> None:
        payload = _executor_fixture_json.loads((_executor_fixture_MATRIX / "claude" / "2.1.226" / "budget_exhaustion.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["structured_subtype"], "error_max_budget_usd")
        self.assertFalse(payload["verified_by_live_paid_canary"])

    def test_hermes_0_17_0_capability_snapshot(self) -> None:
        text = (_executor_fixture_MATRIX / "hermes" / "0.17.0" / "help.txt").read_text(encoding="utf-8")
        self.assertIn("--max-turns", text)
        self.assertIn("--resume", text)
        self.assertIn("sessions delete", text)

    def test_hermes_session_receipt_and_exhaustion_snapshots(self) -> None:
        payload = _executor_fixture_json.loads((_executor_fixture_MATRIX / "hermes" / "0.17.0" / "receipt_samples.json").read_text(encoding="utf-8"))
        self.assertEqual(_executor_fixture_extract_hermes_session_id(payload["session_receipt_stderr"]), "20260810_010203_a1b2c3")
        for output in payload["iteration_exhaustion"]:
            with self.subTest(output=output):
                diagnostics = _executor_fixture__iteration_budget_diagnostics(output, "")
                self.assertTrue(diagnostics["iteration_exhausted"])


_executor_protocol_FIXTURES = _executor_protocol_Path(__file__).parents[1] / "fixtures" / "executor_runtime"
_executor_protocol_MATRIX = _executor_protocol_FIXTURES / "matrix"
_executor_protocol_MANIFEST = _executor_protocol_json.loads((_executor_protocol_MATRIX / "manifest.json").read_text(encoding="utf-8"))


def _executor_protocol_load(executor: str, version: str, surface: str) -> dict:
    return _executor_protocol_json.loads((_executor_protocol_MATRIX / executor / version / surface).read_text(encoding="utf-8"))


def _executor_protocol_load_text(executor: str, version: str, surface: str) -> str:
    return (_executor_protocol_MATRIX / executor / version / surface).read_text(encoding="utf-8")


def _executor_protocol_iter_versions(executor: str):
    for version, entry in _executor_protocol_MANIFEST["executors"][executor]["versions"].items():
        yield (version, entry)


class _executor_protocol_CodexSurfaceTests(_executor_protocol_unittest.TestCase):
    """Receipt parsing and cleanup freeze behavior across Codex versions."""

    def test_unique_thread_started_receipt_is_accepted_for_every_version(self) -> None:
        events = [{"payload": {"type": "thread.started", "thread_id": "019fe9f4-receipt"}}]
        for version, _entry in _executor_protocol_iter_versions("codex"):
            with self.subTest(version=version):
                self.assertEqual(_executor_protocol__extract_codex_session_id(events), "019fe9f4-receipt")

    def test_missing_or_duplicate_receipts_fail_closed(self) -> None:
        samples = _executor_protocol_load("codex", "0.146.0", "receipt_samples.json")
        self.assertEqual(_executor_protocol__extract_codex_session_id([]), "")
        for name in ("missing_receipt_jsonl", "duplicate_receipt_jsonl"):
            events = [{"payload": _executor_protocol_json.loads(line)} for line in samples[name].strip().splitlines()]
            with self.subTest(sample=name):
                self.assertEqual(_executor_protocol__extract_codex_session_id(events), "")

    def test_malformed_thread_id_fails_closed(self) -> None:
        self.assertEqual(_executor_protocol__extract_codex_session_id([{"payload": {"type": "thread.started", "thread_id": " bad id\n"}}]), "")

    def test_unknown_critical_event_is_never_treated_as_a_receipt(self) -> None:
        future_events = [{"payload": {"type": "thread.started.v3", "threadId": "future-id"}}, {"payload": {"type": "unknown.critical.event", "session": "x"}}]
        self.assertEqual(_executor_protocol__extract_codex_session_id(future_events), "")

    def test_frozen_delete_help_qualifies_and_candidate_help_matches(self) -> None:
        frozen = _executor_protocol_load_text("codex", "0.146.0", "delete_help.txt")
        candidate = _executor_protocol_load_text("codex", "0.150.1", "delete_help.txt")
        for text in (frozen, candidate):
            with self.subTest(len(text)):
                self.assertTrue(_executor_protocol__codex_has_exact_session_delete_entry(text))
                self.assertIn("--force", text)
                self.assertNotIn("picker", text.lower())

    def test_fuzzy_or_global_entries_do_not_qualify(self) -> None:
        for text in ("usage: codex delete [OPTIONS] <SESSION|NAME>\n--last picker", "usage: codex delete old sessions prune purge"):
            with self.subTest(text=text[:40]):
                self.assertFalse(_executor_protocol__codex_has_exact_session_delete_entry(text))

    def test_versions_do_not_gate_a_compatible_protocol_surface(self) -> None:
        boundary_cases = {"codex-cli 0.145.9": "schema", "codex-cli 0.146.0": "schema", "codex-cli 0.147.0": "schema", "codex-cli 0.148.0": "schema", "codex-cli 0.150.1": "schema", "codex-cli 0.153.4": "schema", "not-a-version": "schema", "": "unavailable"}
        for output, expected_reason in boundary_cases.items():
            with self.subTest(output=output or "<empty>"):
                probe = _executor_protocol_codex_app_server_contract("/nonexistent/codex", version_output=output, schema_bundle={"definitions": {}}, timeout=1)
                self.assertFalse(probe["ok"])
                self.assertIn(expected_reason, probe["reason"])
                if output:
                    self.assertIn("schema", probe["reason"])


class _executor_protocol_HermesSurfaceTests(_executor_protocol_unittest.TestCase):
    """ACP/receipt/exhaustion semantics across Hermes versions."""

    def test_acp_initialize_protocol_version_drift_fails_closed(self) -> None:
        from agent_bridge_connect.hermes_acp import HERMES_ACP_PROTOCOL_VERSION, validate_initialize_result

        good = {"protocolVersion": HERMES_ACP_PROTOCOL_VERSION}
        self.assertEqual(validate_initialize_result(good), HERMES_ACP_PROTOCOL_VERSION)
        for payload in ({"protocolVersion": 2}, {"protocolVersion": "1"}, {}, None):
            with self.subTest(payload=payload):
                with self.assertRaises(Exception):
                    validate_initialize_result(payload)

    def test_session_receipt_extraction_from_frozen_sample(self) -> None:
        body = _executor_protocol_load("hermes", "0.17.0", "session_events.json")
        stderr_sample = body["receipt_sample_stderr"]
        self.assertEqual(_executor_protocol_extract_hermes_session_id(stderr_sample), "20260810_010203_a1b2c3")

    def test_every_hermes_version_records_an_early_receipt_rule(self) -> None:
        receipt_sample = _executor_protocol_load("hermes", "0.17.0", "session_events.json")["receipt_sample_stderr"]
        self.assertEqual(_executor_protocol_extract_hermes_session_id(receipt_sample), "20260810_010203_a1b2c3")
        for version, _entry in _executor_protocol_iter_versions("hermes"):
            body = _executor_protocol_load("hermes", version, "session_events.json")
            with self.subTest(version=version):
                self.assertTrue(body["early_receipt"]["required"])
                frozen_pattern = _executor_protocol_re.compile(body["early_receipt"]["format_regex"])
                self.assertTrue(frozen_pattern.fullmatch("20260810_010203_a1b2c3"))
                for rejected in ("", "not-a-session", "20260810_010203", "2026_1_2_abc", "../escape/session"):
                    self.assertIsNone(frozen_pattern.fullmatch(rejected))

    def test_iteration_exhaustion_detectors_match_frozen_strings(self) -> None:
        body = _executor_protocol_load("hermes", "0.17.0", "resource_exhaustion.json")
        self.assertTrue(body["iteration_detector_strings"])
        for output in body["iteration_detector_strings"]:
            with self.subTest(output=output):
                diagnostics = _executor_protocol__iteration_budget_diagnostics(output, "")
                self.assertTrue(diagnostics["iteration_exhausted"])

    def test_non_exhaustion_output_is_not_flagged(self) -> None:
        diagnostics = _executor_protocol__iteration_budget_diagnostics("turn complete: 12/60 iterations", "")
        self.assertFalse(diagnostics["iteration_exhausted"])

    def test_sessions_delete_entry_freeze(self) -> None:
        frozen = _executor_protocol_load_text("hermes", "0.17.0", "help.txt")
        candidate = _executor_protocol_load_text("hermes", "0.20.1", "sessions_delete_help.txt")
        for text in (frozen, candidate):
            with self.subTest(length=len(text)):
                self.assertTrue(_executor_protocol__hermes_has_exact_session_delete_entry(text))

    def test_resume_picker_entries_do_not_qualify_for_cleanup(self) -> None:
        entries_that_never_qualify = _executor_protocol_load("hermes", "0.17.0", "session_delete.json")["entries_that_never_qualify"]
        self.assertIn("--continue", entries_that_never_qualify)
        text = "usage: hermes --continue [SESSION_NAME]\npicker\nprune\npurge"
        self.assertFalse(_executor_protocol__hermes_has_exact_session_delete_entry(text))

    def test_acp_probe_surface_is_recorded_for_both_versions(self) -> None:
        frozen = _executor_protocol_load("hermes", "0.17.0", "acp_probe.json")
        candidate = _executor_protocol_load("hermes", "0.20.1", "acp_probe.json")
        self.assertFalse(frozen["captured"])
        self.assertTrue(candidate["captured"])
        self.assertEqual(candidate["version_output"].strip(), "0.20.1")
        self.assertEqual(candidate["initialize_requirement"]["protocolVersion"], 1)


class _executor_protocol_ClaudeSurfaceTests(_executor_protocol_unittest.TestCase):
    """Path capability, project purge and budget exhaustion surfaces."""

    def test_path_capability_bounds_exclude_pending_capture_helper(self) -> None:
        from agent_bridge_connect.claude_path_capability import CLAUDE_PATH_CAPABILITY_MAX_VERSION, CLAUDE_PATH_CAPABILITY_MIN_VERSION
        from agent_bridge_connect.codex_app_server import parse_codex_version

        for version, _entry in _executor_protocol_iter_versions("claude"):
            body = _executor_protocol_load("claude", version, "path_capability.json")
            with self.subTest(version=version):
                self.assertEqual(body["production_bounds"]["min_inclusive"], ".".join(map(str, CLAUDE_PATH_CAPABILITY_MIN_VERSION)))
                self.assertEqual(body["production_bounds"]["max_exclusive"], ".".join(map(str, CLAUDE_PATH_CAPABILITY_MAX_VERSION)))
        parsed_low = parse_codex_version("2.1.226")
        self.assertTrue(CLAUDE_PATH_CAPABILITY_MIN_VERSION <= parsed_low < CLAUDE_PATH_CAPABILITY_MAX_VERSION)

    def test_budget_exhaustion_subtype_matches_frozen_snapshot(self) -> None:
        legacy = _executor_protocol_json.loads((_executor_protocol_MATRIX / "claude" / "2.1.226" / "budget_exhaustion.json").read_text(encoding="utf-8"))
        derived = _executor_protocol_load("claude", "2.1.226", "resource_exhaustion.json")
        pending = _executor_protocol_load("claude", "2.1.233", "resource_exhaustion.json")
        self.assertEqual(legacy["structured_subtype"], "error_max_budget_usd")
        self.assertEqual(derived["structured_subtype"], legacy["structured_subtype"])
        self.assertFalse(derived["verified_by_live_paid_canary"])
        self.assertEqual(pending["identical_to"], "claude/2.1.226")

    def test_project_purge_requires_the_documented_usage_line(self) -> None:
        from agent_bridge_connect.executors.claude import _supports_claude_project_purge

        help_text = _executor_protocol_load_text("claude", "2.1.226", "help.txt")
        self.assertTrue(_supports_claude_project_purge(help_text))
        for broken in ("", "Usage: claude something else"):
            with self.subTest(broken=broken or "<empty>"):
                self.assertFalse(_supports_claude_project_purge(broken))

    def test_pending_capture_purge_surface_admits_no_evidence(self) -> None:
        body = _executor_protocol_load("claude", "2.1.233", "project_purge.json")
        self.assertFalse(body["help_entry_captured"])
        self.assertEqual(body["status"], "pending_capture")

    def test_init_receipt_rule_present_for_all_claude_versions(self) -> None:
        from agent_bridge_connect.executors.claude import _STREAM_INIT_SUBTYPE

        for version, _entry in _executor_protocol_iter_versions("claude"):
            body = _executor_protocol_load("claude", version, "session_events.json")
            with self.subTest(version=version):
                self.assertEqual(body["early_receipt"]["subtype"], _STREAM_INIT_SUBTYPE)
                self.assertTrue(body["early_receipt"]["required"])

    def test_tool_fence_argv_is_stable_in_the_matrix(self) -> None:
        for version, _entry in _executor_protocol_iter_versions("claude"):
            body = _executor_protocol_load("claude", version, "argv_contract.json")
            with self.subTest(version=version):
                self.assertEqual(body["tool_fence_args"][1], "TaskCreate,TaskUpdate,TodoWrite")


class _executor_protocol_ArgvAndEnvironmentContractTests(_executor_protocol_unittest.TestCase):
    """Canonical argv/cwd/writable-root/environment contracts per executor."""

    REQUIRED_KEYS = {"cwd", "controlled_environment"}

    def test_every_argv_contract_declares_cwd_and_environment(self) -> None:
        argv_paths = sorted(_executor_protocol_MATRIX.glob("*/*/argv_contract.json"))
        self.assertGreaterEqual(len(argv_paths), 7)
        for path in argv_paths:
            body = _executor_protocol_json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(surface=str(path.relative_to(_executor_protocol_MATRIX))):
                self.assertLessEqual(self.REQUIRED_KEYS, set(body))
                environment = body["controlled_environment"]
                self.assertFalse(environment.get("leaks_parent_secrets"))
                self.assertIsInstance(environment, dict)

    def test_writable_roots_are_explicit_per_executor(self) -> None:
        expectations = {"codex": "--add-dir", "claude": "--add-dir"}
        for executor, flag in expectations.items():
            paths = sorted(_executor_protocol_MATRIX.glob(f"{executor}/*/argv_contract.json"))
            self.assertGreaterEqual(len(paths), 1)
            for path in paths:
                with self.subTest(surface=str(path.relative_to(_executor_protocol_MATRIX))):
                    text = path.read_text(encoding="utf-8")
                    self.assertIn(flag, text)
        hermes_body = _executor_protocol_load("hermes", "0.17.0", "argv_contract.json")
        self.assertEqual(hermes_body["full_permission_flag"], "--yolo")
        self.assertEqual(hermes_body["controlled_environment"], {})


class OpenCodeProtocolGateRepairTests(unittest.TestCase):
    """R1 counterexamples: a callback alone never proves completion.

    These regressions were supplied by the CNWV-001 independent review
    (missing stop, cross-session callback, final rejection + callback,
    legitimate recovery after a tool error, malformed/truncated tail).
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.board = self.tmp / "board"
        self.board.mkdir()
        self.packet = make_task_packet(self.project, board=self.board)
        self.executor = OpenCodeExecutor(command="/bin/echo")

    @staticmethod
    def _marker(task_id: str, *, final_state: str = "completed") -> str:
        payload = {
            "version": 1,
            "task_id": task_id,
            "final_state": final_state,
            "summary": "repair regression",
            "step_results": [{"id": 1, "status": "done"}],
        }
        return "AGENTBC_FINAL_CALLBACK: " + json.dumps(payload, ensure_ascii=False)

    def _poll(self, events: list[dict]) -> object:
        return self.executor._build_poll_result(
            self.packet,
            "repair-run",
            "inherit",
            {
                "stdout": jsonl(events),
                "stderr": "",
                "returncode": 0,
                "cleanup_confirmed": True,
            },
        )

    def test_callback_without_stop_is_not_completed(self) -> None:
        poll = self._poll(
            [
                text_event(self._marker("FAKE-001")),
                step_finish_event("tool-calls"),
            ]
        )
        self.assertFalse(poll.result["stop_finish_seen"])
        self.assertNotEqual(poll.status, "completed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(poll.result["failure"]["kind"], "opencode_stop_boundary_missing")

    def test_cross_session_callback_is_not_completed(self) -> None:
        poll = self._poll(
            [
                text_event(self._marker("FAKE-001"), session_id="s1"),
                step_finish_event("stop", session_id="s2"),
            ]
        )
        self.assertTrue(poll.result["session_ambiguous"])
        self.assertNotEqual(poll.status, "completed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertEqual(poll.result["failure"]["kind"], "opencode_session_ambiguous")

    def test_final_permission_rejection_with_callback_is_not_completed(self) -> None:
        poll = self._poll(
            [
                tool_use_event(status="error", error={"message": "permission rejected"}),
                text_event(self._marker("FAKE-001")),
                step_finish_event("stop"),
            ]
        )
        self.assertEqual(poll.result["unrecovered_tool_error_count"], 1)
        self.assertTrue(poll.result["unrecovered_permission_rejected"])
        self.assertNotEqual(poll.status, "completed")
        self.assertEqual(poll.result["failure"]["kind"], "opencode_tool_error_unrecovered")

    def test_later_execution_invalidates_earlier_completed_turn(self) -> None:
        tails = [
            [{"type": "step_start", "sessionID": "ses-main"}],
            [text_event("still executing"), step_finish_event("tool-calls")],
            [tool_use_event(status="running")],
            [{"type": "reasoning", "part": {"text": "working"}}],
        ]
        for tail in tails:
            with self.subTest(tail=tail):
                poll = self._poll([
                    text_event(self._marker("FAKE-001")), step_finish_event("stop"), *tail,
                ])
                self.assertNotEqual(poll.status, "completed")
                self.assertEqual(poll.result["failure"]["kind"], "opencode_stop_boundary_missing")

    def test_unknown_metadata_after_completion_is_allowed(self) -> None:
        poll = self._poll([
            text_event(self._marker("FAKE-001")), step_finish_event("stop"),
            {"type": "future_metadata", "part": {"usage": 1}},
        ])
        self.assertEqual(poll.status, "completed")

    def test_tool_recovery_requires_same_identified_action(self) -> None:
        for call_id, command in [("other-call", "pwd"), ("other-call", "write secret"), ("call-1", "pwd"), ("", "write secret")]:
            with self.subTest(call_id=call_id, command=command):
                failed = tool_use_event(status="error", error="permission denied")
                failed["part"]["state"]["input"] = {"command": "write secret"}
                success = tool_use_event(status="completed")
                success["part"]["callID"] = call_id
                success["part"]["state"]["input"] = {"command": command}
                poll = self._poll([
                    failed, success, text_event(self._marker("FAKE-001")), step_finish_event("stop"),
                ])
                self.assertNotEqual(poll.status, "completed")
                self.assertEqual(poll.result["unrecovered_tool_error_count"], 1)
                self.assertNotIn("write secret", json.dumps(poll.result))

    def test_same_call_and_input_recovers_without_public_input(self) -> None:
        failed = tool_use_event(status="error", error="transient")
        success = tool_use_event(status="completed")
        failed["part"]["state"]["input"] = {"command": "write secret", "cwd": "/private"}
        success["part"]["state"]["input"] = {"cwd": "/private", "command": "write secret"}
        poll = self._poll([
            failed, success, text_event(self._marker("FAKE-001")), step_finish_event("stop"),
        ])
        self.assertEqual(poll.status, "completed")
        self.assertNotIn("write secret", json.dumps(poll.result))

    def test_recovered_tool_error_allows_completion(self) -> None:
        poll = self._poll(
            [
                tool_use_event(status="error", error={"message": "transient boom"}),
                tool_use_event(status="completed"),
                text_event(self._marker("FAKE-001")),
                step_finish_event("stop"),
            ]
        )
        self.assertEqual(poll.result["unrecovered_tool_error_count"], 0)
        self.assertEqual(poll.status, "completed")
        self.assertIsNotNone(poll.result["agent_callback"])

    def test_non_permission_tool_error_is_not_labelled_a_permission_rejection(self) -> None:
        poll = self._poll(
            [
                tool_use_event(status="error", error={"message": "network reset"}),
                step_finish_event("tool-calls"),
            ]
        )
        self.assertEqual(poll.result["tool_error_count"], 1)
        self.assertFalse(poll.result["permission_rejected"])
        self.assertFalse(poll.result["auto_rejected"])

    def test_malformed_and_truncated_tail_do_not_fabricate_completion(self) -> None:
        events = make_fake_opencode(self.tmp)
        stdout_file = self.tmp / "truncated.jsonl"
        stdout_file.write_text(
            '{"type":"text","sessionID":"s1","part":{"te'  # truncated tail, no newline
            + "\n"
            + "not-json\n"
            + json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "s1",
                    "part": {"reason": "stop"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        executor = OpenCodeExecutor(command=str(events))
        with _oc_mock.patch.dict(
            os.environ, {"OPENCODE_FAKE_STDOUT_FILE": str(stdout_file)}
        ):
            started = executor.start(dict(self.packet))
        poll = executor.poll(started.run_id)
        self.assertNotEqual(poll.status, "completed")
        self.assertIsNone(poll.result["agent_callback"])
        self.assertGreaterEqual(poll.result["parse_diagnostics"]["malformed_lines"], 1)
        self.assertEqual(poll.result["final_marker_count"], 0)
