"""Deterministic calibration, human review, qualification and ensembles.

Calibration records contain only opaque case/result references and bounded
labels or scores.  They intentionally do not contain reviewer identity,
comments, prompt text, model output, or provider configuration.
"""

# Calibration values are runtime-validated closed records; suppress only
# pyright's redundant narrowing diagnostics for those guards.
# pyright: reportUnnecessaryIsInstance=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportGeneralTypeIssues=false

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aura_observatory.evaluation_judges import JudgeAdvisoryState, JudgeOutcome

_LOGGER = logging.getLogger(__name__)

MIN_CALIBRATION_LABELS = 20
MIN_AGREEMENT = 0.80
MAX_MEAN_ABSOLUTE_ERROR = 0.15
MAX_DISAGREEMENT = 0.20
QUALIFICATION_OUTCOMES = frozenset({"qualified", "revoked"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}(?::[A-Za-z0-9][A-Za-z0-9._+-]{0,63})?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# Judge output labels are pass/fail/inconclusive; owner reference and review
# labels additionally use the contract's accepted/rejected/needs_review terms.
_LABELS = frozenset({"pass", "fail", "inconclusive", "accepted", "rejected", "needs_review"})
_BUDGET_KEYS = frozenset(
    {
        "timeoutMs",
        "maxInputBytes",
        "maxOutputBytes",
        "maxOutputTokens",
        "maxContextTokens",
        "maxConcurrency",
        "temperature",
    }
)


def _validate_complete_budget(value: Mapping[str, int | float]) -> None:
    """Validate the canonical, lossless execution budget projection."""

    if frozenset(value) != _BUDGET_KEYS:
        raise CalibrationError("validation")
    integer_limits = {
        "timeoutMs": (100, 120_000),
        "maxInputBytes": (1, 1_048_576),
        "maxOutputBytes": (1, 8_192),
        "maxOutputTokens": (1, 2_048),
        "maxContextTokens": (1, 16_384),
        "maxConcurrency": (1, 2),
    }
    for key, (lower, upper) in integer_limits.items():
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or not lower <= item <= upper:
            raise CalibrationError("validation")
    temperature = value.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or float(temperature) != 0.0
    ):
        raise CalibrationError("validation")


