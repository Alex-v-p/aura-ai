"""Versioned, closed metadata-observation contract.

This module intentionally contains no Core imports.  Validation is performed
before an observation reaches either Observatory store and extra fields are
never retained as a defensive privacy boundary.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_COMPONENT_ID = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_DIMENSION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_DIMENSION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
# Keep these two syntax expressions separate from the bounded, case-insensitive
# privacy guard below.  The JSON Schema/OpenAPI grammar permits upper-case
# revision values, but never private/content-like delimited segments.
_REVISION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MODEL_IDENTIFIER = re.compile(
    r"^(?!(?:https?|ftp|file|mailto|ssh|ws|wss):)"
    r"(?!(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)(?:$|[._:+-]))"
    r"(?!.*[._:+-](?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)(?:$|[._:+-]))"
    r"[a-z0-9][a-z0-9._+-]{0,127}"
    r"(?::(?=[a-z0-9._+-]{1,64}$)(?=[a-z0-9._+-]*[a-z])[a-z0-9._+-]+)?$"
)
_PRIVATE_MARKER_SEGMENT = re.compile(
    r"(?:^|[._:+-])"
    r"(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)"
    r"(?:$|[._:+-])",
    re.IGNORECASE,
)
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)

ServiceRole = Literal["api", "worker", "scheduler", "cli"]
MeasurementUnit = Literal["count", "milliseconds", "bytes", "ratio", "score"]
ObservationKind = Literal["metric", "span"]


class ContractViolation(ValueError):
    """Raised when a payload is valid JSON but violates the closed contract."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ComponentIdentity(_ClosedModel):
    id: str
    version: str

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _COMPONENT_ID.fullmatch(value):
            raise ValueError("component id is not a stable aura identifier")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("component version is not bounded")
        return value


class Measurement(_ClosedModel):
    name: str
    value: float = Field(ge=-1.0e15, le=1.0e15)
    unit: MeasurementUnit | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("measurement name is not bounded")
        return value

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("measurement must be finite")
        return value


class TraceLineage(_ClosedModel):
    trace_id: str = Field(alias="traceId")
    span_id: str | None = Field(default=None, alias="spanId")
    parent_span_id: str | None = Field(default=None, alias="parentSpanId")
    linked_span_ids: list[str] | None = Field(default=None, alias="linkedSpanIds")

    @field_validator("trace_id")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if not _TRACE_ID.fullmatch(value):
            raise ValueError("trace identifier must be 32 lowercase hexadecimal characters")
        return value

    @field_validator("span_id", "parent_span_id")
    @classmethod
    def valid_span(cls, value: str | None) -> str | None:
        if value is not None and not _SPAN_ID.fullmatch(value):
            raise ValueError("span identifier must be 16 lowercase hexadecimal characters")
        return value

    @field_validator("linked_span_ids")
    @classmethod
    def valid_linked_spans(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 8 or any(not _SPAN_ID.fullmatch(item) for item in value):
            raise ValueError("at most eight valid linked span identifiers are permitted")
        if len(set(value)) != len(value):
            raise ValueError("linked span identifiers must be unique")
        return value

    @model_validator(mode="after")
    def parent_requires_span(self) -> TraceLineage:
        if self.parent_span_id is not None and self.span_id is None:
            raise ValueError("parent span requires a span identifier")
        return self


class Correlation(_ClosedModel):
    run_id: str | None = Field(default=None, alias="runId")
    conversation_id: str | None = Field(default=None, alias="conversationId")

    @field_validator("run_id", "conversation_id")
    @classmethod
    def valid_correlation_id(cls, value: str | None) -> str | None:
        if value is not None and not _IDENTIFIER.fullmatch(value):
            raise ValueError("correlation identifiers must be UUIDs")
        return value

    @model_validator(mode="after")
    def requires_identifier(self) -> Correlation:
        if self.run_id is None and self.conversation_id is None:
            raise ValueError("correlation requires runId or conversationId")
        return self


class DeploymentMetadata(_ClosedModel):
    deployment_id: str = Field(alias="deploymentId")
    git_commit: str = Field(alias="gitCommit")
    environment: str
    host_id: str = Field(alias="hostId")

    @field_validator("deployment_id", "host_id")
    @classmethod
    def valid_metadata_id(cls, value: str) -> str:
        if not _SAFE_NAME.fullmatch(value):
            raise ValueError("deployment metadata identifier is not bounded")
        return value

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: str) -> str:
        if not _ENVIRONMENT.fullmatch(value):
            raise ValueError("deployment environment is not bounded")
        return value

    @field_validator("git_commit")
    @classmethod
    def valid_commit(cls, value: str) -> str:
        if not _COMMIT.fullmatch(value):
            raise ValueError("deployment commit must be a bounded git identifier")
        return value


