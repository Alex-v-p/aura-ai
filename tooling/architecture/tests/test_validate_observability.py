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

    def _runner_manifest(self):
        path = ROOT / "services/aura-core/resources/component-manifests/evaluation-runner.yaml"
        return path, OBSERVABILITY.read_yaml(path)

    def test_aura0091_runner_uses_typed_bounded_metadata_contract(self):
        path, manifest = self._runner_manifest()

        self.assertIn("aura.evaluation.runner", OBSERVABILITY.TYPED_COMPONENTS)
        self.assertEqual([], OBSERVABILITY.catalogue_errors([(path, manifest)]))

        metrics = manifest["metrics"]
        self.assertEqual(set(metrics), set(manifest["metric_semantics"]))
        self.assertEqual(set(metrics), set(manifest["metric_units"]))
        self.assertEqual(
            set(manifest["export"]["health_counters"]),
            OBSERVABILITY.RUNNER_EXPORT_HEALTH_COUNTERS,
        )

    def test_aura0091_runner_rejects_untyped_metric_and_unbounded_export(self):
        path, manifest = self._runner_manifest()
        mutated = deepcopy(manifest)
        metric = "evaluation_runner_queue_accepted"
        mutated["metric_units"][metric] = "seconds"
        mutated["metric_semantics"].pop(metric)
        mutated["cardinality"]["metric_attributes"].append("request_id")
        mutated["export"]["health_counters"].append("request_id")
        mutated["retention"]["process_buffer"] = "unbounded"

        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])

        self.assertTrue(any("metric_semantics must cover" in error for error in errors))
        self.assertTrue(any("is not a bounded unit" in error for error in errors))
        self.assertTrue(
            any("metric_attributes must be the fixed bounded set" in error for error in errors)
        )
        self.assertTrue(
            any("health counters are not fixed and bounded" in error for error in errors)
        )
        self.assertTrue(
            any("process retention is not the bounded policy" in error for error in errors)
        )

    def test_aura0091_runner_rejects_missing_trace_dependency_privacy_and_export_fields(self):
        path, manifest = self._runner_manifest()
        mutated = deepcopy(manifest)
        mutated["dependencies"] = []
        mutated["cardinality"]["trace_only"] = ["trace_id"]
        mutated["cardinality"]["prohibited"] = ["content"]
        mutated["export"].pop("sink")

        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])

        self.assertTrue(
            any("dependencies must be a non-empty string list" in error for error in errors)
        )
        self.assertTrue(
            any("trace_only must be the fixed bounded set" in error for error in errors)
        )
        self.assertTrue(
            any("prohibited fields lack required privacy fields" in error for error in errors)
        )
        self.assertTrue(any("export sink is not bounded" in error for error in errors))

    def test_aura0091_runner_requires_closed_per_metric_definitions(self):
        path, manifest = self._runner_manifest()
        mutations = {
            "name": "evaluation_runner_unknown_metric",
            "type": "summary",
            "unit": "seconds",
            "labels": ["request_id"],
            "dependencies": ["unbounded_dependency"],
            "trace": "request_payload",
            "correlation": "request_id",
            "export": ["prometheus"],
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                mutated = deepcopy(manifest)
                mutated["metric_definitions"][0][field] = value
                errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
                self.assertTrue(
                    any("metric_definitions" in error for error in errors),
                    msg=f"missing per-metric validation for {field}: {errors}",
                )

    def test_aura0091_runner_requires_exact_capture_policy_and_private_field_set(self):
        path, manifest = self._runner_manifest()
        mutated = deepcopy(manifest)
        mutated["capture_policy"] = "controlled_evaluation"
        errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
        self.assertTrue(any("capture_policy must be metadata_only" in error for error in errors))

        for field in OBSERVABILITY.RUNNER_PROHIBITED_FIELDS:
            with self.subTest(prohibited=field):
                mutated = deepcopy(manifest)
                mutated["cardinality"]["prohibited"].remove(field)
                errors = OBSERVABILITY.catalogue_errors([(path, mutated)])
                self.assertTrue(
                    any(
                        "prohibited fields lack required privacy fields" in error
                        for error in errors
                    ),
                    msg=f"missing private-field validation for {field}: {errors}",
                )

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
