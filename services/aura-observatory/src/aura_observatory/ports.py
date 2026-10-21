"""Narrow storage ports owned by Observatory."""
# ruff: noqa: E501
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from aura_observatory.contract import MetadataObservation

if TYPE_CHECKING:
    from aura_observatory.evaluation_definitions import (
        CaseDefinition,
        DatasetDefinition,
        DefinitionCommandClaim,
        EvaluatorDefinition,
        SuiteDefinition,
    )
    from aura_observatory.evaluation_judges import JudgeArtifactReference, JudgeResponse
    from aura_observatory.evaluation_storage import (
        ArtifactRecord,
        ArtifactUpload,
        DeletionCommand,
        DeletionSelector,
        EvaluationScore,
    )


@dataclass(frozen=True, slots=True)
class EvaluationArtifactMetadata:
    """Content-free authorization metadata used during case admission."""

    artifact_reference_id: str
    dataset_id: str
    capture_classification: str
    authorization_record_id: str
    authorization_purpose: str
    authorization_context: str
    retention_mode: str
    sha256: str
    status: str
    revoked: bool = False
    authorized_at: datetime | None = None
    # These are authoritative storage facts used by content-bearing workers;
    # they are never accepted from a transport request.
    artifact_version: str = "1.0.0"
    media_type: str = "text/plain"
    byte_count: int = 0
    media_classification: str = "evaluation_input"
    intended_use: str = "input"


# These defaults are part of the bounded analytics request contract.  They
# live beside the inward port so storage adapters can type the request without
# importing the application query implementation.
ANALYTICS_DEFAULT_MAX_POINTS = 200
ANALYTICS_DEFAULT_MAX_SERIES = 12


@dataclass(frozen=True, slots=True)
class AnalyticsRequest:
    """Transport-neutral, bounded request passed through the analytics port."""

    metric: str
    unit: str
    aggregation: str = "average"
    focus: str = "all"
    from_time: datetime | None = None
    to_time: datetime | None = None
    bucket: str = "auto"
    group_by: str = "none"
    filters: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    series_limit: int = ANALYTICS_DEFAULT_MAX_SERIES
    point_limit: int = ANALYTICS_DEFAULT_MAX_POINTS


class InsertOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class DeletionResult:
    observations: int
    receipts: int
    runs: int


class ClaimOutcome(StrEnum):
    ACQUIRED = "acquired"
    DUPLICATE = "duplicate"
    PENDING = "pending"


class ObservationStore(Protocol):
    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome: ...


class ObservationIdRegistry(Protocol):
    """Durable, atomic pending/completed state for stable observation IDs."""

    async def claim(self, observation_id: str) -> ClaimOutcome: ...

    async def complete(self, observation_id: str) -> None: ...

    async def commit_ingest_boundary(self, observation_id: str) -> None: ...

    async def next_ingest_sequence(self, observation_id: str) -> int: ...

    async def set_observed_at(self, observation_id: str, observed_at: datetime) -> None: ...

    async def mark_ingest_visible(self, sequence: int) -> None: ...

    async def abort(self, observation_id: str) -> None: ...


class ObservatoryCatalog(Protocol):
    async def record_component(self, component_id: str, version: str) -> None: ...

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None: ...

    async def increment_counter(self, name: str, amount: int = 1) -> None: ...

    async def counters(self) -> Mapping[str, int]: ...

    async def last_ingest_at(self) -> datetime | None: ...



class ObservatoryHealthPort(Protocol):
    """Explicit narrow port for durable self-monitoring state and events."""

    async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None: ...
    async def record_rejection(self, reason: str, amount: int = 1) -> None: ...
    async def record_duplicate(self, amount: int = 1) -> None: ...
    async def record_storage_failure(self, amount: int = 1) -> None: ...
    async def record_latency(self, kind: str, duration_ms: float) -> None: ...
    async def record_worker_heartbeat(self, at: datetime | None = None) -> None: ...
    async def record_visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | None = None,
    ) -> None: ...
    async def record_query_freshness(self, observed_at: datetime | None) -> None: ...
    async def health_snapshot(self) -> dict[str, object] | None: ...
    async def save_health_snapshot(self, snapshot: dict[str, object]) -> None: ...
    async def health_history(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]: ...
    async def health_events_after(
        self, last_event_id: str | None, upper_sequence: int | None = None
    ) -> list[dict[str, object]]: ...
    async def health_event_sequence(self, event_id: str) -> int | None: ...
    async def latest_health_event_sequence(self) -> int: ...
    async def metric_series(self) -> list[dict[str, object]]: ...


