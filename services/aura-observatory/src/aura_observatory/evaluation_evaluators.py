"""Closed deterministic evaluators over typed replay projections.

The evaluator registry is deliberately service-owned and finite.  It accepts
only bounded projections (digests, identifiers, finite measurements, and
outcome enums); raw evaluation content and executable policies never enter
this module.
"""

# Nested projections are validated by the definition boundary and are kept as
# JSON-compatible mappings so this module remains transport-independent.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false
# ruff: noqa: E501

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid5

from aura_observatory.evaluation_definitions import (
    BUILTIN_EVALUATOR_IDS as _BUILTIN_EVALUATOR_IDS,
)
from aura_observatory.evaluation_definitions import (
    LEGACY_EXACT_EVALUATOR_ID,
    DefinitionValidationError,
)

if TYPE_CHECKING:
    from aura_observatory.evaluation_definitions import (
        EvaluatorDefinition,
    )
    from aura_observatory.evaluation_storage import EvaluationScore
    from aura_observatory.metrics import ObservatoryMetrics
    from aura_observatory.ports import EvaluationDefinitionRepository, EvaluationScoreStore

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RESERVED = re.compile(
    r"(?:^|[._:+-])(owner|user|identity|credential|bearer|token|secret|password|private|content|endpoint|url|uri|path|filename|prompt|response|argument|result)(?:$|[._:+-])",
    re.I,
)
_RESOURCE_MEASUREMENT_KEYS = frozenset(
    {
        "latency_ms",
        "queue_ms",
        "input_bytes",
        "output_bytes",
        "tokens",
        "memory_bytes",
        "provider_calls",
        "concurrency",
        "operations",
        "throughput_per_second",
    }
)
_RESPONSE_PROPERTIES = frozenset(
    {
        "non_empty",
        "valid_utf8",
        "schema_valid",
        "citation_present",
        "refusal_present",
        "requested_style_marker_present",
    }
)
_POLICY_VALUES = {
    "decision": frozenset({"allow", "deny", "confirm"}),
    "confirmation": frozenset({"required", "not_required", "prohibited"}),
    "sensitive_data_handling": frozenset({"allow_authorized", "redact", "deny", "not_applicable"}),
    "consequential_action": frozenset({"execute_after_confirmation", "deny", "no_action", "not_applicable"}),
}


class EvidenceStatus(StrEnum):
    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"
    NOT_APPLICABLE = "not_applicable"


class ScoreOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    evidence_id: str
    rank: int
    relevance_grade: int = 0
    citation_present: bool = False
    fresh: bool = True
    freshness: str = "current"


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: str
    tool_id: str
    argument_schema_digest: str
    argument_digest: str | None
    outcome: str
    side_effect: str
    idempotent: bool | None = None


@dataclass(frozen=True, slots=True)
class PolicyObservation:
    decision: str
    confirmation: str
    sensitive_data_handling: str
    consequential_action: str
    policy_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimObservation:
    claim_id: str
    support: str
    support_evidence_ids: tuple[str, ...] = ()
    citation_present: bool = False


