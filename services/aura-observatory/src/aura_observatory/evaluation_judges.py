"""Local-only judge execution and immutable judge metadata.

The judge boundary is deliberately transport neutral.  It accepts opaque,
authorized artifact references, invokes one admitted local provider, and
returns a finite advisory score.  Prompt, rubric, response and rationale bytes
are never part of a persisted record or diagnostic.
"""

# Provider/database rows are validated at this boundary and intentionally use
# closed JSON projections; pyright cannot infer their nested runtime shape.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from aura_observatory.evaluation_storage import EvaluationScore
from aura_observatory.ports import JudgeArtifactReadPort, LocalJudgePort

MAX_ARTIFACT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 8_192
MAX_OUTPUT_TOKENS = 2_048
MAX_TIMEOUT_SECONDS = 120.0
MAX_CONCURRENCY = 2
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}(?::[A-Za-z0-9][A-Za-z0-9._+-]{0,63})?$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def _utc_timestamp(value: datetime) -> str:
    """Render an aware UTC instant in evaluation-v1's canonical ``Z`` form."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise JudgeError("validation")
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")[:-6]
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered + "Z"


def _definition_budget(definition: LocalJudgeDefinition) -> dict[str, int | float]:
    """Return the one canonical execution budget admitted by a definition."""

    return {
        "timeoutMs": round(definition.timeout_seconds * 1000),
        "maxInputBytes": definition.max_input_bytes,
        "maxOutputBytes": definition.max_output_bytes,
        "maxOutputTokens": definition.max_output_tokens,
        "maxContextTokens": definition.context_tokens,
        "maxConcurrency": definition.max_concurrency,
        "temperature": definition.temperature,
    }


def _budget_matches(value: object, expected: Mapping[str, int | float]) -> bool:
    return isinstance(value, Mapping) and dict(value) == dict(expected)


def _requires_strict_execution(
    repository: object | None, definition: LocalJudgeDefinition
) -> bool:
    """Keep production repositories strict while allowing legacy test rows."""

    return (
        (
            repository is not None
            and not isinstance(repository, InMemoryJudgeDefinitionRepository)
        )
        or definition.evaluator_id is not None
        or definition.calibration_id is not None
    )


class JudgeError(RuntimeError):
    """Finite, content-free judge failure."""

    def __init__(self, code: str, *, dependency: str | None = None) -> None:
        self.code = code if code in FAILURE_CODES else "provider_failure"
        self.dependency = dependency
        super().__init__(self.code)


FAILURE_CODES = frozenset(
    {
        "validation",
        "artifact_unauthorized",
        "artifact_revoked",
        "artifact_integrity",
        "artifact_limit",
        "provider_unavailable",
        "provider_timeout",
        "provider_redirect",
        "model_not_found",
        "model_digest_drift",
        "provider_malformed",
        "provider_oversized",
        "provider_resource_limit",
        "duplicate",
        "storage_failure",
        "metadata_storage_failure",
        "definition_storage_failure",
        "execution_storage_failure",
        "artifact_storage_failure",
    }
)


class JudgeOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class JudgeAdvisoryState(StrEnum):
    ADVISORY = "advisory"
    CALIBRATED = "calibrated"
    INCONCLUSIVE = "inconclusive"


def _uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise JudgeError("validation")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise JudgeError("validation") from exc
    return value


def _id(value: str, field_name: str) -> str:
    del field_name
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise JudgeError("validation")
    return value


def _version(value: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise JudgeError("validation")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise JudgeError("validation")
    return value


def _finite(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgeError("validation")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise JudgeError("validation")
    return round(number, 6)


@dataclass(frozen=True, slots=True)
class JudgeArtifactReference:
    """Opaque, content-free reference to one authorized Garage object."""

    artifact_reference_id: str
    dataset_id: str
    authorization_record_id: str
    sha256: str
    byte_count: int
    media_type: str = "text/plain"
    capture_classification: str = "full_content"
    authorization_purpose: str = "owner_supplied_evaluation"
    revoked: bool = False
    artifact_version: str = "1.0.0"
    authorization_context: str = "controlled_evaluation"
    retention_mode: str = "manual_only"
    media_classification: str = "evaluation_input"
    intended_use: str = "input"

    def validate(self, *, require_execution_identity: bool = True) -> None:
        _uuid(self.artifact_reference_id, "artifact_reference_id")
        _uuid(self.dataset_id, "dataset_id")
        _uuid(self.authorization_record_id, "authorization_record_id")
        _version(self.artifact_version)
        _digest(self.sha256)
        if (
            not isinstance(self.byte_count, int)
            or isinstance(self.byte_count, bool)
            or not 0 <= self.byte_count <= MAX_ARTIFACT_BYTES
        ):
            raise JudgeError("validation")
        if self.media_type not in {
            "text/plain",
            "text/markdown",
            "application/json",
            "application/octet-stream",
        }:
            raise JudgeError("validation")
        if self.capture_classification not in {"redacted", "sampled_content", "full_content"}:
            raise JudgeError("artifact_unauthorized")
        if self.authorization_purpose not in {
            "synthetic_benchmark",
            "owner_supplied_evaluation",
            "controlled_replay",
            "human_review",
        }:
            raise JudgeError("artifact_unauthorized")
        if self.revoked:
            raise JudgeError("artifact_revoked")
        if self.authorization_context != "controlled_evaluation":
            raise JudgeError("artifact_unauthorized")
        if self.retention_mode != "manual_only":
            raise JudgeError("artifact_unauthorized")
        if self.media_classification not in {
            "evaluation_input",
            "evaluation_output",
            "evaluation_context",
            "evaluation_evidence",
            "evaluation_report",
        }:
            raise JudgeError("artifact_unauthorized")
        if self.intended_use not in {"prompt", "rubric", "input", "comment"}:
            raise JudgeError("artifact_unauthorized")
        # Authorization purpose and media classification are a role matrix,
        # never caller-provided hints.  Prompt/rubric/input bindings are
        # evaluation inputs; review comments are evidence and require the
        # separate human-review authorization scope.
        if self.intended_use in {"prompt", "rubric", "input"}:
            if self.media_classification != "evaluation_input":
                raise JudgeError("artifact_unauthorized")
            if self.authorization_purpose not in {
                "synthetic_benchmark",
                "owner_supplied_evaluation",
            } and not (
                self.intended_use == "input" and self.authorization_purpose == "controlled_replay"
            ):
                raise JudgeError("artifact_unauthorized")
        elif (
            self.media_classification != "evaluation_evidence"
            or self.authorization_purpose != "human_review"
        ):
            raise JudgeError("artifact_unauthorized")


@dataclass(frozen=True, slots=True)
class LocalJudgeDefinition:
    """Immutable exact local provider, artifact and budget provenance."""

    judge_id: str
    judge_version: str
    model_name: str
    model_version: str
    model_digest: str
    prompt_version: str
    rubric_version: str
    calibration_version: str
    prompt_artifact: JudgeArtifactReference
    rubric_artifact: JudgeArtifactReference
    label_ids: tuple[str, ...] = ("pass", "fail", "inconclusive")
    seed: int = 0
    temperature: float = 0.0
    timeout_seconds: float = 120.0
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    max_input_bytes: int = MAX_ARTIFACT_BYTES
    context_tokens: int = 4_096
    max_concurrency: int = 1
    metric: str = "evaluation.local_judge.score"
    calibration_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    provenance_component: str = "aura.observatory.local_judges"
    provenance_version: str = "1.0.0"
    provenance_digest: str = "0" * 64
    # Legacy rows may not carry these fields and are readable only for
    # migration/inspection.  New definitions and executable requests must
    # provide an evaluator identity distinct from the judge definition.
    evaluator_id: str | None = None
    evaluator_version: str | None = None

    def validate(self, *, require_execution_identity: bool = True) -> None:
        _uuid(self.judge_id, "judge_id")
        for value in (
            self.judge_version,
            self.model_version,
            self.prompt_version,
            self.rubric_version,
            self.calibration_version,
        ):
            _version(value)
        if not _MODEL.fullmatch(self.model_name):
            raise JudgeError("validation")
        _digest(self.model_digest)
        self.prompt_artifact.validate()
        self.rubric_artifact.validate()
        if (
            self.prompt_artifact.intended_use != "prompt"
            or self.rubric_artifact.intended_use != "rubric"
        ):
            raise JudgeError("artifact_unauthorized")
        if self.prompt_artifact.dataset_id != self.rubric_artifact.dataset_id:
            raise JudgeError("validation")
        if (
            not self.label_ids
            or len(self.label_ids) > 16
            or len(set(self.label_ids)) != len(self.label_ids)
        ):
            raise JudgeError("validation")
        for value in self.label_ids:
            _id(value, "label_id")
        if set(self.label_ids) != {"pass", "fail", "inconclusive"}:
            raise JudgeError("validation")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed <= 2_147_483_647
            or self.temperature != 0.0
        ):
            raise JudgeError("validation")
        if (
            not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise JudgeError("validation")
        if not 1 <= self.max_concurrency <= MAX_CONCURRENCY:
            raise JudgeError("validation")
        if (
            not 1 <= self.max_output_bytes <= MAX_OUTPUT_BYTES
            or not 1 <= self.max_output_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise JudgeError("validation")
        if (
            not 1 <= self.max_input_bytes <= MAX_ARTIFACT_BYTES
            or not 1 <= self.context_tokens <= 16_384
        ):
            raise JudgeError("validation")
        _id(self.metric, "metric")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != UTC.utcoffset(
            self.created_at
        ):
            raise JudgeError("validation")
        if not self.provenance_component.startswith("aura."):
            raise JudgeError("validation")
        _version(self.provenance_version)
        _digest(self.provenance_digest)
        if self.calibration_id is None:
            if require_execution_identity:
                raise JudgeError("validation")
        else:
            _uuid(self.calibration_id, "calibration_id")
        if self.evaluator_id is None or self.evaluator_version is None:
            if require_execution_identity:
                raise JudgeError("validation")
        else:
            _uuid(self.evaluator_id, "evaluator_id")
            _version(self.evaluator_version)

    def as_contract(self) -> dict[str, object]:
        """Return the closed evaluation-v1 local-judge projection."""

        self.validate()
        return {
            "schemaVersion": 1,
            "resourceType": "local_judge_definition",
            "judgeDefinitionId": self.judge_id,
            "judgeDefinitionVersion": self.judge_version,
            "evaluator": {
                "evaluatorId": self.evaluator_id,
                "evaluatorVersion": self.evaluator_version,
                "kind": "local_judge",
            },
            "model": {
                "provider": "local_ollama",
                "modelName": self.model_name,
                "modelVersion": self.model_version,
                "modelDigest": self.model_digest,
            },
            "prompt": {
                "artifactId": self.prompt_artifact.artifact_reference_id,
                "artifactVersion": self.prompt_artifact.artifact_version,
                "purpose": "judge_prompt",
                "digest": self.prompt_artifact.sha256,
            },
            "rubric": {
                "artifactId": self.rubric_artifact.artifact_reference_id,
                "artifactVersion": self.rubric_artifact.artifact_version,
                "purpose": "judge_rubric",
                "digest": self.rubric_artifact.sha256,
            },
            "calibration": {
                "calibrationId": self.calibration_id,
                "calibrationVersion": self.calibration_version,
            },
            "seed": self.seed,
            "budget": _definition_budget(self),
            "outputContract": "local_judge_score_v1",
            "defaultDisposition": "advisory_only",
            "retentionMode": "manual_only",
            "provenance": {
                "createdByComponent": self.provenance_component,
                "createdByVersion": self.provenance_version,
                "sourceDigest": self.provenance_digest,
            },
            "createdAt": _utc_timestamp(self.created_at),
        }

    def fingerprint(self, *, require_execution_identity: bool = True) -> str:
        self.validate(require_execution_identity=require_execution_identity)
        value = {
            "judgeId": self.judge_id,
            "judgeVersion": self.judge_version,
            "modelName": self.model_name,
            "modelVersion": self.model_version,
            "modelDigest": self.model_digest,
            "promptVersion": self.prompt_version,
            "rubricVersion": self.rubric_version,
            "calibrationVersion": self.calibration_version,
            "promptArtifact": {
                "id": self.prompt_artifact.artifact_reference_id,
                "version": self.prompt_artifact.artifact_version,
                "dataset": self.prompt_artifact.dataset_id,
                "authorization": self.prompt_artifact.authorization_record_id,
                "digest": self.prompt_artifact.sha256,
                "bytes": self.prompt_artifact.byte_count,
                "mediaType": self.prompt_artifact.media_type,
                "capture": self.prompt_artifact.capture_classification,
                "purpose": self.prompt_artifact.authorization_purpose,
                "context": self.prompt_artifact.authorization_context,
                "retention": self.prompt_artifact.retention_mode,
                "classification": self.prompt_artifact.media_classification,
                "use": self.prompt_artifact.intended_use,
            },
            "rubricArtifact": {
                "id": self.rubric_artifact.artifact_reference_id,
                "version": self.rubric_artifact.artifact_version,
                "dataset": self.rubric_artifact.dataset_id,
                "authorization": self.rubric_artifact.authorization_record_id,
                "digest": self.rubric_artifact.sha256,
                "bytes": self.rubric_artifact.byte_count,
                "mediaType": self.rubric_artifact.media_type,
                "capture": self.rubric_artifact.capture_classification,
                "purpose": self.rubric_artifact.authorization_purpose,
                "context": self.rubric_artifact.authorization_context,
                "retention": self.rubric_artifact.retention_mode,
                "classification": self.rubric_artifact.media_classification,
                "use": self.rubric_artifact.intended_use,
            },
            "labels": list(self.label_ids),
            "metric": self.metric,
            "seed": self.seed,
            "temperature": self.temperature,
            "timeoutSeconds": self.timeout_seconds,
            "maxOutputBytes": self.max_output_bytes,
            "maxOutputTokens": self.max_output_tokens,
            "maxInputBytes": self.max_input_bytes,
            "contextTokens": self.context_tokens,
            "maxConcurrency": self.max_concurrency,
        }
        if (
            require_execution_identity
            or self.evaluator_id is not None
            or self.evaluator_version is not None
        ):
            value["evaluatorId"] = self.evaluator_id
            value["evaluatorVersion"] = self.evaluator_version
        if require_execution_identity or self.calibration_id is not None:
            value["calibrationId"] = self.calibration_id
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    request_id: str
    idempotency_key: str
    case_id: str
    result_id: str
    job_id: str
    suite_id: str
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    input_artifacts: tuple[JudgeArtifactReference, ...]
    definition: LocalJudgeDefinition
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dataset_version: str | None = None
    suite_version: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> JudgeRequest:
        """Decode the bounded JSON projection stored in local work.

        The queue contains references and admitted definition metadata only;
        this decoder deliberately has no fields for prompt, response, or
        provider content.
        """

        def mapping(value: object, name: str) -> Mapping[str, object]:
            if not isinstance(value, Mapping):
                raise JudgeError("validation")
            return value

        def text(values: Mapping[str, object], name: str) -> str:
            value = values.get(name)
            if not isinstance(value, str):
                raise JudgeError("validation")
            return value

        def optional_text(values: Mapping[str, object], name: str) -> str | None:
            value = values.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise JudgeError("validation")
            return value

        def integer(values: Mapping[str, object], name: str, default: int | None = None) -> int:
            value = values.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise JudgeError("validation")
            return value

        def number(values: Mapping[str, object], name: str, default: float) -> float:
            value = values.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JudgeError("validation")
            return float(value)

        def artifact(value: object, expected_use: str) -> JudgeArtifactReference:
            values = mapping(value, "artifact")
            item = JudgeArtifactReference(
                artifact_reference_id=text(values, "artifact_reference_id"),
                dataset_id=text(values, "dataset_id"),
                authorization_record_id=text(values, "authorization_record_id"),
                sha256=text(values, "sha256"),
                byte_count=integer(values, "byte_count"),
                media_type=text(values, "media_type"),
                capture_classification=text(values, "capture_classification"),
                authorization_purpose=text(values, "authorization_purpose"),
                revoked=values.get("revoked", False) is True,
                artifact_version=text(values, "artifact_version"),
                authorization_context=text(values, "authorization_context"),
                retention_mode=text(values, "retention_mode"),
                media_classification=text(values, "media_classification"),
                intended_use=expected_use,
            )
            item.validate()
            if item.intended_use != expected_use:
                raise JudgeError("artifact_unauthorized")
            return item

        definition_values = mapping(payload.get("definition"), "definition")
        input_values = payload.get("input_artifacts")
        if not isinstance(input_values, Sequence) or isinstance(input_values, (str, bytes)):
            raise JudgeError("validation")
        labels = definition_values.get("label_ids")
        if labels is None:
            label_ids = ("pass", "fail", "inconclusive")
        elif isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
            label_ids = tuple(value for value in labels if isinstance(value, str))
        else:
            raise JudgeError("validation")
        definition = LocalJudgeDefinition(
            judge_id=text(definition_values, "judge_id"),
            judge_version=text(definition_values, "judge_version"),
            model_name=text(definition_values, "model_name"),
            model_version=text(definition_values, "model_version"),
            model_digest=text(definition_values, "model_digest"),
            prompt_version=text(definition_values, "prompt_version"),
            rubric_version=text(definition_values, "rubric_version"),
            calibration_version=text(definition_values, "calibration_version"),
            prompt_artifact=artifact(definition_values.get("prompt_artifact"), "prompt"),
            rubric_artifact=artifact(definition_values.get("rubric_artifact"), "rubric"),
            calibration_id=text(definition_values, "calibration_id"),
            evaluator_id=text(definition_values, "evaluator_id"),
            evaluator_version=text(definition_values, "evaluator_version"),
            label_ids=label_ids,
            seed=integer(definition_values, "seed", 0),
            timeout_seconds=number(definition_values, "timeout_seconds", MAX_TIMEOUT_SECONDS),
            max_output_bytes=integer(definition_values, "max_output_bytes", MAX_OUTPUT_BYTES),
            max_output_tokens=integer(definition_values, "max_output_tokens", MAX_OUTPUT_TOKENS),
            max_input_bytes=integer(definition_values, "max_input_bytes", MAX_ARTIFACT_BYTES),
            context_tokens=integer(definition_values, "context_tokens", 16_384),
            max_concurrency=integer(definition_values, "max_concurrency", 1),
            metric=text(definition_values, "metric"),
        )
        request = cls(
            request_id=text(payload, "request_id"),
            idempotency_key=text(payload, "idempotency_key"),
            case_id=text(payload, "case_id"),
            result_id=text(payload, "result_id"),
            job_id=text(payload, "job_id"),
            suite_id=text(payload, "suite_id"),
            evaluator_id=text(payload, "evaluator_id"),
            evaluator_version=text(payload, "evaluator_version"),
            component_id=text(payload, "component_id"),
            component_version=text(payload, "component_version"),
            input_artifacts=tuple(artifact(value, "input") for value in input_values),
            definition=definition,
            observed_at=datetime.fromisoformat(text(payload, "observed_at")),
            dataset_version=optional_text(payload, "dataset_version"),
            suite_version=optional_text(payload, "suite_version"),
        )
        request.validate()
        return request

    def validate(self, *, require_execution_identity: bool = True) -> None:
        for value in (
            self.request_id,
            self.idempotency_key,
            self.case_id,
            self.result_id,
            self.job_id,
            self.suite_id,
            self.evaluator_id,
        ):
            _uuid(value, "request identifier")
        _id(self.component_id, "component_id")
        _version(self.component_version)
        if self.dataset_version is not None:
            _version(self.dataset_version)
        if self.suite_version is not None:
            _version(self.suite_version)
        self.definition.validate(require_execution_identity=require_execution_identity)
        if require_execution_identity and (
            self.definition.evaluator_id != self.evaluator_id
            or self.definition.evaluator_version != self.evaluator_version
        ):
            raise JudgeError("validation")
        if not 1 <= len(self.input_artifacts) <= 16:
            raise JudgeError("validation")
        for artifact in self.input_artifacts:
            artifact.validate()
        if sum(item.byte_count for item in self.input_artifacts) > self.definition.max_input_bytes:
            raise JudgeError("artifact_limit")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != UTC.utcoffset(
            self.observed_at
        ):
            raise JudgeError("validation")

    def idempotency_fingerprint(self, *, require_execution_identity: bool = True) -> str:
        self.validate(require_execution_identity=require_execution_identity)
        value = [
            self.definition.fingerprint(require_execution_identity=require_execution_identity),
            self.case_id,
            self.result_id,
            self.request_id,
            self.job_id,
            self.suite_id,
            self.evaluator_id,
            self.evaluator_version,
            self.dataset_version,
            self.suite_version,
            self.component_id,
            self.component_version,
            *(item.sha256 for item in self.input_artifacts),
            *(
                {
                    "id": item.artifact_reference_id,
                    "version": item.artifact_version,
                    "dataset": item.dataset_id,
                    "authorization": item.authorization_record_id,
                    "digest": item.sha256,
                    "bytes": item.byte_count,
                    "mediaType": item.media_type,
                    "capture": item.capture_classification,
                    "purpose": item.authorization_purpose,
                    "context": item.authorization_context,
                    "retention": item.retention_mode,
                    "classification": item.media_classification,
                    "use": item.intended_use,
                }
                for item in self.input_artifacts
            ),
        ]
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class JudgeResponse:
    score: float
    outcome: JudgeOutcome
    label: str

    def validate(self, labels: Sequence[str]) -> None:
        _finite(self.score)
        if self.outcome not in set(JudgeOutcome) or self.label not in labels:
            raise JudgeError("provider_malformed")
        if self.outcome is JudgeOutcome.INCONCLUSIVE and self.label != "inconclusive":
            raise JudgeError("provider_malformed")


@dataclass(frozen=True, slots=True)
class JudgeResult:
    score_id: str
    request_id: str
    fingerprint: str
    score: float | None
    outcome: JudgeOutcome
    label: str
    advisory_state: JudgeAdvisoryState
    failure_code: str | None = None
    reproducibility: str = "local-ollama-v1"


ArtifactReader = JudgeArtifactReadPort


class AuthorizedArtifactReader:
    """Join the existing metadata repository and private object-store port."""

    def __init__(
        self, metadata_repository: Any, object_store: Any, metrics: Any | None = None
    ) -> None:
        self.metadata_repository = metadata_repository
        self.object_store = object_store
        self.metrics = metrics

    async def read(
        self,
        reference: JudgeArtifactReference,
        *,
        max_bytes: int,
        intended_use: str | None = None,
    ) -> bytes:
        reference.validate()
        if intended_use is not None and intended_use != reference.intended_use:
            raise JudgeError("artifact_unauthorized")
        if reference.byte_count > max_bytes:
            raise JudgeError("artifact_limit")
        metadata_started = monotonic()
        try:
            metadata = await self.metadata_repository.lookup_artifact_metadata(
                reference.artifact_reference_id
            )
        except Exception:
            self._record_boundary(
                "postgresql", "failure", "metadata_storage_failure", metadata_started
            )
            raise JudgeError("metadata_storage_failure") from None
        self._record_boundary("postgresql", "success", None, metadata_started)
        if metadata is None or getattr(metadata, "revoked", True):
            raise JudgeError(
                "artifact_revoked" if metadata is not None else "artifact_unauthorized"
            )
        checks = (
            getattr(metadata, "artifact_reference_id", None) == reference.artifact_reference_id,
            getattr(metadata, "dataset_id", None) == reference.dataset_id,
            getattr(metadata, "authorization_record_id", None) == reference.authorization_record_id,
            getattr(metadata, "artifact_version", None) == reference.artifact_version,
            getattr(metadata, "sha256", None) == reference.sha256,
            getattr(metadata, "byte_count", None) == reference.byte_count,
            getattr(metadata, "media_type", None) == reference.media_type,
            getattr(metadata, "media_classification", None) == reference.media_classification,
            getattr(metadata, "intended_use", None) == reference.intended_use,
            getattr(metadata, "authorization_purpose", None) == reference.authorization_purpose,
            getattr(metadata, "authorization_context", None) == reference.authorization_context,
            getattr(metadata, "retention_mode", None) == reference.retention_mode,
            getattr(metadata, "status", None) == "available",
            getattr(metadata, "capture_classification", None) == reference.capture_classification,
        )
        if not all(checks):
            raise JudgeError("artifact_integrity")
        garage_started = monotonic()
        try:
            data = await self.object_store.read(
                reference.artifact_reference_id,
                expected_sha256=reference.sha256,
                expected_bytes=reference.byte_count,
                max_bytes=max_bytes,
            )
            if len(data) > max_bytes:
                raise JudgeError("artifact_limit")
            if len(data) != reference.byte_count:
                raise JudgeError("artifact_integrity")
            self._record_boundary("garage", "success", None, garage_started)
            return data
        except Exception as exc:
            code = getattr(exc, "failure_code", None)
            if isinstance(exc, JudgeError):
                self._record_boundary("garage", "failure", exc.code, garage_started)
                raise
            self._record_boundary("garage", "failure", "artifact_storage_failure", garage_started)
            raise JudgeError(
                code
                if code in {"artifact_limit", "artifact_integrity"}
                else "artifact_storage_failure"
            ) from None

    def _record_boundary(
        self, dependency: str, outcome: str, error_code: str | None, started: float
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if callable(recorder):
            try:
                recorder(
                    component_id="aura.observatory.local_judges",
                    component_version="1.0.0",
                    deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")),
                    operation="read",
                    duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                    outcome=outcome,
                    dependency=dependency,
                    error_code=error_code,
                )
            except Exception:
                pass


class JudgeScoreStore(Protocol):
    async def insert_score(self, score: EvaluationScore) -> None: ...


class JudgeDefinitionRepository(Protocol):
    async def create(self, definition: LocalJudgeDefinition) -> LocalJudgeDefinition: ...

    async def get(self, judge_id: str, version: str) -> LocalJudgeDefinition | None: ...


class JudgeExecutionRepository(Protocol):
    """Durable idempotency and claim state for provider executions."""

    async def get(self, key: str, fingerprint: str) -> JudgeResult | None: ...

    async def claim(self, key: str, fingerprint: str) -> bool: ...

    async def complete(self, key: str, fingerprint: str, result: JudgeResult) -> None: ...

    async def fail(self, key: str, fingerprint: str, failure_code: str) -> None: ...


class InMemoryJudgeDefinitionRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], LocalJudgeDefinition] = {}
        self.audits: list[dict[str, str]] = []

    async def create(self, definition: LocalJudgeDefinition) -> LocalJudgeDefinition:
        definition.validate(require_execution_identity=False)
        key = (definition.judge_id, definition.judge_version)
        current = self.items.get(key)
        if current is not None:
            if current.fingerprint(require_execution_identity=False) != definition.fingerprint(
                require_execution_identity=False
            ):
                self.audits.append({"outcome": "conflict", "resource": "judge"})
                raise JudgeError("validation")
            self.audits.append({"outcome": "duplicate", "resource": "judge"})
            return current
        self.items[key] = definition
        self.audits.append({"outcome": "accepted", "resource": "judge"})
        return definition

    async def get(self, judge_id: str, version: str) -> LocalJudgeDefinition | None:
        return self.items.get((judge_id, version))


class InMemoryJudgeExecutionRepository:
    """Bounded reference implementation mirroring the durable claim protocol."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str, JudgeResult | None]] = {}

    async def get(self, key: str, fingerprint: str) -> JudgeResult | None:
        item = self.items.get(key)
        if item is None:
            return None
        if item[0] != fingerprint:
            raise JudgeError("validation")
        return item[2] if item[1] == "completed" else None

    async def claim(self, key: str, fingerprint: str) -> bool:
        item = self.items.get(key)
        if item is not None:
            if item[0] != fingerprint:
                raise JudgeError("validation")
            if item[1] == "failed":
                self.items[key] = (fingerprint, "claimed", None)
                return True
            return False
        self.items[key] = (fingerprint, "claimed", None)
        return True

    async def complete(self, key: str, fingerprint: str, result: JudgeResult) -> None:
        item = self.items.get(key)
        if item is None or item[0] != fingerprint:
            raise JudgeError("storage_failure")
        self.items[key] = (fingerprint, "completed", result)

    async def fail(self, key: str, fingerprint: str, failure_code: str) -> None:
        item = self.items.get(key)
        if item is not None and item[0] == fingerprint:
            self.items[key] = (fingerprint, "failed", None)


