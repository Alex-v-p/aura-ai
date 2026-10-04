from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("aura_common", ROOT / "tooling" / "architecture" / "common.py")
COMMON = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(COMMON)


class CommonTests(unittest.TestCase):
    def test_templates_validate(self):
        pairs = {
            "work-item.yaml": "work-item.schema.json",
            "implementation-handoff.yaml": "implementation-handoff.schema.json",
            "review-verdict.yaml": "review-verdict.schema.json",
            "integration-report.yaml": "integration-report.schema.json",
        }
        for template, schema in pairs.items():
            with self.subTest(template=template):
                document, errors = COMMON.validate_document(ROOT / "work-items" / "templates" / template, schema)
                self.assertIsNotNone(document)
                self.assertEqual([], errors)

    def test_active_bootstrap_work_item_validates(self):
        item, errors = COMMON.validate_document(ROOT / "work-items" / "archived" / "AURA-0001.yaml", "work-item.schema.json")
        errors.extend(COMMON.semantic_work_item_errors(item))
        self.assertEqual([], errors)

    def test_path_matching(self):
        self.assertTrue(COMMON.path_matches(".codex/hooks.json", ".codex/**"))
        self.assertTrue(COMMON.path_matches("docs/AGENTS.md", "**/AGENTS.md"))
        self.assertTrue(COMMON.path_matches(ROOT / "docs" / "development" / "x.md", "docs/**"))
        self.assertFalse(COMMON.path_matches("services/aura-core/a.py", "docs/**"))

    def test_allowed_forbidden_overlap_rejected(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item["allowed_paths"] = ["services/aura-core/**"]
        item["forbidden_paths"] = ["services/aura-core/migrations/**"]
        self.assertTrue(any("overlaps" in error for error in COMMON.semantic_work_item_errors(item)))

    def test_critical_path_requires_authorization(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item["allowed_paths"] = [".codex/**"]
        item["forbidden_paths"] = ["services/**"]
        errors = COMMON.scope_errors([".codex/config.toml"], item)
        self.assertTrue(any("human_approval_required" in error for error in errors))
        item["human_approval_required"] = True
        item["change_class"] = "repository_governance"
        self.assertEqual([], COMMON.scope_errors([".codex/config.toml"], item))

    def test_baseline_permission_required(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item.update(change_class="evaluation", human_approval_required=True)
        item["allowed_paths"] = ["evaluations/golden-results/**"]
        item["forbidden_paths"] = ["services/**"]
        errors = COMMON.scope_errors(["evaluations/golden-results/result.yaml"], item)
        self.assertTrue(any("baseline" in error for error in errors))
        item["baseline_changes_allowed"] = True
        self.assertEqual([], COMMON.scope_errors(["evaluations/golden-results/result.yaml"], item))

    def test_unowned_path_is_rejected(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item["allowed_paths"] = ["unknown/**"]
        item["forbidden_paths"] = ["services/**"]
        errors = COMMON.scope_errors(["unknown/file.txt"], item)
        self.assertTrue(any("no architectural owner" in error for error in errors))

    def test_platform_worker_owns_root_compose(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item["allowed_paths"] = ["compose.yaml"]
        item["forbidden_paths"] = ["services/**"]

        self.assertEqual([], COMMON.scope_errors(["compose.yaml"], item, "platform_worker"))

    def test_non_owner_cannot_change_root_compose(self):
        item = COMMON.read_yaml(ROOT / "work-items" / "templates" / "work-item.yaml")
        item["allowed_paths"] = ["compose.yaml"]
        item["forbidden_paths"] = ["services/**"]

        errors = COMMON.scope_errors(["compose.yaml"], item, "frontend_worker")

        self.assertTrue(any("role frontend_worker is not an owner" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
