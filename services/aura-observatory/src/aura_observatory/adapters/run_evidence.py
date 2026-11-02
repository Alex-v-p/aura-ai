"""Observatory-owned run-evidence PostgreSQL and Garage adapters."""
# ruff: noqa: E501,F401
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportPrivateUsage=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

import httpx

from aura_observatory.adapters.object_storage import GarageEvaluationObjectStore
from aura_observatory.run_evidence import (
    MAX_ENVELOPE_BYTES,
    MAX_EVIDENCE_BYTES,
    DuplicateCollision,
    RunEvidenceDeletion,
    RunEvidenceDeletionClosed,
    RunEvidenceDeletionInProgress,
    RunEvidenceManifest,
    RunEvidenceRepository,
    ValidatedRunEvidence,
    _decode_cursor,
    _encode_cursor,
    _record_component_span,
)

_APPROVED_MEDIA = frozenset(
    {"application/json", "text/plain", "text/markdown", "image/png", "image/jpeg", "image/webp", "application/pdf"}
)


def run_evidence_object_key(run_id: str, evidence_id: str) -> str:
    """Derive an internal key; no caller-controlled path is accepted."""

    try:
        run = UUID(run_id)
        evidence = UUID(evidence_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("run and evidence identifiers must be UUIDs") from exc
    if str(run) != run_id or str(evidence) != evidence_id:
        raise ValueError("run and evidence identifiers must be canonical UUIDs")
    return f"runs/{run}/evidence/{evidence}"


class RunEvidenceObjectStore:
    async def put(self, run_id: str, evidence_id: str, data: bytes, *, media_type: str, digest: str) -> int: ...
    async def get(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> bytes: ...
    def stream(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> AsyncIterator[bytes]: ...
    async def delete(self, run_id: str, evidence_id: str) -> None: ...


class MemoryRunEvidenceObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, run_id: str, evidence_id: str, data: bytes, *, media_type: str, digest: str) -> int:
        if len(data) > MAX_EVIDENCE_BYTES:
            raise ValueError("payload_limit_exceeded")
        if media_type not in _APPROVED_MEDIA:
            raise ValueError("unsupported_media_type")
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("storage integrity failure")
        self.objects[run_evidence_object_key(run_id, evidence_id)] = bytes(data)
        return len(data)

    async def get(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> bytes:
        data = self.objects.get(run_evidence_object_key(run_id, evidence_id))
        if data is None or len(data) != byte_count or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise FileNotFoundError("evidence object unavailable")
        return data

    async def stream(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> AsyncIterator[bytes]:
        data = await self.get(run_id, evidence_id, digest=digest, byte_count=byte_count)
        for offset in range(0, len(data), 64 * 1024):
            yield data[offset : offset + 64 * 1024]

    async def delete(self, run_id: str, evidence_id: str) -> None:
        self.objects.pop(run_evidence_object_key(run_id, evidence_id), None)


class UnavailableRunEvidenceObjectStore:
    """Fail closed when production Garage credentials are not configured."""

    async def put(self, run_id: str, evidence_id: str, data: bytes, *, media_type: str, digest: str) -> int:
        del run_id, evidence_id, data, media_type, digest
        raise RuntimeError("object_store_unavailable")

    async def get(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> bytes:
        del run_id, evidence_id, digest, byte_count
        raise RuntimeError("object_store_unavailable")

    async def stream(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> AsyncIterator[bytes]:
        del run_id, evidence_id, digest, byte_count
        raise RuntimeError("object_store_unavailable")

    async def delete(self, run_id: str, evidence_id: str) -> None:
        del run_id, evidence_id
        raise RuntimeError("object_store_unavailable")


class GarageRunEvidenceObjectStore:
    """Garage adapter scoped to the dedicated run-evidence bucket."""

    def __init__(self, endpoint: str, bucket: str, *, access_key: str, secret_key: str, region: str = "garage", timeout_seconds: float = 30.0) -> None:
        self._garage = GarageEvaluationObjectStore(endpoint, bucket, access_key=access_key, secret_key=secret_key, region=region, timeout_seconds=timeout_seconds)

    async def put(self, run_id: str, evidence_id: str, data: bytes, *, media_type: str, digest: str) -> int:
        key = run_evidence_object_key(run_id, evidence_id)
        if len(data) > MAX_EVIDENCE_BYTES:
            raise ValueError("payload_limit_exceeded")
        if media_type not in _APPROVED_MEDIA:
            raise ValueError("unsupported_media_type")
        if digest != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ValueError("storage integrity failure")
        request = self._garage.signed_request("PUT", key, headers={"content-type": media_type}, payload_hash=hashlib.sha256(data).hexdigest(), content=_one(data))
        async with httpx.AsyncClient(timeout=self._garage.timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.send(request)
            try:
                response.raise_for_status()
            finally:
                await response.aclose()
        return len(data)

    async def get(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> bytes:
        chunks = [chunk async for chunk in self.stream(run_id, evidence_id, digest=digest, byte_count=byte_count)]
        return b"".join(chunks)

    async def stream(self, run_id: str, evidence_id: str, *, digest: str, byte_count: int) -> AsyncIterator[bytes]:
        if byte_count < 0 or byte_count > MAX_EVIDENCE_BYTES:
            raise ValueError("storage integrity failure")
        key = run_evidence_object_key(run_id, evidence_id)
        request = self._garage.signed_request("GET", key, payload_hash=hashlib.sha256(b"").hexdigest())
        client = httpx.AsyncClient(timeout=self._garage.timeout, follow_redirects=False, trust_env=False)
        response: httpx.Response | None = None
        total = 0
        hasher = hashlib.sha256()
        try:
            response = await client.send(request, stream=True)
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_EVIDENCE_BYTES or int(declared) != byte_count):
                raise ValueError("storage integrity failure")
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > MAX_EVIDENCE_BYTES or total > byte_count:
                    raise ValueError("storage integrity failure")
                hasher.update(chunk)
                yield chunk
            if total != byte_count or "sha256:" + hasher.hexdigest() != digest:
                raise ValueError("storage integrity failure")
        finally:
            if response is not None:
                await response.aclose()
            await client.aclose()

    async def delete(self, run_id: str, evidence_id: str) -> None:
        key = run_evidence_object_key(run_id, evidence_id)
        request = self._garage.signed_request("DELETE", key, payload_hash=hashlib.sha256(b"").hexdigest())
        async with httpx.AsyncClient(timeout=self._garage.timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.send(request)
            try:
                if response.status_code not in {200, 204, 404}:
                    response.raise_for_status()
            finally:
                await response.aclose()


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


class PostgresRunEvidenceRepository(RunEvidenceRepository):
    """Durable manifest repository; object bytes are committed before capture."""

    def __init__(self, connection_factory: Any, object_store: RunEvidenceObjectStore, *, events: Any | None = None, metrics: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._object_store = object_store
        self._events = events
        self._metrics = metrics
        self._last_dedupe_duration_ms = 0.001

    async def ingest(self, item: ValidatedRunEvidence) -> str:
        value = item.envelope
        delivery = cast(Mapping[str, object], value["delivery"])
        async with self._connection_factory() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("run-evidence-run:" + item.run_id,),
            )
            deletion_result = await connection.execute(
                "SELECT status FROM observatory_run_evidence_deletions WHERE run_id=%s ORDER BY (status = 'completed') DESC, requested_at DESC LIMIT 1",
                (item.run_id,),
            )
            deletion_row = await deletion_result.fetchone()
            if item.envelope.get("captureState") == "captured" and deletion_row is not None:
                if str(deletion_row[0]) == "in_progress":
                    raise RunEvidenceDeletionInProgress()
                if str(deletion_row[0]) == "completed":
                    raise RunEvidenceDeletionClosed()
            # Serialize competing deliveries for each immutable identity
            # before any Garage write.  Locking the sorted pair avoids a
            # cross-swapped event/evidence deadlock while closing the
            # SELECT/PUT/INSERT race across workers.
            for stable_id in sorted((item.event_id, item.evidence_id)):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("run-evidence:" + stable_id,),
                )
            dedupe_started = monotonic()
            result = await connection.execute("SELECT fingerprint FROM observatory_run_evidence_manifests WHERE evidence_id = %s OR event_id = %s", (item.evidence_id, item.event_id))
            rows = await result.fetchall()
            self._last_dedupe_duration_ms = max(0.001, (monotonic() - dedupe_started) * 1000.0)
            if rows:
                if any(str(row[0]) != item.fingerprint for row in rows):
                    # Collision truth is committed in its own transaction;
                    # the terminal DuplicateCollision must not roll it back.
                    async with self._connection_factory() as collision_connection:
                        for stable_id in (item.event_id, item.evidence_id):
                            await collision_connection.execute(
                                "INSERT INTO observatory_run_evidence_collisions(collision_id,stable_id,run_id,outcome) VALUES (%s,%s,%s,'duplicate_collision')",
                                (str(uuid4()), stable_id, item.run_id),
                            )
                    raise DuplicateCollision()
                await connection.execute(
                    "INSERT INTO observatory_run_evidence_deliveries(delivery_id,event_id,evidence_id,producer_id,attempt,first_delivery_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (delivery["deliveryId"], item.event_id, item.evidence_id, delivery["producerId"], delivery["attempt"], delivery.get("firstDeliveryId", delivery["deliveryId"])),
                )
                return "duplicate"
            object_written = item.envelope.get("captureState") == "captured"
            if object_written:
                # Commit a cleanup intent before the external PUT.  If the
                # process dies after Garage accepts bytes but before the
                # manifest transaction, the recovery worker can still find
                # and remove the orphan.
                await self._record_orphan(item)
                object_started = monotonic()
                try:
                    await self._object_store.put(item.run_id, item.evidence_id, item.canonical_bytes, media_type=cast(str, value["mediaType"]), digest=cast(str, value["digest"]))
                except Exception:
                    _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_write", duration_ms=(monotonic() - object_started) * 1000.0, outcome="failure", dependency="garage", error_code="object_store_unavailable", trace_id=cast(str, value["traceId"]), parent_span_id=cast(str | None, value.get("parentSpanId")), run_id=item.run_id)
                    raise
                _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_write", duration_ms=(monotonic() - object_started) * 1000.0, outcome="success", dependency="garage", trace_id=cast(str, value["traceId"]), parent_span_id=cast(str | None, value.get("parentSpanId")), run_id=item.run_id)
                record = getattr(self._metrics, "record", None)
                if callable(record):
                    try:
                        record("run_evidence_object_storage_latency_ms", (monotonic() - object_started) * 1000.0)
                    except (TypeError, ValueError):
                        pass
            manifest_started = monotonic()
            try:
                await connection.execute(
                    """INSERT INTO observatory_run_evidence_manifests(
                evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,
                capture_state,media_classification,media_type,digest,byte_count,captured_at,reason,fingerprint,object_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (evidence_id) DO NOTHING""",
                (item.evidence_id, item.event_id, item.run_id, value["traceId"], value["spanId"], value.get("parentSpanId"), value["sequence"], value["stepCategory"], value["evidenceKind"], value["captureState"], value["mediaClassification"], value["mediaType"], value.get("digest"), value["byteCount"], value["observedAt"], value.get("reason"), item.fingerprint, run_evidence_object_key(item.run_id, item.evidence_id) if object_written else None),
                )
                await connection.execute(
                    "INSERT INTO observatory_run_evidence_events(event_id,run_id,occurred_at) VALUES (%s,%s,%s) ON CONFLICT (event_id) DO NOTHING",
                    (str(uuid4()), item.run_id, value["observedAt"]),
                )
                await connection.execute(
                    "INSERT INTO observatory_run_evidence_deliveries(delivery_id,event_id,evidence_id,producer_id,attempt,first_delivery_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (delivery["deliveryId"], item.event_id, item.evidence_id, delivery["producerId"], delivery["attempt"], delivery.get("firstDeliveryId", delivery["deliveryId"])),
                )
                if object_written:
                    # This removal is in the same transaction as the
                    # authoritative manifest.  A crash before commit leaves
                    # the pending cleanup intent; a committed manifest can
                    # never be mistaken for an orphan.
                    await connection.execute(
                        "DELETE FROM observatory_run_evidence_orphans WHERE run_id=%s AND evidence_id=%s",
                        (item.run_id, item.evidence_id),
                    )
            except Exception:
                _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.manifest_write", duration_ms=(monotonic() - manifest_started) * 1000.0, outcome="failure", dependency="postgresql", error_code="manifest_store_unavailable", trace_id=cast(str, value["traceId"]), parent_span_id=cast(str | None, value.get("parentSpanId")), run_id=item.run_id)
                if object_written:
                    try:
                        await self._object_store.delete(item.run_id, item.evidence_id)
                    except Exception:
                        await self._record_orphan(item)
                raise
            _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.manifest_write", duration_ms=(monotonic() - manifest_started) * 1000.0, outcome="success", dependency="postgresql", trace_id=cast(str, value["traceId"]), parent_span_id=cast(str | None, value.get("parentSpanId")), run_id=item.run_id)
            record = getattr(self._metrics, "record", None)
            if callable(record):
                try:
                    record("run_evidence_manifest_storage_latency_ms", (monotonic() - manifest_started) * 1000.0)
                except (TypeError, ValueError):
                    pass
        if self._events is not None:
            await self._events(item.run_id)
        return "accepted"

    async def _record_orphan(self, item: ValidatedRunEvidence) -> None:
        async with self._connection_factory() as connection:
            await connection.execute(
                "INSERT INTO observatory_run_evidence_orphans(orphan_id,run_id,evidence_id,object_key,status) VALUES (%s,%s,%s,%s,'pending') ON CONFLICT (run_id,evidence_id) DO NOTHING",
                (str(uuid4()), item.run_id, item.evidence_id, run_evidence_object_key(item.run_id, item.evidence_id)),
            )

    async def recover_orphans(self, limit: int = 100) -> int:
        async with self._connection_factory() as connection:
            result = await connection.execute("SELECT orphan_id,run_id,evidence_id FROM observatory_run_evidence_orphans WHERE status='pending' ORDER BY created_at LIMIT %s", (max(1, min(100, limit)),))
            rows = await result.fetchall()
        recovered = 0
        for orphan_id, run_id, evidence_id in rows:
            try:
                async with self._connection_factory() as connection:
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("run-evidence-run:" + str(run_id),),
                    )
                    manifest_result = await connection.execute(
                        "SELECT 1 FROM observatory_run_evidence_manifests WHERE run_id=%s AND evidence_id=%s LIMIT 1",
                        (str(run_id), str(evidence_id)),
                    )
                    if await manifest_result.fetchone() is None:
                        # Keep the transaction/lock open through the external
                        # delete. Ingest uses the same run lock, so a
                        # successful manifest commit cannot race cleanup.
                        await self._object_store.delete(str(run_id), str(evidence_id))
                    await connection.execute("UPDATE observatory_run_evidence_orphans SET status='recovered',recovered_at=CURRENT_TIMESTAMP WHERE orphan_id=%s", (str(orphan_id),))
                    await connection.execute("INSERT INTO observatory_run_evidence_events(event_id,run_id,occurred_at) VALUES (%s,%s,CURRENT_TIMESTAMP)", (str(uuid4()), str(run_id)))
                recovered += 1
            except Exception:
                async with self._connection_factory() as connection:
                    await connection.execute("UPDATE observatory_run_evidence_orphans SET attempts=attempts+1,last_error='delete_failed' WHERE orphan_id=%s", (str(orphan_id),))
        return recovered

    async def recover_deletions(self, limit: int = 100) -> int:
        """Resume committed deletion intents after a worker/API restart.

        The deletion row is the durable work queue.  Re-entering ``delete``
        is idempotent: it re-reads the still-captured manifests, repeats only
        bounded UUID-key deletes, and commits the terminal projection after
        every object has been handled.
        """

        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT run_id,idempotency_key FROM observatory_run_evidence_deletions "
                "WHERE status IN ('in_progress','failed') ORDER BY requested_at LIMIT %s",
                (max(1, min(100, int(limit))),),
            )
            rows = await result.fetchall()
        recovered = 0
        for run_id, idempotency_key in rows:
            try:
                result = await self.delete(str(run_id), str(idempotency_key), _resume=True)
                if result.status == "completed":
                    recovered += 1
            except Exception:
                # Keep the durable failed/in-progress row for the next pass;
                # the recovery worker must not turn a provider outage into
                # data loss or stop unrelated evidence consumption.
                continue
        return recovered

    async def manifests(self, run_id: str, *, cursor: str | None = None, limit: int = 1000) -> tuple[list[RunEvidenceManifest], str | None]:
        limit = max(1, min(1000, int(limit)))
        decoded = _decode_cursor(cursor)
        async with self._connection_factory() as connection:
            if isinstance(decoded, tuple):
                result = await connection.execute("""SELECT evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,capture_state,media_classification,media_type,digest,byte_count,captured_at,reason FROM observatory_run_evidence_manifests WHERE run_id = %s AND (sequence,captured_at,evidence_id) > (%s,%s,%s) ORDER BY sequence,captured_at,evidence_id LIMIT %s""", (run_id, decoded[0], decoded[1], decoded[2], limit + 1))
            else:
                result = await connection.execute("""SELECT evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,capture_state,media_classification,media_type,digest,byte_count,captured_at,reason FROM observatory_run_evidence_manifests WHERE run_id = %s ORDER BY sequence,captured_at,evidence_id OFFSET %s LIMIT %s""", (run_id, decoded, limit + 1))
            rows = await result.fetchall()
        values = [_manifest(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and values:
            last = values[-1]
            next_cursor = _encode_cursor((last.sequence, last.captured_at, last.evidence_id))
        return values, next_cursor

    async def inspection_page(self, run_id: str, *, cursor: str | None = None, limit: int = 1000) -> tuple[list[RunEvidenceManifest], str | None, Mapping[str, object]]:
        """Read page and run-wide summary from one repeatable-read snapshot."""

        limit = max(1, min(1000, int(limit)))
        decoded = _decode_cursor(cursor)
        async with self._connection_factory() as connection:
            # psycopg starts a transaction implicitly on the first statement
            # when autocommit is disabled.  SET TRANSACTION must therefore be
            # the first statement; a second explicit BEGIN would either fail
            # or leave the connection at READ COMMITTED.
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            try:
                if isinstance(decoded, tuple):
                    result = await connection.execute("""SELECT evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,capture_state,media_classification,media_type,digest,byte_count,captured_at,reason FROM observatory_run_evidence_manifests WHERE run_id = %s AND (sequence,captured_at,evidence_id) > (%s,%s,%s) ORDER BY sequence,captured_at,evidence_id LIMIT %s""", (run_id, decoded[0], decoded[1], decoded[2], limit + 1))
                else:
                    result = await connection.execute("""SELECT evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,capture_state,media_classification,media_type,digest,byte_count,captured_at,reason FROM observatory_run_evidence_manifests WHERE run_id = %s ORDER BY sequence,captured_at,evidence_id OFFSET %s LIMIT %s""", (run_id, decoded, limit + 1))
                rows = await result.fetchall()
                values = [_manifest(row) for row in rows[:limit]]
                next_cursor = None
                if len(rows) > limit and values:
                    last = values[-1]
                    next_cursor = _encode_cursor((last.sequence, last.captured_at, last.evidence_id))
                counts_result = await connection.execute(
                    "SELECT capture_state,COUNT(*) FROM observatory_run_evidence_manifests WHERE run_id=%s GROUP BY capture_state",
                    (run_id,),
                )
                counts_rows = await counts_result.fetchall()
                reasons_result = await connection.execute(
                    "SELECT DISTINCT reason FROM observatory_run_evidence_manifests WHERE run_id=%s AND reason IS NOT NULL",
                    (run_id,),
                )
                reason_rows = await reasons_result.fetchall()
                deletion_result = await connection.execute(
                    "SELECT status FROM observatory_run_evidence_deletions WHERE run_id=%s ORDER BY (status = 'completed') DESC, requested_at DESC LIMIT 1",
                    (run_id,),
                )
                deletion_row = await deletion_result.fetchone()
                await connection.execute("COMMIT")
            except Exception:
                await connection.execute("ROLLBACK")
                raise
        counts = {str(row[0]): int(row[1]) for row in counts_rows}
        aggregate = {"total": sum(counts.values()), "counts": counts, "reasons": sorted(str(row[0]) for row in reason_rows), "deletionStatus": str(deletion_row[0]) if deletion_row else None}
        return values, next_cursor, aggregate

    async def summary(self, run_id: str) -> Mapping[str, object]:
        async with self._connection_factory() as connection:
            result = await connection.execute("SELECT capture_state,COUNT(*) FROM observatory_run_evidence_manifests WHERE run_id=%s GROUP BY capture_state", (run_id,))
            rows = await result.fetchall()
            reasons_result = await connection.execute("SELECT DISTINCT reason FROM observatory_run_evidence_manifests WHERE run_id=%s AND reason IS NOT NULL", (run_id,))
            reason_rows = await reasons_result.fetchall()
            deletion_result = await connection.execute(
                "SELECT status FROM observatory_run_evidence_deletions WHERE run_id=%s ORDER BY (status = 'completed') DESC, requested_at DESC LIMIT 1",
                (run_id,),
            )
            deletion_row = await deletion_result.fetchone()
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {"total": sum(counts.values()), "counts": counts, "reasons": sorted(str(row[0]) for row in reason_rows), "deletionStatus": str(deletion_row[0]) if deletion_row else None}

    async def stream(self, run_id: str, evidence_id: str) -> tuple[RunEvidenceManifest, AsyncIterator[bytes]]:
        async with self._connection_factory() as connection:
            result = await connection.execute("SELECT evidence_id,event_id,run_id,trace_id,span_id,parent_span_id,sequence,step_category,evidence_kind,capture_state,media_classification,media_type,digest,byte_count,captured_at,reason FROM observatory_run_evidence_manifests WHERE run_id = %s AND evidence_id = %s", (run_id, evidence_id))
            row = await result.fetchone()
        if row is None:
            raise KeyError("evidence not found")
        manifest = _manifest(row)
        if manifest.capture_state == "deleted" or manifest.digest is None:
            raise FileNotFoundError("evidence deleted")
        stream = self._object_store.stream(
            run_id, evidence_id, digest=manifest.digest, byte_count=manifest.byte_count
        )
        return manifest, stream

    async def delete(self, run_id: str, idempotency_key: str, *, _resume: bool = False) -> RunEvidenceDeletion:
        """Serialize one run's deletion across API and recovery workers."""

        async with self._connection_factory() as lock_connection:
            await lock_connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                ("run-evidence-run:" + run_id,),
            )
            try:
                return await self._delete_locked(run_id, idempotency_key, _resume=_resume)
            finally:
                await lock_connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    ("run-evidence-run:" + run_id,),
                )

    async def _delete_locked(self, run_id: str, idempotency_key: str, *, _resume: bool = False) -> RunEvidenceDeletion:
        # Commit intent and the target set before touching Garage.  A worker
        # restart can therefore resume an in-progress deletion without
        # relying on an uncommitted PostgreSQL transaction.
        async with self._connection_factory() as connection:
            # The public wrapper holds the run-scoped session advisory lock
            # for the complete operation.  Taking the same lock again from
            # this second pooled connection would self-deadlock.
            prior = await connection.execute("SELECT deletion_id,status,requested_at,completed_at,deleted_evidence_count,failure_code FROM observatory_run_evidence_deletions WHERE run_id = %s AND idempotency_key = %s", (run_id, idempotency_key))
            row = await prior.fetchone()
            terminal = await connection.execute(
                "SELECT deletion_id,status,requested_at,completed_at,deleted_evidence_count,failure_code FROM observatory_run_evidence_deletions WHERE run_id=%s AND status='completed' ORDER BY completed_at DESC LIMIT 1",
                (run_id,),
            )
            terminal_row = await terminal.fetchone()
            if terminal_row is not None and (row is None or str(row[1]) != "completed"):
                row = terminal_row
            if row is not None and str(row[1]) == "completed":
                return _deletion(row, run_id)
            if row is not None and str(row[1]) == "in_progress" and not _resume:
                # Another request owns the committed work item.  Recovery is
                # the only path allowed to resume an abandoned owner.
                return _deletion(row, run_id)
            result = await connection.execute("SELECT evidence_id,trace_id,parent_span_id FROM observatory_run_evidence_manifests WHERE run_id = %s AND capture_state = 'captured'", (run_id,))
            targets = [(str(value[0]), str(value[1]), str(value[2]) if value[2] is not None else None) for value in await result.fetchall()]
            now = datetime.now(UTC)
            deletion_id = str(row[0]) if row is not None else str(uuid4())
            requested_at = row[2] if row is not None else now
            if row is not None and str(row[1]) == "failed":
                await connection.execute(
                    "UPDATE observatory_run_evidence_deletions SET status='in_progress',completed_at=NULL,failure_code=NULL WHERE deletion_id=%s",
                    (deletion_id,),
                )
            await connection.execute(
                "INSERT INTO observatory_run_evidence_deletions(deletion_id,run_id,idempotency_key,status,requested_at,completed_at,deleted_evidence_count,audit_mode,failure_code) VALUES (%s,%s,%s,'in_progress',%s,NULL,%s,'content_free',NULL) ON CONFLICT (run_id,idempotency_key) DO NOTHING",
                (deletion_id, run_id, idempotency_key, requested_at, 0),
            )
            await connection.execute("INSERT INTO observatory_run_evidence_audit(audit_id,run_id,operation,outcome,occurred_at) VALUES (%s,%s,'delete_requested','accepted',%s)", (str(uuid4()), run_id, now))
            await connection.execute("INSERT INTO observatory_run_evidence_events(event_id,run_id,occurred_at) VALUES (%s,%s,%s)", (str(uuid4()), run_id, now))
        # A failed/restarted deletion already has a durable count for objects
        # acknowledged by Garage on an earlier attempt.  Keep that count and
        # add only the remaining captured targets below.
        deleted_count = int(row[4]) if row is not None else 0
        try:
            for evidence_id, trace_id, parent_span_id in targets:
                object_started = monotonic()
                try:
                    await self._object_store.delete(run_id, evidence_id)
                except Exception:
                    _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_delete", duration_ms=(monotonic() - object_started) * 1000.0, outcome="failure", dependency="garage", error_code="object_store_unavailable", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
                    raise
                _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_delete", duration_ms=(monotonic() - object_started) * 1000.0, outcome="success", dependency="garage", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
                # Garage has acknowledged the delete.  Transition the
                # manifest and advance the durable count in one transaction;
                # recovery will only select manifests that remain captured.
                next_deleted_count = deleted_count
                async with self._connection_factory() as progress_connection:
                    transition = await progress_connection.execute(
                        "UPDATE observatory_run_evidence_manifests SET capture_state='deleted',digest=NULL,byte_count=0,reason='manually_deleted',fingerprint=NULL,object_key=NULL WHERE run_id=%s AND evidence_id=%s AND capture_state='captured' RETURNING evidence_id",
                        (run_id, evidence_id),
                    )
                    transitioned = await transition.fetchone()
                    if transitioned is not None:
                        next_deleted_count += 1
                        await progress_connection.execute("UPDATE observatory_run_evidence_deletions SET deleted_evidence_count=%s WHERE deletion_id=%s AND status='in_progress'", (next_deleted_count, deletion_id))
                # A commit failure rolls the transition and count back.  Do
                # not let the failure handler persist this speculative value.
                deleted_count = next_deleted_count
        except Exception:
            failed_at = datetime.now(UTC)
            failure_started = monotonic()
            async with self._connection_factory() as failure_connection:
                # The failing target remains captured with its object key so
                # recover_deletions can retry it after a restart.  Successful
                # targets were transitioned atomically with their progress
                # count above.
                await failure_connection.execute("UPDATE observatory_run_evidence_deletions SET status='failed',completed_at=NULL,deleted_evidence_count=%s,failure_code='object_delete_failed' WHERE deletion_id=%s", (deleted_count, deletion_id))
                await failure_connection.execute("INSERT INTO observatory_run_evidence_audit(audit_id,run_id,operation,outcome,occurred_at) VALUES (%s,%s,'delete','failed',%s)", (str(uuid4()), run_id, failed_at))
                await failure_connection.execute("INSERT INTO observatory_run_evidence_events(event_id,run_id,occurred_at) VALUES (%s,%s,%s)", (str(uuid4()), run_id, failed_at))
            failure_trace = targets[0][1] if targets else None
            failure_parent = targets[0][2] if targets else None
            _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.deletion_finalize", duration_ms=(monotonic() - failure_started) * 1000.0, outcome="failure", dependency="postgresql", error_code="deletion_failed", trace_id=failure_trace, parent_span_id=failure_parent, run_id=run_id)
            return RunEvidenceDeletion(deletion_id, run_id, "failed", _iso(requested_at), None, deleted_count, "object_delete_failed")
        completed_at = datetime.now(UTC)
        finalize_started = monotonic()
        finalize_trace = targets[0][1] if targets else None
        finalize_parent = targets[0][2] if targets else None
        async with self._connection_factory() as finalize_connection:
            await finalize_connection.execute("UPDATE observatory_run_evidence_manifests SET capture_state='deleted',digest=NULL,byte_count=0,reason='manually_deleted',fingerprint=NULL,object_key=NULL WHERE run_id = %s", (run_id,))
            await finalize_connection.execute("UPDATE observatory_run_evidence_deletions SET status='completed',completed_at=%s,deleted_evidence_count=%s,failure_code=NULL WHERE deletion_id=%s", (completed_at, deleted_count, deletion_id))
            await finalize_connection.execute("INSERT INTO observatory_run_evidence_audit(audit_id,run_id,operation,outcome,occurred_at) VALUES (%s,%s,'delete','completed',%s)", (str(uuid4()), run_id, completed_at))
            await finalize_connection.execute("INSERT INTO observatory_run_evidence_events(event_id,run_id,occurred_at) VALUES (%s,%s,%s)", (str(uuid4()), run_id, completed_at))
        _record_component_span(self._metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.deletion_finalize", duration_ms=(monotonic() - finalize_started) * 1000.0, outcome="success", dependency="postgresql", trace_id=finalize_trace, parent_span_id=finalize_parent, run_id=run_id)
        return RunEvidenceDeletion(deletion_id, run_id, "completed", _iso(requested_at), _iso(completed_at), deleted_count)

    async def events_after(self, last_event_id: str | None = None) -> list[dict[str, object]]:
        async with self._connection_factory() as connection:
            if last_event_id:
                cursor_result = await connection.execute(
                    "SELECT sequence FROM observatory_run_evidence_events WHERE event_id = %s",
                    (last_event_id,),
                )
                cursor_row = await cursor_result.fetchone()
                if cursor_row is None:
                    return []
                cursor = int(cursor_row[0])
            else:
                cursor = 0
            result = await connection.execute(
                "SELECT sequence,event_id,run_id FROM observatory_run_evidence_events WHERE sequence > %s ORDER BY sequence LIMIT 1000",
                (cursor,),
            )
            rows = await result.fetchall()
        return [
            {
                "id": str(row[1]),
                "type": "observation.changed",
                "data": {
                    "resourceType": "run",
                    "resourceId": str(row[2]),
                    "capturePolicy": "metadata_only",
                },
            }
            for row in rows
        ]

    async def has_event(self, event_id: str) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT 1 FROM observatory_run_evidence_events WHERE event_id = %s LIMIT 1",
                (event_id,),
            )
            return await result.fetchone() is not None

    async def latest_event_id(self) -> str | None:
        async with self._connection_factory() as connection:
            result = await connection.execute("SELECT event_id FROM observatory_run_evidence_events ORDER BY sequence DESC LIMIT 1")
            row = await result.fetchone()
            return str(row[0]) if row is not None else None

    async def record_rejection(self, payload: bytes | Mapping[str, object], reason: str) -> None:
        wire_size = len(payload) if isinstance(payload, (bytes, bytearray)) else 0
        byte_count = min(MAX_ENVELOPE_BYTES, wire_size)
        safe_reason = reason if reason in {"payload_limit_exceeded", "ingest_rejected", "storage_failure", "transport_failure", "queue_pressure", "export_unavailable"} else "ingest_rejected"
        async with self._connection_factory() as connection:
            await connection.execute(
                "INSERT INTO observatory_run_evidence_rejections(rejection_id,reason,byte_count) VALUES (%s,%s,%s)",
                (str(uuid4()), safe_reason, byte_count),
            )


def _manifest(row: Any) -> RunEvidenceManifest:
    return RunEvidenceManifest(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), str(row[5]) if row[5] is not None else None, int(row[6]), str(row[7]), str(row[8]), str(row[9]), str(row[10]), str(row[11]), str(row[12]) if row[12] is not None else None, int(row[13]), _iso(row[14]), str(row[15]) if row[15] is not None else None)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value)


def _deletion(row: Any, run_id: str) -> RunEvidenceDeletion:
    return RunEvidenceDeletion(str(row[0]), run_id, str(row[1]), _iso(row[2]), _iso(row[3]) if row[3] else None, int(row[4]), str(row[5]) if row[5] else None)


__all__ = ["GarageRunEvidenceObjectStore", "MemoryRunEvidenceObjectStore", "PostgresRunEvidenceRepository", "UnavailableRunEvidenceObjectStore", "run_evidence_object_key"]
