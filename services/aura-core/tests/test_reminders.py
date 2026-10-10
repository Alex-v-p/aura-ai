"""Acceptance coverage for the owner-scoped one-shot reminder domain.

The repository tests exercise the public domain port with deterministic time.
They intentionally assert owner, lifecycle, idempotency, cursor, and
observability invariants without depending on SQLAlchemy internals.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from aura_core.domains.automation.reminders.public import (
    REMINDER_TOPIC,
    ReminderIdempotencyConflict,
    ReminderNotFound,
    ReminderStatus,
    ReminderTransitionError,
    ReminderValidationError,
    ReminderVersionConflict,
    ReminderView,
    as_payload,
    event_command_id,
    normalize_content,
    normalize_due_at,
)
from aura_core.domains.automation.reminders.repository import (
    MemoryReminderRepository,
    SqlReminderRepository,
)
from aura_core.platform.outbox import make_aggregate_command
from aura_core.platform.telemetry import MetadataMetrics

OWNER_ISSUER = "https://authentik.example"
OWNER = "alice"
OTHER_OWNER = "bob"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EVENT_SCHEMA = (
    Path(__file__).parents[3] / "contracts/events/automation/v1/reminder-events.schema.json"
)


def _repo() -> MemoryReminderRepository:
    return MemoryReminderRepository(clock=lambda: NOW)


async def _create(
    repo: MemoryReminderRepository,
    *,
    owner: str = OWNER,
    content: str = "  call the dentist  ",
    key: str | None = None,
    due_at: datetime | None = None,
):
    return await repo.create(
        OWNER_ISSUER,
        owner,
        content=content,
        due_at=due_at or datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
        idempotency_key=key or str(uuid4()),
    )


def test_content_and_due_time_require_public_invariants() -> None:
    assert normalize_content("  a reminder  ") == "a reminder"
    with pytest.raises(ReminderValidationError):
        normalize_content("   ")
    with pytest.raises(ReminderValidationError):
        normalize_content("x" * 2001)
    with pytest.raises(ReminderValidationError, match="explicit RFC3339 offset"):
        normalize_due_at(datetime(2026, 2, 1, 9, 0), now=NOW)
    with pytest.raises(ReminderValidationError, match="future"):
        normalize_due_at(NOW, now=NOW)


def test_offset_and_dst_equivalent_instants_are_normalized_to_the_same_utc_value() -> None:
    # These are the same instant around the Europe/Brussels spring-forward
    # transition, represented with explicit RFC3339 offsets.
    first = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    second = datetime(2026, 3, 29, 3, 30, tzinfo=ZoneInfo("Europe/Brussels"))
    assert first.utcoffset() is not None and second.utcoffset() is not None
    assert normalize_due_at(first, now=NOW) == normalize_due_at(second, now=NOW)
    assert normalize_due_at(second, now=NOW).tzinfo is UTC


def _validate_event_schema(payload: dict[str, object], *, event_type: str, status: str) -> None:
    """Validate the bounded event schema without adding a runtime dependency.

    The contract is deliberately small and closed.  This helper interprets
    its common and status-specific definitions directly, so the test still
    validates the checked-in schema and remains runnable in the Core test
    environment, which does not package a JSON Schema library.
    """

    schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
    common = schema["$defs"]["EventProperties"]
    branch = next(
        definition
        for name, definition in schema["$defs"].items()
        if name.startswith("Reminder")
        and name.removeprefix("Reminder").removesuffix("Event").casefold() == event_type
    )
    common_properties = set(common["properties"])
    branch_properties = set(branch["allOf"][1]["properties"])
    assert set(payload) == common_properties | branch_properties
    assert set(common["required"]) <= set(payload)
    assert payload["schemaVersion"] == 1
    assert payload["aggregateType"] == "automation.reminder"
    assert payload["eventType"] == f"reminder.{event_type}"
    assert payload["status"] == status
    assert type(payload["version"]) is int and payload["version"] >= 1
    for key in ("commandId", "aggregateId", "correlationId", "causationId"):
        value = payload[key]
        assert isinstance(value, str)
        assert len(value.split("-")) == 5
    assert isinstance(payload["createdAt"], str)


def _event_payload(item: object, *, operation: str) -> dict[str, object]:
    reminder = cast(Any, item)
    command = make_aggregate_command(
        REMINDER_TOPIC,
        event_command_id(reminder.id, reminder.status, reminder.version),
        "automation.reminder",
        reminder.id,
        identifiers=(
            ("eventType", f"reminder.{operation}"),
            ("status", reminder.status.value),
            ("version", reminder.version),
        ),
    )
    payload = command.payload()
    assert type(payload["version"]) is int
    return payload


@pytest.mark.asyncio
async def test_real_reminder_lifecycle_payloads_match_closed_event_schema() -> None:
    repository = MemoryReminderRepository(clock=lambda: NOW)
    created = await _create(repository, key="schema-created")
    fired = replace(
        created,
        status=ReminderStatus.FIRED,
        version=2,
        fired_at=NOW,
    )
    completed = replace(fired, status=ReminderStatus.COMPLETED, version=3, completed_at=NOW)
    canceled = replace(created, status=ReminderStatus.CANCELED, version=2, canceled_at=NOW)
    for item, operation in (
        (created, "created"),
        (fired, "fired"),
        (completed, "completed"),
        (canceled, "canceled"),
    ):
        payload = _event_payload(item, operation=operation)
        _validate_event_schema(payload, event_type=operation, status=item.status.value)
        response = as_payload(item)
        assert type(response["version"]) is int
        assert response["status"] == item.status.value


@pytest.mark.asyncio
async def test_create_is_owner_scoped_and_exactly_replayable() -> None:
    repo = _repo()
    reminder = await _create(repo, key="create-1")
    assert reminder.content == "call the dentist"
    assert reminder.status is ReminderStatus.SCHEDULED
    assert reminder.due_at.tzinfo is UTC
    assert (
        await repo.create(
            OWNER_ISSUER,
            OWNER,
            content="call the dentist",
            due_at=datetime(2026, 2, 1, 10, 0, tzinfo=ZoneInfo("Europe/Brussels")),
            idempotency_key="create-1",
        )
        == reminder
    )
    with pytest.raises(ReminderIdempotencyConflict):
        await _create(repo, key="create-1", content="different content")

    with pytest.raises(ReminderNotFound):
        await repo.get(OWNER_ISSUER, OTHER_OWNER, reminder.id)
    assert (
        await repo.list(OWNER_ISSUER, OTHER_OWNER, view=ReminderView.UPCOMING, limit=10)
    ).items == ()


@pytest.mark.asyncio
async def test_create_replay_precedes_due_validation_after_clock_advances() -> None:
    current = [NOW]
    repo = MemoryReminderRepository(clock=lambda: current[0])
    due = datetime(2026, 2, 1, 9, tzinfo=UTC)
    original = await repo.create(
        OWNER_ISSUER,
        OWNER,
        content="clock-stable command",
        due_at=due,
        idempotency_key="clock-advanced-create",
    )
    current[0] = datetime(2026, 2, 2, 9, tzinfo=UTC)
    assert (
        await repo.create(
            OWNER_ISSUER,
            OWNER,
            content="clock-stable command",
            due_at=due,
            idempotency_key="clock-advanced-create",
        )
        == original
    )
    with pytest.raises(ReminderIdempotencyConflict):
        await repo.create(
            OWNER_ISSUER,
            OWNER,
            content="mismatched after clock advance",
            due_at=due,
            idempotency_key="clock-advanced-create",
        )


@pytest.mark.asyncio
async def test_lifecycle_transitions_allow_expected_paths_and_keep_terminal_rows_immutable() -> (
    None
):
    repo = _repo()
    scheduled = await _create(repo, key="lifecycle-1")
    completed = await repo.complete(
        OWNER_ISSUER, OWNER, scheduled.id, expected_version=1, idempotency_key="complete-1"
    )
    assert completed.status is ReminderStatus.COMPLETED
    assert completed.version == 2
    assert completed.completed_at == NOW
    with pytest.raises(ReminderTransitionError):
        await repo.cancel(
            OWNER_ISSUER,
            OWNER,
            scheduled.id,
            expected_version=2,
            idempotency_key="cancel-after-complete",
        )
    with pytest.raises(ReminderTransitionError):
        await repo.complete(
            OWNER_ISSUER, OWNER, scheduled.id, expected_version=2, idempotency_key="complete-again"
        )

    canceled = await _create(repo, key="lifecycle-2")
    canceled = await repo.cancel(
        OWNER_ISSUER, OWNER, canceled.id, expected_version=1, idempotency_key="cancel-1"
    )
    assert canceled.status is ReminderStatus.CANCELED
    with pytest.raises(ReminderTransitionError):
        await repo.cancel(
            OWNER_ISSUER, OWNER, canceled.id, expected_version=2, idempotency_key="cancel-again"
        )

    # The fired -> completed path is consumed by the later scheduler work
    # item, but the public lifecycle port must preserve that allowed transition.
    fired = await _create(repo, key="lifecycle-3")
    cast(Any, repo)._items[fired.id] = replace(
        fired, status=ReminderStatus.FIRED, version=2, fired_at=NOW
    )
    completed_fired = await repo.complete(
        OWNER_ISSUER, OWNER, fired.id, expected_version=2, idempotency_key="complete-fired"
    )
    assert completed_fired.status is ReminderStatus.COMPLETED


@pytest.mark.asyncio
async def test_mutations_enforce_version_and_idempotency_conflicts_without_side_effects() -> None:
    metrics = MetadataMetrics()
    repo = MemoryReminderRepository(clock=lambda: NOW, metrics=metrics)
    reminder = await _create(repo, key="version-1")
    with pytest.raises(ReminderVersionConflict):
        await repo.complete(
            OWNER_ISSUER,
            OWNER,
            reminder.id,
            expected_version=99,
            idempotency_key="version-conflict",
        )
    assert await repo.get(OWNER_ISSUER, OWNER, reminder.id) == reminder
    with pytest.raises(ReminderIdempotencyConflict):
        await repo.complete(
            OWNER_ISSUER, OWNER, reminder.id, expected_version=1, idempotency_key="version-1"
        )
    assert {item.metric for item in metrics.snapshot()} >= {
        "reminder_version_conflict_count",
        "reminder_idempotency_conflict_count",
    }
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_owner_bound_opaque_cursors_are_stable_and_cannot_cross_owners_or_views() -> None:
    repo = _repo()
    await _create(repo, key="cursor-1", content="first", due_at=datetime(2026, 2, 1, 9, tzinfo=UTC))
    second = await _create(
        repo, key="cursor-2", content="second", due_at=datetime(2026, 2, 2, 9, tzinfo=UTC)
    )
    first_page = await repo.list(OWNER_ISSUER, OWNER, view=ReminderView.UPCOMING, limit=1)
    assert first_page.next_cursor
    second_page = await repo.list(
        OWNER_ISSUER, OWNER, view=ReminderView.UPCOMING, limit=1, cursor=first_page.next_cursor
    )
    assert [item.id for item in second_page.items] == [second.id]
    with pytest.raises(ReminderValidationError, match="invalid reminder cursor"):
        await repo.list(
            OWNER_ISSUER,
            OTHER_OWNER,
            view=ReminderView.UPCOMING,
            limit=1,
            cursor=first_page.next_cursor,
        )
    with pytest.raises(ReminderValidationError, match="invalid reminder cursor"):
        await repo.list(
            OWNER_ISSUER, OWNER, view=ReminderView.HISTORY, limit=1, cursor=first_page.next_cursor
        )


@pytest.mark.asyncio
async def test_lifecycle_events_audit_and_metrics_are_content_free() -> None:
    secret = "private reminder content that must never leak"
    metrics = MetadataMetrics()
    repo = MemoryReminderRepository(clock=lambda: NOW, metrics=metrics)
    reminder = await _create(repo, content=secret, key="privacy-1")
    await repo.complete(
        OWNER_ISSUER, OWNER, reminder.id, expected_version=1, idempotency_key="privacy-2"
    )
    for record in (*repo.events, *repo.audit, *(metrics.snapshot())):
        assert secret not in repr(record)
    allowed_event_fields = {
        "topic",
        "aggregateType",
        "aggregateId",
        "eventType",
        "status",
        "version",
    }
    assert all(set(event) <= allowed_event_fields for event in repo.events)


@pytest.mark.asyncio
async def test_sql_reminder_stage_spans_are_manifest_aligned_on_success_and_failure() -> None:
    metrics = MetadataMetrics()
    repository = SqlReminderRepository(cast(Any, None), metrics=metrics)
    item = await _create(_repo(), key="stage-span-seed")

    class Outbox:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        async def stage_command(self, session: Any, *, command: Any) -> None:
            del session, command
            if self.fail:
                raise RuntimeError("outbox unavailable")

    class Identities:
        async def resolve(self, session: Any, issuer: str, subject: str) -> Any:
            del session, issuer, subject
            return SimpleNamespace(id=uuid4())

    class Audit:
        def stage(
            self,
            session: Any,
            principal_id: Any,
            action: str,
            resource: str,
            metadata: dict[str, object],
        ) -> None:
            del session, principal_id, action, resource, metadata

    cast(Any, repository)._identities = Identities()
    cast(Any, repository)._audit = Audit()
    cast(Any, repository)._outbox = Outbox()
    await cast(Any, repository)._stage(object(), item, "created", OWNER_ISSUER, OWNER)
    cast(Any, repository)._outbox = Outbox(fail=True)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await cast(Any, repository)._stage(object(), item, "completed", OWNER_ISSUER, OWNER)
    cast(Any, repository)._outbox = Outbox()
    await cast(Any, repository)._stage(object(), item, "canceled", OWNER_ISSUER, OWNER)

    allowed_trace_fields = {
        "operation",
        "reminder_id",
        "aggregate_id",
        "aggregate_type",
        "event_type",
        "event_status",
        "event_version",
        "command_id",
        "correlation_id",
        "causation_id",
    }
    spans = [measurement for measurement in metrics.snapshot() if measurement.kind == "span"]
    assert {dict(span.trace_attributes)["operation"] for span in spans} == {
        "reminder.create",
        "reminder.complete",
        "reminder.cancel",
    }
    for span in spans:
        assert span.component_id == "aura.automation.reminder_management"
        attributes = dict(span.trace_attributes)
        assert set(attributes) <= allowed_trace_fields
        assert attributes["reminder_id"] == str(item.id)
        assert attributes["aggregate_id"] == str(item.id)
        assert attributes["aggregate_type"] == "automation.reminder"
        assert attributes["event_type"].startswith("reminder.")
        assert attributes["event_status"] == item.status.value
        assert attributes["event_version"] == str(item.version)
        assert attributes["command_id"]
        assert attributes["correlation_id"]
        assert attributes["causation_id"]
        assert dict(span.dimensions)["dependency"] == "postgresql"
        assert span.trace_id == item.id.hex
        assert all(
            key not in attributes
            for key in {
                "content",
                "prompt",
                "response",
                "payload",
                "token",
                "credential",
                "owner_subject",
            }
        )
    assert {dict(span.dimensions)["outcome"] for span in spans} == {"ok", "error"}
    assert all(
        measurement.metric == "reminder_command_duration_ms"
        for measurement in metrics.snapshot()
        if measurement.kind == "span"
    )
    assert metrics.stats().rejected == 0


def _postgres_url_or_skip() -> str:
    url = os.environ.get("AURA_0054_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0054_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")
    return url


@pytest.mark.asyncio
async def test_postgresql_concurrent_idempotency_reuse_is_exact_or_conflicting() -> None:
    """Exercise advisory-lock serialization against the real SQL repository."""

    url = _postgres_url_or_skip()
    try:
        from alembic import command
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "0014_reminder_foundation")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    current = [NOW]
    repository = SqlReminderRepository(sessions, clock=lambda: current[0])
    subject = f"concurrency-{uuid4()}"
    due = datetime(2026, 2, 1, 9, tzinfo=UTC)
    try:

        async def create_same() -> object:
            return await repository.create(
                OWNER_ISSUER,
                subject,
                content="same command",
                due_at=due,
                idempotency_key="pg-create-same",
            )

        same_results = await asyncio.gather(create_same(), create_same(), return_exceptions=True)
        same_successes = [item for item in same_results if not isinstance(item, Exception)]
        assert len(same_successes) == 2
        assert same_successes[0] == same_successes[1]
        current[0] = datetime(2026, 2, 2, 9, tzinfo=UTC)
        replayed = await repository.create(
            OWNER_ISSUER,
            subject,
            content="same command",
            due_at=due,
            idempotency_key="pg-create-same",
        )
        assert replayed == same_successes[0]
        with pytest.raises(ReminderIdempotencyConflict):
            await repository.create(
                OWNER_ISSUER,
                subject,
                content="mismatched after clock advance",
                due_at=due,
                idempotency_key="pg-create-same",
            )

        from aura_core.domains.governance.audit.persistence import AuditRow
        from sqlalchemy import select

        async with sessions() as session:
            audit_row = (
                await session.execute(
                    select(AuditRow).where(
                        AuditRow.action == "reminder.created",
                        AuditRow.resource == str(cast(Any, same_successes[0]).id),
                    )
                )
            ).scalar_one()
            assert audit_row.principal_id is not None
            assert "same command" not in repr(audit_row.metadata_)

        async def create_conflicting(content: str) -> object:
            return await repository.create(
                OWNER_ISSUER,
                subject,
                content=content,
                due_at=due,
                idempotency_key="pg-create-conflict",
            )

        conflicting_results = await asyncio.gather(
            create_conflicting("first command"),
            create_conflicting("second command"),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in conflicting_results) == 1
        assert (
            sum(isinstance(item, ReminderIdempotencyConflict) for item in conflicting_results) == 1
        )

        reminder = cast(Any, same_successes[0])

        async def complete_same() -> object:
            return await repository.complete(
                OWNER_ISSUER,
                subject,
                reminder.id,
                expected_version=1,
                idempotency_key="pg-complete-same",
            )

        complete_results = await asyncio.gather(
            complete_same(), complete_same(), return_exceptions=True
        )
        complete_successes = [item for item in complete_results if not isinstance(item, Exception)]
        assert len(complete_successes) == 2
        assert complete_successes[0] == complete_successes[1]
        assert cast(Any, complete_successes[0]).version == 2
        with pytest.raises(ReminderIdempotencyConflict):
            await repository.complete(
                OWNER_ISSUER,
                subject,
                reminder.id,
                expected_version=2,
                idempotency_key="pg-complete-same",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_foreign_owner_access_is_non_disclosing_and_denied() -> None:
    url = _postgres_url_or_skip()
    try:
        from alembic import command
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "0014_reminder_foundation")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlReminderRepository(sessions, clock=lambda: NOW)
    owner = f"owner-{uuid4()}"
    foreign = f"foreign-{uuid4()}"
    reminder = await repository.create(
        OWNER_ISSUER,
        owner,
        content="owner-private",
        due_at=datetime(2026, 2, 1, 9, tzinfo=UTC),
        idempotency_key="foreign-owner-create",
    )
    try:
        with pytest.raises(ReminderNotFound, match="^reminder not found$"):
            await repository.get(OWNER_ISSUER, foreign, reminder.id)
        assert (
            await repository.list(OWNER_ISSUER, foreign, view=ReminderView.UPCOMING, limit=10)
        ).items == ()
        with pytest.raises(ReminderNotFound, match="^reminder not found$"):
            await repository.complete(
                OWNER_ISSUER,
                foreign,
                reminder.id,
                expected_version=1,
                idempotency_key="foreign-owner-complete",
            )
        with pytest.raises(ReminderNotFound, match="^reminder not found$"):
            await repository.cancel(
                OWNER_ISSUER,
                foreign,
                reminder.id,
                expected_version=1,
                idempotency_key="foreign-owner-cancel",
            )
    finally:
        await engine.dispose()
