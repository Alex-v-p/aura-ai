"""Authenticated metadata observation validation and persistence orchestration."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import Any, cast

from pydantic import ValidationError

from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import IngestCounters, IngestHealth, ObservatoryMetrics
from aura_observatory.ports import (
    InsertOutcome,
    NullObservatoryHealthPort,
    ObservationStore,
    ObservatoryCatalog,
    ObservatoryHealthPort,
)


class InvalidCredential(PermissionError):
    """The private ingest boundary did not receive the configured credential."""


class IngestPayloadError(ValueError):
    """The request envelope cannot be interpreted as a bounded batch."""


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
        started = monotonic()
        self.authenticate(credential)
        try:
            records = _extract_records(payload, max_batch_size=self.max_batch_size)
        except IngestPayloadError:
            await self.record_malformed(credential=credential, started=started)
            raise

        accepted = rejected = duplicates = malformed = storage_failures = 0
        for record in records:
            try:
                observation = MetadataObservation.from_payload(record)
            except ValidationError:
                rejected += 1
                self.health.rejection("schema_validation")
                await _safe_health_call(self.health_port.record_rejection("schema_validation"))
                continue
            except (TypeError, ValueError):
                malformed += 1
                self.health.rejection("malformed")
                await _safe_health_call(self.health_port.record_rejection("malformed"))
                continue
            observed_at = observation.observed_at.astimezone(UTC)
            try:
                await self.catalog.record_component(
                    observation.component.id, observation.component.version
                )
            except Exception:
                storage_failures += 1
                self.health.storage_failure()
                await _safe_health_call(self.health_port.record_storage_failure())
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
                self.health.storage_failure()
                await _safe_health_call(self.health_port.record_storage_failure())
                continue
            storage_started = perf_counter()
            try:
                outcome = await self.observation_store.insert_observation(observation)
            except Exception:
                self.health.storage_latency.add((perf_counter() - storage_started) * 1000.0)
                duration = (perf_counter() - storage_started) * 1000.0
                self.health.storage_failure()
                if self.metrics is not None:
                    self.metrics.record("observatory_ingest_storage_duration_ms", duration)
                await _safe_health_call(self.health_port.record_latency("storage", duration))
                await _safe_health_call(self.health_port.record_storage_failure())
                storage_failures += 1
                continue
            storage_duration = (perf_counter() - storage_started) * 1000.0
            self.health.storage_latency.add(storage_duration)
            if self.metrics is not None:
                self.metrics.record("observatory_ingest_storage_duration_ms", storage_duration)
            await _safe_health_call(self.health_port.record_latency("storage", storage_duration))
            if outcome is InsertOutcome.DUPLICATE:
                duplicates += 1
                self.health.duplicate()
                await _safe_health_call(self.health_port.record_duplicate())
            elif outcome is InsertOutcome.RETRYABLE:
                storage_failures += 1
                self.health.storage_failure()
                await _safe_health_call(self.health_port.record_storage_failure())
            else:
                accepted += 1
                self.health.accepted(observed_at)
                await _safe_health_call(self.health_port.record_accepted_observation(observed_at))

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


__all__ = ["IngestPayloadError", "InvalidCredential", "TelemetryIngestService"]
