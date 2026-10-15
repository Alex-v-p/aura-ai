"""Authenticated metadata observation validation and persistence orchestration."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from time import monotonic
from typing import Any, cast

from pydantic import ValidationError

from aura_observatory.contract import MetadataObservation
from aura_observatory.metrics import IngestCounters
from aura_observatory.ports import InsertOutcome, ObservationStore, ObservatoryCatalog


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
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be positive")
        self.observation_store = observation_store
        self.catalog = catalog
        self.credential = credential
        self.max_batch_size = max_batch_size
        self.counters = counters or IngestCounters()

    def authenticate(self, presented: str | None) -> None:
        expected = self.credential
        if expected is None or presented is None or not secrets.compare_digest(presented, expected):
            raise InvalidCredential("invalid service credential")

    async def record_malformed(
        self, *, credential: str | None, started: float | None = None
    ) -> float:
        """Count malformed input without retaining or exposing its raw bytes."""

        self.authenticate(credential)
        batch_started = started if started is not None else monotonic()
        self.counters.malformed += 1
        catalog_failed = False
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
                continue
            except TypeError, ValueError:
                malformed += 1
                continue
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
            try:
                outcome = await self.observation_store.insert_observation(observation)
            except Exception:
                storage_failures += 1
                continue
            if outcome is InsertOutcome.DUPLICATE:
                duplicates += 1
            elif outcome is InsertOutcome.RETRYABLE:
                storage_failures += 1
            else:
                accepted += 1

        catalog_failures = 0
        for name, amount in (
            ("accepted", accepted),
            ("rejected", rejected),
            ("duplicate", duplicates),
            ("malformed", malformed),
            ("storage_failure", storage_failures),
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
        self.counters.accepted += accepted
        self.counters.rejected += rejected
        self.counters.duplicate += duplicates
        self.counters.malformed += malformed
        self.counters.storage_failure += storage_failures
        if catalog_failures:
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
