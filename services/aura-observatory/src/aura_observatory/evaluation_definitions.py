"""Closed, immutable Observatory evaluation definitions.

This module is the domain boundary for AURA-0093.  Definitions are inert
metadata: they identify an authorized evaluation and its typed expectations,
but they never contain prompt/response bytes, executable configuration,
provider locations, credentials, or owner identity.  The PostgreSQL adapter
persists only the projections returned by :meth:`to_storage`.
"""

# The JSON contract is intentionally decoded at a single closed boundary. The
# helper validates every key/value before projecting it into typed records.
# Pyright cannot infer the nested JSON union without duplicating that decoder.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, cast
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    from aura_observatory.metrics import ObservatoryMetrics
    from aura_observatory.ports import (
        EvaluationArtifactMetadata,
        EvaluationArtifactRepository,
        EvaluationDefinitionRepository,
    )


class DefinitionValidationError(ValueError):
    """A definition is malformed, unsafe, or references unsupported data."""


class DefinitionStorageError(RuntimeError):
    """A provider failure crossing the definition repository boundary."""


class DefinitionCommandPendingError(RuntimeError):
    """Another request currently owns a durable definition command claim."""


class DefinitionConflictError(DefinitionValidationError):
    """An immutable identifier/version was reused with a different payload."""


class DefinitionNotFoundError(DefinitionValidationError):
    """A required versioned definition or component does not exist."""


@dataclass(frozen=True, slots=True)
class DefinitionCommandClaim:
    """Durable command claim state returned by the definition repository."""

    replay: Mapping[str, object] | None
    claimed: bool
    claim_token: str | None = None
    claim_phase: str = "definition_claim"


class SourceKind(StrEnum):
    SYNTHETIC = "synthetic"
    OWNER_SUPPLIED = "owner_supplied"
    CAPTURED = "captured"


class CaptureClassification(StrEnum):
    METADATA_ONLY = "metadata_only"
    HASHED = "hashed"
    REDACTED = "redacted"
    SAMPLED_CONTENT = "sampled_content"
    FULL_CONTENT = "full_content"


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_SEGMENT = re.compile(
    r"(?:^|[._:+-])(owner|identity|credential|bearer|token|secret|password|private|content|endpoint|url|uri|filename|path)(?:$|[._:+-])",
    re.IGNORECASE,
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)

