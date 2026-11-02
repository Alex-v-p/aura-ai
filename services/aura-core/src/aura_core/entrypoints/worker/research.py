"""Identifier-only durable research worker seam."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from aura_core.domains.knowledge.research.public import ResearchCoordinator

RESEARCH_EXECUTE_TOPIC = "aura.knowledge.research.execute.v1"


async def run_research_once(
    coordinator: ResearchCoordinator,
    research_id: UUID,
    *,
    owner: tuple[str, str] | None = None,
) -> object:
    """Execute one admitted research identifier and return its settlement."""

    return await coordinator.run_once(research_id, owner=owner)


async def run_research_loop(
    coordinator: ResearchCoordinator,
    receive: Callable[[], Awaitable[UUID | None]],
) -> None:
    """Keep a transport-owned identifier consumer alive until cancellation."""

    while True:
        research_id = await receive()
        if research_id is not None:
            await run_research_once(coordinator, research_id)
        await asyncio.sleep(0)


async def run_research_consumer_once(coordinator: ResearchCoordinator, consumer: object) -> None:
    """Process one identifier-only command and acknowledge after settlement."""
    consumer_api = cast(Any, consumer)
    receive = cast(Callable[[], Awaitable[Any]], consumer_api.receive)
    command = await receive()
    if command is None:
        return
    research_id = getattr(command, "aggregate_id", None)
    if research_id is None:
        research_id = getattr(command, "run_id", None)
    if research_id is None:
        reject = getattr(consumer, "reject", None)
        if callable(reject):
            await cast(Callable[..., Awaitable[Any]], reject)(error_class="validation")
        raise ValueError("research command has no aggregate identity")
    await coordinator.run_once(research_id)
    acknowledge = getattr(consumer, "ack", None)
    if callable(acknowledge):
        await cast(Callable[[], Awaitable[Any]], acknowledge)()


async def run_research_consumer_loop(coordinator: ResearchCoordinator, consumer: object) -> None:
    while True:
        await run_research_consumer_once(coordinator, consumer)


__all__ = [
    "RESEARCH_EXECUTE_TOPIC",
    "run_research_consumer_loop",
    "run_research_consumer_once",
    "run_research_loop",
    "run_research_once",
]
