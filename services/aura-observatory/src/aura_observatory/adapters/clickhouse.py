"""ClickHouse high-cardinality observation adapter.

The HTTP protocol is used deliberately to keep the service dependency-free;
the repository port remains the only interface visible to the ingest use case.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import ClaimOutcome, InsertOutcome, ObservationIdRegistry


class ClickHouseObservationStore:
    def __init__(
        self,
        endpoint: str,
        *,
        database: str = "aura_observatory",
        id_registry: ObservationIdRegistry,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.database = database
        self.id_registry = id_registry

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        claim = await self.id_registry.claim(observation.observation_id)
        if claim is ClaimOutcome.DUPLICATE:
            return InsertOutcome.DUPLICATE
        if claim is ClaimOutcome.PENDING:
            return InsertOutcome.RETRYABLE
        payload = observation.to_payload()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                insert = (
                    "INSERT INTO observations FORMAT JSONEachRow\n"
                    + json.dumps(clickhouse_row(payload), separators=(",", ":"))
                )
                inserted = await client.post(
                    self.endpoint,
                    params={
                        "database": self.database,
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": observation.observation_id,
                    },
                    content=insert,
                )
                inserted.raise_for_status()
            await self.id_registry.complete(observation.observation_id)
        except Exception:
            # Keep the pending lease on transport/response failure.  A retry
            # can safely recover it after expiry using the same ClickHouse
            # deduplication token, avoiding a second visible observation.
            raise
        return InsertOutcome.ACCEPTED


def clickhouse_row(payload: dict[str, Any]) -> dict[str, Any]:
    component = cast(dict[str, Any], payload["component"])
    measurement = cast(dict[str, Any], payload["measurement"])
    trace = cast(dict[str, Any], payload.get("trace") or {})
    correlation = cast(dict[str, Any], payload.get("correlation") or {})
    deployment = cast(dict[str, Any], payload["deployment"])
    return {
        "observation_id": payload["observationId"],
        "observed_at": payload["observedAt"],
        "kind": payload["kind"],
        "component_id": component["id"],
        "component_version": component["version"],
        "operation": payload["operation"],
        "measurement_name": measurement["name"],
        "measurement_value": measurement["value"],
        "measurement_unit": measurement.get("unit", ""),
        "dimensions": payload["dimensions"],
        "trace_id": trace.get("traceId", ""),
        "span_id": trace.get("spanId", ""),
        "parent_span_id": trace.get("parentSpanId") or "",
        "linked_span_ids": trace.get("linkedSpanIds", []),
        "run_id": correlation.get("runId", ""),
        "conversation_id": correlation.get("conversationId", ""),
        "deployment_id": deployment["deploymentId"],
        "git_commit": deployment["gitCommit"],
        "environment": deployment["environment"],
        "host_id": deployment["hostId"],
        "service_role": payload["serviceRole"],
        "schema_version": payload["schemaVersion"],
        "capture_policy": payload["capturePolicy"],
    }


__all__ = ["ClickHouseObservationStore", "clickhouse_row"]
