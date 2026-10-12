"""Regression coverage for durable run-event sequencing."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.events import RunEvent, new_event, validate_tool_status
from aura_core.domains.execution.runs.persistence import RunEventRow, RunRow
from aura_core.domains.execution.runs.repository import SqlRunRepository
from aura_core.runtime.streaming.publisher import EventPublisher


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


def test_tool_status_event_payload_is_identifier_and_outcome_only() -> None:
    event = new_event(
        "tool.status",
        uuid4(),
        uuid4(),
        0,
        {
            "callId": uuid4(),
            "toolId": "memory.search",
            "toolVersion": 1,
            "status": "failed",
            "errorClass": "timeout",
            "errorCode": "TOOL_TIMEOUT",
        },
    )
    payload = event.payload()
    assert payload["eventType"] == "tool.status"
    assert set(payload["data"]) <= {
        "callId",
        "toolId",
        "toolVersion",
        "status",
        "errorClass",
        "errorCode",
        "resultCount",
    }
    assert not {
        "query",
        "arguments",
        "content",
        "result",
        "prompt",
        "response",
        "reasoning",
    } & set(payload["data"])


def test_tool_status_validation_rejects_content_and_unbounded_outcomes() -> None:
    valid = {
        "callId": uuid4(),
        "toolId": "memory.search",
        "toolVersion": 1,
        "status": "completed",
        "resultCount": 1,
    }
    assert validate_tool_status(valid)["toolId"] == "memory.search"
    with pytest.raises(ValueError):
        validate_tool_status({**valid, "query": "private"})
    with pytest.raises(ValueError):
        validate_tool_status({**valid, "resultCount": 9})
    with pytest.raises(ValueError):
        validate_tool_status({**valid, "status": "running"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("toolId", "ollama://private-host/memory.search"),
        ("toolId", "memory-search"),
        ("errorCode", "provider.error"),
        ("errorCode", "TOOL-FAIL"),
        ("errorCode", "tool_failure"),
    ],
)
def test_tool_status_rejects_provider_names_and_non_contract_identifiers(
    field: str, value: str
) -> None:
    valid = {
        "callId": uuid4(),
        "toolId": "memory.search",
        "toolVersion": 1,
        "status": "failed",
    }
    with pytest.raises(ValueError):
        validate_tool_status({**valid, field: value})


@pytest.mark.asyncio
async def test_tool_status_replay_boundary_validates_before_persistence() -> None:
    invalid_payloads = (
        {
            "callId": uuid4(),
            "toolId": "memory.search",
            "toolVersion": 1,
            "status": "completed",
            "content": "private memory text",
        },
        {
            "callId": uuid4(),
            "toolId": "ollama://private-host/memory.search",
            "toolVersion": 1,
            "status": "failed",
            "errorCode": "provider.error",
        },
    )
    for operation in ("publish", "hydrate", "ingest_external"):
        for payload in invalid_payloads:
            publisher = EventPublisher()
            event = new_event("tool.status", uuid4(), uuid4(), 0, payload)
            with pytest.raises(ValueError):
                if operation == "publish":
                    await publisher.publish(event)
                elif operation == "hydrate":
                    await publisher.hydrate([event])
                else:
                    await publisher.ingest_external(event)


def test_approval_run_lifecycle_is_versioned_and_metadata_only() -> None:
    schema_path = (
        Path(__file__).parents[3]
        / "contracts/events/runs/v1/run-events.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "waiting_for_confirmation" in schema["$defs"]["RunStatus"]["enum"]
    assert "awaiting_confirmation" in schema["$defs"]["ToolStatus"]["enum"]
    run_properties = schema["$defs"]["Run"]["properties"]
    assert {
        "clientTimeZone",
        "approvalId",
        "pendingToolCallId",
        "toolLifecycleStatus",
    } <= set(run_properties)
    properties = schema["$defs"]["ToolStatusData"]["properties"]
    assert not {
        "content",
        "dueAt",
        "arguments",
        "prompt",
        "response",
    } & set(properties)
