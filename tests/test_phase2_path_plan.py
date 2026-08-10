from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.path_model import (
    build_path_plan,
    canonical_executor_project_root,
    validate_path_plan_workspace,
)
from agent_bridge_connect.protocol import ABCError


class Phase2PathPlanTests(unittest.TestCase):
    """Phase 2 Claude Project Path Plan: planning and validation only.

    The canonical executor project root is strictly
    ``<agentbc_root>/tasks/artifacts/<task_date>/<task_code>/<task_id>/claude``.
    Building a plan never creates directories; validation only ever runs on
    path metadata.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.config = {"workspace_root": str(self.workspace)}
        self.task_date = "2026-08-10"

    def _plan(
        self,
        *,
        customer_dir: bool = False,
        customer_path: str | Path | None = None,
        task_code: str = "KRMB",
        iteration: int | str = 1,
    ):
        return build_path_plan(
            customer_dir=customer_dir,
            customer_path=customer_path,
            task_code=task_code,
            iteration=iteration,
            config=self.config,
            task_date=self.task_date,
        )

    def _managed_executor_root(self, task_code: str, iteration: int) -> Path:
        task_id = f"{task_code}-{int(iteration):03d}"
        return (
            self.workspace.resolve()
            / "tasks"
            / "artifacts"
            / self.task_date
            / task_code
            / task_id
            / "claude"
        )

    # --- default path mode --------------------------------------------------

    def test_default_mode_canonical_executor_project_root(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        self.assertEqual(
            Path(workspace["executor_project_root"]),
            self._managed_executor_root("KRMB", 1),
        )
        # Planned only: the executor project root is never created.
        self.assertFalse(Path(workspace["executor_project_root"]).exists())
        validate_path_plan_workspace(workspace)  # fresh plan round-trips

    def test_canonical_path_structure(self) -> None:
        root = Path(self._plan(task_code="ABCD", iteration=3).executor_project_root)
        self.assertEqual(root.name, "claude")
        self.assertEqual(root.parent.name, "ABCD-003")
        self.assertEqual(root.parent.parent.name, "ABCD")
        self.assertEqual(root.parent.parent.parent.name, self.task_date)
        self.assertEqual(root.parent.parent.parent.parent.name, "artifacts")
        self.assertEqual(
            root,
            canonical_executor_project_root(
                self.workspace.resolve(), self.task_date, "ABCD", "ABCD-003"
            ),
        )

    # --- customer path mode -------------------------------------------------

    def test_customer_mode_preserves_customer_path_but_plans_managed_root(self) -> None:
        customer = self.root / "customer"
        customer.mkdir()
        plan = self._plan(customer_dir=True, customer_path=customer, task_code="KRMB", iteration=1)
        workspace = plan.to_workspace()
        # The user path is preserved for provenance; the executor works from a
        # managed artifact path, never from the user project.
        self.assertEqual(Path(workspace["customer_path"]), customer.resolve())
        self.assertEqual(Path(workspace["project_root"]), customer.resolve())
        executor_root = Path(workspace["executor_project_root"])
        self.assertTrue(executor_root.is_relative_to(self.workspace.resolve() / "tasks" / "artifacts"))
        self.assertFalse(executor_root.is_relative_to(customer.resolve()))
        validate_path_plan_workspace(workspace)

    # --- iteration / branch isolation ---------------------------------------

    def test_iteration_isolation_uses_full_task_id(self) -> None:
        first = self._plan(task_code="KRMB", iteration=1)
        second = self._plan(task_code="KRMB", iteration=2)
        self.assertNotEqual(first.executor_project_root, second.executor_project_root)
        self.assertIn("KRMB-001", first.executor_project_root.parts)
        self.assertIn("KRMB-002", second.executor_project_root.parts)

    def test_iteration_roots_share_chain_dir_but_isolate_task_id(self) -> None:
        first = self._plan(task_code="KRMB", iteration=1)
        second = self._plan(task_code="KRMB", iteration=2)
        # The chain task-code directory is shared across the handoff chain...
        self.assertEqual(first.executor_project_root.parent.parent, second.executor_project_root.parent.parent)
        # ...while each iteration owns its own task-id-scoped project root.
        self.assertNotEqual(first.executor_project_root.parent, second.executor_project_root.parent)

    def test_branch_and_task_code_isolation(self) -> None:
        branch_a = self._plan(task_code="KRMB", iteration=1)
        branch_b = self._plan(task_code="KRMC", iteration=1)
        self.assertNotEqual(branch_a.executor_project_root, branch_b.executor_project_root)

    # --- tampering ----------------------------------------------------------

    def test_validate_rejects_wrong_task_id_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(Path(workspace["executor_project_root"]).parts)
        parts[-2] = "KRMB-002"
        workspace["executor_project_root"] = str(Path(*parts))
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_wrong_date_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(Path(workspace["executor_project_root"]).parts)
        parts[parts.index(self.task_date)] = "2026-08-11"
        workspace["executor_project_root"] = str(Path(*parts))
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_wrong_task_code_segment(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        parts = list(Path(workspace["executor_project_root"]).parts)
        parts[-3] = "KRMC"
        workspace["executor_project_root"] = str(Path(*parts))
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_metadata_that_disagrees_with_path(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["task_code"] = "KRMC"
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["iteration"] = "002"
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_invalid_metadata_shape(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["iteration"] = "not-a-number"
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    # --- out of bounds ------------------------------------------------------

    def test_validate_rejects_path_outside_managed_artifacts(self) -> None:
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        workspace["executor_project_root"] = str(
            self.workspace.resolve()
            / "tasks"
            / "report"
            / self.task_date
            / "KRMB"
            / "KRMB-001"
            / "claude"
        )
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_path_under_customer_project(self) -> None:
        customer = self.root / "customer"
        customer.mkdir()
        workspace = self._plan(
            customer_dir=True, customer_path=customer, task_code="KRMB", iteration=1
        ).to_workspace()
        # Pointing the executor root back at the user project must fail closed.
        workspace["executor_project_root"] = str(customer.resolve())
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    # --- symlink ------------------------------------------------------------

    def test_validate_rejects_existing_parent_symlink_escape(self) -> None:
        date_dir = self.workspace / "tasks" / "artifacts" / self.task_date
        date_dir.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        date_dir.symlink_to(outside, target_is_directory=True)
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    def test_validate_rejects_symlink_to_non_canonical_managed_branch(self) -> None:
        date_dir = self.workspace / "tasks" / "artifacts" / self.task_date
        date_dir.parent.mkdir(parents=True)
        alternative = self.workspace / "tasks" / "artifacts" / "other-date"
        alternative.mkdir()
        date_dir.symlink_to(alternative, target_is_directory=True)
        workspace = self._plan(task_code="KRMB", iteration=1).to_workspace()
        with self.assertRaises(ABCError):
            validate_path_plan_workspace(workspace)

    # --- legacy records -----------------------------------------------------

    def test_legacy_workspace_without_executor_root_still_validates(self) -> None:
        # Records created before this phase (e.g. task_board.create_task) omit
        # the internal executor project root and must continue to validate.
        chain_root = self.workspace.resolve() / "tasks" / "artifacts" / self.task_date / "KRMB"
        report_root = self.workspace.resolve() / "tasks" / "report" / self.task_date / "KRMB"
        workspace = {
            "customer_dir": False,
            "customer_path": "",
            "project_root": str(chain_root),
            "default_path": str(chain_root),
            "agentbc_root": str(self.workspace.resolve()),
            "artifact_root": str(chain_root),
            "report_root": str(report_root),
            "task_file": str(report_root / "KRMB-001-task.md"),
            "report_file": str(report_root / "KRMB-001-report.md"),
            "task_code": "KRMB",
            "iteration": "001",
            "task_date": self.task_date,
        }
        validate_path_plan_workspace(workspace)


if __name__ == "__main__":
    unittest.main()
