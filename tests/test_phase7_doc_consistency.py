"""Phase 7 Task 3 (DOCFIX-001) help/document/Skill consistency gates.

These tests pin the shared command table, the corrected record semantics, the
absence of retired Git-specific commands, and the bilingual/platform alignment
of the user documentation and the canonical controller contract. They only add
constraints; they never relax an existing contract.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.cli import build_parser, main
from agent_bridge_connect.record_management import (
    RECORD_README,
    clean_terminal_records,
    ensure_record_root,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The canonical command table shared by the CLI, the bilingual User Guides, the
# Quick Starts, the READMEs, the development handbook, and the platform Skills.
CANONICAL_COMMANDS: dict[str, set[str] | dict[str, set[str]]] = {
    "setup": set(),
    "doctor": set(),
    "uninstall": set(),
    "init": set(),
    "claude": {"budget"},
    "hermes": {"max-turns"},
    "session": {"retention"},
    "record": {"clean"},
    "task": {
        "create",
        "list",
        "status",
        "dispatch",
        "respond",
        "report",
        "logs",
        "progress",
        "pause",
        "resume",
        "cancel",
        "close",
        "delete",
        "correct",
        "retry",
        "reassign",
        "handoff",
        "preflight",
        "recover",
        "callback",
    },
    "worker": {"run", "reap"},
    "runner": {
        "start",
        "stop",
        "serve",
        "status",
        "process-sample",
        "cancel",
        "show",
    },
}

RETENTION_SUBCOMMANDS = {"status", "enable", "disable"}

# Retired Git-specific public surface from the abandoned Phase 6 Task 1
# (SWJF-001). They must not return anywhere in the CLI or user-facing docs.
RETIRED_COMMAND_TOKENS = ("--git-write", "--commit-sha", "commit_required", "agentbc.git")

# User-facing files that must never present the retired commands as live.
USER_FACING_DOCS = (
    "README.md",
    "README_ZH.md",
    "docs/QUICK_START.md",
    "docs/QUICK_START_ZH.md",
    "docs/USER_GUIDE.md",
    "docs/USER_GUIDE_ZH.md",
    "src/agent_bridge_connect/skills/codex_skill.md",
    "src/agent_bridge_connect/skills/claude_skill.md",
    "src/agent_bridge_connect/skills/hermes_skill.md",
    "src/agent_bridge_connect/skills/codex_openai.yaml",
    "src/agent_bridge_connect/skills/references/controller-contract.md",
)

# Headers that exist only in the canonical contract; thin platform Skills must
# not duplicate the contract body under the same structure.
CANONICAL_CONTRACT_HEADERS = (
    "## Source Of Truth",
    "## Runner Gate",
    "## Target Executor",
    "## Permission Modes",
    "## Steps Contract",
    "## New Task Path Plan",
    "## Status And Exact Task Resolution",
    "## Continuations, New Roots, And Close",
    "## Configuration And Health",
    "## Dispatcher Traceability",
    "## Progress, Completion, Intervention, And Acceptance",
)


def _dotted_commands() -> set[str]:
    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> set[str]:
        found: set[str] = set()
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, subparser in action.choices.items():
                path = prefix + (name,)
                found.add(".".join(path))
                found |= walk(subparser, path)
        return found

    return walk(build_parser(), ())


def _canonical_dotted() -> set[str]:
    dotted: set[str] = set()
    for group, subs in CANONICAL_COMMANDS.items():
        if not subs:
            dotted.add(group)
            continue
        for sub in subs:
            dotted.add(f"{group}.{sub}")
    for sub in RETENTION_SUBCOMMANDS:
        dotted.add(f"session.retention.{sub}")
    return dotted


def _parser_help_text() -> str:
    chunks: list[str] = [build_parser().format_help()]
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                chunks.append(subparser.format_help())
    return "\n".join(chunks)


class SharedCommandTableTests(unittest.TestCase):
    def test_every_canonical_command_exists_in_the_cli_parser(self) -> None:
        parser_paths = _dotted_commands()
        for dotted in _canonical_dotted():
            with self.subTest(command=dotted):
                self.assertIn(dotted, parser_paths)

    def test_command_table_is_documented_bilingually(self) -> None:
        guide_en = (REPOSITORY_ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
        guide_zh = (REPOSITORY_ROOT / "docs" / "USER_GUIDE_ZH.md").read_text(encoding="utf-8")
        documented = {
            "agentbc claude budget",
            "agentbc hermes max-turns",
            "agentbc session retention status",
            "agentbc session retention enable",
            "agentbc session retention disable",
            "agentbc record clean",
            "agentbc task close",
            "agentbc doctor",
        }
        for command in documented:
            with self.subTest(command=command):
                self.assertIn(command, guide_en)
                self.assertIn(command, guide_zh)

    def test_quick_starts_and_readmes_cover_the_shared_contract_topics(self) -> None:
        files = {
            "README.md",
            "README_ZH.md",
            "docs/QUICK_START.md",
            "docs/QUICK_START_ZH.md",
        }
        topics = (
            "claude budget",
            "hermes max-turns",
            "session retention",
            "record clean",
            "task close",
            "doctor",
        )
        for name in files:
            text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            for topic in topics:
                with self.subTest(file=name, topic=topic):
                    self.assertIn(topic, text)


class CorrectedRecordSemanticsTests(unittest.TestCase):
    def test_record_clean_help_states_diagnostics_only_and_reports_never_deleted(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            main(["record", "--help"])
        help_text = out.getvalue()

        self.assertIn("Remove eligible terminal-task runtime diagnostics", help_text)
        self.assertIn("reports are never deleted", help_text)
        self.assertNotIn("terminal-task reports", help_text)
        self.assertNotIn("Remove terminal-task reports", help_text)

    def test_record_readme_queued_head_is_closable_terminal_and_stale_rejected(self) -> None:
        self.assertIn("current queued (pending) or active", RECORD_README)
        self.assertIn("terminal and stale iterations are rejected", RECORD_README)
        self.assertNotIn("Terminal, pending, and stale iterations are rejected", RECORD_README)
        self.assertIn("reports are never deleted by record cleanup", RECORD_README)
        self.assertIn("task.json", RECORD_README)
        self.assertIn("TASK_INDEX.md", RECORD_README)
        self.assertIn("task_index.jsonl", RECORD_README)

    def test_ensure_record_root_writes_the_corrected_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_root = ensure_record_root(Path(tmp) / "record")
            self.assertEqual(
                (record_root / "README.md").read_text(encoding="utf-8"),
                RECORD_README,
            )

    def test_record_clean_preserves_task_json_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_root = ensure_record_root(root / "record")
            task_dir = record_root / "ABCD" / "001"
            task_dir.mkdir(parents=True)
            (task_dir / "task.json").write_text(
                json.dumps({"id": "ABCD-001", "status": "completed", "title": "t"}),
                encoding="utf-8",
            )
            (task_dir / "events.jsonl").write_text('{"event": "x"}\n', encoding="utf-8")
            (task_dir / "run_lease.json").write_text('{"state": "closed"}\n', encoding="utf-8")
            (task_dir / "ABCD-001-run.log").write_text("tail\n", encoding="utf-8")
            report_dir = root / "tasks" / "report" / "2026-08-12" / "ABCD"
            report_dir.mkdir(parents=True)
            report = report_dir / "ABCD-001-report.md"
            report.write_text("# Report\n", encoding="utf-8")

            result = clean_terminal_records(record_root)

            self.assertEqual(result["ok"], True)
            self.assertEqual(result["tasks_cleaned"], ["ABCD-001"])
            self.assertTrue((task_dir / "task.json").is_file())
            self.assertFalse((task_dir / "events.jsonl").exists())
            self.assertFalse((task_dir / "run_lease.json").exists())
            self.assertFalse((task_dir / "ABCD-001-run.log").exists())
            self.assertTrue(report.is_file())
            self.assertEqual(report.read_text(encoding="utf-8"), "# Report\n")
            self.assertTrue((record_root / "README.md").is_file())
            self.assertIn("preserved", result)

    def test_record_clean_skips_non_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_root = ensure_record_root(Path(tmp) / "record")
            pending_dir = record_root / "PEND" / "001"
            pending_dir.mkdir(parents=True)
            (pending_dir / "task.json").write_text(
                json.dumps({"id": "PEND-001", "status": "pending", "title": "p"}),
                encoding="utf-8",
            )
            (pending_dir / "events.jsonl").write_text('{"event": "x"}\n', encoding="utf-8")

            result = clean_terminal_records(record_root)

            self.assertEqual(result["tasks_cleaned"], [])
            self.assertTrue((pending_dir / "events.jsonl").is_file())


class RetiredCommandAbsenceTests(unittest.TestCase):
    def test_retired_commands_absent_from_cli_parser(self) -> None:
        help_text = _parser_help_text()
        for token in RETIRED_COMMAND_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, help_text)

    def test_retired_commands_absent_from_user_facing_docs(self) -> None:
        for relative in USER_FACING_DOCS:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            for token in RETIRED_COMMAND_TOKENS:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, text)


class BilingualAndPlatformAlignmentTests(unittest.TestCase):
    def test_bilingual_guides_share_the_behavior_contract(self) -> None:
        guide_en = (REPOSITORY_ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
        guide_zh = (REPOSITORY_ROOT / "docs" / "USER_GUIDE_ZH.md").read_text(encoding="utf-8")
        en_phrases = (
            "reports are never deleted",
            "current queued (pending) or active chain head",
            "stale non-head iterations are rejected",
            "requested_permission",
            "permission_denied_by_user",
            "0` = healthy",
            "2` = unavailable",
            "never deletes the dispatcher conversation",
            "one-time `full` grant",
        )
        zh_phrases = (
            "永远不会删除报告",
            "排队中（pending）或活跃的 chain head",
            "requested_permission",
            "permission_denied_by_user",
            "0` = healthy",
            "2` = unavailable",
            "不会删除派发者会话",
            "一次性 `full` 授权",
        )
        for phrase in en_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide_en)
        for phrase in zh_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide_zh)

    def test_platform_skills_are_thin_and_reference_the_canonical_contract(self) -> None:
        skills_root = REPOSITORY_ROOT / "src" / "agent_bridge_connect" / "skills"
        contract = (skills_root / "references" / "controller-contract.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("This is the canonical controller contract", contract)
        for header in CANONICAL_CONTRACT_HEADERS:
            self.assertIn(header, contract)

        for skill in ("codex_skill.md", "claude_skill.md", "hermes_skill.md"):
            text = (skills_root / skill).read_text(encoding="utf-8")
            with self.subTest(skill=skill):
                self.assertIn("references/controller-contract.md", text)
                self.assertLess(len(text.splitlines()), 200)
                for header in CANONICAL_CONTRACT_HEADERS:
                    self.assertNotIn(header, text)
                self.assertNotIn("This is the canonical controller contract", text)
                self.assertNotIn("## Source Of Truth", text)

    def test_platform_skills_cover_the_shared_topics_without_duplicating_the_body(self) -> None:
        skills_root = REPOSITORY_ROOT / "src" / "agent_bridge_connect" / "skills"
        for skill in ("codex_skill.md", "claude_skill.md"):
            text = (skills_root / skill).read_text(encoding="utf-8")
            self.assertIn("never deletes the controller conversation", text)
            self.assertIn("claude budget", text)
            self.assertIn("hermes max-turns", text)
            self.assertIn("session retention", text)
            self.assertIn("record clean", text)
            self.assertIn("task close", text)
            self.assertIn("doctor", text)
            self.assertIn("0/1/2", text)
        hermes = (skills_root / "hermes_skill.md").read_text(encoding="utf-8")
        self.assertIn("绝不删除控制端会话", hermes)
        self.assertIn("claude budget", hermes)
        self.assertIn("hermes max-turns", hermes)
        self.assertIn("session retention", hermes)
        self.assertIn("record clean", hermes)
        self.assertIn("task close", hermes)
        self.assertIn("0/1/2", hermes)

    def test_canonical_contract_keeps_single_source_of_truth_role(self) -> None:
        contract = (
            REPOSITORY_ROOT
            / "src"
            / "agent_bridge_connect"
            / "skills"
            / "references"
            / "controller-contract.md"
        ).read_text(encoding="utf-8")
        self.assertIn("canonical controller contract", contract)
        self.assertIn("where prose is duplicated elsewhere, this file is authoritative", contract)
        self.assertIn("agentbc doctor", contract)
        self.assertIn("0` = healthy, `1` = warning,", contract)
        self.assertIn("consumed when that run is authorized", contract)
        self.assertIn("unused grant is revoked", contract)
        self.assertNotIn("next same-session continuation only, then revoked", contract)
        self.assertIn("AgentBC never deletes the", contract)
        self.assertIn("dispatcher conversation", contract)


if __name__ == "__main__":
    unittest.main()
