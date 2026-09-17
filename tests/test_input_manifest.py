from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_bridge_connect.input_manifest import (
    INPUTS_EXTENSION_KEY,
    cleanup_task_input_root,
    prepare_task_inputs,
    public_inputs_view,
    task_input_paths,
)
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.prompt_contract import build_prompt_contract
from agent_bridge_connect.service import TaskService


class InputManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.external = self.root / "outside"
        self.project.mkdir()
        self.external.mkdir()
        self.workspace = {
            "agentbc_root": str(self.root / "workspace"),
            "project_root": str(self.project),
            "task_date": "2026-09-13",
            "task_code": "ABCD",
            "iteration": "001",
            "task_id": "ABCD-001",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_external_image_and_file_are_frozen_without_type_or_size_policy(self) -> None:
        image = self.external / "input.unlisted-image-type"
        file_input = self.external / "payload.custom"
        image.write_bytes(b"image-bytes")
        file_input.write_bytes(b"x" * (1024 * 1024 + 7))

        extension, committed = prepare_task_inputs(
            images=[image], files=[file_input], workspace=self.workspace
        )

        self.assertIsNotNone(committed)
        packet = {"extensions": extension}
        frozen_images = task_input_paths(packet, kind="image")
        frozen_files = task_input_paths(packet, kind="file")
        self.assertEqual(frozen_images[0].read_bytes(), b"image-bytes")
        self.assertEqual(frozen_files[0].read_bytes(), file_input.read_bytes())
        self.assertNotEqual(frozen_images[0], image)
        self.assertEqual(extension[INPUTS_EXTENSION_KEY]["entries"][0]["source_scope"], "external_import")
        cleanup_task_input_root(committed)

    def test_project_input_is_direct_and_public_projection_redacts_path(self) -> None:
        source = self.project / "notes.txt"
        source.write_text("hello", encoding="utf-8")
        extension, committed = prepare_task_inputs(
            images=[], files=[source], workspace=self.workspace
        )
        self.assertIsNone(committed)
        packet = {"extensions": extension}
        self.assertEqual(task_input_paths(packet, kind="file"), [source.resolve()])
        public = public_inputs_view(extension[INPUTS_EXTENSION_KEY])
        self.assertNotIn("materialized_path", public["entries"][0])
        self.assertEqual(public["entries"][0]["source_scope"], "project")

    def test_symlink_source_fails_before_staging(self) -> None:
        target = self.external / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = self.external / "link.txt"
        link.symlink_to(target)
        with self.assertRaisesRegex(ABCError, "input source must not be a symlink"):
            prepare_task_inputs(images=[], files=[link], workspace=self.workspace)
        self.assertFalse((self.root / "workspace" / "tasks" / "inputs").exists())

    def test_generic_file_is_in_executor_prompt_and_does_not_create_approval(self) -> None:
        source = self.external / "query.txt"
        source.write_text("needle", encoding="utf-8")
        extension, committed = prepare_task_inputs(
            images=[], files=[source], workspace=self.workspace
        )
        packet = {
            "task_id": "ABCD-001",
            "title": "input test",
            "workspace": {
                **self.workspace,
                "artifact_root": str(self.project),
                "report_root": str(self.root / "reports"),
                "task_file": str(self.root / "task.md"),
                "report_file": str(self.root / "report.md"),
            },
            "task_board": {"root": str(self.root / "record")},
            "steps": [{"id": 1, "description": "inspect input", "status": "pending"}],
            "extensions": extension,
        }
        prompt = build_prompt_contract(packet)
        self.assertIn("Frozen file inputs:", prompt)
        self.assertIn(str(task_input_paths(packet, kind="file")[0]), prompt)
        self.assertNotIn("agentbc.approval", extension)
        self.assertNotIn("agentbc.input", extension)
        cleanup_task_input_root(committed)

    def test_task_creation_freezes_external_file_before_source_disappears(self) -> None:
        source = self.external / "external.txt"
        source.write_text("frozen", encoding="utf-8")
        service = TaskService(
            self.root / "record",
            config={"workspace_root": str(self.root / "workspace")},
        )
        task = service.create_task(
            "external input",
            "codex",
            [{"id": 1, "description": "read the file"}],
            customer_dir=True,
            customer_path=self.project,
            files=[source],
            permission_mode="full",
        )
        source.unlink()
        frozen = task_input_paths(task.to_dict(), kind="file")
        self.assertEqual(frozen[0].read_text(encoding="utf-8"), "frozen")
        self.assertNotIn("agentbc.approval", task.extensions)
        self.assertNotIn("agentbc.input", task.extensions)

    def test_input_import_does_not_change_any_executor_permission_mode(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            for mode in ("safe", "full"):
                with self.subTest(executor=executor, mode=mode):
                    source = self.external / f"{executor}-{mode}.txt"
                    source.write_text("payload", encoding="utf-8")
                    service = TaskService(
                        self.root / f"record-{executor}-{mode}",
                        config={"workspace_root": str(self.root / f"workspace-{executor}-{mode}")},
                    )
                    task = service.create_task(
                        "permission invariant",
                        executor,
                        [{"id": 1, "description": "read input"}],
                        customer_dir=True,
                        customer_path=self.project,
                        files=[source],
                        permission_mode=mode,
                    )
                    permission = task.extensions["agentbc.permission"]
                    self.assertEqual(permission["effective_mode"], mode)
                    self.assertNotIn("agentbc.approval", task.extensions)
                    self.assertNotIn("agentbc.input", task.extensions)

    def test_task_delete_removes_only_agentbc_owned_frozen_inputs(self) -> None:
        source = self.external / "delete-me-source.txt"
        source.write_text("preserve source", encoding="utf-8")
        service = TaskService(
            self.root / "delete-record",
            config={"workspace_root": str(self.root / "delete-workspace")},
        )
        task = service.create_task(
            "delete frozen input",
            "codex",
            [{"id": 1, "description": "read input"}],
            customer_dir=True,
            customer_path=self.project,
            files=[source],
            permission_mode="full",
        )
        input_root = Path(task.workspace["input_root"])
        task.status = "completed"
        service.store.write_task(task.id, task.to_dict())
        service._refresh_task_index()

        result = service.delete_task_chain(task.workspace["task_code"], confirmed=True)

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(input_root.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "preserve source")


if __name__ == "__main__":
    unittest.main()
