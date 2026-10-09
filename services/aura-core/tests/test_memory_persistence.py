"""Persistence contract coverage for the AURA-0037 memory boundary.

The PostgreSQL cases are opt-in because the vector column intentionally cannot
be represented by SQLite.  They use the same disposable-database convention as
the other Core adapter tests.
"""

from __future__ import annotations

import os
from asyncio import gather
from collections.abc import AsyncIterator, Awaitable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import yaml
from aura_core.bootstrap.database import metadata
from aura_core.bootstrap.memory_uow import memory_repository
from aura_core.domains.interaction.agents.adapters.sql_store import SqlAgentStore
from aura_core.domains.interaction.agents.persistence import AgentProfileRow
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    ConfigurationIdempotencyConflict,
    ConfigurationVersionConflict,
    MemoryPolicy,
)
from aura_core.domains.knowledge.memory import persistence as _memory_mappings
from aura_core.domains.knowledge.memory.public import (
    PURGE_CONFIRMATION,
    CandidateState,
    MemoryAction,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingJob,
    MemoryRecord,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
)
from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository
from aura_core.platform.database.base import Base
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.telemetry import MetadataMetrics
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

ISSUER = "https://issuer.example"
OWNER = "owner"
OTHER_OWNER = "other-owner"


def _database_url() -> str | None:
    value = os.environ.get("AURA_TEST_DATABASE_URL")
    if not value or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        return None
    return value


@pytest_asyncio.fixture
async def sql_memory_store() -> AsyncIterator[tuple[SqlMemoryRepository, AsyncEngine]]:
    database_url = _database_url()
    if database_url is None:
        pytest.skip(
            "set AURA_TEST_DATABASE_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 "
            "for PostgreSQL memory persistence coverage"
        )
    # Importing the mapping above registers the vector-backed tables in Core
    # metadata; the migration owns extension creation in production.
    try:
        engine = make_engine(database_url)
    except ModuleNotFoundError as exc:
        pytest.skip(f"PostgreSQL async driver unavailable: {exc.name}")
    sessions = session_factory(engine)
    initialized = False
    try:
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await connection.run_sync(Base.metadata.create_all)
                initialized = True
        except (DBAPIError, OperationalError) as exc:
            pytest.skip(f"PostgreSQL memory schema unavailable: {type(exc).__name__}")
        yield SqlMemoryRepository(sessions), engine
    finally:
        if initialized:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _create(
    store: MemoryStore | SqlMemoryRepository, *, subject: str = OWNER, **kwargs: object
) -> MemoryRecord:
    return await store.create_memory(
        ISSUER,
        subject,
        content=kwargs.pop("content", "The owner prefers concise answers."),
        kind=MemoryKind.PREFERENCE,
        scope=kwargs.pop("scope", MemoryScope(MemoryScopeType.USER)),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        **kwargs,
    )


async def _candidate(
    store: SqlMemoryRepository,
    *,
    subject: str = OWNER,
    action: MemoryAction = MemoryAction.CREATE,
    content: str | None = "The owner prefers tea.",
    related_memory_id: UUID | None = None,
    importance: float | None = 0.8,
    half_life_days: float | None = 30,
) -> MemoryCandidate:
    job = MemoryProcessingJob(uuid4(), ISSUER, subject, uuid4(), uuid4())
    await store.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(),
        job.id,
        ISSUER,
        subject,
        action,
        content,
        MemoryKind.PREFERENCE if content is not None else None,
        MemoryScope(MemoryScopeType.USER) if content is not None else None,
        0.9,
        importance=importance if content is not None else None,
        half_life_days=half_life_days if content is not None else None,
        related_memory_id=related_memory_id,
    )
    return await store.persist_candidate(candidate)


def test_memory_mapping_is_core_owned_and_keeps_sensitive_values_out_of_audit_rows() -> None:
    assert _memory_mappings.MemoryRow.__tablename__ == "memories"
    tables = set(metadata().tables)
    assert {
        "memories",
        "memory_revisions",
        "memory_provenance",
        "memory_embeddings",
        "memory_relations",
        "memory_purge_audit",
    } <= tables
    audit_columns = {column.name for column in metadata().tables["memory_purge_audit"].columns}
    assert {
        "memory_id",
        "principal_issuer",
        "principal_subject",
        "action",
        "created_at",
    } <= audit_columns
    assert "content" not in audit_columns
    assert "evidence" not in audit_columns
    assert "vector" not in audit_columns


