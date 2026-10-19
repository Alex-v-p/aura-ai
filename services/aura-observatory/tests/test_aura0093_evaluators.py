"""AURA-0093 deterministic evaluator families and score privacy."""
# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

from datetime import UTC, datetime
from math import inf, nan
from typing import Any

import pytest
from aura_observatory.evaluation_definitions import DefinitionValidationError
from aura_observatory.evaluation_evaluators import (
    BUILTIN_EVALUATOR_DEFINITIONS,
    ClaimObservation,
    DeterministicEvaluatorRegistry,
    EvidenceStatus,
    PolicyObservation,
    ResourceProjection,
    ResponseProjection,
    RetrievedEvidence,
    ScoreContext,
    ScoreOutcome,
    ToolObservation,
    TypedReplayEvidence,
    to_evaluation_score,
)
from test_aura0093_contract import _expectations

DIGEST = "a" * 64
UUIDS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 8)]


def _evidence() -> TypedReplayEvidence:
    return TypedReplayEvidence(
        fields={"answer": DIGEST},
        schema_digest=DIGEST,
        schema_fields=frozenset({"answer"}),
        retrieved=(RetrievedEvidence("fixture-1", 1, 3, True, True),),
        selected_evidence_ids=frozenset({"fixture-1"}),
        selected_tokens=100,
        injection_resisted=True,
        truncated=True,
        conflict_detected=False,
        authority_preserved=True,
        tool_calls=(
            ToolObservation("lookup-1", "lookup", DIGEST, DIGEST, "success", "none", True),
        ),
        policy=PolicyObservation(
            "allow", "not_required", "not_applicable", "no_action", "policy-1"
        ),
        claims=(ClaimObservation("claim-1", "supported", ("fixture-1",), True),),
        response=ResponseProjection(
            response_digest=DIGEST,
            byte_count=100,
            token_count=50,
            properties=frozenset({"non_empty", "valid_utf8"}),
            refusal=False,
            instruction_ids=frozenset({"instruction-1"}),
            style_ids=frozenset({"style-1"}),
        ),
        resources=ResourceProjection(
            {
                "latency_ms": 100,
                "queue_ms": 20,
                "input_bytes": 100,
                "output_bytes": 100,
                "tokens": 50,
                "memory_bytes": 10_000,
                "provider_calls": 1,
                "concurrency": 1,
                "operations": 1,
                "throughput_per_second": 1.0,
            }
        ),
    )


def test_all_nine_families_pass_with_typed_content_free_evidence() -> None:
    registry = DeterministicEvaluatorRegistry()
    measurements = registry.evaluate_case(_expectations(), _evidence())

    assert len(measurements) == 9
    assert {measurement.metric for measurement in measurements} == {
        f"evaluation.{family}.ratio" for family in _expectations()
    }
    assert all(
        measurement.evidence_status is EvidenceStatus.OBSERVED
        for measurement in measurements
    )
    assert all(measurement.outcome is ScoreOutcome.PASS for measurement in measurements)
    assert all(measurement.value == 1.0 for measurement in measurements)


def test_repeatability_is_exact_and_registry_is_closed() -> None:
    registry = DeterministicEvaluatorRegistry()
    first = registry.evaluate_case(_expectations(), _evidence())
    second = registry.evaluate_case(_expectations(), _evidence())
    assert first == second
    for evaluator_id, version in BUILTIN_EVALUATOR_DEFINITIONS.values():
        assert registry.resolve(evaluator_id, version).metric.startswith("evaluation.")

    with pytest.raises(DefinitionValidationError):
        registry.resolve("aura.evaluator.unknown", "1.0.0")
    with pytest.raises(DefinitionValidationError):
        registry.resolve("aura.evaluator.exact", "9.0.0")
    with pytest.raises(DefinitionValidationError):
        registry.resolve("builtins.eval", "1.0.0")


