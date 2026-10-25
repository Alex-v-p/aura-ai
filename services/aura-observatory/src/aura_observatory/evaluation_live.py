"""Persisted, metadata-only evaluation live notifications."""

# Event payloads are intentionally transport mappings at this boundary.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

_RESOURCE_TYPES = frozenset(
    {
        "evaluation_dataset",
        "evaluation_case",
        "evaluation_suite",
        "evaluation_evaluator",
        "evaluation_job",
        "evaluation_attempt",
        "evaluation_result",
        "evaluation_artifact",
        "evaluation_review",
        "evaluation_calibration",
        "evaluation_qualification",
        "evaluation_deletion",
        "evaluation_experiment",
        "evaluation_baseline",
        "evaluation_regression",
        "evaluation_attribution",
    }
)
_CHANGES = frozenset(
    {
        "created",
        "updated",
        "state_changed",
        "cancel_requested",
        "qualified",
        "revoked",
        "deletion_requested",
        "deleted",
        "candidate_created",
        "promoted",
        "comparison_completed",
        "attribution_completed",
    }
)


@dataclass(frozen=True, slots=True)
class EvaluationLiveEvent:
    event_id: str
    resource_type: str
    resource_id: str
    change: str
    state: str
    occurred_at: str
    resource_version: str | None = None

    def as_sse(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": self.event_id,
            "eventType": "evaluation.changed",
            "occurredAt": self.occurred_at,
            "resourceType": self.resource_type,
            "resourceId": self.resource_id,
            "change": self.change,
            "state": self.state,
            "capturePolicy": "metadata_only",
        }


@dataclass(slots=True)
class EvaluationLiveEventStore:
    """Bounded in-process reference log; production may replace it with a DB port."""

    events: list[EvaluationLiveEvent] = field(default_factory=list)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    async def publish(
        self, resource_type: str, resource_id: str, change: str, state: str,
        resource_version: str | None = None,
    ) -> EvaluationLiveEvent:
        # Normalize historical worker vocabulary at the application boundary;
        # persisted events use only the versioned closed change set.
        change = {
            "completed": "state_changed",
            "requested": "deletion_requested",
        }.get(change, change)
        if resource_type not in _RESOURCE_TYPES or change not in _CHANGES:
            raise ValueError("evaluation live event classification is invalid")
        if not resource_id or len(resource_id) > 128 or not state or len(state) > 64:
            raise ValueError("evaluation live event metadata is invalid")
        event = EvaluationLiveEvent(
            event_id=str(uuid4()),
            resource_type=resource_type,
            resource_id=resource_id,
            change=change,
            state=state,
            occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            resource_version=resource_version,
        )
        self.events.append(event)
        del self.events[:-10_000]
        self._wake.set()
        self._wake = asyncio.Event()
        return event

    async def after(
        self, last_event_id: str | None, *, limit: int = 1000
    ) -> list[EvaluationLiveEvent]:
        bounded_limit = max(1, min(1000, int(limit)))
        if last_event_id is None:
            return list(self.events[-bounded_limit:])
        for index, event in enumerate(self.events):
            if event.event_id == last_event_id:
                return list(self.events[index + 1 : index + 1 + bounded_limit])
        # Unknown/expired cursors intentionally catch up from the retained
        # beginning rather than making the stream silently omit state changes.
        return list(self.events[:bounded_limit])

    async def wait(self, timeout: float = 15.0) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            return


class EvaluationLiveEventPort(Protocol):
    async def publish(
        self, resource_type: str, resource_id: str, change: str, state: str,
        resource_version: str | None = None,
    ) -> EvaluationLiveEvent: ...

    async def after(
        self, last_event_id: str | None, *, limit: int = 1000
    ) -> Sequence[EvaluationLiveEvent]: ...

    async def wait(self, timeout: float = 15.0) -> None: ...


__all__ = [
    "EvaluationLiveEvent",
    "EvaluationLiveEventPort",
    "EvaluationLiveEventStore",
]