def test_memory_manifest_uses_the_same_revision_trace_attribute_as_code() -> None:
    manifest_path = (
        Path(__file__).parents[1]
        / "resources"
        / "component-manifests"
        / "memory-persistence.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text())
    trace_only = set(manifest["cardinality"]["trace_only"])
    assert "memory_revision_id" in trace_only
    assert "revision_id" not in trace_only


@pytest.mark.asyncio
async def test_composed_memory_repository_emits_correlated_metrics_for_success_and_errors() -> None:
    metrics = MetadataMetrics()
    store = memory_repository(None, testing=True, metrics=metrics)
    content = "The owner prefers concise answers."
    start = len(metrics.snapshot())
    created = await store.create_memory(
        ISSUER,
        OWNER,
        content=content,
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        idempotency_key="composed-create",
    )
    operations: list[tuple[str, int, int]] = [("success", start, len(metrics.snapshot()))]
    start = len(metrics.snapshot())
    with pytest.raises(MemoryIdempotencyConflict):
        await store.create_memory(
            ISSUER,
            OWNER,
            content="changed payload",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
            idempotency_key="composed-create",
        )
    operations.append(("idempotency", start, len(metrics.snapshot())))
    start = len(metrics.snapshot())
    with pytest.raises(MemoryNotFound):
        await store.get_memory(ISSUER, OWNER, uuid4())
    operations.append(("not_found", start, len(metrics.snapshot())))
    start = len(metrics.snapshot())
    with pytest.raises(MemoryValidationError):
        await store.create_memory(
            ISSUER,
            OWNER,
            content="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
            idempotency_key="composed-secret",
        )
    operations.append(("validation", start, len(metrics.snapshot())))

    expected_metrics = {
        "memory_operation_duration_ms",
        "memory_operation_outcome",
        "memory_lifecycle_status",
        "memory_scope_type",
    }
    for operation, start, end in operations:
        measurements = metrics.snapshot()[start:end]
        trace_ids = {item.trace_id for item in measurements if item.trace_id is not None}
        assert len(trace_ids) == 1, operation
        assert expected_metrics <= {item.metric for item in measurements}
        for item in measurements:
            assert content not in repr(item)
        spans = [item for item in measurements if item.kind == "span"]
        assert len(spans) == 1
        attrs = dict(spans[0].trace_attributes)
        assert "revision_id" not in attrs
        if operation == "success":
            assert attrs["memory_revision_id"] == str(created.current_revision_id)
        else:
            assert dict(spans[0].dimensions)["error_class"] == operation


@pytest.mark.asyncio
async def test_composed_repository_traces_bounded_operations_and_identifiers_separately() -> None:
    metrics = MetadataMetrics()
    store = memory_repository(None, testing=True, metrics=metrics)

    async def invoke(operation: str, action: Awaitable[object]) -> tuple[dict[str, str], object]:
        start = len(metrics.snapshot())
        result = await action
        spans = [item for item in metrics.snapshot()[start:] if item.kind == "span"]
        assert len(spans) == 1
        attributes = dict(spans[0].trace_attributes)
        assert attributes["operation"] == operation
        assert all(
            value not in repr(spans[0])
            for value in ("private memory text", "secret credential")
        )
        dimensions = dict(spans[0].dimensions)
        assert set(dimensions) <= {
            "dependency",
            "outcome",
            "error_class",
        }
        assert not any(key.endswith("_id") for key in dimensions)
        return attributes, result

    async def invoke_authorization_failure(action: Awaitable[object]) -> None:
        start = len(metrics.snapshot())
        with pytest.raises(MemoryScopeAuthorizationRequired):
            await action
        measurements = metrics.snapshot()[start:]
        spans = [item for item in measurements if item.kind == "span"]
        assert len(spans) == 1
        attributes = dict(spans[0].trace_attributes)
        assert attributes["operation"] == "memory.create"
        assert dict(spans[0].dimensions)["error_class"] in {"authorization", "policy"}
        assert dict(spans[0].dimensions)["error_class"] != "persistence"
        assert all("unauthorized private memory" not in repr(item) for item in measurements)
        assert {
            "memory_operation_duration_ms",
            "memory_operation_outcome",
            "memory_lifecycle_status",
            "memory_scope_type",
        } <= {item.metric for item in measurements}

    create_attributes, create_result = await invoke(
        "memory.create",
        store.create_memory(
            ISSUER,
            OWNER,
            content="private memory text",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
            idempotency_key="trace-create",
        ),
    )
    memory = cast(MemoryRecord, create_result)
    historical_revision_id = memory.current_revision_id
    assert create_attributes["memory_id"] == str(memory.id)
    assert create_attributes["memory_revision_id"] == str(historical_revision_id)
    unauthorized_agent = uuid4()
    await invoke_authorization_failure(
        store.create_memory(
            ISSUER,
            OWNER,
            content="unauthorized private memory",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope(MemoryScopeType.AGENT, unauthorized_agent),
            authorized_agent_ids=frozenset({uuid4()}),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
            idempotency_key="trace-unauthorized-create",
        )
    )
    await invoke("memory.list", store.list_memories(ISSUER, OWNER, MemoryFilters()))
    await invoke("memory.get", store.get_memory(ISSUER, OWNER, memory.id))
    _, revised_result = await invoke(
        "memory.revise",
        store.revise_memory(
            ISSUER,
            OWNER,
            memory.id,
            content="private memory text revised",
            reason="owner correction",
            expected_version=1,
            idempotency_key="trace-revise",
        ),
    )
    revised = cast(MemoryRecord, revised_result)
    _, revised_result = await invoke(
        "memory.status",
        store.set_status(
            ISSUER,
            OWNER,
            memory.id,
            status=MemoryLifecycleStatus.DISABLED,
            expected_version=2,
            idempotency_key="trace-status",
        ),
    )
    revised = cast(MemoryRecord, revised_result)
    _, revised_result = await invoke(
        "memory.pin",
        store.set_pinned(
            ISSUER,
            OWNER,
            memory.id,
            pinned=True,
            expected_version=3,
            idempotency_key="trace-pin",
        ),
    )
    revised = cast(MemoryRecord, revised_result)
    assert revised.version == 4
    generation_attributes, generation_result = await invoke(
        "memory.embedding.register",
        store.register_embedding_generation(
            ISSUER,
            OWNER,
            generation=1,
            model_id="embedding-model",
            model_revision="rev-1",
            dimension=3,
            model_digest="a" * 64,
        ),
    )
    generation = cast(MemoryEmbeddingGeneration, generation_result)
    assert "memory_id" not in generation_attributes
    embedding_attributes, _ = await invoke(
        "memory.embedding.attach",
        store.attach_embedding(
            ISSUER,
            OWNER,
            memory.id,
            generation_id=generation.id,
            revision_id=historical_revision_id,
            vector=(0.1, 0.2, 0.3),
            digest="b" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        ),
    )
    assert embedding_attributes["memory_id"] == str(memory.id)
    assert embedding_attributes["memory_revision_id"] == str(historical_revision_id)
    _, _ = await invoke(
        "memory.embedding.attach",
        store.attach_embedding(
            ISSUER,
            OWNER,
            memory.id,
            generation_id=generation.id,
            revision_id=revised.current_revision_id,
            vector=(0.1, 0.2, 0.3),
            digest="c" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        ),
    )
    activation_attributes, _ = await invoke(
        "memory.embedding.activate",
        store.activate_embedding_generation(ISSUER, OWNER, generation.id),
    )
    assert "memory_id" not in activation_attributes
    assert activation_attributes["generation_id"] == str(generation.id)
    purge_attributes, _ = await invoke(
        "memory.purge",
        store.purge(
            ISSUER,
            OWNER,
            memory.id,
            confirmation=PURGE_CONFIRMATION,
            expected_version=4,
            idempotency_key="trace-purge",
        ),
    )
    assert purge_attributes["memory_id"] == str(memory.id)


@pytest.mark.asyncio
async def test_sql_repository_matches_in_memory_owner_scope_and_immutable_revision_contract(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, _ = sql_memory_store
    stores: tuple[MemoryStore | SqlMemoryRepository, ...] = (MemoryStore(), sql_store)
    for store in stores:
        memory = await _create(store)
        corrected = await store.revise_memory(
            ISSUER,
            OWNER,
            memory.id,
            content="The owner prefers detailed answers.",
            expected_version=1,
        )
        assert corrected.version == 2
        assert [revision.content for revision in corrected.revisions] == [
            "The owner prefers concise answers.",
            "The owner prefers detailed answers.",
        ]
        assert await store.list_memories(ISSUER, OTHER_OWNER) == []
        agent_memory = await _create(
            store,
            scope=MemoryScope(MemoryScopeType.AGENT, uuid4()),
            content="Agent-private record.",
        )
        with pytest.raises(MemoryValidationError):
            MemoryFilters(scope_type=None)  # type: ignore[arg-type]
        with pytest.raises(MemoryNotFound):
            await store.get_memory(ISSUER, OWNER, agent_memory.id, scope_type=None)
        resolved_agent = await store.get_memory(
            ISSUER,
            OWNER,
            agent_memory.id,
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_memory.scope.agent_profile_id,
        )
        assert resolved_agent.id == agent_memory.id
        with pytest.raises(MemoryNotFound):
            await store.get_memory(ISSUER, OTHER_OWNER, agent_memory.id, scope_type=None)
        with pytest.raises(MemoryNotFound):
            await store.get_memory(ISSUER, OTHER_OWNER, memory.id)


@pytest.mark.asyncio
async def test_status_and_pin_idempotent_replays_recheck_scope_in_both_adapters(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, _ = sql_memory_store
    for store in (MemoryStore(), sql_store):
        agent_id, wrong_agent = uuid4(), uuid4()
        memory = await _create(
            store,
            scope=MemoryScope(MemoryScopeType.AGENT, agent_id),
            content="Private agent state.",
        )
        await store.set_status(
            ISSUER,
            OWNER,
            memory.id,
            status=MemoryLifecycleStatus.DISABLED,
            expected_version=1,
            idempotency_key="scope-status-replay",
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_id,
            authorized_agent_ids=frozenset({agent_id}),
        )
        for scope_type, profile_id in (
            (MemoryScopeType.USER, None),
            (MemoryScopeType.AGENT, wrong_agent),
        ):
            with pytest.raises(MemoryNotFound):
                await store.set_status(
                    ISSUER,
                    OWNER,
                    memory.id,
                    status=MemoryLifecycleStatus.DISABLED,
                    expected_version=1,
                    idempotency_key="scope-status-replay",
                    scope_type=scope_type,
                    agent_profile_id=profile_id,
                    authorized_agent_ids=frozenset({wrong_agent}),
                )

        pinned = await store.set_pinned(
            ISSUER,
            OWNER,
            memory.id,
            pinned=True,
            expected_version=2,
            idempotency_key="scope-pin-replay",
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_id,
            authorized_agent_ids=frozenset({agent_id}),
        )
        assert pinned.pinned is True
        with pytest.raises(MemoryNotFound):
            await store.set_pinned(
                ISSUER,
                OWNER,
                memory.id,
                pinned=True,
                expected_version=2,
                idempotency_key="scope-pin-replay",
                scope_type=MemoryScopeType.AGENT,
                agent_profile_id=wrong_agent,
                authorized_agent_ids=frozenset({wrong_agent}),
                )


@pytest.mark.asyncio
async def test_sql_concurrent_same_key_create_and_versioned_mutation_replay_exactly(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _engine = sql_memory_store

    async def create() -> MemoryRecord:
        return await _create(
            store,
            content="Concurrent idempotent memory.",
            idempotency_key="concurrent-create",
        )

    created = await gather(create(), create())
    assert created[0].id == created[1].id  # type: ignore[attr-defined]
    memory = created[0]
    revised = await store.revise_memory(
        ISSUER,
        OWNER,
        memory.id,  # type: ignore[attr-defined]
        content="Concurrent memory corrected once.",
        reason="owner correction",
        expected_version=1,
        idempotency_key="concurrent-correction",
    )
    replay = await store.revise_memory(
        ISSUER,
        OWNER,
        memory.id,  # type: ignore[attr-defined]
        content="Concurrent memory corrected once.",
        reason="owner correction",
        expected_version=1,
        idempotency_key="concurrent-correction",
    )
    assert replay == revised
    with pytest.raises(MemoryIdempotencyConflict):
        await store.revise_memory(
            ISSUER,
            OWNER,
            memory.id,  # type: ignore[attr-defined]
            content="Changed payload must not replay.",
            reason="owner correction",
            expected_version=2,
            idempotency_key="concurrent-correction",
        )


@pytest.mark.asyncio
async def test_correction_reason_secret_rejection_matches_adapters(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, _ = sql_memory_store
    for store in (MemoryStore(), sql_store):
        memory = await _create(store)
        with pytest.raises(MemoryValidationError):
            await store.revise_memory(
                ISSUER,
                OWNER,
                memory.id,
                content="The owner prefers detailed answers.",
                reason="api_key: must not be stored",
                expected_version=1,
            )


@pytest.mark.asyncio
async def test_relations_require_exact_scope_and_created_at_survives_later_mutations(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, _ = sql_memory_store
    for store in (MemoryStore(), sql_store):
        agent_a, agent_b = uuid4(), uuid4()
        source = await _create(store)
        related = await _create(store, content="User-scoped related fact.")
        linked = await store.set_status(
            ISSUER,
            OWNER,
            source.id,
            status=MemoryLifecycleStatus.DISPUTED,
            related_memory_id=related.id,
            expected_version=1,
        )
        relation_created_at = linked.relations[0].created_at
        await store.set_pinned(ISSUER, OWNER, source.id, pinned=True, expected_version=2)
        corrected = await store.revise_memory(
            ISSUER,
            OWNER,
            source.id,
            content="The owner prefers detailed answers.",
            expected_version=3,
        )
        assert corrected.relations[0].created_at == relation_created_at

        invalid_pairs = (
            (MemoryScope(MemoryScopeType.USER), MemoryScope(MemoryScopeType.AGENT, agent_a)),
            (MemoryScope(MemoryScopeType.AGENT, agent_a), MemoryScope(MemoryScopeType.USER)),
            (
                MemoryScope(MemoryScopeType.AGENT, agent_a),
                MemoryScope(MemoryScopeType.AGENT, agent_b),
            ),
        )
        for index, (source_scope, related_scope) in enumerate(invalid_pairs):
            invalid_source = await _create(store, scope=source_scope, content=f"source {index}")
            invalid_related = await _create(store, scope=related_scope, content=f"related {index}")
            with pytest.raises(MemoryNotFound):
                await store.set_status(
                    ISSUER,
                    OWNER,
                    invalid_source.id,
                    status=MemoryLifecycleStatus.DISPUTED,
                    related_memory_id=invalid_related.id,
                    expected_version=1,
                    scope_type=source_scope.type,
                    agent_profile_id=source_scope.agent_profile_id,
                    authorized_agent_ids=frozenset({agent_a, agent_b}),
                )


@pytest.mark.asyncio
async def test_sql_purge_removes_revision_provenance_and_embeddings_transactionally(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, engine = sql_memory_store
    memory = await _create(store)
    audit = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
    )
    assert audit.memory_id == memory.id
    with pytest.raises(MemoryNotFound):
        await store.get_memory(ISSUER, OWNER, memory.id)
    async with engine.connect() as connection:
        for table in ("memories", "memory_revisions", "memory_provenance", "memory_embeddings"):
            query = (
                "SELECT COUNT(*) FROM memory_embeddings e "
                "JOIN memory_revisions r ON r.id = e.revision_id "
                "WHERE r.memory_id = :id"
                if table == "memory_embeddings"
                else f"SELECT COUNT(*) FROM {table} WHERE "
                + ("id = :id" if table == "memories" else "memory_id = :id")
            )
            assert (
                await connection.execute(
                    text(query),
                    {"id": memory.id},
                )
            ).scalar_one() == 0
        assert (
            await connection.execute(
                text("SELECT action FROM memory_purge_audit WHERE memory_id = :id"),
                {"id": memory.id},
            )
        ).scalar_one() == "purge"


@pytest.mark.asyncio
async def test_sql_owner_optimistic_version_prevents_lost_updates(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    memory = await _create(store)
    await store.set_pinned(ISSUER, OWNER, memory.id, pinned=True, expected_version=1)
    with pytest.raises(MemoryVersionConflict):
        await store.set_status(
            ISSUER,
            OWNER,
            memory.id,
            status="disabled",
            expected_version=1,
        )


@pytest.mark.asyncio
async def test_sql_and_memory_embedding_metadata_are_per_revision_and_generation_unique(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, _ = sql_memory_store
    for store in (MemoryStore(), sql_store):
        memory = await _create(store)
        generation = await store.register_embedding_generation(
            ISSUER,
            OWNER,
            generation=1,
            model_id="embedding-model",
            model_revision="rev-1",
            dimension=3,
            model_digest="b" * 64,
        )
        with pytest.raises(MemoryValidationError):
            await store.register_embedding_generation(
                ISSUER,
                OWNER,
                generation=1,
                model_id="other-embedding-model",
                model_revision="rev-2",
                dimension=3,
                model_digest="d" * 64,
            )
        current = await store.attach_embedding(
            ISSUER,
            OWNER,
            memory.id,
            generation_id=generation.id,
            revision_id=memory.current_revision_id,
            vector=(0.1, 0.2, 0.3),
            digest="c" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        )
        assert len(current.embeddings) == 1
        assert current.embeddings[0].generation == 1
        assert current.embeddings[0].revision_id == current.current_revision_id
        assert current.embeddings[0].dimension == len(current.embeddings[0].vector or ())
        assert len({item.generation for item in current.embeddings}) == len(current.embeddings)
        foreign = await _create(store, content="Foreign memory revision.")
        with pytest.raises(MemoryNotFound):
            await store.attach_embedding(
                ISSUER,
                OWNER,
                memory.id,
                generation_id=generation.id,
                revision_id=foreign.current_revision_id,
                vector=(0.1, 0.2, 0.3),
                digest="e" * 64,
                model_id=generation.model_id,
                model_revision=generation.model_revision,
                model_digest=generation.model_digest,
            )


@pytest.mark.asyncio
async def test_non_finite_embedding_vectors_are_rejected_before_adapter_mutation(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    sql_store, engine = sql_memory_store
    for store in (MemoryStore(), sql_store):
        generation = await store.register_embedding_generation(
            ISSUER,
            OWNER,
            generation=1,
            model_id="embedding-model",
            model_revision="rev-1",
            dimension=3,
            model_digest="f" * 64,
        )
        for index, invalid in enumerate((float("nan"), float("inf"), float("-inf"))):
            memory = await _create(store, content=f"Non-finite candidate {index}.")
            with pytest.raises(MemoryValidationError):
                await store.attach_embedding(
                    ISSUER,
                    OWNER,
                    memory.id,
                    generation_id=generation.id,
                    vector=(invalid, 0.2, 0.3),
                    digest=f"{index + 1:064x}",
                    model_id=generation.model_id,
                    model_revision=generation.model_revision,
                    model_digest=generation.model_digest,
                )
            current = await store.get_memory(ISSUER, OWNER, memory.id)
            assert current.embeddings == []
            if isinstance(store, SqlMemoryRepository):
                async with engine.connect() as connection:
                    count = await connection.execute(
                        text(
                            "SELECT COUNT(*) FROM memory_embeddings e "
                            "JOIN memory_revisions r ON r.id = e.revision_id "
                            "WHERE r.memory_id = :id"
                        ),
                        {"id": memory.id},
                    )
                    assert count.scalar_one() == 0


@pytest.mark.asyncio
async def test_sql_search_filters_before_limit(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    await _create(store, content="target phrase appears in the older memory")
    await _create(store, content="newer unrelated memory one")
    await _create(store, content="newer unrelated memory two")
    matches = await store.list_memories(
        ISSUER,
        OWNER,
        filters=MemoryFilters(q="target phrase", limit=1),
    )
    assert len(matches) == 1
    assert matches[0].content == "target phrase appears in the older memory"


@pytest.mark.asyncio
async def test_sql_purge_replays_scrubbed_receipt_and_removes_old_commands(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    memory = await _create(store)
    key = "purge-key"
    receipt = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
        idempotency_key=key,
    )
    replay = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
        idempotency_key=key,
    )
    assert replay == receipt
    with pytest.raises(MemoryIdempotencyConflict):
        await store.purge(
            ISSUER,
            OWNER,
            memory.id,
            confirmation=PURGE_CONFIRMATION,
            expected_version=2,
            idempotency_key=key,
        )
    async with sql_memory_store[1].connect() as connection:
        assert (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM memory_command_idempotency "
                    "WHERE memory_id = :id AND audit_id IS NULL"
                ),
                {"id": memory.id},
            )
            ).scalar_one() == 0


@pytest.mark.asyncio
async def test_postgres_terminal_processing_rows_are_content_free_and_owner_scoped(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    """Terminal worker metadata must survive without retaining turn content."""

    store, engine = sql_memory_store
    jobs = [
        MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4()),
        MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4()),
        MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4()),
    ]
    for job in jobs:
        await store.enqueue_processing_job(job)
        loaded = await store.get_processing_job(job.id, ISSUER, OWNER)
        assert loaded.id == job.id
        with pytest.raises(MemoryNotFound):
            await store.get_processing_job(job.id, ISSUER, OTHER_OWNER)
        claimed = await store.claim_processing_job_by_id(job.id, ISSUER, OWNER)
        assert claimed is not None
        with pytest.raises(MemoryValidationError):
            await store.settle_processing_job(
                job.id, uuid4(), issuer=ISSUER, subject=OWNER
            )
        with pytest.raises(TypeError):
            await store.settle_processing_job(job.id, claimed.lease_id)  # type: ignore[arg-type]
        with pytest.raises(MemoryValidationError):
            await store.settle_processing_job(
                job.id,
                claimed.lease_id,  # type: ignore[arg-type]
                issuer=ISSUER,
                subject=OTHER_OWNER,
            )
        await store.settle_processing_job(
            job.id,
            claimed.lease_id,  # type: ignore[arg-type]
            issuer=ISSUER,
            subject=OWNER,
            retryable=True,
            error_class="unconfigured" if job is jobs[0] else "provider",
        )
    for action, state, reason in (
        (MemoryAction.IGNORE, CandidateState.REJECTED, "ignored"),
        (MemoryAction.CREATE, CandidateState.REJECTED, "low_confidence"),
        (MemoryAction.REVIEW, CandidateState.RETRYABLE, "malformed"),
        (MemoryAction.CREATE, CandidateState.REJECTED, "credential"),
    ):
        await store.persist_candidate(
            MemoryCandidate(
                uuid4(), jobs[0].id, ISSUER, OWNER, action, None, None, None,
                0.1, state=state, decision_reason=reason,
            )
        )
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT content, decision_reason, state FROM memory_candidates "
                    "WHERE principal_issuer = :issuer AND principal_subject = :subject"
                ),
                {"issuer": ISSUER, "subject": OWNER},
            )
        ).all()
    assert len(rows) == 4
    assert all(row.content is None for row in rows)
    assert {row.decision_reason for row in rows} == {
        "ignored", "low_confidence", "malformed", "credential"
    }
    assert await store.claim_processing_job_by_id(jobs[0].id, ISSUER, OTHER_OWNER) is None
    assert await store.get_candidate_for_job(jobs[0].id, ISSUER, OTHER_OWNER) is None


@pytest.mark.asyncio
async def test_postgres_claim_paths_preserve_pinned_policy_snapshot(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _engine = sql_memory_store
    policy_by_id = uuid4()
    policy_general = uuid4()
    first = MemoryProcessingJob(
        uuid4(), ISSUER, OWNER, uuid4(), uuid4(),
        allow_shared_user_promotion=True,
        memory_policy_revision_id=policy_by_id,
    )
    second = MemoryProcessingJob(
        uuid4(), ISSUER, OWNER, uuid4(), uuid4(),
        allow_shared_user_promotion=False,
        memory_policy_revision_id=policy_general,
    )
    await store.enqueue_processing_job(first)
    await store.enqueue_processing_job(second)
    claimed_by_id = await store.claim_processing_job_by_id(first.id, ISSUER, OWNER)
    assert claimed_by_id is not None
    assert claimed_by_id.allow_shared_user_promotion is True
    assert claimed_by_id.memory_policy_revision_id == policy_by_id
    claimed_general = await store.claim_processing_job(ISSUER, OWNER)
    assert claimed_general is not None
    assert claimed_general.allow_shared_user_promotion is False
    assert claimed_general.memory_policy_revision_id == policy_general


@pytest.mark.asyncio
async def test_postgres_generation_owner_numbering_first_generation_completeness_and_fences(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    memory = await _create(store, content="A retained fact.")
    owner_generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embed-owner", dimension=2, model_digest="a" * 64
    )
    foreign_generation = await store.register_embedding_generation(
        ISSUER,
        OTHER_OWNER,
        generation=1,
        model_id="embed-other",
        dimension=2,
        model_digest="b" * 64,
    )
    assert owner_generation.generation == foreign_generation.generation == 1
    with pytest.raises(MemoryNotFound):
        await store.get_embedding_generation(ISSUER, OTHER_OWNER, owner_generation.id)
    with pytest.raises(MemoryValidationError):
        await store.activate_embedding_generation(ISSUER, OWNER, owner_generation.id)
    with pytest.raises(MemoryNotFound):
        await store.activate_embedding_generation(ISSUER, OTHER_OWNER, owner_generation.id)
    await store.attach_embedding(
        ISSUER, OWNER, memory.id, revision_id=memory.current_revision_id,
        generation_id=owner_generation.id, vector=(0.1, 0.2), digest="c" * 64,
        model_id=owner_generation.model_id, model_revision=owner_generation.model_revision,
        model_digest=owner_generation.model_digest,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, owner_generation.id)
    with pytest.raises(MemoryNotFound):
        await store.attach_embedding(
            ISSUER, OTHER_OWNER, memory.id, revision_id=memory.current_revision_id,
            generation_id=owner_generation.id, vector=(0.1, 0.2), digest="d" * 64,
            model_id=owner_generation.model_id, model_revision=owner_generation.model_revision,
            model_digest=owner_generation.model_digest,
        )


@pytest.mark.asyncio
async def test_postgres_embedding_queue_is_idempotent_and_retry_rows_remain_owner_scoped(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    memory = await _create(store, content="A queueable fact.")
    generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=2, model_digest="e" * 64
    )
    await store.attach_embedding(
        ISSUER, OWNER, memory.id, revision_id=memory.current_revision_id,
        generation_id=generation.id, vector=(0.1, 0.2), digest="f" * 64,
        model_id=generation.model_id, model_revision=generation.model_revision,
        model_digest=generation.model_digest,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    first = await store.queue_embedding_job(
        ISSUER, OWNER, memory_id=memory.id, revision_id=memory.current_revision_id,
        generation_id=generation.id,
    )
    replay = await store.queue_embedding_job(
        ISSUER, OWNER, memory_id=memory.id, revision_id=memory.current_revision_id,
        generation_id=generation.id,
    )
    assert replay.id == first.id
    # Install the deterministic clock after queue creation so the initial
    # claim is not defeated by sub-millisecond client/DB wall-clock skew.
    fake_now = [datetime.now(UTC)]
    store.set_clock_for_testing(lambda: fake_now[0])
    initial_claim = await store.claim_embedding_job(ISSUER, OWNER, lease_seconds=30)
    assert initial_claim is not None and initial_claim.lease_id is not None
    retry = await store.settle_embedding_job(
        initial_claim.id,
        issuer=ISSUER,
        subject=OWNER,
        lease_id=initial_claim.lease_id,
        retryable=True,
        error_class="provider",
    )
    assert retry.status is ProcessingJobStatus.RETRYABLE
    # Production retry pacing remains future-dated; advance a deterministic
    # test clock instead of weakening the backoff for immediate reclaims.
    fake_now[0] += timedelta(seconds=3)
    claimed = await store.claim_embedding_job(ISSUER, OWNER, lease_seconds=30)
    assert claimed is not None
    assert claimed.status is ProcessingJobStatus.RUNNING
    assert claimed.lease_id is not None
    assert await store.claim_embedding_job(ISSUER, OTHER_OWNER, lease_seconds=30) is None
    with pytest.raises(MemoryNotFound):
        await store.settle_embedding_job(
            claimed.id, issuer=ISSUER, subject=OTHER_OWNER,
            lease_id=claimed.lease_id, retryable=True,
            error_class="provider",
        )
    with pytest.raises(TypeError):
        await cast(Any, store).settle_embedding_job(
            claimed.id, lease_id=claimed.lease_id, retryable=True,
            error_class="provider",
        )
    completed = await store.settle_embedding_job(
        claimed.id, issuer=ISSUER, subject=OWNER, lease_id=claimed.lease_id
    )
    assert completed.status is ProcessingJobStatus.COMPLETED
    with pytest.raises(MemoryNotFound):
        await store.settle_embedding_job(
            uuid4(), issuer=ISSUER, subject=OWNER,
            lease_id=uuid4(),
            retryable=True, error_class="provider",
        )


@pytest.mark.asyncio
async def test_postgres_purge_scrubs_linked_candidate_outcome_embedding_and_retry_fence(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, engine = sql_memory_store
    job = MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4())
    memory = await _create(
        store,
        content="Purge fence fact.",
        idempotency_key=f"memory-job:{job.id}",
    )
    generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=2, model_digest="f" * 64
    )
    await store.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(), job.id, ISSUER, OWNER, MemoryAction.REINFORCE,
        "Purge candidate content", MemoryKind.PREFERENCE,
        MemoryScope(MemoryScopeType.USER), 0.9,
        state=CandidateState.ACCEPTED, decision_reason="reinforced",
        related_memory_id=memory.id,
    )
    await store.persist_candidate(candidate)
    await store.record_action_outcome(
        candidate_id=candidate.id,
        job_id=job.id,
        issuer=ISSUER,
        subject=OWNER,
        action="reinforce",
        outcome="created",
    )
    embedding_job = await store.queue_embedding_job(
        ISSUER, OWNER, memory_id=memory.id, revision_id=memory.current_revision_id,
        generation_id=generation.id,
    )
    await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=memory.version,
        idempotency_key="purge-linked-work",
    )
    async with engine.connect() as connection:
        counts = {}
        for table, column, value in (
            ("memory_candidates", "id", candidate.id),
            ("memory_action_outcomes", "candidate_id", candidate.id),
            ("memory_embedding_jobs", "id", embedding_job.id),
            ("memory_purge_fences", "memory_id", memory.id),
            ("memory_command_idempotency", "memory_id", memory.id),
        ):
            counts[table] = (
                await connection.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :value"),
                    {"value": value},
                )
            ).scalar_one()
    assert counts["memory_candidates"] == 0
    assert counts["memory_action_outcomes"] == 0
    assert counts["memory_embedding_jobs"] == 0
    assert counts["memory_purge_fences"] == 1
    assert counts["memory_command_idempotency"] == 0
    assert await store.is_processing_command_purged(ISSUER, OWNER, job.id) is True
    assert await store.is_processing_command_purged(ISSUER, OWNER, uuid4()) is False
    # The delayed processing job must not recreate the purged memory.
    assert await store.get_processing_job(job.id, ISSUER, OWNER)