@pytest.mark.parametrize(
    "family",
    [
        "exact",
        "schema",
        "retrieval",
        "context",
        "tool",
        "policy",
        "groundedness",
        "response",
        "resource",
    ],
)
def test_missing_evidence_is_explicitly_not_observed(family: str) -> None:
    measurement = DeterministicEvaluatorRegistry().evaluate(
        f"aura.evaluator.{family}", "1.0.0", _expectations()[family], TypedReplayEvidence()
    )
    assert measurement.evidence_status is EvidenceStatus.NOT_OBSERVED
    assert measurement.value is None
    assert measurement.outcome is None
    assert measurement.failure_code == "missing_evidence"


@pytest.mark.parametrize(
    ("family", "evidence"),
    [
        ("exact", TypedReplayEvidence(fields={"answer": "b" * 64})),
        (
            "schema",
            TypedReplayEvidence(
                schema_digest="b" * 64, schema_fields=frozenset({"answer"})
            ),
        ),
        (
            "retrieval",
            TypedReplayEvidence(
                retrieved=(RetrievedEvidence("fixture-1", 99, 0, False, False),)
            ),
        ),
        (
            "context",
            TypedReplayEvidence(
                selected_evidence_ids=frozenset({"fixture-noise", "fixture-1"}),
                selected_tokens=1,
            ),
        ),
        (
            "tool",
            TypedReplayEvidence(
                tool_calls=(
                    ToolObservation(
                        "lookup-1", "lookup", "b" * 64, "b" * 64, "success", "none"
                    ),
                )
            ),
        ),
        (
            "policy",
            TypedReplayEvidence(
                policy=PolicyObservation(
                    "deny", "required", "deny", "deny", "wrong-policy"
                )
            ),
        ),
        (
            "groundedness",
            TypedReplayEvidence(
                claims=(ClaimObservation("claim-1", "unsupported", (), False),)
            ),
        ),
        (
            "response",
            TypedReplayEvidence(
                response=ResponseProjection(
                    response_digest="b" * 64, byte_count=9999, token_count=9999
                )
            ),
        ),
            (
                "resource",
                TypedReplayEvidence(
                    resources=ResourceProjection(
                        {
                            "latency_ms": 100000,
                            "queue_ms": 0,
                            "input_bytes": 0,
                            "output_bytes": 0,
                            "tokens": 0,
                            "memory_bytes": 0,
                            "provider_calls": 0,
                            "concurrency": 1,
                            "operations": 0,
                            "throughput_per_second": 1,
                        }
                    )
                ),
            ),
    ],
)
def test_contradictory_or_over_budget_evidence_fails(
    family: str, evidence: TypedReplayEvidence
) -> None:
    measurement = DeterministicEvaluatorRegistry().evaluate(
        f"aura.evaluator.{family}", "1.0.0", _expectations()[family], evidence
    )
    assert measurement.evidence_status in {
        EvidenceStatus.OBSERVED,
        EvidenceStatus.NOT_OBSERVED,
    }
    if measurement.evidence_status is EvidenceStatus.OBSERVED:
        assert measurement.outcome is ScoreOutcome.FAIL
        assert measurement.value is not None and measurement.value < 1.0
    else:
        assert measurement.value is None
        assert measurement.outcome is None


def test_not_applicable_and_partial_evidence_are_distinct() -> None:
    registry = DeterministicEvaluatorRegistry()
    not_applicable = registry.evaluate(
        "aura.evaluator.retrieval",
        "1.0.0",
        {"rankedEvidence": [], "cutoffs": [1]},
        TypedReplayEvidence(retrieved=(RetrievedEvidence("fixture-1", 1),)),
    )
    assert not_applicable.evidence_status is EvidenceStatus.NOT_APPLICABLE
    assert not_applicable.outcome is None

    partial = registry.evaluate(
        "aura.evaluator.response",
        "1.0.0",
        {"properties": ["non_empty"], "maxBytes": 100, "maxTokens": 100},
        TypedReplayEvidence(response=ResponseProjection(properties=frozenset({"non_empty"}))),
    )
    assert partial.evidence_status is EvidenceStatus.NOT_OBSERVED
    assert partial.failure_code == "partial_evidence"


