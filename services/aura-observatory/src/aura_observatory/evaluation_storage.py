"""Application services for authorized evaluation artifacts and scores.

This module is intentionally independent from the HTTP, Core, and event
modules.  It accepts only the narrow metadata shape needed by the later
evaluation work items.  Bytes are streamed once to the private object-store
port and are never represented in a domain record, log, metric, or database
parameter.
"""
# pyright: reportUnnecessaryIsInstance=false

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
from collections import defaultdict
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from aura_observatory.ports import (
    EvaluationArtifactRepository,
    EvaluationObjectStore,
    EvaluationScoreStore,
)

_LOGGER = logging.getLogger(__name__)


class _DeletionRepositoryFixture(Protocol):
    """Minimal structural seam for isolated, in-memory deletion tests."""

    async def claim_deletion_command(self, command_id: str) -> str | None: ...

    async def deletion_targets(
        self, command_id: str, *, limit: int
    ) -> Sequence[Mapping[str, str]]: ...

    async def mark_deletion_target(self, command_id: str, reference: str) -> None: ...

    async def delete_artifact_metadata(self, reference: str) -> None: ...

    async def complete_deletion_command(self, command_id: str, deleted_count: int) -> None: ...

    async def fail_deletion_command(self, command_id: str, failure_code: str) -> None: ...


class _ObjectStoreFixture(Protocol):
    async def put_stream(
        self, reference: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int: ...

    async def delete(self, reference: str) -> None: ...


class _ScoreStoreFixture(Protocol):
    async def delete_scores_for_artifact(self, reference: str) -> None: ...


MAX_IDENTIFIER_LENGTH = 128
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_COMPONENT_ID_RE = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_RESERVED_VERSION_SEGMENTS = frozenset(
    {
        "argument",
        "bearer",
        "bytes",
        "comment",
        "content",
        "credential",
        "document",
        "endpoint",
        "filename",
        "identity",
        "memory",
        "owner",
        "password",
        "path",
        "payload",
        "private",
        "prompt",
        "query",
        "raw",
        "response",
        "result",
        "rubric",
        "secret",
        "sensitive",
        "token",
        "url",
    }
)
ALLOWED_AUTHORIZATION_PURPOSES = frozenset(
    {"synthetic_benchmark", "owner_supplied_evaluation", "controlled_replay", "human_review"}
)
ALLOWED_CAPTURE_CLASSIFICATIONS = frozenset({"redacted", "sampled_content", "full_content"})
ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "audio/wav",
    }
)
ALLOWED_MEDIA_CLASSIFICATIONS = frozenset(
    {
        "evaluation_input",
        "evaluation_output",
        "evaluation_context",
        "evaluation_evidence",
        "evaluation_report",
    }
)


class ArtifactStorageError(RuntimeError):
    """Bounded, content-free artifact failure."""

    def __init__(self, failure_code: str, message: str | None = None) -> None:
        self.failure_code = failure_code
        super().__init__(message or failure_code)


