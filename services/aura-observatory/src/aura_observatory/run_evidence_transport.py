"""Dedicated JetStream consumer for the one-way Core run-evidence channel."""
# ruff: noqa: E501
# pyright: reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from nats.errors import TimeoutError as NatsTimeoutError

from aura_observatory.evaluation_transport import connect_nats
from aura_observatory.run_evidence import (
    MAX_ENVELOPE_BYTES,
    RunEvidenceError,
    RunEvidenceIngestionService,
)

RUN_EVIDENCE_SUBJECT = "aura.runs.evidence.v1"
RUN_EVIDENCE_STREAM = "AURA_RUN_EVIDENCE"
RUN_EVIDENCE_DURABLE = "aura-observatory-run-evidence"


class RunEvidenceTransportError(RuntimeError):
    def __init__(self, code: str = "transport_unavailable") -> None:
        self.code = code if code in {"transport_unavailable", "timeout"} else "transport_unavailable"
        super().__init__(self.code)


class NatsRunEvidenceTransport:
    def __init__(self, connection: Any, *, durable: str = RUN_EVIDENCE_DURABLE, max_bytes: int = MAX_ENVELOPE_BYTES) -> None:
        self.connection = connection
        self.durable = durable
        self.max_bytes = min(MAX_ENVELOPE_BYTES, max(1024, max_bytes))

    async def messages(self, stop: asyncio.Event) -> AsyncIterator[Any]:
        try:
            subscription = await self.connection.jetstream().pull_subscribe(
                RUN_EVIDENCE_SUBJECT, durable=self.durable, stream=RUN_EVIDENCE_STREAM
            )
        except Exception as exc:
            raise RunEvidenceTransportError() from exc
        while not stop.is_set():
            try:
                messages = await subscription.fetch(1, timeout=0.5)
            except (NatsTimeoutError, TimeoutError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise RunEvidenceTransportError() from exc
            for message in messages:
                yield message

    async def consume(self, service: RunEvidenceIngestionService, stop: asyncio.Event) -> None:
        async for message in self.messages(stop):
            data = getattr(message, "data", b"")
            if not isinstance(data, bytes) or len(data) > self.max_bytes:
                if isinstance(data, bytes):
                    record_rejection = getattr(service, "record_rejection", None)
                    if callable(record_rejection):
                        await record_rejection(data[:MAX_ENVELOPE_BYTES], "payload_limit_exceeded")
                await _ack(message)
                continue
            try:
                await service.ingest(data)
            except RunEvidenceError:
                # Invalid, prohibited, duplicate, or colliding evidence is
                # terminal for this delivery.  It is safe to ACK because the
                # validator has already classified the bounded reason.
                await _ack(message)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # PostgreSQL/Garage outages remain retryable at the durable
                # worker boundary.  Do not ACK: JetStream redelivers it.
                raise
            await _ack(message)


async def _ack(message: Any) -> None:
    try:
        await asyncio.wait_for(message.ack(), timeout=2.0)
    except (TimeoutError, OSError):
        raise RunEvidenceTransportError("timeout") from None


async def connect_run_evidence_nats(url: str, *, user: str = "", password: str = "") -> Any:
    return await connect_nats(url, user=user, password=password)


__all__ = ["NatsRunEvidenceTransport", "RUN_EVIDENCE_DURABLE", "RUN_EVIDENCE_STREAM", "RUN_EVIDENCE_SUBJECT", "RunEvidenceTransportError", "connect_run_evidence_nats"]