def test_typed_evidence_rejects_non_finite_and_unbounded_values() -> None:
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(fields={"answer": nan}).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(resources=ResourceProjection({"latency_ms": inf})).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(selected_tokens=100_000_001).validate()


@pytest.mark.parametrize(
    "evidence",
    [
        TypedReplayEvidence(fields={"endpoint": "https://private.invalid"}),
        TypedReplayEvidence(fields={"answer": "raw-content"}),
        TypedReplayEvidence(schema_fields=frozenset({"owner_identity"})),
        TypedReplayEvidence(selected_evidence_ids=frozenset({"credential"})),
        TypedReplayEvidence(
            retrieved=(RetrievedEvidence("private.path", 1),)
        ),
        TypedReplayEvidence(
            tool_calls=(ToolObservation("call", "tool", "a" * 63, None, "success", "none"),)
        ),
        TypedReplayEvidence(
            tool_calls=(ToolObservation("call", "tool", DIGEST, None, "unexpected", "none"),)
        ),
        TypedReplayEvidence(
            claims=(ClaimObservation("claim", "supported", ("owner.identity",), False),)
        ),
        TypedReplayEvidence(
            response=ResponseProjection(instruction_ids=frozenset({"prompt"}))
        ),
        TypedReplayEvidence(resources=ResourceProjection({"private.endpoint": 1})),
        TypedReplayEvidence(
            tool_calls=(
                ToolObservation("owner.identity", "lookup", DIGEST, DIGEST, "success", "none"),
            )
        ),
        TypedReplayEvidence(
            tool_calls=(
                ToolObservation("lookup", "private.tool", DIGEST, DIGEST, "success", "none"),
            )
        ),
        TypedReplayEvidence(
            policy=PolicyObservation("maybe", "not_required", "not_applicable", "no_action")
        ),
        TypedReplayEvidence(resources=ResourceProjection({"custom_measurement": 1})),
        TypedReplayEvidence(injection_resisted="yes"),  # type: ignore[arg-type]
        TypedReplayEvidence(truncated="yes"),  # type: ignore[arg-type]
        TypedReplayEvidence(conflict_detected="yes"),  # type: ignore[arg-type]
    ],
)
def test_replay_evidence_rejects_raw_private_malformed_and_sensitive_values(
    evidence: TypedReplayEvidence,
) -> None:
    with pytest.raises(DefinitionValidationError):
        evidence.validate()


def test_replay_evidence_rejects_oversized_collections_and_strings() -> None:
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(fields={f"field-{index}": "x" for index in range(129)}).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(fields={"answer": "x" * 257}).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(
            retrieved=tuple(RetrievedEvidence(f"e-{index}", 1) for index in range(257))
        ).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(
            resources=ResourceProjection({f"metric-{index}": 1 for index in range(33)})
        ).validate()


@pytest.mark.parametrize(
    "response",
    [
        ResponseProjection(properties=frozenset({"unknown_property"})),
        ResponseProjection(properties=frozenset({f"property-{index}" for index in range(9)})),
        ResponseProjection(
            instruction_ids=frozenset(f"instruction-{index}" for index in range(65))
        ),
        ResponseProjection(style_ids=frozenset(f"style-{index}" for index in range(33))),
        ResponseProjection(byte_count=1_073_741_825),
        ResponseProjection(token_count=100_000_001),
        ResponseProjection(byte_count=True),
        ResponseProjection(token_count=False),
    ],
)
def test_response_projection_rejects_closed_enum_count_and_magnitude_boundaries(
    response: ResponseProjection,
) -> None:
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(response=response).validate()


