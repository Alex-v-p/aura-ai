"""Bridge the versioned AURA-0148 evaluation gate into the platform runner.

The adapter consumes the evaluation worker's public gate helper and produces a
bounded typed decision report.  It does not change evaluation thresholds or
persist content-bearing evidence.  A missing, duplicate, malformed, or failed
decision is a hard release-gate failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any, cast

VERSIONED_FIXTURES = (
    "artifacts-task-center-v1.json",
    "destination-corpus-v1.json",
    "evaluator-manifest-v1.json",
    "journeys-v1.json",
    "privacy-telemetry-v1.json",
    "quality-citations-v1.json",
    "resilience-durability-v1.json",
)


def _validate_fixture_provenance(root: Path, gate: Any) -> None:
    """Require every checked-in evaluation fixture to pass its digest gate."""

    fixture_root = root / "evaluations/research/fixtures"
    observed = tuple(sorted(path.name for path in fixture_root.glob("*.json")))
    if observed != tuple(sorted(VERSIONED_FIXTURES)):
        raise ValueError("evaluation fixture inventory is not immutable")
    for name in VERSIONED_FIXTURES:
        gate.load_versioned_fixture(fixture_root / name)


def _observed_result(
    evaluator_id: str,
    observation: dict[str, object],
    manifest_entries: dict[str, dict[str, str]],
    result_type: Any,
) -> Any:
    """Construct a result only after a matching executed assertion passed."""

    entry = manifest_entries[evaluator_id]
    if observation.get("outcome") != "passed":
        raise ValueError(f"observed assertion did not pass for {evaluator_id}")
    return result_type(
        evaluator_id=evaluator_id,
        evaluator_version=entry["evaluatorVersion"],
        component_id=entry["componentId"],
        component_version=entry["componentVersion"],
        fixture_revision=entry["fixtureRevision"],
        replay_key=entry["replayKey"],
        score=1.0,
        outcome="pass",
        metadata=(
            ("observation", str(observation["observationId"])),
            ("assertionCount", str(observation["assertionCount"])),
            ("outcome", "passed"),
        ),
    )


def _journey_results(
    observations: dict[str, dict[str, object]],
    manifest_entries: dict[str, dict[str, str]],
    result_type: Any,
) -> list[Any]:
    results: list[Any] = []
    for mode in ("quick", "auto", "deep", "agent_led"):
        for origin in ("chat", "research"):
            mode_id = "auto_detached" if mode == "auto" else mode
            evaluator_id = f"journey_{mode_id}_{origin}@1"
            results.append(
                _observed_result(
                    evaluator_id,
                    observations[evaluator_id],
                    manifest_entries,
                    result_type,
                )
            )
    return results


def _projection_result(
    observations: dict[str, dict[str, object]],
    manifest_entries: dict[str, dict[str, str]],
    result_type: Any,
) -> Any:
    return _observed_result(
        "task_projection_consistency@1",
        observations["task_projection_consistency@1"],
        manifest_entries,
        result_type,
    )


def _destination_result(
    observations: dict[str, dict[str, object]],
    manifest_entries: dict[str, dict[str, str]],
    result_type: Any,
) -> Any:
    return _observed_result(
        "research_destination_policy@1",
        observations["research_destination_policy@1"],
        manifest_entries,
        result_type,
    )


def _artifact_results(
    observations: dict[str, dict[str, object]],
    manifest_entries: dict[str, dict[str, str]],
    result_type: Any,
) -> list[Any]:
    return [
        _observed_result(
            "generated_artifact_integrity@1",
            observations["generated_artifact_integrity@1"],
            manifest_entries,
            result_type,
        ),
        _observed_result(
            "generated_artifact_authorization@1",
            observations["generated_artifact_authorization@1"],
            manifest_entries,
            result_type,
        ),
        _observed_result(
            "generated_artifact_deletion_truth@1",
            observations["generated_artifact_deletion_truth@1"],
            manifest_entries,
            result_type,
        ),
        _observed_result(
            "generated_artifact_privacy@1",
            observations["generated_artifact_privacy@1"],
            manifest_entries,
            result_type,
        ),
    ]


def build_report(root: Path, observed_path: Path | None = None) -> dict[str, Any]:
    # Import through the evaluation package's public helper.  No private Core
    # module is used and no production content is passed across the boundary.
    research_evaluation = cast(Any, import_module("aura_core.providers.research.evaluation"))
    gate = cast(Any, import_module("evaluations.research.suites.gate"))

    _validate_fixture_provenance(root, gate)
    observed_path = observed_path or root / ".aura-governed-research-observed-acceptance.json"
    if not observed_path.is_file():
        raise ValueError("observed AURA-0148 acceptance report is missing")
    threshold_path = root / "evaluations/research/release-thresholds.yaml"
    thresholds = gate.load_thresholds(threshold_path)
    fixture_revision = str(thresholds["fixtureRevision"])
    manifest = cast(dict[str, Any], thresholds["manifest"])
    manifest_entries = {
        str(entry["id"]): cast(dict[str, str], entry) for entry in manifest["evaluators"]
    }
    required = tuple(str(item) for item in thresholds["evaluators"])
    observations = gate.load_observed_acceptance(observed_path, required_evaluators=required)
    result_type = research_evaluation.ResearchEvaluationResult
    suite = research_evaluation.run_research_evaluation_fixtures(
        research_evaluation.DeterministicResearchEvaluationCaptureAdapter()
    )
    # Core's executable evaluator supplies provenance and diagnostic metadata;
    # the observed acceptance assertion is the sole pass/fail authority for
    # the platform release decision, including deletion and injection.
    results: list[object] = [
        replace(
            result,
            score=1.0 if observations[result.evaluator_id]["outcome"] == "passed" else 0.0,
            outcome="pass" if observations[result.evaluator_id]["outcome"] == "passed" else "fail",
        )
        for result in suite.baseline
    ]
    results.append(
        _destination_result(
            observations,
            manifest_entries,
            result_type,
        )
    )
    results.extend(_artifact_results(observations, manifest_entries, result_type))
    results.append(
        _projection_result(
            observations,
            manifest_entries,
            result_type,
        )
    )
    results.extend(
        _journey_results(
            observations,
            manifest_entries,
            result_type,
        )
    )
    decisions = gate.aggregate(
        results,
        required_evaluators=required,
        thresholds=thresholds["evaluators"],
        manifest=manifest,
        observations=observations,
    )
    payload = gate.build_gate_report(
        decisions,
        manifest_revision=str(thresholds["manifestRevision"]),
        fixture_revision=fixture_revision,
    )
    gate.assert_content_free_metadata(payload)
    return payload


def validate_report(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("release gate report is not an object")
    payload = cast(dict[str, Any], payload)
    if payload.get("capturePolicy") != "metadata_only":
        raise ValueError("release gate report is not metadata-only")
    if payload.get("realProvidersEnabled") is not False or payload.get("published") is not False:
        raise ValueError("release gate report violates provider/publication policy")
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise ValueError("release gate decisions are missing")
    decision_values: list[object] = cast(list[object], raw_decisions)
    decisions: list[dict[str, Any]] = []
    for raw_item in decision_values:
        if not isinstance(raw_item, dict):
            raise ValueError("release gate decision is malformed")
        decisions.append(cast(dict[str, Any], raw_item))
    required = {"evaluatorId", "score", "threshold", "passed", "reason"}
    identifiers: list[str] = []
    for item in decisions:
        if set(item) != required:
            raise ValueError("release gate decision is malformed")
        if not isinstance(item["evaluatorId"], str):
            raise ValueError("release gate decision identity is malformed")
        if item["score"] is not None and type(item["score"]) is not float:
            raise ValueError("release gate decision score is malformed")
        if item["threshold"] is not None and type(item["threshold"]) is not float:
            raise ValueError("release gate decision threshold is malformed")
        if type(item["passed"]) is not bool or not isinstance(item["reason"], str):
            raise ValueError("release gate decision result is malformed")
        identifiers.append(item["evaluatorId"])
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("release gate contains duplicate evaluator decisions")
    if any(item["passed"] is not True for item in decisions):
        raise ValueError("release gate contains a failed evaluator decision")
    if payload.get("status") != "passed" or payload.get("evaluatorCount") != len(decisions):
        raise ValueError("release gate summary is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(args.root, args.observed)
        validate_report(report)
    except (ImportError, OSError, ValueError, TypeError, AssertionError) as error:
        print(f"release gate failed closed: {type(error).__name__}", file=sys.stderr)
        return 1
    args.report.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"evaluation release gate passed ({len(report['decisions'])} decisions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
