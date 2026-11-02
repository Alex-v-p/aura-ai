"""AURA-0096 experiment, baseline, regression, and attribution contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
EVALUATION_PATH = ROOT / "contracts/evaluation/v1/evaluation.schema.json"
GENERATED_PATH = (
    ROOT
    / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


def _resolve_parameter(document: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    reference = item.get("$ref")
    if reference is None:
        return item
    return document["components"]["parameters"][reference.rsplit("/", 1)[-1]]


def test_evaluation_v1_adds_closed_regression_and_attribution_resources() -> None:
    document = _load(EVALUATION_PATH)
    definitions = document["$defs"]
    union_refs = {item["$ref"].rsplit("/", 1)[-1] for item in document["oneOf"]}
    assert {"RegressionDecision", "AttributionReport"} <= union_refs

    for name in (
        "ComparisonEvidenceReference",
        "RegressionMetricComparison",
        "AttributionCandidate",
        "RegressionDecision",
        "AttributionReport",
    ):
        assert definitions[name]["additionalProperties"] is False

    decision = definitions["RegressionDecision"]
    assert decision["properties"]["decision"]["enum"] == [
        "regressed",
        "unchanged",
        "inconclusive",
        "unavailable",
        "incompatible",
    ]
    assert decision["properties"]["captureClassification"]["const"] == "metadata_only"
    assert decision["properties"]["evidenceState"]["enum"] == [
        "complete",
        "partial",
        "insufficient",
        "missing",
        "incompatible",
        "late",
    ]
    assert decision["properties"]["baselineResultIds"]["minItems"] == 1
    assert decision["properties"]["candidateResultIds"]["minItems"] == 1
    assert decision["properties"]["comparisons"]["maxItems"] == 128

    metric = definitions["RegressionMetricComparison"]
    assert metric["properties"]["metricClass"]["enum"] == [
        "operational",
        "quality",
    ]
    assert metric["properties"]["comparisonMode"]["enum"] == [
        "absolute",
        "relative",
    ]
    assert "confidence" not in metric["properties"]
    assert "significance" not in metric["properties"]

    attribution = definitions["AttributionReport"]
    assert attribution["properties"]["captureClassification"]["const"] == "metadata_only"
    assert attribution["properties"]["candidates"]["maxItems"] == 64


def test_observatory_v1_exposes_bounded_owner_only_resources() -> None:
    document = _load(OPENAPI_PATH)
    operations = _operations(document)
    expected = {
        "listExperiments",
        "createExperiment",
        "getExperiment",
        "listBaselines",
        "createBaselineCandidate",
        "getBaseline",
        "promoteBaseline",
        "listRegressions",
        "createRegressionComparison",
        "getRegression",
        "getRegressionAttribution",
    }
    assert expected <= operations.keys()

    for operation_id in expected:
        operation = operations[operation_id]
        assert operation.get("security", [{"observatorySession": []}]) == [
            {"observatorySession": []}
        ]
        assert {"401", "403"} <= operation["responses"].keys()

    for operation_id in ("listExperiments", "listBaselines", "listRegressions"):
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id]["parameters"]
        ]
        page_size = next(item for item in parameters if item["name"] == "page_size")
        assert page_size["schema"]["maximum"] == 200

    for operation_id in ("getRegression", "getRegressionAttribution"):
        operation = operations[operation_id]
        parameters = [
            _resolve_parameter(document, item)
            for item in operation["parameters"]
        ]
        version = next(item for item in parameters if item["name"] == "version")
        assert version["required"] is True
        assert version["schema"] == {
            "$ref": "#/components/schemas/EvaluationVersion"
        }
        assert "400" in operation["responses"]

    for operation_id in (
        "createExperiment",
        "createBaselineCandidate",
        "promoteBaseline",
        "createRegressionComparison",
    ):
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id]["parameters"]
        ]
        headers = {item["name"]: item for item in parameters if item["in"] == "header"}
        assert {"X-Observatory-CSRF", "Idempotency-Key"} <= headers.keys()

    promotion_parameters = [
        _resolve_parameter(document, item)
        for item in operations["promoteBaseline"]["parameters"]
    ]
    confirmation = next(
        item
        for item in promotion_parameters
        if item["name"] == "X-Observatory-Confirmation"
    )
    assert confirmation["schema"] == {"type": "string", "const": "PROMOTE_IMMUTABLE_BASELINE"}


def test_transport_shapes_are_closed_bounded_and_content_free() -> None:
    schemas = _load(OPENAPI_PATH)["components"]["schemas"]
    assert schemas["EvaluationSuite"]["properties"]["variantBindings"]["maxItems"] == 16
    names = (
        "EvaluationExperimentVariant",
        "EvaluationCohort",
        "CreateEvaluationExperimentRequest",
        "EvaluationExperiment",
        "EvaluationRegressionThreshold",
        "CreateEvaluationBaselineCandidateRequest",
        "PromoteEvaluationBaselineRequest",
        "EvaluationBaseline",
        "EvaluationEvidenceReference",
        "CreateEvaluationRegressionRequest",
        "EvaluationMetricComparison",
        "EvaluationAttributionCandidate",
        "EvaluationAttribution",
        "EvaluationRegression",
    )
    forbidden_properties = {
        "prompt",
        "response",
        "content",
        "comment",
        "rationale",
        "filename",
        "path",
        "endpoint",
        "credential",
        "token",
        "ownerId",
        "attributes",
    }
    for name in names:
        schema = schemas[name]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert forbidden_properties.isdisjoint(schema.get("properties", {})), name

    assert schemas["EvaluationCohort"]["properties"]["caseIds"]["maxItems"] == 1000
    assert schemas["EvaluationRegressionEvidenceState"]["enum"][-1] == "late"
    assert (
        schemas["EvaluationRegression"]["properties"]["capturePolicy"]["const"]
        == "metadata_only"
    )
    assert (
        schemas["EvaluationAttribution"]["properties"]["capturePolicy"]["const"]
        == "metadata_only"
    )
    assert schemas["EvaluationBaseline"]["properties"]["immutable"]["const"] is True
    assert (
        schemas["EvaluationBaseline"]["properties"]["promotionMode"]["const"]
        == "manual_owner_action"
    )


def test_history_and_sse_extensions_remain_metadata_only() -> None:
    schemas = _load(OPENAPI_PATH)["components"]["schemas"]
    assert {"experiment", "baseline", "regression", "attribution"} <= set(
        schemas["EvaluationResourceType"]["enum"]
    )
    event = schemas["EvaluationLiveEvent"]
    assert event["properties"]["capturePolicy"]["const"] == "metadata_only"
    assert {
        "evaluation_experiment",
        "evaluation_baseline",
        "evaluation_regression",
        "evaluation_attribution",
    } <= set(event["properties"]["resourceType"]["enum"])
    assert "evidence" not in event["properties"]
    assert "comparisons" not in event["properties"]


def test_generated_client_contains_exact_additive_transport_surface() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")
    for operation_id in (
        "listExperiments",
        "createExperiment",
        "getExperiment",
        "listBaselines",
        "createBaselineCandidate",
        "getBaseline",
        "promoteBaseline",
        "listRegressions",
        "createRegressionComparison",
        "getRegression",
        "getRegressionAttribution",
    ):
        assert f"readonly {operation_id}:" in generated
        assert f"  {operation_id}:" in generated
    assert "export type EvaluationRegression =" in generated
    assert "export type EvaluationAttribution =" in generated
    assert "export type EvaluationBaseline =" in generated


def test_openapi_local_references_resolve() -> None:
    document = _load(OPENAPI_PATH)
    unresolved: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                target: object = document
                for segment in reference[2:].split("/"):
                    if not isinstance(target, dict) or segment not in target:
                        unresolved.append(reference)
                        break
                    target = target[segment]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    assert unresolved == []
