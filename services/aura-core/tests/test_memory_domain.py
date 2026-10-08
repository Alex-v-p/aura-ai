"""Public-domain contract tests for the AURA-0037 memory foundation."""

from datetime import UTC, datetime, timedelta
from math import isclose
from uuid import uuid4

import pytest
from aura_core.domains.knowledge.memory.public import (
    MAX_HALF_LIFE_DAYS,
    MIN_HALF_LIFE_DAYS,
    PURGE_CONFIRMATION,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryNotFound,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
    MemoryVersionConflict,
    contains_secret,
    validate_revision,
)

ISSUER = "https://issuer.example"
OWNER = "owner"
OTHER_OWNER = "other-owner"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _provenance(*, evidence: str = "owner supplied evidence") -> MemoryProvenance:
    return MemoryProvenance(uuid4(), "manual", evidence=evidence)


async def _create(
    store: MemoryStore,
    *,
    subject: str = OWNER,
    scope: MemoryScope | None = None,
    content: str = "The owner prefers concise answers.",
    **kwargs: object,
):
    return await store.create_memory(
        ISSUER,
        subject,
        content=content,
        kind=MemoryKind.PREFERENCE,
        scope=scope or MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_relevance_uses_half_life_without_mutating_record() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _create(store)
    original_version = memory.version

    assert isclose(memory.relevance(NOW), 1.0, rel_tol=1e-9)
    assert isclose(memory.relevance(NOW + timedelta(days=30)), 0.5, rel_tol=1e-9)
    assert memory.version == original_version


@pytest.mark.parametrize("half_life", [MIN_HALF_LIFE_DAYS - 0.01, MAX_HALF_LIFE_DAYS + 0.01])
def test_half_life_bounds_are_inclusive_and_reject_out_of_range(half_life: float) -> None:
    with pytest.raises(MemoryValidationError):
        validate_revision("a fact", 0.5, 0.5, half_life, None, None)


def test_validation_rejects_credential_like_content_and_invalid_validity() -> None:
    credential_values = (
        "api_key: do-not-store",
        "client_secret = do-not-store",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN PRIVATE KEY-----",
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    )
    for value in credential_values:
        assert contains_secret(value), value
        with pytest.raises(MemoryValidationError, match="credential"):
            validate_revision(value, 0.9, 0.5, 30, None, None)
    with pytest.raises(MemoryValidationError, match="valid-to"):
        validate_revision(
            "a fact",
            0.9,
            0.5,
            30,
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_correction_appends_immutable_revision_and_preserves_provenance() -> None:
    store = MemoryStore(clock=lambda: NOW)
    evidence = _provenance()
    memory = await _create(store, provenance=[evidence])
    first_revision = memory.revisions[0]

    corrected = await store.revise_memory(
        ISSUER,
        OWNER,
        memory.id,
        content="The owner prefers detailed answers.",
        reason="owner correction",
        expected_version=1,
        idempotency_key="correction-1",
    )

    assert corrected.version == 2
    assert len(corrected.revisions) == 2
    assert corrected.revisions[0] == first_revision
    assert corrected.revisions[0].content == "The owner prefers concise answers."
    assert corrected.content == "The owner prefers detailed answers."
    assert corrected.provenance == [evidence]


@pytest.mark.asyncio
async def test_correction_reason_rejects_credential_like_values() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _create(store)
    with pytest.raises(MemoryValidationError, match="credential"):
        await store.revise_memory(
            ISSUER,
            OWNER,
            memory.id,
            content="The owner prefers detailed answers.",
            reason="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            expected_version=1,
        )


@pytest.mark.asyncio
async def test_provenance_evidence_is_subject_to_the_same_secret_policy() -> None:
    store = MemoryStore(clock=lambda: NOW)
    with pytest.raises(MemoryValidationError, match="credential"):
        await _create(
            store,
            provenance=[
                _provenance(
                    evidence="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
                )
            ],
        )


@pytest.mark.asyncio
async def test_status_and_pin_changes_do_not_overwrite_revision_history() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _create(store)
    revision_ids = tuple(revision.id for revision in memory.revisions)

    pinned = await store.set_pinned(
        ISSUER, OWNER, memory.id, pinned=True, expected_version=memory.version
    )
    dormant = await store.set_status(
        ISSUER,
        OWNER,
        memory.id,
        status=MemoryLifecycleStatus.DORMANT,
        expected_version=pinned.version,
    )

    assert dormant.pinned is True
    assert dormant.status is MemoryLifecycleStatus.DORMANT
    assert tuple(revision.id for revision in dormant.revisions) == revision_ids
    assert dormant.dormant_at == NOW


@pytest.mark.asyncio
async def test_reads_are_owner_scoped_and_agent_filter_is_explicit() -> None:
    store = MemoryStore(clock=lambda: NOW)
    agent_id = uuid4()
    user_memory = await _create(store)
    agent_memory = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_id),
        content="Agent-only working preference.",
    )

    assert {item.id for item in await store.list_memories(ISSUER, OWNER)} == {user_memory.id}
    scoped = await store.list_memories(
        ISSUER,
        OWNER,
        MemoryFilters(
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_id,
            authorized_agent_ids=frozenset({agent_id}),
        ),
    )
    assert [item.id for item in scoped] == [agent_memory.id]
    with pytest.raises(MemoryNotFound):
        await store.get_memory(ISSUER, OTHER_OWNER, user_memory.id)
    assert await store.list_memories(ISSUER, OTHER_OWNER) == []


