"""Focused compatibility and safety tests for the platform outbox."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Settings
from aura_core.platform.outbox import (
    OutboxCommand,
    identifier_trace_metadata,
    make_aggregate_command,
)


def test_run_envelope_remains_byte_compatible() -> None:
    run_id = uuid4()
    conversation_id = uuid4()
    command = OutboxCommand(
        uuid4(),
        "aura.runs.execute.v1",
        run_id,
        conversation_id,
        datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
        run_id,
        run_id,
    )

    assert (
        command.wire_payload()
        == json.dumps(
            {
                "schemaVersion": 1,
                "commandId": str(command.id),
                "runId": str(run_id),
                "conversationId": str(conversation_id),
                "createdAt": "2026-01-02T03:04:05.678901+00:00",
                "correlationId": str(run_id),
                "causationId": str(run_id),
            },
            separators=(",", ":"),
        ).encode()
    )


def test_generic_aggregate_command_has_no_run_or_conversation_ids() -> None:
    reminder_id = uuid4()
    command = make_aggregate_command(
        "aura.automation.events.v1",
        uuid4(),
        "automation.reminder",
        reminder_id,
        identifiers=(
            ("eventType", "reminder.created"),
            ("status", "scheduled"),
            ("version", 3),
        ),
    )

    payload = json.loads(command.wire_payload())
    assert command.run_id is None
    assert command.conversation_id is None
    assert payload["aggregateType"] == "automation.reminder"
    assert payload["aggregateId"] == str(reminder_id)
    assert payload["version"] == 3
    assert "runId" not in payload
    assert "conversationId" not in payload
    assert identifier_trace_metadata(command)["aggregate_type"] == "automation.reminder"
    assert identifier_trace_metadata(command)["aggregate_id"] == str(reminder_id)
    assert (
        OutboxCommand.from_record(command.id, command.topic, payload, command.created_at) == command
    )


def test_generic_aggregate_command_rejects_content_identifiers() -> None:
    with pytest.raises(ValueError, match="content-bearing"):
        make_aggregate_command(
            "aura.automation.events.v1",
            uuid4(),
            "reminder",
            uuid4(),
            identifiers=(("content", "private-reminder"),),
        )


def test_notification_event_schema_and_outbox_payload_are_identifier_only() -> None:
    root = Path(__file__).parents[3]
    schema = json.loads(
        (root / "contracts/events/automation/v1/notification-events.schema.json").read_text()
    )
    props = schema["$defs"]["NotificationEventProperties"]["properties"]
    assert not {"content", "prompt", "response", "credential"} & set(props)
    command = make_aggregate_command(
        "aura.automation.notifications.v1",
        uuid4(),
        "automation.reminder",
        uuid4(),
        identifiers=(
            ("eventType", "reminder.fired"),
            ("status", "fired"),
            ("version", 2),
        ),
    )
    payload = json.loads(command.wire_payload())
    assert "runId" not in payload and "conversationId" not in payload
    assert not {"content", "prompt", "response"} & set(payload)


def test_generic_envelope_rejects_run_identity_mixing() -> None:
    with pytest.raises(ValueError, match="run or conversation"):
        OutboxCommand(
            uuid4(),
            "aura.automation.events.v1",
            uuid4(),
            uuid4(),
            datetime.now(UTC),
            aggregate_type="reminder",
            aggregate_id=uuid4(),
        )


def test_generic_reminder_dispatch_records_accepted_metadata_only_telemetry() -> None:
    async def scenario() -> None:
        state = AppState(
            Settings(
                public_origin="https://aura.dev.example",
                oidc_issuer="https://authentik.dev.example",
                owner_subject="owner",
                secure_cookies=False,
            ),
            testing=True,
        )
        reminder_id = uuid4()
        command = make_aggregate_command(
            "aura.automation.reminders.v1",
            uuid4(),
            "automation.reminder",
            reminder_id,
            identifiers=(
                ("eventType", "reminder.created"),
                ("status", "scheduled"),
                ("version", 1),
            ),
        )

        class Store:
            def __init__(self) -> None:
                self.pending_calls = 0
                self.published = asyncio.Event()

            async def pending_commands(self) -> list[Any]:
                self.pending_calls += 1
                return [command] if self.pending_calls == 1 else []

            async def mark_published(self, command_id: Any) -> None:
                assert command_id == command.id
                self.published.set()

        class Nats:
            async def publish(self, value: Any) -> bool:
                assert value == command
                return False

        store = Store()
        state.sql_store = cast(Any, store)
        state.nats = cast(Any, Nats())
        task = asyncio.create_task(cast(Any, state)._dispatch_outbox())
        try:
            await asyncio.wait_for(store.published.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        measurements = state.metrics.snapshot()
        dispatch_spans = [
            item
            for item in measurements
            if item.kind == "span"
            and item.component_id == "aura.automation.reminder_management"
            and dict(item.trace_attributes).get("operation") == "outbox.dispatch"
        ]
        allowed_trace_fields = {
            "operation",
            "reminder_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "event_status",
            "event_version",
            "command_id",
            "correlation_id",
            "causation_id",
        }
        assert dispatch_spans
        for span in dispatch_spans:
            attributes = dict(span.trace_attributes)
            assert set(attributes) <= allowed_trace_fields
            assert attributes["reminder_id"] == str(reminder_id)
            assert attributes["aggregate_type"] == "automation.reminder"
            assert attributes["aggregate_id"] == str(reminder_id)
            assert attributes["event_type"] == "reminder.created"
            assert attributes["event_status"] == "scheduled"
            assert attributes["event_version"] == "1"
            assert attributes["command_id"] == str(command.id)
            assert attributes["correlation_id"] == str(command.correlation_id)
            assert attributes["causation_id"] == str(command.causation_id)
        assert any(item.metric == "reminder_outbox_dispatched" for item in measurements)
        assert state.metrics.stats().rejected == 0

    asyncio.run(scenario())


def test_reminder_outbox_persistence_failure_records_error_and_retries() -> None:
    async def scenario() -> None:
        state = AppState(
            Settings(
                public_origin="https://aura.dev.example",
                oidc_issuer="https://authentik.dev.example",
                owner_subject="owner",
                secure_cookies=False,
            ),
            testing=True,
        )
        reminder_id = uuid4()
        command = make_aggregate_command(
            "aura.automation.reminders.v1",
            uuid4(),
            "automation.reminder",
            reminder_id,
            identifiers=(
                ("eventType", "reminder.created"),
                ("status", "scheduled"),
                ("version", 1),
            ),
        )

        class Store:
            def __init__(self) -> None:
                self.pending_calls = 0
                self.mark_calls = 0
                self.retried = asyncio.Event()

            async def pending_commands(self) -> list[Any]:
                self.pending_calls += 1
                return [command] if self.pending_calls <= 2 else []

            async def mark_published(self, command_id: Any) -> None:
                assert command_id == command.id
                self.mark_calls += 1
                if self.mark_calls == 1:
                    raise RuntimeError("postgres unavailable")
                self.retried.set()

        class Nats:
            def __init__(self) -> None:
                self.publish_calls = 0

            async def publish(self, value: Any) -> bool:
                assert value == command
                self.publish_calls += 1
                return False

        store = Store()
        nats = Nats()
        state.sql_store = cast(Any, store)
        state.nats = cast(Any, nats)
        task = asyncio.create_task(cast(Any, state)._dispatch_outbox())
        try:
            await asyncio.wait_for(store.retried.wait(), timeout=2.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        measurements = state.metrics.snapshot()
        persist_spans = [
            item
            for item in measurements
            if item.kind == "span"
            and item.component_id == "aura.automation.reminder_management"
            and dict(item.trace_attributes).get("operation") == "outbox.persist"
        ]
        assert len(persist_spans) >= 2
        error_spans = [
            item for item in persist_spans if dict(item.dimensions).get("outcome") == "error"
        ]
        assert error_spans
        error = error_spans[0]
        assert dict(error.dimensions)["dependency"] == "postgresql"
        assert dict(error.dimensions)["error_class"] == "persistence"
        attributes = dict(error.trace_attributes)
        allowed_trace_fields = {
            "operation",
            "reminder_id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "event_status",
            "event_version",
            "command_id",
            "correlation_id",
            "causation_id",
        }
        assert set(attributes) <= allowed_trace_fields
        assert attributes["reminder_id"] == str(reminder_id)
        assert attributes["aggregate_type"] == "automation.reminder"
        assert attributes["aggregate_id"] == str(reminder_id)
        assert attributes["event_type"] == "reminder.created"
        assert attributes["event_status"] == "scheduled"
        assert attributes["event_version"] == "1"
        assert attributes["command_id"] == str(command.id)
        assert attributes["correlation_id"] == str(command.correlation_id)
        assert attributes["causation_id"] == str(command.causation_id)
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
        assert nats.publish_calls == 2
        assert store.pending_calls >= 2
        assert store.mark_calls == 2
        assert any(
            dict(item.dimensions).get("outcome") == "ok" for item in persist_spans
        )
        assert not any(
            item.kind == "span"
            and dict(item.dimensions).get("dependency") == "nats_jetstream"
            and dict(item.dimensions).get("outcome") == "error"
            for item in measurements
        )
        assert state.metrics.stats().rejected == 0

    asyncio.run(scenario())


def test_nats_publish_failure_keeps_delivery_classification_and_legacy_attribution() -> None:
    async def scenario() -> None:
        state = AppState(
            Settings(
                public_origin="https://aura.dev.example",
                oidc_issuer="https://authentik.dev.example",
                owner_subject="owner",
                secure_cookies=False,
            ),
            testing=True,
        )
        run_id = uuid4()
        conversation_id = uuid4()
        command = OutboxCommand(
            uuid4(),
            "aura.runs.execute.v1",
            run_id,
            conversation_id,
            datetime.now(UTC),
            run_id,
            run_id,
        )

        class Store:
            def __init__(self) -> None:
                self.pending_calls = 0
                self.publish_failed = asyncio.Event()

            async def pending_commands(self) -> list[Any]:
                self.pending_calls += 1
                return [command] if self.pending_calls == 1 else []

            async def mark_published(self, command_id: Any) -> None:
                raise AssertionError(f"mark_published must not run: {command_id}")

        class Nats:
            async def publish(self, value: Any) -> bool:
                assert value == command
                store.publish_failed.set()
                raise RuntimeError("nats unavailable")

        store = Store()
        state.sql_store = cast(Any, store)
        state.nats = cast(Any, Nats())
        task = asyncio.create_task(cast(Any, state)._dispatch_outbox())
        try:
            await asyncio.wait_for(store.publish_failed.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        measurements = state.metrics.snapshot()
        publish_errors = [
            item
            for item in measurements
            if item.kind == "span"
            and item.component_id == "aura.execution.run_coordinator"
            and dict(item.trace_attributes).get("operation") == "outbox.dispatch"
            and dict(item.dimensions).get("outcome") == "error"
        ]
        assert publish_errors
        error = publish_errors[0]
        assert dict(error.dimensions)["dependency"] == "nats_jetstream"
        assert dict(error.dimensions)["error_class"] == "delivery"
        attributes = dict(error.trace_attributes)
        assert attributes["run_id"] == str(run_id)
        assert attributes["conversation_id"] == str(conversation_id)
        assert attributes["command_id"] == str(command.id)
        assert attributes["correlation_id"] == str(command.correlation_id)
        assert attributes["causation_id"] == str(command.causation_id)
        assert not any(
            item.kind == "span"
            and dict(item.trace_attributes).get("operation") == "outbox.persist"
            for item in measurements
        )
        assert state.metrics.stats().rejected == 0

    asyncio.run(scenario())