def _utc_timestamp(value: datetime) -> str:
    """Render an aware UTC instant in evaluation-v1's canonical ``Z`` form."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ArtifactStorageError("score_invalid")
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")[:-6]
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered + "Z"


class DeletionFailure(RuntimeError):
    """Bounded, recoverable deletion failure."""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__(failure_code)


# These are the only provider/database failure values that may cross an
# Observatory storage boundary.  Exception messages are deliberately never
# used for classification: adapters attach one of these values, and an
# unexpected exception receives the operation-specific safe fallback.
_STORAGE_FAILURE_CODES = frozenset(
    {
        "storage_failure",
        "storage_authentication",
        "storage_integrity",
        "storage_timeout",
        "database_failure",
    }
)
_ARTIFACT_STREAM_FAILURE_CODES = _STORAGE_FAILURE_CODES | frozenset(
    {"artifact_limit_exceeded", "malformed_stream", "integrity_mismatch", "integrity_invalid"}
)
_DELETION_FAILURE_CODES = _STORAGE_FAILURE_CODES | frozenset({"target_invalid", "lease_lost"})


@dataclass(frozen=True, slots=True)
class ArtifactUpload:
    """A validated metadata envelope around one untrusted byte stream."""

    artifact_id: str
    artifact_reference_id: str
    request_id: str
    result_id: str
    idempotency_key: str
    authorization_record_id: str
    capture_classification: str
    media_classification: str
    media_type: str
    expected_sha256: str
    max_bytes: int
    job_id: str
    dataset_id: str
    suite_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    authorization_purpose: str = "owner_supplied_evaluation"
    authorization_context: str = "controlled_evaluation"
    intended_use: str | None = None
    artifact_version: str = "1.0.0"

    def validate(self, configured_max_bytes: int) -> None:
        for name in (
            "artifact_id",
            "artifact_reference_id",
            "request_id",
            "result_id",
            "idempotency_key",
            "authorization_record_id",
            "job_id",
            "dataset_id",
            "suite_id",
        ):
            _require_uuid(getattr(self, name), name)
        if self.capture_classification not in ALLOWED_CAPTURE_CLASSIFICATIONS:
            raise ArtifactStorageError("authorization_required")
        if self.authorization_purpose not in ALLOWED_AUTHORIZATION_PURPOSES:
            raise ArtifactStorageError("authorization_required")
        if self.authorization_context != "controlled_evaluation":
            raise ArtifactStorageError("authorization_required")
        if self.media_classification not in ALLOWED_MEDIA_CLASSIFICATIONS:
            raise ArtifactStorageError("unsupported_media")
        if self.media_type not in ALLOWED_MEDIA_TYPES:
            raise ArtifactStorageError("unsupported_media")
        if not _is_sha256(self.expected_sha256):
            raise ArtifactStorageError("integrity_invalid")
        if self.max_bytes < 0 or self.max_bytes > min(configured_max_bytes, 1_073_741_824):
            raise ArtifactStorageError("artifact_limit_exceeded")
        if not _VERSION_RE.fullmatch(self.artifact_version):
            raise ArtifactStorageError("authorization_required")
        # AURA-0090 callers predating intended_use are normalized to input at
        # the service boundary.  New content-bearing callers must provide an
        # explicit use so the role matrix below is enforced.
        if self.intended_use is None:
            return
        if self.intended_use not in {"prompt", "rubric", "input", "comment"}:
            raise ArtifactStorageError("authorization_required")
        if (
            self.intended_use in {"prompt", "rubric", "input"}
            and self.media_classification != "evaluation_input"
        ):
            raise ArtifactStorageError("authorization_required")
        if (
            self.intended_use in {"prompt", "rubric"}
            and self.authorization_purpose == "controlled_replay"
        ) or (
            self.intended_use == "input"
            and self.authorization_purpose
            not in {
                "synthetic_benchmark",
                "owner_supplied_evaluation",
                "controlled_replay",
            }
        ):
            raise ArtifactStorageError("authorization_required")
        if self.intended_use == "comment" and (
            self.authorization_purpose != "human_review"
            or self.media_classification != "evaluation_evidence"
        ):
            raise ArtifactStorageError("authorization_required")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    artifact_reference_id: str
    request_id: str
    result_id: str
    idempotency_key: str
    authorization_record_id: str
    capture_classification: str
    media_classification: str
    media_type: str
    byte_count: int
    sha256: str
    job_id: str
    dataset_id: str
    suite_id: str
    created_at: datetime
    authorization_purpose: str = "owner_supplied_evaluation"
    authorization_context: str = "controlled_evaluation"
    intended_use: str = "input"
    artifact_version: str = "1.0.0"


class DeletionKind(StrEnum):
    JOB = "job"
    DATASET = "dataset"
    SUITE = "suite"
    CUTOFF = "cutoff"
    PURGE_ALL = "purge_all"


@dataclass(frozen=True, slots=True)
class DeletionSelector:
    kind: DeletionKind
    value: str | datetime | None = None

    def validate(self) -> None:
        if self.kind is DeletionKind.PURGE_ALL:
            if self.value is not None:
                raise ArtifactStorageError("deletion_selector_invalid")
            return
        if self.kind is DeletionKind.CUTOFF:
            if not isinstance(self.value, datetime) or not _is_zero_utc(self.value):
                raise ArtifactStorageError("deletion_selector_invalid")
            return
        if not isinstance(self.value, str):
            raise ArtifactStorageError("deletion_selector_invalid")
        _require_uuid(self.value, f"{self.kind.value}_id")


@dataclass(frozen=True, slots=True)
class DeletionCommand:
    command_id: str
    idempotency_key: str
    selector: DeletionSelector
    confirmation: str
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def validate(self) -> None:
        _require_uuid(self.command_id, "command_id")
        _require_uuid(self.idempotency_key, "idempotency_key")
        if not _is_zero_utc(self.requested_at):
            raise ArtifactStorageError("deletion_requested_at_invalid")
        self.selector.validate()
        if self.confirmation != "DELETE_EVALUATION_ARTIFACTS":
            raise ArtifactStorageError("deletion_confirmation_required")


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    score_id: str
    job_id: str
    result_id: str
    dataset_id: str
    suite_id: str
    evaluator_id: str
    evaluator_version: str
    evaluator_kind: str
    component_id: str
    component_version: str
    capture_classification: str
    evidence_status: str
    value: float | None
    outcome: str | None
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
    metric: str = "evaluation.score"
    failure_code: str | None = None
    evidence_reference: str | None = None
    reproducibility: str = "deterministic-v1"
    threshold_applicable: bool | None = None
    threshold_reference: str | None = None
    threshold_value: float | None = None
    threshold_operator: str | None = None
    threshold_status: str | None = None
    # Exact local-judge provenance; all fields are bounded metadata only.
    judge_definition_id: str | None = None
    judge_definition_version: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_digest: str | None = None
    prompt_version: str | None = None
    rubric_version: str | None = None
    calibration_version: str | None = None
    seed: int | None = None
    budget_timeout_ms: int | None = None
    budget_max_input_bytes: int | None = None
    budget_max_output_bytes: int | None = None
    budget_max_output_tokens: int | None = None
    budget_max_context_tokens: int | None = None
    budget_max_concurrency: int | None = None
    score_disposition: str | None = None
    qualification_id: str | None = None
    qualification_version: str | None = None
    uncertainty: float | None = None
    disagreement: str | None = None
    prompt_artifact_id: str | None = None
    prompt_artifact_version: str | None = None
    prompt_artifact_digest: str | None = None
    rubric_artifact_id: str | None = None
    rubric_artifact_version: str | None = None
    rubric_artifact_digest: str | None = None
    calibration_id: str | None = None

    def validate(self) -> None:
        for name in (
            "score_id",
            "job_id",
            "result_id",
            "dataset_id",
            "suite_id",
            "evaluator_id",
        ):
            _require_uuid(getattr(self, name), name)
        if self.artifact_reference_id is not None:
            _require_uuid(self.artifact_reference_id, "artifact_reference_id")
        if self.case_id is not None:
            _require_uuid(self.case_id, "case_id")
        for name in (
            "judge_definition_id",
            "qualification_id",
            "prompt_artifact_id",
            "rubric_artifact_id",
            "calibration_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_uuid(value, name)
        for name in (
            "judge_definition_version",
            "model_version",
            "prompt_version",
            "rubric_version",
            "calibration_version",
            "qualification_version",
            "prompt_artifact_version",
            "rubric_artifact_version",
        ):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", value):
                raise ArtifactStorageError("score_invalid")
        if self.model_name is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model_name
        ):
            raise ArtifactStorageError("score_invalid")
        if self.model_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.model_digest):
            raise ArtifactStorageError("score_invalid")
        for value in (self.prompt_artifact_digest, self.rubric_artifact_digest):
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ArtifactStorageError("score_invalid")
        for value in (self.code_revision, self.configuration_revision, self.provenance_version):
            if value is not None and (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value)
                or any(
                    marker in value.casefold()
                    for marker in ("owner", "identity", "credential", "token", "secret", "content")
                )
            ):
                raise ArtifactStorageError("score_invalid")
        for value in (self.image_digest, self.provenance_digest):
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ArtifactStorageError("score_invalid")
        if self.provenance_component is not None and not re.fullmatch(
            r"aura\.[a-z][a-z0-9_.-]{0,90}", self.provenance_component
        ):
            raise ArtifactStorageError("score_invalid")
        if self.execution_provenance is not None:
            _validate_execution_provenance(self.execution_provenance)
        if self.seed is not None and (
            isinstance(self.seed, bool) or not 0 <= self.seed <= 2_147_483_647
        ):
            raise ArtifactStorageError("score_invalid")
        for value, low, high in (
            (self.budget_timeout_ms, 100, 120_000),
            (self.budget_max_input_bytes, 1, 1_048_576),
            (self.budget_max_output_bytes, 1, 8_192),
            (self.budget_max_output_tokens, 1, 2_048),
            (self.budget_max_context_tokens, 1, 16_384),
            (self.budget_max_concurrency, 1, 2),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
            ):
                raise ArtifactStorageError("score_invalid")
        if self.score_disposition is not None and self.score_disposition not in {
            "advisory_only",
            "calibrated_enabled",
        }:
            raise ArtifactStorageError("score_invalid")
        if self.uncertainty is not None and (
            not math.isfinite(self.uncertainty) or not 0 <= self.uncertainty <= 1
        ):
            raise ArtifactStorageError("score_invalid")
        if self.disagreement is not None and self.disagreement not in {
            "none",
            "within_tolerance",
            "score_disagreement",
            "outcome_disagreement",
            "insufficient_evidence",
        }:
            raise ArtifactStorageError("score_invalid")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,95}", self.metric):
            raise ArtifactStorageError("score_invalid")
        for value, _name in (
            (self.failure_code, "failure_code"),
            (self.evidence_reference, "evidence_reference"),
            (self.threshold_reference, "threshold_reference"),
            (self.reproducibility, "reproducibility"),
        ):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 128
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
            ):
                raise ArtifactStorageError("score_invalid")
        if self.threshold_applicable is not None and not isinstance(
            self.threshold_applicable, bool
        ):
            raise ArtifactStorageError("score_invalid")
        if self.threshold_value is not None and (
            not math.isfinite(self.threshold_value) or abs(self.threshold_value) > 1.0e15
        ):
            raise ArtifactStorageError("score_invalid")
        if self.threshold_operator is not None and self.threshold_operator not in {
            "greater_than_or_equal",
            "less_than_or_equal",
            "equal",
        }:
            raise ArtifactStorageError("score_invalid")
        if self.threshold_status is not None and self.threshold_status not in {
            "pass",
            "fail",
            "inconclusive",
            "not_applicable",
        }:
            raise ArtifactStorageError("score_invalid")
        if self.threshold_applicable is False and any(
            value is not None
            for value in (
                self.threshold_reference,
                self.threshold_value,
                self.threshold_operator,
                self.threshold_status,
            )
        ):
            raise ArtifactStorageError("score_invalid")
        if self.evaluator_kind not in {"deterministic", "local_judge", "human_review"}:
            raise ArtifactStorageError("score_invalid")
        if self.capture_classification not in {
            "metadata_only",
            "hashed",
            "redacted",
            "sampled_content",
            "full_content",
        }:
            raise ArtifactStorageError("score_invalid")
        if self.evidence_status not in {"observed", "not_observed", "not_applicable"}:
            raise ArtifactStorageError("score_invalid")
        if self.evidence_status == "observed":
            if self.value is None or self.outcome not in {"pass", "fail", "inconclusive"}:
                raise ArtifactStorageError("score_invalid")
        elif self.value is not None or self.outcome is not None:
            raise ArtifactStorageError("score_invalid")
        if self.value is not None and (not math.isfinite(self.value) or abs(self.value) > 1e15):
            raise ArtifactStorageError("score_invalid")
        _utc_timestamp(self.observed_at)
        _require_version(self.evaluator_version, "evaluator_version")
        _require_version(self.component_version, "component_version")
        _require_component_id(self.component_id)

    def as_local_judge_contract(self) -> dict[str, object]:
        """Return the closed evaluation-v1 LocalJudgeScoreMetadata projection."""

        self.validate()
        if self.evaluator_kind != "local_judge":
            raise ArtifactStorageError("score_invalid")
        required = (
            self.judge_definition_id,
            self.judge_definition_version,
            self.model_name,
            self.model_version,
            self.model_digest,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
            self.prompt_artifact_id,
            self.prompt_artifact_version,
            self.prompt_artifact_digest,
            self.rubric_artifact_id,
            self.rubric_artifact_version,
            self.rubric_artifact_digest,
            self.calibration_id,
            self.seed,
            self.budget_timeout_ms,
            self.budget_max_input_bytes,
            self.budget_max_output_bytes,
            self.budget_max_output_tokens,
            self.budget_max_context_tokens,
            self.budget_max_concurrency,
            self.score_disposition,
            self.disagreement,
        )
        if any(value is None for value in required):
            raise ArtifactStorageError("score_invalid")
        budget = {
            "timeoutMs": self.budget_timeout_ms,
            "maxInputBytes": self.budget_max_input_bytes,
            "maxOutputBytes": self.budget_max_output_bytes,
            "maxOutputTokens": self.budget_max_output_tokens,
            "maxContextTokens": self.budget_max_context_tokens,
            "maxConcurrency": self.budget_max_concurrency,
            "temperature": 0,
        }
        value: dict[str, object] = {
            "judgeDefinition": {
                "judgeDefinitionId": self.judge_definition_id,
                "judgeDefinitionVersion": self.judge_definition_version,
            },
            "model": {
                "provider": "local_ollama",
                "modelName": self.model_name,
                "modelVersion": self.model_version,
                "modelDigest": self.model_digest,
            },
            "prompt": {
                "artifactId": self.prompt_artifact_id,
                "artifactVersion": self.prompt_artifact_version,
                "purpose": "judge_prompt",
                "digest": self.prompt_artifact_digest,
            },
            "rubric": {
                "artifactId": self.rubric_artifact_id,
                "artifactVersion": self.rubric_artifact_version,
                "purpose": "judge_rubric",
                "digest": self.rubric_artifact_digest,
            },
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "seed": self.seed,
            "budget": budget,
            "scoreDisposition": self.score_disposition,
            "disagreement": self.disagreement,
        }
        if self.qualification_id is not None:
            value["qualification"] = {
                "qualificationId": self.qualification_id,
                "qualificationVersion": self.qualification_version,
            }
        return value


def _validate_execution_provenance(value: Mapping[str, object]) -> None:
    """Validate the closed, content-free execution provenance envelope."""

    # AURA-0094 rows may still carry the original bounded reproducibility
    # marker.  Keep accepting that legacy value at the write boundary so an
    # upgrade does not make old deterministic evaluators unusable; the
    # AURA-0095 query adapter deliberately treats such rows as partial.  New
    # authoritative score rows use the complete structured shape below.
    if isinstance(value, str):
        if not _VERSION_RE.fullmatch(value) or len(value) > 128:
            raise ArtifactStorageError("score_invalid")
        return

    allowed = {
        "dataset",
        "suite",
        "evaluators",
        "components",
        "model",
        "deploymentRevision",
        "reproducibility",
    }
    bounded_value = value
    if {str(key) for key in bounded_value} - allowed:
        raise ArtifactStorageError("score_invalid")
    for reference_name in ("dataset", "suite"):
        reference = bounded_value.get(reference_name)
        if not isinstance(reference, Mapping) or {
            str(key) for key in cast(Mapping[str, object], reference)
        } != {
            "resourceId",
            "resourceVersion",
        }:
            raise ArtifactStorageError("score_invalid")
    for collection_name in ("evaluators", "components"):
        collection = bounded_value.get(collection_name)
        if not isinstance(collection, list) or not 1 <= len(cast(list[object], collection)) <= 32:
            raise ArtifactStorageError("score_invalid")
    legacy_reproducibility = bounded_value.get("reproducibility")
    if isinstance(legacy_reproducibility, str):
        if not _VERSION_RE.fullmatch(legacy_reproducibility) or len(legacy_reproducibility) > 128:
            raise ArtifactStorageError("score_invalid")
        return
    evaluators = cast(list[object], bounded_value["evaluators"])
    for evaluator in evaluators:
        if not isinstance(evaluator, Mapping):
            raise ArtifactStorageError("score_invalid")
        evaluator_mapping = cast(Mapping[str, object], evaluator)
        if set(evaluator_mapping) != {
            "evaluatorId",
            "evaluatorVersion",
            "kind",
        }:
            raise ArtifactStorageError("score_invalid")
        evaluator_id = evaluator_mapping.get("evaluatorId")
        evaluator_version = evaluator_mapping.get("evaluatorVersion")
        if not isinstance(evaluator_id, str) or not isinstance(evaluator_version, str):
            raise ArtifactStorageError("score_invalid")
        _require_uuid(evaluator_id, "provenance.evaluatorId")
        if not _VERSION_RE.fullmatch(evaluator_version):
            raise ArtifactStorageError("score_invalid")
        if evaluator_mapping.get("kind") not in {"deterministic", "local_judge", "human_review"}:
            raise ArtifactStorageError("score_invalid")
    components = cast(list[object], bounded_value["components"])
    for component in components:
        if not isinstance(component, Mapping):
            raise ArtifactStorageError("score_invalid")
        component_mapping = cast(Mapping[str, object], component)
        if set(component_mapping) != {
            "componentId",
            "componentVersion",
            "codeRevision",
            "imageDigest",
            "configurationRevision",
        }:
            raise ArtifactStorageError("score_invalid")
        component_id = component_mapping.get("componentId")
        component_version = component_mapping.get("componentVersion")
        code_revision = component_mapping.get("codeRevision")
        image_digest = component_mapping.get("imageDigest")
        configuration_revision = component_mapping.get("configurationRevision")
        if not all(
            isinstance(item, str)
            for item in (
                component_id,
                component_version,
                code_revision,
                image_digest,
                configuration_revision,
            )
        ):
            raise ArtifactStorageError("score_invalid")
        _require_component_id(cast(str, component_id))
        if not _VERSION_RE.fullmatch(cast(str, component_version)):
            raise ArtifactStorageError("score_invalid")
        for revision in (code_revision, configuration_revision):
            if (
                not 1 <= len(cast(str, revision)) <= 128
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", cast(str, revision)) is None
                or any(
                    marker in cast(str, revision).casefold()
                    for marker in ("owner", "identity", "credential", "token", "secret", "content")
                )
            ):
                raise ArtifactStorageError("score_invalid")
        if not _is_sha256(cast(str, image_digest)):
            raise ArtifactStorageError("score_invalid")
    model = bounded_value.get("model")
    if not isinstance(model, Mapping):
        raise ArtifactStorageError("score_invalid")
    model_mapping = cast(Mapping[str, object], model)
    if set(model_mapping) != {"modelId", "modelVersion", "modelDigest"}:
        raise ArtifactStorageError("score_invalid")
    model_id = model_mapping.get("modelId")
    model_version = model_mapping.get("modelVersion")
    model_digest = model_mapping.get("modelDigest")
    if (
        not isinstance(model_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,127}(?::[a-z0-9._+-]+)?", model_id) is None
        or any(
            marker in model_id.casefold()
            for marker in (
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
            )
        )
        or not isinstance(model_version, str)
        or not _VERSION_RE.fullmatch(model_version)
        or not isinstance(model_digest, str)
        or not _is_sha256(model_digest)
    ):
        raise ArtifactStorageError("score_invalid")
    deployment_revision = bounded_value.get("deploymentRevision")
    if (
        not isinstance(deployment_revision, str)
        or not 1 <= len(deployment_revision) <= 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", deployment_revision) is None
    ):
        raise ArtifactStorageError("score_invalid")
    reproducibility = bounded_value.get("reproducibility")
    if not isinstance(reproducibility, Mapping):
        raise ArtifactStorageError("score_invalid")
    reproducibility_mapping = cast(Mapping[str, object], reproducibility)
    if set(reproducibility_mapping) != {
        "seed",
        "clockMode",
        "timeZone",
        "locale",
        "fixtureRevision",
        "promptRevision",
        "policyRevision",
    }:
        raise ArtifactStorageError("score_invalid")
    seed = reproducibility_mapping.get("seed")
    locale = reproducibility_mapping.get("locale")
    if (
        type(seed) is not int
        or not 0 <= seed <= 2_147_483_647
        or reproducibility_mapping.get("clockMode") not in {"fixed", "recorded"}
        or reproducibility_mapping.get("timeZone") != "UTC"
        or not isinstance(locale, str)
        or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale) is None
    ):
        raise ArtifactStorageError("score_invalid")
    for revision_name in ("fixtureRevision", "promptRevision", "policyRevision"):
        revision = reproducibility_mapping.get(revision_name)
        if (
            not isinstance(revision, str)
            or not 1 <= len(revision) <= 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", revision) is None
            or any(
                marker in revision.casefold()
                for marker in ("owner", "identity", "credential", "token", "secret", "content")
            )
        ):
            raise ArtifactStorageError("score_invalid")
    encoded = json.dumps(bounded_value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 16_384 or any(
        marker in encoded.casefold()
        for marker in ("response", "content", "credential", "endpoint", "token", "owner")
    ):
        raise ArtifactStorageError("score_invalid")


class EvaluationMetrics:
    """Bounded runtime metric sink for the worker-owned storage paths."""

    _ALLOWED = frozenset(
        {
            "observatory_evaluation_artifact_operation_count",
            "observatory_evaluation_artifact_operation_duration_ms",
            "observatory_evaluation_deletion_action_count",
            "observatory_evaluation_deletion_deleted_count",
            "observatory_evaluation_deletion_failure_count",
            "observatory_evaluation_deletion_duration_ms",
        }
    )
    _MAX_SERIES = 64

    def __init__(self) -> None:
        self.values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def record(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        if name not in self._ALLOWED or not math.isfinite(float(value)):
            return
        bounded_labels = tuple(
            sorted((str(key), str(val)[:32]) for key, val in (labels or {}).items())
        )
        key = (name, bounded_labels)
        if key not in self.values and len(self.values) >= self._MAX_SERIES:
            return
        self.values[key] += max(0.0, float(value))

    def snapshot(self) -> dict[str, float]:
        return {f"{name}|{labels}": value for (name, labels), value in self.values.items()}


class ArtifactStorageService:
    """Stream, verify, and publish one explicitly authorized artifact."""

    def __init__(
        self,
        repository: Any,
        object_store: Any,
        *,
        max_bytes: int,
        metrics: Any | None = None,
        event_publisher: Callable[[str, str, str, str], Awaitable[object]] | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.max_bytes = max_bytes
        self.metrics = metrics or EvaluationMetrics()
        self.event_publisher = event_publisher

    async def store(self, request: ArtifactUpload, stream: AsyncIterable[bytes]) -> ArtifactRecord:
        started = _monotonic()
        try:
            request.validate(self.max_bytes)
        except ArtifactStorageError:
            self._record_artifact_failure(started)
            raise
        if request.intended_use is None:
            request = replace(request, intended_use="input")
        if request.capture_classification not in ALLOWED_CAPTURE_CLASSIFICATIONS:
            self._record_artifact_failure(started)
            raise ArtifactStorageError("authorization_required")
        try:
            reserved = await self.repository.reserve_artifact(request)
        except ArtifactStorageError:
            self._record_artifact_failure(started)
            raise
        except Exception as exc:
            # Reservation is a PostgreSQL boundary.  Preserve an explicit
            # bounded adapter code (notably auth/timeout) but classify an
            # untyped connection/query/transaction error as database_failure.
            failure_code = _bounded_failure_code(
                exc, _STORAGE_FAILURE_CODES, fallback="database_failure"
            )
            self._record_artifact_failure(started)
            raise ArtifactStorageError(failure_code) from exc
        if isinstance(reserved, ArtifactRecord):
            self._metric(
                "observatory_evaluation_artifact_operation_count",
                labels={"operation": "store", "outcome": "duplicate"},
            )
            self._metric(
                "observatory_evaluation_artifact_operation_duration_ms",
                _elapsed_ms(started),
                {"operation": "store"},
            )
            return reserved
        if reserved is False:
            self._record_artifact_failure(started)
            raise ArtifactStorageError("authorization_required")
        verifier = _DigestingStream(stream, request.max_bytes)
        try:
            byte_count = await _put_stream_with_digest(
                self.object_store,
                request.artifact_reference_id,
                verifier,
                content_type=request.media_type,
                expected_sha256=request.expected_sha256,
            )
            if verifier.byte_count != byte_count and byte_count > 0:
                raise ArtifactStorageError("integrity_invalid")
            digest = verifier.hexdigest
            if verifier.byte_count > request.max_bytes:
                raise ArtifactStorageError("artifact_limit_exceeded")
            if digest != request.expected_sha256:
                raise ArtifactStorageError("integrity_mismatch")
            record = ArtifactRecord(
                artifact_id=request.artifact_id,
                artifact_reference_id=request.artifact_reference_id,
                request_id=request.request_id,
                result_id=request.result_id,
                idempotency_key=request.idempotency_key,
                authorization_record_id=request.authorization_record_id,
                capture_classification=request.capture_classification,
                media_classification=request.media_classification,
                media_type=request.media_type,
                byte_count=verifier.byte_count,
                sha256=digest,
                job_id=request.job_id,
                dataset_id=request.dataset_id,
                suite_id=request.suite_id,
                created_at=request.created_at.astimezone(UTC),
                authorization_purpose=request.authorization_purpose,
                authorization_context=request.authorization_context,
                intended_use=request.intended_use or "input",
                artifact_version=request.artifact_version,
            )
        except ArtifactStorageError as exc:
            await _best_effort_delete(self.object_store, request.artifact_reference_id)
            await self._mark_artifact_failed(request.artifact_reference_id, exc.failure_code)
            self._record_artifact_failure(started)
            raise
        except Exception as exc:
            await _best_effort_delete(self.object_store, request.artifact_reference_id)
            failure_code = _bounded_failure_code(
                exc, _ARTIFACT_STREAM_FAILURE_CODES, fallback="storage_failure"
            )
            await self._mark_artifact_failed(request.artifact_reference_id, failure_code)
            self._record_artifact_failure(started)
            raise ArtifactStorageError(failure_code) from exc

        try:
            await self.repository.publish_artifact(record)
        except ArtifactStorageError as exc:
            await _best_effort_delete(self.object_store, request.artifact_reference_id)
            await self._mark_artifact_failed(request.artifact_reference_id, exc.failure_code)
            self._record_artifact_failure(started)
            raise
        except Exception as exc:
            await _best_effort_delete(self.object_store, request.artifact_reference_id)
            source_code = _bounded_failure_code(
                exc, _STORAGE_FAILURE_CODES, fallback="database_failure"
            )
            await self._mark_artifact_failed(request.artifact_reference_id, source_code)
            self._record_artifact_failure(started)
            raise ArtifactStorageError(source_code) from exc

        self._metric(
            "observatory_evaluation_artifact_operation_count",
            labels={"operation": "store", "outcome": "succeeded"},
        )
        self._metric(
            "observatory_evaluation_artifact_operation_duration_ms",
            _elapsed_ms(started),
            {"operation": "store"},
        )
        await self._publish_live("evaluation_artifact", record.artifact_id, "created", "available")
        return record

    async def _publish_live(
        self, resource_type: str, resource_id: str, change: str, state: str
    ) -> None:
        if self.event_publisher is None:
            return
        try:
            await self.event_publisher(resource_type, resource_id, change, state)
        except Exception:
            _LOGGER.debug("evaluation artifact live notification unavailable")

    async def _mark_artifact_failed(self, reference: str, failure_code: str) -> None:
        """Persist a bounded failure without replacing it with DB diagnostics."""

        try:
            await self.repository.mark_artifact_failed(reference, failure_code)
        except Exception:
            # The original bounded failure remains the public result.  A
            # later recovery pass can reconcile a failure row if the database
            # was unavailable while recording it.
            return

    def _record_artifact_failure(self, started: float) -> None:
        self._metric(
            "observatory_evaluation_artifact_operation_count",
            labels={"operation": "store", "outcome": "failed"},
        )
        self._metric(
            "observatory_evaluation_artifact_operation_duration_ms",
            _elapsed_ms(started),
            {"operation": "store"},
        )

    def _metric(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        recorder = getattr(self.metrics, "record", None)
        if callable(recorder):
            recorder(name, value, labels)


class EvaluationDeletionService:
    """Recoverable object-first deletion driven by immutable PG targets."""

    def __init__(
        self,
        repository: EvaluationArtifactRepository | _DeletionRepositoryFixture,
        object_store: EvaluationObjectStore | _ObjectStoreFixture,
        score_store: EvaluationScoreStore | _ScoreStoreFixture,
        metrics: Any | None = None,
        event_publisher: Callable[[str, str, str, str], Awaitable[object]] | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.score_store = score_store
        self.metrics = metrics or EvaluationMetrics()
        self.event_publisher = event_publisher

    async def create_command(self, command: DeletionCommand) -> DeletionCommand:
        """Capture immutable artifact and score targets before queueing work."""

        command.validate()
        list_ids = getattr(self.score_store, "list_score_ids_for_selector", None)
        score_ids: Sequence[str] = ()
        if callable(list_ids):
            score_ids = await cast(Any, list_ids)(
                command.selector, snapshot_at=command.requested_at, limit=10_000
            )
        create = getattr(self.repository, "create_deletion_command", None)
        if not callable(create):
            raise ArtifactStorageError("database_failure")
        created = await cast(Any, create)(command, score_ids=tuple(score_ids))
        await self._publish_live(
            "evaluation_deletion", command.command_id, "deletion_requested", "queued"
        )
        return created

    async def resume_pending(self, *, page_size: int = 100, limit: int = 16) -> int:
        """Resume persisted deletion commands without exposing repository state."""

        bounded_limit = max(1, min(16, int(limit)))
        pending = cast(
            Callable[..., Awaitable[list[DeletionCommand]]] | None,
            getattr(self.repository, "pending_deletion_commands", None),
        )
        if not callable(pending):
            return 0
        commands: list[DeletionCommand] = await pending(limit=bounded_limit)
        processed = 0
        for command in commands:
            await self.execute(command, page_size=page_size)
            processed += 1
        return processed

    async def execute(self, command: DeletionCommand, *, page_size: int = 100) -> int:
        """Execute one command and emit one bounded duration observation."""

        started = _monotonic()
        outcome = "failed"
        try:
            result = await self._execute(command, page_size=page_size)
        except BaseException:
            raise
        else:
            outcome = "succeeded"
            return result
        finally:
            self._metric(
                "observatory_evaluation_deletion_duration_ms",
                _elapsed_ms(started),
                {"selector": command.selector.kind.value, "outcome": outcome},
            )

    async def _execute(self, command: DeletionCommand, *, page_size: int = 100) -> int:
        command.validate()
        page_size = max(1, min(1000, int(page_size)))
        claimed = await self.repository.claim_deletion_command(command.command_id)
        if claimed is None:
            return 0
        lease = _DeletionLeaseGuard(self.repository, command.command_id, str(claimed))
        await lease.start()
        deleted = 0
        self._metric(
            "observatory_evaluation_deletion_action_count",
            labels={
                "selector": command.selector.kind.value,
                "phase": "started",
                "outcome": "running",
            },
        )
        try:
            try:
                await lease.ensure()
                await _fenced_phase(self.repository, command.command_id, "scores", lease.token)
                delete_ids: Any = getattr(self.score_store, "delete_scores_for_ids", None)
                score_target_reader: Any = getattr(self.repository, "deletion_score_targets", None)
                score_target_marker: Any = getattr(
                    self.repository, "mark_deletion_score_target", None
                )
                if (
                    callable(delete_ids)
                    and callable(score_target_reader)
                    and callable(score_target_marker)
                ):
                    while True:
                        score_targets = await cast(Any, score_target_reader)(
                            command.command_id, limit=page_size
                        )
                        if not score_targets:
                            break
                        score_ids = [_target_score_id(target) for target in score_targets]
                        await cast(Any, delete_ids)(score_ids)
                        for score_id in score_ids:
                            await lease.ensure()
                            marked = await _call_with_optional_lease(
                                cast(Any, score_target_marker),
                                (command.command_id, score_id),
                                lease.token,
                            )
                            if _accepts_lease(score_target_marker) and marked is False:
                                raise DeletionFailure("lease_lost")
                else:
                    delete_selector: Any = getattr(
                        self.score_store, "delete_scores_for_selector", None
                    )
                    if callable(delete_selector):
                        await cast(Any, delete_selector)(
                            command.selector, snapshot_at=command.requested_at
                        )
                    else:
                        # Compatibility seam for isolated unit fakes. Production
                        # adapters always use selector-scoped mutation deletion.
                        score_targets = await self.repository.deletion_targets(
                            command.command_id, limit=page_size
                        )
                        for target in score_targets:
                            await self.score_store.delete_scores_for_artifact(
                                _target_reference(target)
                            )
                await lease.ensure()
                await _fenced_phase(self.repository, command.command_id, "objects", lease.token)
            except Exception as exc:
                failure_code = _failure_code(exc)
                if failure_code != "lease_lost":
                    await _fenced_failure(
                        self.repository, command.command_id, failure_code, lease.token
                    )
                self._record_deletion_failure(command, "scores", failure_code)
                raise DeletionFailure(failure_code) from exc
            while True:
                await lease.ensure()
                targets = await self.repository.deletion_targets(
                    command.command_id, limit=page_size
                )
                if not targets:
                    completed = await _fenced_complete(
                        self.repository, command.command_id, deleted, lease.token
                    )
                    if completed is None:
                        raise DeletionFailure("lease_lost")
                    self._metric(
                        "observatory_evaluation_deletion_action_count",
                        labels={
                            "selector": command.selector.kind.value,
                            "phase": "complete",
                            "outcome": "succeeded",
                        },
                    )
                    await self._publish_live(
                        "evaluation_deletion", command.command_id, "deleted", "succeeded"
                    )
                    return completed
                for target in targets:
                    reference = _target_reference(target)
                    try:
                        await lease.ensure()
                        await self.object_store.delete(reference)
                        await lease.ensure()
                        await _fenced_metadata_delete(
                            self.repository, command.command_id, reference, lease.token
                        )
                        await _fenced_target(
                            self.repository, command.command_id, reference, lease.token
                        )
                        deleted += 1
                        self._metric(
                            "observatory_evaluation_deletion_deleted_count",
                            labels={"selector": command.selector.kind.value},
                        )
                    except Exception as exc:
                        failure_code = _failure_code(exc)
                        if failure_code != "lease_lost":
                            await _fenced_failure(
                                self.repository, command.command_id, failure_code, lease.token
                            )
                        self._record_deletion_failure(command, "objects", failure_code)
                        raise DeletionFailure(failure_code) from exc
        finally:
            await lease.close()

    def _record_deletion_failure(
        self, command: DeletionCommand, phase: str, failure_code: str
    ) -> None:
        self._metric(
            "observatory_evaluation_deletion_action_count",
            labels={
                "selector": command.selector.kind.value,
                "phase": phase,
                "outcome": "failed",
            },
        )
        self._metric(
            "observatory_evaluation_deletion_failure_count",
            labels={"phase": phase, "failure_code": failure_code},
        )

    def record_worker_failure(self, failure_code: str) -> None:
        if failure_code not in {
            "storage_failure",
            "storage_authentication",
            "storage_integrity",
            "storage_timeout",
            "database_failure",
            "target_invalid",
            "lease_lost",
        }:
            failure_code = "storage_failure"
        self._metric(
            "observatory_evaluation_deletion_failure_count",
            labels={"phase": "worker", "failure_code": failure_code},
        )
        _LOGGER.error("evaluation deletion worker failure code=%s", failure_code)

    async def _publish_live(
        self, resource_type: str, resource_id: str, change: str, state: str
    ) -> None:
        if self.event_publisher is None:
            return
        try:
            await self.event_publisher(resource_type, resource_id, change, state)
        except Exception:
            _LOGGER.debug("evaluation deletion live notification unavailable")

    def _metric(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        recorder = getattr(self.metrics, "record", None)
        if callable(recorder):
            recorder(name, value, labels)


class _DeletionLeaseGuard:
    """Renew a production deletion lease and fence stale workers."""

    def __init__(self, repository: Any, command_id: str, token: str) -> None:
        self.repository = repository
        self.command_id = command_id
        self.token = token
        self.lost = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if callable(getattr(self.repository, "renew_deletion_lease", None)):
            self._task = asyncio.create_task(self._renew_loop())

    async def ensure(self) -> None:
        if self.lost:
            raise DeletionFailure("lease_lost")
        renew = getattr(self.repository, "renew_deletion_lease", None)
        if callable(renew):
            result = await _call_with_optional_lease(renew, (self.command_id,), self.token)
            if result is False:
                self.lost = True
                raise DeletionFailure("lease_lost")

    async def _renew_loop(self) -> None:
        while not self.lost:
            await asyncio.sleep(20.0)
            try:
                renew = getattr(self.repository, "renew_deletion_lease", None)
                if not callable(renew):
                    return
                renewed = await _call_with_optional_lease(renew, (self.command_id,), self.token)
                if renewed is False:
                    self.lost = True
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.lost = True
                return

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def _accepts_lease(method: Any) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except TypeError, ValueError:
        return True
    return any(
        parameter.name == "lease_token" or parameter.kind is parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _call_with_optional_lease(method: Any, args: tuple[Any, ...], token: str) -> Any:
    if _accepts_lease(method):
        return await method(*args, lease_token=token)
    return await method(*args)


async def _fenced_phase(repository: Any, command_id: str, phase: str, token: str) -> None:
    method = getattr(repository, "mark_deletion_phase", None)
    if not callable(method):
        return
    result = await _call_with_optional_lease(method, (command_id, phase), token)
    if _accepts_lease(method) and result is False:
        raise DeletionFailure("lease_lost")


async def _fenced_target(repository: Any, command_id: str, reference: str, token: str) -> None:
    method = getattr(repository, "mark_deletion_target", None)
    if not callable(method):
        return
    result = await _call_with_optional_lease(method, (command_id, reference), token)
    if _accepts_lease(method) and result is False:
        raise DeletionFailure("lease_lost")


async def _fenced_metadata_delete(
    repository: Any, command_id: str, reference: str, token: str
) -> None:
    method = getattr(repository, "delete_artifact_metadata", None)
    if not callable(method):
        return
    if _accepts_lease(method):
        await cast(Any, method)(reference, command_id=command_id, lease_token=token)
    else:
        await cast(Any, method)(reference)


async def _fenced_complete(
    repository: Any, command_id: str, deleted: int, token: str
) -> int | None:
    method = getattr(repository, "complete_deletion_command", None)
    if not callable(method):
        return deleted
    result = await _call_with_optional_lease(method, (command_id, deleted), token)
    if _accepts_lease(method):
        return int(result) if isinstance(result, int) else None
    return deleted if result is None else int(result)


async def _fenced_failure(repository: Any, command_id: str, failure_code: str, token: str) -> None:
    method = getattr(repository, "fail_deletion_command", None)
    if not callable(method):
        return
    await _call_with_optional_lease(method, (command_id, failure_code), token)


class _DigestingStream:
    def __init__(self, source: AsyncIterable[Any], max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._digest = hashlib.sha256()
        self.byte_count = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            if not isinstance(chunk, bytes):
                raise ArtifactStorageError("malformed_stream")
            if not chunk:
                continue
            self.byte_count += len(chunk)
            if self.byte_count > self._max_bytes:
                raise ArtifactStorageError("artifact_limit_exceeded")
            self._digest.update(chunk)
            yield chunk

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _require_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ArtifactStorageError("identifier_invalid", f"{field_name} must be a UUID") from exc
    if str(parsed) != value.lower() or len(value) != 36:
        raise ArtifactStorageError("identifier_invalid", f"{field_name} must be a canonical UUID")


def _require_version(value: str, field_name: str) -> None:
    if not _VERSION_RE.fullmatch(value) or any(
        segment in _RESERVED_VERSION_SEGMENTS for segment in re.split(r"[._+-]+", value.casefold())
    ):
        raise ArtifactStorageError("score_invalid", f"{field_name} is not a valid version")


def _require_component_id(value: str) -> None:
    if not _COMPONENT_ID_RE.fullmatch(value):
        raise ArtifactStorageError("score_invalid", "component_id is not a valid component id")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _target_reference(target: Any) -> str:
    if isinstance(target, str):
        _require_uuid(target, "artifact_reference_id")
        return target.lower()
    value: Any = (
        cast(Mapping[str, Any], target).get("artifact_reference_id")
        if isinstance(target, Mapping)
        else getattr(target, "artifact_reference_id", None)
    )
    if not isinstance(value, str):
        raise DeletionFailure("target_invalid")
    _require_uuid(value, "artifact_reference_id")
    return value.lower()


def _target_score_id(target: Any) -> str:
    value: Any = (
        target
        if isinstance(target, str)
        else (
            cast(Mapping[str, Any], target).get("score_id")
            if isinstance(target, Mapping)
            else getattr(target, "score_id", None)
        )
    )
    if not isinstance(value, str):
        raise DeletionFailure("target_invalid")
    _require_uuid(value, "score_id")
    return value.lower()


def _is_zero_utc(value: datetime) -> bool:
    """Require an aware timestamp whose explicit offset is exactly UTC."""

    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _failure_code(error: Exception) -> str:
    return _bounded_failure_code(error, _DELETION_FAILURE_CODES, fallback="storage_failure")


def _bounded_failure_code(error: BaseException, allowed: frozenset[str], *, fallback: str) -> str:
    """Read only a finite adapter code; never expose exception diagnostics."""

    value = getattr(error, "failure_code", None)
    return value if isinstance(value, str) and value in allowed else fallback


def _monotonic() -> float:
    from time import monotonic

    return monotonic()


def _elapsed_ms(started: float) -> float:
    return max(0.0, (_monotonic() - started) * 1000.0)


async def _best_effort_delete(store: Any, reference: str) -> None:
    try:
        await store.delete(reference)
    except Exception:
        # The durable artifact row remains non-visible and recovery can retry
        # cleanup.  Never replace the bounded integrity/limit failure with a
        # provider exception or its response body.
        return


async def _put_stream_with_digest(
    store: Any,
    reference: str,
    stream: AsyncIterable[bytes],
    *,
    content_type: str,
    expected_sha256: str,
) -> int:
    """Pass the expected digest to production stores while retaining old test seams."""

    method = store.put_stream
    try:
        parameters = dict(inspect.signature(method).parameters)
    except TypeError, ValueError:
        parameters: dict[str, inspect.Parameter] = {}
    if "expected_sha256" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        return await method(
            reference,
            stream,
            content_type=content_type,
            expected_sha256=expected_sha256,
        )
    return await method(reference, stream, content_type=content_type)


__all__ = [
    "ALLOWED_CAPTURE_CLASSIFICATIONS",
    "ALLOWED_MEDIA_CLASSIFICATIONS",
    "ALLOWED_MEDIA_TYPES",
    "ArtifactRecord",
    "ArtifactStorageError",
    "ArtifactStorageService",
    "ArtifactUpload",
    "DeletionCommand",
    "DeletionFailure",
    "DeletionKind",
    "DeletionSelector",
    "EvaluationDeletionService",
    "EvaluationMetrics",
    "EvaluationScore",
]