@pytest.mark.asyncio
async def test_none_scope_filter_resolves_owner_authorized_agent_records() -> None:
    store = MemoryStore(clock=lambda: NOW)
    agent_memory = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, uuid4()),
        content="Agent-only working preference.",
    )
    with pytest.raises(MemoryValidationError):
        MemoryFilters(scope_type=None)  # type: ignore[arg-type]
    with pytest.raises(MemoryNotFound):
        await store.get_memory(ISSUER, OWNER, agent_memory.id, scope_type=None)
    resolved = await store.get_memory(
        ISSUER,
        OWNER,
        agent_memory.id,
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=agent_memory.scope.agent_profile_id,
    )
    assert resolved.id == agent_memory.id


@pytest.mark.asyncio
async def test_idempotent_create_replays_exactly_and_rejects_payload_reuse() -> None:
    store = MemoryStore(clock=lambda: NOW)
    first = await _create(store, idempotency_key="create-1")
    replay = await _create(store, idempotency_key="create-1")
    assert replay.id == first.id
    assert len(store.memories) == 1

    with pytest.raises(MemoryIdempotencyConflict):
        await _create(store, idempotency_key="create-1", content="different")


@pytest.mark.asyncio
async def test_status_idempotency_includes_related_memory_and_relations_are_scoped() -> None:
    store = MemoryStore(clock=lambda: NOW)
    agent_a, agent_b = uuid4(), uuid4()
    source = await _create(store, scope=MemoryScope(MemoryScopeType.AGENT, agent_a))
    related = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_a),
        content="The related fact belongs to agent A.",
    )
    other_agent = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_b),
        content="The related fact belongs to agent B.",
    )
    foreign_owner = await _create(
        store,
        subject=OTHER_OWNER,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_a),
        content="The related fact belongs to another owner.",
    )
    foreign_source = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_a),
        content="The source whose relation crosses owners must remain unchanged.",
    )
    first = await store.set_status(
        ISSUER,
        OWNER,
        source.id,
        status=MemoryLifecycleStatus.DISPUTED,
        related_memory_id=related.id,
        expected_version=1,
        idempotency_key="status-1",
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=agent_a,
        authorized_agent_ids=frozenset({agent_a}),
    )
    replay = await store.set_status(
        ISSUER,
        OWNER,
        source.id,
        status=MemoryLifecycleStatus.DISPUTED,
        related_memory_id=related.id,
        expected_version=1,
        idempotency_key="status-1",
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=agent_a,
        authorized_agent_ids=frozenset({agent_a}),
    )
    assert replay.version == first.version == 2
    assert replay.relations == first.relations
    with pytest.raises(MemoryIdempotencyConflict):
        await store.set_status(
            ISSUER,
            OWNER,
            source.id,
            status=MemoryLifecycleStatus.DISPUTED,
            related_memory_id=other_agent.id,
            expected_version=1,
            idempotency_key="status-1",
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_a,
            authorized_agent_ids=frozenset({agent_a}),
        )
    with pytest.raises(MemoryNotFound):
        await store.set_status(
            ISSUER,
            OWNER,
            related.id,
            status=MemoryLifecycleStatus.SUPERSEDED,
            related_memory_id=other_agent.id,
            expected_version=1,
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_a,
            authorized_agent_ids=frozenset({agent_a}),
        )
    with pytest.raises(MemoryNotFound):
        await store.set_status(
            ISSUER,
            OWNER,
            foreign_source.id,
            status=MemoryLifecycleStatus.SUPERSEDED,
            related_memory_id=foreign_owner.id,
            expected_version=1,
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_a,
            authorized_agent_ids=frozenset({agent_a}),
        )


