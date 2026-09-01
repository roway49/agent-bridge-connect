"""PERM-104-002 executor-native protocol capability tests.

CLI and SDK versions are diagnostic only.  Compatible releases and forks are
admitted by the mechanical SDK protocol shape; missing protocol members fail
closed without version allowlists or text matching.
"""

from __future__ import annotations

import json
import platform as platform_module
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.permission_runtime import (
    PERMISSION_RUNTIME_DOMAINS,
    PERMISSION_TRANSPORT_UNSUPPORTED,
)
from agent_bridge_connect.permission_transport import (
    CLAUDE_INIT_RECEIPT_KIND,
    CLAUDE_PERMISSION_PROMPT_TOOL_FLAG,
    CLAUDE_SDK_PINNED_VERSION,
    CLAUDE_STDIO_CONTROL_RESPONSE,
    CONTROL_PATH_MCP_PERMISSION_TOOL,
    CONTROL_PATH_SDK_TRANSPORT,
    CONTROL_PATH_STDIO_CAN_USE_TOOL,
    KNOWN_CLAUDE_VERSIONS,
    SDK_CLAUDE_DEPENDENCY_MISSING,
    assert_claude_sdk_environment,
    assert_git_metadata_not_in_add_dir,
    assert_inner_sandbox_within_outer,
    claude_control_path_capability,
    claude_inner_sandbox_contract,
    claude_sdk_protocol_capability,
    parse_claude_version,
    probe_claude_permission_prompt_tool,
    select_claude_control_path,
)
from agent_bridge_connect.protocol import ABCError

MATRIX = Path("tests/fixtures/executor_runtime/matrix")
WITHDRAWN_VERSIONS = ("2.1.226", "2.1.233")
LIVE_PROBE_DIR = MATRIX / "claude" / "live_probe_2026-08-28"


class ClaudeControlPathCapabilityTests(unittest.TestCase):
    def test_version_probe_line_is_parsed_to_canonical_triple(self) -> None:
        self.assertEqual(parse_claude_version("2.1.226 (Claude Code)"), "2.1.226")
        self.assertEqual(parse_claude_version("2.1.247"), "2.1.247")
        self.assertIsNone(parse_claude_version(""))
        self.assertIsNone(parse_claude_version("not-a-version"))
        self.assertIsNone(parse_claude_version(None))

    def test_no_version_allowlist_controls_admission(self) -> None:
        self.assertEqual(KNOWN_CLAUDE_VERSIONS, frozenset())
        for version in ("2.1.226", "2.1.247", "9.9.9", "", None):
            with self.subTest(version=version):
                capability = claude_control_path_capability(version)
                self.assertTrue(capability["sdk_control_transport"])
                self.assertTrue(capability["version_is_diagnostic"])
                self.assertFalse(capability["mcp_permission_tool"])

    def test_worker_selects_sdk_transport_independent_of_version(self) -> None:
        for version in ("2.1.226", "2.1.233", "2.1.247", "9.9.9", "", None):
            for probe in (True, False, None):
                with self.subTest(version=version, probe=probe):
                    self.assertEqual(
                        select_claude_control_path(version, probe),
                        CONTROL_PATH_SDK_TRANSPORT,
                    )

    def test_installed_sdk_protocol_shape_is_supported(self) -> None:
        capability = claude_sdk_protocol_capability()
        self.assertTrue(capability["available"])
        self.assertEqual(capability["protocol"], "sdk.can_use_tool")

    def test_live_probe_gate_rejects_help_without_the_flag(self) -> None:
        # The real production-host probe: the installed 2.1.247 help does not
        # list --permission-prompt-tool, so the MCP path can never be chosen.
        self.assertFalse(
            probe_claude_permission_prompt_tool("Usage: claude [options]\n")
        )
        self.assertTrue(
            probe_claude_permission_prompt_tool(
                f"  {CLAUDE_PERMISSION_PROMPT_TOOL_FLAG} <tool>\n"
            )
        )
        self.assertFalse(probe_claude_permission_prompt_tool(None))
        captured = (LIVE_PROBE_DIR / "help.txt").read_text(encoding="utf-8")
        self.assertIn("Usage: claude", captured)
        self.assertNotIn(CLAUDE_PERMISSION_PROMPT_TOOL_FLAG, captured)

    def test_live_probed_fixture_records_transport_unsupported(self) -> None:
        fixture = json.loads(
            (LIVE_PROBE_DIR / "permission_control.json").read_text(encoding="utf-8")
        )
        self.assertTrue(fixture["captured_live"])
        self.assertFalse(fixture["probe"]["help_contains_permission_prompt_tool"])
        self.assertEqual(fixture["decision"], PERMISSION_TRANSPORT_UNSUPPORTED)
        self.assertFalse(fixture["mcp_permission_tool"]["supported"])
        self.assertFalse(fixture["stdio_can_use_tool"]["supported"])

    def test_inner_sandbox_contract_is_frozen(self) -> None:
        contract = claude_inner_sandbox_contract()
        self.assertEqual(
            contract["sandbox_keys"],
            ("sandbox.enabled", "sandbox.failIfUnavailable"),
        )
        self.assertTrue(contract["edit_deny"])
        self.assertFalse(contract["git_metadata_in_add_dir"])
        self.assertTrue(contract["allow_write_within_outer_roots"])

    def test_git_metadata_never_in_add_dir(self) -> None:
        metadata = ["/repo/.git", "/repo/.git/worktrees/agent"]
        assert_git_metadata_not_in_add_dir(["/tmp/artifact"], metadata)
        with self.assertRaises(ABCError):
            assert_git_metadata_not_in_add_dir(
                ["/repo/.git/worktrees/agent"], metadata
            )

    def test_inner_allow_write_within_outer_roots(self) -> None:
        assert_inner_sandbox_within_outer(
            ["/tmp/project/out"], ["/tmp/project"]
        )
        with self.assertRaises(ABCError):
            assert_inner_sandbox_within_outer(
                ["/tmp/outside"], ["/tmp/project"]
            )


