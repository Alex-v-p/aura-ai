"""ClickHouse high-cardinality observation adapter.

The HTTP protocol is used deliberately to keep the service dependency-free;
the repository port remains the only interface visible to the ingest use case.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

import httpx

from aura_observatory.contract import MetadataObservation
from aura_observatory.ports import (
    BatchClaim,
    BatchObservationIdRegistry,
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


def _record_storage_span(
    metrics: Any | None,
    *,
    duration_ms: float,
    outcome: str,
    error_code: str | None = None,
) -> None:
    if metrics is None:
        return
    record_span = getattr(metrics, "record_component_span", None)
    if not callable(record_span):
        return
    try:
        record_span(
            component_id="aura.observatory.telemetry_storage",
            component_version="1.0.0",
            deployment_id=metrics.deployment_id,
            operation="write",
            duration_ms=duration_ms,
            outcome=outcome,
            dependency="clickhouse",
            error_code=error_code,
        )
    except (AttributeError, TypeError, ValueError):
        return


def _storage_error_code(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if error.__class__.__name__ == "CancelledError":
        # Cancellation is represented by the existing finite provider failure
        # class; the metadata span contract intentionally has no cancellation
        # label and must not be expanded by this work item.
        return "transport_unavailable"
    return "transport_unavailable"


class ClickHouseObservationStore:
    def __init__(
        self,
        endpoint: str,
        *,
        database: str = "aura_observatory",
        user: str = "default",
        password: str = "",
        id_registry: ObservationIdRegistry,
        max_concurrency: int = 8,
        metrics: Any | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError("ClickHouse ingest concurrency is out of bounds")
        self.endpoint = endpoint.rstrip("/")
        self.database = database
        self.user = user
        self.password = password
        self.id_registry = id_registry
        self.max_concurrency = max_concurrency
        self.metrics = metrics

    async def insert_observations(
        self, observations: Sequence[MetadataObservation]
    ) -> dict[str, InsertOutcome]:
        """Write one bounded batch with one client and bounded concurrency.

        PostgreSQL claims, immutable sequences, and pending receipts are
        established before this method performs any external I/O.  Failed or
        ambiguous provider calls remain pending for redelivery/reconciliation;
        only confirmed successes are settled and made visible.
        """

        if not observations:
            return {}
        if len(observations) > 5000:
            raise ValueError("ClickHouse ingest batch is out of bounds")
        claim_batch = getattr(self.id_registry, "claim_batch", None)
        if not callable(claim_batch):
            result: dict[str, InsertOutcome] = {}
            for observation in observations:
                result[observation.observation_id] = await self.insert_observation(observation)
            return result
        batch_registry = cast(BatchObservationIdRegistry, self.id_registry)
        claims = await cast(
            Callable[[Sequence[tuple[str, datetime]]], Awaitable[Sequence[BatchClaim]]],
            batch_registry.claim_batch,
        )(
            [(observation.observation_id, observation.observed_at) for observation in observations]
        )
        by_id = {claim.observation_id: claim for claim in claims}
        result = {
            observation.observation_id: (
                InsertOutcome.DUPLICATE
                if by_id[observation.observation_id].outcome is ClaimOutcome.DUPLICATE
                else InsertOutcome.RETRYABLE
                if by_id[observation.observation_id].outcome is ClaimOutcome.PENDING
                else InsertOutcome.RETRYABLE
            )
            for observation in observations
        }
        acquired = [
            observation
            for observation in observations
            if by_id[observation.observation_id].outcome is ClaimOutcome.ACQUIRED
            and by_id[observation.observation_id].ingest_sequence is not None
        ]
        if not acquired:
            await cast(BatchObservationIdRegistry, self.id_registry).abort_batch([])
            return result
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def write_one(observation: MetadataObservation) -> tuple[str, bool]:
            claim = by_id[observation.observation_id]
            sequence = claim.ingest_sequence
            if sequence is None:
                return observation.observation_id, False
            async with semaphore:
                try:
                    await self._insert_with_client(client, observation, sequence)
                except Exception:
                    return observation.observation_id, False
            return observation.observation_id, True

        successful: list[tuple[str, int]] = []
        started = perf_counter()
        provider_span_recorded = False
        try:
            async with httpx.AsyncClient() as client:
                outcomes: list[tuple[str, bool]] = []
                for offset in range(0, len(acquired), self.max_concurrency):
                    wave = acquired[offset : offset + self.max_concurrency]
                    outcomes.extend(await asyncio.gather(*(write_one(item) for item in wave)))
            for observation_id, success in outcomes:
                sequence = by_id[observation_id].ingest_sequence
                if success and sequence is not None:
                    successful.append((observation_id, sequence))
                    result[observation_id] = InsertOutcome.ACCEPTED
            provider_partial = len(successful) < len(acquired)
            _record_storage_span(
                self.metrics,
                duration_ms=(perf_counter() - started) * 1000.0,
                outcome="partial" if provider_partial else "complete",
                error_code="transport_unavailable" if provider_partial else None,
            )
            provider_span_recorded = True
            if successful:
                await cast(BatchObservationIdRegistry, self.id_registry).settle_batch(successful)
            else:
                await cast(BatchObservationIdRegistry, self.id_registry).abort_batch(
                    [item.observation_id for item in acquired]
                )
        except BaseException as exc:
            if not provider_span_recorded:
                _record_storage_span(
                    self.metrics,
                    duration_ms=(perf_counter() - started) * 1000.0,
                    outcome="failure",
                    error_code=_storage_error_code(exc),
                )
            await cast(BatchObservationIdRegistry, self.id_registry).abort_batch(
                [item.observation_id for item in acquired]
            )
            raise
        return result

    async def _insert_with_client(
        self, client: httpx.AsyncClient, observation: MetadataObservation, sequence: int
    ) -> None:
        payload = observation.to_payload()
        insert = (
            "INSERT INTO observations SETTINGS "
            "insert_deduplicate=1, "
            f"insert_deduplication_token={_sql_literal(observation.observation_id)} "
            "FORMAT JSONEachRow\n"
            + json.dumps(clickhouse_row(payload, ingest_sequence=sequence), separators=(",", ":"))
        )
        inserted = await client.post(
            self.endpoint,
            params={
                "database": self.database,
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
                insert = (
                    "INSERT INTO observations SETTINGS "
                    "insert_deduplicate=1, "
                    f"insert_deduplication_token={_sql_literal(observation.observation_id)} "
                    "FORMAT JSONEachRow\n"
                    + json.dumps(
                        clickhouse_row(payload, ingest_sequence=sequence), separators=(",", ":")
                    )
                )
                inserted = await client.post(
                    self.endpoint,
                    params={"database": self.database},
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
