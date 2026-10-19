"""Dedicated Observatory evaluation dispatch/result worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aura_observatory.composition import build_evaluation_orchestration
from aura_observatory.evaluation_orchestration import OrchestrationDependencyError
from aura_observatory.evaluation_transport import (
    NatsEvaluationTransport,
    NatsTransportError,
    connect_nats,
)
from aura_observatory.settings import Settings

_LOGGER = logging.getLogger(__name__)
_FAILURE_CODES = {"timeout", "unavailable", "failed"}


async def run(settings: Settings | None = None, stop: Awaitable[None] | None = None) -> None:
    resolved = settings or Settings(role="evaluation_worker")
    user, password = resolved.evaluation_nats_credential()
    try:
        connection = await connect_nats(resolved.evaluation_nats_url, user=user, password=password)
    except NatsTransportError as error:
        _LOGGER.warning("evaluation NATS connection unavailable", extra={"code": error.code})
        return
    stop_event = asyncio.Event()
    stop_task = asyncio.create_task(_wait_stop(stop, stop_event)) if stop is not None else None
    service: Any | None = None
    try:
        service = build_evaluation_orchestration(
            resolved,
            transport=NatsEvaluationTransport(
                connection, max_bytes=resolved.evaluation_max_envelope_bytes
            ),
        )
        result_consumer = asyncio.create_task(service.consume_results(stop_event))
        try:
            while not stop_event.is_set():
                if result_consumer.done():
                    try:
                        result_consumer.result()
                    except Exception as error:
                        _record_failure(service, error)
                    else:
                        _record_failure(service, RuntimeError("consumer_stopped"))
                    if not stop_event.is_set():
                        result_consumer = asyncio.create_task(service.consume_results(stop_event))
                try:
                    await service.recover_once()
                    await service.dispatch_once()
                    # A cancellation or a transaction recovered after a process
                    # crash may already have an outbox row whose attempt is no longer
                    # claimable; drain those rows independently.
                    await service.publish_outbox_once()
                    heartbeat = getattr(service, "heartbeat_once", None)
                    if callable(heartbeat):
                        typed_heartbeat = cast(Callable[[], Awaitable[Any]], heartbeat)
                        await typed_heartbeat()
                except Exception as error:
                    _record_failure(service, error)
                await _bounded_wait(stop_event, resolved.evaluation_poll_seconds)
        finally:
            stop_event.set()
            if not result_consumer.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(result_consumer),
                        timeout=resolved.evaluation_shutdown_timeout_seconds,
                    )
                except TimeoutError:
                    result_consumer.cancel()
                    await asyncio.gather(result_consumer, return_exceptions=True)
                except asyncio.CancelledError:
                    result_consumer.cancel()
                    await asyncio.gather(result_consumer, return_exceptions=True)
    finally:
        metrics = getattr(service, "metrics", None)
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
            await asyncio.gather(stop_task, return_exceptions=True)
        try:
            await asyncio.wait_for(
                connection.close(), timeout=resolved.evaluation_shutdown_timeout_seconds
            )
        except TimeoutError, asyncio.CancelledError:
            _LOGGER.warning("evaluation NATS close incomplete", extra={"code": "timeout"})
        except Exception:
            _LOGGER.warning("evaluation NATS close failed", extra={"code": "failed"})


async def _wait_stop(stop: Awaitable[None], event: asyncio.Event) -> None:
    await stop
    event.set()


async def _bounded_wait(event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.01, min(60.0, timeout)))
    except TimeoutError:
        return


def _failure_code(error: object) -> str:
    declared = getattr(error, "code", None)
    if declared in {"timeout", "transport_unavailable"}:
        return "timeout" if declared == "timeout" else "unavailable"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "unavailable"
    return "failed"


def _record_failure(service: object, error: object) -> None:
    code = _failure_code(error)
    if code not in _FAILURE_CODES:
        code = "failed"
    _LOGGER.warning("evaluation worker operation failed", extra={"code": code})
    metrics = getattr(service, "metrics", None)
    record = getattr(metrics, "record", None)
    # Application dependency failures have already emitted their durable
    # operation outcome; the loop boundary only classifies raw provider errors.
    if callable(record) and not isinstance(error, OrchestrationDependencyError):
        dependency = (
            "postgresql"
            if isinstance(error, OrchestrationDependencyError)
            and error.dependency == "postgresql"
            else "evaluation_nats"
        )
        try:
            record(
                "observatory_evaluation_dependency_outcome_count",
                1.0,
                {"operation": dependency, "outcome": "failed"},
            )
        except Exception:
            return


def main() -> None:
    try:
        asyncio.run(run())
    except NatsTransportError:
        return
    except KeyboardInterrupt:
        return


__all__ = ["main", "run"]


if __name__ == "__main__":
    main()
