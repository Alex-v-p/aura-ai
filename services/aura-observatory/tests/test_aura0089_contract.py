"""AURA-0089 closed evaluation-resource contract verification.

These tests intentionally use a small dependency-free JSON-Schema runner.  The
repository does not make a Python schema-validation package part of the
runtime, while the contract itself is validated here rather than by copying
the schema into a second model.  Keeping the runner local also makes the
negative privacy cases deterministic in the minimal Observatory image.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[3]
SCHEMA_PATH = ROOT / "contracts/evaluation/v1/evaluation.schema.json"
FAILURE_CODES_PATH = ROOT / "contracts/evaluation/v1/failure-codes.schema.json"


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class InvalidSchema(ValueError):
    pass


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _matches(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    try:
        validate_schema(value, schema, root, "$instance")
    except InvalidSchema:
        return False
    return True


def validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            validate_schema(value, root["$defs"][ref.removeprefix("#/$defs/")], root, path)
        elif ref.endswith("failure-codes.schema.json"):
            failure_schema = json.loads(FAILURE_CODES_PATH.read_text(encoding="utf-8"))
            validate_schema(value, failure_schema, failure_schema, path)
        else:
            raise InvalidSchema(f"unsupported reference at {path}: {ref}")
        return
    if "oneOf" in schema:
        matches = sum(_matches(value, candidate, root) for candidate in schema["oneOf"])
        if matches != 1:
            raise InvalidSchema(f"oneOf expected one match at {path}, got {matches}")
    if "anyOf" in schema and not any(
        _matches(value, candidate, root) for candidate in schema["anyOf"]
    ):
        raise InvalidSchema(f"anyOf expected a match at {path}")
    if "allOf" in schema:
        for candidate in schema["allOf"]:
            validate_schema(value, candidate, root, path)
    if "if" in schema:
        selected = _matches(value, schema["if"], root)
        branch = schema.get("then" if selected else "else")
        if branch is not None:
            validate_schema(value, branch, root, path)
    if "not" in schema and _matches(value, schema["not"], root):
        raise InvalidSchema(f"not matched at {path}")
    if "type" in schema:
        expected = cast(str | list[str], schema["type"])
        if isinstance(expected, list):
            if not any(_type_matches(value, item) for item in expected):
                raise InvalidSchema(f"type mismatch at {path}")
        elif not _type_matches(value, expected):
            raise InvalidSchema(f"type mismatch at {path}")
    if "const" in schema and value != schema["const"]:
        raise InvalidSchema(f"const mismatch at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidSchema(f"enum mismatch at {path}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 2**31):
            raise InvalidSchema(f"length mismatch at {path}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise InvalidSchema(f"pattern mismatch at {path}")
        if (
            schema.get("format") == "uuid"
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
            is None
        ):
            raise InvalidSchema(f"uuid format mismatch at {path}")
        if (
            schema.get("format") == "date-time"
            and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z",
                value,
            )
            is None
        ):
            raise InvalidSchema(f"date-time format mismatch at {path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get(
            "maximum", float("inf")
        ):
            raise InvalidSchema(f"number range mismatch at {path}")
    if isinstance(value, list):
        items = cast(list[Any], value)
        if len(items) < schema.get("minItems", 0) or len(items) > schema.get("maxItems", 2**31):
            raise InvalidSchema(f"array length mismatch at {path}")
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in items}
        ) != len(items):
            raise InvalidSchema(f"duplicate items at {path}")
        if "items" in schema:
            for index, item in enumerate(items):
                validate_schema(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(value, dict):
        value_dict = cast(dict[str, Any], value)
        min_properties = schema.get("minProperties")
        if min_properties is not None and len(value_dict) < min_properties:
            raise InvalidSchema(f"object size mismatch at {path}")
        required = cast(list[str], schema.get("required", []))
        missing = [name for name in required if name not in value]
        if missing:
            raise InvalidSchema(f"missing {missing} at {path}")
        properties = cast(dict[str, dict[str, Any]], schema.get("properties", {}))
        if schema.get("additionalProperties") is False:
            unknown: set[str] = set(cast(dict[str, Any], value)).difference(properties)
            if unknown:
                raise InvalidSchema(f"unknown {sorted(unknown)} at {path}")
        for name, child in properties.items():
            if name in value:
                validate_schema(value[name], child, root, f"{path}.{name}")


def _assert_valid(value: dict[str, Any]) -> None:
    schema = _schema()
    validate_schema(value, schema, schema, "$instance")


UUIDS = {
    "dataset": "00000000-0000-0000-0000-000000000001",
    "case": "00000000-0000-0000-0000-000000000002",
    "suite": "00000000-0000-0000-0000-000000000003",
    "evaluator": "00000000-0000-0000-0000-000000000004",
    "score": "00000000-0000-0000-0000-000000000005",
    "request": "00000000-0000-0000-0000-000000000006",
    "job": "00000000-0000-0000-0000-000000000007",
    "result": "00000000-0000-0000-0000-000000000008",
    "attempt": "00000000-0000-0000-0000-000000000009",
    "artifact": "00000000-0000-0000-0000-000000000010",
    "artifact_ref": "00000000-0000-0000-0000-000000000011",
    "experiment": "00000000-0000-0000-0000-000000000012",
    "baseline": "00000000-0000-0000-0000-000000000013",
    "replay": "00000000-0000-0000-0000-000000000014",
}
DIGEST = "a" * 64
AT = "2026-10-18T12:00:00Z"
IDEMPOTENCY = {
    "score": "00000000-0000-0000-0000-000000000015",
    "request": "00000000-0000-0000-0000-000000000016",
    "result": "00000000-0000-0000-0000-000000000017",
    "artifact": "00000000-0000-0000-0000-000000000018",
    "baseline": "00000000-0000-0000-0000-000000000019",
    "replay": "00000000-0000-0000-0000-000000000020",
}


def _auth(classification: str = "metadata_only") -> dict[str, Any]:
    result: dict[str, Any] = {
        "captureClassification": classification,
        "authorizationPurpose": "synthetic_benchmark",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
    }
    if classification != "metadata_only":
        result.update({"authorizationRecordId": UUIDS["dataset"], "authorizedAt": AT})
    return result


def _provenance() -> dict[str, Any]:
    return {
        "createdByComponent": "aura.observatory.contracts",
        "createdByVersion": "1.0.0",
        "sourceDigest": DIGEST,
    }


def _limits() -> dict[str, Any]:
    return {
        "timeoutMs": 1000,
        "maxConcurrency": 1,
        "maxAttempts": 2,
        "maxCases": 10,
        "maxInputBytes": 1000,
        "maxOutputBytes": 1000,
        "maxTokens": 100,
    }


def _component() -> dict[str, Any]:
    return {
        "componentId": "aura.core.runner",
        "componentVersion": "1.0.0",
        "codeRevision": "commit-1",
        "imageDigest": DIGEST,
        "configurationRevision": "config-1",
    }


def _model() -> dict[str, Any]:
    return {"modelId": "qwen3-8b", "modelVersion": "1.0.0", "modelDigest": DIGEST}


def _reproducibility() -> dict[str, Any]:
    return {
        "seed": 7,
        "clockMode": "fixed",
        "timeZone": "UTC",
        "locale": "en-US",
        "fixtureRevision": "fixture-1",
        "promptRevision": "prompt-1",
        "policyRevision": "policy-1",
    }


def _execution_provenance() -> dict[str, Any]:
    return {
        "dataset": {"id": UUIDS["dataset"], "version": "1.0.0"},
        "suite": {"id": UUIDS["suite"], "version": "1.0.0"},
        "evaluators": [
            {
                "evaluatorId": UUIDS["evaluator"],
                "evaluatorVersion": "1.0.0",
                "kind": "deterministic",
            }
        ],
        "components": [_component()],
        "model": _model(),
        "deploymentRevision": "deployment-1",
        "reproducibility": _reproducibility(),
    }


def _resource_base(resource_type: str) -> dict[str, Any]:
    return {"schemaVersion": 1, "resourceType": resource_type}


def _resources() -> dict[str, dict[str, Any]]:
    dataset = _resource_base("dataset") | {
        "datasetId": UUIDS["dataset"],
        "datasetVersion": "1.0.0",
        "sourceKind": "synthetic",
        "scopes": ["public_project"],
        "captureAuthorization": _auth(),
        "caseIds": [UUIDS["case"]],
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    case = _resource_base("case") | {
        "caseId": UUIDS["case"],
        "caseVersion": "1.0.0",
        "dataset": {"id": UUIDS["dataset"], "version": "1.0.0"},
        "sourceKind": "synthetic",
        "scopes": ["public_project"],
        "captureAuthorization": _auth(),
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    suite = _resource_base("suite") | {
        "suiteId": UUIDS["suite"],
        "suiteVersion": "1.0.0",
        "dataset": {"id": UUIDS["dataset"], "version": "1.0.0"},
        "evaluators": [
            {
                "evaluatorId": UUIDS["evaluator"],
                "evaluatorVersion": "1.0.0",
                "kind": "deterministic",
            }
        ],
        "components": [_component()],
        "limits": _limits(),
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    evaluator = _resource_base("evaluator") | {
        "evaluatorId": UUIDS["evaluator"],
        "evaluatorVersion": "1.0.0",
        "kind": "deterministic",
        "metric": "exact_match",
        "componentIds": ["aura.core.runner"],
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    score = _resource_base("score") | {
        "scoreId": UUIDS["score"],
        "resultId": UUIDS["result"],
        "caseId": UUIDS["case"],
        "idempotencyKey": IDEMPOTENCY["score"],
        "captureAuthorization": _auth(),
        "evaluator": {
            "evaluatorId": UUIDS["evaluator"],
            "evaluatorVersion": "1.0.0",
            "kind": "deterministic",
        },
        "component": _component(),
        "metric": "exact_match",
        "evidenceStatus": "observed",
        "value": 1,
        "outcome": "pass",
        "uncertainty": {"method": "none", "lower": 1, "upper": 1},
        "limits": _limits(),
        "retentionMode": "manual_only",
        "provenance": _execution_provenance(),
        "createdAt": AT,
    }
    request = _resource_base("request") | {
        "requestId": UUIDS["request"],
        "jobId": UUIDS["job"],
        "requestVersion": "1.0.0",
        "idempotencyKey": IDEMPOTENCY["request"],
        "captureAuthorization": _auth(),
        "provenance": _execution_provenance(),
        "limits": _limits(),
        "deadlineAt": "2026-10-18T12:05:00Z",
        "retentionMode": "manual_only",
        "createdAt": AT,
    }
    result = _resource_base("result") | {
        "resultId": UUIDS["result"],
        "requestId": UUIDS["request"],
        "jobId": UUIDS["job"],
        "attemptId": UUIDS["attempt"],
        "idempotencyKey": IDEMPOTENCY["result"],
        "status": "completed",
        "captureAuthorization": _auth(),
        "provenance": _execution_provenance(),
        "limits": _limits(),
        "scoreIds": [UUIDS["score"]],
        "artifactIds": [],
        "retentionMode": "manual_only",
        "createdAt": AT,
        "completedAt": "2026-10-18T12:00:01Z",
    }
    artifact = _resource_base("artifact") | {
        "artifactId": UUIDS["artifact"],
        "artifactReferenceId": UUIDS["artifact_ref"],
        "requestId": UUIDS["request"],
        "resultId": UUIDS["result"],
        "idempotencyKey": IDEMPOTENCY["artifact"],
        "mediaClassification": "evaluation_input",
        "mediaType": "application/json",
        "byteCount": 1,
        "integrity": {"algorithm": "sha256", "digest": DIGEST},
        "captureAuthorization": _auth("full_content"),
        "limits": _limits(),
        "deletionScopes": ["job"],
        "retentionMode": "manual_only",
        "provenance": _execution_provenance(),
        "createdAt": AT,
    }
    variant = {
        "variantId": "variant-a",
        "components": [_component()],
        "model": _model(),
        "configurationRevision": "config-1",
        "promptRevision": "prompt-1",
        "policyRevision": "policy-1",
        "deploymentRevision": "deployment-1",
    }
    experiment = _resource_base("experiment") | {
        "experimentId": UUIDS["experiment"],
        "experimentVersion": "1.0.0",
        "suite": {"id": UUIDS["suite"], "version": "1.0.0"},
        "variants": [variant, {**variant, "variantId": "variant-b"}],
        "cohorts": [{"cohortId": "cohort-a", "caseIds": [UUIDS["case"]]}],
        "captureAuthorization": _auth(),
        "limits": _limits(),
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    baseline = _resource_base("baseline") | {
        "baselineId": UUIDS["baseline"],
        "baselineVersion": "1.0.0",
        "idempotencyKey": IDEMPOTENCY["baseline"],
        "state": "candidate",
        "suite": {"id": UUIDS["suite"], "version": "1.0.0"},
        "sourceResultIds": [UUIDS["result"]],
        "thresholds": [
            {
                "metric": "exact_match",
                "operator": "greater_than_or_equal",
                "value": 0.9,
                "minimumSampleSize": 1,
            }
        ],
        "captureAuthorization": _auth(),
        "limits": _limits(),
        "immutable": True,
        "promotionMode": "manual_owner_action",
        "retentionMode": "manual_only",
        "provenance": _execution_provenance(),
        "createdAt": AT,
    }
    replay = _resource_base("replay") | {
        "replayId": UUIDS["replay"],
        "replayVersion": "1.0.0",
        "sourceResultId": UUIDS["result"],
        "caseIds": [UUIDS["case"]],
        "captureAuthorization": _auth(),
        "limits": _limits(),
        "idempotencyKey": IDEMPOTENCY["replay"],
        "retentionMode": "manual_only",
        "provenance": _execution_provenance(),
        "createdAt": AT,
    }
    return {
        "dataset": dataset,
        "case": case,
        "suite": suite,
        "evaluator": evaluator,
        "score": score,
        "request": request,
        "result": result,
        "artifact": artifact,
        "experiment": experiment,
        "baseline": baseline,
        "replay": replay,
    }


def test_schema_meta_contract_is_versioned_closed_and_preserves_additive_resources() -> None:
    schema = _schema()
    assert schema["$schema"].endswith("/draft/2020-12/schema")
    assert schema["$id"].endswith("/contracts/evaluation/v1/evaluation.schema.json")
    refs = [entry["$ref"] for entry in schema["oneOf"]]
    original_refs = [
        f"#/$defs/{name.title()}"
        for name in (
            "dataset",
            "case",
            "suite",
            "evaluator",
            "score",
            "request",
            "result",
            "artifact",
            "experiment",
            "baseline",
            "replay",
        )
    ]
    additive_refs = {
        "#/$defs/JudgeArtifact",
        "#/$defs/LocalJudgeDefinition",
        "#/$defs/CalibrationDefinition",
        "#/$defs/CalibrationLabel",
        "#/$defs/HumanReview",
        "#/$defs/CalibrationResult",
        "#/$defs/JudgeQualification",
        "#/$defs/JudgeEnsemble",
        "#/$defs/RegressionDecision",
        "#/$defs/AttributionReport",
    }
    assert [reference for reference in refs if reference in original_refs] == original_refs
    assert set(refs) == set(original_refs) | additive_refs
    assert len(refs) == len(set(refs)) == len(original_refs) + len(additive_refs)
    for name in refs:
        definition = schema["$defs"][name.removeprefix("#/$defs/")]
        assert definition["type"] == "object"
        assert definition["additionalProperties"] is False
        assert set(definition["required"]).issubset(definition["properties"])


@pytest.mark.parametrize(
    "resource_type",
    [
        "dataset",
        "case",
        "suite",
        "evaluator",
        "score",
        "request",
        "result",
        "artifact",
        "experiment",
        "baseline",
        "replay",
    ],
)
def test_valid_representative_for_every_resource_kind(resource_type: str) -> None:
    _assert_valid(_resources()[resource_type])


def test_metadata_only_case_rejects_content_digest_and_artifact_references() -> None:
    case = _resources()["case"]
    for field, value in (
        ("content", {"inputText": "untrusted"}),
        ("contentDigest", DIGEST),
        ("inputArtifactIds", [UUIDS["artifact"]]),
    ):
        candidate = deepcopy(case)
        candidate[field] = value
        with pytest.raises(InvalidSchema):
            _assert_valid(candidate)


def test_capture_levels_require_explicit_authorization_and_content_shape() -> None:
    case = _resources()["case"]
    case["captureAuthorization"] = _auth("full_content")
    case["content"] = {"inputText": "treated as inert untrusted data"}
    _assert_valid(case)

    missing_record = deepcopy(case)
    del missing_record["captureAuthorization"]["authorizationRecordId"]
    with pytest.raises(InvalidSchema):
        _assert_valid(missing_record)

    hashed = deepcopy(case)
    hashed["captureAuthorization"] = _auth("hashed")
    hashed.pop("content")
    hashed["contentDigest"] = DIGEST
    _assert_valid(hashed)


def test_content_capable_case_requires_a_nonempty_opaque_artifact_reference() -> None:
    case = _resources()["case"]
    case["captureAuthorization"] = _auth("full_content")
    case["inputArtifactIds"] = [UUIDS["artifact"]]
    _assert_valid(case)
    empty = deepcopy(case)
    empty["inputArtifactIds"] = []
    with pytest.raises(InvalidSchema):
        _assert_valid(empty)


def test_artifact_capture_classification_controls_nonzero_content() -> None:
    artifact = _resources()["artifact"]
    artifact["captureAuthorization"] = _auth("full_content")
    artifact["mediaClassification"] = "evaluation_input"
    artifact["byteCount"] = 1
    _assert_valid(artifact)

    metadata = deepcopy(artifact)
    metadata["captureAuthorization"] = _auth()
    with pytest.raises(InvalidSchema):
        _assert_valid(metadata)

    hashed = deepcopy(artifact)
    hashed["captureAuthorization"] = _auth("hashed")
    with pytest.raises(InvalidSchema):
        _assert_valid(hashed)


def test_metadata_only_replay_rejects_artifact_references_and_content_replay_is_explicit() -> None:
    replay = _resources()["replay"]
    replay["captureAuthorization"] = _auth("full_content")
    replay["inputArtifactIds"] = [UUIDS["artifact"]]
    _assert_valid(replay)

    metadata = _resources()["replay"]
    metadata["inputArtifactIds"] = [UUIDS["artifact"]]
    with pytest.raises(InvalidSchema):
        _assert_valid(metadata)


@pytest.mark.parametrize(
    "field",
    [
        "ownerIdentity",
        "credential",
        "bearerToken",
        "endpoint",
        "objectKey",
        "filename",
        "arbitraryAttributes",
        "executablePath",
        "permissions",
    ],
)
def test_unknown_authority_and_private_fields_are_rejected(field: str) -> None:
    candidate = _resources()["request"]
    candidate[field] = "must not become a contract authority"
    with pytest.raises(InvalidSchema):
        _assert_valid(candidate)


def test_missing_required_identity_provenance_limits_and_retention_fields_fail() -> None:
    request = _resources()["request"]
    for field in ("requestId", "idempotencyKey", "provenance", "limits", "retentionMode"):
        candidate = deepcopy(request)
        del candidate[field]
        with pytest.raises(InvalidSchema):
            _assert_valid(candidate)


def test_evaluator_and_score_never_infer_missing_evidence_and_local_judge_is_versioned() -> None:
    evaluator = _resources()["evaluator"]
    evaluator.update(
        {"kind": "local_judge", "rubricVersion": "rubric-1", "calibrationVersion": "calibration-1"}
    )
    _assert_valid(evaluator)
    score = _resources()["score"]
    score["evaluator"]["kind"] = "local_judge"
    score.update({"rubricVersion": "rubric-1", "calibrationVersion": "calibration-1"})
    _assert_valid(score)
    unavailable = deepcopy(_resources()["score"])
    unavailable["evidenceStatus"] = "not_observed"
    unavailable.pop("value")
    unavailable.pop("outcome")
    _assert_valid(unavailable)


@pytest.mark.parametrize("evidence_status", ["not_observed", "not_applicable"])
@pytest.mark.parametrize("field", ["value", "outcome"])
def test_unavailable_score_evidence_rejects_each_observed_field(
    evidence_status: str, field: str
) -> None:
    score = _resources()["score"]
    score["evidenceStatus"] = evidence_status
    score.pop("value")
    score.pop("outcome")
    score[field] = 0 if field == "value" else "inconclusive"
    with pytest.raises(InvalidSchema):
        _assert_valid(score)


def test_exact_and_one_past_bounds_are_enforced_for_resource_scalars_and_collections() -> None:
    dataset = _resources()["dataset"]
    dataset["datasetVersion"] = "v" * 64
    _assert_valid(dataset)
    dataset["datasetVersion"] = "v" * 65
    with pytest.raises(InvalidSchema):
        _assert_valid(dataset)

    limits = _limits()
    request = _resources()["request"]
    request["limits"] = {**limits, "timeoutMs": 86_400_000}
    _assert_valid(request)
    request["limits"] = {**limits, "timeoutMs": 86_400_001}
    with pytest.raises(InvalidSchema):
        _assert_valid(request)

    case = _resources()["case"]
    case["captureAuthorization"] = _auth("full_content")
    case["inputArtifactIds"] = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 33)]
    _assert_valid(case)
    case["inputArtifactIds"].append("00000000-0000-0000-0000-000000000033")
    with pytest.raises(InvalidSchema):
        _assert_valid(case)


def test_experiment_variant_and_numeric_bounds_reject_one_past_limits() -> None:
    experiment = _resources()["experiment"]
    variant = experiment["variants"][0]
    experiment["variants"] = [{**variant, "variantId": f"variant-{index}"} for index in range(16)]
    _assert_valid(experiment)
    experiment["variants"].append({**variant, "variantId": "variant-16"})
    with pytest.raises(InvalidSchema):
        _assert_valid(experiment)

    score = _resources()["score"]
    score["uncertainty"] = {
        "method": "none",
        "lower": 1_000_000_000_000_000,
        "upper": 1_000_000_000_000_000,
    }
    score["value"] = 1_000_000_000_000_000
    _assert_valid(score)
    score["value"] = 1_000_000_000_000_001
    with pytest.raises(InvalidSchema):
        _assert_valid(score)


def test_exact_duplicate_experiment_variants_are_rejected() -> None:
    experiment = _resources()["experiment"]
    duplicate = deepcopy(experiment["variants"][0])
    experiment["variants"] = [duplicate, deepcopy(duplicate)]
    with pytest.raises(InvalidSchema):
        _assert_valid(experiment)


def test_untrusted_content_object_requires_at_least_one_bounded_member() -> None:
    case = _resources()["case"]
    case["captureAuthorization"] = _auth("full_content")
    case["content"] = {}
    with pytest.raises(InvalidSchema):
        _assert_valid(case)


@pytest.mark.parametrize(
    "candidate",
    ["Bearer abc.def.ghi", "credential-secret", "free form idempotency", "owner@example.invalid"],
)
def test_idempotency_keys_are_uuid_only_and_reject_secrets_tokens_and_free_text(
    candidate: str,
) -> None:
    request = _resources()["request"]
    request["idempotencyKey"] = candidate
    with pytest.raises(InvalidSchema):
        _assert_valid(request)


def test_registered_failure_taxonomy_is_finite_and_provider_free() -> None:
    failure_schema = json.loads(FAILURE_CODES_PATH.read_text(encoding="utf-8"))
    categories = failure_schema["enum"]
    assert categories == [
        "REQUEST_INVALID",
        "DEADLINE_EXCEEDED",
        "RESOURCE_LIMIT_EXCEEDED",
        "DEPENDENCY_UNAVAILABLE",
        "EXECUTION_FAILED",
        "RESULT_VALIDATION_FAILED",
        "STORAGE_FAILED",
        "INTERNAL_FAILURE",
    ]
    for category in categories:
        result = _resources()["result"]
        result["status"] = "failed"
        result["failureCode"] = category
        _assert_valid(result)
    for invalid in (
        "PROVIDER_TIMEOUT",
        "credential=secret",
        "exception ValueError",
        "Bearer token",
    ):
        result = _resources()["result"]
        result["status"] = "failed"
        result["failureCode"] = invalid
        with pytest.raises(InvalidSchema):
            _assert_valid(result)


def test_baseline_requires_manual_promotion_and_is_immutable() -> None:
    baseline = _resources()["baseline"]
    promoted = deepcopy(baseline)
    promoted.update(
        {"state": "promoted", "manualPromotionRecordId": UUIDS["baseline"], "promotedAt": AT}
    )
    _assert_valid(promoted)
    for field in ("manualPromotionRecordId", "promotedAt"):
        candidate = deepcopy(promoted)
        del candidate[field]
        with pytest.raises(InvalidSchema):
            _assert_valid(candidate)
    automatic = deepcopy(baseline)
    automatic["promotionMode"] = "automatic"
    with pytest.raises(InvalidSchema):
        _assert_valid(automatic)
