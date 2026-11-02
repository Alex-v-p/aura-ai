"""Focused public-domain coverage for AURA-0032 conversation metadata.

These tests deliberately use the conversation store seam rather than reaching
through ORM internals.  They protect owner isolation, metadata-only search,
the reversible archive lifecycle, and cursor/activity invariants shared by
the in-memory and PostgreSQL stores.
"""

import base64
import json
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.dto import Run, RunStatus
from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    ConversationArchiveState,
    ConversationListFilters,
    Message,
    TitleState,
)
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    ArchivedConversationConflict,
    ConversationStore,
    IdempotencyConflict,
    InvalidConversationCursor,
    InvalidConversationMetadata,
    VersionConflict,
)
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import ModelDescriptor

ISSUER = "https://issuer.example"
SUBJECT = "owner"


async def _store_with_models() -> tuple[ConversationStore, tuple[ModelDescriptor, ...]]:
    provider = FakeChatModel()
    return ConversationStore(), tuple(await provider.list_models())


async def _conversation(
    store: ConversationStore,
    models: tuple[ModelDescriptor, ...],
    message: str,
    *,
    issuer: str = ISSUER,
    subject: str = SUBJECT,
    model: str = "fake",
) -> tuple[Conversation, Message, Run]:
    return await store.create(issuer, subject, message, model, models, str(uuid4()))


@pytest.mark.asyncio
async def test_manual_rename_is_trimmed_and_title_inference_cannot_overwrite_it() -> None:
    store, models = await _store_with_models()
    conversation, _, run = await _conversation(store, models, "first prompt")

    updated = await store.update_metadata(
        conversation.id,
        SUBJECT,
        title="  A manually chosen title  ",
        archived=None,
        version=conversation.version,
        idempotency_key="rename-1",
        issuer=ISSUER,
    )

    assert updated.title == "A manually chosen title"
    assert updated.title_state is TitleState.MANUAL
    assert updated.version == 2
    assert await store.settle_title(run.id, "Generated title", TitleState.GENERATED) is False
    assert updated.title == "A manually chosen title"
    assert updated.title_state is TitleState.MANUAL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "x" * 256,
        "line\nbreak",
        "tab\tvalue",
        "nul\x00value",
        "delete\x7f",
        "c1-next\u0085",
        "c1-last\u009f",
        "line\u2028separator",
        "paragraph\u2029separator",
    ],
)
async def test_metadata_title_rejects_empty_oversized_and_control_input(title: str) -> None:
    store, models = await _store_with_models()
    conversation, _, _ = await _conversation(store, models, "prompt")

    with pytest.raises(InvalidConversationMetadata):
        await store.update_metadata(
            conversation.id,
            SUBJECT,
            title=title,
            archived=None,
            version=conversation.version,
            idempotency_key=str(uuid4()),
            issuer=ISSUER,
        )


@pytest.mark.asyncio
async def test_manual_title_trims_before_length_validation() -> None:
    store, models = await _store_with_models()
    conversation, _, _ = await _conversation(store, models, "prompt")
    accepted = await store.update_metadata(
        conversation.id,
        SUBJECT,
        title=f"  {'x' * 255}  ",
        archived=None,
        version=conversation.version,
        idempotency_key="trimmed-255",
        issuer=ISSUER,
    )
    assert accepted.title == "x" * 255

    with pytest.raises(InvalidConversationMetadata):
        await store.update_metadata(
            conversation.id,
            SUBJECT,
            title=f"  {'x' * 256}  ",
            archived=None,
            version=accepted.version,
            idempotency_key="trimmed-256",
            issuer=ISSUER,
        )