class PostgresJudgeExecutionRepository:
    """Transactional adapter for restart-safe execution claims."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    async def get(self, key: str, fingerprint: str) -> JudgeResult | None:
        _uuid(key, "idempotency_key")
        _digest(fingerprint)
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """SELECT request_fingerprint, status, result_json
                FROM observatory_local_judge_execution_claims
                WHERE idempotency_key=%s""",
                (key,),
            )
            row = await result.fetchone()
        return self._result_from_row(row, fingerprint)

    async def claim(self, key: str, fingerprint: str) -> bool:
        _uuid(key, "idempotency_key")
        _digest(fingerprint)
        async with self.connection_factory() as connection:
            inserted = await connection.execute(
                """INSERT INTO observatory_local_judge_execution_claims(
                    idempotency_key, request_fingerprint, status)
                VALUES (%s,%s,'claimed')
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING status""",
                (key, fingerprint),
            )
            # RETURNING is the ownership decision.  A follow-up SELECT alone
            # races with the winner and makes a fresh claimant look like a
            # duplicate.
            inserted_row = await inserted.fetchone()
            if inserted_row is not None:
                return True
            result = await connection.execute(
                """SELECT request_fingerprint, status FROM
                observatory_local_judge_execution_claims WHERE idempotency_key=%s""",
                (key,),
            )
            row = await result.fetchone()
            if row is None or str(row[0]) != fingerprint:
                raise JudgeError("validation")
            status = str(row[1])
            if status == "failed":
                reclaimed = await connection.execute(
                    """UPDATE observatory_local_judge_execution_claims
                    SET status='claimed', failure_code=NULL, claimed_at=CURRENT_TIMESTAMP,
                        completed_at=NULL, result_json=NULL
                    WHERE idempotency_key=%s AND request_fingerprint=%s
                      AND (status='failed' OR
                           (status='claimed' AND claimed_at <
                            CURRENT_TIMESTAMP - INTERVAL '5 minutes'))""",
                    (key, fingerprint),
                )
                return int(getattr(reclaimed, "rowcount", 0) or 0) == 1
            if status == "claimed":
                reclaimed = await connection.execute(
                    """UPDATE observatory_local_judge_execution_claims
                    SET claimed_at=CURRENT_TIMESTAMP
                    WHERE idempotency_key=%s AND request_fingerprint=%s
                      AND status='claimed'
                      AND claimed_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes'""",
                    (key, fingerprint),
                )
                return int(getattr(reclaimed, "rowcount", 0) or 0) == 1
            return False

    async def complete(self, key: str, fingerprint: str, result: JudgeResult) -> None:
        _uuid(key, "idempotency_key")
        _digest(fingerprint)
        payload = json.dumps(
            {
                "scoreId": result.score_id,
                "requestId": result.request_id,
                "fingerprint": result.fingerprint,
                "score": result.score,
                "outcome": result.outcome.value,
                "label": result.label,
                "advisoryState": result.advisory_state.value,
                "failureCode": result.failure_code,
                "reproducibility": result.reproducibility,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.connection_factory() as connection:
            result_row = await connection.execute(
                """UPDATE observatory_local_judge_execution_claims
                SET status='completed', result_json=%s::jsonb, completed_at=CURRENT_TIMESTAMP
                WHERE idempotency_key=%s AND request_fingerprint=%s AND status='claimed'""",
                (payload, key, fingerprint),
            )
            if int(getattr(result_row, "rowcount", 0) or 0) != 1:
                raise JudgeError("execution_storage_failure")

    async def fail(self, key: str, fingerprint: str, failure_code: str) -> None:
        _uuid(key, "idempotency_key")
        _digest(fingerprint)
        safe_code = failure_code if failure_code in FAILURE_CODES else "storage_failure"
        async with self.connection_factory() as connection:
            await connection.execute(
                """UPDATE observatory_local_judge_execution_claims
                SET status='failed', failure_code=%s, completed_at=CURRENT_TIMESTAMP
                WHERE idempotency_key=%s AND request_fingerprint=%s AND status='claimed'""",
                (safe_code, key, fingerprint),
            )

    @staticmethod
    def _result_from_row(row: Any, fingerprint: str) -> JudgeResult | None:
        if row is None:
            return None
        if str(row[0]) != fingerprint:
            raise JudgeError("validation")
        if str(row[1]) != "completed" or row[2] is None:
            return None
        value: object = row[2]
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise JudgeError("storage_failure")
        try:
            return JudgeResult(
                str(value["scoreId"]),
                str(value["requestId"]),
                str(value["fingerprint"]),
                None if value.get("score") is None else float(value["score"]),
                JudgeOutcome(str(value["outcome"])),
                str(value["label"]),
                JudgeAdvisoryState(str(value["advisoryState"])),
                None if value.get("failureCode") is None else str(value["failureCode"]),
                str(value.get("reproducibility", "local-ollama-v1")),
            )
        except KeyError, TypeError, ValueError:
            raise JudgeError("storage_failure") from None


def _artifact_payload(reference: JudgeArtifactReference) -> dict[str, object]:
    """Return the complete, content-free contract binding for a definition."""

    return {
        "artifactReferenceId": reference.artifact_reference_id,
        "artifactVersion": reference.artifact_version,
        "datasetId": reference.dataset_id,
        "authorizationRecordId": reference.authorization_record_id,
        "sha256": reference.sha256,
        "byteCount": reference.byte_count,
        "mediaType": reference.media_type,
        "captureClassification": reference.capture_classification,
        "authorizationPurpose": reference.authorization_purpose,
        "authorizationContext": reference.authorization_context,
        "retentionMode": reference.retention_mode,
        "mediaClassification": reference.media_classification,
        "intendedUse": reference.intended_use,
        "revoked": reference.revoked,
    }


def _artifact_from_payload(
    raw: object, legacy: Mapping[str, object], intended_use: str
) -> dict[str, object]:
    """Decode new lossless bindings while safely supporting old rows."""

    if isinstance(raw, Mapping):
        return {
            "artifact_reference_id": str(raw["artifactReferenceId"]),
            "dataset_id": str(raw["datasetId"]),
            "authorization_record_id": str(raw["authorizationRecordId"]),
            "sha256": str(raw["sha256"]),
            "byte_count": int(raw["byteCount"]),
            "media_type": str(raw.get("mediaType", "text/plain")),
            "capture_classification": str(raw.get("captureClassification", "full_content")),
            "authorization_purpose": str(
                raw.get("authorizationPurpose", "owner_supplied_evaluation")
            ),
            "revoked": bool(raw.get("revoked", False)),
            "artifact_version": str(raw.get("artifactVersion", "1.0.0")),
            "authorization_context": str(raw.get("authorizationContext", "controlled_evaluation")),
            "retention_mode": str(raw.get("retentionMode", "manual_only")),
            "media_classification": str(raw.get("mediaClassification", "evaluation_input")),
            "intended_use": str(raw.get("intendedUse", intended_use)),
        }
    prefix = "prompt" if intended_use == "prompt" else "rubric"
    return {
        "artifact_reference_id": str(legacy[f"{prefix}ArtifactId"]),
        "dataset_id": str(legacy["datasetId"]),
        "authorization_record_id": str(legacy["authorizationRecordId"]),
        "sha256": str(legacy[f"{prefix}Digest"]),
        "byte_count": int(legacy[f"{prefix}Bytes"]),
        "media_type": str(legacy.get(f"{prefix}MediaType", "text/plain")),
        "capture_classification": str(legacy.get("captureClassification", "full_content")),
        "authorization_purpose": str(
            legacy.get("authorizationPurpose", "owner_supplied_evaluation")
        ),
        "artifact_version": str(legacy.get(f"{prefix}ArtifactVersion", "1.0.0")),
        "authorization_context": str(legacy.get("authorizationContext", "controlled_evaluation")),
        "retention_mode": str(legacy.get("retentionMode", "manual_only")),
        "media_classification": str(legacy.get(f"{prefix}MediaClassification", "evaluation_input")),
        "intended_use": intended_use,
    }


class PostgresJudgeDefinitionRepository:
    """PostgreSQL adapter for immutable, content-free judge definitions."""

    def __init__(self, connection_factory: Any) -> None:
        self.connection_factory = connection_factory

    async def create(self, definition: LocalJudgeDefinition) -> LocalJudgeDefinition:
        definition.validate()
        payload = {
            "judgeId": definition.judge_id,
            "judgeVersion": definition.judge_version,
            "evaluatorId": definition.evaluator_id,
            "evaluatorVersion": definition.evaluator_version,
            "modelName": definition.model_name,
            "modelVersion": definition.model_version,
            "modelDigest": definition.model_digest,
            "promptVersion": definition.prompt_version,
            "rubricVersion": definition.rubric_version,
            "calibrationVersion": definition.calibration_version,
            # Keep each binding lossless.  The shared legacy fields below are
            # retained only for upgrades; new rows are reconstructed from
            # these independent contract-shaped references.
            "promptArtifact": _artifact_payload(definition.prompt_artifact),
            "rubricArtifact": _artifact_payload(definition.rubric_artifact),
            "labelIds": list(definition.label_ids),
            "seed": definition.seed,
            "temperature": definition.temperature,
            "timeoutSeconds": definition.timeout_seconds,
            "maxOutputBytes": definition.max_output_bytes,
            "maxOutputTokens": definition.max_output_tokens,
            "maxInputBytes": definition.max_input_bytes,
            "contextTokens": definition.context_tokens,
            "maxConcurrency": definition.max_concurrency,
            "metric": definition.metric,
            "calibrationId": definition.calibration_id,
            "createdAt": _utc_timestamp(definition.created_at),
            "provenanceComponent": definition.provenance_component,
            "provenanceVersion": definition.provenance_version,
            "provenanceDigest": definition.provenance_digest,
        }
        async with self.connection_factory() as connection:
            result = await connection.execute(
                """INSERT INTO observatory_local_judge_definitions(
                    judge_id, judge_version, evaluator_id, evaluator_version,
                    model_name, model_version, model_digest,
                    instruction_version, rubric_version, calibration_version, calibration_id,
                    instruction_artifact_id, rubric_artifact_id, label_ids, seed,
                    temperature, timeout_seconds, max_output_bytes, max_output_tokens,
                    max_input_bytes, context_tokens, max_concurrency, metric, fingerprint,
                    definition_json)
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (judge_id, judge_version) DO NOTHING""",
                (
                    definition.judge_id,
                    definition.judge_version,
                    definition.evaluator_id,
                    definition.evaluator_version,
                    definition.model_name,
                    definition.model_version,
                    definition.model_digest,
                    definition.prompt_version,
                    definition.rubric_version,
                    definition.calibration_version,
                    definition.calibration_id,
                    definition.prompt_artifact.artifact_reference_id,
                    definition.rubric_artifact.artifact_reference_id,
                    list(definition.label_ids),
                    definition.seed,
                    definition.temperature,
                    definition.timeout_seconds,
                    definition.max_output_bytes,
                    definition.max_output_tokens,
                    definition.max_input_bytes,
                    definition.context_tokens,
                    definition.max_concurrency,
                    definition.metric,
                    definition.fingerprint(),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
            del result
            existing = await connection.execute(
                "SELECT fingerprint FROM observatory_local_judge_definitions "
                "WHERE judge_id=%s AND judge_version=%s",
                (definition.judge_id, definition.judge_version),
            )
            row = await existing.fetchone()
            if row is None or str(row[0]) != definition.fingerprint():
                raise JudgeError("validation")
        return definition

    async def get(self, judge_id: str, version: str) -> LocalJudgeDefinition | None:
        _uuid(judge_id, "judge_id")
        _version(version)
        async with self.connection_factory() as connection:
            result = await connection.execute(
                "SELECT calibration_id, definition_json, fingerprint "
                "FROM observatory_local_judge_definitions "
                "WHERE judge_id=%s AND judge_version=%s",
                (judge_id, version),
            )
            row = await result.fetchone()
        if row is None:
            return None
        column_calibration_id: object | None = None
        value = row[0] if not isinstance(row, Mapping) else row.get("definition_json")
        stored_fingerprint: object | None = None
        if isinstance(row, Mapping):
            column_calibration_id = row.get("calibration_id")
            stored_fingerprint = row.get("fingerprint")
        elif isinstance(row, (tuple, list)) and len(row) > 1:
            if len(row) > 2:
                column_calibration_id, value, stored_fingerprint = row[0], row[1], row[2]
            else:
                stored_fingerprint = row[1]
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            raise JudgeError("storage_failure")
        # The repository only reconstructs opaque references; bytes remain in
        # Garage and are checked again by AuthorizedArtifactReader at use time.
        prompt = JudgeArtifactReference(
            **_artifact_from_payload(value.get("promptArtifact"), value, "prompt"),
        )
        rubric = JudgeArtifactReference(
            **_artifact_from_payload(value.get("rubricArtifact"), value, "rubric"),
        )
        legacy_identity = value.get("evaluatorId") is None or value.get("evaluatorVersion") is None
        definition = LocalJudgeDefinition(
            judge_id,
            version,
            str(value["modelName"]),
            str(value["modelVersion"]),
            str(value["modelDigest"]),
            str(value["promptVersion"]),
            str(value["rubricVersion"]),
            str(value["calibrationVersion"]),
            prompt,
            rubric,
            tuple(str(item) for item in value.get("labelIds", ["pass", "fail", "inconclusive"])),
            int(value.get("seed", 0)),
            float(value.get("temperature", 0.0)),
            float(value.get("timeoutSeconds", MAX_TIMEOUT_SECONDS)),
            int(value.get("maxOutputBytes", MAX_OUTPUT_BYTES)),
            int(value.get("maxOutputTokens", MAX_OUTPUT_TOKENS)),
            int(value.get("maxInputBytes", MAX_ARTIFACT_BYTES)),
            int(value.get("contextTokens", 4_096)),
            int(value.get("maxConcurrency", 1)),
            str(value.get("metric", "evaluation.local_judge.score")),
            calibration_id=(
                None
                if value.get("calibrationId", column_calibration_id) is None
                else str(value.get("calibrationId", column_calibration_id))
            ),
            created_at=(
                datetime.fromisoformat(str(value["createdAt"]))
                if value.get("createdAt") is not None
                else datetime.now(UTC)
            ),
            provenance_component=str(
                value.get("provenanceComponent", "aura.observatory.local_judges")
            ),
            provenance_version=str(value.get("provenanceVersion", "1.0.0")),
            provenance_digest=str(value.get("provenanceDigest", "0" * 64)),
            evaluator_id=(None if value.get("evaluatorId") is None else str(value["evaluatorId"])),
            evaluator_version=(
                None if value.get("evaluatorVersion") is None else str(value["evaluatorVersion"])
            ),
        )
        if stored_fingerprint is not None and str(stored_fingerprint) != definition.fingerprint(
            require_execution_identity=not legacy_identity
        ):
            raise JudgeError("storage_failure")
        return definition


class InMemoryJudgeScoreStore:
    def __init__(self) -> None:
        self.scores: dict[str, EvaluationScore] = {}

    async def insert_score(self, score: EvaluationScore) -> None:
        score.validate()
        existing = self.scores.get(score.score_id)
        if existing is not None and existing != score:
            raise JudgeError("storage_failure")
        self.scores[score.score_id] = score


class LocalJudgeService:
    """Admit, execute and persist one advisory local-judge score."""

    def __init__(
        self,
        provider: LocalJudgePort,
        artifacts: ArtifactReader,
        *,
        definition_repository: JudgeDefinitionRepository | None = None,
        execution_repository: JudgeExecutionRepository | None = None,
        score_store: JudgeScoreStore | None = None,
        metrics: Any | None = None,
        max_concurrency: int = 1,
        qualification_resolver: Callable[..., Awaitable[object | None]] | None = None,
        calibration_result_resolver: Callable[[str], Awaitable[object | None]] | None = None,
    ) -> None:
        self.provider = provider
        self.artifacts = artifacts
        self.definition_repository = definition_repository
        self.execution_repository = execution_repository
        self.score_store = score_store
        self.metrics = metrics
        self.qualification_resolver = qualification_resolver
        self.calibration_result_resolver = calibration_result_resolver
        self._results: dict[str, tuple[str, JudgeResult]] = {}
        # The durable execution repository is the production authority; this
        # narrow lock prevents duplicate provider work for concurrent delivery
        # in one worker and bounds the local result memoization.
        self._idempotency_lock = asyncio.Lock()
        self._semaphore = __import__("asyncio").Semaphore(
            max(1, min(MAX_CONCURRENCY, max_concurrency))
        )

    async def evaluate(self, request: JudgeRequest) -> JudgeResult:
        async with self._idempotency_lock:
            return await self._evaluate_locked(request)

    async def _evaluate_locked(self, request: JudgeRequest) -> JudgeResult:
        started = monotonic()
        execution_claimed = False
        execution_key: str | None = None
        execution_fingerprint: str | None = None
        try:
            # The in-memory adapter is a legacy-only compatibility seam for
            # pre-identity fixtures. Every production repository remains
            # strict even when it reads a legacy row.
            strict_execution = _requires_strict_execution(
                self.definition_repository, request.definition
            )
            request.validate(require_execution_identity=strict_execution)
            if self.definition_repository is not None and (
                request.dataset_version is None or request.suite_version is None
            ):
                raise JudgeError("validation")
            if self.definition_repository is not None:
                phase_started = monotonic()
                try:
                    admitted = await self.definition_repository.get(
                        request.definition.judge_id, request.definition.judge_version
                    )
                except JudgeError:
                    self._phase_span(
                        "definition_get", phase_started, "failure", "postgresql", "validation"
                    )
                    raise
                except Exception:
                    self._phase_span(
                        "definition_get",
                        phase_started,
                        "failure",
                        "postgresql",
                        "metadata_storage_failure",
                    )
                    raise JudgeError("definition_storage_failure") from None
                self._phase_span("definition_get", phase_started, "success", "postgresql", None)
                if admitted is None or admitted.fingerprint(
                    require_execution_identity=strict_execution
                ) != request.definition.fingerprint(
                    require_execution_identity=strict_execution
                ):
                    raise JudgeError("validation")
                request = JudgeRequest(
                    request.request_id,
                    request.idempotency_key,
                    request.case_id,
                    request.result_id,
                    request.job_id,
                    request.suite_id,
                    request.evaluator_id,
                    request.evaluator_version,
                    request.component_id,
                    request.component_version,
                    request.input_artifacts,
                    admitted,
                    request.observed_at,
                    request.dataset_version,
                    request.suite_version,
                )
            key = request.idempotency_key
            fingerprint = request.idempotency_fingerprint(
                require_execution_identity=strict_execution
            )
            execution_key = key
            execution_fingerprint = fingerprint
            existing = self._results.get(key)
            if existing is not None:
                if existing[0] != fingerprint:
                    raise JudgeError("validation")
                self._record("duplicate", started)
                return existing[1]
            if self.execution_repository is not None:
                phase_started = monotonic()
                try:
                    durable = await self.execution_repository.get(key, fingerprint)
                except JudgeError:
                    self._phase_span(
                        "execution_get", phase_started, "failure", "postgresql", "validation"
                    )
                    raise
                except Exception:
                    self._phase_span(
                        "execution_get",
                        phase_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                    raise JudgeError("execution_storage_failure") from None
                self._phase_span("execution_get", phase_started, "success", "postgresql", None)
                if durable is not None:
                    self._results[key] = (fingerprint, durable)
                    self._record("duplicate", started)
                    return durable
                phase_started = monotonic()
                try:
                    claimed = await self.execution_repository.claim(key, fingerprint)
                except JudgeError:
                    self._phase_span(
                        "execution_claim", phase_started, "failure", "postgresql", "validation"
                    )
                    raise
                except Exception:
                    self._phase_span(
                        "execution_claim",
                        phase_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                    raise JudgeError("execution_storage_failure") from None
                self._phase_span("execution_claim", phase_started, "success", "postgresql", None)
                if not claimed:
                    # A concurrent worker owns the claim.  A completed race is
                    # returned when observable; an in-flight claim is a finite
                    # duplicate outcome and can be retried by orchestration.
                    try:
                        durable = await self.execution_repository.get(key, fingerprint)
                    except JudgeError:
                        raise
                    except Exception:
                        raise JudgeError("execution_storage_failure") from None
                    if durable is not None:
                        self._results[key] = (fingerprint, durable)
                        self._record("duplicate", started)
                        return durable
                    raise JudgeError("duplicate")
                execution_claimed = True
            async with self._semaphore:
                phase_started = monotonic()
                try:
                    await self.provider.verify_model(
                        request.definition.model_name,
                        request.definition.model_digest,
                        model_version=request.definition.model_version,
                    )
                except Exception as error:
                    self._phase_span(
                        "verify_model",
                        phase_started,
                        "failure",
                        "ollama",
                        getattr(error, "code", "provider_failure"),
                    )
                    raise
                self._phase_span("verify_model", phase_started, "success", "ollama", None)
                phase_started = monotonic()
                try:
                    prompt = await self.artifacts.read(
                        request.definition.prompt_artifact,
                        max_bytes=request.definition.max_input_bytes,
                        intended_use="prompt",
                    )
                    rubric = await self.artifacts.read(
                        request.definition.rubric_artifact,
                        max_bytes=request.definition.max_input_bytes,
                        intended_use="rubric",
                    )
                    evidence_list: list[bytes] = []
                    for item in request.input_artifacts:
                        evidence_list.append(
                            await self.artifacts.read(
                                item,
                                max_bytes=request.definition.max_input_bytes,
                                intended_use="input",
                            )
                        )
                except Exception as error:
                    self._phase_span(
                        "read_artifacts",
                        phase_started,
                        "failure",
                        None,
                        getattr(error, "code", "artifact_storage_failure"),
                    )
                    raise
                # The aggregate phase covers the Garage reads (including the
                # preceding authorization metadata joins). Keep its
                # dependency aligned with the child Garage boundary on both
                # outcomes; the child PostgreSQL spans retain exact metadata
                # lookup timing and attribution.
                self._phase_span("read_artifacts", phase_started, "success", "garage", None)
                evidence = tuple(evidence_list)
                phase_started = monotonic()
                try:
                    response = await self.provider.generate(
                        model_name=request.definition.model_name,
                        prompt=prompt,
                        rubric=rubric,
                        evidence=evidence,
                        timeout_seconds=request.definition.timeout_seconds,
                        max_output_bytes=request.definition.max_output_bytes,
                        max_output_tokens=request.definition.max_output_tokens,
                        context_tokens=request.definition.context_tokens,
                        seed=request.definition.seed,
                    )
                except Exception as error:
                    self._phase_span(
                        "generate",
                        phase_started,
                        "failure",
                        "ollama",
                        getattr(error, "code", "provider_failure"),
                    )
                    raise
                self._phase_span("generate", phase_started, "success", "ollama", None)
            response.validate(request.definition.label_ids)
            qualification: object | None = None
            if self.qualification_resolver is not None:
                phase_started = monotonic()
                try:
                    qualification = await self.qualification_resolver(
                        suite_id=request.suite_id,
                        suite_version=request.suite_version,
                        dataset_id=request.definition.prompt_artifact.dataset_id,
                        dataset_version=request.dataset_version,
                        evaluator_id=request.evaluator_id,
                        evaluator_version=request.evaluator_version,
                        judge_id=request.definition.judge_id,
                        judge_version=request.definition.judge_version,
                        model_name=request.definition.model_name,
                        model_version=request.definition.model_version,
                        model_digest=request.definition.model_digest,
                        prompt_version=request.definition.prompt_version,
                        prompt_artifact_id=request.definition.prompt_artifact.artifact_reference_id,
                        prompt_artifact_version=request.definition.prompt_artifact.artifact_version,
                        prompt_artifact_digest=request.definition.prompt_artifact.sha256,
                        rubric_version=request.definition.rubric_version,
                        rubric_artifact_id=request.definition.rubric_artifact.artifact_reference_id,
                        rubric_artifact_version=request.definition.rubric_artifact.artifact_version,
                        rubric_artifact_digest=request.definition.rubric_artifact.sha256,
                        calibration_version=request.definition.calibration_version,
                    )
                except Exception:
                    self._phase_span(
                        "definition_get",
                        phase_started,
                        "failure",
                        "postgresql",
                        "definition_storage_failure",
                    )
                    raise JudgeError("definition_storage_failure") from None
                self._phase_span("definition_get", phase_started, "success", "postgresql", None)
            qualification_id = getattr(qualification, "qualification_id", None)
            qualification_version = getattr(qualification, "qualification_version", None)
            calibration_result: object | None = None
            calibration_result_id = getattr(qualification, "calibration_result_id", None)
            if calibration_result_id is not None and self.calibration_result_resolver is not None:
                phase_started = monotonic()
                try:
                    calibration_result = await self.calibration_result_resolver(
                        str(calibration_result_id)
                    )
                except Exception:
                    self._phase_span(
                        "definition_get",
                        phase_started,
                        "failure",
                        "postgresql",
                        "definition_storage_failure",
                    )
                    calibration_result = None
                else:
                    self._phase_span("definition_get", phase_started, "success", "postgresql", None)
            exact_scope = (
                request.dataset_version is not None
                and request.suite_version is not None
                and qualification is not None
                and bool(getattr(qualification, "active", False))
                and getattr(qualification, "state", None) == "qualified"
                and getattr(qualification, "score_disposition", None) == "calibrated_enabled"
                and getattr(qualification, "action_mode", None) == "manual_owner_action"
                and getattr(qualification, "calibration_version", None)
                == request.definition.calibration_version
                and (
                    not strict_execution
                    or getattr(qualification, "calibration_id", None)
                    == request.definition.calibration_id
                )
                and getattr(qualification, "suite_id", None) == request.suite_id
                and getattr(qualification, "dataset_id", None)
                == request.definition.prompt_artifact.dataset_id
                and getattr(qualification, "evaluator_id", None) == request.evaluator_id
                and getattr(qualification, "judge_id", None) == request.definition.judge_id
                and getattr(qualification, "judge_version", None)
                == request.definition.judge_version
                and getattr(qualification, "prompt_version", None)
                == request.definition.prompt_version
                and getattr(qualification, "rubric_version", None)
                == request.definition.rubric_version
                and getattr(qualification, "suite_version", None) == request.suite_version
                and getattr(qualification, "dataset_version", None) == request.dataset_version
                and getattr(qualification, "evaluator_version", None) == request.evaluator_version
                and getattr(qualification, "model_name", None) == request.definition.model_name
                and getattr(qualification, "model_version", None)
                == request.definition.model_version
                and getattr(qualification, "model_digest", None) == request.definition.model_digest
                and getattr(qualification, "prompt_artifact_reference_id", None)
                == request.definition.prompt_artifact.artifact_reference_id
                and getattr(qualification, "prompt_artifact_version", None)
                == request.definition.prompt_artifact.artifact_version
                and getattr(qualification, "prompt_artifact_digest", None)
                == request.definition.prompt_artifact.sha256
                and getattr(qualification, "rubric_artifact_reference_id", None)
                == request.definition.rubric_artifact.artifact_reference_id
                and getattr(qualification, "rubric_artifact_version", None)
                == request.definition.rubric_artifact.artifact_version
                and getattr(qualification, "rubric_artifact_digest", None)
                == request.definition.rubric_artifact.sha256
                and calibration_result_id is not None
                and getattr(qualification, "calibration_eligibility", None) == "eligible"
                and getattr(qualification, "qualified_at", None) is not None
                and (
                    not strict_execution
                    or (
                        getattr(qualification, "seed", None) == request.definition.seed
                        and _budget_matches(
                            getattr(qualification, "budget", None),
                            _definition_budget(request.definition),
                        )
                    )
                )
            )
            # A durable qualification is enabled only when its referenced
            # CalibrationResult is present and carries the same immutable
            # execution/artifact/model scope.  The current evaluation result
            # identity is deliberately never used as calibration provenance.
            if self.calibration_result_resolver is not None:
                exact_scope = exact_scope and calibration_result is not None
                for field_name, expected in (
                    ("calibration_result_id", calibration_result_id),
                    ("calibration_version", request.definition.calibration_version),
                    ("dataset_id", request.definition.prompt_artifact.dataset_id),
                    ("dataset_version", request.dataset_version),
                    ("suite_id", request.suite_id),
                    ("suite_version", request.suite_version),
                    ("evaluator_id", request.evaluator_id),
                    ("evaluator_version", request.evaluator_version),
                    ("judge_id", request.definition.judge_id),
                    ("judge_version", request.definition.judge_version),
                    ("prompt_version", request.definition.prompt_version),
                    ("rubric_version", request.definition.rubric_version),
                    ("model_name", request.definition.model_name),
                    ("model_version", request.definition.model_version),
                    ("model_digest", request.definition.model_digest),
                    (
                        "prompt_artifact_reference_id",
                        request.definition.prompt_artifact.artifact_reference_id,
                    ),
                    (
                        "prompt_artifact_version",
                        request.definition.prompt_artifact.artifact_version,
                    ),
                    ("prompt_artifact_digest", request.definition.prompt_artifact.sha256),
                    (
                        "rubric_artifact_reference_id",
                        request.definition.rubric_artifact.artifact_reference_id,
                    ),
                    (
                        "rubric_artifact_version",
                        request.definition.rubric_artifact.artifact_version,
                    ),
                    ("rubric_artifact_digest", request.definition.rubric_artifact.sha256),
                    ("seed", request.definition.seed),
                ):
                    exact_scope = (
                        exact_scope and getattr(calibration_result, field_name, None) == expected
                    )
                if strict_execution:
                    exact_scope = exact_scope and _budget_matches(
                        getattr(calibration_result, "budget", None),
                        _definition_budget(request.definition),
                    )
                    exact_scope = exact_scope and getattr(
                        calibration_result, "seed", None
                    ) == request.definition.seed
                exact_scope = exact_scope and getattr(
                    calibration_result, "calibration_result_id", None
                ) == calibration_result_id
                exact_scope = exact_scope and bool(getattr(calibration_result, "eligible", False))
                exact_scope = (
                    exact_scope
                    and getattr(calibration_result, "score_disposition", None) == "advisory_only"
                )
                exact_scope = exact_scope and getattr(
                    calibration_result, "calibration_id", None
                ) == getattr(qualification, "calibration_id", None)
            else:
                # Compatibility callers without a durable result port cannot
                # assert execution identity as calibration provenance.
                exact_scope = exact_scope and calibration_result_id not in {
                    request.request_id,
                    request.idempotency_key,
                    request.case_id,
                    request.result_id,
                    request.job_id,
                }
            # A qualification is never inferred from a partial transport
            # request.  The production resolver and every direct caller must
            # provide the exact dataset/suite versions; otherwise the score
            # remains advisory, even when a durable qualification exists.
            # A qualification is never inferred from a partial transport
            # request. Exact dataset/suite versions and all immutable
            # definition/artifact bindings are required before a score can
            # become calibrated-enabled, including for in-memory callers.
            calibrated = exact_scope
            state = (
                JudgeAdvisoryState.INCONCLUSIVE
                if response.outcome is JudgeOutcome.INCONCLUSIVE
                else JudgeAdvisoryState.CALIBRATED
                if calibrated
                else JudgeAdvisoryState.ADVISORY
            )
            score_id = str(UUID(request.request_id))
            result = JudgeResult(
                score_id,
                request.request_id,
                fingerprint,
                response.score,
                response.outcome,
                response.label,
                state,
            )
            if self.score_store is not None:
                storage_started = monotonic()
                try:
                    await self.score_store.insert_score(
                        EvaluationScore(
                            score_id=score_id,
                            job_id=request.job_id,
                            result_id=request.result_id,
                            dataset_id=request.definition.prompt_artifact.dataset_id,
                            suite_id=request.suite_id,
                            evaluator_id=request.evaluator_id,
                            evaluator_version=request.evaluator_version,
                            evaluator_kind="local_judge",
                            component_id=request.component_id,
                            component_version=request.component_version,
                            capture_classification="full_content",
                            evidence_status="observed",
                            value=response.score,
                            outcome=response.outcome.value,
                            observed_at=request.observed_at,
                            artifact_reference_id=request.input_artifacts[0].artifact_reference_id,
                            case_id=request.case_id,
                            metric=request.definition.metric,
                            reproducibility=result.reproducibility,
                            judge_definition_id=request.definition.judge_id,
                            judge_definition_version=request.definition.judge_version,
                            model_name=request.definition.model_name,
                            model_version=request.definition.model_version,
                            model_digest=request.definition.model_digest,
                            prompt_version=request.definition.prompt_version,
                            rubric_version=request.definition.rubric_version,
                            calibration_version=request.definition.calibration_version,
                            seed=request.definition.seed,
                            budget_timeout_ms=round(request.definition.timeout_seconds * 1000),
                            budget_max_input_bytes=request.definition.max_input_bytes,
                            budget_max_output_bytes=request.definition.max_output_bytes,
                            budget_max_output_tokens=request.definition.max_output_tokens,
                            budget_max_context_tokens=request.definition.context_tokens,
                            budget_max_concurrency=request.definition.max_concurrency,
                            score_disposition=(
                                "calibrated_enabled" if calibrated else "advisory_only"
                            ),
                            qualification_id=qualification_id,
                            qualification_version=qualification_version,
                            uncertainty=None,
                            disagreement="none",
                            prompt_artifact_id=request.definition.prompt_artifact.artifact_reference_id,
                            prompt_artifact_version=request.definition.prompt_artifact.artifact_version,
                            prompt_artifact_digest=request.definition.prompt_artifact.sha256,
                            rubric_artifact_id=request.definition.rubric_artifact.artifact_reference_id,
                            rubric_artifact_version=request.definition.rubric_artifact.artifact_version,
                            rubric_artifact_digest=request.definition.rubric_artifact.sha256,
                            calibration_id=request.definition.calibration_id,
                        )
                    )
                except Exception:
                    self._phase_span(
                        "write_score",
                        storage_started,
                        "failure",
                        "clickhouse",
                        "storage_failure",
                    )
                    raise JudgeError("storage_failure", dependency="clickhouse") from None
                self._phase_span("write_score", storage_started, "success", "clickhouse", None)
            if self.execution_repository is not None:
                phase_started = monotonic()
                try:
                    await self.execution_repository.complete(key, fingerprint, result)
                except JudgeError:
                    self._phase_span(
                        "execution_complete", phase_started, "failure", "postgresql", "validation"
                    )
                    raise
                except Exception:
                    self._phase_span(
                        "execution_complete",
                        phase_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                    raise JudgeError("execution_storage_failure") from None
                self._phase_span("execution_complete", phase_started, "success", "postgresql", None)
            if len(self._results) >= 1024:
                self._results.pop(next(iter(self._results)))
            self._results[key] = (fingerprint, result)
            self._record(response.outcome.value, started)
            return result
        except JudgeError as error:
            if execution_claimed and self.execution_repository is not None:
                failure_started = monotonic()
                try:
                    await self.execution_repository.fail(
                        execution_key or "", execution_fingerprint or "", error.code
                    )
                except Exception:
                    self._phase_span(
                        "execution_fail",
                        failure_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                else:
                    self._phase_span(
                        "execution_fail", failure_started, "success", "postgresql", None
                    )
            self._record(error.code, started, dependency=_dependency_for_failure(error.code))
            raise
        except TimeoutError:
            if execution_claimed and self.execution_repository is not None:
                failure_started = monotonic()
                try:
                    await self.execution_repository.fail(
                        execution_key or "", execution_fingerprint or "", "provider_timeout"
                    )
                except Exception:
                    self._phase_span(
                        "execution_fail",
                        failure_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                else:
                    self._phase_span(
                        "execution_fail", failure_started, "success", "postgresql", None
                    )
            self._record("provider_timeout", started, dependency="ollama")
            raise JudgeError("provider_timeout") from None
        except Exception:
            if execution_claimed and self.execution_repository is not None:
                failure_started = monotonic()
                try:
                    await self.execution_repository.fail(
                        execution_key or "", execution_fingerprint or "", "storage_failure"
                    )
                except Exception:
                    self._phase_span(
                        "execution_fail",
                        failure_started,
                        "failure",
                        "postgresql",
                        "execution_storage_failure",
                    )
                else:
                    self._phase_span(
                        "execution_fail", failure_started, "success", "postgresql", None
                    )
            self._record("storage_failure", started, dependency="clickhouse")
            raise JudgeError("storage_failure") from None

    def _phase_span(
        self,
        operation: str,
        started: float,
        outcome: str,
        dependency: str | None,
        error_code: str | None,
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if not callable(recorder):
            return
        allowed_errors = {
            "validation",
            "provider_failure",
            "provider_timeout",
            "provider_unavailable",
            "provider_redirect",
            "provider_resource_limit",
            "provider_malformed",
            "provider_oversized",
            "model_not_found",
            "model_digest_drift",
            "artifact_unauthorized",
            "artifact_revoked",
            "artifact_integrity",
            "artifact_limit",
            "artifact_storage_failure",
            "metadata_storage_failure",
            "definition_storage_failure",
            "execution_storage_failure",
            "storage_failure",
        }
        safe_error = error_code if error_code in allowed_errors else "storage_failure"
        try:
            recorder(
                component_id="aura.observatory.local_judges",
                component_version="1.0.0",
                deployment_id=getattr(self.metrics, "deployment_id", "observatory"),
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                outcome=(
                    outcome
                    if outcome in {"success", "failure", "duplicate", "timeout", "unavailable"}
                    else "failure"
                ),
                dependency=dependency,
                error_code=None if outcome == "success" else safe_error,
            )
        except ValueError, TypeError:
            raise JudgeError("validation") from None

    def _record(self, outcome: str, started: float, *, dependency: str | None = None) -> None:
        if self.metrics is None:
            return
        safe_error = (
            outcome
            if outcome
            in {
                "pass",
                "fail",
                "inconclusive",
                "duplicate",
                "validation",
                "artifact_unauthorized",
                "artifact_revoked",
                "artifact_integrity",
                "artifact_limit",
                "provider_unavailable",
                "provider_timeout",
                "provider_redirect",
                "model_not_found",
                "model_digest_drift",
                "provider_malformed",
                "provider_oversized",
                "provider_resource_limit",
                "storage_failure",
                "metadata_storage_failure",
                "definition_storage_failure",
                "execution_storage_failure",
                "artifact_storage_failure",
            }
            else "failure"
        )
        metric_outcome = (
            safe_error
            if safe_error in {"pass", "fail", "inconclusive", "duplicate"}
            else "unavailable"
            if safe_error in {"provider_timeout", "provider_unavailable"}
            else "failure"
        )
        if safe_error == "failure":
            safe_error = "provider_failure"
        try:
            self.metrics.record(
                "observatory_local_judge_request_count", labels={"outcome": metric_outcome}
            )
            self.metrics.record(
                "observatory_local_judge_outcome_count", labels={"outcome": metric_outcome}
            )
            self.metrics.record(
                "observatory_local_judge_duration_ms", max(0.0, (monotonic() - started) * 1000.0)
            )
            span_outcome = (
                "success"
                if metric_outcome in {"pass", "fail", "inconclusive", "duplicate"}
                else "failure"
            )
            record_span = getattr(self.metrics, "record_component_span", None)
            if callable(record_span):
                record_span(
                    component_id="aura.observatory.local_judges",
                    component_version="1.0.0",
                    deployment_id=getattr(self.metrics, "deployment_id", "observatory"),
                    operation="judge",
                    duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                    outcome=span_outcome,
                    dependency=dependency,
                    error_code=safe_error if span_outcome == "failure" else None,
                )
            if safe_error in {
                "provider_failure",
                "provider_timeout",
                "provider_unavailable",
                "provider_redirect",
                "model_not_found",
                "model_digest_drift",
                "provider_malformed",
                "provider_oversized",
                "provider_resource_limit",
            }:
                self.metrics.record(
                    "observatory_local_judge_provider_failure_count",
                    labels={"failure_code": safe_error},
                )
        except ValueError, TypeError:
            raise JudgeError("validation") from None


def _dependency_for_failure(code: str) -> str | None:
    if code.startswith("artifact_"):
        return "garage"
    if code in {
        "metadata_storage_failure",
        "definition_storage_failure",
        "execution_storage_failure",
    }:
        return "postgresql"
    if code.startswith("provider_") or code.startswith("model_"):
        return "ollama"
    if code in {"storage_failure", "duplicate"}:
        return "clickhouse" if code == "storage_failure" else None
    return None


__all__ = [
    "ArtifactReader",
    "AuthorizedArtifactReader",
    "FAILURE_CODES",
    "InMemoryJudgeDefinitionRepository",
    "InMemoryJudgeExecutionRepository",
    "InMemoryJudgeScoreStore",
    "PostgresJudgeExecutionRepository",
    "PostgresJudgeDefinitionRepository",
    "JudgeAdvisoryState",
    "JudgeArtifactReference",
    "JudgeDefinitionRepository",
    "JudgeExecutionRepository",
    "JudgeError",
    "JudgeOutcome",
    "JudgeRequest",
    "JudgeResponse",
    "JudgeResult",
    "JudgeScoreStore",
    "LocalJudgeDefinition",
    "LocalJudgePort",
    "LocalJudgeService",
    "MAX_ARTIFACT_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_TOKENS",
    "MAX_TIMEOUT_SECONDS",
]
