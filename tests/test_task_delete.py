"""Focused DEL-001 tests for terminal task-chain deletion."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.service import TaskService


class TaskDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp()).resolve()
        self.board = self.workspace / "record"
        self.service = TaskService(self.board, config={"workspace_root": str(self.workspace)})

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _create(self, *, customer: Path | None = None):
        task = self.service.create_task(
            "Delete test",
            "shell",
            [{"id": 1, "description": "complete terminal work"}],
            customer_dir=customer is not None,
            customer_path=customer if customer is not None else "default path",
        )
        Path(task.workspace["report_file"]).write_text("terminal report\n", encoding="utf-8")
        if customer is None:
            Path(task.workspace["artifact_root"], "result.txt").write_text("managed\n", encoding="utf-8")
        return task

    def _terminal(self, task, status: str = "completed") -> None:
        data = self.service.store.read_task(task.id)
        data["status"] = status
        self.service.store.write_task(task.id, data)

    def _snapshot(self) -> dict[str, bytes | None]:
        snapshot: dict[str, bytes | None] = {}
        for path in sorted(self.workspace.rglob("*")):
            relative = str(path.relative_to(self.workspace))
            snapshot[relative] = path.read_bytes() if path.is_file() else None
        return snapshot

    def test_dry_run_lists_owned_and_preserved_objects_without_writes(self) -> None:
        customer = self.workspace / "customer-project"
        customer.mkdir()
        keep = customer / "keep.txt"
        keep.write_text("customer-owned\n", encoding="utf-8")
        task = self._create(customer=customer)
        self._terminal(task)
        before = self._snapshot()

        result = self.service.delete_task_chain(
            task.workspace["task_code"],
            dry_run=True,
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertIn("record", {item["kind"] for item in result["delete_objects"]})
        self.assertIn("report", {item["kind"] for item in result["delete_objects"]})
        self.assertEqual(result["preserve_objects"][0]["path"], str(customer))
        self.assertEqual(before, self._snapshot())
        self.assertTrue(keep.is_file())

    def test_confirmed_managed_delete_removes_chain_refreshes_index_and_releases_code(self) -> None:
        from agent_bridge_connect.cli import main

        task = self._create()
        self._terminal(task)
        code = task.workspace["task_code"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["task", "delete", code.lower(), "--root", str(self.board), "--confirm"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "deleted")
        self.assertFalse((self.board / code).exists())
        self.assertFalse(Path(task.workspace["report_root"]).exists())
        self.assertFalse(Path(task.workspace["artifact_root"]).exists())
        self.assertNotIn(task.id, (self.board / "task_index.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(list(self.workspace.rglob(".agentbc-deletions")), [])
        receipt = self.service.store.latest_deletion_receipt(code)
        self.assertEqual(receipt["task_ids"], [task.id])

        with mock.patch("agent_bridge_connect.task_store.allocate_task_code", return_value=code):
            replacement = self._create()
        self.assertEqual(replacement.id, task.id)
        with self.assertRaises(ABCError) as raised:
            self.service.plan_task_delete(code)
        self.assertEqual(raised.exception.code, "task_delete_ineligible")

    def test_customer_project_is_preserved_even_under_managed_looking_path(self) -> None:
        customer = self.workspace / "tasks" / "artifacts" / "customer-lookalike" / "ABCD"
        customer.mkdir(parents=True)
        keep = customer / "keep.txt"
        keep.write_text("never delete\n", encoding="utf-8")
        task = self._create(customer=customer)
        self._terminal(task, "failed")

        result = self.service.delete_task_chain(task.workspace["task_code"], confirmed=True)

        self.assertEqual(result["status"], "deleted")
        self.assertTrue(keep.is_file())
        self.assertFalse(Path(task.workspace["report_root"]).exists())

    def test_multiple_terminal_iterations_are_deleted_as_one_chain(self) -> None:
        source = self._create()
        self._terminal(source, "completed")
        followup = self.service.handoff_task(source.id, "codex", "second iteration")
        Path(followup.workspace["report_file"]).write_text("second report\n", encoding="utf-8")
        self._terminal(followup, "rejected")

        result = self.service.delete_task_chain(source.workspace["task_code"], confirmed=True)

        self.assertEqual(result["task_ids"], [source.id, followup.id])
        self.assertFalse((self.board / source.workspace["task_code"]).exists())
        self.assertFalse(Path(source.workspace["report_root"]).exists())

    def test_active_and_recovery_states_reject_entire_chain(self) -> None:
        for status in ("pending", "running", "input_required", "needs_recovery"):
            with self.subTest(status=status):
                task = self._create()
                self._terminal(task, status)
                with self.assertRaises(ABCError) as raised:
                    self.service.delete_task_chain(task.workspace["task_code"], confirmed=True)
                self.assertEqual(raised.exception.code, "task_delete_ineligible")
                self.assertTrue((self.board / task.workspace["task_code"]).exists())

    def test_iteration_id_is_rejected(self) -> None:
        task = self._create()
        self._terminal(task)

        with self.assertRaises(ABCError) as raised:
            self.service.delete_task_chain(task.id, dry_run=True)

        self.assertEqual(raised.exception.code, "task_delete_requires_chain_code")
        self.assertTrue((self.board / task.workspace["task_code"]).exists())

    def test_exactly_one_explicit_mode_is_required(self) -> None:
        from agent_bridge_connect.cli import build_parser

        task = self._create()
        self._terminal(task)
        code = task.workspace["task_code"]
        for kwargs in ({}, {"dry_run": True, "confirmed": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ABCError) as raised:
                self.service.delete_task_chain(code, **kwargs)
            self.assertEqual(raised.exception.code, "task_delete_confirmation_required")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "delete", code])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["task", "delete", code, "--dry-run", "--confirm"])

    def test_partial_staging_failure_rolls_back_without_releasing_code(self) -> None:
        task = self._create()
        self._terminal(task)
        code = task.workspace["task_code"]
        original_move = self.service.store._stage_delete_target
        calls = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated partial failure")
            original_move(source, destination)

        with mock.patch.object(self.service.store, "_stage_delete_target", side_effect=fail_second):
            with self.assertRaises(ABCError) as raised:
                self.service.delete_task_chain(code, confirmed=True)

        self.assertEqual(raised.exception.code, "task_delete_failed")
        self.assertTrue(raised.exception.details["rollback_complete"])
        self.assertTrue((self.board / code).exists())
        self.assertTrue(Path(task.workspace["report_root"]).exists())
        self.assertTrue(Path(task.workspace["artifact_root"]).exists())
        self.assertIsNone(self.service.store.latest_deletion_receipt(code))
        self.assertNotIn(code, self.service.store.reserved_deletion_codes())

    def test_interrupted_staging_resumes_deterministically(self) -> None:
        task = self._create()
        self._terminal(task)
        code = task.workspace["task_code"]
        plan = self.service.plan_task_delete(code)
        reservation = self.service.store.reserve_chain_deletion(plan)
        self.service.store.stage_chain_deletion(reservation["deletion_id"])
        self.assertIn(code, self.service.store.reserved_deletion_codes())

        result = self.service.delete_task_chain(code, confirmed=True)

        self.assertEqual(result["status"], "deleted")
        self.assertTrue(result["released_task_code"])
        self.assertNotIn(code, self.service.store.reserved_deletion_codes())

    def test_containment_tampering_fails_closed(self) -> None:
        for field in ("artifact_root", "report_root"):
            with self.subTest(field=field):
                task = self._create()
                self._terminal(task)
                data = self.service.store.read_task(task.id)
                workspace = data["workspace"]
                if field == "artifact_root":
                    attack = self.workspace / "tasks" / "artifacts" / "wrong" / "WRNG"
                    attack.mkdir(parents=True)
                    workspace["artifact_root"] = str(attack)
                    workspace["artifacts_dir"] = str(attack)
                else:
                    attack = self.workspace / "tasks" / "report" / "wrong" / "WRNG"
                    attack.mkdir(parents=True)
                    workspace["report_root"] = str(attack)
                marker = attack / "keep.txt"
                marker.write_text("outside canonical chain\n", encoding="utf-8")
                self.service.store.write_task(task.id, data)

                with self.assertRaises(ABCError):
                    self.service.delete_task_chain(task.workspace["task_code"], confirmed=True)
                self.assertTrue(marker.is_file())
                self.assertTrue((self.board / task.workspace["task_code"]).exists())

    def test_repeat_invocation_returns_stable_already_deleted_result(self) -> None:
        task = self._create()
        self._terminal(task, "cancelled")
        code = task.workspace["task_code"]
        first = self.service.delete_task_chain(code, confirmed=True)

        second = self.service.delete_task_chain(code, confirmed=True)
        dry_run = self.service.delete_task_chain(code, dry_run=True)

        self.assertEqual(first["status"], "deleted")
        self.assertEqual(second["status"], "already_deleted")
        self.assertEqual(dry_run["status"], "already_deleted")
        self.assertEqual(second["receipt"]["deletion_id"], first["deletion_id"])

    def test_help_describes_chain_code_and_both_modes(self) -> None:
        from agent_bridge_connect.cli import build_parser

        parser = build_parser()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["task", "delete", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("fully terminal task chain", help_text)
        self.assertIn("--dry-run", help_text)
        self.assertIn("--confirm", help_text)
        self.assertIn("not an iteration id", help_text)


if __name__ == "__main__":
    unittest.main()
