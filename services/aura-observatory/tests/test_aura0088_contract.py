"""AURA-0088 metadata-v2 and run-comparison contract regressions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
V1_SCHEMA_PATH = ROOT / "contracts/telemetry/v1/metadata-observation.schema.json"
V2_SCHEMA_PATH = ROOT / "contracts/telemetry/v2/metadata-observation.schema.json"
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
GENERATED_PATH = (
    ROOT / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_metadata_v2_is_additive_closed_and_keeps_v1_unchanged() -> None:
    v1 = _read_json(V1_SCHEMA_PATH)
    v2 = _read_json(V2_SCHEMA_PATH)

    assert v1["properties"]["schemaVersion"] == {"const": 1}
    assert "execution" not in v1["properties"]
    assert "imageVersion" not in v1["$defs"]["DeploymentMetadata"]["properties"]

    assert v2["$schema"].endswith("/draft/2020-12/schema")
    assert v2["$id"].endswith("/telemetry/v2/metadata-observation.schema.json")
    assert v2["properties"]["schemaVersion"] == {"const": 2}
    assert v2["additionalProperties"] is False
    for name, definition in v2["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name

    unchanged = {
        "observationId",
        "observedAt",
        "kind",
        "component",
        "operation",
        "measurement",
        "dimensions",
        "trace",
        "correlation",
        "serviceRole",
        "capturePolicy",
    }
    for field in unchanged:
        assert v2["properties"][field] == v1["properties"][field]


def test_metadata_v2_allows_only_approved_bounded_revision_markers() -> None:
    schema = _read_json(V2_SCHEMA_PATH)
    deployment = schema["$defs"]["DeploymentMetadataV2"]
    execution = schema["$defs"]["ExecutionMetadata"]

    assert set(deployment["properties"]) == {
        "deploymentId",
        "gitCommit",
        "environment",
        "hostId",
        "imageVersion",
        "configurationVersion",
    }
    assert set(execution["properties"]) == {
        "modelId",
        "modelVersion",
        "agentRevision",
        "promptBundleRevision",
        "policyRevision",
    }
    revision = schema["$defs"]["RevisionIdentifier"]
    assert revision["minLength"] == 1
    assert revision["maxLength"] == 128
    assert revision == _read_json(OPENAPI_PATH)["components"]["schemas"]["RevisionIdentifier"]

    allowed_keys = set(schema["properties"])
    forbidden = {
        "prompt",
        "promptHash",
        "response",
        "memory",
        "document",
        "filename",
        "toolArguments",
        "toolResults",
        "credential",
        "endpoint",
        "bearerToken",
        "ownerIdentity",
        "attributes",
    }
    assert not forbidden.intersection(allowed_keys)
    assert not forbidden.intersection(deployment["properties"])
    assert not forbidden.intersection(execution["properties"])


@pytest.mark.parametrize("model_id", ["qwen3:8b", "llama3.2:latest", "fake"])
def test_model_identifier_accepts_supported_tagged_local_models(model_id: str) -> None:
    telemetry = _read_json(V2_SCHEMA_PATH)
    openapi = _read_json(OPENAPI_PATH)
    telemetry_model = telemetry["$defs"]["ModelIdentifier"]
    api_model = openapi["components"]["schemas"]["ModelIdentifier"]

    assert telemetry_model == api_model
    assert re.fullmatch(telemetry_model["pattern"], model_id)


@pytest.mark.parametrize(
    "revision",
    [
        "release-2026.10",
        "Image-V2",
        "sha256_abc",
        "contention-v1",
        "privateer-v2",
        "ownerless-1",
        "digestive-model-v1",
    ],
)
def test_revision_identifier_preserves_unrelated_safe_values(revision: str) -> None:
    schema = _read_json(V2_SCHEMA_PATH)["$defs"]["RevisionIdentifier"]

    assert re.fullmatch(schema["pattern"], revision)


@pytest.mark.parametrize(
    "segment",
    [
        "owner",
        "identity",
        "credential",
        "bearer",
        "token",
        "secret",
        "password",
        "private",
        "content",
        "hash",
        "digest",
        "endpoint",
    ],
)
def test_revision_and_model_grammars_reject_every_delimited_private_segment(
    segment: str,
) -> None:
    telemetry = _read_json(V2_SCHEMA_PATH)["$defs"]
    api = _read_json(OPENAPI_PATH)["components"]["schemas"]
    revision_patterns = {
        telemetry["RevisionIdentifier"]["pattern"],
        api["RevisionIdentifier"]["pattern"],
    }
    model_patterns = {
        telemetry["ModelIdentifier"]["pattern"],
        api["ModelIdentifier"]["pattern"],
    }

    assert len(revision_patterns) == 1
    assert len(model_patterns) == 1
    for pattern in revision_patterns:
        for candidate in (
            segment,
            f"release-{segment}-v1",
            f"release_{segment.upper()}_v1",
            f"v1.{segment.title()}.build",
        ):
            assert re.fullmatch(pattern, candidate) is None, candidate
    for pattern in model_patterns:
        for candidate in (
            segment,
            f"qwen3:{segment}",
            f"qwen3-{segment}:latest",
            f"qwen3:{segment.upper()}",
        ):
            assert re.fullmatch(pattern, candidate) is None, candidate


def test_private_marker_grammars_cover_every_contract_field_category() -> None:
    telemetry = _read_json(V2_SCHEMA_PATH)["$defs"]
    api = _read_json(OPENAPI_PATH)["components"]["schemas"]
    deployment = telemetry["DeploymentMetadataV2"]["properties"]
    execution = telemetry["ExecutionMetadata"]["properties"]

    for field in ("imageVersion", "configurationVersion"):
        assert deployment[field] == {"$ref": "#/$defs/RevisionIdentifier"}
    for field in (
        "modelVersion",
        "agentRevision",
        "promptBundleRevision",
        "policyRevision",
    ):
        assert execution[field] == {"$ref": "#/$defs/RevisionIdentifier"}
    assert execution["modelId"] == {"$ref": "#/$defs/ModelIdentifier"}
    assert api["ComparisonMarker"]["properties"]["value"]["oneOf"][0] == {
        "$ref": "#/components/schemas/RevisionIdentifier"
    }
    assert api["ModelComparisonMarker"]["properties"]["value"]["oneOf"][0] == {
        "$ref": "#/components/schemas/ModelIdentifier"
    }


@pytest.mark.parametrize(
    "model_id",
    [
        "https://models.invalid/qwen3:8b",
        "models/qwen3:8b",
        r"models\qwen3:8b",
        "qwen 3:8b",
        "owner@example.invalid",
        "owner:model",
        "qwen3:secret",
        "private-qwen:latest",
        "endpoint:latest",
        "qwen3:11434",
        "QWEN3:8b",
    ],
)
def test_model_identifier_rejects_non_metadata_and_private_shapes(
    model_id: str,
) -> None:
    schema = _read_json(V2_SCHEMA_PATH)["$defs"]["ModelIdentifier"]

    assert re.fullmatch(schema["pattern"], model_id) is None


def test_comparison_transport_is_owner_only_body_bounded_and_no_store() -> None:
    document = _read_json(OPENAPI_PATH)
    path = document["paths"]["/api/v1/run-comparisons/query"]
    operation = path["post"]

    assert document["info"]["x-contract-id"] == "observatory-v1"
    assert document["info"]["version"] == "1.0.0"
    assert document["security"] == [{"observatorySession": []}]
    assert set(path) == {"post"}
    assert operation["operationId"] == "compareRuns"
    assert operation.get("parameters", []) == []
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RunComparisonRequest"
    }
    response = operation["responses"]["200"]
    assert response["headers"]["Cache-Control"]["schema"] == {
        "type": "string",
        "const": "no-store",
    }
    assert {"400", "401", "403", "404", "422", "503"} <= set(operation["responses"])

    request = document["components"]["schemas"]["RunComparisonRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["leftRunId", "rightRunId"]
    assert set(request["properties"]) == {"leftRunId", "rightRunId"}


def test_comparison_schema_has_fixed_limits_and_explicit_evidence_states() -> None:
    schemas = _read_json(OPENAPI_PATH)["components"]["schemas"]
    limits = schemas["RunComparisonLimits"]["properties"]

    assert limits["runCount"]["const"] == 2
    assert limits["maximumObservationsPerRun"]["const"] == 10_000
    assert limits["maximumSpansPerRun"]["const"] == 5_000
    assert limits["maximumMeasurements"]["const"] == 2_000
    assert limits["maximumScannedRows"]["const"] == 1_000_000
    assert limits["executionTimeoutMs"]["const"] == 5_000
    assert schemas["ComparisonAvailability"]["enum"] == [
        "available",
        "missing",
        "unavailable",
    ]
    assert schemas["ComparisonState"]["enum"] == [
        "equal",
        "changed",
        "missing",
        "unavailable",
    ]
    assert schemas["ComparisonAlignment"]["enum"] == [
        "matched",
        "left_only",
        "right_only",
        "incompatible",
    ]
    assert set(schemas["TraceIntegrityState"]["enum"]) == {
        "complete",
        "retained_partial",
        "truncated",
        "missing_parent",
        "cyclic",
        "duplicate",
    }
    assert schemas["TraceComparison"]["properties"]["alignedSpans"]["maxItems"] == (10_000)
    assert schemas["RunComparison"]["properties"]["capturePolicy"] == {
        "type": "string",
        "const": "metadata_only",
    }


def test_measurement_and_marker_comparisons_are_closed_and_not_inferred() -> None:
    schemas = _read_json(OPENAPI_PATH)["components"]["schemas"]
    measurement = schemas["ComponentMeasurementComparison"]
    marker_names = set(schemas["ComparisonMarkerName"]["enum"])

    assert measurement["additionalProperties"] is False
    assert set(measurement["required"]) == set(measurement["properties"])
    assert measurement["properties"]["absoluteDelta"]["minimum"] == 0
    assert marker_names == {
        "deployment_id",
        "git_commit",
        "image_version",
        "configuration_version",
        "model_id",
        "model_version",
        "agent_revision",
        "prompt_bundle_revision",
        "policy_revision",
    }
    side = schemas["RunComparisonSide"]
    assert set(side["required"]) == set(side["properties"])
    assert side["properties"]["modelId"] == {"$ref": "#/components/schemas/ModelComparisonMarker"}
    assert schemas["ModelComparisonMarker"]["properties"]["value"]["oneOf"][0] == {
        "$ref": "#/components/schemas/ModelIdentifier"
    }
    assert schemas["ComparisonMarker"]["properties"]["value"]["oneOf"][0] == {
        "$ref": "#/components/schemas/RevisionIdentifier"
    }
    assert side["properties"]["componentVersions"]["maxItems"] == 256
    for name in (
        "RunComparisonRequest",
        "ComparisonMarker",
        "ModelComparisonMarker",
        "MetadataMarkerComparison",
        "ModelMarkerComparison",
        "RunComparisonComponentVersion",
        "RunComparisonSide",
        "ComparisonNumber",
        "ComponentMeasurementComparison",
        "ComponentComparisonSide",
        "RunComponentComparison",
        "ComparableSpan",
        "AlignedSpan",
        "TraceComparison",
        "RunComparisonLimits",
        "RunComparison",
    ):
        assert schemas[name]["additionalProperties"] is False, name
    assert schemas["MarkerComparison"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/MetadataMarkerComparison"},
            {"$ref": "#/components/schemas/ModelMarkerComparison"},
        ]
    }


def test_trace_alignment_contract_matches_two_bounded_span_sides() -> None:
    schemas = _read_json(OPENAPI_PATH)["components"]["schemas"]
    per_side = schemas["RunComparisonLimits"]["properties"]["maximumSpansPerRun"]["const"]
    aligned = schemas["TraceComparison"]["properties"]["alignedSpans"]
    key = schemas["AlignedSpan"]["properties"]["alignmentKey"]

    assert per_side == 5_000
    assert aligned["maxItems"] == per_side * 2 == 10_000
    assert key["minLength"] == 1
    assert key["maxLength"] == 512
    assert key["pattern"] == "^[A-Za-z0-9._:+-]+$"
    assert re.fullmatch(key["pattern"], "aura.runtime.model:infer:0")
    assert re.fullmatch(key["pattern"], "path-" + "a" * 64)
    assert re.fullmatch(key["pattern"], "a" * 512)
    assert re.fullmatch(key["pattern"], "a/b") is None


def test_generated_client_is_transport_only_and_in_sync_with_comparison() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")

    assert "export type RunComparisonRequest =" in generated
    assert "export type RunComparison =" in generated
    assert "readonly compareRuns:" in generated
    assert "method: 'POST', pathTemplate: '/api/v1/run-comparisons/query'" in generated
    assert "requestMode: 'json', responseMode: 'json'" in generated
    assert "fetch(" not in generated
    assert "localStorage" not in generated
    assert "sessionStorage" not in generated
