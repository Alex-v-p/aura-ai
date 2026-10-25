"""Authenticated metadata observation validation and persistence orchestration."""

from __future__ import annotations

import asyncio
import contextvars
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic, perf_counter
from typing import Any, cast

from pydantic import ValidationError

from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import IngestCounters, IngestHealth, ObservatoryMetrics
from aura_observatory.ports import (
    InsertOutcome,
    NullObservatoryHealthPort,
    ObservationPresenceStore,
    ObservationReconciliationRegistry,
    ObservationStore,
    ObservatoryCatalog,
    ObservatoryHealthPort,
)


class InvalidCredential(PermissionError):
    """The private ingest boundary did not receive the configured credential."""


class IngestPayloadError(ValueError):
    """The request envelope cannot be interpreted as a bounded batch."""


class ReconciliationOutcome(StrEnum):
    COMPLETED = "completed"
    MISSING = "missing"
    RACED = "raced"
    FAILED = "failed"


_RECONCILIATION_PHASE: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "observatory_reconciliation_phase",
    default=("postgresql", "database_failure"),
)


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    """Bounded metadata-only outcome of one recovery pass."""

    completed: int = 0
    missing: int = 0
    raced: int = 0
    failed: int = 0
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "completed": self.completed,
            "missing": self.missing,
            "raced": self.raced,
            "failed": self.failed,
            "duration_ms": self.duration_ms,
        }


