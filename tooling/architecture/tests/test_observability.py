from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "tooling" / "architecture" / "validate-observability.py"
sys.path.insert(0, str(ROOT / "tooling" / "architecture"))
SPEC = importlib.util.spec_from_file_location("observability", VALIDATOR)
OBSERVABILITY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(OBSERVABILITY)


class ObservabilityTests(unittest.TestCase):
    def _aura0085_manifests(self):
        names = (
            "telemetry-ingestion.yaml",
            "telemetry-storage.yaml",
            "query-api.yaml",
            "live-updates.yaml",
        )
        return [
            (
                ROOT / "services/aura-observatory/resources/component-manifests" / name,
                OBSERVABILITY.read_yaml(
                    ROOT / "services/aura-observatory/resources/component-manifests" / name
                ),
            )
            for name in names
        ]

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

    def test_aura0085_typed_manifests_pass_closed_contract_validation(self):
        errors = OBSERVABILITY.catalogue_errors(self._aura0085_manifests())
        self.assertEqual([], errors)

    def test_aura0085_typed_manifest_rejects_metric_definition_drift(self):
        entries = self._aura0085_manifests()
        path, manifest = entries[0]
        mutated = deepcopy(manifest)
        mutated["metric_definitions"][0]["type"] = "summary"
        mutated["metric_definitions"][1]["labels"] = ["unbounded_attribute"]
        mutated["cardinality"]["prohibited"] = ["prompt"]
        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
        self.assertTrue(
            any("type must be counter, gauge, or histogram" in error for error in errors)
        )
        self.assertTrue(any("labels must be declared" in error for error in errors))
        self.assertTrue(any("lacks required content/privacy fields" in error for error in errors))

    def test_aura0085_typed_manifest_rejects_replay_capture_and_retention_drift(self):
        path, manifest = self._aura0085_manifests()[2]
        mutated = deepcopy(manifest)
        mutated["input"]["replayable"] = "yes"
        mutated["capture_policy"] = "full_content"
        mutated["retention"] = "automatic_ttl"
        mutated["evaluators"] = []
        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
        self.assertTrue(any("input.replayable must be boolean" in error for error in errors))
        self.assertTrue(any("capture_policy must be metadata_only" in error for error in errors))
        self.assertTrue(
            any("retention must be manual_owner_delete_only" in error for error in errors)
        )
        self.assertTrue(any("evaluators" in error and "bounded" in error for error in errors))

    def test_aura0093_typed_observatory_components_are_mutation_checked(self):
        paths = [
            ROOT
            / "services/aura-observatory/resources/component-manifests/evaluation-definitions.yaml",
            ROOT
            / (
                "services/aura-observatory/resources/component-manifests/"
                "deterministic-evaluators.yaml"
            ),
        ]
        for path in paths:
            manifest = OBSERVABILITY.read_yaml(path)
            self.assertIn(manifest["component"]["id"], OBSERVABILITY.OBSERVATORY_TYPED_COMPONENTS)
            mutated = deepcopy(manifest)
            mutated["metric_definitions"][0]["labels"] = ["raw_content"]
            errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
            self.assertTrue(any("labels must be declared" in error for error in errors))
            mutated = deepcopy(manifest)
            mutated["capture_policy"] = "full_content"
            errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
            self.assertTrue(
                any("capture_policy must be metadata_only" in error for error in errors)
            )

    def test_aura0093_runtime_declares_every_typed_metric(self):
        runtime = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "services/aura-observatory/src").rglob("*.py")
        )
        for name in (
            "observatory_evaluation_definition_operation_count",
            "observatory_evaluation_definition_validation_count",
            "observatory_evaluator_execution_count",
            "observatory_evaluator_duration_ms",
            "observatory_evaluator_outcome_count",
            "observatory_evaluator_incomplete_evidence_count",
        ):
            self.assertIn(name, runtime)

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
