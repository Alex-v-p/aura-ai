"""Disposable integration seams for recurring reminder delivery."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[4]
CORE = ROOT / "services/aura-core/src/aura_core"
REMINDERS = CORE / "domains/automation/reminders"
NOTIFICATIONS = CORE / "domains/automation/notifications"
SCHEDULER = CORE / "entrypoints/scheduler"


def _source(directory: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in directory.glob("*.py"))


def test_recurring_delivery_uses_shared_transactional_pipeline() -> None:
    source = "\n".join(
        (
            _source(REMINDERS),
            _source(NOTIFICATIONS),
            _source(SCHEDULER),
        )
    ).lower()
    for marker in ("reminder", "occurrence", "notification", "outbox", "transaction"):
        assert marker in source
    assert "deliver_due" in source
    assert "fire_due" in source
    assert "ollama" not in source
    assert "browser" not in source


def test_delivery_contains_bounded_recovery_and_one_future_occurrence_guards() -> None:
    source = "\n".join((_source(REMINDERS), _source(NOTIFICATIONS), _source(SCHEDULER))).upper()
    assert "SKIP LOCKED" in source or "SKIP_LOCKED=TRUE" in source
    assert "LIMIT 100" in source or "LIMIT :LIMIT" in source or "BATCH_SIZE = 100" in source
    assert "MISSED" in source
    assert "OCCURRENCE" in source
    assert "UNIQUE" in source
    assert "ROLLBACK" in source or "SESSION" in source


def test_series_event_payload_is_closed_identifier_metadata_only() -> None:
    contract = ROOT / "contracts/events/automation/v1/reminder-series-events.schema.json"
    assert contract.exists()
    schema = json.loads(contract.read_text(encoding="utf-8"))
    properties = schema.get("$defs", {}).get("EventProperties", {}).get("properties", {})
    assert properties
    forbidden = {
        "content",
        "subject",
        "recurrence",
        "timezone",
        "localTime",
        "dueAt",
        "principalIssuer",
        "principalSubject",
    }
    assert not forbidden & set(properties)
    assert {"aggregateId", "eventType", "version", "commandId"} <= set(properties)


def test_disposable_topology_keeps_scheduler_database_only() -> None:
    compose = "\n".join(
        line
        for line in (ROOT / "compose.yaml").read_text(encoding="utf-8").lower().splitlines()
        if not line.lstrip().startswith("#")
    )
    start = compose.index("aura-core-scheduler")
    end = compose.find("\n  aura-core-migrate:", start + 1)
    section = compose[start : end if end > start else None]
    assert "database" in section
    for forbidden in ("ollama", "nats", "valkey", "oidc"):
        assert forbidden not in section
