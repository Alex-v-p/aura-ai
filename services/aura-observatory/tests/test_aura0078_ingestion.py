"""AURA-0078 ingestion failure, retry, deduplication, and counter tests."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pytest
from aura_observatory.contract import MetadataObservation
from aura_observatory.ingest import IngestPayloadError, InvalidCredential, TelemetryIngestService
from aura_observatory.metrics import IngestCounters
from aura_observatory.ports import InsertOutcome


def _payload(observation_id: str = "01234567-89ab-4cde-8123-0123456789ab") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "observationId": observation_id,
        "observedAt": datetime.now(UTC).isoformat(),
        "kind": "metric",
        "component": {"id": "aura.runtime.model_inference", "version": "1.0.0"},
        "operation": "inference",
        "measurement": {"name": "duration_ms", "value": 12.5, "unit": "milliseconds"},
        "dimensions": {"outcome": "success"},
        "trace": {
            "traceId": "abcdefabcdefabcdefabcdefabcdefab",
            "spanId": "abcdefabcdefabcd",
        },
        "correlation": {"runId": "fedcba98-7654-4fed-8123-fedcba987654"},
        "deployment": {
            "environment": "test",
            "deploymentId": "deployment-test",
            "gitCommit": "0123456789abcdef01234567",
            "hostId": "host-test",
        },
        "serviceRole": "worker",
        "capturePolicy": "metadata_only",
    }


class MemoryObservationStore:
    def __init__(self, *, failing_ids: set[str] | None = None) -> None:
        self.ids: set[str] = set()
        self.observations: list[MetadataObservation] = []
        self.failing_ids = failing_ids or set()

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        if observation.observation_id in self.failing_ids:
            raise RuntimeError("storage unavailable")
        if observation.observation_id in self.ids:
            return InsertOutcome.DUPLICATE
        self.ids.add(observation.observation_id)
        self.observations.append(observation)
        return InsertOutcome.ACCEPTED


class MemoryCatalog:
    def __init__(self) -> None:
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.components: set[tuple[str, str]] = set()
        self.deployments: set[tuple[str, str, str, str]] = set()

    async def record_component(self, component_id: str, version: str) -> None:
        self.components.add((component_id, version))

    async def record_deployment(
        self, deployment_id: str, git_commit: str, environment: str, host_id: str
    ) -> None:
        self.deployments.add((deployment_id, git_commit, environment, host_id))

    async def increment_counter(self, name: str, amount: int = 1) -> None:
        self.counts[name] += amount

    async def counters(self) -> dict[str, int]:
        return dict(self.counts)

    async def last_ingest_at(self) -> datetime | None:
        return None


def _service(
    store: MemoryObservationStore | None = None,
    catalog: MemoryCatalog | None = None,
    *,
    counters: IngestCounters | None = None,
) -> tuple[TelemetryIngestService, MemoryObservationStore, MemoryCatalog]:
    observation_store = store or MemoryObservationStore()
    observatory_catalog = catalog or MemoryCatalog()
    return (
        TelemetryIngestService(
            observation_store,
            observatory_catalog,
            credential="collector-secret",
            counters=counters,
        ),
        observation_store,
        observatory_catalog,
    )


@pytest.mark.asyncio
async def test_ingest_requires_private_service_credential_without_leaking_it() -> None:
    service, store, _ = _service()

    with pytest.raises(InvalidCredential) as error:
        await service.ingest([_payload()], credential="wrong-secret")

    assert "collector-secret" not in str(error.value)
    assert store.observations == []


@pytest.mark.asyncio
async def test_ingest_is_idempotent_for_stable_observation_id() -> None:
    service, store, catalog = _service()
    payload = _payload()

    first = await service.ingest([payload], credential="collector-secret")
    second = await service.ingest([payload], credential="collector-secret")

    assert first["accepted"] == 1
    assert second["duplicate"] == 1
    assert len(store.observations) == 1
    assert service.counters.duplicate == 1
    assert catalog.counts["duplicate"] == 1


@pytest.mark.asyncio
async def test_ingest_separates_rejected_records_from_storage_failures() -> None:
    failing_id = "fedcba98-7654-4fed-8123-fedcba987654"
    store = MemoryObservationStore(failing_ids={failing_id})
    service, _, catalog = _service(store)
    invalid = _payload()
    invalid["capturePolicy"] = "full_content"

    result = await service.ingest([_payload(failing_id), invalid], credential="collector-secret")

    assert result["storage_failure"] == 1
    assert result["rejected"] == 1
    assert result["accepted"] == 0
    assert service.counters.storage_failure == 1
    assert service.counters.rejected == 1
    assert catalog.counts["storage_failure"] == 1
    assert catalog.counts["rejected"] == 1
    assert store.observations == []


@pytest.mark.asyncio
async def test_malformed_batch_is_rejected_before_any_store_write() -> None:
    service, store, catalog = _service()

    with pytest.raises(IngestPayloadError):
        await service.ingest({"observations": {"not": "a-list"}}, credential="collector-secret")

    assert store.observations == []
    assert service.counters.malformed == 1
    assert catalog.counts["malformed"] == 1


@pytest.mark.asyncio
async def test_ingest_updates_required_bounded_counters() -> None:
    service, _, _ = _service()
    invalid = _payload()
    invalid["observationId"] = "bad"

    await service.ingest([_payload(), invalid], credential="collector-secret")

    counters = service.counters.as_dict()
    assert counters["telemetry_ingest_accepted_count"] == 1
    assert counters["telemetry_ingest_rejected_count"] == 1
    assert counters["telemetry_ingest_duplicate_count"] == 0
    assert counters["telemetry_ingest_storage_failure_count"] == 0
    assert counters["telemetry_ingest_duration_ms"] >= 0