@pytest.mark.parametrize(
    "evidence",
    [
        TypedReplayEvidence(fields={"answer": 1.0e15 + 1}),
        TypedReplayEvidence(resources=ResourceProjection({"latency_ms": 1.0e15 + 1})),
        TypedReplayEvidence(selected_tokens=100_000_001),
    ],
)
def test_replay_numeric_magnitude_boundaries_are_rejected(
    evidence: TypedReplayEvidence,
) -> None:
    with pytest.raises(DefinitionValidationError):
        evidence.validate()


@pytest.mark.parametrize(
    "evidence",
    [
        TypedReplayEvidence(
            retrieved=(RetrievedEvidence("fixture-1", 1, 3, citation_present=1),)
        ),
        TypedReplayEvidence(
            retrieved=(RetrievedEvidence("fixture-1", 1, 3, fresh=1),)
        ),
        TypedReplayEvidence(
            tool_calls=(
                ToolObservation(
                    "lookup-1", "lookup", DIGEST, DIGEST, "success", "none", idempotent=1
                ),
            )
        ),
        TypedReplayEvidence(
            claims=(ClaimObservation("claim-1", "supported", ("fixture-1",), citation_present=1),)
        ),
    ],
)
def test_optional_boolean_flags_are_strictly_boolean(evidence: TypedReplayEvidence) -> None:
    with pytest.raises(DefinitionValidationError):
        evidence.validate()


def test_claim_support_evidence_ids_have_a_bounded_closed_shape() -> None:
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(
            claims=(
                ClaimObservation(
                    "claim-1",
                    "supported",
                    tuple(f"evidence-{index}" for index in range(33)),
                ),
            )
        ).validate()
    with pytest.raises(DefinitionValidationError):
        TypedReplayEvidence(
            claims=(ClaimObservation("claim-1", "supported", (1,)),)  # type: ignore[arg-type]
        ).validate()


def test_score_projection_is_finite_content_free_and_uses_existing_port() -> None:
    context = ScoreContext(
        score_id=UUIDS[0],
        job_id=UUIDS[1],
        result_id=UUIDS[2],
        dataset_id=UUIDS[3],
        suite_id=UUIDS[4],
        evaluator_id=UUIDS[5],
        evaluator_version="1.0.0",
        component_id="aura.core.runner",
        component_version="1.0.0",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
    )
    score = to_evaluation_score(
        context,
        registry_measurement := DeterministicEvaluatorRegistry().evaluate(
            "aura.evaluator.exact", "1.0.0", _expectations()["exact"], _evidence()
        ),
    )
    assert score.evidence_status == "observed"
    assert score.outcome == "pass"
    assert score.capture_classification == "metadata_only"
    assert "content" not in repr(score).casefold()
    assert registry_measurement.value == 1.0


def test_schema_rejects_undeclared_fields_when_additional_fields_are_prohibited() -> None:
    expectation = _expectations()["schema"]
    evidence = _evidence()
    evidence = TypedReplayEvidence(
        schema_digest=DIGEST,
        schema_fields=frozenset({"answer", "unexpected"}),
    )
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.schema", "1.0.0", expectation, evidence
    )
    assert result.outcome is ScoreOutcome.FAIL
    assert result.value is not None and result.value < 1.0


@pytest.mark.parametrize(
    "retrieved",
    [
        RetrievedEvidence("fixture-1", 99, 3, True, True),
        RetrievedEvidence("fixture-1", 1, 0, True, True),
        RetrievedEvidence("fixture-1", 1, 3, False, True),
        RetrievedEvidence("fixture-1", 1, 3, True, False, "stale"),
    ],
)
def test_retrieval_enforces_rank_relevance_citation_and_freshness(
    retrieved: RetrievedEvidence,
) -> None:
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.retrieval",
        "1.0.0",
        _expectations()["retrieval"],
        TypedReplayEvidence(retrieved=(retrieved,)),
    )
    assert result.outcome is ScoreOutcome.FAIL


