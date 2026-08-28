"""PERM-104-002: executor permission control-path capability matrix tests.

Claude selects its control path from the versioned fixture matrix (MCP
permission-prompt tool vs stdio can_use_tool/control_response); unknown
version/transport combinations fail closed with
``permission_transport_unsupported``.  The fixtures and the production matrix
must agree, and the inner-sandbox contract stays frozen.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_bridge_connect.permission_runtime import (
    PERMISSION_RUNTIME_DOMAINS,
    PERMISSION_TRANSPORT_UNSUPPORTED,
)
from agent_bridge_connect.permission_transport import (
    CLAUDE_INIT_RECEIPT_KIND,
    CLAUDE_STDIO_CONTROL_RESPONSE,
    CONTROL_PATH_MCP_PERMISSION_TOOL,
    CONTROL_PATH_STDIO_CAN_USE_TOOL,
    KNOWN_CLAUDE_VERSIONS,
    assert_git_metadata_not_in_add_dir,
    assert_inner_sandbox_within_outer,
    claude_control_path_capability,
    claude_inner_sandbox_contract,
    parse_claude_version,
    select_claude_control_path,
)
from agent_bridge_connect.protocol import ABCError

MATRIX = Path("tests/fixtures/executor_runtime/matrix")
CLAUDE_VERSIONS = ("2.1.226", "2.1.233")


class ClaudeControlPathCapabilityTests(unittest.TestCase):
    def test_version_probe_line_is_parsed_to_canonical_triple(self) -> None:
        self.assertEqual(parse_claude_version("2.1.226 (Claude Code)"), "2.1.226")
        self.assertEqual(parse_claude_version("2.1.233"), "2.1.233")
        self.assertIsNone(parse_claude_version(""))
        self.assertIsNone(parse_claude_version("not-a-version"))
        self.assertIsNone(parse_claude_version(None))

    def test_known_versions_expose_both_control_paths(self) -> None:
        for version in CLAUDE_VERSIONS:
            with self.subTest(version=version):
                capability = claude_control_path_capability(version)
                self.assertEqual(capability["executor"], "claude")
                self.assertEqual(capability["version"], version)
                self.assertTrue(capability["mcp_permission_tool"])
                self.assertTrue(capability["stdio_can_use_tool"])
                self.assertEqual(
                    capability["control_response"], CLAUDE_STDIO_CONTROL_RESPONSE
                )
                self.assertEqual(capability["init_receipt"], CLAUDE_INIT_RECEIPT_KIND)
                self.assertTrue(capability["same_process_approve_deny"])
                self.assertTrue(capability["transport_death_invalidation"])

    def test_unknown_version_fails_closed(self) -> None:
        for version in ("9.9.9", "2.1.234", "unknown", "", None):
            with self.subTest(version=version):
                with self.assertRaises(ABCError) as raised:
                    claude_control_path_capability(version)
                self.assertEqual(
                    raised.exception.code, PERMISSION_TRANSPORT_UNSUPPORTED
                )

    def test_worker_selects_mcp_path_when_matrix_and_probe_agree(self) -> None:
        for version in CLAUDE_VERSIONS:
            with self.subTest(version=version):
                self.assertEqual(
                    select_claude_control_path(version, True),
                    CONTROL_PATH_MCP_PERMISSION_TOOL,
                )
                self.assertEqual(
                    select_claude_control_path(version, None),
                    CONTROL_PATH_MCP_PERMISSION_TOOL,
                )

    def test_worker_selects_stdio_path_when_probe_disagrees(self) -> None:
        self.assertEqual(
            select_claude_control_path("2.1.226", False),
            CONTROL_PATH_STDIO_CAN_USE_TOOL,
        )

    def test_unknown_combination_never_selects_a_path(self) -> None:
        with self.assertRaises(ABCError) as raised:
            select_claude_control_path("9.9.9", True)
        self.assertEqual(raised.exception.code, PERMISSION_TRANSPORT_UNSUPPORTED)
        with self.assertRaises(ABCError) as raised:
            select_claude_control_path("", False)
        self.assertEqual(raised.exception.code, PERMISSION_TRANSPORT_UNSUPPORTED)

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
    def test_fixture_surfaces_declare_the_same_contract(self) -> None:
        for version in CLAUDE_VERSIONS:
            with self.subTest(version=version):
                fixture_path = MATRIX / "claude" / version / "permission_control.json"
                self.assertTrue(fixture_path.exists(), str(fixture_path))
                fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                self.assertTrue(
                    fixture["mcp_permission_tool"]["supported"]
                )
                self.assertTrue(
                    fixture["stdio_can_use_tool"]["supported"]
                )
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

    def test_fixtures_cover_the_whole_production_matrix(self) -> None:
        for version in KNOWN_CLAUDE_VERSIONS:
            with self.subTest(version=version):
                self.assertIn(version, CLAUDE_VERSIONS)
                self.assertTrue(
                    (MATRIX / "claude" / version / "permission_control.json").exists()
                )

    def test_fixture_sources_are_sanitized(self) -> None:
        for version in CLAUDE_VERSIONS:
            with self.subTest(version=version):
                text = (
                    MATRIX / "claude" / version / "permission_control.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("sk-", text)
                self.assertNotIn("token", text.lower())
                self.assertNotIn("api_key", text.lower())

    def test_manifest_lists_permission_control_surfaces(self) -> None:
        manifest = json.loads(
            (MATRIX / "manifest.json").read_text(encoding="utf-8")
        )
        for version in CLAUDE_VERSIONS:
            with self.subTest(version=version):
                surfaces = manifest["executors"]["claude"]["versions"][version][
                    "surfaces"
                ]
                self.assertIn("permission_control.json", surfaces)
                metadata = surfaces["permission_control.json"]
                self.assertGreater(metadata["bytes"], 0)
                self.assertEqual(len(metadata["sha256"]), 64)


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


if __name__ == "__main__":
    unittest.main()
