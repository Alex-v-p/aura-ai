"""Dedicated Observatory evaluation dispatch/result worker."""

# Optional repository hooks are intentionally narrowed at runtime; the worker
# remains transport-neutral while pyright treats their bounded projections as
# dynamic adapter values.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from typing import Any, cast

from aura_observatory.composition import (
    build_calibration_service,
    build_evaluation_orchestration,
    build_local_judge_service,
)
from aura_observatory.evaluation_calibration import CalibrationService
from aura_observatory.evaluation_judges import JudgeRequest, LocalJudgeService
from aura_observatory.evaluation_orchestration import (
    EvaluationOrchestrationService,
    OrchestrationDependencyError,
)
from aura_observatory.evaluation_transport import (
    NatsEvaluationTransport,
    NatsTransportError,
    connect_nats,
)
from aura_observatory.settings import Settings

_LOGGER = logging.getLogger(__name__)
_FAILURE_CODES = {"timeout", "unavailable", "failed"}
_POSTGRES_FAILURE_CODES = {
    "database_failure",
    "storage_failure",
    "metadata_storage_failure",
    "definition_storage_failure",
    "execution_storage_failure",
    "local_work_lease_lost",
}
_OLLAMA_FAILURE_CODES = {
    "provider_unavailable",
    "provider_timeout",
    "provider_failure",
    "provider_malformed",
    "model_not_found",
    "model_digest_drift",
}
_GARAGE_FAILURE_CODES = {"artifact_storage_failure", "artifact_integrity", "artifact_revoked"}
_CLICKHOUSE_FAILURE_CODES = {"storage_failure"}
_DEPENDENCIES = {"postgresql", "clickhouse", "garage", "ollama", "evaluation_nats"}


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
    service: EvaluationOrchestrationService | None = None
    judge_service: LocalJudgeService | None = None
    calibration_service: CalibrationService | None = None
    try:
        service = build_evaluation_orchestration(
            resolved,
            transport=NatsEvaluationTransport(
                connection, max_bytes=resolved.evaluation_max_envelope_bytes
            ),
        )
        # Compose the worker-only judge boundary in the same process. The
        # orchestration worker remains the durable job authority; no public API
        # or Core request path depends on this optional provider.
        try:
            shared_metrics = getattr(service, "metrics", None)
            judge_service = build_local_judge_service(resolved, metrics=shared_metrics)
            shared_metrics = getattr(judge_service, "metrics", shared_metrics)
            calibration_service = build_calibration_service(resolved, metrics=shared_metrics)
        except Exception:
            # Missing judge credentials/provider state must not prevent normal
            # evaluation dispatch, result consumption, or worker readiness.
            judge_service = None
            calibration_service = None
            metrics = getattr(service, "metrics", None)
            record = getattr(metrics, "record", None)
            if callable(record):
                for metric_name in (
                    "observatory_local_judge_request_count",
                    "observatory_calibration_qualification_count",
                ):
                    try:
                        record(metric_name, labels={"outcome": "unavailable"})
                    except (ValueError, TypeError):
                        pass
            record_span = getattr(metrics, "record_component_span", None)
            if callable(record_span):
                try:
                    record_span(
                        component_id="aura.observatory.local_judges",
                        component_version="1.0.0",
                        deployment_id=str(getattr(metrics, "deployment_id", "observatory")),
                        operation="evaluate",
                        duration_ms=0.0,
                        outcome="unavailable",
                        error_code="provider_unavailable",
                    )
                except (ValueError, TypeError):
                    pass
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
                    # Materialize only persisted calibration definitions; the
                    # repository derives bounded arguments and idempotency.
                    pending = getattr(service, "enqueue_pending_local_work", None)
                    if callable(pending):
                        await cast(Callable[[], Awaitable[int]], pending)()
                    # Consume exactly one bounded durable local-work claim;
                    # repositories without admitted definitions return none.
                    local_claim = getattr(service, "claim_local_work", None)
                    if callable(local_claim):
                        await _dispatch_local_work(service, judge_service, calibration_service)
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
        # Every composed metrics instance owns bounded exporter queues. Flush
        # all of them before releasing the optional services; normal readiness
        # and request processing never wait on this shutdown-only path.
        metrics_instances = [
            getattr(service, "metrics", None),
            getattr(judge_service, "metrics", None),
            getattr(calibration_service, "metrics", None),
        ]
        seen_metrics: set[int] = set()
        for metrics in metrics_instances:
            if metrics is None or id(metrics) in seen_metrics:
                continue
            seen_metrics.add(id(metrics))
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
        del judge_service
        del calibration_service
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


async def _dispatch_local_work(
    service: EvaluationOrchestrationService,
    judge_service: LocalJudgeService | None,
    calibration_service: CalibrationService | None,
) -> int:
    """Run one terminal local-judge/calibration claim through typed ports.

    The orchestration repository owns admission and leases.  It may expose
    these hooks when the evaluation schema contains local work.  A missing
    optional provider is a bounded failed work item, never an abandoned claim.
    """

    metrics = getattr(service, "metrics", None)
    trace_context = getattr(metrics, "trace_context", None)
    context = trace_context() if callable(trace_context) else nullcontext()
    with context:
        work = await service.claim_local_work()
        if work is None:
            return 0
        try:
            if work.kind == "judge":
                if judge_service is None:
                    raise RuntimeError("judge_service_unavailable")
                request = work.request
                if isinstance(request, Mapping):
                    request = JudgeRequest.from_payload(request)
                if not isinstance(request, JudgeRequest):
                    raise RuntimeError("judge_work_invalid")
                result = await judge_service.evaluate(request)
            elif work.kind == "calibration":
                if calibration_service is None:
                    raise RuntimeError("calibration_service_unavailable")
                arguments = work.arguments
                if arguments is None:
                    raise RuntimeError("calibration_work_invalid")
                result = await calibration_service.calibrate(**cast(Mapping[str, Any], arguments))
            else:
                raise RuntimeError("local_work_invalid")
        except Exception as error:
            await service.fail_local_work(work, getattr(error, "code", "storage_failure"))
            raise
        await service.complete_local_work(work, result)
    return 1


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
        declared = getattr(error, "code", None)
        dependency = getattr(error, "dependency", None)
        if dependency not in _DEPENDENCIES and declared in _POSTGRES_FAILURE_CODES:
            dependency = "postgresql"
        elif dependency not in _DEPENDENCIES and declared in _CLICKHOUSE_FAILURE_CODES:
            # A generic score-store failure is explicitly not a PostgreSQL
            # failure.  The LocalJudgeService tags that terminal error with
            # ClickHouse before it reaches the worker.
            dependency = "clickhouse"
        elif dependency not in _DEPENDENCIES and declared in _GARAGE_FAILURE_CODES:
            dependency = "garage"
        elif dependency not in _DEPENDENCIES and declared in _OLLAMA_FAILURE_CODES:
            dependency = "ollama"
        elif (
            dependency not in _DEPENDENCIES
            and error.__class__.__name__ == "CalibrationError"
            and declared
            in {
                "database_failure",
                "storage_failure",
            }
        ):
            dependency = "postgresql"
        if dependency not in _DEPENDENCIES:
            # No safe dependency attribution is available for an untyped
            # terminal failure; do not manufacture a NATS or PostgreSQL
            # signal that would mislead operators.
            return
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