class IngestReconciliationService:
    """Recover response-loss windows without receiving observation content."""

    def __init__(
        self,
        registry: ObservationReconciliationRegistry,
        presence_store: ObservationPresenceStore,
        *,
        batch_size: int = 100,
        timeout_seconds: float = 5.0,
        pass_timeout_seconds: float = 15.0,
        health_port: ObservatoryHealthPort | None = None,
        metrics: ObservatoryMetrics | None = None,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("reconciliation batch size is out of bounds")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("reconciliation timeout is out of bounds")
        if not 0 < pass_timeout_seconds <= 60:
            raise ValueError("reconciliation pass timeout is out of bounds")
        self.registry = registry
        self.presence_store = presence_store
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.pass_timeout_seconds = pass_timeout_seconds
        self.health_port = health_port or NullObservatoryHealthPort()
        self.metrics = metrics

    async def reconcile_once(
        self, *, now: datetime | None = None
    ) -> dict[str, int | float]:
        """Run one bounded pass; provider failures never terminate the worker."""

        started = perf_counter()
        trace_context = getattr(self.metrics, "trace_context", None) if self.metrics else None
        context = cast(Any, trace_context)() if callable(trace_context) else nullcontext()
        phase_token = _RECONCILIATION_PHASE.set(("postgresql", "database_failure"))
        try:
            with context:
                try:
                    return await asyncio.wait_for(
                        self._reconcile_once(now=now), timeout=self.pass_timeout_seconds
                    )
                except TimeoutError:
                    duration_ms = max(0.0, (perf_counter() - started) * 1000.0)
                    dependency, _ = _RECONCILIATION_PHASE.get()
                    self._record_outcome(ReconciliationOutcome.FAILED)
                    self._record_component_span(
                        duration_ms=duration_ms,
                        outcome="timeout",
                        dependency=dependency,
                        error_code="timeout",
                    )
                    if self.metrics is not None:
                        self.metrics.record(
                            "observatory_ingest_reconciliation_duration_ms", duration_ms
                        )
                    return ReconciliationSummary(failed=1, duration_ms=duration_ms).as_dict()
        finally:
            _RECONCILIATION_PHASE.reset(phase_token)

    async def _reconcile_once(
        self, *, now: datetime | None = None
    ) -> dict[str, int | float]:
        """Perform the pass body inside the outer whole-pass deadline."""

        started = perf_counter()
        completed = missing = raced = failed = 0
        _RECONCILIATION_PHASE.set(("postgresql", "database_failure"))
        try:
            receipts = await self.registry.stale_pending_receipts(
                limit=self.batch_size, now=now
            )
        except Exception:
            failed = 1
            receipts = []
            self._record_component_span(
                duration_ms=max(0.0, (perf_counter() - started) * 1000.0),
                outcome="failure",
                dependency="postgresql",
                error_code="database_failure",
            )
            self._record_outcome(ReconciliationOutcome.FAILED, failed)
            return await self._finish(started, completed, missing, raced, failed)

        self._record_component_span(
            duration_ms=max(0.0, (perf_counter() - started) * 1000.0),
            outcome="success",
            dependency="postgresql",
        )

        if receipts:
            _RECONCILIATION_PHASE.set(("clickhouse", "transport_unavailable"))
            probe_started = perf_counter()
            try:
                present = await self.presence_store.existing_observations(
                    receipts, timeout_seconds=self.timeout_seconds
                )
            except Exception:
                failed = len(receipts)
                self._record_component_span(
                    duration_ms=max(0.0, (perf_counter() - probe_started) * 1000.0),
                    outcome="failure",
                    dependency="clickhouse",
                    error_code="transport_unavailable",
                    component_id="aura.observatory.telemetry_storage",
                )
                self._record_outcome(ReconciliationOutcome.FAILED, failed)
                return await self._finish(started, completed, missing, raced, failed)
            self._record_component_span(
                duration_ms=max(0.0, (perf_counter() - probe_started) * 1000.0),
                outcome="complete" if present else "no_data",
                dependency="clickhouse",
                component_id="aura.observatory.telemetry_storage",
            )
            commit_started = perf_counter()
            _RECONCILIATION_PHASE.set(("postgresql", "database_failure"))
            for receipt in receipts:
                if receipt.observation_id not in present:
                    missing += 1
                    self._record_outcome(ReconciliationOutcome.MISSING)
                    continue
                try:
                    recovered = await self.registry.reconcile_visible(
                        receipt.observation_id, receipt.ingest_sequence
                    )
                except Exception:
                    failed += 1
                    self._record_outcome(ReconciliationOutcome.FAILED)
                    continue
                if recovered:
                    completed += 1
                    self._record_outcome(ReconciliationOutcome.COMPLETED)
                else:
                    raced += 1
                    self._record_outcome(ReconciliationOutcome.RACED)
            self._record_component_span(
                duration_ms=max(0.0, (perf_counter() - commit_started) * 1000.0),
                outcome="failure"
                if failed
                else ("partial" if raced or missing else "complete"),
                dependency="postgresql",
                error_code="database_failure" if failed else None,
                component_id="aura.observatory.telemetry_storage",
            )
        continuation = getattr(self.registry, "advance_visibility", None)
        if callable(continuation):
            continuation_started = perf_counter()
            _RECONCILIATION_PHASE.set(("postgresql", "database_failure"))
            try:
                more = bool(await cast(Any, continuation)())
            except Exception:
                failed += 1
                self._record_outcome(ReconciliationOutcome.FAILED)
                self._record_component_span(
                    duration_ms=max(0.0, (perf_counter() - continuation_started) * 1000.0),
                    outcome="failure",
                    dependency="postgresql",
                    error_code="database_failure",
                    component_id="aura.observatory.telemetry_storage",
                )
            else:
                self._record_component_span(
                    duration_ms=max(0.0, (perf_counter() - continuation_started) * 1000.0),
                    outcome="partial" if more else "complete",
                    dependency="postgresql",
                    component_id="aura.observatory.telemetry_storage",
                )
        _RECONCILIATION_PHASE.set(("postgresql", "database_failure"))
        return await self._finish(started, completed, missing, raced, failed)

    async def reconcile(self, *, now: datetime | None = None) -> dict[str, int | float]:
        """Compatibility alias for worker and deterministic acceptance seams."""

        return await self.reconcile_once(now=now)

    def _record_outcome(self, outcome: ReconciliationOutcome, amount: int = 1) -> None:
        if self.metrics is None or amount <= 0:
            return
        self.metrics.record(
            "observatory_ingest_reconciliation_count",
            amount,
            labels={"outcome": outcome.value},
        )

    def _record_component_span(
        self,
        *,
        duration_ms: float,
        outcome: str,
        dependency: str | None = None,
        error_code: str | None = None,
        component_id: str = "aura.observatory.telemetry_ingestion",
    ) -> None:
        """Emit only finite causal recovery metadata; never capture payloads."""

        if self.metrics is None:
            return
        record_span = getattr(self.metrics, "record_component_span", None)
        if not callable(record_span):
            return
        try:
            record_span(
                component_id=component_id,
                component_version="1.0.0",
                deployment_id=self.metrics.deployment_id,
                operation="recover",
                duration_ms=duration_ms,
                outcome=outcome,
                dependency=dependency,
                error_code=error_code,
            )
        except (TypeError, ValueError, AttributeError):
            return

    async def _finish(
        self, started: float, completed: int, missing: int, raced: int, failed: int
    ) -> dict[str, int | float]:
        duration_ms = max(0.0, (perf_counter() - started) * 1000.0)
        if self.metrics is not None:
            self.metrics.record("observatory_ingest_reconciliation_duration_ms", duration_ms)
        try:
            snapshot = await self.registry.visibility_snapshot()
        except Exception:
            self._record_component_span(
                duration_ms=max(0.0, (perf_counter() - started) * 1000.0),
                outcome="failure",
                dependency="postgresql",
                error_code="database_failure",
                component_id="aura.observatory.telemetry_storage",
            )
            snapshot: Mapping[str, int | float | None] = {}
        if snapshot:
            watermark = _bounded_int(snapshot.get("watermark"))
            lag_count = _bounded_int(snapshot.get("visibility_lag_count"))
            backlog_count = _bounded_int(snapshot.get("backlog_count"))
            oldest_age = snapshot.get("oldest_backlog_age_ms")
            oldest_value = float(oldest_age) if isinstance(oldest_age, (int, float)) else None
            await _safe_health_call(
                self.health_port.record_visibility(
                    watermark, lag_count, backlog_count, oldest_value
                )
            )
            if self.metrics is not None:
                set_metric = getattr(self.metrics, "set", None)
                if callable(set_metric):
                    set_metric("observatory_ingest_visibility_lag_count", lag_count)
                    set_metric("observatory_ingest_backlog_count", backlog_count)
        return ReconciliationSummary(
            completed=completed,
            missing=missing,
            raced=raced,
            failed=failed,
            duration_ms=duration_ms,
        ).as_dict()


def _bounded_int(value: object) -> int:
    if not isinstance(value, (int, float)):
        return 0
    return max(0, min(2_147_483_647, int(value)))


class TelemetryIngestService:
    """Use case for one authenticated batch from the Collector."""

    def __init__(
        self,
        observation_store: ObservationStore,
        catalog: ObservatoryCatalog,
        *,
        credential: str | None,
        max_batch_size: int = 500,
        counters: IngestCounters | None = None,
        health: IngestHealth | None = None,
        health_port: ObservatoryHealthPort | None = None,
        metrics: ObservatoryMetrics | None = None,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")
        self.observation_store = observation_store
        self.catalog = catalog
        self.credential = credential
        self.max_batch_size = max_batch_size
        self.counters = counters or IngestCounters()
        self.health = health or IngestHealth()
        self.health_port = health_port or NullObservatoryHealthPort()
        self._health_persists_counters = health_port is not None
        self.metrics = metrics

    def authenticate(self, presented: str | None) -> None:
        expected = self.credential
        if expected is None or presented is None or not secrets.compare_digest(presented, expected):
            raise InvalidCredential("invalid service credential")

    async def record_malformed(
        self,
        *,
        credential: str | None,
        started: float | None = None,
        reason: str = "malformed",
    ) -> float:
        """Count malformed input without retaining or exposing its raw bytes."""

        self.authenticate(credential)
        batch_started = started if started is not None else monotonic()
        self.counters.malformed += 1
        self.health.rejection(reason)
        await _safe_health_call(self.health_port.record_rejection(reason))
        catalog_failed = False
        if not self._health_persists_counters:
            try:
                await self.catalog.increment_counter("malformed")
            except Exception:
                catalog_failed = True
        duration_ms = self.counters.record_duration(batch_started)
        try:
            await self.catalog.increment_counter("duration_ms", int(duration_ms))
        except Exception:
            catalog_failed = True
        if catalog_failed:
            self.counters.storage_failure += 1
            self.health.storage_failure()
            if self._health_persists_counters:
                await _safe_health_call(self.health_port.record_storage_failure())
            else:
                try:
                    await self.catalog.increment_counter("storage_failure")
                except Exception:
                    pass
        return duration_ms

    async def ingest(self, payload: Any, *, credential: str | None) -> dict[str, int | float]:
        trace_context = getattr(self.metrics, "trace_context", None) if self.metrics else None
        if callable(trace_context):
            with cast(Any, trace_context)():
                return await self._ingest(payload, credential=credential)
        return await self._ingest(payload, credential=credential)

    async def _ingest(self, payload: Any, *, credential: str | None) -> dict[str, int | float]:
        started = monotonic()
        self.authenticate(credential)
        try:
            records = _extract_records(payload, max_batch_size=self.max_batch_size)
        except IngestPayloadError:
            await self.record_malformed(credential=credential, started=started)
            raise

        accepted = rejected = duplicates = malformed = storage_failures = 0
        rejection_reasons: dict[str, int] = {}
        observations: list[MetadataObservation] = []
        for record in records:
            try:
                observation = MetadataObservation.from_payload(record)
            except ValidationError:
                rejected += 1
                rejection_reasons["schema_validation"] = (
                    rejection_reasons.get("schema_validation", 0) + 1
                )
                continue
            except (TypeError, ValueError):
                malformed += 1
                rejection_reasons["malformed"] = rejection_reasons.get("malformed", 0) + 1
                continue
            observations.append(observation)

        # Parse the complete bounded request before any catalogue, receipt, or
        # provider write.  Repeated identifiers are classified once here so a
        # batch claim cannot accidentally allocate two sequences for one item.
        unique_observations: list[MetadataObservation] = []
        seen_ids: set[str] = set()
        for observation in observations:
            if observation.observation_id in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(observation.observation_id)
            unique_observations.append(observation)

        for reason, amount in rejection_reasons.items():
            self.health.rejection(reason, amount)
            await _safe_health_call(self.health_port.record_rejection(reason, amount))

        storage_observations: list[MetadataObservation] = []
        # Catalogue writes remain idempotent and are deliberately completed
        # before receipt admission.  They are bounded by the already bounded
        # request and never receive raw payload content.
        batch_catalog = getattr(self.catalog, "record_ingest_batch", None)
        if callable(batch_catalog):
            components = sorted(
                {(item.component.id, item.component.version) for item in unique_observations}
            )
            deployment_roles: dict[tuple[str, str, str, str], set[str]] = {}
            for item in unique_observations:
                deployment = item.deployment
                key = (
                    deployment.deployment_id,
                    deployment.git_commit,
                    deployment.environment,
                    deployment.host_id,
                )
                deployment_roles.setdefault(key, set()).add(item.service_role)
            deployments = [
                (*key, tuple(sorted(roles))) for key, roles in sorted(deployment_roles.items())
            ]
            catalog_started = perf_counter()
            try:
                await cast(Any, batch_catalog)(components, deployments)
            except Exception:
                storage_failures += len(unique_observations)
            else:
                storage_observations = unique_observations
            self._record_ingest_span(
                duration_ms=(perf_counter() - catalog_started) * 1000.0,
                outcome="failure" if storage_failures else "complete",
                dependency="postgresql",
                error_code="database_failure" if storage_failures else None,
            )
        else:
            for observation in unique_observations:
                try:
                    await self.catalog.record_component(
                        observation.component.id, observation.component.version
                    )
                except Exception:
                    storage_failures += 1
                    continue
                try:
                    await self.catalog.record_deployment(
                        observation.deployment.deployment_id,
                        observation.deployment.git_commit,
                        observation.deployment.environment,
                        observation.deployment.host_id,
                    )
                    record_role = getattr(self.catalog, "record_deployment_role", None)
                    if record_role is not None:
                        await record_role(
                            observation.deployment.deployment_id,
                            observation.deployment.git_commit,
                            observation.deployment.environment,
                            observation.deployment.host_id,
                            observation.service_role,
                        )
                except Exception:
                    storage_failures += 1
                    continue
                storage_observations.append(observation)

        storage_started = perf_counter()
        batch_insert = getattr(self.observation_store, "insert_observations", None)
        outcomes: Mapping[str, InsertOutcome] = {}
        if storage_observations:
            try:
                if callable(batch_insert):
                    outcomes = cast(
                        Mapping[str, InsertOutcome],
                        await cast(
                            Callable[[list[MetadataObservation]], Awaitable[object]], batch_insert
                        )(storage_observations),
                    )
                else:
                    outcomes = {
                        observation.observation_id: await self.observation_store.insert_observation(
                            observation
                        )
                        for observation in storage_observations
                    }
            except Exception:
                outcomes = {
                    observation.observation_id: InsertOutcome.RETRYABLE
                    for observation in storage_observations
                }
        storage_duration = (perf_counter() - storage_started) * 1000.0
        if storage_observations:
            self.health.storage_latency.add(storage_duration)
            if self.metrics is not None:
                self.metrics.record("observatory_ingest_storage_duration_ms", storage_duration)
            await _safe_health_call(self.health_port.record_latency("storage", storage_duration))
        accepted_times: list[datetime] = []
        for observation in storage_observations:
            outcome = outcomes.get(observation.observation_id, InsertOutcome.RETRYABLE)
            if outcome is InsertOutcome.DUPLICATE:
                duplicates += 1
            elif outcome is InsertOutcome.RETRYABLE:
                storage_failures += 1
            else:
                accepted += 1
                accepted_times.append(observation.observed_at.astimezone(UTC))

        if accepted_times:
            accepted_at = max(accepted_times)
            self.health.accepted(accepted_at)
            await _safe_health_call(
                self.health_port.record_accepted_observation(accepted_at, len(accepted_times))
            )
        if duplicates:
            for _ in range(duplicates):
                self.health.duplicate()
            await _safe_health_call(self.health_port.record_duplicate(duplicates))
        if storage_failures:
            self.health.storage_failure(storage_failures)
            await _safe_health_call(self.health_port.record_storage_failure(storage_failures))

        catalog_failures = 0
        for name, amount in (
            ("accepted", 0 if self._health_persists_counters else accepted),
            ("rejected", 0 if self._health_persists_counters else rejected),
            ("duplicate", 0 if self._health_persists_counters else duplicates),
            ("malformed", 0 if self._health_persists_counters else malformed),
            (
                "storage_failure",
                0 if self._health_persists_counters else storage_failures,
            ),
        ):
            if amount:
                try:
                    await self.catalog.increment_counter(name, amount)
                except Exception:
                    catalog_failures += 1
        duration_ms = self.counters.record_duration(started)
        try:
            await self.catalog.increment_counter("duration_ms", int(duration_ms))
        except Exception:
            catalog_failures += 1
        storage_failures += catalog_failures
        if catalog_failures:
            self.health.storage_failure(catalog_failures)
            await _safe_health_call(self.health_port.record_storage_failure(catalog_failures))
        self.counters.accepted += accepted
        self.counters.rejected += rejected
        self.counters.duplicate += duplicates
        self.counters.malformed += malformed
        self.counters.storage_failure += storage_failures
        if catalog_failures:
            if not self._health_persists_counters:
                try:
                    await self.catalog.increment_counter("storage_failure", catalog_failures)
                except Exception:
                    pass
        return {
            "accepted": accepted,
            "rejected": rejected,
            "duplicate": duplicates,
            "malformed": malformed,
            "storage_failure": storage_failures,
            "duration_ms": duration_ms,
        }

    async def record_worker_heartbeat(self, at: datetime | None = None) -> None:
        heartbeat = (at or datetime.now(UTC)).astimezone(UTC)
        self.health.heartbeat(heartbeat)
        await _safe_health_call(self.health_port.record_worker_heartbeat(heartbeat))

    async def health_snapshot(self) -> dict[str, object]:
        # The ingest use case owns the validated observation high-water mark.
        # Prefer that in-process view after this worker has accepted data so a
        # catalog adapter that has not yet projected the timestamp cannot
        # report a misleading null intake lag.  Durable adapters still supply
        # the snapshot for a newly started worker with no local observations.
        local = self.health.snapshot().as_dict()
        if (
            self.health.last_observed_at is not None
            or self.health.last_accepted_at is not None
            or self.health.rejection_reasons
            or self.health.duplicate_count
        ):
            return local
        persisted = await _safe_health_result(self.health_port.health_snapshot())
        if isinstance(persisted, dict) and persisted:
            return cast(dict[str, object], persisted)
        return local

    def _record_ingest_span(
        self,
        *,
        duration_ms: float,
        outcome: str,
        dependency: str,
        error_code: str | None = None,
    ) -> None:
        if self.metrics is None:
            return
        record_span = getattr(self.metrics, "record_component_span", None)
        if not callable(record_span):
            return
        try:
            record_span(
                component_id="aura.observatory.telemetry_ingestion",
                component_version="1.0.0",
                deployment_id=self.metrics.deployment_id,
                operation="write",
                duration_ms=duration_ms,
                outcome=outcome,
                dependency=dependency,
                error_code=error_code,
            )
        except (AttributeError, TypeError, ValueError):
            return


async def _safe_health_call(operation: Awaitable[object]) -> None:
    try:
        await operation
    except Exception:
        # Health diagnostics must never turn a successful telemetry write into
        # a Core-visible failure or leak provider exception text.
        return


async def _safe_health_result(operation: Awaitable[object]) -> object | None:
    try:
        return await operation
    except Exception:
        return None


def _extract_records(payload: Any, *, max_batch_size: int = 5000) -> list[Mapping[str, Any]]:
    if isinstance(payload, dict):
        envelope = cast(dict[str, Any], payload)
        records_value = envelope.get("observations")
    else:
        records_value = payload
    if not isinstance(records_value, list):
        raise IngestPayloadError("observations must be a JSON array")
    records = cast(list[object], records_value)
    if not records or len(records) > max_batch_size:
        raise IngestPayloadError("observation batch is empty or too large")
    if any(not isinstance(record, Mapping) for record in records):
        raise IngestPayloadError("each observation must be a JSON object")
    return [cast(Mapping[str, Any], record) for record in records]


__all__ = [
    "IngestPayloadError",
    "InvalidCredential",
    "IngestReconciliationService",
    "ReconciliationOutcome",
    "ReconciliationSummary",
    "TelemetryIngestService",
]