@pytest.mark.asyncio
async def test_sql_candidate_decisions_replay_conflict_scrub_and_relations(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    candidate = await _candidate(store)
    first = await store.reject_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        reason="not durable",
        idempotency_key="candidate-reject-replay",
    )
    replay = await store.reject_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        reason="not durable",
        idempotency_key="candidate-reject-replay",
    )
    assert replay == first
    assert first.state is CandidateState.REJECTED
    assert first.content is None
    assert first.grounded_message_ids == ()
    with pytest.raises(MemoryIdempotencyConflict):
        await store.reject_candidate(
            ISSUER,
            OWNER,
            candidate.id,
            expected_version=1,
            reason="changed reason",
            idempotency_key="candidate-reject-replay",
        )

    secret_candidate = await _candidate(store)
    secret_edit = {
        "content": "api_key: do-not-persist",
        "action": "create",
        "kind": "preference",
        "scope": {"type": "user"},
        "confidence": 0.9,
        "importance": 0.8,
        "halfLifeDays": 30,
        "validTo": None,
        "relatedMemoryId": None,
    }
    with pytest.raises(MemoryValidationError):
        await store.approve_candidate(
            ISSUER,
            OWNER,
            secret_candidate.id,
            expected_version=1,
            edit=secret_edit,
            idempotency_key="candidate-secret-edit",
        )
    reason_candidate = await _candidate(store)
    with pytest.raises(MemoryValidationError):
        await store.reject_candidate(
            ISSUER,
            OWNER,
            reason_candidate.id,
            expected_version=1,
            reason="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret.signature",
            idempotency_key="candidate-secret-reason",
        )

    disputed_memory = await _create(store, content="The owner likes tea.")
    disputed = await _candidate(
        store,
        action=MemoryAction.DISPUTE,
        content="The owner does not like tea.",
        related_memory_id=disputed_memory.id,
    )
    disputed_result = await store.approve_candidate(
        ISSUER,
        OWNER,
        disputed.id,
        expected_version=1,
        idempotency_key="candidate-dispute",
    )
    assert disputed_result.state is CandidateState.ACCEPTED
    current_disputed = await store.get_memory(ISSUER, OWNER, disputed_memory.id)
    assert current_disputed.status is MemoryLifecycleStatus.DISPUTED

    superseded_memory = await _create(store, content="The owner prefers coffee.")
    superseding = await _candidate(
        store,
        action=MemoryAction.SUPERSEDE,
        content="The owner now prefers tea.",
        related_memory_id=superseded_memory.id,
    )
    superseded_result = await store.approve_candidate(
        ISSUER,
        OWNER,
        superseding.id,
        expected_version=1,
        idempotency_key="candidate-supersede",
    )
    assert superseded_result.state is CandidateState.ACCEPTED
    current_superseded = await store.get_memory(ISSUER, OWNER, superseded_memory.id)
    assert current_superseded.status is MemoryLifecycleStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_sql_legacy_review_candidate_approval_normalizes_action_and_decay_defaults(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, engine = sql_memory_store
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(
            ISSUER,
            OWNER,
            "extractor",
            "embedder",
            embedding_model_revision="rev-1",
            embedding_generation=generation.id,
        ),
        expected_version=1,
        dimension=3,
        model_digest="a" * 64,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    candidate = await _candidate(
        store,
        action=MemoryAction.REVIEW,
        importance=None,
        half_life_days=None,
    )

    approved = await store.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key="candidate-legacy-review-defaults",
    )

    assert approved.state is CandidateState.ACCEPTED
    assert approved.action is MemoryAction.CREATE
    assert approved.memory_id is not None
    record = await store.get_memory(ISSUER, OWNER, approved.memory_id)
    assert record.current_revision.importance == 0.5
    assert record.current_revision.half_life_days == 30.0
    assert record.current_revision.valid_to is None
    async with engine.connect() as connection:
        queued_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM memory_embedding_jobs "
                    "WHERE memory_id = :memory_id AND revision_id = :revision_id"
                ),
                {"memory_id": record.id, "revision_id": record.current_revision_id},
            )
        ).scalar_one()
    assert queued_count == 1