@pytest.mark.asyncio
async def test_relations_require_exact_matching_scope_and_created_at_is_immutable() -> None:
    store = MemoryStore()
    agent_a, agent_b = uuid4(), uuid4()

    user_source = await _create(store)
    user_related = await _create(store, content="User-scoped related fact.")
    user_result = await store.set_status(
        ISSUER,
        OWNER,
        user_source.id,
        status=MemoryLifecycleStatus.DISPUTED,
        related_memory_id=user_related.id,
        expected_version=1,
    )
    relation_created_at = user_result.relations[0].created_at
    pinned = await store.set_pinned(ISSUER, OWNER, user_source.id, pinned=True, expected_version=2)
    corrected = await store.revise_memory(
        ISSUER,
        OWNER,
        user_source.id,
        content="The owner prefers detailed answers.",
        expected_version=3,
    )
    assert pinned.relations[0].created_at == relation_created_at
    assert corrected.relations[0].created_at == relation_created_at

    same_agent_source = await _create(store, scope=MemoryScope(MemoryScopeType.AGENT, agent_a))
    same_agent_related = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_a),
        content="Same-agent related fact.",
    )
    same_agent = await store.set_status(
        ISSUER,
        OWNER,
        same_agent_source.id,
        status=MemoryLifecycleStatus.DISPUTED,
        related_memory_id=same_agent_related.id,
        expected_version=1,
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=agent_a,
        authorized_agent_ids=frozenset({agent_a, agent_b}),
    )
    assert same_agent.relations[0].memory_id == same_agent_related.id

    invalid_pairs = (
        (MemoryScope(MemoryScopeType.USER), MemoryScope(MemoryScopeType.AGENT, agent_a)),
        (MemoryScope(MemoryScopeType.AGENT, agent_a), MemoryScope(MemoryScopeType.USER)),
        (
            MemoryScope(MemoryScopeType.AGENT, agent_a),
            MemoryScope(MemoryScopeType.AGENT, agent_b),
        ),
    )
    for index, (source_scope, related_scope) in enumerate(invalid_pairs):
        source = await _create(store, scope=source_scope, content=f"source {index}")
        related = await _create(store, scope=related_scope, content=f"related {index}")
        with pytest.raises(MemoryNotFound):
            await store.set_status(
                ISSUER,
                OWNER,
                source.id,
                status=MemoryLifecycleStatus.DISPUTED,
                related_memory_id=related.id,
                expected_version=1,
                scope_type=source_scope.type,
                agent_profile_id=source_scope.agent_profile_id,
                authorized_agent_ids=frozenset({agent_a, agent_b}),
            )