class RevisionIdentifier(_ClosedModel):
    """Trusted, bounded revision marker carried only by metadata v2."""

    value: str

    @field_validator("value")
    @classmethod
    def valid_value(cls, value: str) -> str:
        if not is_revision_identifier(value):
            raise ValueError("revision identifier is not bounded")
        return value


class DeploymentMetadataV2(DeploymentMetadata):
    """Deployment metadata plus the two approved process revision markers."""

    image_version: str | None = Field(default=None, alias="imageVersion")
    configuration_version: str | None = Field(default=None, alias="configurationVersion")

    @field_validator("image_version", "configuration_version")
    @classmethod
    def valid_revision(cls, value: str | None) -> str | None:
        if value is not None and not is_revision_identifier(value):
            raise ValueError("deployment revision is not bounded")
        return value


class ExecutionMetadata(_ClosedModel):
    """Closed execution provenance. Values are never prompt/content hashes."""

    model_id: str | None = Field(default=None, alias="modelId")
    model_version: str | None = Field(default=None, alias="modelVersion")
    agent_revision: str | None = Field(default=None, alias="agentRevision")
    prompt_bundle_revision: str | None = Field(default=None, alias="promptBundleRevision")
    policy_revision: str | None = Field(default=None, alias="policyRevision")

    @model_validator(mode="after")
    def require_marker(self) -> ExecutionMetadata:
        if not any(
            value is not None
            for value in (
                self.model_id,
                self.model_version,
                self.agent_revision,
                self.prompt_bundle_revision,
                self.policy_revision,
            )
        ):
            raise ValueError("execution metadata requires a marker")
        return self

    @field_validator(
        "model_version",
        "agent_revision",
        "prompt_bundle_revision",
        "policy_revision",
    )
    @classmethod
    def valid_marker(cls, value: str | None) -> str | None:
        if value is not None and not is_revision_identifier(value):
            raise ValueError("execution marker is not bounded")
        return value

    @field_validator("model_id")
    @classmethod
    def valid_model_id(cls, value: str | None) -> str | None:
        if value is not None and not is_model_identifier(value):
            raise ValueError("model identifier is not a bounded local model identifier")
        return value


