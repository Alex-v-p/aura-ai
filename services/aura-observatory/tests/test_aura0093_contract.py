"""AURA-0093 contract closure and compatibility checks.

The v1 additions are deliberately additive, but the new nested expectation
objects must still be closed and bounded.  These tests keep the contract
runner independent from the service domain decoder so a permissive decoder
cannot silently make an unsafe contract valid.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from aura_observatory.evaluation_definitions import DefinitionValidationError
from test_aura0089_contract import InvalidSchema, _assert_valid, _resources

DIGEST = "a" * 64
IDS = {
    "dataset": "00000000-0000-0000-0000-000000000001",
    "case": "00000000-0000-0000-0000-000000000002",
    "suite": "00000000-0000-0000-0000-000000000003",
    "evaluator": "00000000-0000-0000-0000-000000000004",
}


def _expectations() -> dict[str, Any]:
    return {
        "exact": {
            "fields": [
                {"fieldId": "answer", "expectedDigest": DIGEST, "normalization": "none"}
            ]
        },
        "schema": {
            "schemaDigest": DIGEST,
            "dialect": "typed_projection_v1",
            "additionalFieldsPolicy": "reject",
            "requiredFieldIds": ["answer"],
        },
        "retrieval": {
            "rankedEvidence": [
                {
                    "evidenceId": "fixture-1",
                    "relevanceGrade": 3,
                    "citationRequired": True,
                    "freshness": "current",
                }
            ],
            "cutoffs": [1, 3],
            "irrelevantEvidenceIds": ["fixture-noise"],
        },
        "context": {
            "requiredEvidenceIds": ["fixture-1"],
            "prohibitedEvidenceIds": ["fixture-noise"],
            "maxSelectedTokens": 256,
            "truncation": "allowed",
            "conflictHandling": "preserve_authority",
            "injectionHandling": "ignore_untrusted_instructions",
        },
        "tool": {
            "expectedCalls": [
                {
                    "callId": "lookup-1",
                    "toolId": "lookup",
                    "argumentSchemaDigest": DIGEST,
                    "expectedArgumentDigest": DIGEST,
                    "argumentMatch": "exact_digest",
                    "outcome": "completed",
                    "sideEffect": "none",
                    "idempotency": "required",
                }
            ],
            "prohibitedToolIds": ["write"],
            "maxRedundantCalls": 0,
        },
        "policy": {
            "decision": "allow",
            "confirmation": "not_required",
            "sensitiveDataHandling": "not_applicable",
            "consequentialAction": "no_action",
            "policyRevision": "policy-1",
        },
        "groundedness": {
            "claims": [
                {
                    "claimId": "claim-1",
                    "support": "supported",
                    "supportEvidenceIds": ["fixture-1"],
                    "citation": "required",
                }
            ],
            "unsupportedClaimPolicy": "fail",
        },
        "response": {
            "expectedDigest": DIGEST,
            "normalization": "trimmed_text",
            "requiredInstructionIds": ["instruction-1"],
            "requiredStyleIds": ["style-1"],
            "refusal": "prohibited",
            "properties": ["non_empty", "valid_utf8"],
            "maxBytes": 2048,
            "maxTokens": 256,
        },
        "resource": {
            "maxLatencyMs": 1000,
            "maxQueueMs": 500,
            "maxInputBytes": 2048,
            "maxOutputBytes": 4096,
            "maxTokens": 256,
            "maxMemoryBytes": 1_048_576,
            "maxProviderCalls": 2,
            "maxConcurrency": 1,
            "maxOperations": 4,
            "minThroughputPerSecond": 0.5,
        },
    }


def _threshold() -> dict[str, Any]:
    return {
        "evaluator": {
            "evaluatorId": IDS["evaluator"],
            "evaluatorVersion": "1.0.0",
            "kind": "deterministic",
        },
        "metric": "evaluation.exact.ratio",
        "aggregation": "minimum",
        "operator": "greater_than_or_equal",
        "value": 0.95,
        "unit": "ratio",
        "minimumSampleSize": 2,
        "missingEvidencePolicy": "inconclusive",
    }


def test_all_new_expectation_families_and_manual_threshold_are_valid_v1() -> None:
    resources = _resources()
    resources["case"]["expectations"] = _expectations()
    resources["suite"]["manualThresholds"] = [_threshold()]

    _assert_valid(resources["case"])
    _assert_valid(resources["suite"])


def test_injection_handling_is_a_closed_enum() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    case = _resources()["case"] | {"expectations": _expectations()}
    case["expectations"]["context"]["injectionHandling"] = "allow_anything"
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(case)


def test_supported_claim_requires_support_evidence_ids() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    case = _resources()["case"] | {"expectations": _expectations()}
    case["expectations"]["groundedness"]["claims"][0]["supportEvidenceIds"] = []
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(case)


def test_response_normalization_is_conditional_on_expected_digest() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    case = _resources()["case"] | {"expectations": _expectations()}
    case["expectations"]["response"].pop("expectedDigest")
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(case)


@pytest.mark.parametrize(
    ("source_kind", "capture_classification", "purpose"),
    [
        ("synthetic", "full_content", "synthetic_benchmark"),
        ("synthetic", "metadata_only", "owner_supplied_evaluation"),
        ("owner_supplied", "metadata_only", "owner_supplied_evaluation"),
        ("owner_supplied", "full_content", "synthetic_benchmark"),
        ("captured", "metadata_only", "controlled_replay"),
        ("captured", "hashed", "owner_supplied_evaluation"),
    ],
)
def test_source_capture_classification_and_purpose_are_cross_bound(
    source_kind: str, capture_classification: str, purpose: str
) -> None:
    dataset = deepcopy(_resources()["dataset"])
    dataset["sourceKind"] = source_kind
    authorization: dict[str, Any] = {
        "captureClassification": capture_classification,
        "authorizationPurpose": purpose,
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
    }
    if capture_classification != "metadata_only":
        authorization.update(
            {
                "authorizationRecordId": IDS["dataset"],
                "authorizedAt": "2026-10-18T12:00:00Z",
            }
        )
    dataset["captureAuthorization"] = authorization
    from aura_observatory.evaluation_definitions import DatasetDefinition

    with pytest.raises(DefinitionValidationError):
        DatasetDefinition.from_payload(dataset)


@pytest.mark.parametrize(
    ("resource_type", "field", "value"),
    [
        ("case", "expectations", {"exact": {"fields": []}}),
        (
            "case",
            "expectations",
            {
                "exact": {
                    "fields": [
                        {
                            "fieldId": "answer",
                            "expectedDigest": DIGEST,
                            "normalization": "none",
                            "endpoint": "x",
                        }
                    ]
                }
            },
        ),
        (
            "case",
            "expectations",
            {
                "response": {
                    "properties": ["non_empty"],
                    "maxBytes": 1,
                    "maxTokens": 1,
                    "unknown": True,
                }
            },
        ),
        ("suite", "manualThresholds", [_threshold() | {"secret": "x"}]),
    ],
)
def test_new_nested_contracts_reject_unknown_or_invalid_fields(
    resource_type: str, field: str, value: Any
) -> None:
    resource = deepcopy(_resources()[resource_type])
    resource[field] = value
    with pytest.raises(InvalidSchema):
        _assert_valid(resource)


def test_new_contracts_remain_backward_compatible_for_all_aura0089_resources() -> None:
    for resource in _resources().values():
        _assert_valid(resource)


def test_domain_decoder_rejects_non_finite_and_private_nested_values() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    payload = _resources()["case"] | {
        "expectations": _expectations(),
    }
    payload["expectations"]["resource"]["maxLatencyMs"] = float("nan")
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(payload)

    private = _resources()["case"] | {
        "expectations": {
            "exact": {
                "fields": [
                    {
                        "fieldId": "owner.identity",
                        "expectedDigest": DIGEST,
                        "normalization": "none",
                    }
                ]
            }
        },
    }
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(private)


@pytest.mark.parametrize(
    "position",
    [
        "exact.fieldId",
        "retrieval.evidenceId",
        "retrieval.irrelevantEvidenceIds",
        "context.requiredEvidenceIds",
        "context.prohibitedEvidenceIds",
        "tool.callId",
        "tool.toolId",
        "tool.prohibitedToolIds",
        "policy.policyRevision",
        "groundedness.claimId",
        "groundedness.supportEvidenceIds",
        "response.requiredInstructionIds",
        "response.requiredStyleIds",
    ],
)
def test_every_new_identifier_position_rejects_sensitive_segments(position: str) -> None:
    case = _resources()["case"]
    case["expectations"] = _expectations()
    family, field = position.split(".", 1)
    target = case["expectations"][family]
    if field in {"fieldId", "evidenceId", "callId", "toolId", "claimId", "policyRevision"}:
        if field == "fieldId":
            target["fields"][0][field] = "owner.identity"
        elif field == "evidenceId":
            target["rankedEvidence"][0][field] = "endpoint"
        elif field == "callId":
            target["expectedCalls"][0][field] = "credential"
        elif field == "toolId":
            target["expectedCalls"][0][field] = "token"
        elif field == "claimId":
            target["claims"][0][field] = "private.path"
        else:
            target[field] = "owner.subject"
    elif field in {
        "irrelevantEvidenceIds",
        "requiredEvidenceIds",
        "prohibitedEvidenceIds",
        "prohibitedToolIds",
        "supportEvidenceIds",
        "requiredInstructionIds",
        "requiredStyleIds",
    }:
        if field == "supportEvidenceIds":
            target["claims"][0][field][0] = "url"
        else:
            target[field][0] = "url"
    from aura_observatory.evaluation_definitions import CaseDefinition

    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(case)


def test_manual_threshold_metric_rejects_sensitive_identifier() -> None:
    suite = _resources()["suite"]
    suite["manualThresholds"] = [_threshold()]
    suite["manualThresholds"][0]["metric"] = "owner.identity"
    from aura_observatory.evaluation_definitions import SuiteDefinition

    with pytest.raises(DefinitionValidationError):
        SuiteDefinition.from_payload(suite)


def test_domain_decoder_round_trips_every_new_expectation_family_without_content() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    parsed = CaseDefinition.from_payload(_resources()["case"] | {"expectations": _expectations()})
    payload = parsed.to_payload()
    assert payload["expectations"] == _expectations()
    assert payload["createdAt"].endswith("Z")
    assert "content" not in payload
