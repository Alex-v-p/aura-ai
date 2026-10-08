import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.public import (
    ChatMessage,
    RunClaimLost,
    RunCoordinator,
)
from aura_core.domains.interaction.conversations.public import (
    ConversationStore,
    MessageState,
)
from aura_core.domains.interaction.conversations.public import (
    Message as ConversationMessage,
)
from aura_core.entrypoints.api.state import AppState
from aura_core.entrypoints.worker.app import make_worker_telemetry, run_once
from aura_core.platform.auth import Settings
from aura_core.platform.outbox import InMemoryOutbox, OutboxCommand
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.telemetry import (
    COMPONENT_VERSIONS,
    MetadataMetrics,
    StructuredContainerLogExporter,
    TelemetryBatch,
    TelemetryLifecycle,
    new_span_id,
    record_memory_retrieval,
    root_span_id,
)
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.streaming.publisher import EventPublisher


def test_metadata_metrics_is_bounded_and_separates_dimensions_from_trace_fields() -> None:
    metrics = MetadataMetrics(capacity=2)
    run_id, conversation_id = uuid4(), uuid4()
    metadata = {
        "trace_id": run_id.hex,
        "run_id": str(run_id),
        "conversation_id": str(conversation_id),
    }

    metrics.increment("aura.execution.run_coordinator", "run_started", **metadata)
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        provider="ollama",
        model_id="llama3.2:latest",
        **metadata,
    )
    metrics.increment("aura.runtime.stream_delivery", "sse_connections", **metadata)
    metrics.increment(
        "aura.runtime.stream_delivery",
        "errors",
        prompt="private conversation text",
        **metadata,
    )
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        provider="https://secret-bearing-endpoint.invalid",
        model_id="fake",
        **metadata,
    )

    assert [item.metric for item in metrics.snapshot()] == ["model_routed", "sse_connections"]
    routed = metrics.snapshot()[0]
    assert dict(routed.dimensions) == {"provider": "ollama"}
    assert dict(routed.trace_attributes) == {
        "conversation_id": str(conversation_id),
        "model_id": "llama3.2:latest",
        "run_id": str(run_id),
    }
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    assert metrics.stats().dropped == 1
    assert metrics.stats().rejected == 2


def test_memory_product_observations_are_registered_and_metadata_only() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    required = (
        (
            "aura.knowledge.memory_persistence",
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
        ),
        (
            "aura.knowledge.memory_extraction",
            "memory_candidate_review_duration_ms",
            "memory_candidate_review_outcome",
        ),
        (
            "aura.knowledge.memory_persistence",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
        ),
        (
            "aura.interaction.agent_configuration",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
        ),
        (
            "aura.runtime.stream_delivery",
            "memory_activity_publication_duration_ms",
            "memory_activity_publication_outcome",
        ),
        (
            "aura.runtime.stream_delivery",
            "memory_activity_delivery_duration_ms",
            "memory_activity_delivery_outcome",
        ),
    )
    for component, duration, outcome in required:
        metrics.observe(
            component,
            duration,
            1.0,
            dependency="memory_store",
            outcome="ok",
            trace_id=trace_id,
        )
        metrics.increment(
            component,
            outcome,
            dependency="memory_store",
            outcome="ok",
            trace_id=trace_id,
        )
    assert metrics.stats().rejected == 0
    assert {item.metric for item in metrics.snapshot()} == {
        metric
        for _, duration, outcome in required
        for metric in (duration, outcome)
    }
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    retained = len(metrics.snapshot())
    metrics.increment(
        "aura.knowledge.memory_persistence",
        "memory_product_operation_outcome",
        content="must be rejected before export",
    )
    assert len(metrics.snapshot()) == retained
    assert metrics.stats().rejected == 1