class MetadataObservation(_ClosedModel):
    """A single content-free operational measurement."""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    observation_id: str = Field(alias="observationId")
    observed_at: datetime = Field(alias="observedAt")
    kind: ObservationKind
    component: ComponentIdentity
    operation: str
    measurement: Measurement
    dimensions: dict[str, str]
    trace: TraceLineage | None = None
    correlation: Correlation | None = None
    deployment: DeploymentMetadata
    service_role: ServiceRole = Field(alias="serviceRole")
    capture_policy: Literal["metadata_only"] = Field(alias="capturePolicy")

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, payload: Any) -> Any:
        _reject_explicit_nulls(payload)
        return payload

    @field_validator("observation_id")
    @classmethod
    def valid_observation_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("observation id is not a stable identifier")
        return value

    @field_validator("operation")
    @classmethod
    def valid_operation(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("operation is not bounded")
        return value

    @field_validator("dimensions")
    @classmethod
    def valid_dimensions(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 11:
            raise ValueError("at most 11 dimensions are permitted")
        for name, dimension in value.items():
            if not _DIMENSION_NAME.fullmatch(name):
                raise ValueError("dimension name is not bounded")
            if not _DIMENSION_VALUE.fullmatch(dimension):
                raise ValueError("dimension values must be bounded")
            # Explicitly reject names that could carry user/content data.  The
            # closed schema already excludes arbitrary keys; this catches
            # common sensitive aliases even if a future schema adds a key.
            if name not in {
                "archiveState",
                "dependency",
                "errorClass",
                "outcome",
                "provider",
                "status",
                "tokenEstimator",
                "scopeType",
                "retrievalStage",
                "degradation",
                "operation",
            }:
                raise ValueError("sensitive dimension is prohibited")
        return value

    @model_validator(mode="after")
    def enforce_metadata_only(self) -> MetadataObservation:
        if self.capture_policy != "metadata_only":
            raise ContractViolation("only metadata_only capture is accepted")
        if self.kind == "span" and (self.trace is None or self.trace.span_id is None):
            raise ContractViolation("span observations require trace and span identifiers")
        return self

    @field_validator("observed_at", mode="before")
    @classmethod
    def parse_wire_timestamp(cls, value: object) -> object:
        if isinstance(value, str):
            if not _TIMESTAMP.fullmatch(value):
                raise ValueError("observedAt must be a canonical ISO-8601 timestamp")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("observedAt must be an ISO-8601 timestamp") from exc
            if parsed.tzinfo is None:
                raise ValueError("observedAt must include a timezone")
            return parsed
        return value

    def to_payload(self) -> dict[str, Any]:
        """Serialize using the wire contract's camelCase names."""

        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def from_payload(cls, payload: Any) -> MetadataObservation:
        # Accept one standard OTLP JSON point at this public boundary and
        # immediately normalize it into the closed metadata contract.
        if isinstance(payload, Mapping):
            if "resourceMetrics" in payload:
                from aura_observatory.otlp import metrics_to_observations

                records = metrics_to_observations(cast(object, payload))
                if len(records) != 1:
                    raise ContractViolation("exactly one OTLP observation is required")
                payload = records[0]
            elif "resourceSpans" in payload:
                from aura_observatory.otlp import traces_to_observations

                records = traces_to_observations(cast(object, payload))
                if len(records) != 1:
                    raise ContractViolation("exactly one OTLP observation is required")
                payload = records[0]
        if (
            isinstance(payload, Mapping)
            and cast(Mapping[str, object], payload).get("schemaVersion") == 2
        ):
            return MetadataObservationV2.model_validate(payload)
        return cls.model_validate(payload)


class MetadataObservationV2(MetadataObservation):
    """Version two observation; v1 remains a separately closed model."""

    schema_version: Literal[2] = Field(alias="schemaVersion")  # type: ignore[assignment]
    deployment: DeploymentMetadataV2  # type: ignore[assignment]
    execution: ExecutionMetadata | None = None


def _reject_explicit_nulls(payload: Any) -> None:
    """Reject JSON nulls where the schema permits omission but not null."""

    if not isinstance(payload, Mapping):
        return
    root = cast(Mapping[str, object], payload)
    for field in ("trace", "correlation"):
        if field in root and root[field] is None:
            raise ContractViolation(f"{field} may be omitted but not null")
    measurement = root.get("measurement")
    if isinstance(measurement, Mapping) and "unit" in measurement and measurement["unit"] is None:
        raise ContractViolation("measurement.unit may be omitted but not null")
    trace = root.get("trace")
    if isinstance(trace, Mapping):
        for field in ("spanId", "parentSpanId", "linkedSpanIds"):
            if field in trace and trace[field] is None:
                raise ContractViolation(f"trace.{field} may be omitted but not null")
    correlation = root.get("correlation")
    if isinstance(correlation, Mapping):
        for field in ("runId", "conversationId"):
            if field in correlation and correlation[field] is None:
                raise ContractViolation(f"correlation.{field} may be omitted but not null")


__all__ = [
    "ComponentIdentity",
    "Correlation",
    "ContractViolation",
    "DeploymentMetadata",
    "DeploymentMetadataV2",
    "ExecutionMetadata",
    "Measurement",
    "MetadataObservation",
    "MetadataObservationV2",
    "RevisionIdentifier",
    "ServiceRole",
    "TraceLineage",
    "is_revision_identifier",
    "is_model_identifier",
]


def is_revision_identifier(value: str) -> bool:
    """Return whether a value matches the closed metadata revision grammar."""

    return (
        _REVISION_IDENTIFIER.fullmatch(value) is not None
        and _PRIVATE_MARKER_SEGMENT.search(value) is None
    )


def is_model_identifier(value: str) -> bool:
    """Return whether a value matches the closed metadata model-id grammar."""

    return (
        _MODEL_IDENTIFIER.fullmatch(value) is not None
        and _PRIVATE_MARKER_SEGMENT.search(value) is None
    )