@pytest.mark.asyncio
async def test_archive_restore_is_reversible_read_only_and_preserves_activity_position() -> None:
    store, models = await _store_with_models()
    older, _, older_run = await _conversation(store, models, "older")
    older.messages[0].created_at = datetime(2026, 1, 10, tzinfo=UTC)
    await store.start_run(older_run.id)
    await store.finish_run(older_run.id, RunStatus.COMPLETED)
    newer, _, newer_run = await _conversation(store, models, "newer")
    newer.messages[0].created_at = datetime(2026, 1, 20, tzinfo=UTC)
    await store.start_run(newer_run.id)
    await store.finish_run(newer_run.id, RunStatus.COMPLETED)

    await store.update_metadata(
        older.id,
        SUBJECT,
        title=None,
        archived=True,
        version=older.version,
        idempotency_key="archive-1",
        issuer=ISSUER,
    )
    assert older.archived_at is not None
    active, _ = await store.list(SUBJECT, issuer=ISSUER)
    assert [item.id for item in active] == [newer.id]
    archived, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(archive_state=ConversationArchiveState.ARCHIVED),
    )
    assert [item.id for item in archived] == [older.id]

    with pytest.raises(ArchivedConversationConflict):
        await store.update_metadata(
            older.id,
            SUBJECT,
            title="cannot rename while archived",
            archived=None,
            version=older.version,
            idempotency_key="archive-rename",
            issuer=ISSUER,
        )
    with pytest.raises(ArchivedConversationConflict):
        await store.add_run(
            older.id, SUBJECT, "blocked", older.version, "archive-run", ISSUER
        )

    restored = await store.update_metadata(
        older.id,
        SUBJECT,
        title=None,
        archived=False,
        version=older.version,
        idempotency_key="restore-1",
        issuer=ISSUER,
    )
    assert restored.archived_at is None
    active_after_restore, _ = await store.list(SUBJECT, issuer=ISSUER)
    assert [item.id for item in active_after_restore] == [newer.id, older.id]
    assert [
        item["action"]
        for item in store.auth_audit
        if item["action"] in {"conversation.archive", "conversation.restore"}
    ] == ["conversation.archive", "conversation.restore"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED]
)
async def test_archive_rejects_every_nonterminal_run_state(status: RunStatus) -> None:
    store, models = await _store_with_models()
    conversation, _, run = await _conversation(store, models, "active run")
    if status is RunStatus.RUNNING:
        await store.start_run(run.id)
    elif status is RunStatus.CANCEL_REQUESTED:
        await store.request_cancel(run.id, SUBJECT, "cancel-1", ISSUER)
    assert run.status is status

    with pytest.raises(ActiveRunConflict):
        await store.update_metadata(
            conversation.id,
            SUBJECT,
            title=None,
            archived=True,
            version=conversation.version,
            idempotency_key=f"archive-{status.value}",
            issuer=ISSUER,
        )


@pytest.mark.asyncio
async def test_archived_conversation_blocks_new_runs_configuration_and_retry_but_owner_can_read(
) -> None:
    store, models = await _store_with_models()
    conversation, _, run = await _conversation(store, models, "archive me")
    await store.start_run(run.id)
    await store.finish_run(run.id, RunStatus.COMPLETED)
    await store.update_metadata(
        conversation.id,
        SUBJECT,
        title=None,
        archived=True,
        version=conversation.version,
        idempotency_key="archive-read-only",
        issuer=ISSUER,
    )

    assert (await store.get(conversation.id, SUBJECT, ISSUER)).archived_at is not None
    with pytest.raises(ArchivedConversationConflict):
        await store.retry(run.id, SUBJECT, "retry-archived", ISSUER)
    with pytest.raises(ArchivedConversationConflict):
        await store.update_model(
            conversation.id,
            SUBJECT,
            "fake",
            conversation.version,
            models,
            "model-archived",
            ISSUER,
        )


@pytest.mark.asyncio
async def test_archived_cancel_rejects_without_consuming_idempotency_or_audit() -> None:
    store, models = await _store_with_models()
    conversation, _, run = await _conversation(store, models, "cancel archive")
    await store.start_run(run.id)
    await store.finish_run(run.id, RunStatus.COMPLETED)
    await store.update_metadata(
        conversation.id,
        SUBJECT,
        title=None,
        archived=True,
        version=conversation.version,
        idempotency_key="cancel-archive-lifecycle",
        issuer=ISSUER,
    )
    audit_before = list(store.auth_audit)
    with pytest.raises(ArchivedConversationConflict):
        await store.request_cancel(run.id, SUBJECT, "cancel-after-archive", ISSUER)
    assert store.auth_audit == audit_before

    restored = await store.update_metadata(
        conversation.id,
        SUBJECT,
        title=None,
        archived=False,
        version=conversation.version,
        idempotency_key="cancel-archive-restore",
        issuer=ISSUER,
    )
    _, _, next_run = await store.add_run(
        conversation.id,
        SUBJECT,
        "new active run",
        restored.version,
        "cancel-after-archive-new-run",
        ISSUER,
    )
    canceled = await store.request_cancel(
        next_run.id, SUBJECT, "cancel-after-archive", ISSUER
    )
    assert canceled.status is RunStatus.CANCEL_REQUESTED