@pytest.mark.asyncio
async def test_sql_approval_queue_failure_is_repaired_by_idempotent_replay(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = sql_memory_store
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(
            ISSUER,
            OWNER,
            "extractor",
            "embedder",
            embedding_model_revision="rev-1",
            embedding_generation=generation.id,
        ),
        expected_version=1,
        dimension=3,
        model_digest="a" * 64,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    candidate = await _candidate(store, action=MemoryAction.REVIEW)
    original_queue = store.queue_embedding_job
    failed = True

    async def fail_once(
        issuer: str,
        subject: str,
        *,
        memory_id: UUID,
        revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob:
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("injected queue failure")
        return await original_queue(
            issuer,
            subject,
            memory_id=memory_id,
            revision_id=revision_id,
            generation_id=generation_id,
        )

    monkeypatch.setattr(store, "queue_embedding_job", fail_once)
    approved = await store.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key="candidate-queue-replay",
    )
    assert approved.state is CandidateState.ACCEPTED

    # The accepted revision and its embedding job are committed atomically;
    # the post-acceptance queue boundary can fail without losing retryable
    # durable work.
    record = await store.get_memory(ISSUER, OWNER, approved.memory_id)  # type: ignore[arg-type]
    async with engine.connect() as connection:
        queued_count = (
            await connection.execute(
                text(
                    "SELECT COUNT(*) FROM memory_embedding_jobs "
                    "WHERE memory_id = :memory_id AND revision_id = :revision_id"
                ),
                {"memory_id": record.id, "revision_id": record.current_revision_id},
            )
        ).scalar_one()
    assert queued_count == 1

    replay = await store.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key="candidate-queue-replay",
    )
    assert replay == approved
    assert failed is False


