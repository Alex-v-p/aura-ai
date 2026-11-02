"""NATS provider adapter for the application-owned runner transport contract."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import nats
from nats.errors import TimeoutError as NatsTimeoutError

from aura_observatory.evaluation_orchestration import (
    TransportValidationError,
    canonical_digest,
    decode_envelope,
    encode_envelope,
    validate_runner_data,
    validate_transport_envelope,
)
from aura_observatory.ports import EvaluationDispatchIntent, EvaluationResultDelivery

REQUEST_SUBJECT = "aura.evaluation.runner.requested.v1"
CANCELLATION_SUBJECT = "aura.evaluation.runner.cancellation.v1"
RESULT_SUBJECT = "aura.evaluation.runner.results.v1"
EVALUATION_STREAM = "AURA_EVALUATION_RUNNER"
REQUEST_DURABLE = "aura-evaluation-dispatch"
RESULT_DURABLE = "aura-evaluation-dispatch"
MAX_ENVELOPE_BYTES = 1_000_000
CONNECT_TIMEOUT_SECONDS = 3.0
PUBLISH_TIMEOUT_SECONDS = 2.0
FETCH_TIMEOUT_SECONDS = 0.75
ACK_TIMEOUT_SECONDS = 2.0

_LOGGER = logging.getLogger(__name__)


class NatsTransportError(RuntimeError):
    """Provider failure with a finite, content-free public code."""

    def __init__(self, code: str = "transport_unavailable") -> None:
        self.code = (
            code if code in {"transport_unavailable", "timeout"} else "transport_unavailable"
        )
        super().__init__(self.code)


def _provider_available(connection: Any) -> bool:
    try:
        closed = getattr(connection, "is_closed", False)
        connected = getattr(connection, "is_connected", True)
        closed = closed() if callable(closed) else closed
        connected = connected() if callable(connected) else connected
        return not bool(closed) and bool(connected)
    except Exception:
        return False


async def _safe_error_callback(_error: object) -> None:
    """Replace the NATS client's raw exception/URL logging callback."""

    _LOGGER.warning("evaluation NATS provider error", extra={"code": "transport_unavailable"})


async def _safe_disconnected_callback() -> None:
    _LOGGER.info("evaluation NATS provider disconnected", extra={"code": "transport_unavailable"})


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TransportValidationError("invalid_identifier")
    try:
        return str(UUID(value))
    except ValueError, AttributeError:
        raise TransportValidationError("invalid_identifier") from None


class NatsEvaluationTransport:
    """Authenticated JetStream adapter used only by the evaluation worker."""

    def __init__(
        self,
        connection: Any,
        *,
        result_durable: str = RESULT_DURABLE,
        max_bytes: int = MAX_ENVELOPE_BYTES,
    ) -> None:
        self._connection = connection
        self._result_durable = result_durable
        self._max_bytes = max_bytes

    async def publish(self, intent: EvaluationDispatchIntent, event_id: str) -> None:
        subject = {
            "request": REQUEST_SUBJECT,
            "cancellation": CANCELLATION_SUBJECT,
        }.get(intent.kind)
        if subject is None:
            raise TransportValidationError("unsupported_intent")
        payload = encode_envelope(intent.envelope, max_bytes=self._max_bytes)
        if not _provider_available(self._connection):
            raise NatsTransportError()
        try:
            await asyncio.wait_for(
                self._connection.jetstream().publish(
                    subject, payload, headers={"Nats-Msg-Id": _uuid(event_id)}
                ),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError, TransportValidationError:
            raise
        except TimeoutError:
            raise NatsTransportError("timeout") from None
        except Exception:
            raise NatsTransportError() from None

    async def results(self, stop: asyncio.Event) -> AsyncIterator[EvaluationResultDelivery]:
        if not _provider_available(self._connection):
            raise NatsTransportError()
        try:
            subscription = await asyncio.wait_for(
                self._connection.jetstream().pull_subscribe(
                    RESULT_SUBJECT,
                    durable=self._result_durable,
                    stream=EVALUATION_STREAM,
                ),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise NatsTransportError("timeout") from None
        except Exception:
            raise NatsTransportError() from None
        while not stop.is_set():
            try:
                messages = await asyncio.wait_for(
                    subscription.fetch(1, timeout=0.5), timeout=FETCH_TIMEOUT_SECONDS
                )
            except NatsTimeoutError, TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                raise NatsTransportError() from None
            for message in messages:
                try:
                    envelope = decode_envelope(message.data, max_bytes=self._max_bytes)
                except TransportValidationError:
                    await self._ack_message(message)
                    continue
                yield EvaluationResultDelivery(
                    envelope=envelope,
                    acknowledge=lambda message=message: self._ack_message(message),
                )

    @staticmethod
    async def _ack_message(message: Any) -> None:
        try:
            await asyncio.wait_for(message.ack(), timeout=ACK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise NatsTransportError("timeout") from None
        except Exception:
            raise NatsTransportError() from None


async def connect_nats(url: str, *, user: str = "", password: str = "") -> Any:
    options: dict[str, object] = {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "reconnect_time_wait": 1,
        "max_reconnect_attempts": 3,
        "flush_timeout": PUBLISH_TIMEOUT_SECONDS,
        "error_cb": _safe_error_callback,
        "disconnected_cb": _safe_disconnected_callback,
    }
    if user:
        options["user"] = user
        options["password"] = password
    connect: Any = getattr(nats, "connect")  # noqa: B009
    try:
        return await asyncio.wait_for(connect(url, **options), timeout=CONNECT_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise NatsTransportError("timeout") from None
    except Exception:
        raise NatsTransportError() from None


def new_message_id() -> str:
    return str(uuid4())


__all__ = [
    "CANCELLATION_SUBJECT",
    "ACK_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "EVALUATION_STREAM",
    "MAX_ENVELOPE_BYTES",
    "NatsEvaluationTransport",
    "NatsTransportError",
    "PUBLISH_TIMEOUT_SECONDS",
    "REQUEST_DURABLE",
    "REQUEST_SUBJECT",
    "RESULT_DURABLE",
    "RESULT_SUBJECT",
    "TransportValidationError",
    "canonical_digest",
    "connect_nats",
    "decode_envelope",
    "encode_envelope",
    "new_message_id",
    "validate_runner_data",
    "validate_transport_envelope",
]