def test_retrieval_rejects_irrelevant_evidence_even_when_expected_hit_exists() -> None:
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.retrieval",
        "1.0.0",
        _expectations()["retrieval"],
        TypedReplayEvidence(
            retrieved=(
                RetrievedEvidence("fixture-1", 1, 3, True, True),
                RetrievedEvidence("fixture-noise", 2, 0, False, True),
            )
        ),
    )
    assert result.outcome is ScoreOutcome.FAIL
    assert result.failure_code == "irrelevant_evidence"


def test_exact_evaluator_accepts_only_digest_evidence_and_compares_it_exactly() -> None:
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.exact",
        "1.0.0",
        _expectations()["exact"],
        TypedReplayEvidence(fields={"answer": "b" * 64}),
    )
    assert result.evidence_status is EvidenceStatus.OBSERVED
    assert result.outcome is ScoreOutcome.FAIL
    assert result.value == 0.0


def test_retrieval_exposes_distinct_cutoff_and_quality_measurements() -> None:
    measurements = DeterministicEvaluatorRegistry().evaluate_case(
        {"retrieval": _expectations()["retrieval"]}, _evidence()
    )
    metrics = {measurement.metric for measurement in measurements}
    assert {
        "evaluation.retrieval.recall_at_1",
        "evaluation.retrieval.precision_at_1",
        "evaluation.retrieval.recall_at_3",
        "evaluation.retrieval.precision_at_3",
        "evaluation.retrieval.mrr",
        "evaluation.retrieval.citation_support",
        "evaluation.retrieval.freshness",
        "evaluation.retrieval.irrelevant_rate",
    }.issubset(metrics)


def test_context_enforces_token_ceiling_truncation_conflict_and_injection() -> None:
    expectation = _expectations()["context"]
    registry = DeterministicEvaluatorRegistry()
    base = _evidence()
    over_budget = TypedReplayEvidence(
        selected_evidence_ids=base.selected_evidence_ids,
        selected_tokens=expectation["maxSelectedTokens"] + 1,
        injection_resisted=True,
        truncated=True,
        conflict_detected=False,
        authority_preserved=True,
    )
    assert (
        registry.evaluate("aura.evaluator.context", "1.0.0", expectation, over_budget).outcome
        is ScoreOutcome.FAIL
    )

    not_truncated = TypedReplayEvidence(
        selected_evidence_ids=base.selected_evidence_ids,
        selected_tokens=expectation["maxSelectedTokens"],
        injection_resisted=True,
        truncated=False,
        conflict_detected=False,
        authority_preserved=True,
    )
    assert (
        registry.evaluate("aura.evaluator.context", "1.0.0", expectation, not_truncated).outcome
        is ScoreOutcome.PASS
    )

    missing_injection = TypedReplayEvidence(
        selected_evidence_ids=base.selected_evidence_ids,
        selected_tokens=expectation["maxSelectedTokens"],
        truncated=True,
        conflict_detected=False,
    )
    incomplete = registry.evaluate(
        "aura.evaluator.context", "1.0.0", expectation, missing_injection
    )
    assert incomplete.evidence_status is EvidenceStatus.NOT_OBSERVED
    assert incomplete.failure_code == "authority_not_observed"


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("truncated", "truncation_not_observed"),
        ("conflict_detected", "conflict_not_observed"),
        ("authority_preserved", "authority_not_observed"),
    ],
)
def test_context_reports_each_explicit_missing_state(field: str, expected_code: str) -> None:
    values: dict[str, Any] = {
        "selected_evidence_ids": frozenset({"fixture-1"}),
        "selected_tokens": 100,
        "truncated": False,
        "conflict_detected": False,
        "authority_preserved": True,
        "injection_resisted": True,
    }
    values[field] = None
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.context",
        "1.0.0",
        _expectations()["context"],
        TypedReplayEvidence(**values),
    )
    assert result.evidence_status is EvidenceStatus.NOT_OBSERVED
    assert result.failure_code == expected_code


