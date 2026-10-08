"""AURA-0038 lifecycle and resumable embedding-generation acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aura_core.domains.knowledge.memory.public import (
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryProcessingService,
    MemoryRecord,
    MemoryReindexService,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
    ProcessingJobStatus,
)

ISSUER = "https://issuer.example"
OWNER = "owner"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Embedding:
    async def embed(self, model_id: str, content: str) -> object:
        del content
        return type(
            "EmbeddingResult",
            (),
            {"vector": (0.1, 0.2), "digest": "f" * 64, "model_id": model_id},
        )()


async def _memory(store: MemoryStore, content: str) -> MemoryRecord:
    return await store.create_memory(
        ISSUER,
        OWNER,
        content=content,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=30,
    )


@pytest.mark.asyncio
async def test_reindex_generation_stays_parallel_until_complete() -> None:
    store = MemoryStore(clock=lambda: NOW)
    first = await _memory(store, "First retained fact")
    second = await _memory(store, "Second retained fact")
    old = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embed-old",
        model_revision="rev-old",
        dimension=3,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        ISSUER,
        OWNER,
        first.id,  # type: ignore[attr-defined]
        generation_id=old.id,
        revision_id=first.current_revision_id,  # type: ignore[attr-defined]
        vector=(0.1, 0.2, 0.3),
        digest="b" * 64,
    )
    await store.attach_embedding(
        ISSUER,
        OWNER,
        second.id,  # type: ignore[attr-defined]
        generation_id=old.id,
        revision_id=second.current_revision_id,  # type: ignore[attr-defined]
        vector=(0.1, 0.2, 0.3),
        digest="c" * 64,
    )
    active_old = await store.activate_embedding_generation(ISSUER, OWNER, old.id)
    replacement = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=2,
        model_id="embed-new",
        model_revision="rev-new",
        dimension=4,
        model_digest="d" * 64,
    )
    assert active_old.status == "active"
    assert replacement.status == "building"
    assert store.embedding_generations[old.id].status == "active"
    assert store.embedding_generations[replacement.id].status == "building"
    # A partial generation must not become the selected generation. The
    # maintenance implementation owns the resumable completeness check.
    with pytest.raises(MemoryValidationError):
        await store.activate_embedding_generation(ISSUER, OWNER, replacement.id)


@pytest.mark.asyncio
async def test_newer_model_cutover_retires_obsolete_building_generation() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _memory(store, "A retained cutover fact")
    first = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embed-a", dimension=2,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        ISSUER, OWNER, memory.id, generation_id=first.id,
        revision_id=memory.current_revision_id, vector=(0.1, 0.2), digest="b" * 64,
    )
    await store.save_model_configuration(
        ISSUER, OWNER,
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embed-a", embedding_generation=first.id
        ),
    )
    await store.activate_embedding_generation(ISSUER, OWNER, first.id)
    await store.save_model_configuration(
        ISSUER, OWNER,
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embed-b", embedding_generation=first.id
        ),
        dimension=2, model_digest="c" * 64,
    )
    building = [
        generation for generation in store.embedding_generations.values()
        if generation.status == "building"
    ]
    assert len(building) == 1
    obsolete = building[0]
    await store.save_model_configuration(
        ISSUER, OWNER,
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embed-c", embedding_generation=first.id
        ),
        dimension=2, model_digest="d" * 64,
    )
    generations = list(store.embedding_generations.values())
    current = next(item for item in generations if item.model_id == "embed-c")
    assert first.status == "active"
    assert obsolete.status == "retired"
    assert current.status == "building"
    assert await MemoryReindexService(store, _Embedding()).resume(ISSUER, OWNER, current.id) == 1
    assert current.status == "active"
    assert first.status == "retired"


@pytest.mark.asyncio
async def test_reindex_rejects_legacy_credential_revision_before_provider() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _memory(store, "A safe retained fact")
    # Simulate a legacy row written before the credential admission guard.
    memory.revisions[0] = replace(memory.current_revision, content="My password is hunter2")
    generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=2,
        model_digest="a" * 64,
    )
    with pytest.raises(MemoryValidationError, match="cannot be embedded"):
        await MemoryReindexService(store, _Embedding()).resume(
            ISSUER, OWNER, generation.id
        )
    assert memory.embeddings == []
    job = next(iter(store.embedding_jobs.values()))
    assert job.status is ProcessingJobStatus.FAILED
    assert job.last_error_class == "credential"


@pytest.mark.asyncio
async def test_reindex_rejects_mixed_dimensions_and_keeps_prior_generation_usable() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _memory(store, "A retained fact")
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        ISSUER,
        OWNER,
        memory.id,
        generation_id=generation.id,
        revision_id=memory.current_revision_id,
        vector=(0.1, 0.2, 0.3),
        digest="b" * 64,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    replacement = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=2,
        model_id="embedder-new",
        model_revision="rev-2",
        dimension=4,
        model_digest="c" * 64,
    )
    with pytest.raises(MemoryValidationError):
        await store.attach_embedding(
            ISSUER,
            OWNER,
            memory.id,
            generation_id=replacement.id,
            revision_id=memory.current_revision_id,
            vector=(0.1, 0.2),
            digest="d" * 64,
        )
    assert (await store.get_memory(ISSUER, OWNER, memory.id)).status is MemoryLifecycleStatus.ACTIVE


@pytest.mark.asyncio
async def test_generation_failure_can_roll_back_without_deleting_prior_embeddings() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _memory(store, "A durable fact")
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        ISSUER,
        OWNER,
        memory.id,
        generation_id=generation.id,
        revision_id=memory.current_revision_id,
        vector=(0.1, 0.2, 0.3),
        digest="b" * 64,
    )
    await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    replacement = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=2,
        model_id="embedder-new",
        model_revision="rev-2",
        dimension=3,
        model_digest="c" * 64,
    )
    # The failed generation is a resumable metadata state; the prior selected
    # generation and its vector remain available for rollback.
    failed = await store.mark_embedding_generation_failed(ISSUER, OWNER, replacement.id)  # type: ignore[attr-defined]
    assert failed.status == "failed"
    assert generation.status == "active"
    record = await store.get_memory(ISSUER, OWNER, memory.id)
    assert any(item.generation == generation.generation for item in record.embeddings)


@pytest.mark.asyncio
async def test_reindex_transient_failure_keeps_building_generation_resumable() -> None:
    fake_now = [NOW]
    store = MemoryStore(clock=lambda: fake_now[0])
    memory = await store.create_memory(
        ISSUER,
        OWNER,
        content="A fact awaiting replacement embedding.",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
    )
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        dimension=2,
        model_digest="a" * 64,
    )

    class FlakyEmbedder:
        def __init__(self) -> None:
            self.calls = 0

        async def embed(self, model_id: str, content: str) -> object:
            del model_id, content
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary embedding outage")
            return type(
                "EmbeddingResult",
                (),
                {"vector": (0.1, 0.2), "digest": "b" * 64},
            )()

    embedder = FlakyEmbedder()
    reindex = MemoryReindexService(store, embedder)
    with pytest.raises(RuntimeError, match="temporary embedding outage"):
        await reindex.resume(ISSUER, OWNER, generation.id)
    assert store.embedding_generations[generation.id].status == "building"
    assert memory.embeddings == []
    fake_now[0] += timedelta(seconds=3)
    assert await reindex.resume(ISSUER, OWNER, generation.id) == 1
    assert store.embedding_generations[generation.id].status == "active"
    assert len(memory.embeddings) == 1


@pytest.mark.asyncio
async def test_manual_create_and_revision_are_both_reembedded_in_replacement_generation() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _memory(store, "The owner prefers concise answers.")
    first = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder-old",
        model_revision="rev-old",
        dimension=2,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        ISSUER,
        OWNER,
        memory.id,
        revision_id=memory.current_revision_id,
        generation_id=first.id,
        vector=(0.1, 0.2),
        digest="b" * 64,
    )
    await store.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=first.id
        ),
    )
    await store.activate_embedding_generation(ISSUER, OWNER, first.id)
    revised = await store.revise_memory(
        ISSUER,
        OWNER,
        memory.id,
        content="The owner now prefers detailed answers.",
        expected_version=1,
    )
    replacement = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=2,
        model_id="embedder-new",
        model_revision="rev-new",
        dimension=2,
        model_digest="c" * 64,
    )
    # Model cutover validation requires the owner configuration to identify
    # the replacement target before its building generation can activate.
    configuration = store.model_configurations[(ISSUER, OWNER)]
    store.model_configurations[(ISSUER, OWNER)] = replace(
        configuration, embedding_model_id="embedder-new", embedding_model_revision="rev-new"
    )
    processed = await MemoryReindexService(store, _Embedding()).resume(
        ISSUER, OWNER, replacement.id
    )
    assert processed == 2
    assert len(revised.embeddings) == 3
    assert sum(item.generation == replacement.generation for item in revised.embeddings) == 2
    assert replacement.status == "active"
    assert first.status == "retired"
    assert store.model_configurations[(ISSUER, OWNER)].embedding_generation == replacement.id


@pytest.mark.asyncio
async def test_reindex_progress_counts_existing_target_embeddings() -> None:
    store = MemoryStore(clock=lambda: NOW)
    first = await _memory(store, "Already embedded retained fact")
    second = await _memory(store, "Pending retained fact")
    generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=2, model_digest="a" * 64
    )
    await store.attach_embedding(
        ISSUER, OWNER, first.id, revision_id=first.current_revision_id,
        generation_id=generation.id, vector=(0.1, 0.2), digest="b" * 64,
    )
    telemetry: list[dict[str, object]] = []

    def collect(**event: object) -> None:
        telemetry.append(event)

    reindex = MemoryReindexService(store, _Embedding(), telemetry=collect)

    assert await reindex.resume(ISSUER, OWNER, generation.id) == 2
    assert len(second.embeddings) == 1
    chunks = [item for item in telemetry if item["operation"] == "memory.reindex.chunk"]
    assert len(chunks) == 1
    assert chunks[0]["progress"] == 1.0
    assert chunks[0]["backlog"] == 0


@pytest.mark.asyncio
async def test_maintenance_and_reindex_cover_more_than_two_hundred_user_and_agent_revisions(
) -> None:
    store = MemoryStore(clock=lambda: NOW)
    for index in range(201):
        await _memory(store, f"User retained fact {index}")
    agent_id = uuid4()
    agent_memory = await store.create_memory(
        ISSUER,
        OWNER,
        content="Agent-private retained fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_id),
        confidence=0.9,
        importance=0.5,
        half_life_days=30,
    )
    generation = await store.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=2, model_digest="a" * 64
    )
    # A first generation cannot be activated until every retained revision,
    # including private-agent scope, has a vector.
    with pytest.raises(MemoryValidationError):
        await store.activate_embedding_generation(ISSUER, OWNER, generation.id)
    reindex = MemoryReindexService(store, _Embedding())
    processed = await reindex.resume(ISSUER, OWNER, generation.id)
    assert processed == 202
    assert len(agent_memory.embeddings) == 1
    assert generation.status == "active"


@pytest.mark.asyncio
async def test_maintenance_does_not_silently_drop_the_two_hundred_first_record() -> None:
    store = MemoryStore(clock=lambda: NOW)
    for index in range(201):
        await store.create_memory(
            ISSUER,
            OWNER,
            content=f"Expiring fact {index}",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.5,
            half_life_days=0.25,
            observed_at=NOW,
        )
    processor = MemoryProcessingService(store, object(), object(), clock=lambda: NOW)
    changed = await processor.maintain(ISSUER, OWNER, now=NOW + timedelta(days=2))
    assert changed == 201
    assert all(item.status is MemoryLifecycleStatus.DORMANT for item in store.memories.values())
