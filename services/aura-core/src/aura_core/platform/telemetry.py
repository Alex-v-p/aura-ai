"""Bounded metadata-only telemetry and structured container-log export.

The synchronous recording path performs no I/O. Records are retained in a
bounded ring and exported by a periodic lifecycle task. Capacity pressure drops
the oldest record and counts it. Export failures retain the batch for a bounded
number of attempts; an exhausted batch is dropped and counted so telemetry can
never block or exhaust the application.

Only declared dimensions and trace fields can be represented. Prompts,
responses, provider payloads, endpoints, credentials, and token values have no
accepted field and therefore cannot reach the exporter.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID

COMPONENT_VERSIONS: Mapping[str, str] = {
    "aura.interaction.agent_configuration": "1.1.0",
    "aura.execution.run_coordinator": "1.4.0",
    "aura.interaction.conversation_persistence": "1.6.0",
    "aura.runtime.model_inference": "1.4.0",
    "aura.runtime.model_routing": "1.2.0",
    "aura.runtime.prompt_compilation": "1.1.0",
    "aura.runtime.stream_delivery": "1.2.0",
    "aura.knowledge.memory_persistence": "1.0.0",
    "aura.knowledge.memory_extraction": "1.2.0",
    "aura.knowledge.memory_maintenance": "1.0.0",
    "aura.runtime.structured_inference": "1.1.0",
    "aura.runtime.embedding_gateway": "1.0.0",
    "aura.knowledge.memory_retrieval": "1.1.0",
}

_METRICS: Mapping[str, frozenset[str]] = {
    "aura.interaction.agent_configuration": frozenset(
        {
            "configuration_outcome",
            "operation_duration_ms",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
        }
    ),
    "aura.execution.run_coordinator": frozenset(
        {
            "cancellations",
            "consumer_acknowledged",
            "consumer_received",
            "errors",
            "lease_expired",
            "outbox_dispatched",
            "outbox_duplicates",
            "operation_duration_ms",
            "queue_wait_ms",
            "retries",
            "run_duration_ms",
            "run_started",
            "run_status",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {
            "checkpoint_persisted",
            "conversation_list_duration_ms",
            "conversation_metadata_mutation_duration_ms",
            "conversation_filter_outcome",
            "conversation_archive_outcome",
            "errors",
            "model_selection_persisted",
            "persistence_duration_ms",
            "title_persistence_duration_ms",
        }
    ),
    "aura.runtime.model_inference": frozenset(
        {
            "errors",
            "inference_duration_ms",
            "output_tokens",
            "provider_errors",
            "title_generation_outcome",
            "title_inference_duration_ms",
            "title_input_size",
            "title_output_size",
            "time_to_first_token_ms",
        }
    ),
    "aura.runtime.model_routing": frozenset(
        {"errors", "model_routed", "model_routing_duration_ms", "model_selection_persisted"}
    ),
    "aura.runtime.prompt_compilation": frozenset(
        {
            "compiled_prompt_size",
            "operation_duration_ms",
            "prompt_compile_duration_ms",
            "prompt_component_count",
        }
    ),
    "aura.runtime.stream_delivery": frozenset(
        {
            "errors",
            "events_delivered",
            "memory_activity_publication_duration_ms",
            "memory_activity_publication_outcome",
            "memory_activity_delivery_duration_ms",
            "memory_activity_delivery_outcome",
            "sse_connections",
            "sse_reconnects",
            "stream_delivery_duration_ms",
        }
    ),
    "aura.knowledge.memory_persistence": frozenset(
        {
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
            "memory_operation_duration_ms",
            "memory_operation_outcome",
            "memory_lifecycle_status",
            "memory_scope_type",
        }
    ),
    "aura.knowledge.memory_extraction": frozenset({
        "memory_job_outcome", "memory_candidate_outcome", "memory_action_outcome",
        "memory_extraction_outcome", "memory_candidate_decision",
        "memory_personal_retention_gate_outcome", "memory_reviewable_candidate_count",
        "memory_fallback_outcome",
        "structured_inference_duration_ms", "queue_wait_ms", "retry_count", "backlog", "errors",
        "provider_errors", "duration_ms", "backlog_depth",
        "memory_extraction_duration_ms", "memory_job_duration_ms", "memory_job_queue_wait_ms",
        "memory_policy_duration_ms", "memory_retention_gate_duration_ms",
        "memory_processing_errors", "memory_retry_count",
        "missing_embedding_backlog", "embedding_duration_ms", "consumer_received",
        "consumer_acknowledged",
        "consumer_redelivered", "outbox_duplicates", "consumer_nacked", "consumer_rejected",
        "operation_duration_ms",
        "memory_candidate_review_duration_ms", "memory_candidate_review_outcome",
    }),
    "aura.knowledge.memory_maintenance": frozenset({
        "memory_maintenance_transition", "maintenance_duration_ms", "retry_count", "backlog",
        "errors", "duration_ms", "backlog_depth",
        "memory_maintenance_duration_ms", "memory_reindex_backlog",
        "memory_reindex_chunk_duration_ms", "memory_reindex_progress",
        "memory_reindex_switch_duration_ms", "missing_embedding_backlog",
        "embedding_duration_ms", "memory_retry_count", "memory_processing_errors",
        "operation_duration_ms",
    }),
    "aura.runtime.structured_inference": frozenset({
        "structured_inference_duration_ms", "provider_errors", "errors", "duration_ms",
        "backlog_depth", "retry_count",
        "model_inventory_duration_ms", "model_inventory_outcome",
        "model_inventory_cache_outcome", "model_capability_verification_duration_ms",
        "model_capability_verification_outcome",
        "model_capability_verification_cache_outcome",
    }),
    "aura.runtime.embedding_gateway": frozenset({
        "embedding_duration_ms", "provider_errors", "errors", "retry_count", "backlog",
        "duration_ms", "backlog_depth",
    }),
    # Retrieval metrics intentionally contain no memory text, vectors, owner
    # subjects, or provider payloads.  Stage and scope are bounded dimensions;
    # identifiers are retained only as trace attributes where a caller opts in.
    "aura.knowledge.memory_retrieval": frozenset({
        "memory_retrieval_duration_ms",
        "memory_lexical_search_duration_ms",
        "memory_vector_search_duration_ms",
        "memory_rerank_duration_ms",
        "memory_query_embedding_duration_ms",
        "memory_retrieval_candidate_count",
        "memory_recall_count",
        "memory_recall_gate_outcome",
        "memory_fallback_outcome",
        "memory_context_tokens",
        "memory_retrieval_errors",
    }),
}

_SPAN_OPERATIONS: Mapping[str, frozenset[str]] = {
    "aura.interaction.agent_configuration": frozenset({"agent.configure"}),
    "aura.execution.run_coordinator": frozenset(
        {
            "consumer.delivery",
            "conversation.command",
            "lease.expire",
            "outbox.dispatch",
            "run.cancel",
            "run.execute",
            "run.retry",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {
            "checkpoint.persist",
            "conversation.list",
            "conversation.list.request",
            "conversation.metadata.request",
            "conversation.persist",
            "run.claim",
            "run.finish",
        }
    ),
    "aura.runtime.model_inference": frozenset({"model.infer"}),
    "aura.runtime.model_routing": frozenset({"model.route", "model.selection.persist"}),
    "aura.runtime.prompt_compilation": frozenset({"prompt.compile"}),
    "aura.runtime.stream_delivery": frozenset(
        {
            "event.publish",
            "memory.activity.publish",
            "memory.activity.reconcile",
            "memory.activity.replay",
            "memory.activity.cursor_expired",
            "sse.connect",
            "sse.deliver",
            "sse.reconnect",
        }
    ),
    "aura.knowledge.memory_persistence": frozenset(
        {
            "memory.persist",
            "memory.list",
            "memory.get",
            "memory.create",
            "memory.reinforce",
            "memory.revise",
            "memory.status",
            "memory.pin",
            "memory.purge",
            "memory.embedding.register",
            "memory.embedding.activate",
            "memory.embedding.attach",
            "memory.model.configure",
            "memory.model.get",
            "memory.job.enqueue",
            "memory.job.get",
            "memory.job.claim",
            "memory.job.claim_next",
            "memory.job.settle",
            "memory.candidate.persist",
            "memory.candidate.get",
            "memory.candidate.list",
            "memory.candidate.detail",
            "memory.candidate.approve",
            "memory.candidate.reject",
            "memory.retrieval.generation",
            "memory.retrieval.lexical",
            "memory.retrieval.vector",
            "memory.outcome.record",
            "memory.embedding.queue",
            "memory.embedding.settle",
        }
    ),
    "aura.knowledge.memory_extraction": frozenset({
        "memory.job", "memory.job.queue", "memory.extraction", "memory.extract",
        "memory.candidate", "memory.candidate.approve", "memory.candidate.reject",
        "memory.action", "memory.policy", "memory.retention_gate", "memory.embedding",
        "memory.fallback",
        "memory.retry", "memory.error",
    }),
    "aura.knowledge.memory_maintenance": frozenset({
        "memory.maintenance", "memory.decay", "memory.archive", "memory.reindex.chunk",
        "memory.reindex.switch", "memory.embedding", "memory.retry", "memory.error",
    }),
    "aura.runtime.structured_inference": frozenset({
        "structured.inference", "model.inventory", "model.capability.verify"
    }),
    "aura.runtime.embedding_gateway": frozenset({"embedding.generate", "embedding.retry"}),
    "aura.knowledge.memory_retrieval": frozenset({
        "memory.retrieval",
        "memory.query_embedding",
        "memory.lexical",
        "memory.vector",
        "memory.fusion",
        "memory.rerank",
        "memory.selection",
        "memory.context_budget",
        "memory.fallback",
        "memory.degradation",
    }),
}

_SPAN_METRICS: Mapping[str, str] = {
    "aura.interaction.agent_configuration": "operation_duration_ms",
    "aura.execution.run_coordinator": "operation_duration_ms",
    "aura.interaction.conversation_persistence": "persistence_duration_ms",
    "aura.runtime.model_inference": "inference_duration_ms",
    "aura.runtime.model_routing": "model_routing_duration_ms",
    "aura.runtime.prompt_compilation": "prompt_compile_duration_ms",
    "aura.runtime.stream_delivery": "stream_delivery_duration_ms",
    "aura.knowledge.memory_persistence": "memory_operation_duration_ms",
    "aura.knowledge.memory_extraction": "operation_duration_ms",
    "aura.knowledge.memory_maintenance": "operation_duration_ms",
    "aura.runtime.structured_inference": "structured_inference_duration_ms",
    "aura.runtime.embedding_gateway": "embedding_duration_ms",
}

_OPERATION_SPAN_METRICS: Mapping[tuple[str, str], str] = {
    ("aura.runtime.stream_delivery", "memory.activity.publish"):
        "memory_activity_publication_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.job.queue"): "memory_job_queue_wait_ms",
    ("aura.knowledge.memory_extraction", "memory.job"): "memory_job_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.extraction"): "memory_extraction_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.extract"): "memory_extraction_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.policy"): "memory_policy_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.retention_gate"):
        "memory_retention_gate_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.fallback"):
        "operation_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.maintenance"):
        "memory_maintenance_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.reindex.chunk"):
        "memory_reindex_chunk_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.reindex.switch"):
        "memory_reindex_switch_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.embedding"):
        "embedding_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.retrieval"):
        "memory_retrieval_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.lexical"):
        "memory_lexical_search_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.vector"):
        "memory_vector_search_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.rerank"):
        "memory_rerank_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.query_embedding"):
        "memory_query_embedding_duration_ms",
    ("aura.runtime.structured_inference", "model.inventory"):
        "model_inventory_duration_ms",
    ("aura.runtime.structured_inference", "model.capability.verify"):
        "model_capability_verification_duration_ms",
}

# Dimensions are suitable for metric aggregation and intentionally exclude all
# identifiers. Trace attributes may be high-cardinality but are never labels.
_DIMENSION_KEYS = frozenset(
    {
        "archive_state",
        "dependency",
        "error_class",
        "outcome",
        "provider",
        "status",
        "token_estimator",
        "scope_type",
        "retrieval_stage",
        "degradation",
    }
)
_TRACE_ATTRIBUTE_KEYS = frozenset(
    {
        "agent_revision_id",
        "agent_revision_number",
        "profile_id",
        "persona_revision_number",
        "persona_revision_id",
        "prompt_bundle_revision_id",
        "prompt_hash",
        "prompt_component_count",
        "compiled_prompt_size",
        "configuration_outcome",
        "configuration_source",
        "configuration_reason",
        "result_count",
        "memory_id",
        "memory_revision_id",
        "generation_id",
        "job_id",
        "operation_duration_ms",
        "attempt_id",
        "attempt_count",
        "causation_id",
        "command_id",
        "conversation_id",
        "correlation_id",
        "delivery_count",
        "assistant_message_id",
        "model_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
        "user_message_id",
        "title_input_size",
        "title_output_size",
        "memory_policy_revision_id",
        "embedding_generation_id",
        "retrieval_version",
        "selection_order",
        "candidate_count",
        "recalled_count",
        "context_tokens",
        "fallback_grant_id",
        "recall_mode",
        "gate_outcome",
    }
)
_UUID_TRACE_ATTRIBUTES = frozenset(
    {
        "agent_revision_id",
        "profile_id",
        "persona_revision_id",
        "prompt_bundle_revision_id",
        "attempt_id",
        "causation_id",
        "command_id",
        "conversation_id",
        "correlation_id",
        "assistant_message_id",
        "memory_id",
        "memory_revision_id",
        "generation_id",
        "job_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
        "user_message_id",
        "memory_policy_revision_id",
        "embedding_generation_id",
        "fallback_grant_id",
    }
)
_COUNT_TRACE_ATTRIBUTES = frozenset(
    {
        "attempt_count",
        "delivery_count",
        "prompt_component_count",
        "compiled_prompt_size",
        "operation_duration_ms",
        "agent_revision_number",
        "persona_revision_number",
        "title_input_size",
        "title_output_size",
        "result_count",
        "selection_order",
        "candidate_count",
        "recalled_count",
        "context_tokens",
    }
)
_ENUM_DIMENSIONS: Mapping[str, frozenset[str]] = {
    "dependency": frozenset(
        {
            "conversation_store",
            "event_store",
            "model_policy",
            "model_provider",
            "nats_jetstream",
            "postgresql",
            "sse_client",
            "configuration_store",
            "prompt_compiler",
            "memory_store",
            "structured_inference",
            "embedding_provider",
            "query_embedding",
            "memory_worker",
        }
    ),
    "archive_state": frozenset({"active", "archived", "all", "unknown"}),
    "error_class": frozenset(
        {
            "authorization",
            "cancel",
            "conflict",
            "delivery",
            "disabled",
            "idempotency",
            "not_found",
            "persistence",
            "provider",
            "generation",
            "purged",
            "queue",
            "timeout",
            "validation",
            "query_embedding",
            "database",
            "telemetry",
            "budget",
            "scope",
            "lifecycle",
            "dimension",
            "identity_mismatch",
            "cursor_expired",
            "run_not_found",
            "serialization",
            "unknown",
        }
    ),
    "outcome": frozenset(
        {
            "accepted", "canceled", "duplicate", "error", "fallback", "generated", "ignored",
            "ok", "retryable", "review", "skipped", "used", "not_needed", "hit", "miss",
            "denied", "degraded", "truncated", "empty", "not_granted", "not_found",
            "expired", "unknown",
            "policy_off", "selected", "rejected", "personal", "explicit_request", "none",
        }
    ),
    "status": frozenset(
        {
            "canceled",
            "cancel_requested",
            "completed",
            "failed",
            "interrupted",
            "queued",
            "running",
            "active",
            "dormant",
            "archived",
            "disabled",
            "disputed",
            "superseded",
            "unknown",
            "retryable",
        }
    ),
    "token_estimator": frozenset({"chars_div_4_ceil"}),
    "scope_type": frozenset({
        "user", "agent", "owner", "shared_user", "current_agent",
        "foreign_agent", "unknown",
    }),
    "retrieval_stage": frozenset({
        "primary", "query_embedding", "lexical", "vector", "fusion", "rerank", "selection",
        "context_budget", "fallback", "degradation", "unknown",
    }),
    "degradation": frozenset({"none", "query_embedding", "database", "telemetry", "budget"}),
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Measurement:
    kind: str
    component_id: str
    component_version: str
    metric: str
    value: float
    observed_at: datetime
    dimensions: tuple[tuple[str, str], ...] = ()
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    link_span_ids: tuple[str, ...] = ()
    trace_attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryTraceContext:
    """Metadata-only context shared by one asynchronous memory command."""

    trace_id: str
    span_id: str
    command_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    agent_revision_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    memory_id: str | None = None
    memory_revision_id: str | None = None
    generation_id: str | None = None
    link_span_ids: tuple[str, ...] = ()

    def attributes(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "command_id": self.command_id,
                "job_id": self.job_id,
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "agent_revision_id": self.agent_revision_id,
                "user_message_id": self.user_message_id,
                "assistant_message_id": self.assistant_message_id,
                "memory_id": self.memory_id,
                "memory_revision_id": self.memory_revision_id,
                "generation_id": self.generation_id,
            }.items()
            if value is not None
        }


_MEMORY_TRACE_CONTEXT: ContextVar[MemoryTraceContext | None] = ContextVar(
    "aura_memory_trace_context", default=None
)


@contextmanager
def memory_trace_context(context: MemoryTraceContext):
    """Propagate one command trace across async Core/provider call sites."""

    token = _MEMORY_TRACE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _MEMORY_TRACE_CONTEXT.reset(token)


def current_memory_trace_context() -> MemoryTraceContext | None:
    """Return the active memory context without exposing content."""

    return _MEMORY_TRACE_CONTEXT.get()


@dataclass(frozen=True, slots=True)
class TelemetryStats:
    retained: int
    capacity: int
    dropped: int
    rejected: int
    export_failures: int
    abandoned_batches: int
    exporter_configured: bool


@dataclass(frozen=True, slots=True)
class TelemetryBatch:
    measurements: tuple[Measurement, ...]
    stats: TelemetryStats


class TelemetryExporter(Protocol):
    async def export(self, batch: TelemetryBatch) -> None: ...


class StructuredContainerLogExporter:
    """Write metadata-only JSON lines to stdout for the container log driver."""

    def __init__(self, writer: Callable[[str], None] | None = None) -> None:
        self._writer = writer or _write_stdout

    async def export(self, batch: TelemetryBatch) -> None:
        lines = [self._measurement_line(item) for item in batch.measurements]
        lines.append(self._health_line(batch.stats))
        # Export runs only on the background lifecycle task; the application
        # recording path never performs this container-log I/O.
        self._write_lines(lines)
        await asyncio.sleep(0)

    def _write_lines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self._writer(line)

    @staticmethod
    def _measurement_line(item: Measurement) -> str:
        trace: dict[str, object] = {}
        if item.trace_id is not None:
            trace["trace_id"] = item.trace_id
        if item.span_id is not None:
            trace["span_id"] = item.span_id
        if item.parent_span_id is not None:
            trace["parent_span_id"] = item.parent_span_id
        if item.link_span_ids:
            trace["link_span_ids"] = item.link_span_ids
        if item.trace_attributes:
            trace["attributes"] = dict(item.trace_attributes)
        record: dict[str, object] = {
            "capture_policy": "metadata_only",
            "component_id": item.component_id,
            "component_version": item.component_version,
            "dimensions": dict(item.dimensions),
            "event": "aura.telemetry.measurement",
            "kind": item.kind,
            "metric": item.metric,
            "observed_at": item.observed_at.isoformat(),
            "value": item.value,
        }
        if trace:
            record["trace"] = trace
        return json.dumps(record, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _health_line(stats: TelemetryStats) -> str:
        return json.dumps(
            {
                "abandoned_batches": stats.abandoned_batches,
                "capture_policy": "metadata_only",
                "dropped": stats.dropped,
                "event": "aura.telemetry.health",
                "export_failures": stats.export_failures,
                "rejected": stats.rejected,
                "retained": stats.retained,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class MetadataMetrics:
    """Nonblocking bounded sink with metric/trace cardinality separation."""

    def __init__(
        self,
        *,
        capacity: int = 2048,
        exporter: TelemetryExporter | None = None,
        max_export_attempts: int = 3,
    ) -> None:
        if capacity < 1:
            raise ValueError("telemetry capacity must be positive")
        if max_export_attempts < 1:
            raise ValueError("telemetry export attempts must be positive")
        self._capacity = capacity
        self._measurements: deque[Measurement] = deque()
        self._exporter = exporter
        self._max_export_attempts = max_export_attempts
        self._current_export_attempts = 0
        self._dropped = 0
        self._rejected = 0
        self._export_failures = 0
        self._abandoned_batches = 0

    @property
    def exporter(self) -> TelemetryExporter | None:
        return self._exporter

    def increment(
        self,
        component: str,
        name: str,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            1.0,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def observe(
        self,
        component: str,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            value,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        retrieval_stage: str | None = None,
        scope_type: str | None = None,
        degradation: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        metric_name: str | None = None,
        link_span_ids: Sequence[str] = (),
        **trace_attributes: str,
    ) -> None:
        memory_context = current_memory_trace_context()
        if memory_context is not None and component in {
            "aura.knowledge.memory_extraction",
            "aura.knowledge.memory_maintenance",
            "aura.runtime.structured_inference",
            "aura.runtime.embedding_gateway",
            "aura.knowledge.memory_persistence",
            "aura.knowledge.memory_retrieval",
        }:
            trace_id = memory_context.trace_id
            parent_span_id = parent_span_id or memory_context.span_id
            trace_attributes = {**trace_attributes, **memory_context.attributes()}
            link_span_ids = (*memory_context.link_span_ids, *link_span_ids)
        if operation not in _SPAN_OPERATIONS.get(component, frozenset()):
            self._rejected += 1
            return
        if component == "aura.runtime.prompt_compilation":
            dependency = "prompt_compiler"
        dimensions = {"dependency": dependency, "outcome": outcome}
        if error_class is not None:
            dimensions["error_class"] = error_class
        if provider is not None:
            dimensions["provider"] = provider
        if retrieval_stage is not None:
            dimensions["retrieval_stage"] = retrieval_stage
        if scope_type is not None:
            dimensions["scope_type"] = scope_type
        if degradation is not None:
            dimensions["degradation"] = degradation
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        links = tuple(dict.fromkeys(link_span_ids))
        if (
            normalized is None
            or not _valid_span_id(span_id)
            or parent_span_id is not None
            and not _valid_span_id(parent_span_id)
            or any(not _valid_span_id(link) for link in links)
        ):
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="span",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=metric_name
                or _OPERATION_SPAN_METRICS.get((component, operation), _SPAN_METRICS[component]),
                value=float(duration_ms),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                link_span_ids=links,
                trace_attributes=(("operation", operation), *normalized[1]),
            )
        )
        if component == "aura.runtime.prompt_compilation":
            trace_values = dict(normalized[1])
            for metric, attribute in (
                ("prompt_component_count", "prompt_component_count"),
                ("compiled_prompt_size", "compiled_prompt_size"),
            ):
                value = trace_values.get(attribute)
                if value is not None:
                    self._record_metric(
                        component,
                        metric,
                        float(value),
                        trace_id=trace_id,
                        run_id=run_id,
                        conversation_id=conversation_id,
                        attributes={},
                    )

    def _record_metric(
        self,
        component: str,
        metric: str,
        value: float,
        *,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        attributes: Mapping[str, str],
    ) -> None:
        memory_context = current_memory_trace_context()
        if memory_context is not None and component in {
            "aura.knowledge.memory_extraction",
            "aura.knowledge.memory_maintenance",
            "aura.runtime.structured_inference",
            "aura.runtime.embedding_gateway",
            "aura.knowledge.memory_persistence",
            "aura.knowledge.memory_retrieval",
        }:
            trace_id = memory_context.trace_id
            run_id = memory_context.run_id or run_id
            conversation_id = memory_context.conversation_id or conversation_id
            attributes = {**attributes, **memory_context.attributes()}
        if metric not in _METRICS.get(component, frozenset()) or not math.isfinite(value):
            self._rejected += 1
            return
        dimensions = {key: value for key, value in attributes.items() if key in _DIMENSION_KEYS}
        trace_attributes = {
            key: value for key, value in attributes.items() if key in _TRACE_ATTRIBUTE_KEYS
        }
        if len(dimensions) + len(trace_attributes) != len(attributes):
            self._rejected += 1
            return
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        if normalized is None:
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="metric",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=metric,
                value=float(value),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                trace_attributes=normalized[1],
            )
        )

    def _normalize(
        self,
        component: str,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        dimensions: Mapping[str, str],
        trace_attributes: Mapping[str, str],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]] | None:
        if component not in COMPONENT_VERSIONS:
            return None
        if trace_id is not None and not _valid_trace_id(trace_id):
            return None
        complete_trace_attributes = dict(trace_attributes)
        if run_id is not None:
            complete_trace_attributes["run_id"] = run_id
        if conversation_id is not None:
            complete_trace_attributes["conversation_id"] = conversation_id
        normalized_dimensions: list[tuple[str, str]] = []
        for key, value in dimensions.items():
            if key not in _DIMENSION_KEYS or _looks_like_uuid(value):
                return None
            allowed = _ENUM_DIMENSIONS.get(key)
            if allowed is not None and value not in allowed:
                return None
            if key == "provider" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            normalized_dimensions.append((key, value))
        normalized_trace: list[tuple[str, str]] = []
        for key, value in complete_trace_attributes.items():
            if key not in _TRACE_ATTRIBUTE_KEYS:
                return None
            if key in _UUID_TRACE_ATTRIBUTES and not _valid_uuid(value):
                return None
            if key in _COUNT_TRACE_ATTRIBUTES and key != "operation_duration_ms" and (
                not value.isascii() or not value.isdecimal()
            ):
                return None
            if key == "operation_duration_ms":
                try:
                    if not math.isfinite(float(value)) or float(value) < 0:
                        return None
                except ValueError:
                    return None
            if key == "configuration_outcome" and value not in {"ok", "error"}:
                return None
            if key == "archive_state" and value not in {"active", "archived", "all"}:
                return None
            if key == "model_id" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            normalized_trace.append((key, value))
        return tuple(sorted(normalized_dimensions)), tuple(sorted(normalized_trace))

    def _append(self, measurement: Measurement) -> None:
        if len(self._measurements) == self._capacity:
            self._measurements.popleft()
            self._dropped += 1
        self._measurements.append(measurement)

    def snapshot(self) -> tuple[Measurement, ...]:
        return tuple(self._measurements)

    def stats(self) -> TelemetryStats:
        return TelemetryStats(
            retained=len(self._measurements),
            capacity=self._capacity,
            dropped=self._dropped,
            rejected=self._rejected,
            export_failures=self._export_failures,
            abandoned_batches=self._abandoned_batches,
            exporter_configured=self._exporter is not None,
        )

    async def flush(self) -> bool:
        """Export current records and aggregate health with bounded retries."""

        if self._exporter is None:
            return False
        batch_measurements = tuple(self._measurements)
        batch = TelemetryBatch(batch_measurements, self.stats())
        try:
            await self._exporter.export(batch)
        except Exception:
            self._export_failures += 1
            self._current_export_attempts += 1
            if self._current_export_attempts >= self._max_export_attempts:
                for expected in batch_measurements:
                    if self._measurements and self._measurements[0] is expected:
                        self._measurements.popleft()
                        self._dropped += 1
                self._abandoned_batches += 1
                self._current_export_attempts = 0
            return False
        for expected in batch_measurements:
            if self._measurements and self._measurements[0] is expected:
                self._measurements.popleft()
        self._current_export_attempts = 0
        return True


class TelemetryLifecycle:
    """Periodic exporter lifecycle shared by API and worker composition roots."""

    def __init__(self, metrics: MetadataMetrics, *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("telemetry flush interval must be positive")
        self.metrics = metrics
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> bool:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        return await self.metrics.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.metrics.flush()


def new_span_id() -> str:
    return secrets.token_hex(8)


def root_span_id(trace_id: str) -> str:
    if not _valid_trace_id(trace_id):
        raise ValueError("trace id must be 32 lowercase hex characters")
    return trace_id[:16]


def _write_stdout(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError, AttributeError:
        return False
    return True


def _looks_like_uuid(value: str) -> bool:
    return _valid_uuid(value)


def _valid_trace_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _valid_span_id(value: str) -> bool:
    return len(value) == 16 and all(character in "0123456789abcdef" for character in value)


class Stopwatch:
    def __init__(self) -> None:
        self.started = monotonic()

    def elapsed_ms(self) -> float:
        return (monotonic() - self.started) * 1000


def record_memory_operation(
    metrics: MetadataMetrics,
    timer: Stopwatch,
    *,
    outcome: str,
    scope_type: str,
    lifecycle_status: str,
    dependency: str,
    trace_id: str,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    generation_id: str | None = None,
    error_class: str | None = None,
    operation: str = "memory.persist",
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> None:
    """Emit the complete memory measurement set under one trace correlation."""

    bounded_scope = scope_type if scope_type in {"user", "agent"} else "unknown"
    bounded_status = lifecycle_status if lifecycle_status in {
        "active", "dormant", "archived", "disabled", "disputed", "superseded"
    } else "unknown"
    identifiers: dict[str, str] = {}
    if memory_id is not None:
        identifiers["memory_id"] = memory_id
    if memory_revision_id is not None:
        identifiers["memory_revision_id"] = memory_revision_id
    if generation_id is not None:
        identifiers["generation_id"] = generation_id
    metrics.record_span(
        "aura.knowledge.memory_persistence", operation, timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=span_id or new_span_id(),
        parent_span_id=parent_span_id,
        dependency=dependency, outcome=outcome, error_class=error_class, **identifiers,
    )
    # ``trace_id`` is a first-class call argument.  Keeping it out of the
    # attribute map avoids an unregistered duplicate trace dimension and
    # prevents valid memory operations from incrementing telemetry rejection.
    common = dict(identifiers)
    metrics.observe(
        "aura.knowledge.memory_persistence", "memory_operation_duration_ms",
        timer.elapsed_ms(), trace_id=trace_id, outcome=outcome, dependency=dependency, **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence", "memory_operation_outcome",
        trace_id=trace_id, outcome=outcome, dependency=dependency, **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence", "memory_lifecycle_status",
        trace_id=trace_id, status=bounded_status, **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence", "memory_scope_type",
        trace_id=trace_id, scope_type=bounded_scope, **common,
    )


def record_memory_processing(
    metrics: MetadataMetrics,
    *,
    component: str,
    operation: str = "retrieval",
    duration_ms: float,
    trace_id: str,
    outcome: str,
    dependency: str,
    error_class: str | None = None,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    generation_id: str | None = None,
    attempt_count: int | None = None,
    backlog: int | None = None,
    progress: float | None = None,
) -> None:
    """Record worker/model lifecycle metadata with one correlated trace."""

    # Processing also emits maintenance/reindex callbacks through the
    # extraction recorder. Classify by operation so lifecycle and backlog
    # signals remain owned by the maintenance component.
    effective_component = component
    if operation in {
        "memory.maintenance",
        "memory.decay",
        "memory.archive",
        "memory.reindex.chunk",
        "memory.reindex.switch",
    }:
        effective_component = "aura.knowledge.memory_maintenance"

    attrs: dict[str, str] = {}
    if memory_id:
        attrs["memory_id"] = memory_id
    if memory_revision_id:
        attrs["memory_revision_id"] = memory_revision_id
    if generation_id:
        attrs["generation_id"] = generation_id
    if attempt_count is not None:
        attrs["attempt_count"] = str(max(0, attempt_count))
    metrics.record_span(
        effective_component,
        operation,
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency=dependency,
        outcome=outcome,
        error_class=error_class,
        metric_name=_OPERATION_SPAN_METRICS.get((effective_component, operation)),
        **attrs,
    )
    # Core services report semantic boundary operations; keep the mapping in
    # the telemetry platform so domains do not depend on metric names.
    semantic_metrics: Mapping[str, tuple[str, ...]] = {
        "memory.job.queue": (),
        "memory.job": ("memory_job_outcome",),
        "memory.extraction": ("memory_extraction_outcome",),
        "memory.retention_gate": ("memory_personal_retention_gate_outcome",),
        "memory.policy": ("memory_candidate_decision",),
        "memory.fallback": ("memory_fallback_outcome",),
        "memory.candidate": ("memory_candidate_outcome",),
        "memory.action": ("memory_action_outcome",),
        "memory.embedding": ("embedding_duration_ms", "missing_embedding_backlog"),
        # A sweep measures maintenance duration. Lifecycle transitions are
        # emitted only by the concrete decay/archive operations.
        "memory.maintenance": (),
        "memory.decay": ("memory_maintenance_transition",),
        "memory.archive": ("memory_maintenance_transition",),
        "memory.reindex.chunk": (
            "memory_reindex_progress", "memory_reindex_backlog",
        ),
        "memory.reindex.switch": (),
        "memory.retry": ("memory_retry_count",),
        "memory.error": ("memory_processing_errors",),
    }
    for semantic_metric in semantic_metrics.get(operation, ()):
        if semantic_metric.endswith("_duration_ms") or semantic_metric.endswith("_wait_ms"):
            metrics.observe(
                effective_component, semantic_metric, duration_ms, trace_id=trace_id,
                outcome=outcome, dependency=dependency,
            )
        elif semantic_metric.endswith("_progress"):
            metrics.observe(
                effective_component, semantic_metric,
                float(progress if progress is not None else 0),
                trace_id=trace_id, outcome=outcome, dependency=dependency,
            )
        elif semantic_metric.endswith("_backlog"):
            metrics.observe(
                effective_component, semantic_metric, float(max(0, backlog or 0)),
                trace_id=trace_id, outcome=outcome, dependency=dependency,
            )
        else:
            metrics.increment(
                effective_component, semantic_metric, outcome=outcome,
                dependency=dependency, trace_id=trace_id,
            )
    if operation == "memory.candidate" and outcome == "review":
        metrics.increment(
            effective_component,
            "memory_reviewable_candidate_count",
            outcome="review",
            dependency=dependency,
            trace_id=trace_id,
        )
    if backlog is not None and "backlog" in _METRICS.get(effective_component, frozenset()):
        metrics.observe(
            effective_component, "backlog", float(max(0, backlog)), trace_id=trace_id,
            dependency=dependency,
        )


_RETRIEVAL_OPERATION_ALIASES: Mapping[str, str] = {
    "retrieval": "memory.retrieval",
    "memory.retrieval": "memory.retrieval",
    "memory.retrieval.generation": "memory.retrieval",
    "primary": "memory.retrieval",
    "memory.retrieval.primary": "memory.retrieval",
    "query_embedding": "memory.query_embedding",
    "memory.retrieval.query_embedding": "memory.query_embedding",
    "memory.query_embedding": "memory.query_embedding",
    "lexical": "memory.lexical",
    "memory.retrieval.lexical": "memory.lexical",
    "memory.lexical": "memory.lexical",
    "vector": "memory.vector",
    "memory.retrieval.vector": "memory.vector",
    "memory.vector": "memory.vector",
    "fusion": "memory.fusion",
    "memory.retrieval.fusion": "memory.fusion",
    "memory.fusion": "memory.fusion",
    "rerank": "memory.rerank",
    "memory.retrieval.rerank": "memory.rerank",
    "memory.rerank": "memory.rerank",
    "selection": "memory.selection",
    "memory.retrieval.selection": "memory.selection",
    "memory.selection": "memory.selection",
    "context_budget": "memory.context_budget",
    "memory.retrieval.context_budget": "memory.context_budget",
    "memory.context_budget": "memory.context_budget",
    "memory.context_budget.post_render": "memory.context_budget",
    "memory.retrieval.context_budget.post_render": "memory.context_budget",
    "fallback": "memory.fallback",
    "memory.retrieval.fallback": "memory.fallback",
    "memory.fallback": "memory.fallback",
    "degradation": "memory.degradation",
    "memory.retrieval.degradation": "memory.degradation",
    "memory.degradation": "memory.degradation",
}
_RETRIEVAL_SPAN_METRICS: Mapping[str, str] = {
    "memory.retrieval": "memory_retrieval_duration_ms",
    "memory.query_embedding": "memory_query_embedding_duration_ms",
    "memory.lexical": "memory_lexical_search_duration_ms",
    "memory.vector": "memory_vector_search_duration_ms",
    "memory.rerank": "memory_rerank_duration_ms",
    # Fusion, selection, budget, fallback, and degradation are represented by
    # bounded stage dimensions and use the enclosing retrieval duration name.
    "memory.fusion": "memory_retrieval_duration_ms",
    "memory.selection": "memory_retrieval_duration_ms",
    "memory.context_budget": "memory_retrieval_duration_ms",
    "memory.fallback": "memory_retrieval_duration_ms",
    "memory.degradation": "memory_retrieval_duration_ms",
}


def record_memory_retrieval(
    metrics: MetadataMetrics,
    *,
    operation: str = "retrieval",
    duration_ms: float,
    trace_id: str,
    outcome: str,
    dependency: str = "memory_store",
    parent_span_id: str | None = None,
    error_class: str | None = None,
    retrieval_stage: str | None = None,
    scope_type: str | None = None,
    degradation: str | None = None,
    candidate_count: int | None = None,
    recall_count: int | None = None,
    fallback_outcome: str | None = None,
    context_tokens: int | None = None,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    memory_policy_revision_id: str | None = None,
    generation_id: str | None = None,
    embedding_generation_id: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    retrieval_version: str | None = None,
    selection_order: int | None = None,
    fallback_grant_id: str | None = None,
    recall_mode: str | None = None,
    gate_outcome: str | None = None,
) -> None:
    """Record one retrieval boundary and its bounded recall metadata.

    This is the only telemetry seam retrieval code needs.  It deliberately
    accepts identifiers and counts, never memory text, evidence, vectors,
    prompts, responses, credentials, or owner subjects.  A caller can emit
    stage-specific observations without coupling Core retrieval code to the
    platform metric registry.
    """

    span_operation = _RETRIEVAL_OPERATION_ALIASES.get(operation, operation)
    if span_operation not in _RETRIEVAL_OPERATION_ALIASES.values():
        # Let MetadataMetrics apply its normal rejection accounting while
        # retaining the no-I/O property of the recording path.
        metrics.record_span(
            "aura.knowledge.memory_retrieval",
            span_operation,
            duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency=dependency,
            outcome=outcome,
            error_class=error_class,
        )
        return

    # Provider and driver exceptions may arrive as arbitrary class names.
    # Keep those names out of bounded labels and map unknown values to stable
    # privacy-safe buckets before recording.
    bounded_dependency = (
        dependency if dependency in _ENUM_DIMENSIONS["dependency"] else "memory_store"
    )
    bounded_outcome = outcome if outcome in _ENUM_DIMENSIONS["outcome"] else "unknown"
    bounded_error = (
        error_class
        if error_class in _ENUM_DIMENSIONS["error_class"]
        else ("unknown" if error_class is not None else None)
    )
    bounded_fallback = (
        fallback_outcome
        if fallback_outcome in _ENUM_DIMENSIONS["outcome"]
        else ("unknown" if fallback_outcome is not None else None)
    )
    bounded_parent = (
        parent_span_id
        if parent_span_id is not None and _valid_span_id(parent_span_id)
        else None
    )
    # The in-memory recall seam may not have a run trace.  Keep the callback
    # valid without serializing the caller's fallback label as a trace id.
    trace_id = trace_id if _valid_trace_id(trace_id) else secrets.token_hex(16)

    bounded_stage = retrieval_stage or {
        "memory.retrieval": "primary",
        "memory.query_embedding": "query_embedding",
        "memory.lexical": "lexical",
        "memory.vector": "vector",
        "memory.fusion": "fusion",
        "memory.rerank": "rerank",
        "memory.selection": "selection",
        "memory.context_budget": "context_budget",
        "memory.fallback": "fallback",
        "memory.degradation": "degradation",
    }[span_operation]
    trace_attributes: dict[str, str] = {}
    for key, value in {
        "memory_id": memory_id,
        "memory_revision_id": memory_revision_id,
        "memory_policy_revision_id": memory_policy_revision_id,
        "generation_id": generation_id,
        "embedding_generation_id": embedding_generation_id,
        "retrieval_version": retrieval_version,
        "fallback_grant_id": fallback_grant_id,
        "recall_mode": recall_mode,
        "gate_outcome": gate_outcome,
    }.items():
        if value is not None:
            trace_attributes[key] = value
    for key, value in {
        "selection_order": selection_order,
        "candidate_count": candidate_count,
        "recalled_count": recall_count,
        "context_tokens": context_tokens,
    }.items():
        if value is not None:
            trace_attributes[key] = str(max(0, value))

    metrics.record_span(
        "aura.knowledge.memory_retrieval",
        span_operation,
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=bounded_parent,
        dependency=bounded_dependency,
        outcome=bounded_outcome,
        error_class=bounded_error,
        retrieval_stage=bounded_stage,
        scope_type=scope_type,
        degradation=degradation,
        run_id=run_id,
        conversation_id=conversation_id,
        metric_name=_RETRIEVAL_SPAN_METRICS[span_operation],
        **trace_attributes,
    )
    common: dict[str, str] = {
        "outcome": bounded_outcome,
        "dependency": bounded_dependency,
        "retrieval_stage": bounded_stage,
    }
    if scope_type is not None:
        common["scope_type"] = scope_type
    if degradation is not None:
        common["degradation"] = degradation
    if bounded_error is not None:
        common["error_class"] = bounded_error
    common.update(trace_attributes)

    if candidate_count is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_retrieval_candidate_count",
            float(max(0, candidate_count)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if recall_count is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_recall_count",
            float(max(0, recall_count)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if fallback_outcome is not None:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_fallback_outcome",
            outcome=bounded_fallback or "unknown",
            retrieval_stage="fallback",
            dependency=bounded_dependency,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **{key: value for key, value in trace_attributes.items()},
        )
    if gate_outcome is not None:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_recall_gate_outcome",
            outcome=(
                gate_outcome
                if gate_outcome in {"policy_off", "selected", "empty"}
                else "unknown"
            ),
            retrieval_stage="primary",
            dependency=bounded_dependency,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
        )
    if context_tokens is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_context_tokens",
            float(max(0, context_tokens)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if bounded_error is not None or bounded_outcome in {"error", "degraded"}:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_retrieval_errors",
            outcome=bounded_outcome,
            dependency=bounded_dependency,
            error_class=bounded_error or "persistence",
            retrieval_stage=bounded_stage,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **{key: value for key, value in trace_attributes.items()},
        )
