from __future__ import annotations

import importlib.util
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "tooling" / "architecture" / "validate-observability.py"
sys.path.insert(0, str(ROOT / "tooling" / "architecture"))
SPEC = importlib.util.spec_from_file_location("validate_observability", VALIDATOR)
assert SPEC and SPEC.loader
OBSERVABILITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OBSERVABILITY)


class TypedObservabilityValidatorTests(unittest.TestCase):
    def _evaluation_manifests(self):
        manifest_root = ROOT / "services/aura-observatory/resources/component-manifests"
        names = ("evaluation-artifact-storage.yaml", "evaluation-deletion.yaml")
        return [
            (
                manifest_root / name,
                OBSERVABILITY.read_yaml(manifest_root / name),
            )
            for name in names
        ]

    def test_aura0090_components_use_typed_metadata_contract(self):
        identifiers = {
            "aura.observatory.evaluation_artifact_storage",
            "aura.observatory.evaluation_deletion",
        }
        self.assertTrue(identifiers <= OBSERVABILITY.OBSERVATORY_TYPED_COMPONENTS)
        self.assertEqual([], OBSERVABILITY.catalogue_errors(self._evaluation_manifests()))

    def test_artifact_storage_requires_metadata_only_and_privacy_allowlist(self):
        path, manifest = self._evaluation_manifests()[0]
        mutated = deepcopy(manifest)
        mutated["capture_policy"] = "manual_authorized_content"
        mutated["cardinality"]["prohibited"] = ["prompt"]

        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])

        self.assertTrue(any("capture_policy must be metadata_only" in error for error in errors))
        self.assertTrue(any("lacks required content/privacy fields" in error for error in errors))

    def test_evaluation_deletion_requires_typed_metric_contract(self):
        path, manifest = self._evaluation_manifests()[1]
        mutated = deepcopy(manifest)
        mutated["metric_definitions"][0]["type"] = "summary"
        mutated["cardinality"]["prohibited"] = []
        mutated["capture_policy"] = "full_content"

        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])

        self.assertTrue(
            any("type must be counter, gauge, or histogram" in error for error in errors)
        )
        self.assertTrue(any("capture_policy must be metadata_only" in error for error in errors))
        self.assertTrue(any("lacks required content/privacy fields" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