@pytest.mark.asyncio
async def test_metadata_list_filters_are_literal_case_insensitive_and_activity_bounded() -> None:
    store, models = await _store_with_models()
    first, _, first_run = await _conversation(store, models, "Alpha private transcript")
    second, _, _ = await _conversation(store, models, "Beta title")
    first.title = "Alpha title"
    await store.start_run(first_run.id)
    await store.finish_run(first_run.id, RunStatus.COMPLETED)
    # Keep test data deterministic and exercise activity rather than updated_at.
    first.messages[0].created_at = datetime(2026, 1, 10, tzinfo=UTC)
    second.messages[0].created_at = datetime(2026, 1, 20, tzinfo=UTC)
    second.runs[-1].status = RunStatus.FAILED

    by_title, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(q=" alpha "),
    )
    assert [item.id for item in by_title] == [first.id]
    assert (await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(q="private transcript"),
    ))[0] == []  # search is title-only, never message-content search

    by_model, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(model_id="fake"),
    )
    assert {item.id for item in by_model} == {first.id, second.id}
    by_status, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(run_status=RunStatus.FAILED),
    )
    assert [item.id for item in by_status] == [second.id]
    lower, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(
            activity_from=datetime(2026, 1, 10, tzinfo=UTC),
            activity_to=datetime(2026, 1, 20, tzinfo=UTC),
        ),
    )
    assert [item.id for item in lower] == [first.id]
    profile, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(agent_profile_id=first.agent_profile_id),
    )
    assert {item.id for item in profile} == {first.id, second.id}
    none, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(agent_profile_id=uuid4()),
    )
    assert none == []


@pytest.mark.asyncio
async def test_run_status_filter_paginates_matches_after_nonmatching_activity() -> None:
    store, models = await _store_with_models()
    first, _, first_run = await _conversation(store, models, "first failed")
    middle, _, middle_run = await _conversation(store, models, "middle completed")
    last, _, last_run = await _conversation(store, models, "last failed")
    first.messages[0].created_at = datetime(2026, 1, 1, tzinfo=UTC)
    middle.messages[0].created_at = datetime(2026, 1, 2, tzinfo=UTC)
    last.messages[0].created_at = datetime(2026, 1, 3, tzinfo=UTC)
    first_run.status = RunStatus.FAILED
    middle_run.status = RunStatus.COMPLETED
    last_run.status = RunStatus.FAILED

    filters = ConversationListFilters(run_status=RunStatus.FAILED)
    page, cursor = await store.list(SUBJECT, limit=1, issuer=ISSUER, filters=filters)
    assert [item.id for item in page] == [last.id]
    assert cursor is not None
    next_page, next_cursor = await store.list(
        SUBJECT, limit=1, cursor=cursor, issuer=ISSUER, filters=filters
    )
    assert [item.id for item in next_page] == [first.id]
    assert next_cursor is None