@pytest.mark.asyncio
async def test_sql_orphaned_active_generation_is_reassociated_without_resave(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _engine = sql_memory_store
    generation_id = uuid4()
    now = datetime.now(UTC)
    async with store.sessions() as session, session.begin():
        session.add(
            _memory_mappings.MemoryEmbeddingGenerationRow(
                id=generation_id,
                generation=1,
                model_id="embedder",
                model_revision="rev-1",
                model_digest="a" * 64,
                dimension=3,
                status="active",
                created_at=now,
                activated_at=now,
                principal_issuer=ISSUER,
                principal_subject=OWNER,
            )
        )
        session.add(
            _memory_mappings.MemoryModelConfigurationRow(
                principal_issuer=ISSUER,
                principal_subject=OWNER,
                extraction_model_id="extractor",
                extraction_model_revision="extractor-rev",
                embedding_model_id="embedder",
                embedding_model_revision="rev-1",
                embedding_generation=None,
                version=4,
                updated_at=now,
            )
        )

    active = await store.get_active_embedding_generation(ISSUER, OWNER)
    configuration = await store.get_model_configuration(ISSUER, OWNER)

    assert active is not None
    assert active.id == generation_id
    assert configuration.embedding_generation == generation_id
    assert configuration.version == 5


@pytest.mark.asyncio
async def test_sql_authorized_run_without_memory_jobs_returns_settled_activity(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _engine = sql_memory_store
    run_id = uuid4()

    async def recall_metadata_loader(
        loaded_run_id: UUID, issuer: str, subject: str
    ) -> tuple[None, None]:
        assert loaded_run_id == run_id
        assert issuer == ISSUER
        assert subject == OWNER
        return None, None

    store.set_run_recall_metadata_loader(recall_metadata_loader)
    snapshot = await store.get_run_memory_activity(run_id, ISSUER, OWNER)

    assert snapshot.processing_status == "settled"
    assert snapshot.items == ()

    async def missing_run_loader(
        loaded_run_id: UUID, issuer: str, subject: str
    ) -> tuple[None, None]:
        raise MemoryNotFound("run memory activity not found")

    store.set_run_recall_metadata_loader(missing_run_loader)
    with pytest.raises(MemoryNotFound, match="run memory activity"):
        await store.get_run_memory_activity(run_id, ISSUER, OWNER)


@pytest.mark.asyncio
async def test_sql_candidate_approval_uses_one_optimistic_version_under_concurrency(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    candidate = await _candidate(store)
    outcomes = await gather(
        store.approve_candidate(
            ISSUER,
            OWNER,
            candidate.id,
            expected_version=1,
            idempotency_key="candidate-concurrent-a",
        ),
        store.approve_candidate(
            ISSUER,
            OWNER,
            candidate.id,
            expected_version=1,
            idempotency_key="candidate-concurrent-b",
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, MemoryVersionConflict) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_sql_candidate_approval_resumes_durable_retryable_claim_after_failure(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _ = sql_memory_store
    candidate = await _candidate(store)
    original_create = store.create_memory
    failed = True

    async def fail_once(issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        nonlocal failed
        if failed:
            failed = False
            raise RuntimeError("injected candidate action failure")
        return await original_create(issuer, subject, **kwargs)

    monkeypatch.setattr(store, "create_memory", fail_once)
    with pytest.raises(RuntimeError, match="injected candidate action failure"):
        await store.approve_candidate(
            ISSUER,
            OWNER,
            candidate.id,
            expected_version=1,
            idempotency_key="candidate-resume-after-failure",
        )

    resumed = await store.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key="candidate-resume-after-failure",
    )
    replay = await store.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key="candidate-resume-after-failure",
    )
    assert resumed.state is CandidateState.ACCEPTED
    assert replay == resumed


@pytest.mark.asyncio
async def test_sql_model_configuration_replay_conflict_and_owner_scoped_reindex(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    store, _ = sql_memory_store
    first_config = MemoryModelConfiguration(ISSUER, OWNER, "extractor-v1", "embed-v1")
    first = await store.save_model_configuration(
        ISSUER,
        OWNER,
        first_config,
        expected_version=1,
        dimension=3,
        model_digest="a" * 64,
        idempotency_key="model-config-replay",
    )
    assert first.embedding_generation is not None
    initial_generations = await store.list_embedding_generations(ISSUER, OWNER)
    assert len(initial_generations) == 1
    assert initial_generations[0].id == first.embedding_generation
    assert initial_generations[0].generation == 1
    assert initial_generations[0].status == "active"
    assert initial_generations[0].model_id == "embed-v1"
    replay = await store.save_model_configuration(
        ISSUER,
        OWNER,
        first_config,
        expected_version=1,
        dimension=3,
        model_digest="a" * 64,
        idempotency_key="model-config-replay",
    )
    assert replay == first
    with pytest.raises(MemoryIdempotencyConflict):
        await store.save_model_configuration(
            ISSUER,
            OWNER,
            MemoryModelConfiguration(ISSUER, OWNER, "extractor-v2", "embed-v1"),
            expected_version=1,
            dimension=3,
            model_digest="a" * 64,
            idempotency_key="model-config-replay",
        )

    # Preserve compatibility with configurations written before initial
    # generation activation was enforced.
    async with store.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE memory_model_configurations SET embedding_generation = NULL "
                "WHERE principal_issuer = :issuer AND principal_subject = :subject"
            ),
            {"issuer": ISSUER, "subject": OWNER},
        )
        await session.execute(
            text(
                "DELETE FROM memory_embedding_generations "
                "WHERE principal_issuer = :issuer AND principal_subject = :subject"
            ),
            {"issuer": ISSUER, "subject": OWNER},
        )
    repaired = await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(ISSUER, OWNER, "extractor-v1", "embed-v2"),
        expected_version=1,
        dimension=3,
        model_digest="b" * 64,
        idempotency_key="model-config-repair-and-change",
    )
    assert repaired.embedding_generation is not None
    assert repaired.version == 2
    repaired_generations = await store.list_embedding_generations(ISSUER, OWNER)
    assert len(repaired_generations) == 1
    assert repaired_generations[0].id == repaired.embedding_generation
    assert repaired_generations[0].generation == 1
    assert repaired_generations[0].status == "active"
    assert repaired_generations[0].model_id == "embed-v2"
    replay_repaired = await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(ISSUER, OWNER, "extractor-v1", "embed-v2"),
        expected_version=1,
        dimension=3,
        model_digest="b" * 64,
        idempotency_key="model-config-repair-and-change",
    )
    assert replay_repaired == repaired

    changed = await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(ISSUER, OWNER, "extractor-v1", "embed-v3"),
        expected_version=2,
        dimension=3,
        model_digest="c" * 64,
        idempotency_key="model-config-cutover",
    )
    assert changed.version == 3
    generations = await store.list_embedding_generations(ISSUER, OWNER)
    replacement = [item for item in generations if item.status == "building"]
    assert len(replacement) == 1
    assert replacement[0].model_id == "embed-v3"
    assert replacement[0].model_digest == "c" * 64
    assert await store.list_embedding_generations(ISSUER, OTHER_OWNER) == []
    with pytest.raises(MemoryVersionConflict):
        await store.save_model_configuration(
            ISSUER,
            OWNER,
            MemoryModelConfiguration(ISSUER, OWNER, "extractor-v3", "embed-v3"),
            expected_version=2,
            dimension=3,
            model_digest="c" * 64,
            idempotency_key="model-config-stale",
        )