def test_stream_activity_operations_use_distinct_metrics_without_rejection() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.publish",
        3.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="event_store",
        outcome="ok",
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.cursor_expired",
        1.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="event_store",
        outcome="expired",
        error_class="cursor_expired",
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )
    metrics.record_span(
        "aura.runtime.stream_delivery",
        "memory.activity.reconcile",
        2.5,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="memory_store",
        outcome="not_found",
        error_class="run_not_found",
        run_id=str(uuid4()),
    )
    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [item.metric for item in spans] == [
        "memory_activity_publication_duration_ms",
        "stream_delivery_duration_ms",
        "stream_delivery_duration_ms",
    ]
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_export_failure_retries_are_bounded_and_counters_survive() -> None:
    class Exporter:
        def __init__(self) -> None:
            self.fail = True
            self.exported: list[TelemetryBatch] = []

        async def export(self, batch: TelemetryBatch) -> None:
            if self.fail:
                raise RuntimeError("collector unavailable")
            self.exported.append(batch)

    exporter = Exporter()
    metrics = MetadataMetrics(exporter=exporter, max_export_attempts=2)
    metrics.increment("aura.execution.run_coordinator", "run_started")

    assert not await metrics.flush()
    assert len(metrics.snapshot()) == 1
    assert not await metrics.flush()
    assert metrics.snapshot() == ()
    assert metrics.stats().export_failures == 2
    assert metrics.stats().dropped == 1
    assert metrics.stats().abandoned_batches == 1

    exporter.fail = False
    assert await metrics.flush()
    assert exporter.exported[-1].stats.export_failures == 2
    assert exporter.exported[-1].stats.dropped == 1


@pytest.mark.asyncio
async def test_structured_container_export_is_metadata_only_and_exports_health() -> None:
    lines: list[str] = []
    delegate = StructuredContainerLogExporter(lines.append)

    class FailOnce:
        def __init__(self) -> None:
            self.failed = False

        async def export(self, batch: TelemetryBatch) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("stdout temporarily unavailable")
            await delegate.export(batch)

    run_id = uuid4()
    metrics = MetadataMetrics(capacity=1, exporter=FailOnce())
    metrics.increment(
        "aura.runtime.model_routing",
        "model_routed",
        trace_id=run_id.hex,
        run_id=str(run_id),
        provider="ollama",
        model_id="chat:latest",
    )
    metrics.increment("aura.runtime.stream_delivery", "sse_connections")
    metrics.increment(
        "aura.runtime.stream_delivery",
        "errors",
        response="private assistant output",
    )

    assert not await metrics.flush()
    assert await metrics.flush()
    records = [cast(dict[str, Any], json.loads(line)) for line in lines]
    measurement = next(
        record for record in records if record["event"] == "aura.telemetry.measurement"
    )
    health = next(record for record in records if record["event"] == "aura.telemetry.health")
    assert measurement["capture_policy"] == "metadata_only"
    assert set(cast(dict[str, object], measurement["dimensions"])) == set()
    assert health["dropped"] == 1
    assert health["rejected"] == 1
    assert health["export_failures"] == 1
    rendered = "\n".join(lines).casefold()
    assert all(
        forbidden not in rendered
        for forbidden in ("private assistant", '"prompt"', '"response"', '"endpoint"', '"token"')
    )


@pytest.mark.asyncio
async def test_periodic_lifecycle_flushes_and_shutdown_flushes() -> None:
    class Exporter:
        def __init__(self) -> None:
            self.batches: list[TelemetryBatch] = []

        async def export(self, batch: TelemetryBatch) -> None:
            self.batches.append(batch)

    exporter = Exporter()
    metrics = MetadataMetrics(exporter=exporter)
    lifecycle = TelemetryLifecycle(metrics, interval_seconds=0.01)
    metrics.increment("aura.execution.run_coordinator", "run_started")

    await lifecycle.start()
    await asyncio.sleep(0.03)
    metrics.increment("aura.execution.run_coordinator", "run_started")
    assert await lifecycle.stop()

    assert not lifecycle.running
    assert any(batch.measurements for batch in exporter.batches)
    assert metrics.snapshot() == ()


def test_normal_api_composition_configures_real_exporter() -> None:
    state = AppState(
        Settings(
            database_url="postgresql+psycopg://aura:aura@127.0.0.1:5432/aura",
            oidc_issuer="https://identity.example",
        )
    )
    assert isinstance(state.metrics.exporter, StructuredContainerLogExporter)
    assert state.metrics.stats().exporter_configured


