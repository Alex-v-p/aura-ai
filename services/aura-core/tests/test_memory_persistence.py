"""Persistence contract coverage for the AURA-0037 memory boundary.

The PostgreSQL cases are opt-in because the vector column intentionally cannot
be represented by SQLite.  They use the same disposable-database convention as
the other Core adapter tests.
"""

from __future__ import annotations

import os
from asyncio import gather
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from aura_core.bootstrap.database import metadata
from aura_core.bootstrap.memory_uow import memory_repository
from aura_core.domains.knowledge.memory import persistence as _memory_mappings
from aura_core.domains.knowledge.memory.public import (
    PURGE_CONFIRMATION,
    MemoryEmbeddingGeneration,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryNotFound,
    MemoryRecord,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
    MemoryVersionConflict,
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
):
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
    activation_attributes, _ = await invoke(
        "memory.embedding.activate",
        store.activate_embedding_generation(ISSUER, OWNER, generation.id),
    )
    assert "memory_id" not in activation_attributes
    assert activation_attributes["generation_id"] == str(generation.id)
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
        ),
    )
    assert embedding_attributes["memory_id"] == str(memory.id)
    assert embedding_attributes["memory_revision_id"] == str(historical_revision_id)
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
    store, _ = sql_memory_store

    async def create() -> object:
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
