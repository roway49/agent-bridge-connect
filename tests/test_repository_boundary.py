from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_repository_boundary.py"
SPEC = importlib.util.spec_from_file_location("check_repository_boundary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOUNDARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOUNDARY)


class RepositoryBoundaryTests(unittest.TestCase):
    def test_manifest_matches_working_tree_root_files(self) -> None:
        self.assertEqual(
            BOUNDARY.expected_root_files(),
            BOUNDARY.working_tree_root_files(),
        )

    def test_manifest_matches_index_root_files(self) -> None:
        self.assertEqual(
            BOUNDARY.expected_root_files(),
            BOUNDARY.index_root_files(),
        )

    def test_validator_rejects_added_and_deleted_root_files(self) -> None:
        expected = BOUNDARY.expected_root_files()
        self.assertEqual(0, BOUNDARY.validate(expected, source="test"))
        self.assertEqual(
            1,
            BOUNDARY.validate(expected | {"PRIVATE_NOTES.md"}, source="test"),
        )
        self.assertEqual(
            1,
            BOUNDARY.validate(expected - {"README.md"}, source="test"),
        )

if __name__ == "__main__":
    unittest.main()
