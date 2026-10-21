"""AURA-0094 contract closure, privacy, and forward-migration checks."""
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnusedFunction=false

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_aura0089_contract import InvalidSchema, _schema, validate_schema

ROOT = Path(__file__).parents[3]
IDS = {
    "judge": "20000000-0000-0000-0000-000000000001",
    "evaluator": "20000000-0000-0000-0000-000000000002",
    "calibration": "20000000-0000-0000-0000-000000000003",
    "artifact": "20000000-0000-0000-0000-000000000004",
    "authorization": "20000000-0000-0000-0000-000000000005",
    "suite": "20000000-0000-0000-0000-000000000006",
    "dataset": "20000000-0000-0000-0000-000000000007",
    "case": "20000000-0000-0000-0000-000000000008",
    "result": "20000000-0000-0000-0000-000000000009",
    "score": "20000000-0000-0000-0000-000000000010",
}
DIGEST = "b" * 64
AT = "2026-10-19T00:00:00Z"


def _provenance() -> dict[str, Any]:
    return {
        "createdByComponent": "aura.observatory.tests",
        "createdByVersion": "1.0.0",
        "sourceDigest": DIGEST,
    }


def _judge_definition() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "resourceType": "local_judge_definition",
        "judgeDefinitionId": IDS["judge"],
        "judgeDefinitionVersion": "1.0.0",
        "evaluator": {
            "evaluatorId": IDS["evaluator"],
            "evaluatorVersion": "1.0.0",
            "kind": "local_judge",
        },
        "model": {
            "provider": "local_ollama",
            "modelName": "llama3.2:3b",
            "modelVersion": "3.2.0",
            "modelDigest": DIGEST,
        },
        "prompt": {
            "artifactId": IDS["artifact"],
            "artifactVersion": "1.0.0",
            "purpose": "judge_prompt",
            "digest": DIGEST,
        },
        "rubric": {
            "artifactId": IDS["artifact"],
            "artifactVersion": "1.0.0",
            "purpose": "judge_rubric",
            "digest": DIGEST,
        },
        "calibration": {"calibrationId": IDS["calibration"], "calibrationVersion": "1.0.0"},
        "seed": 0,
        "budget": {
            "timeoutMs": 120000,
            "maxInputBytes": 1048576,
            "maxOutputBytes": 8192,
            "maxOutputTokens": 2048,
            "maxContextTokens": 4096,
            "maxConcurrency": 1,
            "temperature": 0,
        },
        "outputContract": "local_judge_score_v1",
        "defaultDisposition": "advisory_only",
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }


def _ref(identifier: str) -> dict[str, str]:
    return {"id": identifier, "version": "1.0.0"}


def _eval_ref(kind: str = "local_judge") -> dict[str, str]:
    return {"evaluatorId": IDS["evaluator"], "evaluatorVersion": "1.0.0", "kind": kind}


def _judge_ref() -> dict[str, str]:
    return {"judgeDefinitionId": IDS["judge"], "judgeDefinitionVersion": "1.0.0"}


def _cal_ref() -> dict[str, str]:
    return {"calibrationId": IDS["calibration"], "calibrationVersion": "1.0.0"}


def _local_model() -> dict[str, str]:
    return {
        "provider": "local_ollama",
        "modelName": "llama3.2:3b",
        "modelVersion": "3.2.0",
        "modelDigest": DIGEST,
    }


def _judge_binding(purpose: str) -> dict[str, str]:
    return {
        "artifactId": IDS["artifact"],
        "artifactVersion": "1.0.0",
        "purpose": purpose,
        "digest": DIGEST,
    }


def _budget() -> dict[str, int]:
    return {
        "timeoutMs": 120000,
        "maxInputBytes": 1048576,
        "maxOutputBytes": 8192,
        "maxOutputTokens": 2048,
        "maxContextTokens": 4096,
        "maxConcurrency": 1,
        "temperature": 0,
    }


def _capture_auth() -> dict[str, str]:
    return {
        "captureClassification": "full_content",
        "authorizationPurpose": "owner_supplied_evaluation",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": IDS["authorization"],
        "authorizedAt": AT,
    }


