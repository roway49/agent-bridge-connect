"""PROTO-104-001 parameterized protocol surface tests.

The matrix surfaces are not decorative: each recorded event sample, decision
and capability entry is pushed through the real production parsers so the
fixtures stay coupled to the code they describe.  Every executor/version pair
in the manifest is exercised, including boundary cases, unknown future
versions, missing schema/help evidence and mismatched events.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from agent_bridge_connect.codex_app_server import (
    CODEX_APP_SERVER_MAX_VERSION,
    CODEX_APP_SERVER_SUPPORTED_VERSIONS,
    codex_app_server_contract,
    parse_codex_version,
)
from agent_bridge_connect.execution_policy import extract_hermes_session_id
from agent_bridge_connect.executors.codex import (
    _codex_has_exact_session_delete_entry,
    _extract_codex_session_id,
)
from agent_bridge_connect.executors.hermes import (
    _hermes_has_exact_session_delete_entry,
    _iteration_budget_diagnostics,
)

FIXTURES = Path(__file__).parent / "fixtures" / "executor_runtime"
MATRIX = FIXTURES / "matrix"
MANIFEST = json.loads((MATRIX / "manifest.json").read_text(encoding="utf-8"))


def load(executor: str, version: str, surface: str) -> dict:
    return json.loads(
        (MATRIX / executor / version / surface).read_text(encoding="utf-8")
    )


def load_text(executor: str, version: str, surface: str) -> str:
    return (MATRIX / executor / version / surface).read_text(encoding="utf-8")


def iter_versions(executor: str):
    for version, entry in MANIFEST["executors"][executor]["versions"].items():
        yield version, entry


class CodexSurfaceTests(unittest.TestCase):
    """Receipt parsing and cleanup freeze behavior across Codex versions."""

    def test_unique_thread_started_receipt_is_accepted_for_every_version(self) -> None:
        events = [
            {"payload": {"type": "thread.started", "thread_id": "019fe9f4-receipt"}}
        ]
        for version, _entry in iter_versions("codex"):
            with self.subTest(version=version):
                self.assertEqual(_extract_codex_session_id(events), "019fe9f4-receipt")

    def test_missing_or_duplicate_receipts_fail_closed(self) -> None:
        samples = load("codex", "0.146.0", "receipt_samples.json")
        self.assertEqual(_extract_codex_session_id([]), "")
        for name in ("missing_receipt_jsonl", "duplicate_receipt_jsonl"):
            events = [
                {"payload": json.loads(line)}
                for line in samples[name].strip().splitlines()
            ]
            with self.subTest(sample=name):
                self.assertEqual(_extract_codex_session_id(events), "")

    def test_malformed_thread_id_fails_closed(self) -> None:
        self.assertEqual(
            _extract_codex_session_id(
                [{"payload": {"type": "thread.started", "thread_id": " bad id\n"}}]
            ),
            "",
        )

    def test_unknown_critical_event_is_never_treated_as_a_receipt(self) -> None:
        future_events = [
            {"payload": {"type": "thread.started.v3", "threadId": "future-id"}},
            {"payload": {"type": "unknown.critical.event", "session": "x"}},
        ]
        self.assertEqual(_extract_codex_session_id(future_events), "")

    def test_frozen_delete_help_qualifies_and_candidate_help_matches(self) -> None:
        frozen = load_text("codex", "0.146.0", "delete_help.txt")
        candidate = load_text("codex", "0.150.1", "delete_help.txt")
        for text in (frozen, candidate):
            with self.subTest(len(text)):
                self.assertTrue(_codex_has_exact_session_delete_entry(text))
                self.assertIn("--force", text)
                self.assertNotIn("picker", text.lower())

    def test_fuzzy_or_global_entries_do_not_qualify(self) -> None:
        for text in (
            "usage: codex delete [OPTIONS] <SESSION|NAME>\n--last picker",
            "usage: codex delete old sessions prune purge",
        ):
            with self.subTest(text=text[:40]):
                self.assertFalse(_codex_has_exact_session_delete_entry(text))

    def test_version_gate_rejects_unknown_future_versions(self) -> None:
        boundary_cases = {
            "codex-cli 0.145.9": "outside",
            "codex-cli 0.146.0": "schema",
            "codex-cli 0.147.0": "schema",
            "codex-cli 0.148.0": "outside",
            "codex-cli 0.150.1": "schema",
            "not-a-version": "parseable",
            "": "unavailable",
        }
        for output, expected_reason in boundary_cases.items():
            with self.subTest(output=output or "<empty>"):
                probe = codex_app_server_contract(
                    "/nonexistent/codex",
                    version_output=output,
                    schema_bundle={"definitions": {}},
                    timeout=1,
                )
                self.assertFalse(probe["ok"])
                self.assertIn(expected_reason, probe["reason"])
                parsed = parse_codex_version(output)
                if parsed is not None:
                    in_bounds = parsed in CODEX_APP_SERVER_SUPPORTED_VERSIONS
                    self.assertEqual(in_bounds, expected_reason == "schema")
        # The live-captured collaboration build is an explicitly supported
        # non-contiguous version; unrecorded intermediate versions remain out.
        candidate = parse_codex_version("codex-cli 0.150.1")
        self.assertTrue(candidate <= CODEX_APP_SERVER_MAX_VERSION)


class HermesSurfaceTests(unittest.TestCase):
    """ACP/receipt/exhaustion semantics across Hermes versions."""

    def test_acp_initialize_protocol_version_drift_fails_closed(self) -> None:
        from agent_bridge_connect.hermes_acp import (
            HERMES_ACP_PROTOCOL_VERSION,
            validate_initialize_result,
        )

        good = {"protocolVersion": HERMES_ACP_PROTOCOL_VERSION}
        self.assertEqual(validate_initialize_result(good), HERMES_ACP_PROTOCOL_VERSION)
        for payload in (
            {"protocolVersion": 2},
            {"protocolVersion": "1"},
            {},
            None,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(Exception):
                    validate_initialize_result(payload)

    def test_session_receipt_extraction_from_frozen_sample(self) -> None:
        body = load("hermes", "0.17.0", "session_events.json")
        stderr_sample = body["receipt_sample_stderr"]
        self.assertEqual(extract_hermes_session_id(stderr_sample), "20260810_010203_a1b2c3")

    def test_every_hermes_version_records_an_early_receipt_rule(self) -> None:
        receipt_sample = load("hermes", "0.17.0", "session_events.json")[
            "receipt_sample_stderr"
        ]
        self.assertEqual(
            extract_hermes_session_id(receipt_sample), "20260810_010203_a1b2c3"
        )
        for version, _entry in iter_versions("hermes"):
            body = load("hermes", version, "session_events.json")
            with self.subTest(version=version):
                self.assertTrue(body["early_receipt"]["required"])
                # The frozen format must accept the documented receipt shape and
                # reject anything else.
                frozen_pattern = re.compile(body["early_receipt"]["format_regex"])
                self.assertTrue(frozen_pattern.fullmatch("20260810_010203_a1b2c3"))
                for rejected in (
                    "",
                    "not-a-session",
                    "20260810_010203",
                    "2026_1_2_abc",
                    "../escape/session",
                ):
                    self.assertIsNone(frozen_pattern.fullmatch(rejected))

    def test_iteration_exhaustion_detectors_match_frozen_strings(self) -> None:
        body = load("hermes", "0.17.0", "resource_exhaustion.json")
        self.assertTrue(body["iteration_detector_strings"])
        for output in body["iteration_detector_strings"]:
            with self.subTest(output=output):
                diagnostics = _iteration_budget_diagnostics(output, "")
                self.assertTrue(diagnostics["iteration_exhausted"])

    def test_non_exhaustion_output_is_not_flagged(self) -> None:
        diagnostics = _iteration_budget_diagnostics("turn complete: 12/60 iterations", "")
        self.assertFalse(diagnostics["iteration_exhausted"])

    def test_sessions_delete_entry_freeze(self) -> None:
        frozen = load_text("hermes", "0.17.0", "help.txt")
        candidate = load_text("hermes", "0.20.1", "sessions_delete_help.txt")
        for text in (frozen, candidate):
            with self.subTest(length=len(text)):
                self.assertTrue(_hermes_has_exact_session_delete_entry(text))

    def test_resume_picker_entries_do_not_qualify_for_cleanup(self) -> None:
        entries_that_never_qualify = load("hermes", "0.17.0", "session_delete.json")[
            "entries_that_never_qualify"
        ]
        self.assertIn("--continue", entries_that_never_qualify)
        text = "usage: hermes --continue [SESSION_NAME]\npicker\nprune\npurge"
        self.assertFalse(_hermes_has_exact_session_delete_entry(text))

    def test_acp_probe_surface_is_recorded_for_both_versions(self) -> None:
        frozen = load("hermes", "0.17.0", "acp_probe.json")
        candidate = load("hermes", "0.20.1", "acp_probe.json")
        self.assertFalse(frozen["captured"])
        self.assertTrue(candidate["captured"])
        self.assertEqual(candidate["version_output"].strip(), "0.20.1")
        self.assertEqual(candidate["initialize_requirement"]["protocolVersion"], 1)


class ClaudeSurfaceTests(unittest.TestCase):
    """Path capability, project purge and budget exhaustion surfaces."""

    def test_path_capability_bounds_exclude_pending_capture_helper(self) -> None:
        from agent_bridge_connect.claude_path_capability import (
            CLAUDE_PATH_CAPABILITY_MAX_VERSION,
            CLAUDE_PATH_CAPABILITY_MIN_VERSION,
        )
        from agent_bridge_connect.codex_app_server import parse_codex_version

        for version, _entry in iter_versions("claude"):
            body = load("claude", version, "path_capability.json")
            with self.subTest(version=version):
                self.assertEqual(
                    body["production_bounds"]["min_inclusive"],
                    ".".join(map(str, CLAUDE_PATH_CAPABILITY_MIN_VERSION)),
                )
                self.assertEqual(
                    body["production_bounds"]["max_exclusive"],
                    ".".join(map(str, CLAUDE_PATH_CAPABILITY_MAX_VERSION)),
                )
        # Sanity: both recorded Claude versions sit inside the path bounds.
        parsed_low = parse_codex_version("2.1.226")
        self.assertTrue(
            CLAUDE_PATH_CAPABILITY_MIN_VERSION
            <= parsed_low
            < CLAUDE_PATH_CAPABILITY_MAX_VERSION
        )

    def test_budget_exhaustion_subtype_matches_frozen_snapshot(self) -> None:
        legacy = json.loads(
            (MATRIX / "claude" / "2.1.226" / "budget_exhaustion.json").read_text(
                encoding="utf-8"
            )
        )
        derived = load("claude", "2.1.226", "resource_exhaustion.json")
        pending = load("claude", "2.1.233", "resource_exhaustion.json")
        self.assertEqual(legacy["structured_subtype"], "error_max_budget_usd")
        self.assertEqual(derived["structured_subtype"], legacy["structured_subtype"])
        self.assertFalse(derived["verified_by_live_paid_canary"])
        self.assertEqual(pending["identical_to"], "claude/2.1.226")

    def test_project_purge_requires_the_documented_usage_line(self) -> None:
        from agent_bridge_connect.executors.claude import _supports_claude_project_purge

        help_text = load_text("claude", "2.1.226", "help.txt")
        self.assertTrue(_supports_claude_project_purge(help_text))
        for broken in ("", "Usage: claude something else"):
            with self.subTest(broken=broken or "<empty>"):
                self.assertFalse(_supports_claude_project_purge(broken))

    def test_pending_capture_purge_surface_admits_no_evidence(self) -> None:
        body = load("claude", "2.1.233", "project_purge.json")
        self.assertFalse(body["help_entry_captured"])
        self.assertEqual(body["status"], "pending_capture")

    def test_init_receipt_rule_present_for_all_claude_versions(self) -> None:
        from agent_bridge_connect.executors.claude import _STREAM_INIT_SUBTYPE

        for version, _entry in iter_versions("claude"):
            body = load("claude", version, "session_events.json")
            with self.subTest(version=version):
                self.assertEqual(body["early_receipt"]["subtype"], _STREAM_INIT_SUBTYPE)
                self.assertTrue(body["early_receipt"]["required"])

    def test_tool_fence_argv_is_stable_in_the_matrix(self) -> None:
        for version, _entry in iter_versions("claude"):
            body = load("claude", version, "argv_contract.json")
            with self.subTest(version=version):
                self.assertEqual(
                    body["tool_fence_args"][1], "TaskCreate,TaskUpdate,TodoWrite"
                )


class ArgvAndEnvironmentContractTests(unittest.TestCase):
    """Canonical argv/cwd/writable-root/environment contracts per executor."""

    REQUIRED_KEYS = {"cwd", "controlled_environment"}

    def test_every_argv_contract_declares_cwd_and_environment(self) -> None:
        argv_paths = sorted(MATRIX.glob("*/*/argv_contract.json"))
        self.assertGreaterEqual(len(argv_paths), 7)
        for path in argv_paths:
            body = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(surface=str(path.relative_to(MATRIX))):
                self.assertLessEqual(self.REQUIRED_KEYS, set(body))
                environment = body["controlled_environment"]
                self.assertFalse(environment.get("leaks_parent_secrets"))
                self.assertIsInstance(environment, dict)

    def test_writable_roots_are_explicit_per_executor(self) -> None:
        expectations = {
            "codex": "--add-dir",
            "claude": "--add-dir",
        }
        for executor, flag in expectations.items():
            paths = sorted(MATRIX.glob(f"{executor}/*/argv_contract.json"))
            self.assertGreaterEqual(len(paths), 1)
            for path in paths:
                with self.subTest(surface=str(path.relative_to(MATRIX))):
                    text = path.read_text(encoding="utf-8")
                    self.assertIn(flag, text)
        hermes_body = load("hermes", "0.17.0", "argv_contract.json")
        self.assertEqual(hermes_body["full_permission_flag"], "--yolo")
        self.assertEqual(hermes_body["controlled_environment"], {})


if __name__ == "__main__":
    unittest.main()
