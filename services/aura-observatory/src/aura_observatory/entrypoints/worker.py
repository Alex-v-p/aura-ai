"""Thin ingest-worker composition entrypoint.

The Collector delivers normalized batches to the API in this first slice.  A
worker process is still provided as a stable deployment seam for durable queue
consumption in the next work item; it performs no Core imports or table reads.
"""
# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast

from aura_observatory.composition import (
    build_evaluation_deletion,
    build_ingest_runtime,
    build_query_service,
    build_operations_services,
    build_run_evidence_services,
)
from aura_observatory.evaluation_storage import EvaluationDeletionService
from aura_observatory.ingest import IngestReconciliationService, TelemetryIngestService
from aura_observatory.settings import Settings
from aura_observatory.run_evidence_transport import (
    NatsRunEvidenceTransport,
    connect_run_evidence_nats,
)


class IngestWorker:
    """Consume normalized batches from a process-local handoff queue.

    The Collector transport is composed in the platform work item; this
    queue keeps the worker entrypoint real and testable without inventing a
    second persistence or Core dependency.
    """

    def __init__(
        self,
        service: TelemetryIngestService,
        reconciler: IngestReconciliationService | None = None,
        *,
        reconciliation_interval_seconds: float = 15.0,
    ) -> None:
        if reconciliation_interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self.service = service
        self.reconciler = reconciler
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self._queue: asyncio.Queue[tuple[Any, str | None]] = asyncio.Queue()

    async def submit(self, payload: Any, credential: str | None) -> None:
        await self._queue.put((payload, credential))

    async def run(self, stop: Awaitable[None] | None = None) -> None:
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(heartbeat_stop))
        reconciliation_stop = asyncio.Event()
        reconciliation_task = (
            asyncio.create_task(self._reconciliation_loop(reconciliation_stop))
            if self.reconciler is not None
            else None
        )
        if stop is None:
            try:
                while True:
                    payload, credential = await self._queue.get()
                    await self.service.ingest(payload, credential=credential)
            finally:
                heartbeat_stop.set()
                heartbeat_task.cancel()
                if reconciliation_task is not None:
                    reconciliation_stop.set()
                    reconciliation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reconciliation_task
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                await _flush_metrics(self.service)
        else:
            stop_task = asyncio.create_task(_await_stop(stop))
            try:
                while True:
                    item_task = asyncio.create_task(self._queue.get())
                    done, _ = await asyncio.wait(
                        (item_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_task in done:
                        item_task.cancel()
                        return
                    payload, credential = item_task.result()
                    await self.service.ingest(payload, credential=credential)
            finally:
                stop_task.cancel()
                heartbeat_stop.set()
                heartbeat_task.cancel()
                if reconciliation_task is not None:
                    reconciliation_stop.set()
                    reconciliation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reconciliation_task
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                await _flush_metrics(self.service)

    async def _run_reconciliation_once(self) -> None:
        if self.reconciler is None:
            return
        try:
            await self.reconciler.reconcile_once()
        except Exception:
            # Recovery is advisory to normal ingestion.  The reconciler
            # already records bounded provider failures; this guard protects
            # the worker from an unexpected adapter implementation failure.
            return

    async def _reconciliation_loop(self, stop: asyncio.Event) -> None:
        # Startup recovery is scheduled alongside heartbeat and queue intake;
        # a slow dependency must never delay normal ingestion or shutdown.
        await self._run_reconciliation_once()
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.reconciliation_interval_seconds
                )
            except TimeoutError:
                await self._run_reconciliation_once()

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.service.record_worker_heartbeat()
            try:
                await asyncio.wait_for(stop.wait(), timeout=15.0)
            except TimeoutError:
                continue


async def _await_stop(stop: Awaitable[None]) -> None:
    await stop


async def run(settings: Settings | None = None) -> None:
    """Run the production worker with configured Observatory adapters."""

    resolved = settings or Settings()
    query = build_query_service(resolved)
    service, reconciler = build_ingest_runtime(resolved)
    worker = IngestWorker(
        service,
        reconciler,
        reconciliation_interval_seconds=resolved.ingest_reconciliation_interval_seconds,
    )
    deletion = build_evaluation_deletion(resolved)
    deletion_stop = asyncio.Event()
    retention_stop = asyncio.Event()
    run_evidence_stop = asyncio.Event()
    retention_task = asyncio.create_task(query.run_retention_worker(retention_stop))
    deletion_task = asyncio.create_task(_run_deletion_worker(deletion, resolved, deletion_stop))
    evidence_ingest, _ = build_run_evidence_services(resolved)
    evidence_repository = getattr(evidence_ingest, "repository", None)
    evidence_recovery_stop = asyncio.Event()
    evidence_recovery_task = asyncio.create_task(
        _run_run_evidence_recovery_worker(evidence_repository, resolved, evidence_recovery_stop)
    )
    evidence_task = asyncio.create_task(
        _run_run_evidence_worker(evidence_ingest, resolved, run_evidence_stop)
    )
    try:
        await worker.run()
    finally:
        retention_stop.set()
        deletion_stop.set()
        run_evidence_stop.set()
        evidence_recovery_stop.set()
        evidence_task.cancel()
        evidence_recovery_task.cancel()
        await retention_task
        await deletion_task
        with suppress(asyncio.CancelledError):
            await evidence_task
        with suppress(asyncio.CancelledError):
            await evidence_recovery_task
        await _flush_metrics(worker.service, deletion)


async def _run_run_evidence_worker(
    service: Any, settings: Settings, stop: asyncio.Event
) -> None:
    """Consume governed evidence independently from normal telemetry intake.

    Connection and storage failures deliberately keep the worker alive.  A
    message is ACKed only after validation/terminal rejection or durable
    persistence; provider failures escape the transport and are redelivered.
    """

    while not stop.is_set():
        try:
            user, password = settings.run_evidence_nats_credential()
            connection = await connect_run_evidence_nats(
                settings.run_evidence_nats_url, user=user, password=password
            )
            transport = NatsRunEvidenceTransport(
                connection,
                durable=settings.run_evidence_nats_durable,
                max_bytes=settings.run_evidence_max_envelope_bytes,
            )
            await transport.consume(service, stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.run_evidence_ingest_poll_seconds
                )
            except TimeoutError:
                continue
        finally:
            close = getattr(locals().get("connection"), "close", None)
            if callable(close):
                with suppress(Exception):
                    await close()


async def _run_run_evidence_recovery_worker(
    repository: Any, settings: Settings, stop: asyncio.Event
) -> None:
    """Resume committed evidence compensation and deletion intents."""

    recover_orphans = getattr(repository, "recover_orphans", None)
    recover_deletions = getattr(repository, "recover_deletions", None)
    if not callable(recover_orphans) and not callable(recover_deletions):
        return
    while not stop.is_set():
        try:
            if callable(recover_orphans):
                await recover_orphans(limit=100)
            if callable(recover_deletions):
                await recover_deletions(limit=100)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.run_evidence_ingest_poll_seconds)
        except TimeoutError:
            continue


