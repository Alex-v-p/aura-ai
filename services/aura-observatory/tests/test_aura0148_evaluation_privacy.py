"""AURA-0148 Observatory-side privacy and report-shape acceptance."""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest
from aura_observatory.run_evidence import evaluate_run_evidence_projection

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "evaluations" / "research" / "fixtures"
REPORT = ROOT / "evaluations" / "research" / "reports" / "AURA-0148-synthetic-report-manifest.json"


def _fixture(name: str) -> dict[str, Any]:
    gate: Any = import_module("evaluations.research.suites.gate")
    return cast(dict[str, Any], gate.load_versioned_fixture(FIXTURES / name))


def test_telemetry_fixture_declares_only_bounded_metadata_and_all_sinks() -> None:
    fixture = _fixture("privacy-telemetry-v1.json")
    assert set(fixture["sinks"]) == {"metrics", "spans", "events", "logs", "browser_state"}
    assert fixture["expectedProhibitedCount"] == 0
    assert set(fixture["prohibitedFields"]) >= {
        "ownerId",
        "taskId",
        "researchId",
        "query",
        "url",
        "excerpt",
        "objectKey",
    }
    assert set(fixture["allowedFields"]) <= {
        "component",
        "componentVersion",
        "operation",
        "outcome",
        "status",
        "dependency",
        "errorClass",
    }