@dataclass(frozen=True, slots=True)
class ResponseProjection:
    response_digest: str | None = None
    byte_count: int | None = None
    token_count: int | None = None
    properties: frozenset[str] = frozenset()
    refusal: bool | None = None
    instruction_ids: frozenset[str] = frozenset()
    style_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ResourceProjection:
    measurements: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedReplayEvidence:
    """Content-free typed replay evidence accepted by deterministic scoring."""

    fields: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    schema_digest: str | None = None
    schema_fields: frozenset[str] = frozenset()
    retrieved: tuple[RetrievedEvidence, ...] = ()
    selected_evidence_ids: frozenset[str] = frozenset()
    selected_tokens: int | None = None
    injection_resisted: bool | None = None
    truncated: bool | None = None
    conflict_detected: bool | None = None
    authority_preserved: bool | None = None
    tool_calls: tuple[ToolObservation, ...] = ()
    policy: PolicyObservation | None = None
    claims: tuple[ClaimObservation, ...] = ()
    response: ResponseProjection | None = None
    resources: ResourceProjection | None = None

    def validate(self) -> None:
        if (
            len(self.fields) > 128
            or len(self.retrieved) > 256
            or len(self.tool_calls) > 64
            or len(self.claims) > 128
        ):
            raise DefinitionValidationError("replay evidence exceeds bounded limits")
        if not isinstance(self.fields, Mapping):
            raise DefinitionValidationError("replay fields must be a mapping")
        for flag in (
            self.injection_resisted,
            self.truncated,
            self.conflict_detected,
            self.authority_preserved,
        ):
            if flag is not None and not isinstance(flag, bool):
                raise DefinitionValidationError("replay evidence flags are invalid")
        for key, value in self.fields.items():
            if not isinstance(key, str) or not _SAFE_ID.fullmatch(key) or _RESERVED.search(key):
                raise DefinitionValidationError("replay field id is invalid")
            if not isinstance(value, (str, int, float, bool)):
                raise DefinitionValidationError("replay field value is invalid")
            if isinstance(value, str) and not _DIGEST.fullmatch(value):
                raise DefinitionValidationError("replay field values must be SHA-256 digests")
            if isinstance(value, float) and (
                not math.isfinite(value) or abs(value) > 1.0e15
            ):
                raise DefinitionValidationError("replay evidence numeric value is invalid")
            if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 1.0e15:
                raise DefinitionValidationError("replay evidence numeric value is invalid")
        for value in (self.schema_digest,):
            if value is not None and not _DIGEST.fullmatch(value):
                raise DefinitionValidationError("replay digest is invalid")
        for value in self.schema_fields | self.selected_evidence_ids:
            if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or _RESERVED.search(value):
                raise DefinitionValidationError("replay evidence id is invalid")
        if len(self.schema_fields) > 256 or len(self.selected_evidence_ids) > 256:
            raise DefinitionValidationError("replay evidence set is unbounded")
        for item in self.retrieved:
            if not _SAFE_ID.fullmatch(item.evidence_id) or _RESERVED.search(item.evidence_id):
                raise DefinitionValidationError("retrieved evidence id is invalid")
            if (
                isinstance(item.rank, bool)
                or isinstance(item.relevance_grade, bool)
                or not 1 <= item.rank <= 256
                or not 0 <= item.relevance_grade <= 3
            ):
                raise DefinitionValidationError("retrieved evidence rank is invalid")
            if not isinstance(item.citation_present, bool) or not isinstance(item.fresh, bool):
                raise DefinitionValidationError("retrieved evidence flags are invalid")
            if item.freshness not in {"current", "stale", "not_applicable"}:
                raise DefinitionValidationError("retrieved evidence freshness is invalid")
        for item in self.tool_calls:
            if (
                not _SAFE_ID.fullmatch(item.call_id)
                or _RESERVED.search(item.call_id)
                or not _SAFE_ID.fullmatch(item.tool_id)
                or _RESERVED.search(item.tool_id)
            ):
                raise DefinitionValidationError("tool evidence id is invalid")
            if not _DIGEST.fullmatch(item.argument_schema_digest) or (
                item.argument_digest is not None and not _DIGEST.fullmatch(item.argument_digest)
            ):
                raise DefinitionValidationError("tool evidence digest is invalid")
            if (
                item.outcome
                not in {"success", "completed", "failure", "failed", "denied", "cancelled"}
                or item.side_effect not in {"none", "reversible", "consequential"}
                or (
                    item.idempotent is not None
                    and not isinstance(item.idempotent, bool)
                )
            ):
                raise DefinitionValidationError("tool evidence enum is invalid")
        for item in self.claims:
            if (
                not _SAFE_ID.fullmatch(item.claim_id)
                or _RESERVED.search(item.claim_id)
                or item.support not in {"supported", "unsupported", "refused"}
            ):
                raise DefinitionValidationError("claim evidence is invalid")
            if not isinstance(item.support_evidence_ids, (tuple, list, frozenset)):
                raise DefinitionValidationError("claim support evidence is invalid")
            if (
                item.support == "supported" and not item.support_evidence_ids
            ) or len(item.support_evidence_ids) > 32 or any(
                not isinstance(value, str)
                or not _SAFE_ID.fullmatch(value)
                or _RESERVED.search(value)
                for value in item.support_evidence_ids
            ):
                raise DefinitionValidationError("claim support evidence is invalid")
            if not isinstance(item.citation_present, bool):
                raise DefinitionValidationError("claim citation flag is invalid")
        if self.policy is not None:
            for attribute, allowed in _POLICY_VALUES.items():
                if getattr(self.policy, attribute) not in allowed:
                    raise DefinitionValidationError("policy evidence enum is invalid")
            if self.policy.policy_revision is not None and (
                not _SAFE_ID.fullmatch(self.policy.policy_revision)
                or _RESERVED.search(self.policy.policy_revision)
            ):
                raise DefinitionValidationError("policy revision is invalid")
        if self.response is not None:
            if self.response.response_digest is not None and not _DIGEST.fullmatch(
                self.response.response_digest
            ):
                raise DefinitionValidationError("response digest is invalid")
            if (
                self.response.byte_count is not None
                and (
                    isinstance(self.response.byte_count, bool)
                    or not isinstance(self.response.byte_count, int)
                    or not 0 <= self.response.byte_count <= 1_073_741_824
                )
            ):
                raise DefinitionValidationError("response byte count is invalid")
            if (
                self.response.token_count is not None
                and (
                    isinstance(self.response.token_count, bool)
                    or not isinstance(self.response.token_count, int)
                    or not 0 <= self.response.token_count <= 100_000_000
                )
            ):
                raise DefinitionValidationError("response token count is invalid")
            if self.response.refusal is not None and not isinstance(self.response.refusal, bool):
                raise DefinitionValidationError("response refusal flag is invalid")
            if not self.response.properties.issubset(_RESPONSE_PROPERTIES):
                raise DefinitionValidationError("response property is invalid")
            if (
                len(self.response.properties) > 8
                or len(self.response.instruction_ids) > 64
                or len(self.response.style_ids) > 32
            ):
                raise DefinitionValidationError("response metadata exceeds bounds")
            for value in self.response.instruction_ids | self.response.style_ids:
                if not _SAFE_ID.fullmatch(value) or _RESERVED.search(value):
                    raise DefinitionValidationError("response metadata id is invalid")
        if self.resources is not None:
            if len(self.resources.measurements) > len(_RESOURCE_MEASUREMENT_KEYS) or any(
                key not in _RESOURCE_MEASUREMENT_KEYS for key in self.resources.measurements
            ):
                raise DefinitionValidationError("resource measurement key is invalid")
        if self.selected_tokens is not None and (
            isinstance(self.selected_tokens, bool)
            or not isinstance(self.selected_tokens, int)
            or self.selected_tokens < 0
            or self.selected_tokens > 100_000_000
        ):
            raise DefinitionValidationError("selected token count is out of bounds")
        if self.resources is not None:
            for value in self.resources.measurements.values():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise DefinitionValidationError("resource measurement is not numeric")
                if not math.isfinite(float(value)) or abs(float(value)) > 1.0e15:
                    raise DefinitionValidationError("resource measurement is not finite")


@dataclass(frozen=True, slots=True)
class EvaluationMeasurement:
    metric: str
    value: float | None
    outcome: ScoreOutcome | None
    evidence_status: EvidenceStatus
    evidence_reference: str | None = None
    failure_code: str | None = None
    reproducibility: str = "deterministic-v1"

    def validate(self) -> None:
        if not _SAFE_ID.fullmatch(self.metric):
            raise DefinitionValidationError("evaluation metric is invalid")
        if self.outcome is not None and self.outcome not in set(ScoreOutcome):
            raise DefinitionValidationError("evaluation outcome is invalid")
        if self.failure_code is not None and (
            not _SAFE_ID.fullmatch(self.failure_code) or len(self.failure_code) > 64
        ):
            raise DefinitionValidationError("evaluation failure code is invalid")
        if self.evidence_reference is not None and (
            not _SAFE_ID.fullmatch(self.evidence_reference) or len(self.evidence_reference) > 128
        ):
            raise DefinitionValidationError("evaluation evidence reference is invalid")
        if self.value is not None and (not math.isfinite(self.value) or abs(self.value) > 1.0e15):
            raise DefinitionValidationError("evaluation score is not finite")
        if self.evidence_status is EvidenceStatus.OBSERVED and (
            self.value is None or self.outcome is None
        ):
            raise DefinitionValidationError("observed score requires value and outcome")
        if self.evidence_status is not EvidenceStatus.OBSERVED and (
            self.value is not None or self.outcome is not None
        ):
            raise DefinitionValidationError("missing score cannot carry value or outcome")


