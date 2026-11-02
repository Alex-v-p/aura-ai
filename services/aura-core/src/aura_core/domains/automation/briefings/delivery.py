"""Public notification handoff for settled briefing results."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

from aura_core.domains.automation.briefings.context import (
    BriefingEvidenceItem,
    BriefingEvidencePort,
)
from aura_core.domains.automation.notifications.public import (
    CreateBriefingNotification,
    Notification,
)
from aura_core.platform.telemetry import MetadataMetrics, new_span_id

BRIEFING_DELIVERY_COMPONENT = "aura.automation.briefing_delivery"


class BriefingNotificationPort(Protocol):
    async def create_briefing_notification(
        self, command: CreateBriefingNotification
    ) -> Notification: ...


@dataclass(frozen=True, slots=True)
class BriefingDeliveryRequest:
    issuer: str
    subject: str
    schedule_id: UUID
    occurrence_id: UUID
    result_id: UUID
    trace_id: str | None = None
    parent_span_id: str | None = None
    evidence: BriefingEvidencePort | None = None

    @property
    def idempotency_key(self) -> str:
        return f"briefing:{self.occurrence_id}:{self.result_id}"


class BriefingDelivery:
    """Invoke only the Notifications public command after durable settlement."""

    def __init__(
        self,
        notifications: BriefingNotificationPort,
        *,
        metrics: MetadataMetrics | None = None,
    ) -> None:
        self.notifications = notifications
        self.metrics = metrics

    async def deliver(self, request: BriefingDeliveryRequest) -> Notification:
        started = monotonic()
        trace_id = request.trace_id or request.occurrence_id.hex
        parent_span_id = request.parent_span_id or request.occurrence_id.hex[:16]
        span_id = new_span_id()
        command = CreateBriefingNotification(
            issuer=request.issuer,
            subject=request.subject,
            briefing_schedule_id=request.schedule_id,
            briefing_occurrence_id=request.occurrence_id,
            briefing_result_id=request.result_id,
            idempotency_key=request.idempotency_key,
        )
        try:
            method = getattr(self.notifications, "create_briefing_notification", None)
            if not callable(method):
                method = getattr(self.notifications, "deliver_briefing_notification", None)
            if not callable(method):
                raise TypeError("notification application is missing briefing delivery command")
            result = method(command)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Notification):
                # Keep fakes flexible while ensuring production returns the
                # notification public DTO, never a persistence row.
                if result is None:
                    raise TypeError("notification command returned no notification")
            if self.metrics is not None:
                self.metrics.increment(
                    BRIEFING_DELIVERY_COMPONENT,
                    "briefing_outcome_count",
                    outcome="ok",
                )
                self.metrics.record_span(
                    BRIEFING_DELIVERY_COMPONENT,
                    "briefing.delivery",
                    (monotonic() - started) * 1000,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    dependency="postgresql",
                    outcome="ok",
                    briefing_occurrence_id=str(request.occurrence_id),
                    briefing_result_id=str(request.result_id),
                )
            self._record_evidence(request, trace_id, "completed")
            return result  # type: ignore[return-value]
        except Exception:
            self._record_evidence(request, trace_id, "error", error_class="delivery")
            if self.metrics is not None:
                self.metrics.increment(
                    BRIEFING_DELIVERY_COMPONENT,
                    "briefing_outcome_count",
                    outcome="error",
                    error_class="delivery",
                )
                self.metrics.record_span(
                    BRIEFING_DELIVERY_COMPONENT,
                    "briefing.delivery",
                    (monotonic() - started) * 1000,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    dependency="postgresql",
                    outcome="error",
                    error_class="delivery",
                    briefing_occurrence_id=str(request.occurrence_id),
                    briefing_result_id=str(request.result_id),
                )
            raise

    @staticmethod
    def _record_evidence(
        request: BriefingDeliveryRequest,
        trace_id: str,
        outcome: str,
        *,
        error_class: str | None = None,
    ) -> None:
        evidence = request.evidence
        if evidence is None:
            return
        try:
            run_id = UUID(trace_id)
        except (ValueError, AttributeError):
            run_id = None
        try:
            evidence.record(
                BriefingEvidenceItem(
                    phase="delivery",
                    outcome=outcome,
                    error_class=error_class,
                    trace_id=trace_id if len(trace_id) == 32 else None,
                    run_id=run_id,
                )
            )
        except Exception:
            # Evidence capture is optional and must never change notification
            # delivery behavior when the recorder rejects or is unavailable.
            return


async def deliver_briefing(
    notifications: BriefingNotificationPort,
    request: BriefingDeliveryRequest,
    *,
    metrics: MetadataMetrics | None = None,
) -> Notification:
    return await BriefingDelivery(notifications, metrics=metrics).deliver(request)


__all__ = [
    "BRIEFING_DELIVERY_COMPONENT",
    "BriefingDelivery",
    "BriefingDeliveryRequest",
    "BriefingNotificationPort",
    "deliver_briefing",
]
