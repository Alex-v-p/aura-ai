"""Bounded recovery and cancellation composition for briefing runs."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

from aura_core.platform.telemetry import MetadataMetrics, new_span_id

MAX_RETRY_ATTEMPTS = 5
RETRY_DELAYS_SECONDS = (1.0, 5.0, 30.0, 120.0, 600.0)
BRIEFING_CANCELLATION_COMPONENT = "aura.automation.briefing_cancellation"


class BriefingRecoveryApplication(Protocol):
    async def recover_stale_claims(self, *, limit: int) -> object: ...


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay_seconds: float
    attempt: int


def retry_decision(attempt: int, *, retryable: bool = True) -> RetryDecision:
    """Return a bounded retry decision; attempts are one-based."""

    if type(attempt) is not int or attempt < 1:
        raise ValueError("retry attempt must be positive")
    bounded = min(attempt, MAX_RETRY_ATTEMPTS)
    return RetryDecision(
        retry=retryable and attempt <= MAX_RETRY_ATTEMPTS,
        delay_seconds=RETRY_DELAYS_SECONDS[bounded - 1],
        attempt=bounded,
    )


class BriefingRecovery:
    """Reconcile stale claims and route cancellation through RunCoordinator."""

    def __init__(
        self,
        application: BriefingRecoveryApplication,
        run_coordinator: object | None = None,
        *,
        claim_limit: int = 100,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        if not 1 <= claim_limit <= 100:
            raise ValueError("briefing recovery claim limit must be between 1 and 100")
        self.application = application
        self.run_coordinator = run_coordinator
        self.claim_limit = claim_limit
        self.metrics = metrics

    async def recover_once(self) -> object:
        method = getattr(self.application, "recover_stale_claims", None)
        if not callable(method):
            method = getattr(self.application, "recover", None)
        if not callable(method):
            raise TypeError("briefing recovery application is missing stale-claim recovery")
        result = method(limit=self.claim_limit)
        return await result if inspect.isawaitable(result) else result

    async def cancel_run(
        self,
        run_id: UUID,
        *,
        issuer: str,
        subject: str,
        idempotency_key: str,
    ) -> object:
        if self.run_coordinator is None:
            raise RuntimeError("briefing run cancellation boundary is unavailable")
        cancel = getattr(self.run_coordinator, "cancel", None)
        if not callable(cancel):
            raise RuntimeError("briefing run cancellation boundary is unavailable")
        started = monotonic()
        trace_id = run_id.hex
        span_id = new_span_id()
        try:
            result = cancel(run_id, subject, issuer, idempotency_key)
            value = await result if inspect.isawaitable(result) else result
        except Exception:
            if self.metrics is not None:
                self.metrics.record_span(
                    BRIEFING_CANCELLATION_COMPONENT,
                    "briefing.cancel",
                    (monotonic() - started) * 1000,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=None,
                    dependency="postgresql",
                    outcome="error",
                    error_class="cancellation",
                    run_id=str(run_id),
                )
            raise
        if self.metrics is not None:
            self.metrics.record_span(
                BRIEFING_CANCELLATION_COMPONENT,
                "briefing.cancel",
                (monotonic() - started) * 1000,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                dependency="postgresql",
                outcome="ok",
                run_id=str(run_id),
            )
        return value


__all__ = [
    "BriefingRecovery",
    "BriefingRecoveryApplication",
    "BRIEFING_CANCELLATION_COMPONENT",
    "MAX_RETRY_ATTEMPTS",
    "RETRY_DELAYS_SECONDS",
    "RetryDecision",
    "retry_decision",
]