class ClaudeControlFixtureTests(unittest.TestCase):
    def test_withdrawn_fixtures_no_longer_declare_support(self) -> None:
        for version in WITHDRAWN_VERSIONS:
            with self.subTest(version=version):
                fixture_path = MATRIX / "claude" / version / "permission_control.json"
                self.assertTrue(fixture_path.exists(), str(fixture_path))
                fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                self.assertFalse(fixture["mcp_permission_tool"]["supported"])
                self.assertFalse(fixture["stdio_can_use_tool"]["supported"])
                self.assertTrue(fixture["e52m003_review"]["withdrawn"])
                self.assertFalse(fixture["e52m003_review"]["production_matrix_entry"])
                self.assertEqual(
                    fixture["stdio_can_use_tool"]["control_response"],
                    CLAUDE_STDIO_CONTROL_RESPONSE,
                )
                self.assertEqual(
                    fixture["stdio_can_use_tool"]["init_receipt"],
                    CLAUDE_INIT_RECEIPT_KIND,
                )
                self.assertFalse(fixture["inner_sandbox"]["git_metadata_in_add_dir"])
                self.assertTrue(fixture["inner_sandbox"]["edit_deny"])

    def test_fixture_sources_are_sanitized(self) -> None:
        for version in WITHDRAWN_VERSIONS:
            with self.subTest(version=version):
                text = (
                    MATRIX / "claude" / version / "permission_control.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("sk-", text)
                self.assertNotIn("token", text.lower())
                self.assertNotIn("api_key", text.lower())
        probe_text = (LIVE_PROBE_DIR / "permission_control.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sk-", probe_text)
        self.assertNotIn("token", probe_text.lower())
        self.assertNotIn("api_key", probe_text.lower())
        help_text = (LIVE_PROBE_DIR / "help.txt").read_text(encoding="utf-8")
        self.assertNotIn("sk-", help_text)
        self.assertNotIn("bearer ", help_text.lower())

    def test_manifest_lists_permission_control_surfaces(self) -> None:
        manifest = json.loads(
            (MATRIX / "manifest.json").read_text(encoding="utf-8")
        )
        for version in WITHDRAWN_VERSIONS:
            with self.subTest(version=version):
                surfaces = manifest["executors"]["claude"]["versions"][version][
                    "surfaces"
                ]
                self.assertIn("permission_control.json", surfaces)
                metadata = surfaces["permission_control.json"]
                self.assertGreater(metadata["bytes"], 0)
                self.assertEqual(len(metadata["sha256"]), 64)
        # The live probe is executor-level evidence, not a matrix version:
        # it proves the installed binary has no AgentBC control path and
        # adds no capability to the matrix.
        live = manifest["executors"]["claude"]["live_probe_e52m003"]
        self.assertTrue(live["live_capture"])
        self.assertFalse(live["help_contains_permission_prompt_tool"])
        self.assertEqual(live["decision"], PERMISSION_TRANSPORT_UNSUPPORTED)
        self.assertNotIn("2.1.247", manifest["executors"]["claude"]["versions"])


class TransportDomainConsistencyTests(unittest.TestCase):
    def test_stable_code_lives_in_both_modules(self) -> None:
        from agent_bridge_connect.permission_runtime import (
            PERMISSION_TRANSPORT_UNSUPPORTED as runtime_code,
        )
        from agent_bridge_connect.permission_transport import (
            PERMISSION_TRANSPORT_UNSUPPORTED as transport_code,
        )

        self.assertEqual(runtime_code, transport_code)
        self.assertEqual(runtime_code, "permission_transport_unsupported")

    def test_domains_stay_fixed_for_status_projection(self) -> None:
        # The projection order is the stable hierarchy order.
        self.assertEqual(
            list(PERMISSION_RUNTIME_DOMAINS),
            [
                "executor_policy",
                "agentbc_policy",
                "runner_pathplan",
                "host_containment",
                "linked_worktree_metadata",
            ],
        )

    def test_control_path_identifiers_unchanged_for_future_proofs(self) -> None:
        # The identifiers stay frozen so a future live proof can re-declare
        # a control path without renaming the contract.
        self.assertEqual(CONTROL_PATH_MCP_PERMISSION_TOOL, "mcp_permission_tool")
        self.assertEqual(CONTROL_PATH_STDIO_CAN_USE_TOOL, "stdio_can_use_tool")
        self.assertEqual(CONTROL_PATH_SDK_TRANSPORT, "sdk_control_transport")
        self.assertEqual(CLAUDE_STDIO_CONTROL_RESPONSE, "control_response")
        self.assertEqual(CLAUDE_INIT_RECEIPT_KIND, "system/init")


class ClaudeSdkEnvironmentGateTests(unittest.TestCase):
    """The SDK transport gates protocol shape, not version strings."""

    def test_environment_gate_passes_on_this_host(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception:
            self.skipTest("claude-agent-sdk is not installed in this env")
        if sys.platform != "darwin" or platform_module.machine() != "arm64":
            self.skipTest("probed tuple is macOS arm64 only")
        facts = assert_claude_sdk_environment(
            "/Users/wangroway/.local/share/claude/versions/2.1.247"
        )
        self.assertTrue(facts["sdk_version"])
        self.assertEqual(facts["protocol"], "sdk.can_use_tool")
        self.assertEqual(facts["platform"], "macOS arm64")

    def test_environment_gate_requires_absolute_configured_cli(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception:
            self.skipTest("claude-agent-sdk is not installed in this env")
        if sys.platform != "darwin" or platform_module.machine() != "arm64":
            self.skipTest("probed tuple is macOS arm64 only")
        for bad in ("", "claude", "relative/claude", "/nonexistent/claude"):
            with self.subTest(cli_path=bad):
                with self.assertRaises(ABCError) as raised:
                    assert_claude_sdk_environment(bad)
                self.assertEqual(
                    raised.exception.code, "claude_sdk_cli_path_unverified"
                )

    def test_missing_sdk_fails_closed_with_stable_code(self) -> None:
        from agent_bridge_connect import permission_transport

        fake = types.ModuleType("claude_agent_sdk")
        fake.__version__ = CLAUDE_SDK_PINNED_VERSION  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with self.assertRaises(ABCError) as raised:
                permission_transport.assert_claude_sdk_environment(
                    "/usr/bin/claude"
                )
            self.assertEqual(
                raised.exception.code, SDK_CLAUDE_DEPENDENCY_MISSING
            )
        del fake

    def test_sdk_version_is_not_an_admission_key(self) -> None:
        from agent_bridge_connect import permission_transport

        import claude_agent_sdk

        with mock.patch.object(claude_agent_sdk, "__version__", "999.0-fork"):
            capability = permission_transport.claude_sdk_protocol_capability()
        self.assertTrue(capability["available"])
        self.assertEqual(capability["sdk_version"], "999.0-fork")

    def test_missing_protocol_member_fails_by_shape_not_version(self) -> None:
        from agent_bridge_connect import permission_transport
        import claude_agent_sdk

        class IncompatibleOptions:
            def __init__(self, cli_path: str | None = None) -> None:
                self.cli_path = cli_path

        with mock.patch.object(
            claude_agent_sdk, "ClaudeAgentOptions", IncompatibleOptions
        ):
            with self.assertRaises(ABCError) as raised:
                permission_transport.claude_sdk_protocol_capability()
        self.assertEqual(
            raised.exception.code, "permission_protocol_shape_unsupported"
        )
        self.assertIn(
            "ClaudeAgentOptions.can_use_tool",
            raised.exception.details["missing_members"],
        )


if __name__ == "__main__":
    unittest.main()