def _new_resources() -> dict[str, dict[str, Any]]:
    artifact = {
        "schemaVersion": 1,
        "resourceType": "judge_artifact",
        "artifactId": IDS["artifact"],
        "artifactVersion": "1.0.0",
        "artifactReferenceId": IDS["artifact"],
        "purpose": "judge_prompt",
        "digest": DIGEST,
        "byteCount": 4,
        "mediaType": "text/plain",
        "captureAuthorization": _capture_auth(),
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    calibration = {
        "schemaVersion": 1,
        "resourceType": "calibration_definition",
        "calibrationId": IDS["calibration"],
        "calibrationVersion": "1.0.0",
        "dataset": _ref(IDS["dataset"]),
        "suite": _ref(IDS["suite"]),
        "evaluator": _eval_ref(),
        "judgeDefinition": _judge_ref(),
        "thresholds": {
            "minimumSampleCount": 20,
            "agreementTolerance": 0.1,
            "minimumAgreementRate": 0.8,
            "maximumMeanAbsoluteError": 0.15,
            "maximumDisagreementRate": 0.2,
        },
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    label = {
        "schemaVersion": 1,
        "resourceType": "calibration_label",
        "labelId": IDS["case"],
        "labelVersion": "1.0.0",
        "idempotencyKey": IDS["score"],
        "calibration": _cal_ref(),
        "caseId": IDS["case"],
        "resultId": IDS["result"],
        "referenceScore": 0.9,
        "referenceOutcome": "pass",
        "referenceLabel": "accepted",
        "authorizationRecordId": IDS["authorization"],
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    review = {
        "schemaVersion": 1,
        "resourceType": "human_review",
        "reviewId": IDS["score"],
        "reviewVersion": "1.0.0",
        "idempotencyKey": IDS["case"],
        "calibration": _cal_ref(),
        "caseId": IDS["case"],
        "resultId": IDS["result"],
        "scoreId": IDS["score"],
        "reviewScore": 0.9,
        "reviewOutcome": "pass",
        "reviewLabel": "accepted",
        "disagreement": "none",
        "authorizationRecordId": IDS["authorization"],
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    result = {
        "schemaVersion": 1,
        "resourceType": "calibration_result",
        "calibrationResultId": IDS["result"],
        "calibration": _cal_ref(),
        "judgeDefinition": _judge_ref(),
        "model": _local_model(),
        "prompt": _judge_binding("judge_prompt"),
        "rubric": _judge_binding("judge_rubric"),
        "seed": 0,
        "budget": _budget(),
        "sampleCount": 20,
        "agreementTolerance": 0.1,
        "agreementRate": 1.0,
        "meanAbsoluteError": 0.0,
        "disagreementRate": 0.0,
        "eligibility": "eligible",
        "scoreDisposition": "advisory_only",
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    qualification = {
        "schemaVersion": 1,
        "resourceType": "judge_qualification",
        "qualificationId": IDS["score"],
        "qualificationVersion": "1.0.0",
        "idempotencyKey": IDS["case"],
        "state": "qualified",
        "actionMode": "manual_owner_action",
        "dataset": _ref(IDS["dataset"]),
        "suite": _ref(IDS["suite"]),
        "evaluator": _eval_ref(),
        "judgeDefinition": _judge_ref(),
        "model": _local_model(),
        "prompt": _judge_binding("judge_prompt"),
        "rubric": _judge_binding("judge_rubric"),
        "calibration": _cal_ref(),
        "calibrationResultId": IDS["result"],
        "calibrationEligibility": "eligible",
        "authorizationRecordId": IDS["authorization"],
        "scoreDisposition": "calibrated_enabled",
        "qualifiedAt": AT,
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    ensemble = {
        "schemaVersion": 1,
        "resourceType": "judge_ensemble",
        "ensembleId": IDS["score"],
        "ensembleVersion": "1.0.0",
        "evaluator": _eval_ref(),
        "members": [
            {
                "memberId": "member-a",
                "kind": "local_judge",
                "evaluator": _eval_ref(),
                "judgeDefinition": _judge_ref(),
                "qualification": {"qualificationId": IDS["score"], "qualificationVersion": "1.0.0"},
                "weightPpm": 500000,
            },
            {
                "memberId": "member-b",
                "kind": "local_judge",
                "evaluator": _eval_ref(),
                "judgeDefinition": _judge_ref(),
                "qualification": {"qualificationId": IDS["score"], "qualificationVersion": "1.0.0"},
                "weightPpm": 500000,
            },
        ],
        "combination": "weighted_mean_ppm",
        "missingMemberDisposition": "advisory_only",
        "inconclusiveMemberDisposition": "advisory_only",
        "admissionRule": "all_local_judges_exactly_qualified",
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }
    return {
        "judge_artifact": artifact,
        "calibration_definition": calibration,
        "calibration_label": label,
        "human_review": review,
        "calibration_result": result,
        "judge_qualification": qualification,
        "judge_ensemble": ensemble,
    }


def test_local_judge_contract_is_closed_and_advisory() -> None:
    schema = _schema()
    # The root discriminator must expose the newly-added closed resources.
    root_refs = {item["$ref"] for item in schema["oneOf"]}
    assert "#/$defs/LocalJudgeDefinition" in root_refs
    assert "#/$defs/CalibrationResult" in root_refs
    assert "#/$defs/HumanReview" in root_refs
    assert "#/$defs/JudgeEnsemble" in root_refs
    value = _judge_definition()
    assert value["evaluator"]["evaluatorId"] != value["judgeDefinitionId"]
    assert value["calibration"]["calibrationId"] == IDS["calibration"]
    validate_schema(value, schema, schema, "$instance")


def test_all_evaluation_v1_additive_resources_dispatch_and_remain_closed() -> None:
    schema = _schema()
    for value in _new_resources().values():
        validate_schema(value, schema, schema, "$instance")
    metadata = {
        "judgeDefinition": _judge_ref(),
        "model": _local_model(),
        "prompt": _judge_binding("judge_prompt"),
        "rubric": _judge_binding("judge_rubric"),
        "calibration": _cal_ref(),
        "seed": 0,
        "budget": _budget(),
        "scoreDisposition": "advisory_only",
        "disagreement": "none",
    }
    validate_schema(metadata, schema["$defs"]["LocalJudgeScoreMetadata"], schema, "$instance")
    for _name, value in _new_resources().items():
        value["unexpected"] = "private"
        with pytest.raises(InvalidSchema, match="unknown|oneOf"):
            validate_schema(value, schema, schema, "$instance")


def test_additive_resources_reject_every_required_null_and_missing_field() -> None:
    """Required fields are closed at the transport boundary, including null."""

    schema = _schema()
    resources = _new_resources()
    by_type = {value["resourceType"]: value for value in resources.values()}
    for _definition_name, definition in schema["$defs"].items():
        resource_type = definition.get("properties", {}).get("resourceType", {}).get("const")
        if resource_type not in by_type:
            continue
        value = by_type[resource_type]
        for required in definition.get("required", []):
            missing = deepcopy(value)
            missing.pop(required, None)
            with pytest.raises(InvalidSchema):
                validate_schema(missing, schema, schema, "$instance")
            null_value = deepcopy(value)
            null_value[required] = None
            with pytest.raises(InvalidSchema):
                validate_schema(null_value, schema, schema, "$instance")


@pytest.mark.parametrize(
    ("resource", "field", "invalid"),
    [
        ("judge_artifact", "purpose", "cloud_prompt"),
        ("calibration_label", "referenceOutcome", "unknown"),
        ("human_review", "disagreement", "unknown"),
        ("calibration_result", "eligibility", "unknown"),
        ("calibration_result", "scoreDisposition", "enabled_without_qualification"),
        ("judge_qualification", "state", "active"),
        ("judge_qualification", "actionMode", "automatic"),
        ("judge_ensemble", "combination", "unweighted"),
        ("judge_ensemble", "missingMemberDisposition", "accepted"),
    ],
)
def test_additive_resources_reject_invalid_enum_values(
    resource: str, field: str, invalid: str
) -> None:
    value = deepcopy(_new_resources()[resource])
    value[field] = invalid
    with pytest.raises(InvalidSchema):
        validate_schema(value, _schema(), _schema(), "$instance")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["model"].update(modelDigest="not-a-digest"),
        lambda value: value["model"].update(provider="cloud"),
        lambda value: value["budget"].update(temperature=0.1),
        lambda value: value.update(endpoint="http://cloud.invalid"),
        lambda value: value.update(credentials="secret"),
        lambda value: value.update(ownerIdentity="owner@example.invalid"),
    ],
)
def test_local_judge_contract_rejects_digest_provider_and_private_fields(mutation: Any) -> None:
    value = _judge_definition()
    mutation(value)
    with pytest.raises(InvalidSchema):
        validate_schema(value, _schema(), _schema(), "$instance")


@pytest.mark.parametrize(
    ("resource", "mutation"),
    [
        ("judge_artifact", lambda value: value.update(content="raw prompt")),
        ("calibration_definition", lambda value: value.update(endpoint="http://cloud.invalid")),
        ("calibration_label", lambda value: value.update(comment="owner secret")),
        ("human_review", lambda value: value.update(ownerIdentity="owner@example.invalid")),
        ("calibration_result", lambda value: value.update(credentials="secret")),
        ("judge_qualification", lambda value: value.update(rawContent="private")),
        ("judge_ensemble", lambda value: value["members"][0].pop("weightPpm")),
        ("judge_ensemble", lambda value: value["members"][0].update(weightPpm=0.5)),
    ],
)
def test_each_additive_resource_rejects_content_provider_and_incomplete_member_fields(
    resource: str, mutation: Any
) -> None:
    value = deepcopy(_new_resources()[resource])
    mutation(value)
    with pytest.raises(InvalidSchema):
        validate_schema(value, _schema(), _schema(), "$instance")


@pytest.mark.parametrize(
    ("resource", "mutation"),
    [
        (resource, lambda value, key=key: value.update(**{key: "private"}))
        for resource in _new_resources()
        for key in ("endpoint", "credential", "ownerIdentity", "rawContent")
    ],
)
def test_every_additive_resource_rejects_each_private_boundary(
    resource: str, mutation: Any
) -> None:
    value = deepcopy(_new_resources()[resource])
    mutation(value)
    with pytest.raises(InvalidSchema):
        validate_schema(value, _schema(), _schema(), "$instance")


@pytest.mark.parametrize(
    ("resource", "mutation"),
    [
        ("judge_artifact", lambda value: value.update(digest="0", byteCount=0)),
        ("judge_artifact", lambda value: value.update(byteCount=1_048_577)),
        ("calibration_result", lambda value: value["budget"].update(timeoutMs=99)),
        ("calibration_result", lambda value: value["budget"].update(maxOutputTokens=2049)),
        (
            "calibration_definition",
            lambda value: value["thresholds"].update(agreementTolerance=1.1),
        ),
        (
            "calibration_definition",
            lambda value: value["thresholds"].update(maximumDisagreementRate=-0.1),
        ),
        (
            "judge_qualification",
            lambda value: value.update(state="qualified", scoreDisposition="advisory_only"),
        ),
        ("judge_qualification", lambda value: value.pop("qualifiedAt")),
        ("judge_ensemble", lambda value: value["members"][0].update(weightPpm=0)),
        ("judge_ensemble", lambda value: value["members"][0].update(weightPpm=1_000_001)),
        ("judge_ensemble", lambda value: value["members"][0].pop("qualification")),
    ],
)
def test_additive_resources_reject_boundary_conditionals_and_weights(
    resource: str, mutation: Any
) -> None:
    value = deepcopy(_new_resources()[resource])
    mutation(value)
    with pytest.raises(InvalidSchema):
        validate_schema(value, _schema(), _schema(), "$instance")


def test_migration_is_forward_only_content_free_and_rerunnable() -> None:
    migration = (
        ROOT
        / (
            "services/aura-observatory/migrations/postgresql/versions/"
            "0010_local_judges_calibration.sql"
        )
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS" in migration
    assert "CREATE INDEX IF NOT EXISTS" in migration
    assert "observatory_local_judge_definitions" in migration
    assert "observatory_local_judge_qualifications" in migration
    assert "observatory_local_judge_audits" in migration
    assert "owner_identity" not in migration.casefold()
    assert "prompt TEXT" not in migration
    assert "response TEXT" not in migration
    assert "endpoint" not in migration.casefold()
    versions = sorted(
        int(path.name[:4])
        for path in (ROOT / "services/aura-observatory/migrations/postgresql/versions").glob(
            "*.sql"
        )
    )
    assert versions[-1] == 13
    assert versions.count(10) == 1
    assert (
        ROOT
        / "services/aura-observatory/migrations/postgresql/versions/"
        "0010_local_judges_calibration.sql"
    ).exists()