def _utc_timestamp(value: datetime) -> str:
    """Render an aware UTC instant in evaluation-v1's canonical ``Z`` form."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise CalibrationError("validation")
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")[:-6]
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered + "Z"


class CalibrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise CalibrationError("validation")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise CalibrationError("validation") from exc
    return value


def _version(value: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise CalibrationError("validation")
    return value


def _bounded_score(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError("validation")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise CalibrationError("validation")
    return round(number, 6)


def _record_fingerprint(item: object) -> str:
    return hashlib.sha256(repr(item).encode("utf-8")).hexdigest()


def _qualification_fingerprint(item: Qualification) -> str:
    """Fingerprint the immutable qualification action, excluding revocation state."""

    value = {
        "qualificationId": item.qualification_id,
        "qualificationVersion": item.qualification_version,
        "idempotencyKey": item.idempotency_key,
        "authorizationRecordId": item.authorization_record_id,
        "suiteId": item.suite_id,
        "suiteVersion": item.suite_version,
        "datasetId": item.dataset_id,
        "datasetVersion": item.dataset_version,
        "evaluatorId": item.evaluator_id,
        "evaluatorVersion": item.evaluator_version,
        "judgeId": item.judge_id,
        "judgeVersion": item.judge_version,
        "promptVersion": item.prompt_version,
        "rubricVersion": item.rubric_version,
        "calibrationVersion": item.calibration_version,
        "calibrationId": item.calibration_id,
        "calibrationResultId": item.calibration_result_id,
        "modelName": item.model_name,
        "modelVersion": item.model_version,
        "modelDigest": item.model_digest,
        "promptArtifactId": item.prompt_artifact_reference_id,
        "promptArtifactVersion": item.prompt_artifact_version,
        "promptArtifactDigest": item.prompt_artifact_digest,
        "rubricArtifactId": item.rubric_artifact_reference_id,
        "rubricArtifactVersion": item.rubric_artifact_version,
        "rubricArtifactDigest": item.rubric_artifact_digest,
        "calibrationEligibility": item.calibration_eligibility,
        "seed": item.seed,
        "budget": dict(item.budget),
        "reason": item.reason,
        "retentionMode": item.retention_mode,
        "provenanceComponent": item.provenance_component,
        "provenanceVersion": item.provenance_version,
        "provenanceDigest": item.provenance_digest,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _revocation_fingerprint(
    qualification_id: str, authorization_record_id: str, revoked_at: datetime
) -> str:
    """Fingerprint the complete, content-free revocation request."""

    value = "|".join(
        (
            "revoke_qualification",
            qualification_id,
            authorization_record_id,
            _utc_timestamp(revoked_at),
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


class DisagreementClass(StrEnum):
    NONE = "none"
    WITHIN_TOLERANCE = "within_tolerance"
    SCORE_DISAGREEMENT = "score_disagreement"
    OUTCOME_DISAGREEMENT = "outcome_disagreement"
    SCORE = "score"
    LABEL = "label"
    EVIDENCE = "evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class CalibrationLabel:
    label_id: str
    case_id: str
    judge_id: str
    judge_version: str
    prompt_version: str
    rubric_version: str
    calibration_version: str
    reference_score: float
    reference_label: str
    judge_score: float | None = None
    judge_label: str | None = None
    comment_artifact_reference_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    label_version: str = "1.0.0"
    idempotency_key: str | None = None
    authorization_record_id: str | None = None
    retention_mode: str = "manual_only"
    provenance: str = "owner_label"
    calibration_id: str | None = None
    result_id: str | None = None
    reference_outcome: JudgeOutcome | None = None
    comment_artifact_version: str | None = None
    comment_artifact_digest: str | None = None

    def validate(self, *, strict: bool = False) -> None:
        for value in (self.label_id, self.case_id, self.judge_id):
            _uuid(value)
        for value in (
            self.judge_version,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
        ):
            _version(value)
        _version(self.label_version)
        _bounded_score(self.reference_score)
        if self.reference_label not in _LABELS:
            raise CalibrationError("validation")
        if self.judge_score is not None:
            _bounded_score(self.judge_score)
        if self.judge_label is not None and self.judge_label not in _LABELS:
            raise CalibrationError("validation")
        if self.comment_artifact_reference_id is not None:
            _uuid(self.comment_artifact_reference_id)
            if self.comment_artifact_version is not None:
                _version(self.comment_artifact_version)
            if self.comment_artifact_digest is not None and not _DIGEST.fullmatch(
                self.comment_artifact_digest
            ):
                raise CalibrationError("validation")
        for value in (self.idempotency_key, self.authorization_record_id):
            if value is None:
                raise CalibrationError("authorization_required")
            _uuid(value)
        if self.retention_mode != "manual_only" or not _ID.fullmatch(self.provenance):
            raise CalibrationError("validation")
        for value in (self.calibration_id, self.result_id):
            if value is not None:
                _uuid(value)
        if self.reference_outcome is not None and self.reference_outcome not in set(JudgeOutcome):
            raise CalibrationError("validation")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != UTC.utcoffset(
            self.created_at
        ):
            raise CalibrationError("validation")
        if strict:
            if self.reference_label not in {"accepted", "rejected", "needs_review"}:
                raise CalibrationError("validation")
            if self.judge_label is not None and self.judge_label not in {
                "accepted",
                "rejected",
                "needs_review",
            }:
                raise CalibrationError("validation")
            if (
                self.reference_outcome is None
                or self.calibration_id is None
                or self.result_id is None
            ):
                raise CalibrationError("validation")
            if self.comment_artifact_reference_id is not None and (
                self.comment_artifact_version is None or self.comment_artifact_digest is None
            ):
                raise CalibrationError("validation")

    def as_contract(self) -> dict[str, object]:
        self.validate(strict=True)
        value: dict[str, object] = {
            "schemaVersion": 1,
            "resourceType": "calibration_label",
            "labelId": self.label_id,
            "labelVersion": self.label_version,
            "idempotencyKey": self.idempotency_key,
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "caseId": self.case_id,
            "resultId": self.result_id,
            "referenceScore": self.reference_score,
            "referenceOutcome": self.reference_outcome.value if self.reference_outcome else None,
            "referenceLabel": self.reference_label,
            "authorizationRecordId": self.authorization_record_id,
            "retentionMode": self.retention_mode,
            "provenance": {
                "createdByComponent": "aura.observatory.calibration",
                "createdByVersion": self.label_version,
                "sourceDigest": _record_fingerprint(self),
            },
            "createdAt": _utc_timestamp(self.created_at),
        }
        if self.comment_artifact_reference_id is not None:
            value["commentArtifact"] = {
                "artifactId": self.comment_artifact_reference_id,
                "artifactVersion": self.comment_artifact_version,
                "purpose": "human_review_comment",
                "digest": self.comment_artifact_digest,
            }
        return value


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    judge_id: str
    judge_version: str
    prompt_version: str
    rubric_version: str
    calibration_version: str
    sample_count: int
    agreement: float
    mean_absolute_error: float
    disagreement_rate: float
    eligible: bool
    threshold_agreement: float = MIN_AGREEMENT
    threshold_mean_absolute_error: float = MAX_MEAN_ABSOLUTE_ERROR
    threshold_disagreement_rate: float = MAX_DISAGREEMENT
    dataset_id: str | None = None
    dataset_version: str | None = None
    suite_id: str | None = None
    suite_version: str | None = None
    evaluator_id: str | None = None
    evaluator_version: str | None = None
    calibration_result_id: str | None = None
    calibration_id: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_digest: str | None = None
    seed: int = 0
    budget: Mapping[str, int | float] = field(default_factory=dict)
    score_disposition: str = "advisory_only"
    retention_mode: str = "manual_only"
    provenance: str = "local_calibration"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    prompt_artifact_reference_id: str | None = None
    prompt_artifact_version: str | None = None
    prompt_artifact_digest: str | None = None
    rubric_artifact_reference_id: str | None = None
    rubric_artifact_version: str | None = None
    rubric_artifact_digest: str | None = None
    provenance_component: str | None = None
    provenance_version: str | None = None
    provenance_digest: str | None = None

    def validate(self, *, strict: bool = False) -> None:
        _uuid(self.judge_id)
        for value in (
            self.judge_version,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
        ):
            _version(value)
        if not isinstance(self.sample_count, int) or not 0 <= self.sample_count <= 100_000:
            raise CalibrationError("validation")
        for value in (
            self.agreement,
            self.mean_absolute_error,
            self.disagreement_rate,
            self.threshold_agreement,
            self.threshold_mean_absolute_error,
            self.threshold_disagreement_rate,
        ):
            _bounded_score(value)
        for value in (
            self.dataset_id,
            self.suite_id,
            self.evaluator_id,
            self.calibration_result_id,
            self.calibration_id,
        ):
            if value is not None:
                _uuid(value)
        for value in (
            self.dataset_version,
            self.suite_version,
            self.evaluator_version,
            self.model_version,
        ):
            if value is not None:
                _version(value)
        if self.model_name is not None and not _MODEL.fullmatch(self.model_name):
            raise CalibrationError("validation")
        if self.model_digest is not None:
            if not _DIGEST.fullmatch(self.model_digest):
                raise CalibrationError("validation")
        for value in (
            self.prompt_artifact_reference_id,
            self.rubric_artifact_reference_id,
        ):
            if value is not None:
                _uuid(value)
        for value in (self.prompt_artifact_version, self.rubric_artifact_version):
            if value is not None:
                _version(value)
        for value in (self.prompt_artifact_digest, self.rubric_artifact_digest):
            if value is not None and not _DIGEST.fullmatch(value):
                raise CalibrationError("validation")
        if self.provenance_component is not None and not self.provenance_component.startswith(
            "aura."
        ):
            raise CalibrationError("validation")
        if self.provenance_version is not None:
            _version(self.provenance_version)
        if self.provenance_digest is not None and not _DIGEST.fullmatch(self.provenance_digest):
            raise CalibrationError("validation")
        if (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not 0 <= self.seed <= 2_147_483_647
        ):
            raise CalibrationError("validation")
        if self.score_disposition not in {"advisory_only", "calibrated_enabled"}:
            raise CalibrationError("validation")
        if self.retention_mode != "manual_only" or not _ID.fullmatch(self.provenance):
            raise CalibrationError("validation")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != UTC.utcoffset(
            self.created_at
        ):
            raise CalibrationError("validation")
        if strict:
            if self.score_disposition != "advisory_only":
                raise CalibrationError("validation")
            required = (
                self.dataset_id,
                self.dataset_version,
                self.suite_id,
                self.suite_version,
                self.evaluator_id,
                self.evaluator_version,
                self.calibration_result_id,
                self.calibration_id,
                self.model_name,
                self.model_version,
                self.model_digest,
                self.prompt_artifact_reference_id,
                self.prompt_artifact_version,
                self.prompt_artifact_digest,
                self.rubric_artifact_reference_id,
                self.rubric_artifact_version,
                self.rubric_artifact_digest,
                self.provenance_component,
                self.provenance_version,
                self.provenance_digest,
            )
            if any(value is None for value in required):
                raise CalibrationError("validation")
            _validate_complete_budget(self.budget)

    def as_contract(self) -> dict[str, object]:
        self.validate(strict=True)
        return {
            "schemaVersion": 1,
            "resourceType": "calibration_result",
            "calibrationResultId": self.calibration_result_id,
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "judgeDefinition": {
                "judgeDefinitionId": self.judge_id,
                "judgeDefinitionVersion": self.judge_version,
            },
            "model": {
                "provider": "local_ollama",
                "modelName": self.model_name,
                "modelVersion": self.model_version,
                "modelDigest": self.model_digest,
            },
            "prompt": {
                "artifactId": self.prompt_artifact_reference_id,
                "artifactVersion": self.prompt_artifact_version,
                "purpose": "judge_prompt",
                "digest": self.prompt_artifact_digest,
            },
            "rubric": {
                "artifactId": self.rubric_artifact_reference_id,
                "artifactVersion": self.rubric_artifact_version,
                "purpose": "judge_rubric",
                "digest": self.rubric_artifact_digest,
            },
            "seed": self.seed,
            "budget": dict(self.budget),
            "sampleCount": self.sample_count,
            "agreementTolerance": 0.1,
            "agreementRate": self.agreement,
            "meanAbsoluteError": self.mean_absolute_error,
            "disagreementRate": self.disagreement_rate,
            "eligibility": "eligible" if self.eligible else "ineligible",
            "scoreDisposition": "advisory_only",
            "retentionMode": self.retention_mode,
            "provenance": {
                "createdByComponent": self.provenance_component,
                "createdByVersion": self.provenance_version,
                "sourceDigest": self.provenance_digest,
            },
            "createdAt": _utc_timestamp(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class CalibrationExecutionClaim:
    calibration_id: str
    calibration_version: str
    fingerprint: str
    status: str = "claimed"

    def validate(self, *, strict: bool = False) -> None:
        _uuid(self.calibration_id)
        _version(self.calibration_version)
        if not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise CalibrationError("validation")
        if self.status not in {"claimed", "completed", "failed"}:
            raise CalibrationError("validation")


@dataclass(frozen=True, slots=True)
class HumanReview:
    review_id: str
    case_id: str
    result_id: str
    judge_id: str
    judge_version: str
    prompt_version: str
    rubric_version: str
    calibration_version: str
    label: str
    score: float
    disagreement: DisagreementClass
    comment_artifact_reference_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    review_version: str = "1.0.0"
    idempotency_key: str | None = None
    authorization_record_id: str | None = None
    outcome: JudgeOutcome | None = None
    retention_mode: str = "manual_only"
    provenance: str = "owner_review"
    calibration_id: str | None = None
    score_id: str | None = None
    review_label: str | None = None
    comment_artifact_version: str | None = None
    comment_artifact_digest: str | None = None
    # Exact owner-supplied review provenance. Durable query projections do not
    # borrow calibration provenance when these fields are absent.
    provenance_component: str | None = None
    provenance_version: str | None = None
    provenance_digest: str | None = None

    def validate(self, *, strict: bool = False) -> None:
        for value in (self.review_id, self.case_id, self.result_id, self.judge_id):
            _uuid(value)
        for value in (
            self.judge_version,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
        ):
            _version(value)
        _version(self.review_version)
        if self.label not in _LABELS:
            raise CalibrationError("validation")
        _bounded_score(self.score)
        if self.disagreement not in set(DisagreementClass):
            raise CalibrationError("validation")
        if self.comment_artifact_reference_id is not None:
            _uuid(self.comment_artifact_reference_id)
            if self.comment_artifact_version is not None:
                _version(self.comment_artifact_version)
            if self.comment_artifact_digest is not None and not _DIGEST.fullmatch(
                self.comment_artifact_digest
            ):
                raise CalibrationError("validation")
        if self.idempotency_key is None or self.authorization_record_id is None:
            raise CalibrationError("authorization_required")
        _uuid(self.idempotency_key)
        _uuid(self.authorization_record_id)
        if self.outcome is not None and self.outcome not in set(JudgeOutcome):
            raise CalibrationError("validation")
        if self.retention_mode != "manual_only" or not _ID.fullmatch(self.provenance):
            raise CalibrationError("validation")
        for value in (self.calibration_id, self.score_id):
            if value is not None:
                _uuid(value)
        if self.review_label is not None and self.review_label not in {
            "accepted",
            "rejected",
            "needs_review",
        }:
            raise CalibrationError("validation")
        if strict:
            if self.outcome is None or self.review_label is None:
                raise CalibrationError("validation")
            if self.review_label not in {"accepted", "rejected", "needs_review"}:
                raise CalibrationError("validation")
            if self.disagreement not in {
                DisagreementClass.NONE,
                DisagreementClass.WITHIN_TOLERANCE,
                DisagreementClass.SCORE_DISAGREEMENT,
                DisagreementClass.OUTCOME_DISAGREEMENT,
                DisagreementClass.INSUFFICIENT_EVIDENCE,
            }:
                raise CalibrationError("validation")
            if self.calibration_id is None or self.score_id is None:
                raise CalibrationError("validation")
            if self.comment_artifact_reference_id is not None and (
                self.comment_artifact_version is None or self.comment_artifact_digest is None
            ):
                raise CalibrationError("validation")

    def as_contract(self) -> dict[str, object]:
        self.validate(strict=True)
        value: dict[str, object] = {
            "schemaVersion": 1,
            "resourceType": "human_review",
            "reviewId": self.review_id,
            "reviewVersion": self.review_version,
            "idempotencyKey": self.idempotency_key,
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "caseId": self.case_id,
            "resultId": self.result_id,
            "scoreId": self.score_id,
            "reviewScore": self.score,
            "reviewOutcome": self.outcome.value if self.outcome else None,
            "reviewLabel": self.review_label,
            "disagreement": self.disagreement.value,
            "authorizationRecordId": self.authorization_record_id,
            "retentionMode": self.retention_mode,
            "provenance": {
                "createdByComponent": "aura.observatory.calibration",
                "createdByVersion": self.review_version,
                "sourceDigest": _record_fingerprint(self),
            },
            "createdAt": _utc_timestamp(self.created_at),
        }
        if self.comment_artifact_reference_id is not None:
            value["commentArtifact"] = {
                "artifactId": self.comment_artifact_reference_id,
                "artifactVersion": self.comment_artifact_version,
                "purpose": "human_review_comment",
                "digest": self.comment_artifact_digest,
            }
        return value


@dataclass(frozen=True, slots=True)
class Qualification:
    qualification_id: str
    suite_id: str
    suite_version: str
    dataset_id: str
    dataset_version: str
    evaluator_id: str
    evaluator_version: str
    judge_id: str
    judge_version: str
    prompt_version: str
    rubric_version: str
    calibration_version: str
    qualified: bool = True
    reason: str = "manual_owner_qualification"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    qualification_version: str = "1.0.0"
    idempotency_key: str | None = None
    authorization_record_id: str | None = None
    calibration_result_id: str | None = None
    state: str = "qualified"
    action_mode: str = "manual_owner_action"
    score_disposition: str = "calibrated_enabled"
    retention_mode: str = "manual_only"
    model_name: str | None = None
    model_version: str | None = None
    model_digest: str | None = None
    prompt_artifact_reference_id: str | None = None
    rubric_artifact_reference_id: str | None = None
    calibration_eligibility: str = "eligible"
    revocation_record_id: str | None = None
    qualified_at: datetime | None = None
    prompt_artifact_version: str | None = None
    prompt_artifact_digest: str | None = None
    rubric_artifact_version: str | None = None
    rubric_artifact_digest: str | None = None
    seed: int = 0
    budget: Mapping[str, int | float] = field(default_factory=dict)
    provenance_component: str | None = None
    provenance_version: str | None = None
    provenance_digest: str | None = None
    calibration_id: str | None = None

    def validate(self, *, strict: bool = False) -> None:
        for value in (
            self.qualification_id,
            self.suite_id,
            self.dataset_id,
            self.evaluator_id,
            self.judge_id,
        ):
            _uuid(value)
        for value in (
            self.suite_version,
            self.dataset_version,
            self.evaluator_version,
            self.judge_version,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
        ):
            _version(value)
        _version(self.qualification_version)
        if not self.reason or len(self.reason) > 64 or not _ID.fullmatch(self.reason):
            raise CalibrationError("validation")
        if self.revoked_at is not None and (
            self.revoked_at.tzinfo is None
            or self.revoked_at.utcoffset() != UTC.utcoffset(self.revoked_at)
        ):
            raise CalibrationError("validation")
        if self.qualified_at is not None and (
            self.qualified_at.tzinfo is None
            or self.qualified_at.utcoffset() != UTC.utcoffset(self.qualified_at)
        ):
            raise CalibrationError("validation")
        if self.idempotency_key is None or self.authorization_record_id is None:
            raise CalibrationError("authorization_required")
        for value in (
            self.idempotency_key,
            self.authorization_record_id,
            self.calibration_result_id,
            self.calibration_id,
        ):
            if value is not None:
                _uuid(value)
        for value in (self.prompt_artifact_version, self.rubric_artifact_version):
            if value is not None:
                _version(value)
        for value in (self.prompt_artifact_digest, self.rubric_artifact_digest):
            if value is not None and not _DIGEST.fullmatch(value):
                raise CalibrationError("validation")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= 2_147_483_647
        ):
            raise CalibrationError("validation")
        if not isinstance(self.budget, Mapping) or len(self.budget) > 16:
            raise CalibrationError("validation")
        for key, value in self.budget.items():
            if (
                not isinstance(key, str)
                or not _ID.fullmatch(key)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise CalibrationError("validation")
        if self.provenance_component is not None and not self.provenance_component.startswith(
            "aura."
        ):
            raise CalibrationError("validation")
        if self.provenance_version is not None:
            _version(self.provenance_version)
        if self.provenance_digest is not None and not _DIGEST.fullmatch(self.provenance_digest):
            raise CalibrationError("validation")
        if self.state not in QUALIFICATION_OUTCOMES:
            raise CalibrationError("validation")
        if self.action_mode != "manual_owner_action" or self.score_disposition not in {
            "advisory_only",
            "calibrated_enabled",
        }:
            raise CalibrationError("validation")
        if self.retention_mode != "manual_only":
            raise CalibrationError("validation")
        if self.qualified and self.state != "qualified":
            raise CalibrationError("validation")
        if not self.qualified and self.state != "revoked":
            raise CalibrationError("validation")
        if self.qualified and self.score_disposition != "calibrated_enabled":
            raise CalibrationError("validation")
        if not self.qualified and self.score_disposition != "advisory_only":
            raise CalibrationError("validation")
        if self.calibration_eligibility not in {"eligible", "ineligible"}:
            raise CalibrationError("validation")
        for value in (
            self.prompt_artifact_reference_id,
            self.rubric_artifact_reference_id,
            self.revocation_record_id,
        ):
            if value is not None:
                _uuid(value)
        if strict:
            required = (
                self.model_name,
                self.model_version,
                self.model_digest,
                self.prompt_artifact_reference_id,
                self.prompt_artifact_version,
                self.prompt_artifact_digest,
                self.rubric_artifact_reference_id,
                self.rubric_artifact_version,
                self.rubric_artifact_digest,
                self.calibration_result_id,
                self.calibration_id,
                self.provenance_component,
                self.provenance_version,
                self.provenance_digest,
            )
            if any(value is None for value in required):
                raise CalibrationError("validation")
            _validate_complete_budget(self.budget)
            if self.calibration_eligibility != "eligible":
                raise CalibrationError("validation")
            if self.qualified and self.qualified_at is None:
                raise CalibrationError("validation")
            if not self.qualified and (
                self.revocation_record_id is None or self.revoked_at is None
            ):
                raise CalibrationError("validation")

    def as_contract(self) -> dict[str, object]:
        self.validate(strict=True)
        value: dict[str, object] = {
            "schemaVersion": 1,
            "resourceType": "judge_qualification",
            "qualificationId": self.qualification_id,
            "qualificationVersion": self.qualification_version,
            "idempotencyKey": self.idempotency_key,
            "state": self.state,
            "actionMode": self.action_mode,
            "dataset": {"id": self.dataset_id, "version": self.dataset_version},
            "suite": {"id": self.suite_id, "version": self.suite_version},
            "evaluator": {
                "evaluatorId": self.evaluator_id,
                "evaluatorVersion": self.evaluator_version,
                "kind": "local_judge",
            },
            "judgeDefinition": {
                "judgeDefinitionId": self.judge_id,
                "judgeDefinitionVersion": self.judge_version,
            },
            "model": {
                "provider": "local_ollama",
                "modelName": self.model_name,
                "modelVersion": self.model_version,
                "modelDigest": self.model_digest,
            },
            "prompt": {
                "artifactId": self.prompt_artifact_reference_id,
                "artifactVersion": self.prompt_artifact_version,
                "purpose": "judge_prompt",
                "digest": self.prompt_artifact_digest,
            },
            "rubric": {
                "artifactId": self.rubric_artifact_reference_id,
                "artifactVersion": self.rubric_artifact_version,
                "purpose": "judge_rubric",
                "digest": self.rubric_artifact_digest,
            },
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "calibrationResultId": self.calibration_result_id,
            "calibrationEligibility": self.calibration_eligibility,
            "authorizationRecordId": self.authorization_record_id,
            "scoreDisposition": self.score_disposition,
            "qualifiedAt": _utc_timestamp(self.qualified_at) if self.qualified_at else None,
            "retentionMode": self.retention_mode,
            "provenance": {
                "createdByComponent": self.provenance_component,
                "createdByVersion": self.provenance_version,
                "sourceDigest": self.provenance_digest,
            },
            "createdAt": _utc_timestamp(self.created_at),
        }
        if self.revocation_record_id is not None:
            value["revocationRecordId"] = self.revocation_record_id
        if self.revoked_at is not None:
            value["revokedAt"] = _utc_timestamp(self.revoked_at)
        return value

    @property
    def active(self) -> bool:
        return self.qualified and self.revoked_at is None


@dataclass(frozen=True, slots=True)
class EnsembleMember:
    member_id: str
    judge_id: str
    judge_version: str
    # Runtime admission remains integer-only; the union keeps legacy callers
    # type-checkable so they receive the bounded validation error at runtime.
    weight: int | float
    advisory_state: JudgeAdvisoryState
    score: float | None
    outcome: JudgeOutcome
    calibration_version: str
    qualification_id: str | None = None

    def validate(self) -> None:
        if not _ID.fullmatch(self.member_id):
            raise CalibrationError("validation")
        _uuid(self.judge_id)
        _version(self.judge_version)
        _version(self.calibration_version)
        if self.qualification_id is not None:
            _uuid(self.qualification_id)
        if (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, int)
            or not 0 < self.weight <= 1_000_000
        ):
            raise CalibrationError("validation")
        if self.score is not None:
            _bounded_score(self.score)
        if self.outcome not in set(JudgeOutcome) or self.advisory_state not in set(
            JudgeAdvisoryState
        ):
            raise CalibrationError("validation")


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    ensemble_id: str
    score: float | None
    outcome: JudgeOutcome
    advisory_state: JudgeAdvisoryState
    member_ids: tuple[str, ...]
    failure_code: str | None = None


class InMemoryCalibrationRepository:
    """Reference repository with immutable/idempotent state for worker tests."""

    def __init__(self) -> None:
        self.labels: dict[str, CalibrationLabel] = {}
        self.reviews: dict[str, HumanReview] = {}
        self.qualifications: dict[str, Qualification] = {}
        self.calibration_claims: dict[tuple[str, str], CalibrationExecutionClaim] = {}
        self.calibration_results: dict[str, CalibrationReport] = {}
        self.revocation_idempotency: dict[str, tuple[str, str]] = {}
        self.audits: list[dict[str, str]] = []

    async def put_label(self, label: CalibrationLabel) -> CalibrationLabel:
        label.validate()
        return self._put(self.labels, label, "label")

    async def put_review(self, review: HumanReview) -> HumanReview:
        review.validate()
        return self._put(self.reviews, review, "review")

    async def put_qualification(self, qualification: Qualification) -> Qualification:
        qualification.validate()
        current = self.qualifications.get(qualification.qualification_id)
        if current is not None:
            if _qualification_fingerprint(current) != _qualification_fingerprint(qualification):
                self.audits.append({"kind": "qualification", "outcome": "conflict"})
                raise CalibrationError("idempotency_conflict")
            self.audits.append({"kind": "qualification", "outcome": "duplicate"})
            return current
        self.qualifications[qualification.qualification_id] = qualification
        self.audits.append({"kind": "qualification", "outcome": "accepted"})
        return qualification

    def _put(self, store: dict[str, Any], item: Any, kind: str) -> Any:
        key = (
            item.label_id
            if kind == "label"
            else item.review_id
            if kind == "review"
            else item.qualification_id
        )
        current = store.get(key)
        if current is not None and current != item:
            self.audits.append({"kind": kind, "outcome": "conflict"})
            raise CalibrationError("idempotency_conflict")
        if current is not None:
            self.audits.append({"kind": kind, "outcome": "duplicate"})
            return current
        store[key] = item
        self.audits.append({"kind": kind, "outcome": "accepted"})
        return item

    async def list_labels(
        self,
        *,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
    ) -> list[CalibrationLabel]:
        return [
            item
            for item in self.labels.values()
            if (
                item.judge_id,
                item.judge_version,
                item.prompt_version,
                item.rubric_version,
                item.calibration_version,
            )
            == (
                judge_id,
                judge_version,
                prompt_version,
                rubric_version,
                calibration_version,
            )
        ]

    async def get_qualification(self, qualification_id: str) -> Qualification | None:
        return self.qualifications.get(qualification_id)

    async def active_qualification(
        self, **scope: str | int | Mapping[str, int | float]
    ) -> Qualification | None:
        aliases = {
            "instruction_version": "prompt_version",
            "prompt_artifact_id": "prompt_artifact_reference_id",
            "rubric_artifact_id": "rubric_artifact_reference_id",
        }
        for item in self.qualifications.values():
            if item.active and all(
                getattr(item, aliases.get(key, key), object()) == value
                for key, value in scope.items()
            ):
                return item
        return None

    async def claim_calibration(
        self, claim: CalibrationExecutionClaim
    ) -> CalibrationExecutionClaim | None:
        claim.validate()
        key = (claim.calibration_id, claim.calibration_version)
        current = self.calibration_claims.get(key)
        if current is not None:
            if current.fingerprint != claim.fingerprint:
                raise CalibrationError("idempotency_conflict")
            return None
        self.calibration_claims[key] = claim
        return claim

    async def put_calibration_result(self, result: CalibrationReport) -> CalibrationReport:
        if result.calibration_result_id is None:
            raise CalibrationError("validation")
        _uuid(result.calibration_result_id)
        current = self.calibration_results.get(result.calibration_result_id)
        if current is not None and current != result:
            raise CalibrationError("idempotency_conflict")
        self.calibration_results[result.calibration_result_id] = result
        return result

    async def fail_calibration(self, claim: CalibrationExecutionClaim) -> None:
        claim.validate()
        key = (claim.calibration_id, claim.calibration_version)
        current = self.calibration_claims.get(key)
        if current is not None and current.fingerprint == claim.fingerprint:
            self.calibration_claims[key] = replace(current, status="failed")

    async def get_calibration_result(self, result_id: str) -> CalibrationReport | None:
        return self.calibration_results.get(_uuid(result_id))

    async def revoke_qualification(
        self,
        qualification_id: str,
        *,
        revoked_at: datetime,
        authorization_record_id: str,
        idempotency_key: str | None = None,
    ) -> Qualification | None:
        current = self.qualifications.get(qualification_id)
        if current is None:
            return None
        fingerprint = _revocation_fingerprint(
            qualification_id, authorization_record_id, revoked_at
        )
        prior = self.revocation_idempotency.get(qualification_id)
        if prior is not None:
            if idempotency_key != prior[0] or fingerprint != prior[1]:
                raise CalibrationError("idempotency_conflict")
            return current
        if current.state == "revoked":
            if current.revocation_record_id == authorization_record_id:
                self.audits.append({"kind": "qualification", "outcome": "duplicate"})
                return current
            raise CalibrationError("authorization_conflict")
        if current.authorization_record_id not in {None, authorization_record_id}:
            raise CalibrationError("authorization_conflict")
        updated = replace(
            current,
            qualified=False,
            revoked_at=revoked_at,
            state="revoked",
            score_disposition="advisory_only",
            revocation_record_id=authorization_record_id,
        )
        self.qualifications[qualification_id] = updated
        if idempotency_key is not None:
            self.revocation_idempotency[qualification_id] = (idempotency_key, fingerprint)
        self.audits.append({"kind": "qualification", "outcome": "revoked"})
        return updated


class CalibrationRepositoryPort(Protocol):
    async def put_label(self, label: CalibrationLabel, /) -> CalibrationLabel: ...

    async def put_review(self, review: HumanReview, /) -> HumanReview: ...

    async def put_qualification(self, qualification: Qualification, /) -> Qualification: ...

    async def list_labels(
        self,
        *,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
    ) -> list[CalibrationLabel]: ...

    async def get_qualification(self, qualification_id: str) -> Qualification | None: ...

    async def active_qualification(
        self, **scope: str | int | Mapping[str, int | float]
    ) -> Qualification | None: ...

    async def revoke_qualification(
        self,
        qualification_id: str,
        *,
        revoked_at: datetime,
        authorization_record_id: str,
        idempotency_key: str | None = None,
    ) -> Qualification | None: ...

    async def claim_calibration(
        self, claim: CalibrationExecutionClaim, /
    ) -> CalibrationExecutionClaim | None: ...

    async def fail_calibration(self, claim: CalibrationExecutionClaim, /) -> None: ...

    async def put_calibration_result(self, result: CalibrationReport, /) -> CalibrationReport: ...

    async def get_calibration_result(self, result_id: str) -> CalibrationReport | None: ...


class PostgresCalibrationRepository:
    """Transactional adapter for content-free calibration metadata."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    async def is_authorized(
        self,
        authorization_record_id: str,
        purpose: str,
        *,
        resource_id: str | None = None,
        scope_fingerprint: str | None = None,
    ) -> bool:
        _uuid(authorization_record_id)
        if purpose not in {"qualification", "human_review", "calibration_label", "revocation"}:
            raise CalibrationError("validation")
        if resource_id is not None:
            _uuid(resource_id)
        if scope_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", scope_fingerprint):
            raise CalibrationError("validation")
        clauses = ["authorization_record_id=%s", "action=%s", "revoked_at IS NULL"]
        values: list[str] = [authorization_record_id, purpose]
        if resource_id is not None:
            clauses.append("resource_id=%s")
            values.append(resource_id)
        if scope_fingerprint is not None:
            clauses.append("scope_fingerprint=%s")
            values.append(scope_fingerprint)
        async with self.connection_factory() as connection:
            result = await connection.execute(
                "SELECT 1 FROM observatory_evaluation_owner_actions WHERE " + " AND ".join(clauses),
                tuple(values),
            )
            return await result.fetchone() is not None

    async def is_comment_artifact_authorized(
        self,
        artifact_reference_id: str,
        *,
        artifact_version: str | None = None,
        artifact_digest: str | None = None,
    ) -> bool:
        _uuid(artifact_reference_id)
        if artifact_version is not None:
            _version(artifact_version)
        if artifact_digest is not None and not _DIGEST.fullmatch(artifact_digest):
            raise CalibrationError("validation")
        async with self.connection_factory() as connection:
            version_clause = " AND artifact.artifact_version=%s" if artifact_version else ""
            digest_clause = " AND artifact.sha256=%s" if artifact_digest else ""
            result = await connection.execute(
                """SELECT 1 FROM observatory_evaluation_artifacts AS artifact
                JOIN observatory_evaluation_authorizations AS authz
                  ON authz.authorization_record_id=artifact.authorization_record_id
                WHERE artifact.artifact_reference_id=%s AND artifact.status='available'
                  AND artifact.intended_use='comment'
                  AND artifact.media_classification='evaluation_evidence'
                  AND authz.authorization_purpose='human_review'
                  AND authz.revoked_at IS NULL AND authz.retention_mode='manual_only'"""
                + version_clause
                + digest_clause,
                tuple(
                    value
                    for value in (artifact_reference_id, artifact_version, artifact_digest)
                    if value is not None
                ),
            )
            return await result.fetchone() is not None

    async def list_labels(
        self,
        *,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
    ) -> list[CalibrationLabel]:
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """SELECT label_id, case_id, judge_id, judge_version,
                instruction_version, rubric_version, calibration_version,
                reference_score, reference_label, judge_score, judge_label,
                comment_artifact_id, comment_artifact_version, comment_artifact_digest,
                created_at, label_version,
                idempotency_key, authorization_record_id, retention_mode, provenance,
                calibration_id, result_id, reference_outcome
                FROM observatory_local_judge_labels
                WHERE judge_id=%s AND judge_version=%s AND instruction_version=%s
                  AND rubric_version=%s AND calibration_version=%s""",
                (judge_id, judge_version, prompt_version, rubric_version, calibration_version),
            )
            rows = await result.fetchall()
        return [
            CalibrationLabel(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                float(row[7]),
                str(row[8]),
                judge_score=None if row[9] is None else float(row[9]),
                judge_label=None if row[10] is None else str(row[10]),
                comment_artifact_reference_id=None if row[11] is None else str(row[11]),
                comment_artifact_version=(
                    None if len(row) <= 12 or row[12] is None else str(row[12])
                ),
                comment_artifact_digest=(
                    None if len(row) <= 13 or row[13] is None else str(row[13])
                ),
                created_at=cast(datetime, row[14]).astimezone(UTC),
                label_version=str(row[15]) if len(row) > 15 and row[15] is not None else "1.0.0",
                idempotency_key=str(row[16]) if len(row) > 16 and row[16] is not None else None,
                authorization_record_id=(
                    str(row[17]) if len(row) > 17 and row[17] is not None else None
                ),
                retention_mode=(
                    str(row[18]) if len(row) > 18 and row[18] is not None else "manual_only"
                ),
                provenance=str(row[19]) if len(row) > 19 and row[19] is not None else "owner_label",
                calibration_id=None if len(row) <= 20 or row[20] is None else str(row[20]),
                result_id=None if len(row) <= 21 or row[21] is None else str(row[21]),
                reference_outcome=None
                if len(row) <= 22 or row[22] is None
                else JudgeOutcome(str(row[22])),
            )
            for row in rows
        ]

    async def claim_calibration(
        self, claim: CalibrationExecutionClaim
    ) -> CalibrationExecutionClaim | None:
        claim.validate()
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_calibration_claims(
                    calibration_id, calibration_version, fingerprint, status)
                VALUES (%s,%s,%s,'claimed') ON CONFLICT DO NOTHING
                RETURNING status""",
                (claim.calibration_id, claim.calibration_version, claim.fingerprint),
            )
            inserted_row = await inserted.fetchone()
            if inserted_row is not None:
                return claim
            result = await connection.execute(
                """SELECT fingerprint, status FROM observatory_local_judge_calibration_claims
                WHERE calibration_id=%s AND calibration_version=%s""",
                (claim.calibration_id, claim.calibration_version),
            )
            row = await result.fetchone()
        if row is None or str(row[0]) != claim.fingerprint:
            raise CalibrationError("idempotency_conflict")
        if str(row[1]) == "failed":
            async with self.connection_factory() as connection:
                reclaimed = await connection.execute(
                    """UPDATE observatory_local_judge_calibration_claims
                    SET status='claimed', claimed_at=CURRENT_TIMESTAMP, completed_at=NULL
                    WHERE calibration_id=%s AND calibration_version=%s AND status='failed'""",
                    (claim.calibration_id, claim.calibration_version),
                )
            return claim if int(getattr(reclaimed, "rowcount", 0) or 0) == 1 else None
        if str(row[1]) != "claimed":
            return None
        async with self.connection_factory() as connection:
            reclaimed = await connection.execute(
                """UPDATE observatory_local_judge_calibration_claims
                SET claimed_at=CURRENT_TIMESTAMP
                WHERE calibration_id=%s AND calibration_version=%s
                  AND fingerprint=%s AND status='claimed'
                  AND claimed_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes'""",
                (claim.calibration_id, claim.calibration_version, claim.fingerprint),
            )
        return claim if int(getattr(reclaimed, "rowcount", 0) or 0) == 1 else None

    async def put_calibration_result(self, result: CalibrationReport) -> CalibrationReport:
        result.validate(strict=True)
        if result.calibration_result_id is None or result.calibration_id is None:
            raise CalibrationError("validation")
        _uuid(result.calibration_result_id)
        _uuid(result.calibration_id)
        payload = json.dumps(
            {
                "judgeId": result.judge_id,
                "judgeVersion": result.judge_version,
                "promptVersion": result.prompt_version,
                "rubricVersion": result.rubric_version,
                "calibrationVersion": result.calibration_version,
                "datasetId": result.dataset_id,
                "datasetVersion": result.dataset_version,
                "suiteId": result.suite_id,
                "suiteVersion": result.suite_version,
                "evaluatorId": result.evaluator_id,
                "evaluatorVersion": result.evaluator_version,
                "calibrationResultId": result.calibration_result_id,
                "calibrationId": result.calibration_id,
                "modelName": result.model_name,
                "modelVersion": result.model_version,
                "modelDigest": result.model_digest,
                "seed": result.seed,
                "budget": dict(result.budget),
                "scoreDisposition": result.score_disposition,
                "retentionMode": result.retention_mode,
                "provenance": result.provenance,
                "createdAt": _utc_timestamp(result.created_at),
                "sampleCount": result.sample_count,
                "agreement": result.agreement,
                "meanAbsoluteError": result.mean_absolute_error,
                "disagreementRate": result.disagreement_rate,
                "thresholdAgreement": result.threshold_agreement,
                "thresholdMeanAbsoluteError": result.threshold_mean_absolute_error,
                "thresholdDisagreementRate": result.threshold_disagreement_rate,
                "eligible": result.eligible,
                "promptArtifactReferenceId": result.prompt_artifact_reference_id,
                "promptArtifactVersion": result.prompt_artifact_version,
                "promptArtifactDigest": result.prompt_artifact_digest,
                "rubricArtifactReferenceId": result.rubric_artifact_reference_id,
                "rubricArtifactVersion": result.rubric_artifact_version,
                "rubricArtifactDigest": result.rubric_artifact_digest,
                "provenanceComponent": result.provenance_component,
                "provenanceVersion": result.provenance_version,
                "provenanceDigest": result.provenance_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        eligibility = "eligible" if result.eligible else "ineligible"
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_calibration_results(
                    calibration_result_id, calibration_id, calibration_version,
                    judge_id, judge_version, sample_count, agreement_rate,
                    mean_absolute_error, disagreement_rate, eligibility, result_json,
                    model_name, model_version, model_digest,
                    prompt_artifact_id, prompt_artifact_version, prompt_artifact_digest,
                    rubric_artifact_id, rubric_artifact_version, rubric_artifact_digest,
                    seed, budget_json, provenance_component, provenance_version,
                    provenance_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (calibration_result_id) DO NOTHING
                RETURNING calibration_result_id""",
                (
                    result.calibration_result_id,
                    result.calibration_id,
                    result.calibration_version,
                    result.judge_id,
                    result.judge_version,
                    result.sample_count,
                    result.agreement,
                    result.mean_absolute_error,
                    result.disagreement_rate,
                    eligibility,
                    payload,
                    result.model_name,
                    result.model_version,
                    result.model_digest,
                    result.prompt_artifact_reference_id,
                    result.prompt_artifact_version,
                    result.prompt_artifact_digest,
                    result.rubric_artifact_reference_id,
                    result.rubric_artifact_version,
                    result.rubric_artifact_digest,
                    result.seed,
                    json.dumps(dict(result.budget), sort_keys=True, separators=(",", ":")),
                    result.provenance_component,
                    result.provenance_version,
                    result.provenance_digest,
                ),
            )
            if await inserted.fetchone() is None:
                current = await connection.execute(
                    "SELECT result_json FROM observatory_local_judge_calibration_results "
                    "WHERE calibration_result_id=%s",
                    (result.calibration_result_id,),
                )
                row = await current.fetchone()
                if row is None:
                    raise CalibrationError("storage_failure")
                stored = row[0] if not isinstance(row[0], str) else json.loads(row[0])
                if not isinstance(stored, Mapping) or stored != json.loads(payload):
                    raise CalibrationError("idempotency_conflict")
            completed = await connection.execute(
                """UPDATE observatory_local_judge_calibration_claims
                SET status='completed', completed_at=CURRENT_TIMESTAMP
                WHERE calibration_id=%s AND calibration_version=%s
                  AND status='claimed'""",
                (result.calibration_id, result.calibration_version),
            )
            if int(getattr(completed, "rowcount", 0) or 0) != 1:
                raise CalibrationError("storage_failure")
        return result

    async def fail_calibration(self, claim: CalibrationExecutionClaim) -> None:
        claim.validate()
        async with self.connection_factory() as connection:
            await connection.execute(
                """UPDATE observatory_local_judge_calibration_claims
                SET status='failed', completed_at=CURRENT_TIMESTAMP
                WHERE calibration_id=%s AND calibration_version=%s
                  AND fingerprint=%s AND status='claimed'""",
                (claim.calibration_id, claim.calibration_version, claim.fingerprint),
            )

    async def get_calibration_result(self, result_id: str) -> CalibrationReport | None:
        _uuid(result_id)
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """SELECT calibration_result_id, calibration_id, calibration_version,
                    judge_id, judge_version, sample_count, agreement_rate,
                    mean_absolute_error, disagreement_rate, eligibility, result_json
                FROM observatory_local_judge_calibration_results
                WHERE calibration_result_id=%s""",
                (result_id,),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return self._report_from_row(row)

    @staticmethod
    def _report_from_row(row: Any) -> CalibrationReport:
        raw = row[10]
        if isinstance(raw, str):
            raw = json.loads(raw)
        value = raw if isinstance(raw, Mapping) else {}
        budget_value = value.get("budget", {})
        if isinstance(budget_value, str):
            try:
                budget_value = json.loads(budget_value)
            except json.JSONDecodeError:
                budget_value = {}
        return CalibrationReport(
            str(row[3]),
            str(row[4]),
            str(value.get("promptVersion", "")),
            str(value.get("rubricVersion", "")),
            str(row[2]),
            int(row[5]),
            float(row[6]),
            float(row[7]),
            float(row[8]),
            str(row[9]) == "eligible",
            threshold_agreement=float(value.get("thresholdAgreement", MIN_AGREEMENT)),
            threshold_mean_absolute_error=float(
                value.get("thresholdMeanAbsoluteError", MAX_MEAN_ABSOLUTE_ERROR)
            ),
            threshold_disagreement_rate=float(
                value.get("thresholdDisagreementRate", MAX_DISAGREEMENT)
            ),
            calibration_result_id=str(row[0]),
            calibration_id=str(row[1]),
            dataset_id=None if value.get("datasetId") is None else str(value["datasetId"]),
            dataset_version=(
                None if value.get("datasetVersion") is None else str(value["datasetVersion"])
            ),
            suite_id=None if value.get("suiteId") is None else str(value["suiteId"]),
            suite_version=None if value.get("suiteVersion") is None else str(value["suiteVersion"]),
            evaluator_id=None if value.get("evaluatorId") is None else str(value["evaluatorId"]),
            evaluator_version=(
                None if value.get("evaluatorVersion") is None else str(value["evaluatorVersion"])
            ),
            model_name=None if value.get("modelName") is None else str(value["modelName"]),
            model_version=None if value.get("modelVersion") is None else str(value["modelVersion"]),
            model_digest=None if value.get("modelDigest") is None else str(value["modelDigest"]),
            seed=int(value.get("seed", 0)),
            budget=cast(
                Mapping[str, int | float],
                budget_value if isinstance(budget_value, Mapping) else {},
            ),
            score_disposition=str(value.get("scoreDisposition", "advisory_only")),
            retention_mode=str(value.get("retentionMode", "manual_only")),
            provenance=str(value.get("provenance", "local_calibration")),
            created_at=(
                datetime.fromisoformat(str(value["createdAt"])).astimezone(UTC)
                if value.get("createdAt")
                else datetime.now(UTC)
            ),
            prompt_artifact_reference_id=(
                None
                if value.get("promptArtifactReferenceId") is None
                else str(value["promptArtifactReferenceId"])
            ),
            prompt_artifact_version=(
                None
                if value.get("promptArtifactVersion") is None
                else str(value["promptArtifactVersion"])
            ),
            prompt_artifact_digest=(
                None
                if value.get("promptArtifactDigest") is None
                else str(value["promptArtifactDigest"])
            ),
            rubric_artifact_reference_id=(
                None
                if value.get("rubricArtifactReferenceId") is None
                else str(value["rubricArtifactReferenceId"])
            ),
            rubric_artifact_version=(
                None
                if value.get("rubricArtifactVersion") is None
                else str(value["rubricArtifactVersion"])
            ),
            rubric_artifact_digest=(
                None
                if value.get("rubricArtifactDigest") is None
                else str(value["rubricArtifactDigest"])
            ),
            provenance_component=(
                None
                if value.get("provenanceComponent") is None
                else str(value["provenanceComponent"])
            ),
            provenance_version=(
                None if value.get("provenanceVersion") is None else str(value["provenanceVersion"])
            ),
            provenance_digest=(
                None if value.get("provenanceDigest") is None else str(value["provenanceDigest"])
            ),
        )

    async def get_qualification(self, qualification_id: str) -> Qualification | None:
        _uuid(qualification_id)
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """SELECT qualification_id, suite_id, suite_version, dataset_id,
                dataset_version, evaluator_id, evaluator_version, judge_id,
                judge_version, instruction_version, rubric_version,
                calibration_version, qualified, reason, created_at, revoked_at,
                qualification_version, idempotency_key, authorization_record_id,
                calibration_result_id, state, action_mode, score_disposition,
                retention_mode, model_name, model_version, model_digest,
                prompt_artifact_id, rubric_artifact_id, calibration_eligibility,
                qualified_at, revocation_record_id,
                prompt_artifact_version, prompt_artifact_digest,
                rubric_artifact_version, rubric_artifact_digest, seed, budget_json,
                provenance_component, provenance_version, provenance_digest,
                record_fingerprint, calibration_id
                FROM observatory_local_judge_qualifications
                WHERE qualification_id=%s""",
                (qualification_id,),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return Qualification(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            bool(row[12]),
            str(row[13]),
            cast(datetime, row[14]).astimezone(UTC),
            None if row[15] is None else cast(datetime, row[15]).astimezone(UTC),
            str(row[16]) if len(row) > 16 and row[16] is not None else "1.0.0",
            str(row[17]) if len(row) > 17 and row[17] is not None else None,
            str(row[18]) if len(row) > 18 and row[18] is not None else None,
            str(row[19]) if len(row) > 19 and row[19] is not None else None,
            str(row[20]) if len(row) > 20 and row[20] is not None else "qualified",
            str(row[21]) if len(row) > 21 and row[21] is not None else "manual_owner_action",
            str(row[22]) if len(row) > 22 and row[22] is not None else "calibrated_enabled",
            str(row[23]) if len(row) > 23 and row[23] is not None else "manual_only",
            model_name=None if len(row) <= 24 or row[24] is None else str(row[24]),
            model_version=None if len(row) <= 25 or row[25] is None else str(row[25]),
            model_digest=None if len(row) <= 26 or row[26] is None else str(row[26]),
            prompt_artifact_reference_id=(
                None if len(row) <= 27 or row[27] is None else str(row[27])
            ),
            rubric_artifact_reference_id=(
                None if len(row) <= 28 or row[28] is None else str(row[28])
            ),
            calibration_eligibility=(
                str(row[29]) if len(row) > 29 and row[29] is not None else "eligible"
            ),
            qualified_at=(
                None
                if len(row) <= 30 or row[30] is None
                else cast(datetime, row[30]).astimezone(UTC)
            ),
            revocation_record_id=(None if len(row) <= 31 or row[31] is None else str(row[31])),
            prompt_artifact_version=(None if len(row) <= 32 or row[32] is None else str(row[32])),
            prompt_artifact_digest=(None if len(row) <= 33 or row[33] is None else str(row[33])),
            rubric_artifact_version=(None if len(row) <= 34 or row[34] is None else str(row[34])),
            rubric_artifact_digest=(None if len(row) <= 35 or row[35] is None else str(row[35])),
            seed=int(row[36]) if len(row) > 36 and row[36] is not None else 0,
            budget=(
                cast(Mapping[str, int | float], row[37])
                if len(row) > 37 and isinstance(row[37], Mapping)
                else (
                    cast(Mapping[str, int | float], json.loads(row[37]))
                    if len(row) > 37 and isinstance(row[37], str)
                    else {}
                )
            ),
            provenance_component=(None if len(row) <= 38 or row[38] is None else str(row[38])),
            provenance_version=(None if len(row) <= 39 or row[39] is None else str(row[39])),
            provenance_digest=(None if len(row) <= 40 or row[40] is None else str(row[40])),
            calibration_id=(None if len(row) <= 42 or row[42] is None else str(row[42])),
        )

    async def active_qualification(
        self, **scope: str | int | Mapping[str, int | float]
    ) -> Qualification | None:
        allowed = {
            "suite_id",
            "suite_version",
            "dataset_id",
            "dataset_version",
            "evaluator_id",
            "evaluator_version",
            "judge_id",
            "judge_version",
            "instruction_version",
            "rubric_version",
            "calibration_version",
            "model_name",
            "model_version",
            "model_digest",
            "prompt_artifact_id",
            "prompt_artifact_version",
            "prompt_artifact_digest",
            "rubric_artifact_id",
            "rubric_artifact_version",
            "rubric_artifact_digest",
            "calibration_result_id",
            "seed",
        }
        if not scope or any(key not in allowed for key in scope):
            raise CalibrationError("validation")
        clauses = [f"{key}=%s" for key in scope]
        values = [scope[key] for key in scope]
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """SELECT qualification_id FROM observatory_local_judge_qualifications
                WHERE qualified=TRUE AND state='qualified'
                  AND score_disposition='calibrated_enabled'
                  AND revoked_at IS NULL AND """
                + " AND ".join(clauses)
                + " ORDER BY qualified_at DESC NULLS LAST, qualification_id LIMIT 1",
                tuple(values),
            )
            row = await result.fetchone()
        return None if row is None else await self.get_qualification(str(row[0]))

    async def put_label(self, item: CalibrationLabel) -> CalibrationLabel:
        item.validate(strict=True)
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_labels(
                    label_id, case_id, judge_id, judge_version, instruction_version,
                    rubric_version, calibration_version, reference_score, reference_label,
                    judge_score, judge_label, comment_artifact_id,
                    comment_artifact_version, comment_artifact_digest, created_at,
                    label_version, idempotency_key, authorization_record_id,
                    retention_mode, provenance, calibration_id, result_id,
                    reference_outcome, record_fingerprint)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (label_id) DO NOTHING
                RETURNING label_id""",
                (
                    item.label_id,
                    item.case_id,
                    item.judge_id,
                    item.judge_version,
                    item.prompt_version,
                    item.rubric_version,
                    item.calibration_version,
                    item.reference_score,
                    item.reference_label,
                    item.judge_score,
                    item.judge_label,
                    item.comment_artifact_reference_id,
                    item.comment_artifact_version,
                    item.comment_artifact_digest,
                    item.created_at,
                    item.label_version,
                    item.idempotency_key,
                    item.authorization_record_id,
                    item.retention_mode,
                    item.provenance,
                    item.calibration_id,
                    item.result_id,
                    None if item.reference_outcome is None else item.reference_outcome.value,
                    _record_fingerprint(item),
                ),
            )
            if await inserted.fetchone() is None:
                current = await connection.execute(
                    "SELECT record_fingerprint FROM observatory_local_judge_labels "
                    "WHERE label_id=%s",
                    (item.label_id,),
                )
                row = await current.fetchone()
                if row is None or str(row[0]) != _record_fingerprint(item):
                    raise CalibrationError("idempotency_conflict")
        return item

    async def put_review(self, item: HumanReview) -> HumanReview:
        item.validate(strict=True)
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_reviews(
                    review_id, case_id, result_id, judge_id, judge_version,
                    instruction_version, rubric_version, calibration_version,
                    label, score, review_outcome, disagreement, comment_artifact_id,
                    comment_artifact_version, comment_artifact_digest, created_at,
                    review_version, idempotency_key, authorization_record_id,
                    retention_mode, provenance, calibration_id, score_id, review_label,
                    record_fingerprint, review_provenance_component,
                    review_provenance_version, review_provenance_digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (review_id) DO NOTHING
                RETURNING review_id""",
                (
                    item.review_id,
                    item.case_id,
                    item.result_id,
                    item.judge_id,
                    item.judge_version,
                    item.prompt_version,
                    item.rubric_version,
                    item.calibration_version,
                    item.label,
                    item.score,
                    None if item.outcome is None else item.outcome.value,
                    item.disagreement.value,
                    item.comment_artifact_reference_id,
                    item.comment_artifact_version,
                    item.comment_artifact_digest,
                    item.created_at,
                    item.review_version,
                    item.idempotency_key,
                    item.authorization_record_id,
                    item.retention_mode,
                    item.provenance,
                    item.calibration_id,
                    item.score_id,
                    item.review_label or item.label,
                    _record_fingerprint(item),
                    item.provenance_component,
                    item.provenance_version,
                    item.provenance_digest,
                ),
            )
            if await inserted.fetchone() is None:
                current = await connection.execute(
                    "SELECT record_fingerprint FROM observatory_local_judge_reviews "
                    "WHERE review_id=%s",
                    (item.review_id,),
                )
                row = await current.fetchone()
                if row is None or str(row[0]) != _record_fingerprint(item):
                    raise CalibrationError("idempotency_conflict")
        return item

    async def put_qualification(self, item: Qualification) -> Qualification:
        item.validate(strict=True)
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_qualifications(
                    qualification_id, suite_id, suite_version, dataset_id, dataset_version,
                    evaluator_id, evaluator_version, judge_id, judge_version,
                    instruction_version, rubric_version, calibration_version,
                    qualified, reason, created_at, revoked_at,
                    qualification_version, idempotency_key, authorization_record_id,
                calibration_result_id, state, action_mode, score_disposition,
                    retention_mode, model_name, model_version, model_digest,
                    prompt_artifact_id, rubric_artifact_id, calibration_eligibility,
                    qualified_at, revocation_record_id,
                prompt_artifact_version, prompt_artifact_digest,
                rubric_artifact_version, rubric_artifact_digest, seed, budget_json,
                provenance_component, provenance_version, provenance_digest,
                record_fingerprint, calibration_id)
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (qualification_id) DO NOTHING
                RETURNING qualification_id""",
                (
                    item.qualification_id,
                    item.suite_id,
                    item.suite_version,
                    item.dataset_id,
                    item.dataset_version,
                    item.evaluator_id,
                    item.evaluator_version,
                    item.judge_id,
                    item.judge_version,
                    item.prompt_version,
                    item.rubric_version,
                    item.calibration_version,
                    item.qualified,
                    item.reason,
                    item.created_at,
                    item.revoked_at,
                    item.qualification_version,
                    item.idempotency_key,
                    item.authorization_record_id,
                    item.calibration_result_id,
                    item.state,
                    item.action_mode,
                    item.score_disposition,
                    item.retention_mode,
                    item.model_name,
                    item.model_version,
                    item.model_digest,
                    item.prompt_artifact_reference_id,
                    item.rubric_artifact_reference_id,
                    item.calibration_eligibility,
                    item.qualified_at if item.qualified else None,
                    item.revocation_record_id,
                    item.prompt_artifact_version,
                    item.prompt_artifact_digest,
                    item.rubric_artifact_version,
                    item.rubric_artifact_digest,
                    item.seed,
                    json.dumps(dict(item.budget), sort_keys=True, separators=(",", ":")),
                    item.provenance_component,
                    item.provenance_version,
                    item.provenance_digest,
                    _qualification_fingerprint(item),
                    item.calibration_id,
                ),
            )
            if await inserted.fetchone() is None:
                current = await connection.execute(
                    "SELECT record_fingerprint FROM observatory_local_judge_qualifications "
                    "WHERE qualification_id=%s",
                    (item.qualification_id,),
                )
                row = await current.fetchone()
                if row is None or str(row[0]) != _qualification_fingerprint(item):
                    raise CalibrationError("idempotency_conflict")
                # A replay after revocation returns the durable revoked truth;
                # caller state never becomes authoritative.
        stored = await self.get_qualification(item.qualification_id)
        if stored is None:
            raise CalibrationError("storage_failure")
        return stored

    async def revoke_qualification(
        self,
        qualification_id: str,
        *,
        revoked_at: datetime,
        authorization_record_id: str,
        idempotency_key: str | None = None,
    ) -> Qualification | None:
        _uuid(qualification_id)
        _uuid(authorization_record_id)
        effective_key = idempotency_key or f"legacy-revoke:{qualification_id}"
        fingerprint = _revocation_fingerprint(
            qualification_id, authorization_record_id, revoked_at
        )
        async with self.connection_factory() as connection:
            current = await connection.execute(
                """SELECT state, revocation_record_id FROM
                observatory_local_judge_qualifications WHERE qualification_id=%s
                FOR UPDATE""",
                (qualification_id,),
            )
            current_row = await current.fetchone()
            if current_row is None:
                raise CalibrationError("not_found")
            replay = await connection.execute(
                """SELECT idempotency_key, request_fingerprint
                FROM observatory_evaluation_qualification_revocations
                WHERE qualification_id=%s FOR UPDATE""",
                (qualification_id,),
            )
            replay_row = await replay.fetchone()
            if replay_row is not None:
                if effective_key != str(replay_row[0]) or fingerprint != str(replay_row[1]):
                    raise CalibrationError("idempotency_conflict")
                return await self.get_qualification(qualification_id)
            if str(current_row[0]) == "revoked":
                if str(current_row[1]) == authorization_record_id:
                    return await self.get_qualification(qualification_id)
                raise CalibrationError("authorization_conflict")
            result = await connection.execute(
                """UPDATE observatory_local_judge_qualifications
                SET qualified=FALSE, revoked_at=%s,
                    state='revoked', score_disposition='advisory_only',
                    revocation_record_id=%s
                WHERE qualification_id=%s AND qualified=TRUE AND state='qualified'""",
                (
                    revoked_at,
                    authorization_record_id,
                    qualification_id,
                ),
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                raise CalibrationError("authorization_conflict")
            await connection.execute(
                """INSERT INTO observatory_evaluation_qualification_revocations(
                    qualification_id, idempotency_key, request_fingerprint,
                    authorization_record_id, revoked_at)
                VALUES (%s,%s,%s,%s,%s)""",
                (
                    qualification_id,
                    effective_key,
                    fingerprint,
                    authorization_record_id,
                    revoked_at,
                ),
            )
            await connection.execute(
                """INSERT INTO observatory_local_judge_audits(
                    audit_id, resource_type, resource_id, operation, outcome,
                    occurred_at)
                VALUES (%s,'qualification',%s,'revoke','accepted',%s)""",
                (str(uuid4()), qualification_id, revoked_at),
            )
        return await self.get_qualification(qualification_id)


class CalibrationService:
    def __init__(
        self,
        repository: CalibrationRepositoryPort | None = None,
        *,
        metrics: Any | None = None,
        event_publisher: Callable[[str, str, str, str], Awaitable[object]] | None = None,
    ) -> None:
        self.repository = repository or InMemoryCalibrationRepository()
        self.metrics = metrics
        self.event_publisher = event_publisher

    async def get_calibration_result(self, result_id: str) -> CalibrationReport | None:
        """Read the authoritative calibration result through the service port."""

        try:
            return await self.repository.get_calibration_result(_uuid(result_id))
        except CalibrationError:
            raise
        except Exception:
            raise CalibrationError("database_failure") from None

    async def is_authorized(
        self,
        authorization_record_id: str,
        purpose: str,
        *,
        resource_id: str | None = None,
        scope_fingerprint: str | None = None,
    ) -> bool:
        """Expose the narrow authorization lookup owned by calibration."""

        checker = getattr(self.repository, "is_authorized", None)
        if not callable(checker):
            return isinstance(self.repository, InMemoryCalibrationRepository)
        try:
            return bool(
                await checker(
                    _uuid(authorization_record_id),
                    purpose,
                    resource_id=_uuid(resource_id) if resource_id is not None else None,
                    scope_fingerprint=scope_fingerprint,
                )
            )
        except CalibrationError:
            raise
        except Exception:
            raise CalibrationError("database_failure") from None

    async def _publish_live(
        self, resource_type: str, resource_id: str, change: str, state: str
    ) -> None:
        if self.event_publisher is None:
            return
        try:
            await self.event_publisher(resource_type, resource_id, change, state)
        except Exception:
            _LOGGER.debug("calibration live notification unavailable")

    async def label(self, item: CalibrationLabel) -> CalibrationLabel:
        started = monotonic()
        try:
            await self._require_action_authorization(
                item.authorization_record_id,
                "calibration_label",
                resource_id=item.label_id,
                scope_fingerprint=_record_fingerprint(item),
            )
        except CalibrationError as error:
            self._operation_span("label", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("label", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        if item.comment_artifact_reference_id is not None:
            if item.comment_artifact_version is None or item.comment_artifact_digest is None:
                if not isinstance(self.repository, InMemoryCalibrationRepository):
                    self._operation_span(
                        "label", started, "rejected", error_code="artifact_unauthorized"
                    )
                    raise CalibrationError("artifact_unauthorized")
            checker = getattr(self.repository, "is_comment_artifact_authorized", None)
            if callable(checker):
                try:
                    comment_authorized = await checker(
                        item.comment_artifact_reference_id,
                        artifact_version=item.comment_artifact_version,
                        artifact_digest=item.comment_artifact_digest,
                    )
                except TypeError:
                    if not isinstance(self.repository, InMemoryCalibrationRepository):
                        self._operation_span(
                            "label", started, "rejected", error_code="artifact_unauthorized"
                        )
                        raise CalibrationError("artifact_unauthorized") from None
                    try:
                        comment_authorized = await checker(item.comment_artifact_reference_id)
                    except Exception:
                        self._operation_span(
                            "label", started, "failure", error_code="database_failure"
                        )
                        raise CalibrationError("database_failure") from None
                except Exception:
                    self._operation_span("label", started, "failure", error_code="database_failure")
                    raise CalibrationError("database_failure") from None
                if not comment_authorized:
                    self._operation_span(
                        "label", started, "rejected", error_code="artifact_unauthorized"
                    )
                    raise CalibrationError("artifact_unauthorized")
            if not callable(checker) and not isinstance(
                self.repository, InMemoryCalibrationRepository
            ):
                self._operation_span(
                    "label", started, "rejected", error_code="artifact_unauthorized"
                )
                raise CalibrationError("artifact_unauthorized")
        try:
            result = await self.repository.put_label(item)
        except CalibrationError as error:
            self._operation_span("label", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("label", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        self._metric("observatory_calibration_review_count", {"outcome": "accepted"})
        self._operation_span("label", started, "success")
        return result

    async def review(self, item: HumanReview) -> HumanReview:
        started = monotonic()
        try:
            await self._require_action_authorization(
                item.authorization_record_id,
                "human_review",
                resource_id=item.review_id,
                scope_fingerprint=_record_fingerprint(item),
            )
        except CalibrationError as error:
            self._operation_span("review", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("review", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        if item.comment_artifact_reference_id is not None:
            if item.comment_artifact_version is None or item.comment_artifact_digest is None:
                if not isinstance(self.repository, InMemoryCalibrationRepository):
                    self._operation_span(
                        "review", started, "rejected", error_code="artifact_unauthorized"
                    )
                    raise CalibrationError("artifact_unauthorized")
            checker = getattr(self.repository, "is_comment_artifact_authorized", None)
            if callable(checker):
                try:
                    comment_authorized = await checker(
                        item.comment_artifact_reference_id,
                        artifact_version=item.comment_artifact_version,
                        artifact_digest=item.comment_artifact_digest,
                    )
                except TypeError:
                    if not isinstance(self.repository, InMemoryCalibrationRepository):
                        self._operation_span(
                            "review", started, "rejected", error_code="artifact_unauthorized"
                        )
                        raise CalibrationError("artifact_unauthorized") from None
                    try:
                        comment_authorized = await checker(item.comment_artifact_reference_id)
                    except Exception:
                        self._operation_span(
                            "review", started, "failure", error_code="database_failure"
                        )
                        raise CalibrationError("database_failure") from None
                except Exception:
                    self._operation_span(
                        "review", started, "failure", error_code="database_failure"
                    )
                    raise CalibrationError("database_failure") from None
                if not comment_authorized:
                    self._operation_span(
                        "review", started, "rejected", error_code="artifact_unauthorized"
                    )
                    raise CalibrationError("artifact_unauthorized")
            elif not isinstance(self.repository, InMemoryCalibrationRepository):
                self._operation_span(
                    "review", started, "rejected", error_code="artifact_unauthorized"
                )
                raise CalibrationError("artifact_unauthorized")
        try:
            result = await self.repository.put_review(item)
        except CalibrationError as error:
            self._operation_span("review", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("review", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        self._metric("observatory_calibration_review_count", {"outcome": "accepted"})
        self._operation_span("review", started, "success")
        await self._publish_live("evaluation_review", result.review_id, "created", "completed")
        return result

    async def _require_action_authorization(
        self,
        record_id: str | None,
        action: str,
        *,
        resource_id: str | None = None,
        scope_fingerprint: str | None = None,
    ) -> None:
        if record_id is None:
            raise CalibrationError("authorization_required")
        checker = getattr(self.repository, "is_authorized", None)
        if callable(checker):
            if not await checker(
                record_id,
                action,
                resource_id=resource_id,
                scope_fingerprint=scope_fingerprint,
            ):
                raise CalibrationError("authorization_required")
        elif not isinstance(self.repository, InMemoryCalibrationRepository):
            raise CalibrationError("authorization_required")

    async def calibrate(
        self,
        *,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        suite_id: str | None = None,
        suite_version: str | None = None,
        evaluator_id: str | None = None,
        evaluator_version: str | None = None,
        calibration_result_id: str | None = None,
        calibration_id: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        model_digest: str | None = None,
        prompt_artifact_reference_id: str | None = None,
        prompt_artifact_version: str | None = None,
        prompt_artifact_digest: str | None = None,
        rubric_artifact_reference_id: str | None = None,
        rubric_artifact_version: str | None = None,
        rubric_artifact_digest: str | None = None,
        seed: int = 0,
        budget: Mapping[str, int | float] | None = None,
        provenance_component: str | None = None,
        provenance_version: str | None = None,
        provenance_digest: str | None = None,
    ) -> CalibrationReport:
        started = monotonic()
        claim: CalibrationExecutionClaim | None = None
        if calibration_id is not None:
            _uuid(calibration_id)
            claim_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "judge": judge_id,
                        "judgeVersion": judge_version,
                        "prompt": prompt_version,
                        "rubric": rubric_version,
                        "calibration": calibration_version,
                        "dataset": dataset_id,
                        "suite": suite_id,
                        "evaluator": evaluator_id,
                        "result": calibration_result_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            claim = CalibrationExecutionClaim(
                calibration_id, calibration_version, claim_fingerprint
            )
            try:
                claimed = await self.repository.claim_calibration(claim)
            except CalibrationError as error:
                self._operation_span("calibrate", started, "failure", error_code=error.code)
                raise
            except Exception:
                self._operation_span("calibrate", started, "failure", error_code="database_failure")
                raise CalibrationError("database_failure") from None
            if claimed is None:
                try:
                    existing = (
                        await self.repository.get_calibration_result(calibration_result_id)
                        if calibration_result_id
                        else None
                    )
                except CalibrationError:
                    self._operation_span(
                        "calibrate", started, "failure", error_code="database_failure"
                    )
                    raise
                except Exception:
                    self._operation_span(
                        "calibrate", started, "failure", error_code="database_failure"
                    )
                    raise CalibrationError("database_failure") from None
                if existing is not None:
                    self._operation_span("calibrate", started, "duplicate")
                    return existing
                self._operation_span("calibrate", started, "duplicate")
                raise CalibrationError("duplicate")
        list_labels = cast(Any, getattr(self.repository, "list_labels", None))
        try:
            labels: list[CalibrationLabel] = (
                await cast(Callable[..., Awaitable[list[CalibrationLabel]]], list_labels)(
                    judge_id=judge_id,
                    judge_version=judge_version,
                    prompt_version=prompt_version,
                    rubric_version=rubric_version,
                    calibration_version=calibration_version,
                )
                if callable(list_labels)
                else []
            )
        except CalibrationError as error:
            self._operation_span("calibrate", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("calibrate", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        if not isinstance(self.repository, InMemoryCalibrationRepository):
            try:
                for item in labels:
                    item.validate(strict=True)
            except CalibrationError:
                self._operation_span("calibrate", started, "failure", error_code="validation")
                raise
        if not labels:
            report = CalibrationReport(
                judge_id,
                judge_version,
                prompt_version,
                rubric_version,
                calibration_version,
                0,
                0.0,
                1.0,
                1.0,
                False,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                suite_id=suite_id,
                suite_version=suite_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                calibration_result_id=calibration_result_id,
                calibration_id=calibration_id,
                model_name=model_name,
                model_version=model_version,
                model_digest=model_digest,
                prompt_artifact_reference_id=prompt_artifact_reference_id,
                prompt_artifact_version=prompt_artifact_version,
                prompt_artifact_digest=prompt_artifact_digest,
                rubric_artifact_reference_id=rubric_artifact_reference_id,
                rubric_artifact_version=rubric_artifact_version,
                rubric_artifact_digest=rubric_artifact_digest,
                seed=seed,
                budget=dict(budget or {}),
                provenance_component=provenance_component,
                provenance_version=provenance_version,
                provenance_digest=provenance_digest,
            )
            await self._persist_report_recover(report, claim)
            self._operation_span("calibrate", started, "insufficient")
            return report
        valid = [
            item for item in labels if item.judge_score is not None and item.judge_label is not None
        ]
        sample_count = len(valid)
        if not valid:
            report = CalibrationReport(
                judge_id,
                judge_version,
                prompt_version,
                rubric_version,
                calibration_version,
                0,
                0.0,
                1.0,
                1.0,
                False,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                suite_id=suite_id,
                suite_version=suite_version,
                evaluator_id=evaluator_id,
                evaluator_version=evaluator_version,
                calibration_result_id=calibration_result_id,
                calibration_id=calibration_id,
                model_name=model_name,
                model_version=model_version,
                model_digest=model_digest,
                prompt_artifact_reference_id=prompt_artifact_reference_id,
                prompt_artifact_version=prompt_artifact_version,
                prompt_artifact_digest=prompt_artifact_digest,
                rubric_artifact_reference_id=rubric_artifact_reference_id,
                rubric_artifact_version=rubric_artifact_version,
                rubric_artifact_digest=rubric_artifact_digest,
                seed=seed,
                budget=dict(budget or {}),
                provenance_component=provenance_component,
                provenance_version=provenance_version,
                provenance_digest=provenance_digest,
            )
            await self._persist_report_recover(report, claim)
            self._operation_span("calibrate", started, "insufficient")
            return report
        scores = [float(item.judge_score) for item in valid if item.judge_score is not None]
        # The score agreement gate is inclusive at exactly 0.10. Label or
        # outcome disagreement is reported separately below.
        agreement = (
            sum(
                abs(score - item.reference_score) <= 0.10
                for score, item in zip(scores, valid, strict=True)
            )
            / sample_count
        )
        mae = (
            sum(
                abs(score - item.reference_score) for score, item in zip(scores, valid, strict=True)
            )
            / sample_count
        )
        disagreement = (
            sum(item.judge_label != item.reference_label for item in valid) / sample_count
        )
        self._metric(
            "observatory_judge_disagreement_count",
            {"disagreement": "label" if disagreement > 0 else "none"},
        )
        report = CalibrationReport(
            judge_id,
            judge_version,
            prompt_version,
            rubric_version,
            calibration_version,
            sample_count,
            round(agreement, 6),
            round(mae, 6),
            round(disagreement, 6),
            sample_count >= MIN_CALIBRATION_LABELS
            and agreement >= MIN_AGREEMENT
            and mae <= MAX_MEAN_ABSOLUTE_ERROR
            and disagreement <= MAX_DISAGREEMENT,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            suite_id=suite_id,
            suite_version=suite_version,
            evaluator_id=evaluator_id,
            evaluator_version=evaluator_version,
            calibration_result_id=calibration_result_id,
            calibration_id=calibration_id,
            model_name=model_name,
            model_version=model_version,
            model_digest=model_digest,
            prompt_artifact_reference_id=prompt_artifact_reference_id,
            prompt_artifact_version=prompt_artifact_version,
            prompt_artifact_digest=prompt_artifact_digest,
            rubric_artifact_reference_id=rubric_artifact_reference_id,
            rubric_artifact_version=rubric_artifact_version,
            rubric_artifact_digest=rubric_artifact_digest,
            seed=seed,
            budget=dict(budget or {}),
            provenance_component=provenance_component,
            provenance_version=provenance_version,
            provenance_digest=provenance_digest,
        )
        self._metric(
            "observatory_calibration_review_count",
            {"outcome": "qualified" if report.eligible else "inconclusive"},
        )
        try:
            await self._persist_report(report)
        except CalibrationError as error:
            self._operation_span("calibrate", started, "failure", error_code=error.code)
            if claim is not None:
                try:
                    await self.repository.fail_calibration(claim)
                except Exception:
                    self._operation_span(
                        "calibrate", started, "failure", error_code="database_failure"
                    )
            raise
        except Exception:
            self._operation_span("calibrate", started, "failure", error_code="database_failure")
            if claim is not None:
                try:
                    await self.repository.fail_calibration(claim)
                except Exception:
                    self._operation_span(
                        "calibrate", started, "failure", error_code="database_failure"
                    )
            raise CalibrationError("database_failure") from None
        self._operation_span("calibrate", started, "success" if report.eligible else "insufficient")
        if report.calibration_result_id is not None:
            await self._publish_live(
                "evaluation_calibration",
                report.calibration_result_id,
                "state_changed",
                "eligible" if report.eligible else "inconclusive",
            )
        return report

    async def _persist_report(self, report: CalibrationReport) -> None:
        report.validate()
        if report.calibration_result_id is None:
            if report.calibration_id is not None:
                raise CalibrationError("validation")
            return
        put_result = getattr(self.repository, "put_calibration_result", None)
        if callable(put_result):
            typed_put = cast(
                Callable[[CalibrationReport], Awaitable[CalibrationReport]], put_result
            )
            try:
                await typed_put(report)
            except CalibrationError:
                raise
            except Exception:
                raise CalibrationError("storage_failure") from None

    async def _persist_report_recover(
        self, report: CalibrationReport, claim: CalibrationExecutionClaim | None
    ) -> None:
        """Persist every result through one failure path that releases its claim."""

        started = monotonic()
        try:
            await self._persist_report(report)
        except CalibrationError as error:
            self._operation_span("calibrate", started, "failure", error_code=error.code)
            if claim is not None:
                recovery_started = monotonic()
                try:
                    await self.repository.fail_calibration(claim)
                except Exception:
                    self._operation_span(
                        "recover", recovery_started, "failure", error_code="database_failure"
                    )
                else:
                    self._operation_span("recover", recovery_started, "success")
            raise
        except Exception:
            self._operation_span("calibrate", started, "failure", error_code="database_failure")
            if claim is not None:
                recovery_started = monotonic()
                try:
                    await self.repository.fail_calibration(claim)
                except Exception:
                    self._operation_span(
                        "recover", recovery_started, "failure", error_code="database_failure"
                    )
                else:
                    self._operation_span("recover", recovery_started, "success")
            raise CalibrationError("database_failure") from None

    async def qualify(
        self,
        item: Qualification,
        report: CalibrationReport,
        *,
        owner_authorized: bool,
        idempotency_key: str,
    ) -> Qualification:
        started = monotonic()
        production = not isinstance(self.repository, InMemoryCalibrationRepository)
        item.validate(strict=production)
        report.validate(strict=production)
        if production:
            result_lookup = getattr(self.repository, "get_calibration_result", None)
            if not callable(result_lookup) or item.calibration_result_id is None:
                self._operation_span(
                    "qualify", started, "rejected", error_code="qualification_denied"
                )
                raise CalibrationError("qualification_denied")
            try:
                stored_report = await result_lookup(item.calibration_result_id)
            except CalibrationError:
                raise
            except Exception:
                self._operation_span("qualify", started, "failure", error_code="database_failure")
                raise CalibrationError("database_failure") from None
            if stored_report is None or stored_report != report:
                self._operation_span(
                    "qualify", started, "rejected", error_code="qualification_denied"
                )
                raise CalibrationError("qualification_denied")
        durable_authorized = True
        authorization_check = getattr(self.repository, "is_authorized", None)
        if callable(authorization_check) and item.authorization_record_id is not None:
            try:
                durable_authorized = bool(
                    await authorization_check(
                        item.authorization_record_id,
                        "qualification",
                        resource_id=item.qualification_id,
                        scope_fingerprint=_qualification_fingerprint(item),
                    )
                )
            except CalibrationError:
                raise
            except Exception:
                self._operation_span("qualify", started, "failure", error_code="database_failure")
                raise CalibrationError("database_failure") from None
        elif not isinstance(self.repository, InMemoryCalibrationRepository):
            # A production repository without an authorization join is not an
            # authority.  The compatibility boolean is intentionally ignored.
            durable_authorized = False
        if (
            not owner_authorized
            or not durable_authorized
            or item.authorization_record_id is None
            or item.idempotency_key != idempotency_key
            or item.calibration_result_id is None
            or not report.eligible
            or (
                item.judge_id,
                item.judge_version,
                item.prompt_version,
                item.rubric_version,
                item.calibration_version,
            )
            != (
                report.judge_id,
                report.judge_version,
                report.prompt_version,
                report.rubric_version,
                report.calibration_version,
            )
            or (
                production
                and (
                    item.model_name,
                    item.model_version,
                    item.model_digest,
                    item.prompt_artifact_reference_id,
                    item.prompt_artifact_version,
                    item.prompt_artifact_digest,
                    item.rubric_artifact_reference_id,
                    item.rubric_artifact_version,
                    item.rubric_artifact_digest,
                    item.seed,
                    dict(item.budget),
                )
                != (
                    report.model_name,
                    report.model_version,
                    report.model_digest,
                    report.prompt_artifact_reference_id,
                    report.prompt_artifact_version,
                    report.prompt_artifact_digest,
                    report.rubric_artifact_reference_id,
                    report.rubric_artifact_version,
                    report.rubric_artifact_digest,
                    report.seed,
                    dict(report.budget),
                )
            )
            or any(
                expected is not None and getattr(item, field_name) != expected
                for field_name, expected in (
                    ("dataset_id", report.dataset_id),
                    ("dataset_version", report.dataset_version),
                    ("suite_id", report.suite_id),
                    ("suite_version", report.suite_version),
                    ("evaluator_id", report.evaluator_id),
                    ("evaluator_version", report.evaluator_version),
                    ("calibration_result_id", report.calibration_result_id),
                )
            )
        ):
            self._metric("observatory_calibration_qualification_count", {"outcome": "denied"})
            self._operation_span("qualify", started, "rejected", error_code="qualification_denied")
            raise CalibrationError("qualification_denied")
        try:
            result = await self.repository.put_qualification(item)
        except CalibrationError as error:
            self._operation_span("qualify", started, "failure", error_code=error.code)
            raise
        except Exception:
            self._operation_span("qualify", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        self._metric("observatory_calibration_qualification_count", {"outcome": "qualified"})
        self._operation_span("qualify", started, "success")
        await self._publish_live(
            "evaluation_qualification", result.qualification_id, "created", "qualified"
        )
        return result

    async def revoke(
        self,
        qualification_id: str,
        *,
        at: datetime | None = None,
        authorization_record_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Qualification:
        started = monotonic()
        try:
            current = (
                await self.repository.get_qualification(_uuid(qualification_id))
                if hasattr(self.repository, "get_qualification")
                else cast(
                    dict[str, Qualification], getattr(self.repository, "qualifications", {})
                ).get(_uuid(qualification_id))
            )
        except CalibrationError:
            raise
        except Exception:
            self._operation_span("revoke", started, "failure", error_code="database_failure")
            raise CalibrationError("database_failure") from None
        if current is None:
            self._operation_span("revoke", started, "not_found")
            raise CalibrationError("not_found")
        if authorization_record_id is None:
            self._operation_span("revoke", started, "rejected")
            raise CalibrationError("authorization_required")
        _uuid(authorization_record_id)
        checker = getattr(self.repository, "is_authorized", None)
        authorized_revoke = False
        if callable(checker):
            try:
                authorized_revoke = await checker(
                    authorization_record_id,
                    "revocation",
                    resource_id=qualification_id,
                    scope_fingerprint=_qualification_fingerprint(current),
                )
            except CalibrationError:
                raise
            except Exception:
                self._operation_span("revoke", started, "failure", error_code="database_failure")
                raise CalibrationError("database_failure") from None
            # Compatibility is limited to legacy in-memory records that lack
            # the versioned provenance required by the production schema.
            if not authorized_revoke and isinstance(self.repository, InMemoryCalibrationRepository):
                legacy = all(
                    getattr(current, name, None) is None
                    for name in (
                        "model_name",
                        "model_version",
                        "model_digest",
                        "prompt_artifact_reference_id",
                        "rubric_artifact_reference_id",
                        "prompt_artifact_version",
                        "rubric_artifact_version",
                        "prompt_artifact_digest",
                        "rubric_artifact_digest",
                    )
                )
                if legacy:
                    authorized_revoke = await checker(
                        authorization_record_id,
                        "revocation",
                        resource_id=qualification_id,
                        scope_fingerprint=None,
                    )
        if callable(checker) and not authorized_revoke:
            self._operation_span("revoke", started, "rejected")
            raise CalibrationError("authorization_required")
        if not callable(checker) and not isinstance(self.repository, InMemoryCalibrationRepository):
            raise CalibrationError("authorization_required")
        revoked_at = at or datetime.now(UTC)
        durable_revoke = getattr(self.repository, "revoke_qualification", None)
        if callable(durable_revoke):
            typed_revoke = cast(Callable[..., Awaitable[Qualification | None]], durable_revoke)
            try:
                updated = await typed_revoke(
                    qualification_id,
                    revoked_at=revoked_at,
                    authorization_record_id=authorization_record_id,
                    idempotency_key=idempotency_key,
                )
            except CalibrationError as error:
                self._operation_span("revoke", started, "failure", error_code=error.code)
                raise
            except Exception:
                self._operation_span("revoke", started, "failure", error_code="database_failure")
                raise CalibrationError("database_failure") from None
            if updated is None:
                self._operation_span("revoke", started, "not_found")
                raise CalibrationError("not_found")
            self._metric("observatory_calibration_qualification_count", {"outcome": "revoked"})
            self._operation_span("revoke", started, "success")
            await self._publish_live(
                "evaluation_qualification", updated.qualification_id, "revoked", "revoked"
            )
            return updated
        updated = Qualification(
            qualification_id=current.qualification_id,
            suite_id=current.suite_id,
            suite_version=current.suite_version,
            dataset_id=current.dataset_id,
            dataset_version=current.dataset_version,
            evaluator_id=current.evaluator_id,
            evaluator_version=current.evaluator_version,
            judge_id=current.judge_id,
            judge_version=current.judge_version,
            prompt_version=current.prompt_version,
            rubric_version=current.rubric_version,
            calibration_version=current.calibration_version,
            qualified=False,
            reason=current.reason,
            created_at=current.created_at,
            revoked_at=revoked_at,
            qualification_version=current.qualification_version,
            idempotency_key=current.idempotency_key,
            authorization_record_id=current.authorization_record_id,
            calibration_result_id=current.calibration_result_id,
            state="revoked",
            action_mode=current.action_mode,
            score_disposition="advisory_only",
            retention_mode=current.retention_mode,
            revocation_record_id=authorization_record_id,
        )
        qualifications = cast(
            dict[str, Qualification], getattr(self.repository, "qualifications", {})
        )
        qualifications[qualification_id] = updated
        audits = getattr(self.repository, "audits", None)
        if isinstance(audits, list):
            cast(list[dict[str, str]], audits).append(
                {"kind": "qualification", "outcome": "revoked"}
            )
        self._metric("observatory_calibration_qualification_count", {"outcome": "revoked"})
        self._operation_span("revoke", started, "success")
        return updated

    def is_qualified(
        self,
        *,
        suite_id: str,
        dataset_id: str,
        evaluator_id: str,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
    ) -> bool:
        # A durable repository is authoritative.  The synchronous helper is
        # retained only for the in-memory test adapter; production callers use
        # ``is_qualified_async`` below so a stale process-local map cannot
        # admit an ensemble.
        if not isinstance(self.repository, InMemoryCalibrationRepository):
            return False
        return any(
            item.active
            and (
                item.suite_id,
                item.dataset_id,
                item.evaluator_id,
                item.judge_id,
                item.judge_version,
                item.prompt_version,
                item.rubric_version,
                item.calibration_version,
            )
            == (
                suite_id,
                dataset_id,
                evaluator_id,
                judge_id,
                judge_version,
                prompt_version,
                rubric_version,
                calibration_version,
            )
            for item in cast(
                dict[str, Qualification], getattr(self.repository, "qualifications", {})
            ).values()
        )

    async def is_qualified_async(
        self,
        *,
        suite_id: str,
        dataset_id: str,
        evaluator_id: str,
        judge_id: str,
        judge_version: str,
        prompt_version: str,
        rubric_version: str,
        calibration_version: str,
    ) -> bool:
        resolver = getattr(self.repository, "active_qualification", None)
        if not callable(resolver):
            return False
        item = await resolver(
            suite_id=suite_id,
            dataset_id=dataset_id,
            evaluator_id=evaluator_id,
            judge_id=judge_id,
            judge_version=judge_version,
            instruction_version=prompt_version,
            rubric_version=rubric_version,
            calibration_version=calibration_version,
        )
        return item is not None and item.active

    def _metric(self, name: str, labels: Mapping[str, str]) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.record(name, labels=labels)
        except ValueError, TypeError:
            # Metric names/labels are service-owned constants.  A validation
            # failure is an implementation defect and must not disappear.
            raise CalibrationError("validation") from None

    def _operation_span(
        self, operation: str, started: float, outcome: str, *, error_code: str | None = None
    ) -> None:
        if self.metrics is None:
            return
        try:
            supported_outcomes = {"success", "failure", "duplicate", "timeout", "unavailable"}
            span_outcome = outcome if outcome in supported_outcomes else "failure"
            supported_errors = {
                "database_failure",
                "storage_failure",
                "transport_unavailable",
                "timeout",
                "validation",
                "provider_failure",
                "provider_timeout",
                "provider_unavailable",
                "insufficient",
                "rejected",
                "authorization_required",
                "not_found",
                "artifact_unauthorized",
                "duplicate",
                "idempotency_conflict",
                "authorization_conflict",
                "qualification_denied",
            }
            raw_error = error_code or (
                outcome if outcome in supported_errors else "storage_failure"
            )
            # The domain keeps conflict detail for callers, while telemetry
            # uses the finite public vocabulary shared by the component
            # manifest.  Never turn a normal duplicate or denial into a
            # storage failure.
            safe_error = {
                "idempotency_conflict": "duplicate",
                "authorization_conflict": "authorization_required",
            }.get(raw_error, raw_error)
            self.metrics.record(
                "observatory_calibration_duration_ms",
                max(0.0, (monotonic() - started) * 1000.0),
                labels={"operation": operation, "outcome": span_outcome},
            )
            record_span = getattr(self.metrics, "record_component_span", None)
            if callable(record_span):
                record_span(
                    component_id="aura.observatory.calibration",
                    component_version="1.0.0",
                    deployment_id=getattr(self.metrics, "deployment_id", "observatory"),
                    operation=operation,
                    duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                    outcome=span_outcome,
                    dependency="postgresql",
                    error_code=None if span_outcome == "success" else safe_error,
                )
        except ValueError, TypeError:
            raise CalibrationError("validation") from None


def combine_ensemble(
    ensemble_id: str,
    members: Sequence[EnsembleMember],
    *,
    all_qualified: bool,
    required_member_ids: Sequence[str] | None = None,
    required_versions: Mapping[str, tuple[str, str]] | None = None,
    qualification_states: Mapping[str, Qualification] | None = None,
) -> EnsembleResult:
    _uuid(ensemble_id)
    del all_qualified
    # ``all_qualified`` is retained as a source-compatible hint only.  It is
    # never an admission authority: production callers must supply the
    # durable qualification projection for every exact member.
    if (
        not 2 <= len(members) <= 16
        or qualification_states is None
        or not all(
            isinstance(qualification_states.get(item.member_id), Qualification)
            and item.qualification_id is not None
            and qualification_states[item.member_id].active
            and qualification_states[item.member_id].qualification_id == item.qualification_id
            and qualification_states[item.member_id].judge_id == item.judge_id
            and qualification_states[item.member_id].judge_version == item.judge_version
            and qualification_states[item.member_id].calibration_version == item.calibration_version
            and item.advisory_state is JudgeAdvisoryState.CALIBRATED
            for item in members
        )
    ):
        return EnsembleResult(
            ensemble_id,
            None,
            JudgeOutcome.INCONCLUSIVE,
            JudgeAdvisoryState.INCONCLUSIVE,
            tuple(item.member_id for item in members),
            "incomplete_calibration",
        )
    # The member contract carries only the exact qualification reference, so
    # the durable qualification projection must supply the remaining binding
    # fields. Require those fields and keep every member on one
    # prompt/rubric/calibration-result scope; a partial or mixed projection is
    # never safe to aggregate.
    qualification_scope: tuple[object, ...] | None = None
    for item in members:
        qualification = qualification_states[item.member_id]
        try:
            qualification.validate(strict=True)
        except CalibrationError:
            return EnsembleResult(
                ensemble_id,
                None,
                JudgeOutcome.INCONCLUSIVE,
                JudgeAdvisoryState.INCONCLUSIVE,
                tuple(member.member_id for member in members),
                "incomplete_calibration",
            )
        current_scope = (
            qualification.suite_id,
            qualification.suite_version,
            qualification.dataset_id,
            qualification.dataset_version,
            qualification.evaluator_id,
            qualification.evaluator_version,
            qualification.judge_id,
            qualification.judge_version,
            qualification.prompt_version,
            qualification.rubric_version,
            qualification.calibration_version,
            qualification.calibration_result_id,
            qualification.model_name,
            qualification.model_version,
            qualification.model_digest,
            qualification.prompt_artifact_reference_id,
            qualification.prompt_artifact_version,
            qualification.prompt_artifact_digest,
            qualification.rubric_artifact_reference_id,
            qualification.rubric_artifact_version,
            qualification.rubric_artifact_digest,
            qualification.seed,
            tuple(sorted(qualification.budget.items())),
            qualification.calibration_eligibility,
            qualification.action_mode,
            qualification.score_disposition,
            qualification.provenance_component,
            qualification.provenance_version,
            qualification.provenance_digest,
        )
        if (
            not current_scope[11]
            or qualification_scope is not None
            and current_scope != qualification_scope
        ):
            return EnsembleResult(
                ensemble_id,
                None,
                JudgeOutcome.INCONCLUSIVE,
                JudgeAdvisoryState.INCONCLUSIVE,
                tuple(item.member_id for item in members),
                "incomplete_calibration",
            )
        qualification_scope = current_scope
    member_ids = tuple(item.member_id for item in members)
    if required_member_ids is not None and set(member_ids) != set(required_member_ids):
        return EnsembleResult(
            ensemble_id,
            None,
            JudgeOutcome.INCONCLUSIVE,
            JudgeAdvisoryState.INCONCLUSIVE,
            member_ids,
            "missing_member",
        )
    if len(set(member_ids)) != len(member_ids):
        return EnsembleResult(
            ensemble_id,
            None,
            JudgeOutcome.INCONCLUSIVE,
            JudgeAdvisoryState.INCONCLUSIVE,
            member_ids,
            "duplicate_member",
        )
    versions_by_judge: dict[str, tuple[str, str]] = {}
    for member in members:
        version_key = (member.judge_version, member.calibration_version)
        previous = versions_by_judge.setdefault(member.judge_id, version_key)
        if previous != version_key:
            return EnsembleResult(
                ensemble_id,
                None,
                JudgeOutcome.INCONCLUSIVE,
                JudgeAdvisoryState.INCONCLUSIVE,
                member_ids,
                "version_incompatible",
            )
    total = 0.0
    weight = 0
    for member in members:
        member.validate()
        if required_versions is not None:
            expected = required_versions.get(member.judge_id)
            if expected is None or expected != (
                member.judge_version,
                member.calibration_version,
            ):
                return EnsembleResult(
                    ensemble_id,
                    None,
                    JudgeOutcome.INCONCLUSIVE,
                    JudgeAdvisoryState.INCONCLUSIVE,
                    member_ids,
                    "version_incompatible",
                )
        if (
            member.score is None
            or member.outcome is JudgeOutcome.INCONCLUSIVE
            or member.advisory_state is not JudgeAdvisoryState.CALIBRATED
        ):
            return EnsembleResult(
                ensemble_id,
                None,
                JudgeOutcome.INCONCLUSIVE,
                JudgeAdvisoryState.INCONCLUSIVE,
                member_ids,
                "incomplete_member",
            )
        total += member.score * member.weight
        weight += member.weight
    if weight != 1_000_000:
        return EnsembleResult(
            ensemble_id,
            None,
            JudgeOutcome.INCONCLUSIVE,
            JudgeAdvisoryState.INCONCLUSIVE,
            member_ids,
            "invalid_weight",
        )
    score = round(total / 1_000_000, 6)
    return EnsembleResult(
        ensemble_id,
        score,
        JudgeOutcome.PASS if score >= 0.5 else JudgeOutcome.FAIL,
        JudgeAdvisoryState.CALIBRATED,
        member_ids,
    )


__all__ = [
    "CalibrationError",
    "CalibrationExecutionClaim",
    "CalibrationLabel",
    "CalibrationReport",
    "CalibrationService",
    "CalibrationRepositoryPort",
    "DisagreementClass",
    "EnsembleMember",
    "EnsembleResult",
    "HumanReview",
    "InMemoryCalibrationRepository",
    "PostgresCalibrationRepository",
    "Qualification",
    "MIN_CALIBRATION_LABELS",
    "MIN_AGREEMENT",
    "MAX_MEAN_ABSOLUTE_ERROR",
    "MAX_DISAGREEMENT",
    "QUALIFICATION_OUTCOMES",
    "combine_ensemble",
]