def test_normal_worker_composition_configures_real_exporter() -> None:
    metrics, lifecycle = make_worker_telemetry()

    assert isinstance(metrics.exporter, StructuredContainerLogExporter)
    assert metrics.stats().exporter_configured
    assert lifecycle.metrics is metrics


@pytest.mark.asyncio
async def test_production_worker_execution_emits_a_unique_real_root_span() -> None:
    provider = FakeChatModel(("ok",))
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "worker", "fake", models, str(uuid4())
    )
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)
    command = OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        run.id,
        run.conversation_id,
        datetime.now(UTC),
        run.id,
        run.id,
    )

    class Consumer:
        acknowledged = False

        async def receive(self) -> OutboxCommand:
            return command

        async def ack(self) -> None:
            self.acknowledged = True

    consumer = Consumer()
    await run_once(coordinator, provider, cast(NatsRunConsumer, cast(object, consumer)))

    roots = [
        item
        for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "run.execute"
    ]
    assert consumer.acknowledged
    assert len(roots) == 1
    assert roots[0].trace_id == run.id.hex
    assert roots[0].parent_span_id is None
    assert roots[0].span_id != root_span_id(run.id.hex)


@pytest.mark.asyncio
async def test_coordinator_records_actual_execution_boundaries() -> None:
    provider = FakeChatModel(("four", " chars"))
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "question", "fake", models, str(uuid4())
    )
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, provider)

    measurements = metrics.snapshot()
    names = {item.metric for item in measurements}
    assert {
        "checkpoint_persisted",
        "model_routed",
        "output_tokens",
        "queue_wait_ms",
        "run_duration_ms",
        "time_to_first_token_ms",
    } <= names
    output = next(item for item in measurements if item.metric == "output_tokens")
    assert output.value == 3
    assert dict(output.dimensions)["token_estimator"] == "chars_div_4_ceil"
    duration = next(item for item in measurements if item.metric == "run_duration_ms")
    assert dict(duration.dimensions)["status"] == "completed"
    assert all(item.trace_id == run.id.hex for item in measurements)
    spans = {
        dict(item.trace_attributes)["operation"]: item
        for item in measurements
        if item.kind == "span"
    }
    worker = spans["run.execute"]
    routed = spans["model.route"]
    inferred = spans["model.infer"]
    assert worker.metric == "operation_duration_ms"
    assert routed.metric == "model_routing_duration_ms"
    assert inferred.metric == "inference_duration_ms"
    assert routed.value > 0
    assert routed.parent_span_id == inferred.parent_span_id == worker.span_id
    assert worker.parent_span_id is None
    assert len({worker.span_id, routed.span_id, inferred.span_id}) == 3


@pytest.mark.asyncio
async def test_claim_loss_during_stream_is_not_classified_as_provider_error() -> None:
    catalog = FakeChatModel()

    class ClaimLostOnAppendStore(ConversationStore):
        async def append_assistant(
            self,
            run_id: UUID,
            text: str,
            state: MessageState = MessageState.PARTIAL,
            *,
            attempt_id: UUID | None = None,
        ) -> ConversationMessage:
            del run_id, text, state, attempt_id
            raise RunClaimLost

    store = ClaimLostOnAppendStore()
    models = await catalog.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "lease loss", "fake", models, str(uuid4())
    )

    class LeaseLostProvider:
        def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del model_id, messages

            async def chunks() -> AsyncIterator[str]:
                yield "stale"

            return chunks()

    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, LeaseLostProvider())

    inference = next(
        item
        for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "model.infer"
    )
    assert dict(inference.dimensions)["outcome"] == "skipped"
    assert "error_class" not in dict(inference.dimensions)
    assert not any(item.metric == "provider_errors" for item in metrics.snapshot())


