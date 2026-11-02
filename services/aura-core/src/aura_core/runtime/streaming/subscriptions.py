"""Subscription protocol kept separate from the SSE transport."""

from collections.abc import AsyncIterator
from uuid import UUID

from aura_core.domains.execution.runs.events import RunEvent
from aura_core.runtime.streaming.publisher import EventPublisher


class RunSubscriptions:
    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher

    def events(self, run_id: UUID, after: UUID | None = None) -> AsyncIterator[RunEvent]:
        return self._publisher.stream(run_id, after)