@pytest.mark.asyncio
async def test_agent_scope_filters_keep_two_agents_separate_and_embedding_metadata_is_per_revision(
) -> None:
    store = MemoryStore(clock=lambda: NOW)
    agent_a, agent_b = uuid4(), uuid4()
    first = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_a),
    )
    second = await _create(
        store,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_b),
        content="Agent B private fact.",
    )
    assert [item.id for item in await store.list_memories(
        ISSUER,
        OWNER,
        MemoryFilters(
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_a,
            authorized_agent_ids=frozenset({agent_a}),
        ),
    )] == [first.id]
    assert [item.id for item in await store.list_memories(
        ISSUER,
        OWNER,
        MemoryFilters(
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_b,
            authorized_agent_ids=frozenset({agent_b}),
        ),
    )] == [second.id]
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedding-model",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    first = await store.attach_embedding(
        ISSUER,
        OWNER,
        first.id,
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=agent_a,
        authorized_agent_ids=frozenset({agent_a}),
        generation_id=generation.id,
        vector=(0.1, 0.2, 0.3),
        digest="b" * 64,
        model_id=generation.model_id,
        model_revision=generation.model_revision,
        model_digest=generation.model_digest,
    )
    assert len(first.embeddings) == 1
    assert first.embeddings[0].revision_id == first.current_revision_id
    assert len({item.generation for item in first.embeddings}) == len(first.embeddings)
    with pytest.raises(MemoryValidationError):
        await store.attach_embedding(
            ISSUER,
            OWNER,
            first.id,
            scope_type=MemoryScopeType.AGENT,
            agent_profile_id=agent_a,
            authorized_agent_ids=frozenset({agent_a}),
            generation_id=generation.id,
            vector=(0.1, 0.2, 0.3),
            digest="c" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        )

    other = await _create(store, content="A second memory revision.")
    with pytest.raises(MemoryNotFound):
        await store.attach_embedding(
            ISSUER,
            OWNER,
            first.id,
            generation_id=generation.id,
            revision_id=other.current_revision_id,
            vector=(0.1, 0.2, 0.3),
            digest="d" * 64,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        )
    with pytest.raises(MemoryValidationError):
        await store.register_embedding_generation(
            ISSUER,
            OWNER,
            generation=1,
            model_id="other-model",
            model_revision="rev-2",
            dimension=3,
            model_digest="e" * 64,
        )


@pytest.mark.asyncio
async def test_non_finite_embedding_vectors_are_rejected_before_memory_mutation() -> None:
    store = MemoryStore(clock=lambda: NOW)
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


@pytest.mark.asyncio
async def test_optimistic_versions_block_lost_corrections_and_purge_requires_confirmation() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _create(store)
    with pytest.raises(MemoryVersionConflict):
        await store.revise_memory(
            ISSUER,
            OWNER,
            memory.id,
            content="stale correction",
            expected_version=99,
        )
    with pytest.raises(MemoryPurgeConfirmationRequired):
        await store.purge(ISSUER, OWNER, memory.id, confirmation="PURGE", expected_version=1)
    receipt = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
        idempotency_key="purge-1",
    )
    assert receipt.action == "purge"
    assert receipt.memory_id == memory.id
    assert memory.id not in store.memories
    with pytest.raises(MemoryNotFound):
        await store.get_memory(ISSUER, OWNER, memory.id)
    replay = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
        idempotency_key="purge-1",
    )
    assert replay == receipt
    with pytest.raises(MemoryIdempotencyConflict):
        await store.purge(
            ISSUER,
            OWNER,
            memory.id,
            confirmation=PURGE_CONFIRMATION,
            expected_version=2,
            idempotency_key="purge-1",
        )


@pytest.mark.asyncio
async def test_purge_audit_contains_metadata_only() -> None:
    store = MemoryStore(clock=lambda: NOW)
    memory = await _create(store, content="private fact")
    audit = await store.purge(
        ISSUER,
        OWNER,
        memory.id,
        confirmation=PURGE_CONFIRMATION,
        expected_version=1,
    )
    assert audit.issuer == ISSUER
    assert audit.subject == OWNER
    assert audit.action == "purge"
    assert not hasattr(audit, "content")
    assert not hasattr(audit, "evidence")