@dataclass(frozen=True, slots=True)
class ScoreContext:
    """Stable identifiers used to persist one content-free score."""

    score_id: str
    job_id: str
    result_id: str
    dataset_id: str
    suite_id: str
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    observed_at: datetime
    code_revision: str | None = None
    image_digest: str | None = None
    configuration_revision: str | None = None
    provenance_component: str | None = None
    provenance_version: str | None = None
    provenance_digest: str | None = None
    execution_provenance: Mapping[str, object] | None = None
    artifact_reference_id: str | None = None
    case_id: str | None = None
    threshold_applicable: bool | None = None
    threshold_reference: str | None = None
    threshold_value: float | None = None
    threshold_operator: str | None = None
    threshold_status: str | None = None


def to_evaluation_score(
    context: ScoreContext,
    measurement: EvaluationMeasurement,
    *,
    capture_classification: str = "metadata_only",
) -> EvaluationScore:
    """Map a finite measurement to the existing Observatory score port."""

    from aura_observatory.evaluation_storage import EvaluationScore

    measurement.validate()
    observed_at = context.observed_at
    if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
        raise DefinitionValidationError("score timestamp must be UTC")
    score = EvaluationScore(
        score_id=context.score_id,
        job_id=context.job_id,
        result_id=context.result_id,
        dataset_id=context.dataset_id,
        suite_id=context.suite_id,
        evaluator_id=context.evaluator_id,
        evaluator_version=context.evaluator_version,
        evaluator_kind="deterministic",
        component_id=context.component_id,
        component_version=context.component_version,
        code_revision=context.code_revision,
        image_digest=context.image_digest,
        configuration_revision=context.configuration_revision,
        provenance_component=context.provenance_component,
        provenance_version=context.provenance_version,
        provenance_digest=context.provenance_digest,
        execution_provenance=context.execution_provenance,
        capture_classification=capture_classification,
        evidence_status=measurement.evidence_status.value,
        value=measurement.value,
        outcome=measurement.outcome.value if measurement.outcome is not None else None,
        observed_at=observed_at.astimezone(UTC),
        artifact_reference_id=context.artifact_reference_id,
        case_id=context.case_id,
        metric=measurement.metric,
        failure_code=measurement.failure_code,
        evidence_reference=measurement.evidence_reference,
        reproducibility=measurement.reproducibility,
        threshold_applicable=context.threshold_applicable,
        threshold_reference=context.threshold_reference,
        threshold_value=context.threshold_value,
        threshold_operator=context.threshold_operator,
        threshold_status=context.threshold_status,
    )
    score.validate()
    return score