@pytest.mark.asyncio
async def test_cursors_are_versioned_filter_bound_and_legacy_is_default_only() -> None:
    store, models = await _store_with_models()
    first, _, first_run = await _conversation(store, models, "first")
    await store.start_run(first_run.id)
    await store.finish_run(first_run.id, RunStatus.COMPLETED)
    _, _, second_run = await _conversation(store, models, "second")
    await store.start_run(second_run.id)
    await store.finish_run(second_run.id, RunStatus.COMPLETED)

    page, cursor = await store.list(SUBJECT, limit=1, issuer=ISSUER)
    assert len(page) == 1 and cursor
    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    assert payload["v"] == 1
    assert set(payload) == {"v", "a", "i", "f"}
    with pytest.raises(InvalidConversationCursor):
        await store.list(
            SUBJECT,
            limit=1,
            cursor=cursor,
            issuer=ISSUER,
            filters=ConversationListFilters(q="first"),
        )

    activity = first.messages[0].created_at
    legacy = (
        base64.urlsafe_b64encode(f"{activity.isoformat()}|{first.id}".encode())
        .decode()
        .rstrip("=")
    )
    rest, _ = await store.list(SUBJECT, cursor=legacy, issuer=ISSUER)
    assert all(item.id != first.id for item in rest)
    with pytest.raises(InvalidConversationCursor):
        await store.list(
            SUBJECT,
            cursor=legacy,
            issuer=ISSUER,
            filters=ConversationListFilters(model_id="fake"),
        )


@pytest.mark.asyncio
async def test_list_and_direct_reads_are_principal_isolated() -> None:
    store, models = await _store_with_models()
    owner, _, owner_run = await _conversation(store, models, "owner title")
    await store.start_run(owner_run.id)
    await store.finish_run(owner_run.id, RunStatus.COMPLETED)
    other_issuer, _, other_run = await _conversation(
        store, models, "other issuer", issuer="https://other.example"
    )
    await store.start_run(other_run.id)
    await store.finish_run(other_run.id, RunStatus.COMPLETED)
    other_subject, _, other_subject_run = await _conversation(
        store, models, "other subject", subject="other"
    )
    await store.start_run(other_subject_run.id)
    await store.finish_run(other_subject_run.id, RunStatus.COMPLETED)

    listed, _ = await store.list(
        SUBJECT,
        issuer=ISSUER,
        filters=ConversationListFilters(archive_state=ConversationArchiveState.ALL),
    )
    assert [item.id for item in listed] == [owner.id]
    with pytest.raises(LookupError):
        await store.get(other_issuer.id, SUBJECT, ISSUER)
    with pytest.raises(LookupError):
        await store.get(other_subject.id, SUBJECT, ISSUER)


@pytest.mark.asyncio
async def test_metadata_idempotency_and_optimistic_version_conflicts_are_stable() -> None:
    store, models = await _store_with_models()
    conversation, _, _ = await _conversation(store, models, "idempotent")
    first = await store.update_metadata(
        conversation.id,
        SUBJECT,
        title="Stable",
        archived=None,
        version=conversation.version,
        idempotency_key="same",
        issuer=ISSUER,
    )
    replay = await store.update_metadata(
        conversation.id,
        SUBJECT,
        title="Stable",
        archived=None,
        version=1,
        idempotency_key="same",
        issuer=ISSUER,
    )
    assert replay is first
    with pytest.raises(IdempotencyConflict):
        await store.update_metadata(
            conversation.id,
            SUBJECT,
            title="Different",
            archived=None,
            version=1,
            idempotency_key="same",
            issuer=ISSUER,
        )
    with pytest.raises(VersionConflict):
        await store.update_metadata(
            conversation.id,
            SUBJECT,
            title="Fresh key but stale version",
            archived=None,
            version=1,
            idempotency_key="stale",
            issuer=ISSUER,
        )


@pytest.mark.asyncio
async def test_metadata_audit_is_identifier_only_and_excludes_title_and_search_content() -> None:
    store, models = await _store_with_models()
    conversation, _, _ = await _conversation(store, models, "message-secret")
    private_title = "private-title-secret"
    await store.update_metadata(
        conversation.id,
        SUBJECT,
        title=private_title,
        archived=None,
        version=conversation.version,
        idempotency_key="privacy-rename",
        issuer=ISSUER,
    )
    rendered = json.dumps(store.auth_audit)
    assert str(conversation.id) in rendered
    assert private_title not in rendered
    assert "message-secret" not in rendered
    for item in store.auth_audit:
        metadata = item["metadata"]
        assert isinstance(metadata, dict)
        metadata_map = cast(dict[str, object], metadata)
        assert set(metadata_map) <= {"conversationId", "version", "archived"}
        assert "title" not in metadata_map