class EvaluationSchedulerPort(Protocol):
    """DB-only application port for deterministic schedule materialization."""

    async def materialize_due_schedules(
        self, *, now: datetime, limit: int, lease_token: str
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class EvaluationDispatchIntent:
    """Application-safe dispatch intent; providers choose wire subjects."""

    kind: Literal["request", "cancellation"]
    envelope: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EvaluationResultDelivery:
    """A validated result envelope and its provider-owned acknowledgement."""

    envelope: Mapping[str, object]
    acknowledge: Callable[[], Awaitable[None]]


class EvaluationTransportPort(Protocol):
    """Inward transport port with no provider subject or connection details."""

    async def publish(self, intent: EvaluationDispatchIntent, event_id: str) -> None: ...

    def results(self, stop: Any) -> AsyncIterator[EvaluationResultDelivery]: ...


class NullObservatoryHealthPort:
    """Explicit no-op health port for isolated unit compositions."""

    async def record_accepted_observation(self, observed_at: datetime, amount: int = 1) -> None:
        del observed_at, amount

    async def record_rejection(self, reason: str, amount: int = 1) -> None:
        del reason, amount

    async def record_duplicate(self, amount: int = 1) -> None:
        del amount

    async def record_storage_failure(self, amount: int = 1) -> None:
        del amount

    async def record_latency(self, kind: str, duration_ms: float) -> None:
        del kind, duration_ms

    async def record_worker_heartbeat(self, at: datetime | None = None) -> None:
        del at

    async def record_visibility(
        self,
        watermark: int,
        lag_count: int,
        backlog_count: int = 0,
        oldest_backlog_age_ms: float | None = None,
    ) -> None:
        del watermark, lag_count, backlog_count, oldest_backlog_age_ms

    async def record_query_freshness(self, observed_at: datetime | None) -> None:
        del observed_at

    async def health_snapshot(self) -> dict[str, object] | None:
        return None

    async def save_health_snapshot(self, snapshot: dict[str, object]) -> None:
        del snapshot

    async def health_history(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]:
        del cursor, from_time, to_time, limit
        return {"items": [], "nextCursor": None, "snapshotAt": None}

    async def health_events_after(
        self, last_event_id: str | None, upper_sequence: int | None = None
    ) -> list[dict[str, object]]:
        del last_event_id, upper_sequence
        return []

    async def health_event_sequence(self, event_id: str) -> int | None:
        del event_id
        return None

    async def latest_health_event_sequence(self) -> int:
        return 0

    async def metric_series(self) -> list[dict[str, object]]:
        return []


class ObservatoryQueryPort(Protocol):
    """Query-specific inward port; adapters must not expose table hydration."""

    async def overview_query(self) -> dict[str, object]: ...

    async def components_query(self) -> list[dict[str, object]]: ...

    async def component_collection_query(
        self,
        *,
        definition_state: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]: ...

    async def component_definition_query(
        self, component_id: str, component_version: str
    ) -> dict[str, object] | None: ...

    async def component_dependencies_query(
        self,
        component_id: str,
        component_version: str,
        *,
        depth: int = 1,
        max_nodes: int = 100,
    ) -> dict[str, object] | None: ...

    async def deployments_query(self) -> list[dict[str, object]]: ...

    async def run_query(
        self, filters: Any, *, cursor: str | None, limit: int
    ) -> dict[str, Any]: ...

    async def run_detail_query(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None: ...

    async def trace_detail_query(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None: ...

    def events_after_query(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, object]]: ...

    async def component_health_query(
        self,
        component_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        *,
        component_version: str | None = None,
        point_limit: int = 200,
    ) -> dict[str, object] | None: ...

    async def health_snapshot_query(self) -> dict[str, object] | None: ...

    async def health_history_query(
        self,
        *,
        cursor: str | None,
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> dict[str, object]: ...

    async def operational_analytics_query(self, request: AnalyticsRequest) -> dict[str, object]: ...

    async def run_comparison_query(
        self, left_run_id: str, right_run_id: str
    ) -> dict[str, object] | None: ...


class ObservatoryReferencePort(Protocol):
    """Small in-memory/reference seam used only by deterministic composition."""

    observations: dict[str, MetadataObservation]
    actions: dict[str, Any]
    event_sequence: dict[str, int]
    health_events: list[dict[str, object]]
    snapshot_sequence: int

    async def list_observations(self) -> list[MetadataObservation]: ...
    async def components(self) -> list[dict[str, object]]: ...
    async def deployments(self) -> list[dict[str, object]]: ...
    async def ingest_overview(self) -> dict[str, object]: ...
    async def wait_for_event(self, timeout: float) -> None: ...
    async def delete_observations(self, cutoff: datetime | None) -> DeletionResult: ...


class ObservatoryRetentionPort(Protocol):
    async def find_retention_by_key(self, key: str) -> Any | None: ...

    async def save_retention(self, action: Any) -> bool: ...

    async def enqueue_retention(self, action: Any) -> None: ...

    async def attach_retention(self, action: Any) -> None: ...

    async def get_retention(self, action_id: str) -> Any | None: ...

    async def pending_retentions(self) -> list[Any]: ...

    async def claim_pending_retention(self, action_id: str) -> Any | None: ...

    async def prepare_retention(self, action: Any) -> None: ...

    async def update_retention(self, action: Any) -> None: ...

    async def persisted_metric_series(self) -> list[dict[str, object]]: ...


class EvaluationObjectStore(Protocol):
    """Private worker-only object store addressed by opaque artifact UUIDs."""

    async def put_stream(
        self,
        artifact_reference_id: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> int: ...

    async def delete(self, artifact_reference_id: str) -> None: ...


class EvaluationArtifactRepository(Protocol):
    """Transactional metadata boundary for authorized evaluation artifacts."""

    async def reserve_artifact(self, request: ArtifactUpload) -> ArtifactRecord | None | bool: ...

    async def publish_artifact(self, artifact: ArtifactRecord) -> None: ...

    async def mark_artifact_failed(self, artifact_reference_id: str, failure_code: str) -> None: ...

    async def lookup_artifact_metadata(
        self, artifact_reference_id: str
    ) -> EvaluationArtifactMetadata | None: ...

    async def lookup_authorization_metadata(
        self, authorization_record_id: str, dataset_id: str
    ) -> EvaluationArtifactMetadata | None: ...

    async def create_deletion_command(
        self, command: DeletionCommand, *, score_ids: Sequence[str] = ()
    ) -> DeletionCommand: ...

    async def claim_deletion_command(self, command_id: str) -> str | None: ...

    async def renew_deletion_lease(self, command_id: str, lease_token: str) -> bool: ...

    async def mark_deletion_phase(
        self, command_id: str, phase: str, *, lease_token: str | None = None
    ) -> bool: ...

    async def pending_deletion_commands(self, *, limit: int = 16) -> list[DeletionCommand]: ...

    async def deletion_targets(self, command_id: str, *, limit: int) -> list[Mapping[str, str]]: ...

    async def deletion_score_targets(
        self, command_id: str, *, limit: int
    ) -> list[Mapping[str, str]]: ...

    async def mark_deletion_target(
        self, command_id: str, artifact_reference_id: str, *, lease_token: str | None = None
    ) -> bool: ...

    async def mark_deletion_score_target(
        self, command_id: str, score_id: str, *, lease_token: str | None = None
    ) -> bool: ...

    async def delete_artifact_metadata(
        self,
        artifact_reference_id: str,
        *,
        command_id: str | None = None,
        lease_token: str | None = None,
    ) -> bool: ...

    async def complete_deletion_command(
        self, command_id: str, deleted_count: int = 0, *, lease_token: str | None = None
    ) -> int | None: ...

    async def fail_deletion_command(
        self, command_id: str, failure_code: str, *, lease_token: str | None = None
    ) -> bool: ...


class EvaluationScoreStore(Protocol):
    """Metadata-safe score storage; implementations must have no content columns."""

    async def insert_score(self, score: EvaluationScore) -> None: ...

    async def delete_scores_for_artifact(self, artifact_reference_id: str) -> None: ...

    async def delete_scores_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime | None = None
    ) -> None: ...

    async def list_score_ids_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime, limit: int
    ) -> list[str]: ...

    async def delete_scores_for_ids(self, score_ids: Sequence[str]) -> None: ...



class ExperimentRegressionFactStore(Protocol):
    """Owner port for deleting derived regression facts by identity."""

    async def delete_regression_facts(
        self, regression_ids: Sequence[str],
        *, regression_keys: Sequence[tuple[str, str]] = (),
    ) -> None: ...


class EvaluationDefinitionRepository(Protocol):
    """Immutable Observatory-owned definitions and exact-version references."""

    async def create_dataset(self, definition: DatasetDefinition) -> DatasetDefinition: ...

    async def create_case(self, definition: CaseDefinition) -> CaseDefinition: ...

    async def create_evaluator(self, definition: EvaluatorDefinition) -> EvaluatorDefinition: ...

    async def create_suite(self, definition: SuiteDefinition) -> SuiteDefinition: ...

    async def begin_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        resource_id: str,
        resource_version: str,
    ) -> DefinitionCommandClaim: ...

    async def complete_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        response: Mapping[str, object],
        claim_token: str,
    ) -> dict[str, object]: ...

    async def renew_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        claim_token: str,
    ) -> bool: ...

    async def get_dataset(self, resource_id: str, version: str) -> DatasetDefinition | None: ...

    async def get_case(self, resource_id: str, version: str) -> CaseDefinition | None: ...

    async def get_evaluator(self, resource_id: str, version: str) -> EvaluatorDefinition | None: ...

    async def get_suite(self, resource_id: str, version: str) -> SuiteDefinition | None: ...

    async def component_exists(self, component_id: str, version: str) -> bool: ...

    async def dataset_case_membership_complete(self, dataset_id: str, version: str) -> bool: ...

    async def case_declared(self, dataset_id: str, version: str, case_id: str) -> bool: ...

    async def case_selection_is_valid(
        self, dataset_id: str, version: str, case_ids: tuple[str, ...]
    ) -> bool: ...


