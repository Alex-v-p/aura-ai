"""Worker composition root; durable deployments replace the in-process outbox."""

from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.domains.execution.runs.public import ChatCompletionPort, RunCoordinator
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox import TransactionalOutboxTransport
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.telemetry import (
    MetadataMetrics,
    Stopwatch,
    StructuredContainerLogExporter,
    TelemetryLifecycle,
    new_span_id,
)
from aura_core.providers.models.ollama.adapter import OllamaAdapter


async def run_once(
    coordinator: RunCoordinator,
    provider: ChatCompletionPort,
    consumer: NatsRunConsumer,
) -> None:
    command = await consumer.receive()
    if command is None:
        return
    try:
        await coordinator.execute(command.run_id, provider)
    except Exception:
        # Leave the JetStream delivery unacknowledged so it can be redelivered.
        raise
    else:
        acknowledge = getattr(consumer, "ack", None)
        if acknowledge is not None:
            await acknowledge()


async def run_forever(settings: Settings | None = None) -> None:
    """Production worker loop; every command is acknowledged after execution."""
    config = settings or Settings()
    engine = make_engine(config.database_url)
    sessions = session_factory(engine)
    store = SqlConversationStore(sessions)
    metrics, telemetry = make_worker_telemetry()
    consumer = NatsRunConsumer(config.nats_url, metrics=metrics)
    event_transport = NatsOutbox(config.nats_url)
    await telemetry.start()
    try:
        await consumer.connect()
        await event_transport.connect()
        from aura_core.runtime.streaming.publisher import PersistentEventPublisher

        coordinator = RunCoordinator(
            store,
            PersistentEventPublisher(store.persist_event, event_transport.publish_event),
            TransactionalOutboxTransport(),
            metrics,
            lease_seconds=config.ollama_run_timeout_seconds + 30.0,
            timeout_seconds=config.ollama_run_timeout_seconds,
            context_token_budget=config.context_token_budget,
        )
        provider = OllamaAdapter(config.ollama_url, config.ollama_run_timeout_seconds)
        while True:
            expired_runs = await store.expire_leases()
            for expired_run in expired_runs:
                expiry_timer = Stopwatch()
                expiry_span_id = new_span_id()
                try:
                    await coordinator.publish_reconciled_status(expired_run)
                except Exception:
                    metrics.record_span(
                        "aura.execution.run_coordinator",
                        "lease.expire",
                        expiry_timer.elapsed_ms(),
                        trace_id=expired_run.id.hex,
                        span_id=expiry_span_id,
                        parent_span_id=None,
                        dependency="event_store",
                        outcome="error",
                        error_class="delivery",
                        run_id=str(expired_run.id),
                        conversation_id=str(expired_run.conversation_id),
                    )
                    raise
                metrics.record_span(
                    "aura.execution.run_coordinator",
                    "lease.expire",
                    expiry_timer.elapsed_ms(),
                    trace_id=expired_run.id.hex,
                    span_id=expiry_span_id,
                    parent_span_id=None,
                    dependency="event_store",
                    outcome="ok",
                    run_id=str(expired_run.id),
                    conversation_id=str(expired_run.conversation_id),
                )
                metrics.increment(
                    "aura.execution.run_coordinator",
                    "lease_expired",
                    trace_id=expired_run.id.hex,
                    run_id=str(expired_run.id),
                    conversation_id=str(expired_run.conversation_id),
                )
            await run_once(coordinator, provider, consumer)
    finally:
        await consumer.close()
        await event_transport.close()
        await telemetry.stop()
        await engine.dispose()


def make_worker_telemetry() -> tuple[MetadataMetrics, TelemetryLifecycle]:
    """Build the production metadata-only exporter and its lifecycle."""

    metrics = MetadataMetrics(exporter=StructuredContainerLogExporter())
    return metrics, TelemetryLifecycle(metrics)


def main() -> None:
    import asyncio

    asyncio.run(run_forever())
