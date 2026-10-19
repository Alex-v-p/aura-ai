"""DB-only Observatory evaluation schedule/recovery loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aura_observatory.composition import build_evaluation_scheduler
from aura_observatory.evaluation_orchestration import OrchestrationDependencyError
from aura_observatory.settings import Settings

_LOGGER = logging.getLogger(__name__)


async def run(settings: Settings | None = None, stop: Awaitable[None] | None = None) -> None:
    resolved = settings or Settings(role="evaluation_scheduler")
    scheduler = build_evaluation_scheduler(resolved)
    stop_event = asyncio.Event()
    stop_task = asyncio.create_task(_wait_stop(stop, stop_event)) if stop is not None else None
    try:
        while not stop_event.is_set():
            try:
                await scheduler.heartbeat_once()
                await scheduler.run_once()
            except Exception as error:
                code = _failure_code(error)
                _LOGGER.warning("evaluation scheduler operation failed", extra={"code": code})
                metrics = getattr(scheduler, "metrics", None)
                record = getattr(metrics, "record", None)
                # heartbeat_once records its PostgreSQL dependency outcome
                # before raising this bounded error.  Do not count it again at
                # the process boundary.  Raw run_once provider failures are
                # still recorded here because the service has not classified
                # them yet.
                if callable(record) and not isinstance(error, OrchestrationDependencyError):
                    try:
                        record(
                            "observatory_evaluation_dependency_outcome_count",
                            1.0,
                            {"operation": "postgresql", "outcome": "failed"},
                        )
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=resolved.evaluation_poll_seconds)
            except TimeoutError:
                continue
    finally:
        metrics = getattr(scheduler, "metrics", None)
        flush = getattr(metrics, "flush", None)
        if callable(flush):
            try:
                flush_async = cast(Callable[..., Awaitable[Any]], flush)
                await asyncio.wait_for(
                    flush_async(timeout_seconds=resolved.evaluation_shutdown_timeout_seconds),
                    timeout=resolved.evaluation_shutdown_timeout_seconds,
                )
            except TimeoutError, asyncio.CancelledError:
                pass
        if stop_task is not None:
            stop_task.cancel()


async def _wait_stop(stop: Awaitable[None], event: asyncio.Event) -> None:
    await stop
    event.set()


def _failure_code(error: object) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "unavailable"
    return "failed"


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


__all__ = ["main", "run"]


if __name__ == "__main__":
    main()