class EvaluationResultReferencePort(Protocol):
    """Read-only result envelope boundary for experiment validation."""

    async def get_result_envelopes(
        self, result_ids: Sequence[str]
    ) -> list[tuple[str, Mapping[str, object]]]: ...

    async def result_ids_for_selector(self, selector: DeletionSelector) -> list[str]: ...


class EvaluationDeletionReferencePort(Protocol):
    """Read-only evaluation deletion scope owned by the artifact module."""

    async def result_ids_for_selector(self, selector: DeletionSelector) -> list[str]: ...


class EvaluationScoreReferencePort(Protocol):
    """Read-only score metadata boundary for comparison projections."""

    async def metric_values(
        self,
        result_ids: Sequence[str],
        metric: str,
        *,
        case_ids: Sequence[str] = (),
    ) -> tuple[float, ...] | None: ...

    async def score_metadata(
        self,
        result_ids: Sequence[str],
        metric: str,
        *,
        case_ids: Sequence[str] = (),
    ) -> list[Mapping[str, object]]: ...


class ExperimentRetentionPort(Protocol):
    """Public owner boundary for deleting experiment projections."""

    async def regression_projection_ids(
        self, selector: DeletionSelector, *, snapshot_at: datetime | None = None
    ) -> list[str]: ...

    async def fence_projection_deletion(self, selector: DeletionSelector) -> None: ...

    async def delete_for_selector(self, selector: DeletionSelector) -> int: ...

    async def delete_for_retention(
        self, *, cutoff: datetime | None, purge_all: bool,
        plan: ExperimentRetentionPlan | None = None,
    ) -> int: ...

    async def retention_projection_ids(
        self, *, cutoff: datetime | None, purge_all: bool
    ) -> list[str]: ...

    async def prepare_for_retention(
        self, *, cutoff: datetime | None, purge_all: bool
    ) -> ExperimentRetentionPlan: ...

    async def fence_projection_ids(self, regression_ids: Sequence[str]) -> None: ...

    async def fence_projection_keys(self, regression_keys: Sequence[tuple[str, str]]) -> None: ...