def test_versioned_fixture_mutation_fails_closed(tmp_path: Path) -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    source = FIXTURES / "privacy-telemetry-v1.json"
    mutated = tmp_path / source.name
    mutated.write_bytes(
        source.read_bytes().replace(
            b'"expectedProhibitedCount": 0', b'"expectedProhibitedCount": 1'
        )
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        gate.load_versioned_fixture(mutated)


def test_every_versioned_fixture_rejects_digest_and_provenance_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    for name, (revision, expected_digest) in gate._FIXTURE_PROVENANCE.items():
        source = FIXTURES / name
        digest_mutation = tmp_path / f"digest-{name}"
        digest_mutation.write_bytes(source.read_bytes() + b" ")
        monkeypatch.setitem(
            gate._FIXTURE_PROVENANCE,
            digest_mutation.name,
            (revision, expected_digest),
        )
        with pytest.raises(ValueError, match="digest mismatch"):
            gate.load_versioned_fixture(digest_mutation)
        monkeypatch.delitem(gate._FIXTURE_PROVENANCE, digest_mutation.name)

        document = json.loads(source.read_text(encoding="utf-8"))
        revision_key = (
            "manifestRevision" if name == "evaluator-manifest-v1.json" else "fixtureRevision"
        )
        document[revision_key] = f"{revision}-mutated"
        provenance_mutation = tmp_path / f"provenance-{name}"
        mutated = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        provenance_mutation.write_bytes(mutated)
        monkeypatch.setitem(
            gate._FIXTURE_PROVENANCE,
            provenance_mutation.name,
            (revision, sha256(mutated).hexdigest()),
        )
        with pytest.raises(ValueError, match="provenance mismatch"):
            gate.load_versioned_fixture(provenance_mutation)
        monkeypatch.delitem(gate._FIXTURE_PROVENANCE, provenance_mutation.name)


def test_release_report_manifest_is_local_metadata_only_and_not_published() -> None:
    manifest = json.loads(REPORT.read_text(encoding="utf-8"))
    gate: Any = import_module("evaluations.research.suites.gate")
    gate.assert_content_free_metadata(manifest)
    assert manifest["workItem"] == "AURA-0148"
    assert manifest["capturePolicy"] == "metadata_only"
    assert manifest["realProvidersEnabled"] is False
    assert manifest["published"] is False
    assert manifest["contentPolicy"] == "excluded"


def test_observatory_run_projection_is_metadata_only_and_truthful() -> None:
    projection: dict[str, object] = {
        "schemaVersion": 1,
        "runId": "00000000-0000-4000-8000-000000000148",
        "traceId": "0123456789abcdef0123456789abcdef",
        "status": "partial",
        "steps": [],
        "completeness": {
            "state": "partial",
            "expectedEvidenceCount": 1,
            "capturedEvidenceCount": 0,
            "excludedEvidenceCount": 0,
            "partialEvidenceCount": 1,
            "droppedEvidenceCount": 0,
            "deletedEvidenceCount": 0,
            "reasons": ["provider_unavailable"],
        },
        "partial": True,
    }
    results = evaluate_run_evidence_projection(projection)
    assert results["observatory_run_evidence_privacy"] == "pass"
    assert results["run_inspection_projection_truth"] == "pass"
    assert all(value in {"pass", "fail", "inconclusive"} for value in results.values())

    prohibited = evaluate_run_evidence_projection(
        {**projection, "payload": "synthetic private text"}
    )
    assert prohibited["observatory_run_evidence_privacy"] == "fail"
    assert prohibited["metadata_v2_allowlist"] == "fail"


def test_content_free_release_report_helper_rejects_private_fields() -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    assert_content_free_metadata = gate.assert_content_free_metadata

    assert_content_free_metadata(
        gate.build_gate_report(
            (gate.GateDecision("research_privacy@1", True, 1.0, 1.0, "threshold_met"),),
            manifest_revision="aura0148-evaluator-manifest-v1",
            fixture_revision="research-provider-eval-fixtures-v1",
        )
    )
    with pytest.raises(ValueError, match="content-bearing"):
        assert_content_free_metadata(
            {
                "schemaVersion": 1,
                "metadata": {"objectKey": "synthetic-private-object"},
            }
        )
    with pytest.raises(ValueError):
        assert_content_free_metadata(
            {
                "schemaVersion": 1,
                "metadata": {"nested": {"url": "https://example.invalid"}},
            }
        )
    with pytest.raises(ValueError):
        assert_content_free_metadata(
            {
                "schemaVersion": 1,
                "reportRevision": "aura0148-gate-report-v1",
                "decisions": [
                    {
                        "evaluatorId": "research_privacy@1",
                        "score": float("nan"),
                        "threshold": 1.0,
                        "passed": False,
                        "reason": "malformed_score",
                    }
                ],
            }
        )


def test_release_report_requires_complete_constants_and_cross_field_counts() -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    valid = gate.build_gate_report(
        (gate.GateDecision("research_privacy@1", True, 1.0, 1.0, "threshold_met"),),
        manifest_revision="aura0148-evaluator-manifest-v1",
        fixture_revision="research-provider-eval-fixtures-v1",
    )
    broken = {**valid, "passedCount": 0}
    with pytest.raises(ValueError, match="counts"):
        gate.assert_content_free_metadata(broken)
    broken = {**valid, "capturePolicy": "private evidence"}
    with pytest.raises(ValueError):
        gate.assert_content_free_metadata(broken)
    broken = {key: value for key, value in valid.items() if key != "decisions"}
    with pytest.raises(ValueError, match="incomplete"):
        gate.assert_content_free_metadata(broken)


def test_typed_metadata_rejects_nested_canaries_and_scalar_identifiers() -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    valid = gate.build_gate_report(
        (gate.GateDecision("research_privacy@1", True, 1.0, 1.0, "threshold_met"),),
        manifest_revision="aura0148-evaluator-manifest-v1",
        fixture_revision="research-provider-eval-fixtures-v1",
    )
    decision = dict(valid["decisions"][0])
    decision["reason"] = "https://private.invalid"
    with pytest.raises(ValueError):
        gate.assert_content_free_metadata({**valid, "decisions": [decision]})
    decision = dict(valid["decisions"][0])
    decision["evaluatorId"] = "00000000-0000-4000-8000-000000000148"
    with pytest.raises(ValueError):
        gate.assert_content_free_metadata({**valid, "decisions": [decision]})


def test_observed_acceptance_contract_rejects_missing_duplicate_and_failed_assertions(
    tmp_path: Path,
) -> None:
    gate: Any = import_module("evaluations.research.suites.gate")
    thresholds = gate.load_thresholds(ROOT / "evaluations/research/release-thresholds.yaml")
    inventory = gate.load_acceptance_inventory()
    python_ids = sorted(inventory["python"])
    playwright_ids = sorted(inventory["playwright"])

    def report(python: list[str], playwright: list[str]) -> dict[str, object]:
        def section(command: str, tests: list[str]) -> dict[str, object]:
            rows = [{"id": item, "outcome": "passed", "assertionCount": 1} for item in tests]
            return {
                "command": command,
                "exitCode": 0,
                "discovered": len(rows),
                "passed": len(rows),
                "failed": 0,
                "skipped": 0,
                "tests": rows,
            }

        return {
            "schemaVersion": 1,
            "runner": "aura0148-observed-acceptance@1",
            "capturePolicy": "metadata_only",
            "python": section("aura0148.python.scoped", python),
            "playwright": section("aura0148.playwright.scoped", playwright),
        }

    required = tuple(thresholds["evaluators"])
    valid = report(python_ids, playwright_ids)
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(valid), encoding="utf-8")
    observations = gate.load_observed_acceptance(valid_path, required_evaluators=required)
    assert len(observations) == 26
    assert all(
        observation["observationId"] in gate._OBSERVATION_IDS
        for observation in observations.values()
    )
    assert (
        len(
            {
                (evaluator, observation["observationId"])
                for evaluator, observation in observations.items()
            }
        )
        == 26
    )

    missing = report(python_ids[:-1], playwright_ids)
    missing_path = tmp_path / "missing.json"
    missing_path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        gate.load_observed_acceptance(missing_path, required_evaluators=required)

    duplicate_id = gate._PLAYWRIGHT_RESEARCH_TEST_ID
    duplicate = report(python_ids, playwright_ids)
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    original_plan = gate._OBSERVATION_PLAN["journey_quick_chat@1"]
    gate._OBSERVATION_PLAN["journey_quick_chat@1"] = (
        original_plan[0],
        (*original_plan[1], duplicate_id),
        original_plan[2],
    )
    try:
        with pytest.raises(ValueError, match="missing or duplicate"):
            gate.load_observed_acceptance(duplicate_path, required_evaluators=required)
    finally:
        gate._OBSERVATION_PLAN["journey_quick_chat@1"] = original_plan

    failed = report(python_ids, playwright_ids)
    failed["python"]["tests"][0]["outcome"] = "failed"  # type: ignore[index]
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(failed), encoding="utf-8")
    with pytest.raises(ValueError, match="test record"):
        gate.load_observed_acceptance(failed_path, required_evaluators=required)

    research_test_id = gate._PLAYWRIGHT_RESEARCH_TEST_ID
    admission_test_id = (
        "desktop::governed-research-acceptance.spec.ts::"
        "Aura governed research synthetic acceptance › admits every bounded mode with "
        "server-owned budgets from /research"
    )
    admission_only = report(
        python_ids,
        [
            admission_test_id,
            *(
                item
                for item in playwright_ids
                if item not in {research_test_id, admission_test_id}
            ),
        ],
    )
    admission_only_path = tmp_path / "research-admission-only.json"
    admission_only_path.write_text(json.dumps(admission_only), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        gate.load_observed_acceptance(admission_only_path, required_evaluators=required)

    research_duplicate_id = admission_test_id
    research_duplicate = report(python_ids, playwright_ids)
    research_duplicate_path = tmp_path / "research-duplicate.json"
    research_duplicate_path.write_text(json.dumps(research_duplicate), encoding="utf-8")
    original_research_plan = gate._OBSERVATION_PLAN["journey_quick_research@1"]
    gate._OBSERVATION_PLAN["journey_quick_research@1"] = (
        original_research_plan[0],
        (*original_research_plan[1], research_duplicate_id),
        original_research_plan[2],
    )
    try:
        with pytest.raises(ValueError, match="missing or duplicate"):
            gate.load_observed_acceptance(
                research_duplicate_path, required_evaluators=required
            )
    finally:
        gate._OBSERVATION_PLAN["journey_quick_research@1"] = original_research_plan

    research_failed = report(python_ids, playwright_ids)
    research_failed_playwright = cast(dict[str, object], research_failed["playwright"])
    research_failed_rows = cast(
        list[dict[str, object]], research_failed_playwright["tests"]
    )
    for row in research_failed_rows:
        if row["id"] == research_test_id:
            row["outcome"] = "failed"
    research_failed_path = tmp_path / "research-failed.json"
    research_failed_path.write_text(json.dumps(research_failed), encoding="utf-8")
    with pytest.raises(ValueError, match="test record"):
        gate.load_observed_acceptance(research_failed_path, required_evaluators=required)
