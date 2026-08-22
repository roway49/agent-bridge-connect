from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_homebrew_formula.py"
SPEC = importlib.util.spec_from_file_location("generate_homebrew_formula", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class HomebrewFormulaTests(unittest.TestCase):
    def test_formula_is_manifest_pinned_and_has_service_and_smoke(self) -> None:
        manifest = {
            "tag": "v1.0.3A",
            "package_version": "1.0.3a1",
            "artifacts": [
                {
                    "filename": "agentbc-1.0.3a1.tar.gz",
                    "sha256": "a" * 64,
                },
                {
                    "filename": "agentbc-1.0.3a1-py3-none-any.whl",
                    "sha256": "b" * 64,
                },
            ],
        }
        formula = MODULE.generate_formula(manifest, "https://example.test/v1.0.3A")
        self.assertIn('url "https://example.test/v1.0.3A/agentbc-1.0.3a1.tar.gz"', formula)
        self.assertIn(f'sha256 "{"a" * 64}"', formula)
        self.assertIn('version "1.0.3a1"', formula)
        self.assertIn('depends_on "python@3.13"', formula)
        self.assertIn('virtualenv_install_with_resources', formula)
        self.assertIn('service do', formula)
        self.assertIn('"runner", "serve"', formula)
        self.assertIn('agentbc setup', formula)
        self.assertIn('brew services start agentbc', formula)

    def test_invalid_or_unpinned_manifest_is_rejected(self) -> None:
        cases = (
            {"tag": "v1.0.3A", "package_version": "1.0.3a1", "artifacts": []},
            {
                "tag": "v1.0.4A",
                "package_version": "1.0.3a1",
                "artifacts": [
                    {"filename": "agentbc-1.0.3a1.tar.gz", "sha256": "a" * 64}
                ],
            },
            {
                "tag": "v1.0.3A",
                "package_version": "1.0.3a1",
                "artifacts": [{"filename": "agentbc.tar.gz", "sha256": "bad"}],
            },
        )
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                MODULE.generate_formula(manifest, "https://example.test")

        valid = {
            "tag": "v1.0.3A",
            "package_version": "1.0.3a1",
            "artifacts": [
                {"filename": "agentbc-1.0.3a1.tar.gz", "sha256": "a" * 64}
            ],
        }
        with self.assertRaises(ValueError):
            MODULE.generate_formula(valid, "http://example.test")

    def test_release_workflows_generate_and_publish_formula(self) -> None:
        root = Path(__file__).parents[1]
        publish = (root / ".github" / "workflows" / "publish-pypi.yml").read_text(
            encoding="utf-8"
        )
        release_check = (
            root / ".github" / "workflows" / "release-check.yml"
        ).read_text(encoding="utf-8")
        for workflow in (publish, release_check):
            self.assertIn("scripts/generate_homebrew_formula.py", workflow)
            self.assertIn("ruby -c dist/agentbc.rb", workflow)
        self.assertIn("dist/agentbc.rb", publish)


if __name__ == "__main__":
    unittest.main()
