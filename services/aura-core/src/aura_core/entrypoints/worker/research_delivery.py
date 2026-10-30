"""Worker composition for research settlement and conversational delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aura_core.domains.knowledge.research.public import ResearchCoordinator


async def run_research_consumer_once(
    coordinator: ResearchCoordinator,
    consumer: object,
    completion_delivery: object,
) -> None:
    """Settle research, deliver its terminal result, then acknowledge the command."""

    consumer_api = cast(Any, consumer)
    receive = cast(Callable[[], Awaitable[Any]], consumer_api.receive)
    command = await receive()
    if command is None:
        return
    research_id = getattr(command, "aggregate_id", None) or getattr(command, "run_id", None)
    if research_id is None:
        reject = getattr(consumer, "reject", None)
        if callable(reject):
            await cast(Callable[..., Awaitable[Any]], reject)(error_class="validation")
        return
    try:
        result = await coordinator.run_once(research_id)
        deliver = getattr(completion_delivery, "deliver", None)
        if callable(deliver) and getattr(result, "terminal", False):
            snapshot: object = result
            snapshot_provider = getattr(completion_delivery, "snapshot_provider", None)
            if callable(snapshot_provider):
                snapshot = await cast(
                    Callable[[object], Awaitable[object]], snapshot_provider
                )(result)
            ensure = getattr(completion_delivery, "ensure_delivery", None)
            if callable(ensure):
                await cast(Callable[[object], Awaitable[Any]], ensure)(snapshot)
            await cast(Callable[[object], Awaitable[Any]], deliver)(snapshot)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never acknowledge an uncertain settlement.  Durable delivery state
        # and JetStream redelivery provide bounded recovery after restart.
        nack = getattr(consumer, "nack", None)
        if callable(nack):
            count = int(getattr(consumer, "delivery_count", 1))
            delay = min(300.0, 2.0 ** min(max(count - 1, 0), 8))
            await cast(Callable[..., Awaitable[Any]], nack)(
                retry_delay=delay, error_class="delivery"
            )
            return
        raise
    acknowledge = getattr(consumer, "ack", None)
    if callable(acknowledge):
        await cast(Callable[[], Awaitable[Any]], acknowledge)()


async def run_research_consumer_loop(
    coordinator: ResearchCoordinator,
    consumer: object,
    completion_delivery: object,
) -> None:
    while True:
        try:
            await run_research_consumer_once(coordinator, consumer, completion_delivery)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A consumer implementation without nack still must not terminate
            # the worker process; the next receive retries the durable command.
            pass
        await asyncio.sleep(0)


__all__ = ["run_research_consumer_loop", "run_research_consumer_once"]
