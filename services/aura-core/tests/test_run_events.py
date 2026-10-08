"""Regression coverage for durable run-event sequencing."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.events import RunEvent, new_event
from aura_core.domains.execution.runs.persistence import RunEventRow, RunRow
from aura_core.domains.execution.runs.repository import SqlRunRepository


class _Session:
    """Small session double for the repository's lock/idempotency boundary."""

    def __init__(self, run_id: UUID) -> None:
        self.run = SimpleNamespace(id=run_id)
        self.events: dict[UUID, Any] = {}
        self.max_sequence: int | None = None
        self.added: list[Any] = []
        self.run_lock_requested = False
        self.scalar_calls = 0

    async def get(self, model: object, identifier: object, **kwargs: object) -> Any:
        if model is RunEventRow:
            return self.events.get(identifier) if isinstance(identifier, UUID) else None
        assert model is RunRow
        assert kwargs == {"with_for_update": True}
        self.run_lock_requested = True
        return self.run if identifier == self.run.id else None

    async def scalar(self, statement: object) -> int | None:
        del statement
        self.scalar_calls += 1
        return self.max_sequence

    def add(self, row: Any) -> None:
        self.added.append(row)
        self.events[row.id] = row
        self.max_sequence = row.sequence


def _event(run_id: UUID, *, event_id: UUID | None = None) -> RunEvent:
    return new_event(
        "run.completed",
        run_id,
        uuid4(),
        0,
        {"status": "completed"},
    ) if event_id is None else RunEvent(
        event_id,
        0,
        "run.completed",
        run_id,
        uuid4(),
        datetime.now(UTC),
        {"status": "completed"},
    )


@pytest.mark.asyncio
async def test_append_event_allocates_zero_then_one_without_sequence_collision() -> None:
    run_id = uuid4()
    session: Any = _Session(run_id)
    repository = SqlRunRepository()

    first = await repository.append_event(session, _event(run_id))  # type: ignore[arg-type]
    second = await repository.append_event(session, _event(run_id))  # type: ignore[arg-type]

    assert (first.sequence, second.sequence) == (0, 1)
    assert [row.sequence for row in session.added] == [0, 1]
    assert session.run_lock_requested


@pytest.mark.asyncio
async def test_append_event_replay_returns_original_durable_event() -> None:
    run_id = uuid4()
    event = _event(run_id)
    session: Any = _Session(run_id)
    repository = SqlRunRepository()

    persisted = await repository.append_event(session, event)  # type: ignore[arg-type]
    replayed = await repository.append_event(  # type: ignore[arg-type]
        session,
        RunEvent(
            event.event_id,
            999,
            event.event_type,
            event.run_id,
            event.conversation_id,
            event.occurred_at,
            {"status": "different producer payload"},
            event.schema_version,
        ),
    )

    assert replayed == persisted
    assert len(session.added) == 1
    assert session.scalar_calls == 1
