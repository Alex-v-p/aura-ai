"""Deterministic recall contract and opt-in SQL retrieval coverage."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from aura_core.domains.interaction.agents.public import MemoryPolicy, MemoryRecallMode
from aura_core.domains.knowledge.memory.public import (
    MemoryEmbeddingGeneration,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
)
from aura_core.domains.knowledge.memory.recall import (
    MemoryQueryEmbedding,
    MemoryRecallRepository,
    MemoryRecallRequest,
    MemoryRecallService,
    MemoryRecallTelemetryEvent,
)
from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.telemetry import MetadataMetrics, record_memory_retrieval
from sqlalchemy.exc import DBAPIError, OperationalError

ISSUER = "https://recall-tests.example"
SUBJECT = f"owner-{uuid4()}"
AGENT = UUID("11111111-1111-4111-8111-111111111111")
FOREIGN_AGENT = UUID("22222222-2222-4222-8222-222222222222")


async def _memory_store() -> tuple[MemoryStore, MemoryEmbeddingGeneration, datetime]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = MemoryStore(clock=lambda: now)
    generation = await store.register_embedding_generation(
        ISSUER, SUBJECT, generation=1, model_id="test-embedder", dimension=2
    )
    record = await store.create_memory(
        ISSUER,
        SUBJECT,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content="family enjoys pizza",
        confidence=0.95,
        importance=0.8,
    )
    await store.attach_embedding(
        ISSUER,
        SUBJECT,
        record.id,
        revision_id=record.current_revision_id,
        generation_id=generation.id,
        vector=(1.0, 0.0),
        digest="a" * 64,
        model_id=generation.model_id,
        model_revision=generation.model_revision,
        model_digest=generation.model_digest,
    )
    await store.save_model_configuration(
        ISSUER,
        SUBJECT,
        MemoryModelConfiguration(
            ISSUER, SUBJECT, "extract", "test-embedder", embedding_generation=generation.id
        ),
    )
    await store.activate_embedding_generation(ISSUER, SUBJECT, generation.id)
    return store, generation, now


@pytest.mark.asyncio
async def test_recall_enforces_generation_scope_lifecycle_and_budget() -> None:
    store, generation, now = await _memory_store()
    policy = MemoryPolicy(uuid4(), AGENT, 1, fallback_relevance_threshold=0.99)
    result = await MemoryRecallService(store).recall(
        MemoryRecallRequest(
            ISSUER,
            SUBJECT,
            AGENT,
            "pizza",
            policy,
            now=now,
            context_token_budget=100,
            query_embedding=MemoryQueryEmbedding(
                (1.0, 0.0),
                generation.id,
                generation.model_id,
                generation.model_revision,
                generation.dimension,
            ),
        )
    )
    assert [item.content for item in result.candidates] == ["family enjoys pizza"]
    assert result.embedding_generation_id == generation.id
    assert all("content" not in item for item in result.metadata)
    assert len(result.candidates) <= 8

    expired = await store.create_memory(
        ISSUER,
        SUBJECT,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content="expired pizza fact",
        confidence=0.95,
        importance=0.8,
        valid_to=now - timedelta(days=1),
    )
    await store.attach_embedding(
        ISSUER,
        SUBJECT,
        expired.id,
        revision_id=expired.current_revision_id,
        generation_id=generation.id,
        vector=(1.0, 0.0),
        digest="c" * 64,
        model_id=generation.model_id,
        model_revision=generation.model_revision,
        model_digest=generation.model_digest,
    )

    disabled = next(iter(store.memories.values()))
    disabled.status = MemoryLifecycleStatus.DISABLED
    filtered = await MemoryRecallService(store).recall(
        MemoryRecallRequest(
            ISSUER,
            SUBJECT,
            AGENT,
            "pizza",
            policy,
            now=now,
            query_embedding=MemoryQueryEmbedding(
                (1.0, 0.0),
                generation.id,
                generation.model_id,
                generation.model_revision,
                generation.dimension,
            ),
        )
    )
    assert filtered.candidates == ()

    historical = await MemoryRecallService(store).recall(
        MemoryRecallRequest(
            ISSUER,
            SUBJECT,
            AGENT,
            "pizza",
            policy,
            now=now,
            historical=True,
            requested_historical_statuses=frozenset({MemoryLifecycleStatus.DISABLED}),
            query_embedding=MemoryQueryEmbedding(
                (1.0, 0.0),
                generation.id,
                generation.model_id,
                generation.model_revision,
                generation.dimension,
            ),
        )
    )
    assert historical.candidates
    assert all(item.scope_type is MemoryScopeType.USER for item in historical.candidates)


@pytest.mark.asyncio
async def test_off_policy_skips_generation_embedding_and_storage_lookup() -> None:
    class NoRecallRepository:
        async def get_active_embedding_generation(self, *_args: object) -> None:
            raise AssertionError("off recall must not resolve an embedding generation")

    events: list[MemoryRecallTelemetryEvent] = []

    class Collector:
        def record(self, event: MemoryRecallTelemetryEvent) -> None:
            events.append(event)

    agent = uuid4()
    policy = MemoryPolicy(agent, agent, 1, recall_mode=MemoryRecallMode.OFF)
    result = await MemoryRecallService(
        cast(MemoryRecallRepository, NoRecallRepository()), telemetry=Collector()
    ).recall(
        MemoryRecallRequest(ISSUER, SUBJECT, agent, "private task", policy)
    )

    assert result.candidates == ()
    assert result.gate_outcome == "skipped/policy_off"
    assert events and events[-1].outcome == "skipped"
    assert events[-1].gate_outcome == "policy_off"
    assert events[-1].recall_mode == "off"


@pytest.mark.asyncio
async def test_correction_excludes_prior_revision_until_reembedded() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = MemoryStore(clock=lambda: now)
    generation = await store.register_embedding_generation(
        ISSUER,
        SUBJECT,
        generation=1,
        model_id="test-embedder",
        model_revision="rev-1",
        dimension=2,
        model_digest="a" * 64,
    )
    record = await store.create_memory(
        ISSUER,
        SUBJECT,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content="family enjoys pizza",
        confidence=0.95,
        importance=0.8,
    )
    identity = {
        "model_id": generation.model_id,
        "model_revision": generation.model_revision,
        "model_digest": generation.model_digest,
    }
    await store.attach_embedding(
        ISSUER,
        SUBJECT,
        record.id,
        revision_id=record.current_revision_id,
        generation_id=generation.id,
        vector=(1.0, 0.0),
        digest="b" * 64,
        **identity,
    )
    await store.save_model_configuration(
        ISSUER,
        SUBJECT,
        MemoryModelConfiguration(
            ISSUER, SUBJECT, "extract", "test-embedder", embedding_generation=generation.id
        ),
    )
    await store.activate_embedding_generation(ISSUER, SUBJECT, generation.id)
    policy = MemoryPolicy(uuid4(), AGENT, 1)
    request = MemoryRecallRequest(
        ISSUER,
        SUBJECT,
        AGENT,
        "pizza",
        policy,
        now=now,
        query_embedding=MemoryQueryEmbedding(
            (1.0, 0.0),
            generation.id,
            generation.model_id,
            generation.model_revision,
            generation.dimension,
            model_digest=generation.model_digest,
        ),
    )
    assert (await MemoryRecallService(store).recall(request)).candidates

    revised = await store.revise_memory(
        ISSUER,
        SUBJECT,
        record.id,
        expected_version=record.version,
        content="family orders pizza weekly",
    )
    assert revised.current_revision_id != record.revisions[0].id
    assert (await MemoryRecallService(store).recall(request)).candidates == ()

    await store.attach_embedding(
        ISSUER,
        SUBJECT,
        record.id,
        revision_id=revised.current_revision_id,
        generation_id=generation.id,
        vector=(1.0, 0.0),
        digest="c" * 64,
        **identity,
    )
    assert (await MemoryRecallService(store).recall(request)).candidates


@pytest.mark.asyncio
async def test_recall_telemetry_is_metadata_only_and_non_blocking() -> None:
    store, generation, now = await _memory_store()
    events: list[MemoryRecallTelemetryEvent] = []

    class Collector:
        def record(self, event: MemoryRecallTelemetryEvent) -> None:
            events.append(event)

    policy = MemoryPolicy(uuid4(), AGENT, 1)
    request = MemoryRecallRequest(
        ISSUER,
        SUBJECT,
        AGENT,
        "pizza",
        policy,
        now=now,
        run_id=uuid4(),
        conversation_id=uuid4(),
        trace_id="trace-recall",
        parent_span_id="parent-span",
        query_embedding=MemoryQueryEmbedding(
            (1.0, 0.0),
            generation.id,
            generation.model_id,
            generation.model_revision,
            generation.dimension,
        ),
    )
    result = await MemoryRecallService(store, telemetry=Collector()).recall(request)
    assert result.candidates
    assert {event.operation for event in events} >= {
        "lexical",
        "vector",
        "rerank",
        "fusion",
        "selection",
    }
    assert all(event.trace_id == "trace-recall" for event in events)
    assert all(event.parent_span_id == "parent-span" for event in events)
    assert all(not hasattr(event, "content") for event in events)

    class Rejecting:
        def record(self, event: MemoryRecallTelemetryEvent) -> None:
            del event
            raise RuntimeError("telemetry sink unavailable")

    result = await MemoryRecallService(store, telemetry=Rejecting()).recall(request)
    assert result.candidates


@pytest.mark.asyncio
async def test_recall_production_telemetry_stages_have_zero_platform_rejections() -> None:
    store, _generation, now = await _memory_store()
    metrics = MetadataMetrics()
    events: list[MemoryRecallTelemetryEvent] = []

    class QueryEmbedding:
        async def embed_query(
            self, query: str, generation: MemoryEmbeddingGeneration
        ) -> MemoryQueryEmbedding:
            del query
            return MemoryQueryEmbedding(
                (1.0, 0.0),
                generation.id,
                generation.model_id,
                generation.model_revision,
                generation.dimension,
                model_digest=generation.model_digest,
            )

    class PlatformSink:
        def record(self, event: MemoryRecallTelemetryEvent) -> None:
            events.append(event)
            record_memory_retrieval(
                metrics,
                operation=event.operation,
                duration_ms=event.duration_ms,
                trace_id="a" * 32,
                parent_span_id="b" * 16,
                outcome=event.outcome,
                dependency=event.dependency,
                error_class=event.error_class,
                retrieval_stage=event.retrieval_stage,
                degradation=event.degradation,
                candidate_count=event.candidate_count,
                recall_count=event.recall_count,
                fallback_outcome=event.fallback_outcome,
                context_tokens=event.context_tokens,
            )

    policy = MemoryPolicy(uuid4(), AGENT, 1)
    result = await MemoryRecallService(
        store,
        embedding_port=QueryEmbedding(),
        telemetry=PlatformSink(),
    ).recall(
        MemoryRecallRequest(
            ISSUER,
            SUBJECT,
            AGENT,
            "pizza",
            policy,
            now=now,
            trace_id="a" * 32,
            parent_span_id="b" * 16,
        )
    )
    assert result.candidates
    assert {event.operation for event in events} >= {
        "query_embedding",
        "lexical",
        "vector",
        "fusion",
        "rerank",
        "selection",
        "retrieval",
        "fallback",
    }
    assert all(event.trace_id == "a" * 32 for event in events)
    assert all(event.parent_span_id == "b" * 16 for event in events)
    assert {event.dependency for event in events if event.operation == "query_embedding"} == {
        "embedding_provider"
    }
    assert {event.dependency for event in events if event.operation == "vector"} == {
        "memory_store"
    }
    assert sum(event.operation == "fallback" for event in events) == 1
    assert metrics.stats().rejected == 0


def test_sql_recall_uses_database_bounded_channels() -> None:
    source = Path(__file__).parents[1] / "src/aura_core/domains/knowledge/memory/repository.py"
    text = source.read_text()
    assert "to_tsvector" in text
    assert "<=> CAST(:recall_vector AS vector)" in text
    assert ".limit(min(MAX_RECALL_CANDIDATES" in text
    assert "MemoryEmbeddingRow.generation_id == generation.id" in text
    assert "MemoryEmbeddingRow.model_digest.is_not_distinct_from" in text


def test_recall_public_constants_and_policy_limits_are_deterministic() -> None:
    policy = MemoryPolicy(uuid4(), AGENT, 1)
    assert policy.max_memories == 8
    assert policy.context_budget_fraction == 0.2
    assert policy.fallback_agent_profile_ids == ()


@pytest.mark.asyncio
async def test_sql_recall_applies_owner_generation_and_exact_vector_filters() -> None:
    url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AURA_TEST_DATABASE_URL is required for live SQL recall coverage")
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "head")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if any(
            marker in str(getattr(exc, "orig", exc)).lower()
            for marker in ("connection refused", "could not connect", "timeout")
        ):
            pytest.skip(f"PostgreSQL dependency unavailable: {type(exc).__name__}")
        raise
    engine = make_engine(url)
    repository = SqlMemoryRepository(session_factory(engine))
    issuer, subject = f"{ISSUER}-{uuid4()}", f"{SUBJECT}-{uuid4()}"
    try:
        generation = await repository.register_embedding_generation(
            issuer,
            subject,
            generation=1,
            model_id="test-embedder",
            dimension=2,
        )
        memory = await repository.create_memory(
            issuer,
            subject,
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope(MemoryScopeType.USER),
            content="exact vector family fact",
            confidence=0.95,
            importance=0.8,
            idempotency_key=str(uuid4()),
        )
        await repository.attach_embedding(
            issuer,
            subject,
            memory.id,
            revision_id=memory.current_revision_id,
            generation_id=generation.id,
            vector=(1.0, 0.0),
            digest="b" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        )
        await repository.save_model_configuration(
            issuer,
            subject,
            MemoryModelConfiguration(
                issuer, subject, "extract", "test-embedder", embedding_generation=generation.id
            ),
        )
        await repository.activate_embedding_generation(issuer, subject, generation.id)
        scopes = ((MemoryScopeType.USER, None),)
        lexical = await repository.search_lexical(
            issuer,
            subject,
            query="exact vector",
            scopes=scopes,
            generation=generation,
            now=datetime.now(UTC),
            historical=False,
            statuses=frozenset({MemoryLifecycleStatus.ACTIVE}),
            limit=50,
        )
        vector = await repository.search_vector(
            issuer,
            subject,
            vector=(1.0, 0.0),
            generation=generation,
            scopes=scopes,
            now=datetime.now(UTC),
            historical=False,
            statuses=frozenset({MemoryLifecycleStatus.ACTIVE}),
            limit=50,
        )
        assert [item.id for item in lexical] == [memory.id]
        assert [item.id for item in vector] == [memory.id]
        assert (
            await repository.search_vector(
                issuer,
                "other-owner",
                vector=(1.0, 0.0),
                generation=generation,
                scopes=scopes,
                now=datetime.now(UTC),
                historical=False,
                statuses=frozenset({MemoryLifecycleStatus.ACTIVE}),
                limit=50,
            )
            == []
        )
    finally:
        await engine.dispose()