@pytest.mark.asyncio
async def test_lease_expiry_metric_preserves_the_actual_run_trace() -> None:
    provider = FakeChatModel()
    store = ConversationStore()
    models = await provider.list_models()
    _, _, run = await store.create(
        "https://issuer", "owner", "expire", "fake", models, str(uuid4())
    )
    await store.start_run(run.id, worker_id=uuid4(), lease_seconds=0.001)
    await asyncio.sleep(0.01)
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics)

    await coordinator.execute(run.id, provider)

    expiry = next(item for item in metrics.snapshot() if item.metric == "lease_expired")
    _, interrupted = await store.find_run_any(run.id)
    assert interrupted.error is not None
    assert interrupted.error.trace_id == run.id.hex == expiry.trace_id
    assert expiry.trace_id == run.id.hex
    assert dict(expiry.trace_attributes)["run_id"] == str(run.id)
    assert dict(expiry.trace_attributes)["conversation_id"] == str(run.conversation_id)


def test_parented_span_has_component_identity_dependency_and_trace_only_ids() -> None:
    run_id, conversation_id = uuid4(), uuid4()
    metrics = MetadataMetrics()
    child_id = new_span_id()
    metrics.record_span(
        "aura.interaction.conversation_persistence",
        "checkpoint.persist",
        3.5,
        trace_id=run_id.hex,
        span_id=child_id,
        parent_span_id=root_span_id(run_id.hex),
        dependency="postgresql",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
    )

    span = metrics.snapshot()[0]
    assert span.kind == "span"
    assert span.component_version == COMPONENT_VERSIONS[
        "aura.interaction.conversation_persistence"
    ] == "1.6.0"
    assert span.span_id == child_id
    assert span.parent_span_id == root_span_id(run_id.hex)
    assert dict(span.dimensions) == {"dependency": "postgresql", "outcome": "ok"}
    assert dict(span.trace_attributes)["run_id"] == str(run_id)
    assert not _contains_uuid(dict(span.dimensions))


def test_title_inference_component_version_remains_explicit() -> None:
    assert COMPONENT_VERSIONS["aura.runtime.model_inference"] == "1.4.0"


def test_memory_retrieval_telemetry_is_bounded_and_metadata_only() -> None:
    metrics = MetadataMetrics()
    run_id, memory_id = uuid4(), uuid4()
    record_memory_retrieval(
        metrics,
        operation="vector",
        duration_ms=4.5,
        trace_id=run_id.hex,
        outcome="ok",
        dependency="memory_store",
        scope_type="agent",
        candidate_count=50,
        recall_count=3,
        context_tokens=120,
        memory_id=str(memory_id),
        memory_revision_id=str(uuid4()),
        memory_policy_revision_id=str(uuid4()),
        embedding_generation_id=str(uuid4()),
        run_id=str(run_id),
    )

    measurements = metrics.snapshot()
    assert measurements[0].component_id == "aura.knowledge.memory_retrieval"
    assert measurements[0].metric == "memory_vector_search_duration_ms"
    assert dict(measurements[0].dimensions)["retrieval_stage"] == "vector"
    assert {item.metric for item in measurements} >= {
        "memory_vector_search_duration_ms",
        "memory_retrieval_candidate_count",
        "memory_recall_count",
        "memory_context_tokens",
    }
    rendered = json.dumps(
        [
            {
                "metric": item.metric,
                "dimensions": dict(item.dimensions),
                "trace": dict(item.trace_attributes),
            }
            for item in measurements
        ]
    )
    assert all(
        secret not in rendered
        for secret in ('"content":', '"prompt":', '"response":', '"vector":')
    )


def test_memory_retrieval_production_stage_shapes_preserve_parent_and_bound_labels() -> None:
    metrics = MetadataMetrics()
    trace_id, parent_span_id = uuid4().hex, new_span_id()
    record_memory_retrieval(
        metrics,
        operation="memory.retrieval.lexical",
        duration_ms=2.0,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        outcome="ok",
        dependency="postgresql",
        candidate_count=50,
    )
    record_memory_retrieval(
        metrics,
        operation="memory.retrieval.fallback",
        duration_ms=1.0,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        outcome="ok",
        dependency="foreign-store-driver",
        fallback_outcome="empty",
        error_class="RuntimeError",
    )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(spans) == 2
    assert spans[0].parent_span_id == parent_span_id
    assert spans[0].metric == "memory_lexical_search_duration_ms"
    assert dict(spans[1].dimensions)["dependency"] == "memory_store"
    assert dict(spans[1].dimensions)["error_class"] == "unknown"
    assert metrics.stats().rejected == 0