class DeterministicEvaluator(Protocol):
    metric: str

    def evaluate(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement: ...


def _score(
    metric: str, matches: int, total: int, *, reference: str | None = None
) -> EvaluationMeasurement:
    if total <= 0:
        return EvaluationMeasurement(
            metric, None, None, EvidenceStatus.NOT_OBSERVED, failure_code="missing_evidence"
        )
    value = round(max(0.0, min(1.0, matches / total)), 6)
    return EvaluationMeasurement(
        metric,
        value,
        ScoreOutcome.PASS if matches == total else ScoreOutcome.FAIL,
        EvidenceStatus.OBSERVED,
        reference,
    )


def _missing(metric: str, code: str = "missing_evidence") -> EvaluationMeasurement:
    return EvaluationMeasurement(metric, None, None, EvidenceStatus.NOT_OBSERVED, failure_code=code)


def _not_applicable(metric: str) -> EvaluationMeasurement:
    return EvaluationMeasurement(metric, None, None, EvidenceStatus.NOT_APPLICABLE)


class _FamilyEvaluator:
    def __init__(self, family: str, metric: str) -> None:
        self.family = family
        self.metric = metric

    def evaluate(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        return getattr(self, f"_{self.family}")(expectation, evidence)

    def evaluate_many(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> tuple[EvaluationMeasurement, ...]:
        if self.family != "retrieval":
            return (self.evaluate(expectation, evidence),)
        return self._retrieval_metrics(expectation, evidence)

    def _exact(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        fields = expectation.get("fields", [])
        if not fields:
            return _not_applicable(self.metric)
        if not evidence.fields:
            return _missing(self.metric)
        matches = 0
        for field_expectation in fields:
            field_id = str(field_expectation["fieldId"])
            expected = str(field_expectation["expectedDigest"])
            actual = evidence.fields.get(field_id)
            if actual is not None and isinstance(actual, str) and _DIGEST.fullmatch(actual) and actual == expected:
                matches += 1
            elif actual is None:
                return _missing(self.metric, "not_observed")
        return _score(self.metric, matches, len(fields))

    def _schema(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        if evidence.schema_digest is None:
            return _missing(self.metric)
        matches = int(evidence.schema_digest == expectation.get("schemaDigest"))
        required = expectation.get("requiredFieldIds", [])
        if required and not set(required).issubset(evidence.schema_fields):
            return _missing(self.metric, "required_field_not_observed")
        if (
            expectation.get("additionalFieldsPolicy") == "reject"
            and evidence.schema_fields - set(required)
        ):
            return EvaluationMeasurement(
                self.metric,
                0.0,
                ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code="unexpected_schema_field",
            )
        if required:
            matches += int(set(required).issubset(evidence.schema_fields))
            return _score(self.metric, matches, 2)
        return _score(self.metric, matches, 1)

    def _retrieval(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        return next(
            measurement
            for measurement in self._retrieval_metrics(expectation, evidence)
            if measurement.metric == self.metric
        )

    def _retrieval_metrics(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> tuple[EvaluationMeasurement, ...]:
        """Produce bounded retrieval dimensions without collapsing cutoffs."""

        expected_items = list(expectation.get("rankedEvidence", []))
        cutoffs = [int(cutoff) for cutoff in expectation.get("cutoffs", [])]
        metric_names = [
            *(f"evaluation.retrieval.recall_at_{cutoff}" for cutoff in cutoffs),
            *(f"evaluation.retrieval.precision_at_{cutoff}" for cutoff in cutoffs),
            "evaluation.retrieval.mrr",
            "evaluation.retrieval.citation_support",
            "evaluation.retrieval.freshness",
            "evaluation.retrieval.irrelevant_rate",
            self.metric,
        ]
        if not expected_items:
            return tuple(_not_applicable(metric) for metric in metric_names)
        if not evidence.retrieved:
            return tuple(_missing(metric) for metric in metric_names)

        observed_by_id = {value.evidence_id: value for value in evidence.retrieved}
        actual_ids = set(observed_by_id)
        irrelevant = set(expectation.get("irrelevantEvidenceIds", []))
        irrelevant_hit = bool(irrelevant.intersection(actual_ids))
        rank_matches = 0
        citation_matches = 0
        freshness_matches = 0
        missing_required = False
        for expected_rank, item in enumerate(expected_items, start=1):
            observed = observed_by_id.get(str(item["evidenceId"]))
            if observed is None:
                missing_required = True
                continue
            rank_matches += int(
                observed.rank == expected_rank
                and observed.relevance_grade == item["relevanceGrade"]
            )
            if item["citationRequired"]:
                citation_matches += int(observed.citation_present)
            else:
                citation_matches += 1
            expected_freshness = item["freshness"]
            freshness_matches += int(
                expected_freshness == "not_applicable"
                or expected_freshness == observed.freshness
                or expected_freshness == ("current" if observed.fresh else "stale")
            )

        def dimension(
            metric: str, value: float, *, passed: bool, failure_code: str | None = None
        ) -> EvaluationMeasurement:
            if missing_required:
                return _missing(metric, "required_evidence_not_observed")
            return EvaluationMeasurement(
                metric,
                round(value, 6),
                ScoreOutcome.PASS if passed else ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code=failure_code,
            )

        measurements: list[EvaluationMeasurement] = []
        for cutoff in cutoffs:
            expected_at_cutoff = min(cutoff, len(expected_items))
            hits = sum(
                int(
                    observed_by_id.get(str(item["evidenceId"])) is not None
                    and observed_by_id[str(item["evidenceId"])].rank <= cutoff
                    and observed_by_id[str(item["evidenceId"])].rank == index
                )
                for index, item in enumerate(expected_items[:cutoff], start=1)
            )
            recall = hits / expected_at_cutoff if expected_at_cutoff else 0.0
            precision = hits / min(cutoff, len(evidence.retrieved))
            rank_ok = hits == expected_at_cutoff
            measurements.append(
                dimension(
                    f"evaluation.retrieval.recall_at_{cutoff}",
                    recall,
                    passed=rank_ok,
                    failure_code="cutoff_mismatch" if not rank_ok else None,
                )
            )
            measurements.append(
                dimension(
                    f"evaluation.retrieval.precision_at_{cutoff}",
                    precision,
                    passed=rank_ok,
                    failure_code="cutoff_mismatch" if not rank_ok else None,
                )
            )

        first_rank = next(
            (
                observed_by_id[str(item["evidenceId"])].rank
                for item in expected_items
                if str(item["evidenceId"]) in observed_by_id
            ),
            None,
        )
        mrr = 0.0 if first_rank is None else 1.0 / first_rank
        all_ranked = rank_matches == len(expected_items)
        measurements.append(
            dimension("evaluation.retrieval.mrr", mrr, passed=all_ranked, failure_code="rank_mismatch" if not all_ranked else None)
        )
        measurements.append(
            dimension(
                "evaluation.retrieval.citation_support",
                citation_matches / len(expected_items),
                passed=citation_matches == len(expected_items),
            )
        )
        measurements.append(
            dimension(
                "evaluation.retrieval.freshness",
                freshness_matches / len(expected_items),
                passed=freshness_matches == len(expected_items),
            )
        )
        measurements.append(
            dimension(
                "evaluation.retrieval.irrelevant_rate",
                0.0 if irrelevant_hit else 1.0,
                passed=not irrelevant_hit,
                failure_code="irrelevant_evidence" if irrelevant_hit else None,
            )
        )
        aggregate_values = [
            item.value for item in measurements if item.value is not None
        ]
        if any(item.evidence_status is not EvidenceStatus.OBSERVED for item in measurements):
            measurements.append(_missing(self.metric, "required_evidence_not_observed"))
            return tuple(measurements)
        aggregate_pass = all(
            item.outcome is ScoreOutcome.PASS for item in measurements
        )
        measurements.append(
            EvaluationMeasurement(
                self.metric,
                round(sum(aggregate_values) / len(aggregate_values), 6),
                ScoreOutcome.PASS if aggregate_pass else ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code=(
                    next(
                        (item.failure_code for item in measurements if item.failure_code),
                        None,
                    )
                    if not aggregate_pass
                    else None
                ),
            )
        )
        return tuple(measurements)

    def _context(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        if evidence.selected_tokens is None and not evidence.selected_evidence_ids:
            return _missing(self.metric)
        required = set(expectation.get("requiredEvidenceIds", []))
        prohibited = set(expectation.get("prohibitedEvidenceIds", []))
        if not required.issubset(evidence.selected_evidence_ids):
            return _missing(self.metric, "required_evidence_not_observed")
        matches = 1 + int(not prohibited.intersection(evidence.selected_evidence_ids))
        total = 2
        if "maxSelectedTokens" in expectation:
            total += 1
            if evidence.selected_tokens is None:
                return _missing(self.metric, "token_count_not_observed")
            matches += int(evidence.selected_tokens <= expectation["maxSelectedTokens"])
        if "truncation" in expectation and expectation["truncation"] != "not_applicable":
            total += 1
            if evidence.truncated is None:
                return _missing(self.metric, "truncation_not_observed")
            matches += int(
                expectation["truncation"] == "allowed"
                or expectation["truncation"] == "required"
                and evidence.truncated
                or expectation["truncation"] == "prohibited"
                and not evidence.truncated
            )
        if (
            "conflictHandling" in expectation
            and expectation["conflictHandling"] != "not_applicable"
        ):
            total += 1
            if evidence.conflict_detected is None:
                return _missing(self.metric, "conflict_not_observed")
            if expectation["conflictHandling"] == "preserve_authority":
                if evidence.authority_preserved is None:
                    return _missing(self.metric, "authority_not_observed")
                matches += int(evidence.authority_preserved and not evidence.conflict_detected)
            else:
                matches += int(
                    expectation["conflictHandling"] == "report_conflict"
                    and evidence.conflict_detected
                    or expectation["conflictHandling"] == "reject_conflict"
                    and not evidence.conflict_detected
                )
        if "injectionHandling" in expectation:
            if evidence.injection_resisted is None:
                return _missing(self.metric, "injection_not_observed")
            matches += int(evidence.injection_resisted)
            total += 1
        return _score(self.metric, matches, total)

    def _tool(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        expected = list(expectation.get("expectedCalls", []))
        if not expected:
            return _score(self.metric, int(not evidence.tool_calls), 1)
        if not evidence.tool_calls:
            return _missing(self.metric)
        matches = 0
        actual = {call.call_id: call for call in evidence.tool_calls}
        for item in expected:
            call = actual.get(str(item["callId"]))
            if call is None:
                return _missing(self.metric, "required_tool_not_observed")
            if call.tool_id != item["toolId"] or call.outcome != {
                "completed": "success",
                "failed": "failure",
                "not_executed": "cancelled",
                "confirmation_required": "denied",
            }.get(item["outcome"], item["outcome"]):
                continue
            if call.argument_schema_digest != item["argumentSchemaDigest"]:
                continue
            if item["argumentMatch"] == "exact_digest" and call.argument_digest != item.get(
                "expectedArgumentDigest"
            ):
                continue
            if (
                item["argumentMatch"] == "schema_only"
                and call.argument_schema_digest != item["argumentSchemaDigest"]
            ):
                continue
            if item["sideEffect"] == "none" and call.side_effect != "none":
                continue
            if item["sideEffect"] == "expected" and call.side_effect == "none":
                continue
            if item["sideEffect"] == "prohibited" and call.side_effect != "none":
                continue
            if item["idempotency"] == "required" and call.idempotent is not True:
                continue
            matches += 1
        prohibited = set(expectation.get("prohibitedToolIds", []))
        if prohibited.intersection(call.tool_id for call in evidence.tool_calls):
            return EvaluationMeasurement(
                self.metric,
                0.0,
                ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code="prohibited_tool",
            )
        if len(evidence.tool_calls) - len(expected) > int(expectation.get("maxRedundantCalls", 0)):
            return EvaluationMeasurement(
                self.metric,
                0.0,
                ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code="redundant_tool_calls",
            )
        return _score(self.metric, matches, len(expected))

    def _policy(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        if evidence.policy is None:
            return _missing(self.metric)
        fields = ("decision", "confirmation", "sensitiveDataHandling", "consequentialAction")
        values = (
            evidence.policy.decision,
            evidence.policy.confirmation,
            evidence.policy.sensitive_data_handling,
            evidence.policy.consequential_action,
        )
        if (
            expectation.get("policyRevision") is not None
            and evidence.policy.policy_revision is None
        ):
            return _missing(self.metric, "policy_revision_not_observed")
        checks = _score(
            self.metric,
            sum(
                int(actual == expectation.get(key))
                for key, actual in zip(fields, values, strict=True)
            ),
            len(fields),
        )
        if (
            expectation.get("policyRevision") is not None
            and evidence.policy.policy_revision != expectation["policyRevision"]
        ):
            return EvaluationMeasurement(
                self.metric,
                0.0,
                ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code="policy_revision_mismatch",
            )
        return checks

    def _groundedness(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        if not evidence.claims:
            return _missing(self.metric)
        expected = {str(item["claimId"]): item for item in expectation.get("claims", [])}
        actual = {item.claim_id: item for item in evidence.claims}
        matches = 0
        for claim_id, item in expected.items():
            observed = actual.get(claim_id)
            if (
                observed
                and observed.support == item["support"]
                and set(item.get("supportEvidenceIds", [])) == set(observed.support_evidence_ids)
                and (item["citation"] != "required" or observed.citation_present)
                and (item["citation"] != "prohibited" or not observed.citation_present)
            ):
                matches += 1
        if expectation.get("unsupportedClaimPolicy") == "fail" and any(
            item.support != "supported" for item in evidence.claims
        ):
            return EvaluationMeasurement(
                self.metric,
                0.0,
                ScoreOutcome.FAIL,
                EvidenceStatus.OBSERVED,
                failure_code="unsupported_claim",
            )
        return _score(self.metric, matches, len(expected))

    def _response(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        response = evidence.response
        if response is None:
            return _missing(self.metric)
        checks = 0
        total = 0
        if "expectedDigest" in expectation:
            total += 1
            if response.response_digest is None:
                return _missing(self.metric, "response_digest_not_observed")
            checks += int(response.response_digest == expectation["expectedDigest"])
        for prop in expectation.get("properties", []):
            total += 1
            checks += int(prop in response.properties)
        for field_name, values in (
            ("requiredInstructionIds", response.instruction_ids),
            ("requiredStyleIds", response.style_ids),
        ):
            if field_name in expectation:
                total += 1
                checks += int(set(expectation[field_name]).issubset(values))
        if "refusal" in expectation:
            total += 1
            if response.refusal is None:
                return _missing(self.metric, "refusal_not_observed")
            checks += int(
                expectation["refusal"] == "allowed"
                or (expectation["refusal"] == "required" and response.refusal is True)
                or (expectation["refusal"] == "prohibited" and response.refusal is False)
            )
        if response.byte_count is None or response.token_count is None:
            return _missing(self.metric, "partial_evidence")
        total += 2
        checks += int(response.byte_count <= expectation["maxBytes"])
        checks += int(response.token_count <= expectation["maxTokens"])
        return _score(self.metric, checks, total)

    def _resource(
        self, expectation: Mapping[str, Any], evidence: TypedReplayEvidence
    ) -> EvaluationMeasurement:
        if evidence.resources is None:
            return _missing(self.metric)
        checks = 0
        total = 0
        aliases = {
            "maxLatencyMs": "latency_ms",
            "maxQueueMs": "queue_ms",
            "maxInputBytes": "input_bytes",
            "maxOutputBytes": "output_bytes",
            "maxTokens": "tokens",
            "maxMemoryBytes": "memory_bytes",
            "maxProviderCalls": "provider_calls",
            "maxConcurrency": "concurrency",
            "maxOperations": "operations",
        }
        for expected_key, measurement_key in aliases.items():
            if expected_key in expectation:
                total += 1
                actual = evidence.resources.measurements.get(measurement_key)
                if actual is None:
                    return _missing(self.metric, "resource_not_observed")
                checks += int(actual <= float(expectation[expected_key]))
        if "minThroughputPerSecond" in expectation:
            total += 1
            actual = evidence.resources.measurements.get("throughput_per_second")
            if actual is None:
                return _missing(self.metric, "throughput_not_observed")
            checks += int(actual >= float(expectation["minThroughputPerSecond"]))
        return _score(self.metric, checks, total)


_FAMILIES = (
    "exact",
    "schema",
    "retrieval",
    "context",
    "tool",
    "policy",
    "groundedness",
    "response",
    "resource",
)


class DeterministicEvaluatorRegistry:
    """Finite registry; dynamic imports and caller-provided callables are absent."""

    def __init__(self) -> None:
        self._evaluators: dict[tuple[str, str], _FamilyEvaluator] = {
            (family, "1.0.0"): _FamilyEvaluator(family, f"evaluation.{family}.ratio")
            for family in _FAMILIES
        }

    def resolve(self, identifier: str, version: str) -> DeterministicEvaluator:
        family = _BUILTIN_EVALUATOR_IDS.get(identifier)
        if family is None:
            family = {
                f"aura.evaluator.{candidate}": candidate for candidate in _FAMILIES
            }.get(identifier)
        if family is None and identifier == LEGACY_EXACT_EVALUATOR_ID:
            family = "exact"
        if family is None:
            raise DefinitionValidationError("unknown deterministic evaluator")
        try:
            return self._evaluators[(family, version)]
        except KeyError as exc:
            raise DefinitionValidationError("unknown deterministic evaluator") from exc

    def resolve_definition(self, definition: EvaluatorDefinition) -> DeterministicEvaluator:
        """Resolve an admitted definition to one closed runtime evaluator."""

        if definition.kind != "deterministic":
            raise DefinitionValidationError("AURA-0093 only admits deterministic evaluators")
        evaluator = self.resolve(definition.evaluator_id, definition.evaluator_version)
        if definition.evaluator_id == LEGACY_EXACT_EVALUATOR_ID:
            compatible_metrics = {"exact_match", evaluator.metric}
        else:
            compatible_metrics = {evaluator.metric}
        if definition.metric not in compatible_metrics:
            raise DefinitionValidationError("evaluator metric is not runtime compatible")
        return evaluator

    def validate_definition(self, definition: EvaluatorDefinition) -> None:
        self.resolve_definition(definition)

    def evaluate(
        self,
        identifier: str,
        version: str,
        expectation: Mapping[str, Any],
        evidence: TypedReplayEvidence,
    ) -> EvaluationMeasurement:
        evidence.validate()
        evaluator = self.resolve(identifier, version)
        result = evaluator.evaluate(expectation, evidence)
        result.validate()
        return result

    def evaluate_case(
        self, expectations: Mapping[str, Mapping[str, Any]], evidence: TypedReplayEvidence
    ) -> tuple[EvaluationMeasurement, ...]:
        evidence.validate()
        measurements: list[EvaluationMeasurement] = []
        multi_family = len(expectations) > 1
        for family, payload in expectations.items():
            evaluator = self.resolve(f"aura.evaluator.{family}", "1.0.0")
            if isinstance(evaluator, _FamilyEvaluator):
                # Preserve the AURA-0089 multi-family aggregate projection;
                # selected retrieval execution and application persistence use
                # evaluate_many and retain every quality dimension.
                current = (
                    (evaluator.evaluate(payload, evidence),)
                    if family == "retrieval" and multi_family
                    else evaluator.evaluate_many(payload, evidence)
                )
            else:
                current = (evaluator.evaluate(payload, evidence),)
            for measurement in current:
                measurement.validate()
            measurements.extend(current)
        return tuple(measurements)


class EvaluationApplicationService:
    """Resolve immutable definitions, score typed evidence, and persist scores."""

    def __init__(
        self,
        definitions: EvaluationDefinitionRepository,
        scores: EvaluationScoreStore,
        registry: DeterministicEvaluatorRegistry | None = None,
        metrics: ObservatoryMetrics | None = None,
    ) -> None:
        self._definitions = definitions
        self._scores = scores
        self._registry = registry or DeterministicEvaluatorRegistry()
        self._metrics = metrics

    async def evaluate_case(
        self,
        *,
        case_id: str,
        case_version: str,
        suite_id: str,
        suite_version: str,
        evaluator_id: str,
        evaluator_version: str,
        context: ScoreContext,
        evidence: TypedReplayEvidence,
    ) -> tuple[EvaluationScore, ...]:
        started = monotonic()
        evaluator_recorded = False
        try:
            case = await self._definitions.get_case(case_id, case_version)
            suite = await self._definitions.get_suite(suite_id, suite_version)
            evaluator = await self._definitions.get_evaluator(evaluator_id, evaluator_version)
            if case is None or suite is None or evaluator is None:
                raise DefinitionValidationError("evaluation definition version does not exist")
            if (case.dataset.resource_id, case.dataset.version) != (
                suite.dataset.resource_id,
                suite.dataset.version,
            ):
                raise DefinitionValidationError("case and suite dataset versions differ")
            if (evaluator_id, evaluator_version) not in {
                (ref.evaluator_id, ref.evaluator_version) for ref in suite.evaluators
            }:
                raise DefinitionValidationError("evaluator is not bound to suite")
            runtime_evaluator = self._registry.resolve_definition(evaluator)
            if any(
                binding.component_id not in evaluator.component_ids
                for binding in suite.components
            ):
                raise DefinitionValidationError("evaluator does not support suite component")
            if (
                context.dataset_id != suite.dataset.resource_id
                or context.suite_id != suite.suite_id
                or context.evaluator_id != evaluator_id
                or context.evaluator_version != evaluator_version
                or context.case_id not in {None, case_id}
                or not any(
                    binding.component_id == context.component_id
                    and binding.component_version == context.component_version
                    for binding in suite.components
                )
            ):
                raise DefinitionValidationError("score context provenance is incompatible")
            if case.expectations is None:
                raise DefinitionValidationError("case has no expectations")
            evidence.validate()
            family = runtime_evaluator.family if isinstance(runtime_evaluator, _FamilyEvaluator) else ""
            expectation = case.expectations.families.get(family)
            if expectation is None:
                raise DefinitionValidationError("case has no expectation for selected evaluator")
            evaluator_compute_started = monotonic()
            if isinstance(runtime_evaluator, _FamilyEvaluator):
                measurements = runtime_evaluator.evaluate_many(expectation, evidence)
            else:
                measurements = (runtime_evaluator.evaluate(expectation, evidence),)
            evaluator_compute_ms = max(0.0, (monotonic() - evaluator_compute_started) * 1000.0)
            for measurement in measurements:
                measurement.validate()
            # Publish evaluator telemetry before any score write.  A later
            # ClickHouse failure must not erase evidence that computation ran.
            self._record_measurements(evaluator, measurements)
            self._record_span(evaluator, "success", started, evaluator_compute_ms)
            evaluator_recorded = True
            threshold = next(
                (
                    item
                    for item in suite.manual_thresholds
                    if (item.evaluator.evaluator_id, item.evaluator.evaluator_version)
                    == (evaluator_id, evaluator_version)
                    and item.metric in {runtime_evaluator.metric, evaluator.metric, "exact_match"}
                ),
                None,
            )
            persisted: list[EvaluationScore] = []
            for index, measurement in enumerate(measurements):
                metric_threshold = (
                    threshold
                    if threshold is not None
                    and measurement.metric in {threshold.metric, evaluator.metric, runtime_evaluator.metric}
                    else None
                )
                threshold_reference = (
                    f"{evaluator_id}:{evaluator_version}:{metric_threshold.metric}"
                    if metric_threshold
                    else None
                )
                threshold_status = None
                if (
                    metric_threshold is not None
                    and measurement.evidence_status is EvidenceStatus.NOT_OBSERVED
                ):
                    threshold_status = metric_threshold.missing_evidence_policy
                elif metric_threshold is not None and measurement.value is not None:
                    threshold_status = (
                        "pass"
                        if {
                            "greater_than_or_equal": measurement.value >= metric_threshold.value,
                            "less_than_or_equal": measurement.value <= metric_threshold.value,
                            "equal": measurement.value == metric_threshold.value,
                        }[metric_threshold.operator]
                        else "fail"
                    )
                score_context = ScoreContext(
                    score_id=str(uuid5(UUID(context.score_id), f"{measurement.metric}:{index}")),
                    job_id=context.job_id,
                    result_id=context.result_id,
                    dataset_id=suite.dataset.resource_id,
                    suite_id=suite.suite_id,
                    evaluator_id=evaluator_id,
                    evaluator_version=evaluator_version,
                    component_id=context.component_id,
                    component_version=context.component_version,
                    code_revision=context.code_revision,
                    image_digest=context.image_digest,
                    configuration_revision=context.configuration_revision,
                    provenance_component=context.provenance_component,
                    provenance_version=context.provenance_version,
                    provenance_digest=context.provenance_digest,
                    execution_provenance=context.execution_provenance,
                    observed_at=context.observed_at,
                    artifact_reference_id=context.artifact_reference_id,
                    case_id=case_id,
                    threshold_applicable=metric_threshold is not None,
                    threshold_reference=threshold_reference,
                    threshold_value=metric_threshold.value if metric_threshold else None,
                    threshold_operator=metric_threshold.operator if metric_threshold else None,
                    threshold_status=threshold_status,
                )
                score = to_evaluation_score(score_context, measurement)
                await self._scores.insert_score(score)
                persisted.append(score)
            return tuple(persisted)
        except DefinitionValidationError:
            if not evaluator_recorded:
                self._record_span(None, "failure", started, error_code="validation")
            raise
        except Exception:
            if evaluator_recorded:
                self._record_storage_failure(started)
            else:
                self._record_span(None, "failure", started, error_code="storage_failure")
            raise

    async def evaluate_result(
        self,
        *,
        job_id: str,
        result_id: str,
        request_data: Mapping[str, object],
        result_data: Mapping[str, object],
    ) -> tuple[EvaluationScore, ...]:
        """Score one completed runner result using its persisted bindings.

        The runner transport intentionally carries only content-free runtime
        evidence.  Until a controlled replay projection is available, an
        empty typed evidence projection is therefore used.  Deterministic
        evaluators still persist an explicit ``not_observed`` score for every
        admitted case, retaining coverage and reproducibility without
        manufacturing evidence or copying evaluation content into this
        service.

        Case versions are resolved from the exact persisted membership for the
        admitted dataset version.  A missing or inconsistent membership is a
        permanent projection validation error; a repository failure remains a
        storage error so the result consumer can retry instead of acknowledging
        a completed result with incomplete score coverage.
        """

        if result_data.get("status") != "completed":
            return ()
        raw_case_ids = request_data.get("caseIds")
        target = request_data.get("targetComponent")
        provenance = request_data.get("provenance")
        if (
            not isinstance(raw_case_ids, list)
            or not raw_case_ids
            or any(not isinstance(case_id, str) for case_id in raw_case_ids)
            or len({case_id for case_id in raw_case_ids if isinstance(case_id, str)})
            != len(raw_case_ids)
            or not isinstance(target, Mapping)
            or not isinstance(provenance, Mapping)
        ):
            raise DefinitionValidationError("result scoring bindings are invalid")
        component_id = target.get("componentId")
        component_version = target.get("componentVersion")
        code_revision = target.get("codeRevision")
        image_digest = target.get("imageDigest")
        configuration_revision = target.get("configurationRevision")
        if not all(
            isinstance(value, str)
            for value in (
                component_id,
                component_version,
                code_revision,
                image_digest,
                configuration_revision,
            )
        ):
            raise DefinitionValidationError("result scoring component is invalid")
        completed_at = result_data.get("completedAt")
        if not isinstance(completed_at, str):
            raise DefinitionValidationError("result scoring timestamp is invalid")
        try:
            observed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise DefinitionValidationError("result scoring timestamp is invalid") from error
        if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
            raise DefinitionValidationError("result scoring timestamp is invalid")
        try:
            result_uuid = UUID(result_id)
            UUID(job_id)
        except ValueError as error:
            raise DefinitionValidationError("result scoring identifiers are invalid") from error
        execution_provenance = _score_execution_provenance(provenance)
        evaluators = provenance.get("evaluators")
        if (
            not isinstance(evaluators, list)
            or not evaluators
            or not isinstance(evaluators[0], Mapping)
        ):
            raise DefinitionValidationError("result scoring evaluator is invalid")
        evaluator_reference = evaluators[0]
        evaluator_id = evaluator_reference.get("evaluatorId")
        evaluator_version = evaluator_reference.get("evaluatorVersion")
        if not isinstance(evaluator_id, str) or not isinstance(evaluator_version, str):
            raise DefinitionValidationError("result scoring evaluator is invalid")
        dataset_reference = _resource_reference(provenance, "dataset")
        suite_reference = _resource_reference(provenance, "suite")
        case_ids = tuple(cast(str, case_id) for case_id in raw_case_ids)
        case_versions = await self._definitions.case_versions_for_selection(
            dataset_reference["resourceId"],
            dataset_reference["resourceVersion"],
            case_ids,
        )
        if set(case_versions) != set(case_ids):
            raise DefinitionValidationError("result scoring case membership is invalid")
        context_base = {
            "job_id": job_id,
            "result_id": result_id,
            "dataset_id": str(dataset_reference["resourceId"]),
            "suite_id": str(suite_reference["resourceId"]),
            "evaluator_id": evaluator_id,
            "evaluator_version": evaluator_version,
            "component_id": component_id,
            "component_version": component_version,
            "code_revision": code_revision,
            "image_digest": image_digest,
            "configuration_revision": configuration_revision,
            "provenance_component": "aura.observatory.deterministic_evaluators",
            "provenance_version": "1.0.0",
            "provenance_digest": None,
            "execution_provenance": execution_provenance,
            "observed_at": observed_at.astimezone(UTC),
        }
        persisted: list[EvaluationScore] = []
        for raw_case_id in case_ids:
            case = await self._definitions.get_case(raw_case_id, case_versions[raw_case_id])
            if case is None:
                raise DefinitionValidationError("result scoring case definition is missing")
            context = ScoreContext(
                score_id=str(uuid5(result_uuid, f"case:{raw_case_id}")),
                case_id=raw_case_id,
                dataset_id=context_base["dataset_id"],
                suite_id=context_base["suite_id"],
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                component_id=component_id,
                component_version=component_version,
                code_revision=code_revision,
                image_digest=image_digest,
                configuration_revision=configuration_revision,
                provenance_component=context_base["provenance_component"],
                provenance_version=context_base["provenance_version"],
                provenance_digest=None,
                execution_provenance=execution_provenance,
                observed_at=observed_at.astimezone(UTC),
                job_id=job_id,
                result_id=result_id,
            )
            persisted.extend(
                await self.evaluate_case(
                    case_id=case.case_id,
                    case_version=case.version,
                    suite_id=context.suite_id,
                    suite_version=str(suite_reference["resourceVersion"]),
                    evaluator_id=evaluator_id,
                    evaluator_version=evaluator_version,
                    context=context,
                    evidence=TypedReplayEvidence(),
                )
            )
        return tuple(persisted)

    def _record_measurements(
        self, evaluator: EvaluatorDefinition, measurements: tuple[EvaluationMeasurement, ...]
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.record(
            "observatory_evaluator_execution_count",
            labels={"evaluator_kind": evaluator.kind, "outcome": "success"},
        )
        for measurement in measurements:
            self._metrics.record(
                "observatory_evaluator_outcome_count",
                labels={
                    "evaluator_kind": evaluator.kind,
                    "outcome": measurement.outcome.value if measurement.outcome else "unknown",
                },
            )
            if measurement.evidence_status is EvidenceStatus.NOT_OBSERVED:
                self._metrics.record(
                    "observatory_evaluator_incomplete_evidence_count",
                    labels={
                        "evaluator_kind": evaluator.kind,
                        "evidence_status": measurement.evidence_status.value,
                    },
                )

    def _record_storage_failure(self, started: float) -> None:
        if self._metrics is None:
            return
        self._metrics.record(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": "clickhouse", "outcome": "failure"},
        )
        self._metrics.record_component_span(
            component_id="aura.observatory.deterministic_evaluators",
            component_version="1.0.0",
            deployment_id=self._metrics.deployment_id,
            operation="write",
            duration_ms=max(0.0, (monotonic() - started) * 1000.0),
            outcome="failure",
            dependency="clickhouse",
            error_code="storage_failure",
        )

    def _record_span(
        self,
        evaluator: EvaluatorDefinition | None,
        outcome: str,
        started: float,
        evaluator_duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.record_component_span(
            component_id="aura.observatory.deterministic_evaluators",
            component_version="1.0.0",
            deployment_id=self._metrics.deployment_id,
            operation="evaluate",
            duration_ms=max(0.0, (monotonic() - started) * 1000.0),
            outcome=outcome,
            dependency="evaluation_definition_state",
            error_code=error_code,
        )
        if evaluator is not None:
            self._metrics.record(
                "observatory_evaluator_duration_ms",
                evaluator_duration_ms if evaluator_duration_ms is not None else 0.0,
                {"evaluator_kind": evaluator.kind},
            )


def _resource_reference(provenance: Mapping[str, object], name: str) -> dict[str, str]:
    value = provenance.get(name)
    if not isinstance(value, Mapping):
        raise DefinitionValidationError(f"result scoring {name} reference is invalid")
    identifier = value.get("id")
    version = value.get("version")
    if not isinstance(identifier, str) or not isinstance(version, str):
        raise DefinitionValidationError(f"result scoring {name} reference is invalid")
    return {"resourceId": identifier, "resourceVersion": version}


def _score_execution_provenance(provenance: Mapping[str, object]) -> dict[str, object]:
    """Map the runner's v1 provenance spelling to the score projection."""

    dataset = _resource_reference(provenance, "dataset")
    suite = _resource_reference(provenance, "suite")
    evaluators = provenance.get("evaluators")
    components = provenance.get("components")
    model = provenance.get("model")
    deployment = provenance.get("deploymentRevision")
    reproducibility = provenance.get("reproducibility")
    if not all(
        isinstance(value, list) and value for value in (evaluators, components)
    ) or not isinstance(model, Mapping) or not isinstance(deployment, str) or not isinstance(
        reproducibility, Mapping
    ):
        raise DefinitionValidationError("result scoring provenance is invalid")
    return {
        "dataset": dataset,
        "suite": suite,
        "evaluators": list(evaluators),
        "components": list(components),
        "model": dict(model),
        "deploymentRevision": deployment,
        "reproducibility": dict(reproducibility),
    }


BUILTIN_EVALUATOR_IDS = _BUILTIN_EVALUATOR_IDS
BUILTIN_EVALUATOR_DEFINITIONS: Mapping[str, tuple[str, str]] = {
    family: (identifier, "1.0.0")
    for identifier, family in BUILTIN_EVALUATOR_IDS.items()
}


__all__ = [
    "BUILTIN_EVALUATOR_DEFINITIONS",
    "BUILTIN_EVALUATOR_IDS",
    "ClaimObservation",
    "DeterministicEvaluator",
    "EvaluationApplicationService",
    "DeterministicEvaluatorRegistry",
    "EvaluationMeasurement",
    "EvidenceStatus",
    "PolicyObservation",
    "ResourceProjection",
    "ResponseProjection",
    "RetrievedEvidence",
    "ScoreContext",
    "ScoreOutcome",
    "ToolObservation",
    "TypedReplayEvidence",
    "to_evaluation_score",
]