@dataclass(frozen=True, slots=True)
class ExperimentRetentionPlan:
    """Snapshot-stable identities carried through the multi-store delete."""

    cutoff: datetime | None
    purge_all: bool
    regression_keys: tuple[tuple[str, str], ...]
    baseline_keys: tuple[tuple[str, str], ...]
    experiment_keys: tuple[tuple[str, str], ...]
    attribution_keys: tuple[tuple[str, str], ...] = ()


class EvaluationOrchestrationPort(Protocol):
    """Application boundary for durable evaluation lifecycle operations."""

    async def admit(self, request: Any, *, now: datetime | None = None) -> Any: ...

    async def dispatch_once(self, *, now: datetime | None = None) -> int: ...

    async def publish_outbox_once(self, *, now: datetime | None = None) -> int: ...

    async def cancel(
        self, job_id: str, *, reason: str = "OWNER_REQUESTED", now: datetime | None = None
    ) -> bool: ...

    async def consume_result(
        self, envelope: Mapping[str, object], *, now: datetime | None = None
    ) -> str: ...


class LocalJudgePort(Protocol):
    """Narrow worker boundary for an admitted local model provider."""

    async def verify_model(
        self, model_name: str, model_digest: str, *, model_version: str
    ) -> None: ...

    async def generate(
        self,
        *,
        model_name: str,
        prompt: bytes,
        rubric: bytes,
        evidence: tuple[bytes, ...],
        timeout_seconds: float,
        max_output_bytes: int,
        max_output_tokens: int,
        context_tokens: int,
        seed: int,
    ) -> JudgeResponse: ...