def test_response_reports_missing_digest_and_refusal_states() -> None:
    registry = DeterministicEvaluatorRegistry()
    base: dict[str, Any] = {
        "byte_count": 100,
        "token_count": 50,
        "properties": frozenset({"non_empty", "valid_utf8"}),
        "instruction_ids": frozenset({"instruction-1"}),
        "style_ids": frozenset({"style-1"}),
    }
    digest_missing = registry.evaluate(
        "aura.evaluator.response",
        "1.0.0",
        _expectations()["response"],
        TypedReplayEvidence(response=ResponseProjection(**base, refusal=False)),
    )
    assert digest_missing.failure_code == "response_digest_not_observed"
    refusal_missing = registry.evaluate(
        "aura.evaluator.response",
        "1.0.0",
        _expectations()["response"],
        TypedReplayEvidence(
            response=ResponseProjection(**base, response_digest=DIGEST, refusal=None)
        ),
    )
    assert refusal_missing.failure_code == "refusal_not_observed"


@pytest.mark.parametrize(
    "observation",
    [
        ToolObservation("lookup-1", "lookup", "b" * 64, DIGEST, "success", "none", True),
        ToolObservation("lookup-1", "lookup", DIGEST, DIGEST, "success", "consequential", True),
        ToolObservation("lookup-1", "lookup", DIGEST, DIGEST, "success", "none", False),
    ],
)
def test_tool_enforces_argument_schema_side_effect_and_idempotency(
    observation: ToolObservation,
) -> None:
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.tool",
        "1.0.0",
        _expectations()["tool"],
        TypedReplayEvidence(tool_calls=(observation,)),
    )
    assert result.outcome is ScoreOutcome.FAIL


def test_tool_rejects_prohibited_and_redundant_calls() -> None:
    expectation = _expectations()["tool"]
    registry = DeterministicEvaluatorRegistry()
    prohibited = ToolObservation("write-1", "write", DIGEST, DIGEST, "success", "none", True)
    result = registry.evaluate(
        "aura.evaluator.tool",
        "1.0.0",
        expectation,
        TypedReplayEvidence(tool_calls=(_evidence().tool_calls[0], prohibited)),
    )
    assert result.outcome is ScoreOutcome.FAIL
    assert result.failure_code == "prohibited_tool"

    redundant = ToolObservation("lookup-2", "lookup", DIGEST, DIGEST, "success", "none", True)
    result = registry.evaluate(
        "aura.evaluator.tool",
        "1.0.0",
        expectation,
        TypedReplayEvidence(tool_calls=(_evidence().tool_calls[0], redundant)),
    )
    assert result.outcome is ScoreOutcome.FAIL
    assert result.failure_code == "redundant_tool_calls"


def test_policy_requires_revision_and_groundedness_requires_exact_support_evidence() -> None:
    registry = DeterministicEvaluatorRegistry()
    policy = registry.evaluate(
        "aura.evaluator.policy",
        "1.0.0",
        _expectations()["policy"],
        TypedReplayEvidence(
            policy=PolicyObservation(
                "allow", "not_required", "not_applicable", "no_action", "wrong-policy"
            )
        ),
    )
    assert policy.outcome is ScoreOutcome.FAIL

    groundedness = registry.evaluate(
        "aura.evaluator.groundedness",
        "1.0.0",
        _expectations()["groundedness"],
        TypedReplayEvidence(
            claims=(ClaimObservation("claim-1", "supported", ("other",), True),)
        ),
    )
    assert groundedness.outcome is ScoreOutcome.FAIL


def test_response_requires_refusal_instruction_style_and_properties() -> None:
    response = ResponseProjection(
        response_digest=DIGEST,
        byte_count=2048,
        token_count=256,
        properties=frozenset({"non_empty", "valid_utf8"}),
        refusal=True,
        instruction_ids=frozenset(),
        style_ids=frozenset(),
    )
    result = DeterministicEvaluatorRegistry().evaluate(
        "aura.evaluator.response",
        "1.0.0",
        _expectations()["response"],
        TypedReplayEvidence(response=response),
    )
    assert result.outcome is ScoreOutcome.FAIL
    assert result.value is not None and result.value < 1.0