_EVALUATOR_FAMILIES = (
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
BUILTIN_EVALUATOR_IDS: dict[str, str] = {
    str(uuid5(NAMESPACE_URL, f"https://aura.local/evaluator/{family}")): family
    for family in _EVALUATOR_FAMILIES
}
# AURA-0089 shipped one reviewed UUID before the closed UUID registry was
# introduced.  It is the only compatibility alias retained here.
LEGACY_EXACT_EVALUATOR_ID = "00000000-0000-0000-0000-000000000004"

_EXPECTATION_KEYS = frozenset(
    {
        "exact",
        "schema",
        "retrieval",
        "context",
        "tool",
        "policy",
        "groundedness",
        "response",
        "resource",
    }
)
_EXPECTATION_FIELDS: dict[str, frozenset[str]] = {
    "exact": frozenset({"fields"}),
    "schema": frozenset({"schemaDigest", "dialect", "additionalFieldsPolicy", "requiredFieldIds"}),
    "retrieval": frozenset({"rankedEvidence", "cutoffs", "irrelevantEvidenceIds"}),
    "context": frozenset(
        {
            "requiredEvidenceIds",
            "prohibitedEvidenceIds",
            "maxSelectedTokens",
            "truncation",
            "conflictHandling",
            "injectionHandling",
        }
    ),
    "tool": frozenset({"expectedCalls", "prohibitedToolIds", "maxRedundantCalls"}),
    "policy": frozenset(
        {
            "decision",
            "confirmation",
            "sensitiveDataHandling",
            "consequentialAction",
            "policyRevision",
        }
    ),
    "groundedness": frozenset({"claims", "unsupportedClaimPolicy"}),
    "response": frozenset(
        {
            "expectedDigest",
            "normalization",
            "requiredInstructionIds",
            "requiredStyleIds",
            "refusal",
            "properties",
            "maxBytes",
            "maxTokens",
        }
    ),
    "resource": frozenset(
        {
            "maxLatencyMs",
            "maxQueueMs",
            "maxInputBytes",
            "maxOutputBytes",
            "maxTokens",
            "maxMemoryBytes",
            "maxProviderCalls",
            "maxConcurrency",
            "maxOperations",
            "minThroughputPerSecond",
        }
    ),
}


def _uuid(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise DefinitionValidationError(f"{name} must be a lowercase UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise DefinitionValidationError(f"{name} must be a UUID") from exc
    if str(parsed) != value:
        raise DefinitionValidationError(f"{name} must use canonical UUID spelling")
    return value


def _version(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not _VERSION.fullmatch(value)
        or _PRIVATE_SEGMENT.search(value)
    ):
        raise DefinitionValidationError(f"{name} must be a bounded non-sensitive version")
    return value


def _identifier(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or _PRIVATE_SEGMENT.search(value)
    ):
        raise DefinitionValidationError(f"{name} must be a bounded opaque identifier")
    return value


def _component(value: Any, name: str = "componentId") -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise DefinitionValidationError(f"{name} must be an Aura component id")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise DefinitionValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise DefinitionValidationError(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DefinitionValidationError(f"{name} must be a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise DefinitionValidationError(f"{name} must be UTC")
    return parsed.astimezone(UTC)


def _bounded_number(
    value: Any, name: str, *, lower: float = -1.0e15, upper: float = 1.0e15
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DefinitionValidationError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < lower or number > upper:
        raise DefinitionValidationError(f"{name} must be finite and bounded")
    return number


def _bounded_int(value: Any, name: str, *, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lower or value > upper:
        raise DefinitionValidationError(f"{name} must be a bounded integer")
    return value


def _closed_map(value: Any, name: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DefinitionValidationError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise DefinitionValidationError(f"{name} contains unsupported fields")
    result = dict(value)
    for key, item in result.items():
        if not isinstance(key, str) or len(key) > 64:
            raise DefinitionValidationError(f"{name} has an invalid field")
        if isinstance(item, float) and not math.isfinite(item):
            raise DefinitionValidationError(f"{name} contains a non-finite number")
    return result


def _list_of_ids(
    value: Any, name: str, *, maximum: int = 256, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DefinitionValidationError(f"{name} must be a list")
    if not allow_empty and not value:
        raise DefinitionValidationError(f"{name} must be a non-empty list")
    if len(value) > maximum:
        raise DefinitionValidationError(f"{name} exceeds its bound")
    items = tuple(_identifier(item, name) for item in value)
    if len(set(items)) != len(items):
        raise DefinitionValidationError(f"{name} must be unique")
    return items


def _scopes(value: Any, name: str = "scopes") -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not 1 <= len(value) <= 4
    ):
        raise DefinitionValidationError(f"{name} must contain one to four scopes")
    items = tuple(str(item) for item in value)
    if len(set(items)) != len(items) or any(
        item not in {"public_project", "private", "sensitive", "environment"} for item in items
    ):
        raise DefinitionValidationError(f"{name} contains an unsupported scope")
    return items


def _retention(value: Any, name: str = "retentionMode") -> str:
    if value != "manual_only":
        raise DefinitionValidationError(f"{name} must be manual_only")
    return "manual_only"


@dataclass(frozen=True, slots=True)
class CaptureAuthorization:
    capture_classification: CaptureClassification
    authorization_purpose: str
    authorization_context: str = "controlled_evaluation"
    retention_mode: str = "manual_only"
    authorization_record_id: str | None = None
    authorized_at: datetime | None = None

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> CaptureAuthorization:
        allowed = {
            "captureClassification",
            "authorizationPurpose",
            "authorizationContext",
            "retentionMode",
            "authorizationRecordId",
            "authorizedAt",
        }
        data = _closed_map(value, "captureAuthorization", frozenset(allowed))
        try:
            classification = CaptureClassification(str(data["captureClassification"]))
        except (KeyError, ValueError) as exc:
            raise DefinitionValidationError("unsupported capture classification") from exc
        purpose = data.get("authorizationPurpose")
        if purpose not in {
            "synthetic_benchmark",
            "owner_supplied_evaluation",
            "controlled_replay",
            "human_review",
        }:
            raise DefinitionValidationError("unsupported authorization purpose")
        if (
            data.get("authorizationContext") != "controlled_evaluation"
            or data.get("retentionMode") != "manual_only"
        ):
            raise DefinitionValidationError(
                "evaluation authorization must be controlled and manual-only"
            )
        record = data.get("authorizationRecordId")
        authorized_at = data.get("authorizedAt")
        if classification is CaptureClassification.METADATA_ONLY:
            if record is not None or authorized_at is not None:
                raise DefinitionValidationError(
                    "metadata-only authorization cannot carry content authorization"
                )
        else:
            if record is None or authorized_at is None:
                raise DefinitionValidationError(
                    "content authorization requires an opaque authorization record"
                )
            record = _uuid(record, "authorizationRecordId")
            authorized_at = _timestamp(authorized_at, "authorizedAt")
        return cls(
            classification,
            str(purpose),
            "controlled_evaluation",
            "manual_only",
            record,
            authorized_at,
        )

    def to_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "captureClassification": self.capture_classification.value,
            "authorizationPurpose": self.authorization_purpose,
            "authorizationContext": self.authorization_context,
            "retentionMode": self.retention_mode,
        }
        if self.authorization_record_id is not None:
            data["authorizationRecordId"] = self.authorization_record_id
        if self.authorized_at is not None:
            data["authorizedAt"] = _iso(self.authorized_at)
        return data


def _validate_source_capture(
    source: SourceKind | str, authorization: CaptureAuthorization, name: str
) -> None:
    """Keep source provenance, capture class, and purpose mutually bound."""

    source_value = source.value if isinstance(source, SourceKind) else source
    expected_purpose = {
        SourceKind.SYNTHETIC.value: "synthetic_benchmark",
        SourceKind.OWNER_SUPPLIED.value: "owner_supplied_evaluation",
        SourceKind.CAPTURED.value: "controlled_replay",
    }.get(source_value)
    if expected_purpose is None or authorization.authorization_purpose != expected_purpose:
        raise DefinitionValidationError(f"{name} source and authorization purpose differ")
    if source_value == SourceKind.SYNTHETIC.value:
        if authorization.capture_classification is not CaptureClassification.METADATA_ONLY:
            raise DefinitionValidationError(f"{name} synthetic data must be metadata-only")
    elif authorization.capture_classification is CaptureClassification.METADATA_ONLY:
        raise DefinitionValidationError(f"{name} content source requires authorized capture")


@dataclass(frozen=True, slots=True)
class ResourceProvenance:
    created_by_component: str
    created_by_version: str
    source_digest: str

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ResourceProvenance:
        data = _closed_map(
            value,
            "provenance",
            frozenset({"createdByComponent", "createdByVersion", "sourceDigest"}),
        )
        return cls(
            _component(data.get("createdByComponent"), "createdByComponent"),
            _version(data.get("createdByVersion"), "createdByVersion"),
            _digest(data.get("sourceDigest"), "sourceDigest"),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "createdByComponent": self.created_by_component,
            "createdByVersion": self.created_by_version,
            "sourceDigest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class ResourceReference:
    resource_id: str
    version: str

    @classmethod
    def from_payload(cls, value: Mapping[str, Any], name: str = "reference") -> ResourceReference:
        data = _closed_map(value, name, frozenset({"id", "version"}))
        return cls(
            _uuid(data.get("id"), f"{name}.id"), _version(data.get("version"), f"{name}.version")
        )

    def to_payload(self) -> dict[str, str]:
        return {"id": self.resource_id, "version": self.version}


@dataclass(frozen=True, slots=True)
class ComponentBinding:
    component_id: str
    component_version: str
    code_revision: str
    image_digest: str
    configuration_revision: str

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ComponentBinding:
        data = _closed_map(
            value,
            "component",
            frozenset(
                {
                    "componentId",
                    "componentVersion",
                    "codeRevision",
                    "imageDigest",
                    "configurationRevision",
                }
            ),
        )
        return cls(
            _component(data.get("componentId")),
            _version(data.get("componentVersion"), "componentVersion"),
            _identifier(data.get("codeRevision"), "codeRevision"),
            _digest(data.get("imageDigest"), "imageDigest"),
            _identifier(data.get("configurationRevision"), "configurationRevision"),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "codeRevision": self.code_revision,
            "imageDigest": self.image_digest,
            "configurationRevision": self.configuration_revision,
        }


@dataclass(frozen=True, slots=True)
class VariantBinding:
    """Authoritative, content-free provenance admitted by a suite."""

    variant_id: str
    components: tuple[ComponentBinding, ...]
    model: Mapping[str, str]
    configuration_revision: str
    prompt_revision: str
    policy_revision: str
    deployment_revision: str

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> VariantBinding:
        data = _closed_map(
            value,
            "variantBinding",
            frozenset(
                {
                    "variantId",
                    "components",
                    "model",
                    "configurationRevision",
                    "promptRevision",
                    "policyRevision",
                    "deploymentRevision",
                }
            ),
        )
        components_data = data.get("components")
        if (
            not isinstance(components_data, Sequence)
            or isinstance(components_data, (str, bytes))
            or not 1 <= len(components_data) <= 32
        ):
            raise DefinitionValidationError("variant components are invalid")
        model_data = _closed_map(
            cast(Mapping[str, Any], data.get("model")),
            "variant model",
            frozenset({"modelId", "modelVersion", "modelDigest"}),
        )
        model_id = _identifier(model_data.get("modelId"), "modelId")
        model_version = _version(model_data.get("modelVersion"), "modelVersion")
        model_digest = _digest(model_data.get("modelDigest"), "modelDigest")
        return cls(
            _identifier(data.get("variantId"), "variantId"),
            tuple(
                ComponentBinding.from_payload(cast(Mapping[str, Any], item))
                for item in components_data
            ),
            {"modelId": model_id, "modelVersion": model_version, "modelDigest": model_digest},
            _identifier(data.get("configurationRevision"), "configurationRevision"),
            _identifier(data.get("promptRevision"), "promptRevision"),
            _identifier(data.get("policyRevision"), "policyRevision"),
            _identifier(data.get("deploymentRevision"), "deploymentRevision"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "variantId": self.variant_id,
            "components": [item.to_payload() for item in self.components],
            "model": dict(self.model),
            "configurationRevision": self.configuration_revision,
            "promptRevision": self.prompt_revision,
            "policyRevision": self.policy_revision,
            "deploymentRevision": self.deployment_revision,
        }


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    timeout_ms: int
    max_concurrency: int
    max_attempts: int
    max_cases: int
    max_input_bytes: int
    max_output_bytes: int
    max_tokens: int

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ExecutionLimits:
        data = _closed_map(
            value,
            "limits",
            frozenset(
                {
                    "timeoutMs",
                    "maxConcurrency",
                    "maxAttempts",
                    "maxCases",
                    "maxInputBytes",
                    "maxOutputBytes",
                    "maxTokens",
                }
            ),
        )
        return cls(
            _bounded_int(data.get("timeoutMs"), "timeoutMs", lower=100, upper=86_400_000),
            _bounded_int(data.get("maxConcurrency"), "maxConcurrency", lower=1, upper=64),
            _bounded_int(data.get("maxAttempts"), "maxAttempts", lower=1, upper=10),
            _bounded_int(data.get("maxCases"), "maxCases", lower=1, upper=100_000),
            _bounded_int(data.get("maxInputBytes"), "maxInputBytes", lower=0, upper=1_073_741_824),
            _bounded_int(
                data.get("maxOutputBytes"), "maxOutputBytes", lower=0, upper=1_073_741_824
            ),
            _bounded_int(data.get("maxTokens"), "maxTokens", lower=0, upper=100_000_000),
        )

    def to_payload(self) -> dict[str, int]:
        return {
            "timeoutMs": self.timeout_ms,
            "maxConcurrency": self.max_concurrency,
            "maxAttempts": self.max_attempts,
            "maxCases": self.max_cases,
            "maxInputBytes": self.max_input_bytes,
            "maxOutputBytes": self.max_output_bytes,
            "maxTokens": self.max_tokens,
        }


@dataclass(frozen=True, slots=True)
class EvaluatorReference:
    evaluator_id: str
    evaluator_version: str
    kind: str = "deterministic"

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> EvaluatorReference:
        data = _closed_map(
            value, "evaluator", frozenset({"evaluatorId", "evaluatorVersion", "kind"})
        )
        kind = data.get("kind")
        if kind != "deterministic":
            raise DefinitionValidationError("AURA-0093 suites require deterministic evaluators")
        return cls(
            _uuid(data.get("evaluatorId"), "evaluatorId"),
            _version(data.get("evaluatorVersion"), "evaluatorVersion"),
            str(kind),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class ManualThreshold:
    evaluator: EvaluatorReference
    metric: str
    aggregation: str
    operator: str
    value: float
    unit: str
    minimum_sample_size: int
    missing_evidence_policy: str

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ManualThreshold:
        data = _closed_map(
            value,
            "manualThreshold",
            frozenset(
                {
                    "evaluator",
                    "metric",
                    "aggregation",
                    "operator",
                    "value",
                    "unit",
                    "minimumSampleSize",
                    "missingEvidencePolicy",
                }
            ),
        )
        aggregation = data.get("aggregation")
        operator = data.get("operator")
        unit = data.get("unit")
        policy = data.get("missingEvidencePolicy")
        if (
            aggregation not in {"each", "minimum", "maximum", "mean", "p50", "p95", "p99", "total"}
            or operator not in {"greater_than_or_equal", "less_than_or_equal", "equal"}
            or unit
            not in {"ratio", "count", "milliseconds", "bytes", "tokens", "operations", "per_second"}
            or policy not in {"fail", "inconclusive", "not_applicable"}
        ):
            raise DefinitionValidationError("invalid manual threshold semantics")
        return cls(
            EvaluatorReference.from_payload(cast(Mapping[str, Any], data.get("evaluator"))),
            _identifier(data.get("metric"), "metric"),
            str(aggregation),
            str(operator),
            _bounded_number(data.get("value"), "threshold.value"),
            str(unit),
            _bounded_int(
                data.get("minimumSampleSize"), "minimumSampleSize", lower=1, upper=100_000
            ),
            str(policy),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator.to_payload(),
            "metric": self.metric,
            "aggregation": self.aggregation,
            "operator": self.operator,
            "value": self.value,
            "unit": self.unit,
            "minimumSampleSize": self.minimum_sample_size,
            "missingEvidencePolicy": self.missing_evidence_policy,
        }


def _validate_expectations(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _closed_map(value, "expectations", _EXPECTATION_KEYS)
    if not data:
        raise DefinitionValidationError("expectations must contain at least one family")
    # The nested contract is closed and intentionally retains only IDs,
    # digests, enums, finite numbers, and bounded booleans.  It never accepts
    # content-bearing strings or arbitrary extension objects.
    for family, payload in data.items():
        if not isinstance(payload, Mapping):
            raise DefinitionValidationError(f"expectations.{family} must be an object")
        nested = _closed_map(payload, f"expectations.{family}", _EXPECTATION_FIELDS[family])
        if family == "exact":
            fields = nested.get("fields")
            if (
                not isinstance(fields, Sequence)
                or isinstance(fields, (str, bytes))
                or not 1 <= len(fields) <= 64
            ):
                raise DefinitionValidationError("exact.fields is bounded")
            seen: set[str] = set()
            for field_value in fields:
                field_data = _closed_map(
                    field_value,
                    "exact field",
                    frozenset({"fieldId", "expectedDigest", "normalization"}),
                )
                field_id = _identifier(field_data.get("fieldId"), "fieldId")
                if field_id in seen or field_data.get("normalization") not in {
                    "none",
                    "canonical_json",
                    "unicode_nfc",
                    "trimmed_text",
                }:
                    raise DefinitionValidationError("exact fields are invalid or duplicated")
                seen.add(field_id)
                _digest(field_data.get("expectedDigest"), "expectedDigest")
        elif family == "schema":
            _digest(nested.get("schemaDigest"), "schemaDigest")
            if nested.get("dialect") not in {
                "json_schema_2020_12",
                "openapi_3_1",
                "typed_projection_v1",
            } or nested.get("additionalFieldsPolicy") not in {"allow", "reject"}:
                raise DefinitionValidationError("schema expectation is invalid")
            if "requiredFieldIds" in nested:
                _list_of_ids(nested["requiredFieldIds"], "requiredFieldIds")
        elif family == "retrieval":
            ranked = nested.get("rankedEvidence")
            if (
                not isinstance(ranked, Sequence)
                or isinstance(ranked, (str, bytes))
                or not 1 <= len(ranked) <= 256
            ):
                raise DefinitionValidationError("retrieval evidence is bounded")
            for item in ranked:
                item_data = _closed_map(
                    item,
                    "ranked evidence",
                    frozenset({"evidenceId", "relevanceGrade", "citationRequired", "freshness"}),
                )
                _identifier(item_data.get("evidenceId"), "evidenceId")
                _bounded_int(item_data.get("relevanceGrade"), "relevanceGrade", lower=0, upper=3)
                if not isinstance(item_data.get("citationRequired"), bool) or item_data.get(
                    "freshness"
                ) not in {"current", "stale", "not_applicable"}:
                    raise DefinitionValidationError("ranked evidence is invalid")
            cuts = nested.get("cutoffs")
            if (
                not isinstance(cuts, Sequence)
                or isinstance(cuts, (str, bytes))
                or not 1 <= len(cuts) <= 16
                or len(set(cuts)) != len(cuts)
            ):
                raise DefinitionValidationError("retrieval cutoffs are invalid")
            for cut in cuts:
                _bounded_int(cut, "cutoff", lower=1, upper=1000)
            if "irrelevantEvidenceIds" in nested:
                _list_of_ids(nested["irrelevantEvidenceIds"], "irrelevantEvidenceIds")
        elif family == "context":
            _list_of_ids(nested.get("requiredEvidenceIds"), "requiredEvidenceIds")
            if "prohibitedEvidenceIds" in nested:
                _list_of_ids(nested["prohibitedEvidenceIds"], "prohibitedEvidenceIds")
            _bounded_int(
                nested.get("maxSelectedTokens"), "maxSelectedTokens", lower=0, upper=100_000_000
            )
            if nested.get("truncation") not in {
                "required",
                "allowed",
                "prohibited",
                "not_applicable",
            } or nested.get("conflictHandling") not in {
                "preserve_authority",
                "report_conflict",
                "reject_conflict",
                "not_applicable",
            } or (
                "injectionHandling" in nested
                and nested["injectionHandling"]
                not in {"ignore_untrusted_instructions", "reject_context", "not_applicable"}
            ):
                raise DefinitionValidationError("context expectation is invalid")
        elif family == "tool":
            calls = nested.get("expectedCalls")
            if (
                not isinstance(calls, Sequence)
                or isinstance(calls, (str, bytes))
                or len(calls) > 64
            ):
                raise DefinitionValidationError("tool calls are bounded")
            for item in calls:
                call = _closed_map(
                    item,
                    "tool call",
                    frozenset(
                        {
                            "callId",
                            "toolId",
                            "argumentSchemaDigest",
                            "expectedArgumentDigest",
                            "argumentMatch",
                            "outcome",
                            "sideEffect",
                            "idempotency",
                        }
                    ),
                )
                _identifier(call.get("callId"), "callId")
                _identifier(call.get("toolId"), "toolId")
                _digest(call.get("argumentSchemaDigest"), "argumentSchemaDigest")
                if (
                    call.get("argumentMatch") not in {"schema_only", "exact_digest"}
                    or call.get("outcome")
                    not in {
                        "completed",
                        "denied",
                        "confirmation_required",
                        "failed",
                        "not_executed",
                    }
                    or call.get("sideEffect")
                    not in {"none", "expected", "prohibited", "not_observed"}
                    or call.get("idempotency") not in {"required", "not_required", "not_applicable"}
                ):
                    raise DefinitionValidationError("tool call expectation is invalid")
                if call["argumentMatch"] == "exact_digest":
                    _digest(call.get("expectedArgumentDigest"), "expectedArgumentDigest")
                elif "expectedArgumentDigest" in call:
                    raise DefinitionValidationError("schema-only tool call cannot carry a digest")
            _bounded_int(nested.get("maxRedundantCalls"), "maxRedundantCalls", lower=0, upper=64)
            if "prohibitedToolIds" in nested:
                _list_of_ids(nested["prohibitedToolIds"], "prohibitedToolIds")
        elif family == "policy":
            if (
                nested.get("decision") not in {"allow", "deny", "confirm"}
                or nested.get("confirmation") not in {"required", "not_required", "prohibited"}
                or nested.get("sensitiveDataHandling")
                not in {"allow_authorized", "redact", "deny", "not_applicable"}
                or nested.get("consequentialAction")
                not in {"execute_after_confirmation", "deny", "no_action", "not_applicable"}
            ):
                raise DefinitionValidationError("policy expectation is invalid")
            if "policyRevision" in nested:
                _identifier(nested["policyRevision"], "policyRevision")
        elif family == "groundedness":
            claims = nested.get("claims")
            if (
                not isinstance(claims, Sequence)
                or isinstance(claims, (str, bytes))
                or not 1 <= len(claims) <= 128
            ):
                raise DefinitionValidationError("groundedness claims are bounded")
            for item in claims:
                claim = _closed_map(
                    item,
                    "claim",
                    frozenset({"claimId", "support", "supportEvidenceIds", "citation"}),
                )
                _identifier(claim.get("claimId"), "claimId")
                if claim.get("support") not in {"supported", "unsupported", "refused"} or claim.get(
                    "citation"
                ) not in {"required", "optional", "prohibited", "not_applicable"}:
                    raise DefinitionValidationError("groundedness claim is invalid")
                if "supportEvidenceIds" not in claim:
                    raise DefinitionValidationError(
                        "groundedness claim support evidence is required"
                    )
                _list_of_ids(
                    claim["supportEvidenceIds"],
                    "supportEvidenceIds",
                    allow_empty=claim["support"] != "supported",
                )
            if nested.get("unsupportedClaimPolicy") not in {
                "fail",
                "allow_refusal",
                "not_applicable",
            }:
                raise DefinitionValidationError("groundedness policy is invalid")
        elif family == "response":
            if "expectedDigest" in nested:
                _digest(nested["expectedDigest"], "expectedDigest")
                if nested.get("normalization") not in {"none", "unicode_nfc", "trimmed_text"}:
                    raise DefinitionValidationError("response digest normalization is required")
            elif "normalization" in nested:
                raise DefinitionValidationError("response normalization requires expectedDigest")
            if nested.get("refusal") not in {"required", "allowed", "prohibited"}:
                raise DefinitionValidationError("response refusal policy is invalid")
            props = nested.get("properties")
            if (
                not isinstance(props, Sequence)
                or isinstance(props, (str, bytes))
                or not 1 <= len(props) <= 8
                or any(
                    item
                    not in {
                        "non_empty",
                        "valid_utf8",
                        "schema_valid",
                        "citation_present",
                        "refusal_present",
                        "requested_style_marker_present",
                    }
                    for item in props
                )
            ):
                raise DefinitionValidationError("response properties are invalid")
            for key, upper in (("maxBytes", 1_073_741_824), ("maxTokens", 100_000_000)):
                _bounded_int(nested.get(key), key, lower=0, upper=upper)
            for key in ("requiredInstructionIds", "requiredStyleIds"):
                if key in nested:
                    _list_of_ids(nested[key], key)
        elif family == "resource":
            if not nested:
                raise DefinitionValidationError("resource expectation cannot be empty")
            for key, upper in (
                ("maxLatencyMs", 86_400_000),
                ("maxQueueMs", 86_400_000),
                ("maxInputBytes", 1_073_741_824),
                ("maxOutputBytes", 1_073_741_824),
                ("maxTokens", 100_000_000),
                ("maxMemoryBytes", 1_099_511_627_776),
                ("maxProviderCalls", 100_000),
                ("maxConcurrency", 64),
                ("maxOperations", 1_000_000),
            ):
                if key in nested:
                    _bounded_int(
                        nested[key], key, lower=1 if key == "maxConcurrency" else 0, upper=upper
                    )
            if "minThroughputPerSecond" in nested:
                _bounded_number(
                    nested["minThroughputPerSecond"],
                    "minThroughputPerSecond",
                    lower=0.0,
                    upper=1_000_000_000.0,
                )
    return json.loads(json.dumps(data, separators=(",", ":"), sort_keys=True))


@dataclass(frozen=True, slots=True)
class CaseExpectations:
    families: Mapping[str, Any]

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> CaseExpectations:
        return cls(MappingProxyType(_validate_expectations(value)))

    def to_payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(dict(self.families))))


@dataclass(frozen=True, slots=True)
class _DefinitionBase:
    schema_version: int
    resource_id: str
    version: str
    source_kind: SourceKind
    scopes: tuple[str, ...]
    capture_authorization: CaptureAuthorization
    retention_mode: str
    provenance: ResourceProvenance
    created_at: datetime
    resource_type: ClassVar[str]

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "resourceType": self.resource_type,
            "sourceKind": self.source_kind.value,
            "scopes": list(self.scopes),
            "captureAuthorization": self.capture_authorization.to_payload(),
            "retentionMode": self.retention_mode,
            "provenance": self.provenance.to_payload(),
            "createdAt": _iso(self.created_at),
        }

    def to_payload(self) -> dict[str, Any]:
        raise NotImplementedError

    def canonical_fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_storage(self) -> dict[str, Any]:
        payload = self.to_payload()
        payload.pop("content", None)
        return {
            "resource_id": self.resource_id,
            "version": self.version,
            "fingerprint": self.canonical_fingerprint(),
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class DatasetDefinition(_DefinitionBase):
    resource_type: ClassVar[str] = "dataset"
    case_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def dataset_id(self) -> str:
        return self.resource_id

    @property
    def dataset_version(self) -> str:
        return self.version

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> DatasetDefinition:
        data = _closed_map(
            value,
            "dataset",
            frozenset(
                {
                    "schemaVersion",
                    "resourceType",
                    "datasetId",
                    "datasetVersion",
                    "sourceKind",
                    "scopes",
                    "captureAuthorization",
                    "caseIds",
                    "caseCount",
                    "retentionMode",
                    "provenance",
                    "createdAt",
                }
            ),
        )
        if data.get("schemaVersion") != 1 or data.get("resourceType") != "dataset":
            raise DefinitionValidationError("dataset schema identity is invalid")
        source = data.get("sourceKind")
        if source not in {item.value for item in SourceKind}:
            raise DefinitionValidationError("dataset source kind is unsupported")
        scopes = _scopes(data.get("scopes"))
        case_ids = tuple(_uuid(item, "caseId") for item in cast(Sequence[Any], data.get("caseIds")))
        if not case_ids or len(case_ids) > 100_000 or len(set(case_ids)) != len(case_ids):
            raise DefinitionValidationError("dataset caseIds are invalid")
        case_count = data.get("caseCount", len(case_ids))
        if type(case_count) is not int or case_count != len(case_ids):
            raise DefinitionValidationError("dataset caseCount is invalid")
        authorization = CaptureAuthorization.from_payload(
            cast(Mapping[str, Any], data.get("captureAuthorization"))
        )
        _validate_source_capture(source, authorization, "dataset")
        return cls(
            1,
            _uuid(data.get("datasetId"), "datasetId"),
            _version(data.get("datasetVersion"), "datasetVersion"),
            SourceKind(source),
            scopes,
            authorization,
            _retention(data.get("retentionMode")),
            ResourceProvenance.from_payload(cast(Mapping[str, Any], data.get("provenance"))),
            _timestamp(data.get("createdAt"), "createdAt"),
            case_ids,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._base_payload(),
            "datasetId": self.resource_id,
            "datasetVersion": self.version,
            "caseIds": list(self.case_ids),
            # Persist the projection count as an authoritative bounded fact;
            # readers must not reconstruct it from legacy payloads.
            "caseCount": len(self.case_ids),
        }


@dataclass(frozen=True, slots=True)
class CaseDefinition(_DefinitionBase):
    resource_type: ClassVar[str] = "case"
    dataset: ResourceReference = field(
        default_factory=lambda: ResourceReference("00000000-0000-0000-0000-000000000000", "0")
    )
    content_digest: str | None = None
    input_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    expectations: CaseExpectations | None = None

    @property
    def case_id(self) -> str:
        return self.resource_id

    @property
    def case_version(self) -> str:
        return self.version

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> CaseDefinition:
        allowed = {
            "schemaVersion",
            "resourceType",
            "caseId",
            "caseVersion",
            "dataset",
            "sourceKind",
            "scopes",
            "captureAuthorization",
            "content",
            "contentDigest",
            "inputArtifactIds",
            "expectations",
            "retentionMode",
            "provenance",
            "createdAt",
        }
        data = _closed_map(value, "case", frozenset(allowed))
        if (
            data.get("schemaVersion") != 1
            or data.get("resourceType") != "case"
            or "content" in data
        ):
            raise DefinitionValidationError("case must be a content-free stored projection")
        source = data.get("sourceKind")
        if source not in {item.value for item in SourceKind}:
            raise DefinitionValidationError("case source kind is unsupported")
        capture = CaptureAuthorization.from_payload(
            cast(Mapping[str, Any], data.get("captureAuthorization"))
        )
        _validate_source_capture(source, capture, "case")
        if (
            source == "captured"
            and capture.capture_classification is CaptureClassification.METADATA_ONLY
        ):
            raise DefinitionValidationError(
                "captured cases require an authorized capture classification"
            )
        digest = (
            _digest(data["contentDigest"], "contentDigest") if "contentDigest" in data else None
        )
        artifact_ids = tuple(
            _uuid(item, "inputArtifactId")
            for item in cast(Sequence[Any], data.get("inputArtifactIds", []))
        )
        if len(artifact_ids) > 32 or len(set(artifact_ids)) != len(artifact_ids):
            raise DefinitionValidationError("case artifact references are invalid")
        if source in {"owner_supplied", "captured"} and digest is None and not artifact_ids:
            raise DefinitionValidationError(
                "content-bearing cases require an authorized artifact or digest"
            )
        if source == "owner_supplied" and not artifact_ids:
            raise DefinitionValidationError("owner-supplied cases require an authorized artifact")
        if source == "owner_supplied" and digest is not None and not artifact_ids:
            raise DefinitionValidationError("owner-supplied digest requires an authorized artifact")
        if capture.capture_classification is CaptureClassification.METADATA_ONLY and (
            digest is not None or artifact_ids
        ):
            raise DefinitionValidationError("metadata-only cases cannot reference content")
        if capture.capture_classification is CaptureClassification.HASHED and digest is None:
            raise DefinitionValidationError("hashed cases require a content digest")
        if (
            capture.capture_classification
            in {
                CaptureClassification.REDACTED,
                CaptureClassification.SAMPLED_CONTENT,
                CaptureClassification.FULL_CONTENT,
            }
            and not artifact_ids
        ):
            raise DefinitionValidationError(
                "content-bearing cases require an opaque artifact reference"
            )
        expectations = (
            CaseExpectations.from_payload(cast(Mapping[str, Any], data["expectations"]))
            if "expectations" in data
            else None
        )
        return cls(
            1,
            _uuid(data.get("caseId"), "caseId"),
            _version(data.get("caseVersion"), "caseVersion"),
            SourceKind(source),
            _scopes(data.get("scopes")),
            capture,
            _retention(data.get("retentionMode")),
            ResourceProvenance.from_payload(cast(Mapping[str, Any], data.get("provenance"))),
            _timestamp(data.get("createdAt"), "createdAt"),
            ResourceReference.from_payload(cast(Mapping[str, Any], data.get("dataset")), "dataset"),
            digest,
            artifact_ids,
            expectations,
        )

    def to_payload(self) -> dict[str, Any]:
        data = {
            **self._base_payload(),
            "caseId": self.resource_id,
            "caseVersion": self.version,
            "dataset": self.dataset.to_payload(),
        }
        if self.content_digest is not None:
            data["contentDigest"] = self.content_digest
        if self.input_artifact_ids:
            data["inputArtifactIds"] = list(self.input_artifact_ids)
        if self.expectations is not None:
            data["expectations"] = self.expectations.to_payload()
        return data


@dataclass(frozen=True, slots=True)
class EvaluatorDefinition(_DefinitionBase):
    resource_type: ClassVar[str] = "evaluator"
    kind: str = "deterministic"
    metric: str = ""
    component_ids: tuple[str, ...] = field(default_factory=tuple)
    rubric_version: str | None = None
    calibration_version: str | None = None

    @property
    def evaluator_id(self) -> str:
        return self.resource_id

    @property
    def evaluator_version(self) -> str:
        return self.version

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> EvaluatorDefinition:
        data = _closed_map(
            value,
            "evaluator",
            frozenset(
                {
                    "schemaVersion",
                    "resourceType",
                    "evaluatorId",
                    "evaluatorVersion",
                    "kind",
                    "metric",
                    "componentIds",
                    "rubricVersion",
                    "calibrationVersion",
                    "retentionMode",
                    "provenance",
                    "createdAt",
                }
            ),
        )
        if data.get("schemaVersion") != 1 or data.get("resourceType") != "evaluator":
            raise DefinitionValidationError("evaluator schema identity is invalid")
        if data.get("kind") != "deterministic":
            raise DefinitionValidationError("AURA-0093 only admits deterministic evaluators")
        component_ids = _list_of_ids(data.get("componentIds"), "componentIds", maximum=32)
        if any(not _COMPONENT.fullmatch(item) for item in component_ids):
            raise DefinitionValidationError("evaluator component id is invalid")
        return cls(
            1,
            _uuid(data.get("evaluatorId"), "evaluatorId"),
            _version(data.get("evaluatorVersion"), "evaluatorVersion"),
            SourceKind.SYNTHETIC,
            ("private",),
            CaptureAuthorization.from_payload(
                cast(
                    Mapping[str, Any],
                    data.get(
                        "captureAuthorization",
                        {
                            "captureClassification": "metadata_only",
                            "authorizationPurpose": "synthetic_benchmark",
                            "authorizationContext": "controlled_evaluation",
                            "retentionMode": "manual_only",
                        },
                    ),
                )
            ),
            _retention(data.get("retentionMode", "manual_only")),
            ResourceProvenance.from_payload(cast(Mapping[str, Any], data.get("provenance"))),
            _timestamp(data.get("createdAt"), "createdAt"),
            "deterministic",
            _identifier(data.get("metric"), "metric"),
            tuple(component_ids),
            _version(data["rubricVersion"], "rubricVersion") if "rubricVersion" in data else None,
            _version(data["calibrationVersion"], "calibrationVersion")
            if "calibrationVersion" in data
            else None,
        )

    def to_payload(self) -> dict[str, Any]:
        data = {
            "schemaVersion": 1,
            "resourceType": "evaluator",
            "evaluatorId": self.resource_id,
            "evaluatorVersion": self.version,
            "kind": self.kind,
            "metric": self.metric,
            "componentIds": list(self.component_ids),
            "retentionMode": self.retention_mode,
            "provenance": self.provenance.to_payload(),
            "createdAt": _iso(self.created_at),
        }
        if self.rubric_version is not None:
            data["rubricVersion"] = self.rubric_version
        if self.calibration_version is not None:
            data["calibrationVersion"] = self.calibration_version
        return data


def _default_evaluator_compatibility(definition: EvaluatorDefinition) -> None:
    """Validate a definition against the closed deterministic registry IDs."""

    if definition.kind != "deterministic":
        raise DefinitionValidationError("AURA-0093 only admits deterministic evaluators")
    family = BUILTIN_EVALUATOR_IDS.get(definition.evaluator_id)
    if family is None:
        if (
            definition.evaluator_id == LEGACY_EXACT_EVALUATOR_ID
            and definition.evaluator_version == "1.0.0"
            and definition.metric == "exact_match"
        ):
            return
        raise DefinitionValidationError("unknown deterministic evaluator")
    if definition.evaluator_version != "1.0.0":
        raise DefinitionValidationError("unknown deterministic evaluator version")
    if definition.metric != f"evaluation.{family}.ratio":
        raise DefinitionValidationError("evaluator metric is not runtime compatible")


@dataclass(frozen=True, slots=True)
class SuiteDefinition(_DefinitionBase):
    resource_type: ClassVar[str] = "suite"
    dataset: ResourceReference = field(
        default_factory=lambda: ResourceReference("00000000-0000-0000-0000-000000000000", "0")
    )
    evaluators: tuple[EvaluatorReference, ...] = field(default_factory=tuple)
    components: tuple[ComponentBinding, ...] = field(default_factory=tuple)
    limits: ExecutionLimits = field(default_factory=lambda: ExecutionLimits(100, 1, 1, 1, 0, 0, 0))
    manual_thresholds: tuple[ManualThreshold, ...] = field(default_factory=tuple)
    variant_bindings: tuple[VariantBinding, ...] = field(default_factory=tuple)

    @property
    def suite_id(self) -> str:
        return self.resource_id

    @property
    def suite_version(self) -> str:
        return self.version

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> SuiteDefinition:
        data = _closed_map(
            value,
            "suite",
            frozenset(
                {
                    "schemaVersion",
                    "resourceType",
                    "suiteId",
                    "suiteVersion",
                    "dataset",
                    "evaluators",
                    "components",
                    "limits",
                    "manualThresholds",
                    "variantBindings",
                    "retentionMode",
                    "provenance",
                    "createdAt",
                }
            ),
        )
        if data.get("schemaVersion") != 1 or data.get("resourceType") != "suite":
            raise DefinitionValidationError("suite schema identity is invalid")
        evaluators_data = data.get("evaluators")
        components_data = data.get("components")
        if (
            not isinstance(evaluators_data, Sequence)
            or isinstance(evaluators_data, (str, bytes))
            or not 1 <= len(evaluators_data) <= 32
            or not isinstance(components_data, Sequence)
            or isinstance(components_data, (str, bytes))
            or not 1 <= len(components_data) <= 32
        ):
            raise DefinitionValidationError("suite bindings are bounded and required")
        evaluators = tuple(
            EvaluatorReference.from_payload(cast(Mapping[str, Any], item))
            for item in evaluators_data
        )
        components = tuple(
            ComponentBinding.from_payload(cast(Mapping[str, Any], item)) for item in components_data
        )
        if len({(item.evaluator_id, item.evaluator_version) for item in evaluators}) != len(
            evaluators
        ) or len({(item.component_id, item.component_version) for item in components}) != len(
            components
        ):
            raise DefinitionValidationError("suite bindings must be unique")
        thresholds_data = data.get("manualThresholds", [])
        if (
            not isinstance(thresholds_data, Sequence)
            or isinstance(thresholds_data, (str, bytes))
            or len(thresholds_data) > 128
        ):
            raise DefinitionValidationError("suite thresholds are bounded")
        thresholds = tuple(
            ManualThreshold.from_payload(cast(Mapping[str, Any], item)) for item in thresholds_data
        )
        variant_data = data.get("variantBindings", [])
        if (
            not isinstance(variant_data, Sequence)
            or isinstance(variant_data, (str, bytes))
            or len(variant_data) > 16
        ):
            raise DefinitionValidationError("variant bindings are bounded")
        variant_bindings = tuple(
            VariantBinding.from_payload(cast(Mapping[str, Any], item))
            for item in variant_data
        )
        if len({item.variant_id for item in variant_bindings}) != len(variant_bindings):
            raise DefinitionValidationError("variant identifiers must be unique")
        if len(
            {
                (item.evaluator.evaluator_id, item.evaluator.evaluator_version, item.metric)
                for item in thresholds
            }
        ) != len(thresholds):
            raise DefinitionValidationError("suite thresholds must be unique")
        return cls(
            1,
            _uuid(data.get("suiteId"), "suiteId"),
            _version(data.get("suiteVersion"), "suiteVersion"),
            SourceKind.SYNTHETIC,
            ("private",),
            CaptureAuthorization.from_payload(
                cast(
                    Mapping[str, Any],
                    data.get(
                        "captureAuthorization",
                        {
                            "captureClassification": "metadata_only",
                            "authorizationPurpose": "synthetic_benchmark",
                            "authorizationContext": "controlled_evaluation",
                            "retentionMode": "manual_only",
                        },
                    ),
                )
            ),
            _retention(data.get("retentionMode", "manual_only")),
            ResourceProvenance.from_payload(cast(Mapping[str, Any], data.get("provenance"))),
            _timestamp(data.get("createdAt"), "createdAt"),
            ResourceReference.from_payload(cast(Mapping[str, Any], data.get("dataset")), "dataset"),
            evaluators,
            components,
            ExecutionLimits.from_payload(cast(Mapping[str, Any], data.get("limits"))),
            thresholds,
            variant_bindings,
        )

    def to_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schemaVersion": 1,
            "resourceType": "suite",
            "suiteId": self.resource_id,
            "suiteVersion": self.version,
            "dataset": self.dataset.to_payload(),
            "evaluators": [item.to_payload() for item in self.evaluators],
            "components": [item.to_payload() for item in self.components],
            "limits": self.limits.to_payload(),
            "retentionMode": self.retention_mode,
            "provenance": self.provenance.to_payload(),
            "createdAt": _iso(self.created_at),
        }
        if self.manual_thresholds:
            data["manualThresholds"] = [item.to_payload() for item in self.manual_thresholds]
        if self.variant_bindings:
            data["variantBindings"] = [item.to_payload() for item in self.variant_bindings]
        return data


class EvaluationDefinitionService:
    """Application boundary for parsing and admitting immutable definitions."""

    def __init__(
        self,
        repository: EvaluationDefinitionRepository,
        artifact_repository: EvaluationArtifactRepository | None = None,
        metrics: ObservatoryMetrics | None = None,
        evaluator_compatibility: Callable[[EvaluatorDefinition], None] | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_repository = artifact_repository
        self._metrics = metrics
        self._evaluator_compatibility = evaluator_compatibility or _default_evaluator_compatibility

    async def begin_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        resource_id: str,
        resource_version: str,
    ) -> DefinitionCommandClaim:
        return await self._repository.begin_definition_command(
            operation,
            idempotency_key,
            request_fingerprint,
            resource_id,
            resource_version,
        )

    async def complete_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        response: Mapping[str, object],
        claim_token: str,
    ) -> dict[str, object]:
        if not claim_token:
            raise DefinitionCommandPendingError("definition command claim token is required")
        return await self._repository.complete_definition_command(
            operation, idempotency_key, request_fingerprint, response, claim_token
        )

    async def renew_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        claim_token: str,
    ) -> None:
        renewed = await self._repository.renew_definition_command(
            operation, idempotency_key, request_fingerprint, claim_token
        )
        if renewed is False:
            raise DefinitionCommandPendingError("definition command lease is no longer owned")

    async def validate_execution_bindings(
        self,
        *,
        dataset_id: str,
        dataset_version: str,
        suite_id: str,
        suite_version: str,
        evaluator_id: str,
        evaluator_version: str,
        provenance: Mapping[str, Any],
        target_component: Mapping[str, Any],
        case_ids: Sequence[str],
        execution_limits: Mapping[str, Any],
        max_attempts: int,
    ) -> None:
        """Require an admitted job to use exact persisted definition bindings."""

        suite = await self._repository.get_suite(suite_id, suite_version)
        if suite is None:
            raise DefinitionNotFoundError("suite version does not exist")
        dataset = await self._repository.get_dataset(dataset_id, dataset_version)
        if dataset is None:
            raise DefinitionNotFoundError("dataset version does not exist")
        if (
            suite.dataset.resource_id != dataset.resource_id
            or suite.dataset.version != dataset_version
        ):
            raise DefinitionValidationError("job dataset does not match suite dataset binding")
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise DefinitionValidationError("job case selection is invalid")
        dataset_case_ids = set(dataset.case_ids)
        selected_case_ids = set(case_ids)
        selection_mode = "complete" if selected_case_ids == dataset_case_ids else "subset"
        if selection_mode not in {"complete", "subset"}:
            raise DefinitionValidationError("job case selection semantics are invalid")
        if selection_mode == "complete" and not await (
            self._repository.dataset_case_membership_complete(dataset_id, dataset_version)
        ):
            raise DefinitionValidationError("complete job case selection is unavailable")
        if len(case_ids) > suite.limits.max_cases:
            raise DefinitionValidationError("job case selection exceeds suite case budget")
        if not await self._repository.case_selection_is_valid(
            dataset_id, dataset_version, tuple(case_ids)
        ):
            raise DefinitionValidationError("job case is not bound to dataset version")
        if max_attempts < 1 or max_attempts > suite.limits.max_attempts:
            raise DefinitionValidationError("job attempts exceed suite execution limit")
        expected_limits = suite.limits.to_payload()
        if set(execution_limits) != set(expected_limits):
            raise DefinitionValidationError("job execution limits are invalid")
        for name, requested in execution_limits.items():
            if type(requested) is not int or requested < 0:
                raise DefinitionValidationError("job execution limits are invalid")
            if requested > expected_limits[name]:
                raise DefinitionValidationError("job execution limits broaden suite limits")
        if execution_limits["maxCases"] < len(case_ids):
            raise DefinitionValidationError("job execution case limit is too small")
        evaluator = await self._repository.get_evaluator(evaluator_id, evaluator_version)
        if evaluator is None:
            raise DefinitionNotFoundError("evaluator version does not exist")
        suite_evaluators = tuple(
            (reference.evaluator_id, reference.evaluator_version, reference.kind)
            for reference in suite.evaluators
        )
        selected_evaluator = (evaluator_id, evaluator_version, evaluator.kind)
        if selected_evaluator not in suite_evaluators:
            raise DefinitionValidationError("job evaluator is not bound to suite")
        provenance_dataset = provenance.get("dataset")
        provenance_suite = provenance.get("suite")
        if (
            not isinstance(provenance_dataset, Mapping)
            or not isinstance(provenance_suite, Mapping)
            or set(provenance_dataset) != {"id", "version"}
            or set(provenance_suite) != {"id", "version"}
        ):
            raise DefinitionValidationError("runner provenance bindings are invalid")
        provenance_dataset = cast(Mapping[str, Any], provenance_dataset)
        provenance_suite = cast(Mapping[str, Any], provenance_suite)
        if (
            provenance_dataset.get("id") != dataset_id
            or provenance_dataset.get("version") != dataset_version
            or provenance_suite.get("id") != suite_id
            or provenance_suite.get("version") != suite_version
        ):
            raise DefinitionValidationError("runner provenance definition binding mismatch")
        provenance_evaluators = provenance.get("evaluators")
        if not isinstance(provenance_evaluators, list) or len(provenance_evaluators) != 1:
            raise DefinitionValidationError("runner provenance evaluator binding mismatch")
        evaluator_item = provenance_evaluators[0]
        if not isinstance(evaluator_item, Mapping) or set(evaluator_item) != {
            "evaluatorId", "evaluatorVersion", "kind"
        }:
            raise DefinitionValidationError("runner provenance evaluator binding mismatch")
        typed_evaluator = cast(Mapping[str, Any], evaluator_item)
        if (
            typed_evaluator.get("evaluatorId"),
            typed_evaluator.get("evaluatorVersion"),
            typed_evaluator.get("kind"),
        ) != selected_evaluator:
            raise DefinitionValidationError("runner provenance evaluator binding mismatch")
        suite_components = {
            (
                binding.component_id,
                binding.component_version,
                binding.code_revision,
                binding.image_digest,
                binding.configuration_revision,
            )
            for binding in suite.components
        }
        provenance_components = provenance.get("components")
        if not isinstance(provenance_components, list):
            raise DefinitionValidationError("runner provenance component binding mismatch")
        provenance_component_values: list[tuple[str, str, str, str, str]] = []
        for item in provenance_components:
            if not isinstance(item, Mapping) or set(item) != {
                "componentId",
                "componentVersion",
                "codeRevision",
                "imageDigest",
                "configurationRevision",
            }:
                raise DefinitionValidationError("runner provenance component binding mismatch")
            try:
                binding = ComponentBinding.from_payload(cast(Mapping[str, Any], item))
            except DefinitionValidationError as exc:
                raise DefinitionValidationError(
                    "runner provenance component binding mismatch"
                ) from exc
            provenance_component_values.append(
                (
                    binding.component_id,
                    binding.component_version,
                    binding.code_revision,
                    binding.image_digest,
                    binding.configuration_revision,
                )
            )
        if len(set(provenance_component_values)) != len(provenance_component_values):
            raise DefinitionValidationError("runner provenance component binding mismatch")
        if set(provenance_component_values) != suite_components:
            raise DefinitionValidationError("runner provenance component binding mismatch")
        if set(target_component) != {
            "componentId",
            "componentVersion",
            "codeRevision",
            "imageDigest",
            "configurationRevision",
        }:
            raise DefinitionValidationError("runner target component binding mismatch")
        try:
            target = ComponentBinding.from_payload(target_component)
        except DefinitionValidationError as exc:
            raise DefinitionValidationError("runner target component binding mismatch") from exc
        target_value = (
            target.component_id,
            target.component_version,
            target.code_revision,
            target.image_digest,
            target.configuration_revision,
        )
        if (
            target_value not in suite_components
        ):
            raise DefinitionValidationError("runner target component is not bound to suite")

    async def admit_dataset(self, payload: Mapping[str, Any]) -> DatasetDefinition:
        started = monotonic()
        try:
            definition = DatasetDefinition.from_payload(payload)
            _validate_source_capture(
                definition.source_kind, definition.capture_authorization, "dataset"
            )
            await self._validate_authorization(definition)
            result = await self._repository.create_dataset(definition)
            self._record(definition.resource_type, "accepted", _duration_ms(started))
            return result
        except DefinitionStorageError:
            self._record("dataset", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None
        except (DefinitionValidationError, KeyError):
            self._record("dataset", "rejected", _duration_ms(started), "validation")
            raise
        except Exception:
            self._record("dataset", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None

    async def admit_case(self, payload: Mapping[str, Any]) -> CaseDefinition:
        started = monotonic()
        try:
            definition = CaseDefinition.from_payload(payload)
            if (
                await self._repository.get_dataset(
                    definition.dataset.resource_id, definition.dataset.version
                )
                is None
            ):
                raise DefinitionNotFoundError("case dataset version does not exist")
            dataset = await self._repository.get_dataset(
                definition.dataset.resource_id, definition.dataset.version
            )
            if dataset is None:
                raise DefinitionNotFoundError("case dataset version does not exist")
            self._validate_dataset_case_binding(dataset, definition)
            if not await self._repository.case_declared(
                definition.dataset.resource_id, definition.dataset.version, definition.case_id
            ):
                raise DefinitionValidationError("case is not declared by the dataset version")
            await self._validate_authorization(definition)
            if definition.input_artifact_ids:
                if self._artifact_repository is None:
                    raise DefinitionNotFoundError("artifact metadata repository is unavailable")
                for reference in definition.input_artifact_ids:
                    metadata = await self._artifact_repository.lookup_artifact_metadata(reference)
                    self._validate_artifact_metadata(definition, metadata, reference)
            result = await self._repository.create_case(definition)
            self._record(definition.resource_type, "accepted", _duration_ms(started))
            return result
        except DefinitionStorageError:
            self._record("case", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None
        except (DefinitionValidationError, KeyError):
            self._record("case", "rejected", _duration_ms(started), "validation")
            raise
        except Exception:
            self._record("case", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None

    async def admit_evaluator(self, payload: Mapping[str, Any]) -> EvaluatorDefinition:
        started = monotonic()
        try:
            definition = EvaluatorDefinition.from_payload(payload)
            result = await self._repository.create_evaluator(definition)
            self._record(definition.resource_type, "accepted", _duration_ms(started))
            return result
        except DefinitionStorageError:
            self._record("evaluator", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None
        except (DefinitionValidationError, KeyError):
            self._record("evaluator", "rejected", _duration_ms(started), "validation")
            raise
        except Exception:
            self._record("evaluator", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None

    async def admit_suite(self, payload: Mapping[str, Any]) -> SuiteDefinition:
        started = monotonic()
        try:
            definition = SuiteDefinition.from_payload(payload)
            if (
                await self._repository.get_dataset(
                    definition.dataset.resource_id, definition.dataset.version
                )
                is None
            ):
                raise DefinitionNotFoundError("suite dataset version does not exist")
            for reference in definition.evaluators:
                admitted_evaluator = await self._repository.get_evaluator(
                    reference.evaluator_id, reference.evaluator_version
                )
                if admitted_evaluator is None:
                    raise DefinitionNotFoundError("suite evaluator version does not exist")
                try:
                    self._evaluator_compatibility(admitted_evaluator)
                except DefinitionValidationError:
                    raise DefinitionValidationError(
                        "suite evaluator is not runtime compatible"
                    ) from None
                if reference.kind != admitted_evaluator.kind:
                    raise DefinitionValidationError("suite evaluator kind is incompatible")
                for binding in definition.components:
                    if binding.component_id not in admitted_evaluator.component_ids:
                        raise DefinitionValidationError(
                            "suite component is not declared by evaluator"
                        )
            for binding in definition.components:
                if not await self._repository.component_exists(
                    binding.component_id, binding.component_version
                ):
                    raise DefinitionNotFoundError("suite component version does not exist")
            threshold_keys = {
                (item.evaluator.evaluator_id, item.evaluator.evaluator_version)
                for item in definition.manual_thresholds
            }
            evaluator_keys = {
                (item.evaluator_id, item.evaluator_version) for item in definition.evaluators
            }
            if not threshold_keys.issubset(evaluator_keys):
                raise DefinitionValidationError("suite threshold evaluator is not bound")
            evaluator_metrics: dict[tuple[str, str], str] = {}
            for reference in definition.evaluators:
                evaluator = await self._repository.get_evaluator(
                    reference.evaluator_id, reference.evaluator_version
                )
                if evaluator is not None:
                    evaluator_metrics[
                        (reference.evaluator_id, reference.evaluator_version)
                    ] = evaluator.metric
            for threshold in definition.manual_thresholds:
                key = (threshold.evaluator.evaluator_id, threshold.evaluator.evaluator_version)
                declared_metric = evaluator_metrics.get(key)
                compatible_metric = declared_metric == threshold.metric or (
                    threshold.evaluator.evaluator_id == LEGACY_EXACT_EVALUATOR_ID
                    and declared_metric == "exact_match"
                    and threshold.metric == "evaluation.exact.ratio"
                )
                if not compatible_metric:
                    raise DefinitionValidationError(
                        "suite threshold metric does not match evaluator"
                    )
            if not await self._repository.dataset_case_membership_complete(
                definition.dataset.resource_id, definition.dataset.version
            ):
                raise DefinitionNotFoundError("suite dataset cases are not fully resolved")
            result = await self._repository.create_suite(definition)
            self._record(definition.resource_type, "accepted", _duration_ms(started))
            return result
        except DefinitionStorageError:
            self._record("suite", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None
        except (DefinitionValidationError, KeyError):
            self._record("suite", "rejected", _duration_ms(started), "validation")
            raise
        except Exception:
            self._record("suite", "rejected", _duration_ms(started), "database_failure")
            raise DefinitionValidationError("definition storage failure") from None

    def _record(
        self,
        resource_type: str,
        outcome: str,
        duration_ms: float = 0.0,
        failure_code: str | None = None,
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.record(
            "observatory_evaluation_definition_operation_count",
            labels={"resource_type": resource_type, "outcome": outcome},
        )
        if failure_code != "database_failure":
            self._metrics.record(
                "observatory_evaluation_definition_validation_count",
                labels={
                    "resource_type": resource_type,
                    "outcome": outcome,
                    **({"failure_code": failure_code} if failure_code else {}),
                },
            )
        self._metrics.record_component_span(
            component_id="aura.observatory.evaluation_definitions",
            component_version="1.0.0",
            deployment_id=self._metrics.deployment_id,
            operation="admit",
            duration_ms=duration_ms,
            outcome="success" if outcome == "accepted" else "failure",
            dependency="evaluation_definition_state",
            error_code=failure_code,
        )

    @staticmethod
    def _validate_dataset_case_binding(
        dataset: DatasetDefinition, case: CaseDefinition
    ) -> None:
        if case.source_kind is not dataset.source_kind:
            raise DefinitionValidationError("case source differs from dataset source")
        order = {
            CaptureClassification.METADATA_ONLY: 0,
            CaptureClassification.HASHED: 1,
            CaptureClassification.REDACTED: 2,
            CaptureClassification.SAMPLED_CONTENT: 3,
            CaptureClassification.FULL_CONTENT: 4,
        }
        if order[case.capture_authorization.capture_classification] > order[
            dataset.capture_authorization.capture_classification
        ]:
            raise DefinitionValidationError("case capture exceeds dataset authorization")
        if (
            case.capture_authorization.authorization_purpose
            != dataset.capture_authorization.authorization_purpose
        ):
            raise DefinitionValidationError("case authorization differs from dataset")

    @staticmethod
    def _validate_artifact_metadata(
        definition: CaseDefinition,
        metadata: EvaluationArtifactMetadata | None,
        expected_reference: str | None = None,
    ) -> None:
        if metadata is None:
            raise DefinitionNotFoundError("authorized evaluation artifact is unavailable")
        _uuid(metadata.artifact_reference_id, "artifactReferenceId")
        _uuid(metadata.dataset_id, "artifactDatasetId")
        _uuid(metadata.authorization_record_id, "artifactAuthorizationRecordId")
        if metadata.status != "available" or metadata.revoked:
            raise DefinitionNotFoundError("authorized evaluation artifact is unavailable")
        if expected_reference is not None and metadata.artifact_reference_id != expected_reference:
            raise DefinitionValidationError("evaluation artifact reference is incompatible")
        authorization = definition.capture_authorization
        if (
            metadata.dataset_id != definition.dataset.resource_id
            or metadata.authorization_record_id != authorization.authorization_record_id
            or metadata.authorization_purpose != authorization.authorization_purpose
            or metadata.authorization_context != authorization.authorization_context
            or metadata.retention_mode != "manual_only"
            or metadata.capture_classification != authorization.capture_classification.value
            or (
                definition.content_digest is not None
                and metadata.sha256 != definition.content_digest
            )
            or (
                metadata.authorized_at is not None
                and definition.capture_authorization.authorized_at is not None
                and metadata.authorized_at.astimezone(UTC)
                != definition.capture_authorization.authorized_at.astimezone(UTC)
            )
        ):
            raise DefinitionValidationError("evaluation artifact authorization is incompatible")

    async def _validate_authorization(
        self, definition: DatasetDefinition | CaseDefinition
    ) -> None:
        authorization = definition.capture_authorization
        if authorization.capture_classification is CaptureClassification.METADATA_ONLY:
            return
        if self._artifact_repository is None or authorization.authorization_record_id is None:
            raise DefinitionNotFoundError("authorization metadata repository is unavailable")
        lookup = getattr(self._artifact_repository, "lookup_authorization_metadata", None)
        if lookup is None:
            # AURA-0090 test doubles predating the narrow authorization port
            # expose only artifact lookup.  Use that metadata as a compatibility
            # fallback; production adapters implement the authoritative lookup.
            if isinstance(definition, CaseDefinition) and definition.input_artifact_ids:
                metadata = await self._artifact_repository.lookup_artifact_metadata(
                    definition.input_artifact_ids[0]
                )
                self._validate_artifact_metadata(
                    definition, metadata, definition.input_artifact_ids[0]
                )
                return
            raise DefinitionNotFoundError("authorization metadata lookup is unavailable")
        dataset_id = (
            definition.dataset.resource_id
            if isinstance(definition, CaseDefinition)
            else definition.resource_id
        )
        metadata = await lookup(authorization.authorization_record_id, dataset_id)
        if (
            metadata is None
            or metadata.revoked
            or metadata.status not in {"authorized", "available"}
            or metadata.status == "authorized" and metadata.authorized_at is None
        ):
            raise DefinitionNotFoundError("evaluation authorization is unavailable")
        if (
            metadata.dataset_id != dataset_id
            or metadata.authorization_record_id != authorization.authorization_record_id
            or metadata.capture_classification != authorization.capture_classification.value
            or metadata.authorization_purpose != authorization.authorization_purpose
            or metadata.authorization_context != authorization.authorization_context
            or metadata.retention_mode != "manual_only"
            or (
                metadata.authorized_at is not None
                and authorization.authorized_at is not None
                and metadata.authorized_at.astimezone(UTC)
                != authorization.authorized_at.astimezone(UTC)
            )
        ):
            raise DefinitionValidationError("evaluation authorization is incompatible")


def _iso(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    rendered = normalized.isoformat(timespec="microseconds")
    rendered = rendered[:-6]  # remove the single ``+00:00`` suffix
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered + "Z"


def _duration_ms(started: float) -> float:
    return max(0.0, (monotonic() - started) * 1000.0)


__all__ = [
    "CaptureAuthorization",
    "CaptureClassification",
    "CaseDefinition",
    "CaseExpectations",
    "ComponentBinding",
    "DatasetDefinition",
    "DefinitionConflictError",
    "DefinitionNotFoundError",
    "DefinitionStorageError",
    "EvaluationDefinitionError",
    "DefinitionValidationError",
    "EvaluatorDefinition",
    "EvaluationDefinitionService",
    "EvaluatorReference",
    "ExecutionLimits",
    "ManualThreshold",
    "ResourceProvenance",
    "ResourceReference",
    "SourceKind",
    "SuiteDefinition",
]

EvaluationDefinitionError = DefinitionValidationError
