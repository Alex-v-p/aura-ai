"""ClickHouse high-cardinality observation adapter.

The HTTP protocol is used deliberately to keep the service dependency-free;
the repository port remains the only interface visible to the ingest use case.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import (
    ClaimOutcome,
    InsertOutcome,
    ObservationIdRegistry,
    PendingObservationReceipt,
)

_LOGGER = logging.getLogger(__name__)
_CLICKHOUSE_ERROR_CLASSES = (
    ("parse_error", re.compile(r"cannot parse|cannot convert|type mismatch", re.I)),
    ("schema_error", re.compile(r"unknown (?:column|field)|no such column|missing column", re.I)),
    ("syntax_error", re.compile(r"syntax error|multiquery", re.I)),
    ("auth_error", re.compile(r"authentication|not authorized|access denied", re.I)),
    ("database_error", re.compile(r"database .* does not exist|unknown database", re.I)),
)


class ClickHouseObservationStore:
    def __init__(
        self,
        endpoint: str,
        *,
        database: str = "aura_observatory",
        user: str = "default",
        password: str = "",
        id_registry: ObservationIdRegistry,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.database = database
        self.user = user
        self.password = password
        self.id_registry = id_registry

    async def insert_observation(self, observation: MetadataObservation) -> InsertOutcome:
        claim = await self.id_registry.claim(observation.observation_id)
        if claim is ClaimOutcome.DUPLICATE:
            return InsertOutcome.DUPLICATE
        if claim is ClaimOutcome.PENDING:
            return InsertOutcome.RETRYABLE
        payload = observation.to_payload()
        try:
            sequence = await self.id_registry.next_ingest_sequence(observation.observation_id)
            await self.id_registry.set_observed_at(
                observation.observation_id, observation.observed_at
            )
            # The receipt, immutable sequence, and pending lease are durable
            # before the external ClickHouse request. A crash after this
            # point reuses that same sequence on retry.
            await self.id_registry.commit_ingest_boundary(observation.observation_id)
            async with httpx.AsyncClient() as client:
                insert = "INSERT INTO observations FORMAT JSONEachRow\n" + json.dumps(
                    clickhouse_row(payload, ingest_sequence=sequence), separators=(",", ":")
                )
                inserted = await client.post(
                    self.endpoint,
                    params={
                        "database": self.database,
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": observation.observation_id,
                    },
                    auth=(self.user, self.password),
                    content=insert,
                )
                try:
                    inserted.raise_for_status()
                except httpx.HTTPStatusError:
                    code, message = _clickhouse_error_details(inserted)
                    _LOGGER.error(
                        "ClickHouse observation insert failed status=%s code=%s message=%s",
                        inserted.status_code,
                        code,
                        message,
                    )
                    raise
            await self.id_registry.mark_ingest_visible(sequence)
            await self.id_registry.complete(observation.observation_id)
        except BaseException:
            # Keep the pending lease on transport/response failure.  A retry
            # can safely recover it after expiry using the same ClickHouse
            # deduplication token, avoiding a second visible observation.
            await self.id_registry.abort(observation.observation_id)
            raise
        return InsertOutcome.ACCEPTED

    async def existing_observations(
        self,
        receipts: Sequence[PendingObservationReceipt],
        *,
        timeout_seconds: float,
    ) -> set[str]:
        """Probe a bounded set of immutable identity/sequence pairs.

        Only the two opaque key columns are selected.  This keeps crash-window
        recovery content-free while making the provider request independently
        timeout-bounded from the normal ingest path.
        """

        if not receipts:
            return set()
        if not 1 <= len(receipts) <= 1000:
            raise ValueError("presence probe batch is out of bounds")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("presence probe timeout is out of bounds")
        pairs = ",".join(
            "(" + _sql_literal(receipt.observation_id) + "," + str(receipt.ingest_sequence) + ")"
            for receipt in receipts
        )
        statement = (
            "SELECT observation_id, ingest_sequence FROM observations WHERE "
            "(observation_id, ingest_sequence) IN (" + pairs + ")"
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    params={"database": self.database},
                    auth=(self.user, self.password),
                    content=statement + " FORMAT JSONEachRow",
                )
                response.raise_for_status()
        except httpx.HTTPStatusError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise
        expected_sequences: dict[str, set[int]] = {}
        for receipt in receipts:
            expected_sequences.setdefault(receipt.observation_id, set()).add(
                receipt.ingest_sequence
            )
        present: set[str] = set()
        for line in response.text.splitlines():
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            row_value = cast(dict[str, Any], row)
            observation_id = row_value.get("observation_id")
            ingest_sequence = row_value.get("ingest_sequence")
            if (
                isinstance(observation_id, str)
                and isinstance(ingest_sequence, int)
                and not isinstance(ingest_sequence, bool)
                and ingest_sequence in expected_sequences.get(observation_id, set())
            ):
                present.add(observation_id)
        return present


def clickhouse_row(payload: dict[str, Any], *, ingest_sequence: int = 0) -> dict[str, Any]:
    component = cast(dict[str, Any], payload["component"])
    measurement = cast(dict[str, Any], payload["measurement"])
    trace = cast(dict[str, Any], payload.get("trace") or {})
    correlation = cast(dict[str, Any], payload.get("correlation") or {})
    deployment = cast(dict[str, Any], payload["deployment"])
    execution = cast(dict[str, Any], payload.get("execution") or {})
    row = {
        "observation_id": payload["observationId"],
        "observed_at": _clickhouse_timestamp(payload["observedAt"]),
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
    if int(payload.get("schemaVersion", 1)) == 2:
        row.update(
            {
                "image_version": deployment.get("imageVersion"),
                "configuration_version": deployment.get("configurationVersion"),
                "model_id": execution.get("modelId"),
                "model_version": execution.get("modelVersion"),
                "agent_revision": execution.get("agentRevision"),
                "prompt_bundle_revision": execution.get("promptBundleRevision"),
                "policy_revision": execution.get("policyRevision"),
            }
        )
    if ingest_sequence:
        row["ingest_sequence"] = ingest_sequence
    return row


def _clickhouse_timestamp(value: Any) -> str:
    """Encode a contract timestamp in ClickHouse DateTime64(3) syntax."""

    if not isinstance(value, str):
        raise ValueError("observedAt must be an ISO-8601 timestamp")
    wire_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(wire_value)
    except ValueError as exc:
        raise ValueError("observedAt must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observedAt must include a timezone")
    utc = parsed.astimezone(UTC)
    milliseconds = utc.microsecond // 1_000
    return utc.strftime("%Y-%m-%d %H:%M:%S") + f".{milliseconds:03d}"


def _clickhouse_error_details(response: httpx.Response) -> tuple[str, str]:
    """Return bounded, content-free diagnostics for a ClickHouse failure."""

    # ClickHouse error bodies can echo query fragments. Only classify a bounded
    # copy of the body; never log the raw response or insert payload.
    body = response.text[:4_096]
    code_match = re.search(r"\bCode:\s*(\d{1,5})\b", body, re.IGNORECASE)
    code = code_match.group(1) if code_match else "unknown"
    for message, pattern in _CLICKHOUSE_ERROR_CLASSES:
        if pattern.search(body):
            return code, message
    return code, "unclassified_error"


def _sql_literal(value: str) -> str:
    """Quote one bounded opaque identifier without logging or retaining it."""

    if not re.fullmatch(r"[0-9a-fA-F-]{1,128}", value):
        raise ValueError("invalid observation identifier")
    return "'" + value.replace("'", "''") + "'"


__all__ = ["ClickHouseObservationStore", "clickhouse_row"]