class JudgeArtifactReadPort(Protocol):
    """Bounded, authorization-aware read of one opaque evaluation artifact."""

    async def read(
        self,
        reference: JudgeArtifactReference,
        *,
        max_bytes: int,
        intended_use: str | None = None,
    ) -> bytes: ...


class ObservatoryOperationsPort(Protocol):
    """Public inward boundary for owner-only operational resources."""

    async def create_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None) -> Mapping[str, object]: ...
    async def list_resources(self, resource_type: str, *, cursor: str | None = None, limit: int = 50, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> Mapping[str, object]: ...
    async def get_resource(self, resource_type: str, resource_id: str, version: str | None = None) -> Mapping[str, object] | None: ...
    async def replace_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None) -> Mapping[str, object]: ...
    async def delete_resource(self, resource_type: str, resource_id: str, version: str, *, command_id: str | None = None) -> None: ...
    async def events_after(self, last_event_id: str | None, *, limit: int = 1000) -> list[Mapping[str, object]]: ...


class ObservatoryAnnotationReferencePort(Protocol):
    """Explicit public reference validation for owner-authored annotations."""

    async def validate_annotation_target(self, target: Mapping[str, str]) -> None: ...


__all__ = [
    "ClaimOutcome",
    "DeletionResult",
    "InsertOutcome",
    "ObservationIdRegistry",
    "ObservationStore",
    "ObservatoryCatalog",
    "ObservatoryHealthPort",
    "NullObservatoryHealthPort",
    "ObservatoryQueryPort",
    "ObservatoryReferencePort",
    "ObservatoryRetentionPort",
    "EvaluationArtifactRepository",
    "EvaluationObjectStore",
    "EvaluationScoreStore",
    "ExperimentRegressionFactStore",
    "EvaluationDefinitionRepository",
    "EvaluationResultReferencePort",
    "EvaluationScoreReferencePort",
    "ExperimentRetentionPort",
    "ExperimentRetentionPlan",
    "EvaluationOrchestrationPort",
    "LocalJudgePort",
    "JudgeArtifactReadPort",
    "ObservatoryOperationsPort",
    "ObservatoryAnnotationReferencePort",
]
