from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "tooling" / "architecture" / "validate-observability.py"
sys.path.insert(0, str(ROOT / "tooling" / "architecture"))
SPEC = importlib.util.spec_from_file_location("observability", VALIDATOR)
OBSERVABILITY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(OBSERVABILITY)


class ObservabilityTests(unittest.TestCase):
    def test_catalogue_rejects_manifest_without_id(self):
        manifest = {
            "component": {"version": "1.0.0", "owner": "test", "category": "test"},
            "input": {},
            "output": {},
            "metrics": ["duration_ms"],
            "capture_policy": "metadata_only",
        }
        errors = OBSERVABILITY.catalogue_errors(
            [(ROOT / "services/aura-core/resources/component-manifests/missing-id.yaml", manifest)]
        )
        self.assertTrue(any("component.id is required" in error for error in errors))

    def test_catalogue_rejects_duplicate_ids_before_lookup(self):
        def manifest() -> dict[str, object]:
            return {
                "component": {
                    "id": "aura.test.duplicate",
                    "version": "1.0.0",
                    "owner": "test",
                    "category": "test",
                },
                "input": {},
                "output": {},
                "metrics": ["duration_ms"],
                "capture_policy": "metadata_only",
            }

        entries = [
            (ROOT / "services/aura-core/resources/component-manifests/first.yaml", manifest()),
            (ROOT / "services/aura-core/resources/component-manifests/second.yaml", manifest()),
        ]
        errors = OBSERVABILITY.catalogue_errors(entries)
        self.assertTrue(
            any("duplicate component id aura.test.duplicate" in error for error in errors)
        )

    def test_catalogue_rejects_scalar_and_list_documents(self):
        entries = [
            (
                ROOT / "services/aura-core/resources/component-manifests/scalar.yaml",
                "not a manifest",
            ),
            (
                ROOT / "services/aura-core/resources/component-manifests/list.yaml",
                ["not", "a", "manifest"],
            ),
        ]
        errors = OBSERVABILITY.catalogue_errors(entries)
        self.assertEqual(2, sum("manifest must be a mapping" in error for error in errors))

    def test_no_argument_validates_complete_catalogue(self):
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("passed", payload["status"])

    def test_work_item_mode_keeps_specific_obligation_validation(self):
        result = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--work-item",
                "work-items/archived/AURA-0048.yaml",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("passed", payload["status"])


if __name__ == "__main__":
    unittest.main()
