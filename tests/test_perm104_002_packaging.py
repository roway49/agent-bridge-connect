"""PERM-104-002: packaging fixtures for the optional Claude SDK extra.

Verifies the ``claude`` optional extra stays pinned to the live-probed
``claude-agent-sdk==0.2.142`` in both packaging surfaces (pyproject.toml and
uv.lock ``requires-dist``), that the Alpha/full installer selects the extra by
default while ``AGENTBC_SKIP_CLAUDE_EXTRA=1`` keeps a core-light install, and
that the doctor capability projection stays redacted (no CLI path, no tokens).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
INSTALLER = REPO / "scripts" / "install_local_alpha.sh"

CLAUDE_SDK_PIN = "claude-agent-sdk==0.2.142"


class ClaudeExtraPackagingTests(unittest.TestCase):
    """The optional extra is pinned exactly to the probed SDK tuple."""

    def test_pyproject_declares_pinned_claude_extra(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")
        match = re.search(
            r"^claude\s*=\s*\[(.*?)\]",
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, "pyproject.toml is missing the claude extra")
        assert match is not None  # narrowing for type checkers
        self.assertIn(CLAUDE_SDK_PIN, match.group(1))

    def test_uv_lock_requires_dist_pins_the_extra(self) -> None:
        text = UV_LOCK.read_text(encoding="utf-8")
        self.assertIn(
            "requires-dist = [{ name = \"claude-agent-sdk\", "
            "marker = \"extra == 'claude'\", specifier = \"==0.2.142\" }]",
            text,
        )

    def test_alpha_installer_selects_the_extra_by_default(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn(CLAUDE_SDK_PIN, text)
        # The skip hatch must exist so core installs can stay light, and the
        # default (unset variable) must install the extra.
        self.assertIn("AGENTBC_SKIP_CLAUDE_EXTRA", text)

    def test_pinned_version_matches_transport_contract(self) -> None:
        from agent_bridge_connect.permission_transport import (
            CLAUDE_SDK_PINNED_VERSION,
        )

        self.assertEqual(CLAUDE_SDK_PINNED_VERSION, "0.2.142")

    def test_doctor_capability_projection_stays_redacted(self) -> None:
        from agent_bridge_connect.doctor import _claude_sdk_capability_projection

        projection = _claude_sdk_capability_projection({"executors": {"claude": {}}})
        self.assertFalse(projection["supported"])
        self.assertEqual(projection["status"], "permission_transport_unsupported")
        rendered = repr(projection)
        self.assertNotIn("/", rendered)
        self.assertNotIn("token", rendered.lower())
        self.assertNotIn("sk-", rendered.lower())


class FrozenLiveProbeEvidenceTests(unittest.TestCase):
    """The frozen live allow/deny probe evidence stays on the probed tuple.

    The evidence file is produced by the isolated live probe of the exact
    pinned tuple (claude-agent-sdk 0.2.142 + Claude 2.1.233, macOS arm64);
    packaging may only reference it, never widen it.
    """

    EVIDENCE = (
        REPO
        / "tests"
        / "fixtures"
        / "executor_runtime"
        / "matrix"
        / "claude"
        / "live_probe_sdk_2026-08-29"
        / "probe_evidence_ggqn_2026-08-30.json"
    )

    def test_ggqn_rerun_evidence_records_pass_on_pinned_tuple(self) -> None:
        import json

        self.assertTrue(self.EVIDENCE.is_file(), "GGQN live-probe evidence missing")
        data = json.loads(self.EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "pass")
        self.assertEqual(data["sdk_version"], "0.2.142")
        self.assertEqual(data["cli_version"], "2.1.233 (Claude Code)")
        checks = data["checks"]
        self.assertTrue(checks["allow_file_created"])
        self.assertFalse(checks["deny_file_created"])
        self.assertTrue(checks["single_session_all_phases"])
        self.assertTrue(
            any(
                event.get("event") == "can_use_tool" and event.get("tool_use_id")
                for event in data["events"]
            )
        )


class Ggqn002RuntimeProbeEvidenceTests(unittest.TestCase):
    """GGQN-002 runtime-closure live probe stays pinned on the frozen tuple.

    The evidence is produced by the isolated probe of the exact pinned tuple
    driving the PRODUCTION session driver (``_run_session_coroutine``) with
    the session-scoped hook feed: the approved can_use_tool identity becomes
    the verification anchor and a structured PostToolUse success bound to the
    pre-allocated official session verifies through the hook log.
    """

    EVIDENCE = (
        REPO
        / "tests"
        / "fixtures"
        / "executor_runtime"
        / "matrix"
        / "claude"
        / "live_probe_sdk_2026-08-29"
        / "probe_evidence_ggqn002_runtime_2026-08-30.json"
    )

    def test_ggqn002_runtime_probe_records_pass_on_pinned_tuple(self) -> None:
        import json

        self.assertTrue(self.EVIDENCE.is_file(), "GGQN-002 runtime probe evidence missing")
        data = json.loads(self.EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "pass")
        self.assertEqual(data["sdk_version"], "0.2.142")
        self.assertEqual(data["cli_version"], "2.1.233 (Claude Code)")
        checks = data["checks"]
        # Allow executed the exact original input; deny never executed.
        self.assertTrue(checks["allow_file_created"])
        self.assertEqual(checks["allow_file_content"], "probe-allow")
        self.assertFalse(checks["deny_file_created"])
        # The approved identity anchored the run and its structured
        # PostToolUse success verified, bound to the pre-allocated session.
        self.assertTrue(checks["approved_identity_anchored"])
        self.assertTrue(checks["structured_post_tool_use_success_for_anchor"])
        self.assertTrue(checks["anchor_post_session_bound"])
        self.assertTrue(checks["hook_log_session_bound"])
        self.assertTrue(checks["result_session_matches_probe_session"])
        # Exactly two native requests with stable non-empty identities.
        self.assertTrue(checks["two_can_use_tool_requests"])
        can_use_events = [
            event
            for event in data["events"]
            if event.get("event") == "can_use_tool" and event.get("tool_use_id")
        ]
        self.assertEqual(len(can_use_events), 2)


if __name__ == "__main__":
    unittest.main()