def test_memory_retrieval_query_embedding_primary_and_post_render_budget_shapes() -> None:
    metrics = MetadataMetrics()
    trace_id, parent_span_id = uuid4().hex, new_span_id()
    for operation, stage, fallback in (
        ("memory.retrieval.query_embedding", "query_embedding", None),
        ("memory.retrieval", "primary", "not_needed"),
        ("memory.retrieval.context_budget", "context_budget", "not_granted"),
    ):
        record_memory_retrieval(
            metrics,
            operation=operation,
            duration_ms=1.0,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            outcome="ok",
            dependency="query_embedding" if stage == "query_embedding" else "memory_store",
            retrieval_stage=stage,
            fallback_outcome=fallback,
            context_tokens=96 if stage == "context_budget" else None,
        )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [item.metric for item in spans] == [
        "memory_query_embedding_duration_ms",
        "memory_retrieval_duration_ms",
        "memory_retrieval_duration_ms",
    ]
    assert all(item.parent_span_id == parent_span_id for item in spans)
    fallback = [
        item for item in metrics.snapshot() if item.metric == "memory_fallback_outcome"
    ]
    assert {dict(item.dimensions)["outcome"] for item in fallback} == {
        "not_needed",
        "not_granted",
    }
    assert any(item.metric == "memory_context_tokens" for item in metrics.snapshot())
    assert metrics.stats().rejected == 0


def test_conversation_list_telemetry_is_postgresql_bound_and_metadata_only() -> None:
    from aura_core.bootstrap.conversation_uow import SqlConversationStore

    metrics = MetadataMetrics()
    store = SqlConversationStore(lambda: None)  # type: ignore[arg-type]
    store.set_prompt_metrics(metrics)
    trace_id = uuid4().hex
    record_list = store._record_conversation_list  # pyright: ignore[reportPrivateUsage]

    parent_span_id = new_span_id()
    record_list(trace_id, parent_span_id, 0.0, "ok", None)
    record_list(trace_id, parent_span_id, 0.0, "error", "persistence")

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert [dict(item.trace_attributes)["operation"] for item in spans] == [
        "conversation.list",
        "conversation.list",
    ]
    assert dict(spans[0].dimensions) == {"dependency": "postgresql", "outcome": "ok"}
    assert dict(spans[1].dimensions) == {
        "dependency": "postgresql",
        "error_class": "persistence",
        "outcome": "error",
    }
    assert all("subject" not in dict(item.trace_attributes) for item in spans)
    assert all(item.parent_span_id == parent_span_id for item in spans)