@pytest.mark.asyncio
async def test_sql_memory_policy_replay_conflict_scope_and_fallback_grant(
    sql_memory_store: tuple[SqlMemoryRepository, AsyncEngine],
) -> None:
    _, engine = sql_memory_store
    sessions = session_factory(engine)
    source_agent, fallback_agent = uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add_all(
            [
                AgentProfileRow(
                    id=source_agent, slug="source-agent", display_name="Source"
                ),
                AgentProfileRow(
                    id=fallback_agent, slug="fallback-agent", display_name="Fallback"
                ),
            ]
        )
    agent_store = SqlAgentStore(sessions, AgentCatalog())
    target = MemoryPolicy(uuid4(), fallback_agent, 1)
    await agent_store.create_memory_policy(
        ISSUER, OWNER, target, key="policy-target"
    )
    source = MemoryPolicy(
        uuid4(),
        source_agent,
        1,
        fallback_agent_profile_ids=(fallback_agent,),
    )
    created = await agent_store.create_memory_policy(
        ISSUER, OWNER, source, key="policy-source"
    )
    replay = await agent_store.create_memory_policy(
        ISSUER, OWNER, source, key="policy-source"
    )
    assert replay == created
    with pytest.raises(ConfigurationIdempotencyConflict):
        await agent_store.create_memory_policy(
            ISSUER,
            OWNER,
            replace(source, shared_user_read=False),
            key="policy-source",
        )
    assert (await agent_store.list_memory_policies(ISSUER, OTHER_OWNER, source_agent)) == []
    concurrent = await gather(
        agent_store.create_memory_policy(
            ISSUER, OWNER, MemoryPolicy(uuid4(), source_agent, 2), key="policy-race-a"
        ),
        agent_store.create_memory_policy(
            ISSUER, OWNER, MemoryPolicy(uuid4(), source_agent, 2), key="policy-race-b"
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in concurrent) == 1
    assert sum(isinstance(item, ConfigurationVersionConflict) for item in concurrent) == 1