async def _run_deletion_worker(
    service: EvaluationDeletionService, settings: Settings, stop: asyncio.Event
) -> None:
    """Resume queued/running deletion commands without retaining payloads."""

    while not stop.is_set():
        try:
            await service.resume_pending(page_size=settings.evaluation_deletion_page_size)
        except Exception as exc:
            # Keep the worker alive for unrelated queued commands, but expose
            # the bounded failure through the service metric/log boundary.
            failure_code = getattr(exc, "failure_code", "storage_failure")
            if not isinstance(failure_code, str):
                failure_code = "storage_failure"
            service.record_worker_failure(failure_code)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.evaluation_deletion_poll_seconds)
        except TimeoutError:
            continue


async def _run_operations_worker(service: Any, settings: Settings, stop: asyncio.Event) -> None:  # pyright: ignore[reportUnusedFunction]
    """Compatibility helper for isolated tests; production scheduling lives in evaluation_scheduler."""
    while not stop.is_set():
        try:
            await service.evaluate_once(limit=16)
        except Exception:
            # Advisory evaluation failures must not stop the owning process.
            pass
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=min(3600.0, max(5.0, settings.evaluation_poll_seconds))
            )
        except TimeoutError:
            continue


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


async def _flush_metrics(*services: object) -> None:
    """Drain bounded metric persistence for services owned by this worker."""

    for service in services:
        metrics = getattr(service, "metrics", None)
        flush = getattr(metrics, "flush", None)
        if not callable(flush):
            continue
        try:
            flush_metrics = cast(Callable[..., Awaitable[None]], flush)
            await flush_metrics(timeout_seconds=5.0)
        except Exception:
            # Metric shutdown failures must not hide the worker's primary
            # shutdown path; ObservatoryMetrics already classifies task
            # failures without retaining provider exception text.
            continue


if __name__ == "__main__":
    main()


__all__ = ["main", "run"]