@pytest.mark.asyncio
async def test_sql_conversation_list_records_parented_success_and_failure_spans() -> None:
    from aura_core.bootstrap.conversation_uow import SqlConversationStore

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    metrics = MetadataMetrics()
    store = SqlConversationStore(lambda: SessionContext())  # type: ignore[arg-type]
    store.set_prompt_metrics(metrics)
    trace_id, parent_span_id = uuid4().hex, new_span_id()

    async def no_principal(session: object, issuer: str, subject: str) -> None:
        del session, issuer, subject
        return None

    store.identities.find = no_principal  # type: ignore[method-assign]
    assert await store.list(
        "owner",
        issuer="https://issuer",
        trace_id=trace_id,
        parent_span_id=parent_span_id,
    ) == ([], None)

    async def persistence_failure(session: object, issuer: str, subject: str) -> None:
        del session, issuer, subject
        raise RuntimeError("database details must not escape telemetry")

    store.identities.find = persistence_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await store.list(
            "owner",
            issuer="https://issuer",
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(spans) == 2
    assert [dict(item.dimensions)["outcome"] for item in spans] == ["ok", "error"]
    assert dict(spans[1].dimensions)["error_class"] == "persistence"
    assert all(item.trace_id == trace_id for item in spans)
    assert all(item.parent_span_id == parent_span_id for item in spans)


def test_prompt_compilation_telemetry_keeps_provenance_metadata_without_prompt_content() -> None:
    lines: list[str] = []
    exporter = StructuredContainerLogExporter(lines.append)
    metrics = MetadataMetrics(exporter=exporter)
    run_id, conversation_id = uuid4(), uuid4()
    metrics.record_span(
        "aura.runtime.prompt_compilation",
        "prompt.compile",
        2.5,
        trace_id=run_id.hex,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="model_policy",
        outcome="ok",
        run_id=str(run_id),
        conversation_id=str(conversation_id),
        agent_revision_id=str(uuid4()),
        persona_revision_id=str(uuid4()),
        prompt_bundle_revision_id=str(uuid4()),
        prompt_hash="a" * 64,
        prompt_component_count="4",
        compiled_prompt_size="123",
    )

    measurement = metrics.snapshot()[0]
    assert measurement.component_id == "aura.runtime.prompt_compilation"
    assert measurement.metric == "prompt_compile_duration_ms"
    assert dict(measurement.trace_attributes)["prompt_hash"] == "a" * 64
    assert dict(measurement.trace_attributes)["prompt_component_count"] == "4"
    assert dict(measurement.trace_attributes)["compiled_prompt_size"] == "123"
    assert asyncio.run(metrics.flush())
    rendered = "\n".join(lines)
    assert "instructions" not in rendered
    assert "rendered prompt" not in rendered


@pytest.mark.asyncio
async def test_nats_readiness_recovers_after_stream_subject_is_restored() -> None:
    class Config:
        subjects = ["aura.runs.execute.v1"]

    class Info:
        config = Config()

    class JetStream:
        async def account_info(self) -> object:
            return object()

        async def stream_info(self, name: str) -> Info:
            assert name == "AURA_RUNS"
            return Info()

    class Connection:
        is_closed = False

    transport = NatsOutbox("nats://unused")
    cast(Any, transport)._connection = Connection()
    cast(Any, transport)._jetstream = JetStream()

    assert await transport.readiness() == (True, None)
    Config.subjects = ["wrong.subject"]
    assert await transport.readiness() == (False, "run stream subject missing")
    Config.subjects = ["aura.runs.execute.v1"]
    assert await transport.readiness() == (True, None)


@pytest.mark.asyncio
async def test_consumer_duplicate_and_ack_are_observed_without_uuid_dimensions() -> None:
    command = OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        uuid4(),
        uuid4(),
        datetime.now(UTC),
        uuid4(),
        uuid4(),
    )

    class Message:
        def __init__(self) -> None:
            self.data = command.wire_payload()
            self.metadata = SimpleNamespace(num_delivered=2)
            self.acknowledged = False

        async def ack(self) -> None:
            self.acknowledged = True

    message = Message()

    class Subscription:
        async def fetch(self, count: int, timeout: int) -> Sequence[Message]:
            assert count == 1 and timeout == 30
            return (message,)

    metrics = MetadataMetrics()
    consumer = NatsRunConsumer("nats://unused", metrics=metrics)
    cast(Any, consumer)._subscription = Subscription()

    received = await consumer.receive()
    await consumer.ack()

    assert received == command
    assert message.acknowledged
    names = [item.metric for item in metrics.snapshot()]
    assert names == ["consumer_received", "outbox_duplicates", "consumer_acknowledged"]
    assert all(not _contains_uuid(dict(item.dimensions)) for item in metrics.snapshot())
    assert all(
        dict(item.trace_attributes)["command_id"] == str(command.id)
        for item in metrics.snapshot()
    )


def _contains_uuid(values: dict[str, object]) -> bool:
    for value in values.values():
        try:
            from uuid import UUID

            UUID(str(value))
        except ValueError:
            continue
        return True
    return False
