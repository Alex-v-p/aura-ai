"""AURA-0133 lifecycle-event outbox compatibility coverage."""

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.platform.outbox.nats import (
    BRIEFING_EVENT_SUBJECT,
    BRIEFING_WAKE_SUBJECT,
    NatsOutbox,
)
from aura_core.platform.outbox.service import OutboxCommand


def _schedule_event() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "eventId": str(uuid4()),
        "eventType": "briefing.schedule.created",
        "scheduleId": str(uuid4()),
        "scheduleStatus": "active",
        "scheduleVersion": 1,
        "occurredAt": "2026-10-27T08:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_briefing_lifecycle_outbox_round_trip_publishes_exact_contract() -> None:
    payload = _schedule_event()
    event_id = payload["eventId"]
    assert isinstance(event_id, str)
    occurred_at = datetime.fromisoformat(str(payload["occurredAt"]))
    command = OutboxCommand.from_record(
        uuid4(), BRIEFING_EVENT_SUBJECT, payload, occurred_at
    )

    assert command.topic == BRIEFING_EVENT_SUBJECT
    assert command.payload() == payload
    assert command.aggregate_type == "automation.briefing"
    assert str(command.aggregate_id) == payload["scheduleId"]

    class JetStream:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes, dict[str, str]]] = []

        async def publish(
            self, subject: str, data: bytes, *, headers: dict[str, str]
        ) -> Any:
            self.calls.append((subject, data, headers))
            return type("Ack", (), {"duplicate": False})()

    transport = NatsOutbox("nats://unused")
    jetstream = JetStream()
    cast(Any, transport)._jetstream = jetstream
    assert await transport.publish(command) is False

    assert len(jetstream.calls) == 1
    subject, encoded, headers = jetstream.calls[0]
    assert subject == BRIEFING_EVENT_SUBJECT
    assert json.loads(encoded) == payload
    assert headers == {"Nats-Msg-Id": str(command.id)}


def test_briefing_lifecycle_payload_rejects_private_or_unknown_fields() -> None:
    payload = _schedule_event()
    payload["content"] = "must never cross the lifecycle subject"
    event_id = payload["eventId"]
    assert isinstance(event_id, str)
    with pytest.raises(ValueError, match="schema exact"):
        OutboxCommand.from_record(
            uuid4(),
            BRIEFING_EVENT_SUBJECT,
            payload,
            datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_worker_outbox_readiness_binds_lifecycle_and_wake_subjects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JetStream:
        def __init__(self) -> None:
            self.subjects: list[str] = []
            self.added: list[tuple[str, ...]] = []

        async def streams_info(self) -> list[object]:
            return []

        async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
            assert name == "AURA_RUNS"
            self.added.append(subjects)
            self.subjects = list(subjects)

        async def account_info(self) -> object:
            return object()

        async def stream_info(self, name: str) -> object:
            return type(
                "StreamInfo",
                (),
                {"config": type("Config", (), {"name": name, "subjects": self.subjects})()},
            )()

    class Connection:
        is_closed = False

        def __init__(self, jetstream: JetStream) -> None:
            self._jetstream = jetstream

        def jetstream(self) -> JetStream:
            return self._jetstream

    jetstream = JetStream()
    connection = Connection(jetstream)

    async def connect(url: str) -> Connection:
        assert url == "nats://unused"
        return connection

    monkeypatch.setattr("aura_core.platform.outbox.nats.nats.connect", connect)
    transport = NatsOutbox(
        "nats://unused",
        additional_subjects=(BRIEFING_EVENT_SUBJECT, BRIEFING_WAKE_SUBJECT),
    )
    await transport.connect()
    ready, detail = await transport.readiness()

    assert ready and detail is None
    assert len(jetstream.added) == 1
    expected = {
        "aura.runs.execute.v1",
        BRIEFING_EVENT_SUBJECT,
        BRIEFING_WAKE_SUBJECT,
    }
    assert set(jetstream.added[0]) == expected
    assert set(jetstream.subjects) == expected
