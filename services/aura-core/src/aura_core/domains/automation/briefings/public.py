"""Public owner-scoped scheduled briefing state and application ports.

The briefing domain owns durable schedule, occurrence and result state.  It
deliberately exposes opaque identifiers and value objects only; source
providers, the scheduler and the run executor compose through these ports.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BRIEFING_COMPONENT = "aura.automation.briefing_persistence"
BRIEFING_TOPIC = "aura.automation.briefings.v1"
BRIEFING_ID_NAMESPACE = UUID("dd22d0d0-0b7b-4ea0-9e2e-20ab1be3df5a")
MAX_PAGE_SIZE = 100


def briefing_phase_span_id(run_id: UUID | str, phase: str) -> str:
    """Return the stable metadata span identity for one briefing phase."""

    if not phase or len(phase) > 64 or not phase.replace("_", "").isalnum():
        raise BriefingValidationError("briefing span phase is invalid")
    if isinstance(run_id, UUID):
        run_key = str(run_id)
    elif len(run_id) == 32 and all(char in "0123456789abcdefABCDEF" for char in run_id):
        run_key = run_id.lower()
    else:
        raise BriefingValidationError("briefing trace identifier is invalid")
    return uuid5(BRIEFING_ID_NAMESPACE, f"briefing-phase:{run_key}:{phase}").hex[:16]


class BriefingError(RuntimeError):
    """Base class for content-free briefing failures."""


class BriefingValidationError(BriefingError, ValueError):
    pass


class BriefingNotFound(BriefingError):
    pass


class BriefingVersionConflict(BriefingError):
    pass


class BriefingIdempotencyConflict(BriefingError):
    pass


class BriefingTransitionError(BriefingError):
    pass


@dataclass(frozen=True, slots=True)
class BriefingCapabilities:
    """The bounded public execution capability exposed to Aura Web.

    This is intentionally a closed, metadata-only DTO.  It contains the
    canonical execution policy identity and the fixed limits needed by the
    existing schedule-create request, but no provider, model, prompt, tool,
    owner, or runtime state.
    """

    execution_policy_revision_id: UUID
    execution_policy_revision: str
    deadline_seconds: int
    max_output_tokens: int
    max_output_chars: int
    cadence_kind: str = "daily"
    memory_enabled: bool = False
    tools_enabled: bool = False

    def __post_init__(self) -> None:
        if self.cadence_kind != "daily":
            raise ValueError("briefing capabilities only support daily cadence")
        if self.memory_enabled or self.tools_enabled:
            raise ValueError("briefing capabilities cannot enable memory or tools")
        if self.deadline_seconds <= 0 or self.max_output_tokens <= 0 or self.max_output_chars <= 0:
            raise ValueError("briefing capability budgets must be positive")
        if not self.execution_policy_revision:
            raise ValueError("briefing execution policy revision is required")

    def as_payload(self) -> dict[str, object]:
        """Serialize the closed API resource without private runtime fields."""

        return {
            "executionPolicyRevisionId": str(self.execution_policy_revision_id),
            "executionPolicyRevision": self.execution_policy_revision,
            "deadlineSeconds": self.deadline_seconds,
            "maxOutputTokens": self.max_output_tokens,
            "maxOutputChars": self.max_output_chars,
            "cadenceKind": self.cadence_kind,
            "memoryEnabled": self.memory_enabled,
            "toolsEnabled": self.tools_enabled,
        }


class BriefingScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class BriefingOccurrenceStatus(StrEnum):
    SCHEDULED = "scheduled"
    ADMITTED = "admitted"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class BriefingResultStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    DELETED = "deleted"


class BriefingSourceCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class BriefingDeletionScope(StrEnum):
    SCHEDULE = "schedule"
    OCCURRENCE_HISTORY = "occurrence_history"
    RESULT_CONTENT = "result_content"


@dataclass(frozen=True, slots=True)
class BriefingCancellationLineage:
    """Stable occurrence/run identity passed to the Run Coordinator seam."""

    schedule_id: UUID
    occurrence_id: UUID
    run_id: UUID


class BriefingCancellationPort(Protocol):
    async def cancel_briefing(self, lineage: BriefingCancellationLineage) -> object: ...


class BriefingOutboxStagePort(Protocol):
    """Transaction-local wakeup staging port supplied by composition."""

    def stage_claim(self, transaction: object, occurrence: BriefingOccurrence) -> object: ...


class BriefingLifecycleEventPort(Protocol):
    """Transaction-local staging port for the closed lifecycle event facts."""

    def stage_event(
        self, transaction: object, payload: Mapping[str, object], aggregate_id: UUID
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class BriefingDailyCadence:
    local_time: time
    time_zone: str
    kind: str = "daily"

    def __post_init__(self) -> None:
        if self.kind != "daily":
            raise BriefingValidationError("briefing cadence must be daily")
        if self.local_time.tzinfo is not None:
            raise BriefingValidationError("briefing local time must not include an offset")
        try:
            ZoneInfo(self.time_zone)
        except ZoneInfoNotFoundError as exc:
            raise BriefingValidationError("timeZone must be a supported IANA timezone") from exc
        if self.local_time.second != 0 or self.local_time.microsecond != 0:
            raise BriefingValidationError("briefing localTime must use minute precision")

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> BriefingDailyCadence:
        raw_time = value.get("localTime", value.get("local_time"))
        zone = value.get("timeZone", value.get("time_zone"))
        if not isinstance(raw_time, str) or not isinstance(zone, str):
            raise BriefingValidationError("cadence localTime and timeZone are required")
        try:
            parsed = time.fromisoformat(raw_time)
        except ValueError as exc:
            raise BriefingValidationError("localTime must be HH:MM:00") from exc
        return cls(parsed, zone)

    @property
    def local_time_text(self) -> str:
        return self.local_time.strftime("%H:%M:%S")

    def as_payload(self) -> dict[str, object]:
        return {"kind": "daily", "localTime": self.local_time_text, "timeZone": self.time_zone}


@dataclass(frozen=True, slots=True)
class BriefingSourceGrants:
    include_reminders: bool
    calendar_ids: tuple[UUID, ...]
    document_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not self.include_reminders:
            raise BriefingValidationError("includeReminders must be true")
        if not self.calendar_ids:
            raise BriefingValidationError("at least one calendar is required")
        if len(self.calendar_ids) > 50 or len(self.document_ids) > 50:
            raise BriefingValidationError("briefing source selection is too large")
        if len(set(self.calendar_ids)) != len(self.calendar_ids) or len(
            set(self.document_ids)
        ) != len(self.document_ids):
            raise BriefingValidationError("briefing source identifiers must be unique")

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> BriefingSourceGrants:
        calendars = value.get("calendarIds", value.get("calendar_ids", ()))
        documents = value.get("documentIds", value.get("document_ids", ()))
        if not isinstance(calendars, Sequence) or isinstance(calendars, (str, bytes)):
            raise BriefingValidationError("calendarIds must be an array")
        if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
            raise BriefingValidationError("documentIds must be an array")
        calendars = cast(Sequence[object], calendars)
        documents = cast(Sequence[object], documents)
        try:
            return cls(
                bool(value.get("includeReminders", value.get("include_reminders", True))),
                tuple(UUID(str(item)) for item in calendars),
                tuple(UUID(str(item)) for item in documents),
            )
        except (ValueError, TypeError) as exc:
            raise BriefingValidationError("source identifiers must be UUIDs") from exc

    def as_payload(self) -> dict[str, object]:
        return {
            "includeReminders": self.include_reminders,
            "calendarIds": [str(item) for item in self.calendar_ids],
            "documentIds": [str(item) for item in self.document_ids],
        }


@dataclass(frozen=True, slots=True)
class BriefingExecutionPolicyReferences:
    execution_policy_revision_id: UUID
    prompt_bundle_revision_id: UUID
    model_policy_revision_id: UUID
    tool_policy_revision_id: UUID

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> BriefingExecutionPolicyReferences:
        try:
            return cls(
                UUID(
                    str(
                        value.get(
                            "executionPolicyRevisionId", value.get("execution_policy_revision_id")
                        )
                    )
                ),
                UUID(
                    str(value.get("promptBundleRevisionId", value.get("prompt_bundle_revision_id")))
                ),
                UUID(
                    str(value.get("modelPolicyRevisionId", value.get("model_policy_revision_id")))
                ),
                UUID(str(value.get("toolPolicyRevisionId", value.get("tool_policy_revision_id")))),
            )
        except (ValueError, TypeError) as exc:
            raise BriefingValidationError("all execution policy references are required") from exc

    def as_payload(self) -> dict[str, str]:
        return {
            "executionPolicyRevisionId": str(self.execution_policy_revision_id),
            "promptBundleRevisionId": str(self.prompt_bundle_revision_id),
            "modelPolicyRevisionId": str(self.model_policy_revision_id),
            "toolPolicyRevisionId": str(self.tool_policy_revision_id),
        }


@dataclass(frozen=True, slots=True)
class BriefingSchedule:
    id: UUID
    issuer: str
    subject: str
    display_name: str
    status: BriefingScheduleStatus
    version: int
    cadence: BriefingDailyCadence
    sources: BriefingSourceGrants
    agent_revision_id: UUID
    execution_policies: BriefingExecutionPolicyReferences
    next_due_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject


@dataclass(frozen=True, slots=True)
class BriefingOccurrence:
    id: UUID
    schedule_id: UUID
    issuer: str
    subject: str
    sequence: int
    status: BriefingOccurrenceStatus
    due_at: datetime
    due_local_date: date
    due_local_time: time
    time_zone: str
    source_completeness: BriefingSourceCompleteness
    run_id: UUID | None
    result_id: UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    version: int = 1

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject


@dataclass(frozen=True, slots=True)
class BriefingResult:
    id: UUID
    schedule_id: UUID
    occurrence_id: UUID
    issuer: str
    subject: str
    run_id: UUID | None
    status: BriefingResultStatus
    version: int
    source_completeness: BriefingSourceCompleteness
    content: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    original_status: BriefingResultStatus | None = None

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject


@dataclass(frozen=True, slots=True)
class BriefingSchedulePage:
    items: tuple[BriefingSchedule, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class BriefingOccurrencePage:
    items: tuple[BriefingOccurrence, ...]
    next_cursor: str | None = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int | slice):
        return self.items[index]


@dataclass(frozen=True, slots=True)
class BriefingDeletionReceipt:
    deletion_id: UUID
    scope: BriefingDeletionScope
    schedule_id: UUID
    occurrence_id: UUID | None
    result_id: UUID | None
    deleted_at: datetime

    def as_payload(self) -> dict[str, object]:
        return {
            "deletionId": str(self.deletion_id),
            "scope": self.scope.value,
            "scheduleId": str(self.schedule_id),
            "occurrenceId": str(self.occurrence_id) if self.occurrence_id else None,
            "resultId": str(self.result_id) if self.result_id else None,
            "deletedAt": self.deleted_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class BriefingCommandReceipt:
    idempotency_key: str
    fingerprint: str
    response: object


class BriefingRepository(Protocol):
    async def create_schedule(
        self,
        issuer: str,
        subject: str,
        *,
        display_name: str,
        cadence: BriefingDailyCadence,
        sources: BriefingSourceGrants,
        agent_revision_id: UUID,
        execution_policies: BriefingExecutionPolicyReferences,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> BriefingSchedule: ...

    async def list_schedules(
        self,
        issuer: str,
        subject: str,
        *,
        status: BriefingScheduleStatus | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingSchedulePage: ...

    async def get_schedule(
        self, issuer: str, subject: str, schedule_id: UUID
    ) -> BriefingSchedule: ...

    async def pause_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule: ...

    async def resume_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingSchedule: ...

    async def delete_schedule(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        cancel_run: BriefingCancellationPort | None = None,
    ) -> BriefingDeletionReceipt: ...

    async def list_occurrences(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        limit: int = 30,
        cursor: str | None = None,
    ) -> BriefingOccurrencePage: ...

    async def get_occurrence(
        self, issuer: str, subject: str, schedule_id: UUID, occurrence_id: UUID
    ) -> BriefingOccurrence: ...

    async def get_occurrence_by_id(self, occurrence_id: UUID) -> BriefingOccurrence: ...

    async def delete_history(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        *,
        expected_schedule_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt: ...

    async def get_result(self, issuer: str, subject: str, result_id: UUID) -> BriefingResult: ...

    async def delete_result_content(
        self,
        issuer: str,
        subject: str,
        result_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BriefingDeletionReceipt: ...

    async def claim_due_occurrences(
        self, now: datetime, *, limit: int = 20
    ) -> tuple[BriefingOccurrence, ...]: ...

    async def admit_occurrence(
        self,
        issuer: str,
        subject: str,
        schedule_id: UUID,
        occurrence_id: UUID,
        *,
        run_id: UUID,
        expected_version: int | None = None,
    ) -> BriefingOccurrence: ...

    async def create_result(self, result: BriefingResult) -> BriefingResult: ...

    async def update_result(self, result: BriefingResult) -> BriefingResult: ...

    async def recover_stale_claims(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> tuple[BriefingOccurrence, ...]: ...

    async def claim_due_occurrences_and_stage(
        self,
        now: datetime,
        *,
        limit: int = 20,
        stage: Callable[[object, BriefingOccurrence], object | Awaitable[object]],
    ) -> tuple[BriefingOccurrence, ...]: ...


def owner_key(issuer: str, subject: str) -> tuple[str, str]:
    if not issuer or not subject or "\x00" in issuer or "\x00" in subject:
        raise BriefingValidationError("authenticated owner is required")
    return issuer, subject


def occurrence_id(schedule_id: UUID, due_local_date: date) -> UUID:
    """Return the stable identity for one schedule-local calendar date."""

    return uuid5(BRIEFING_ID_NAMESPACE, f"occurrence:{schedule_id}:{due_local_date.isoformat()}")


def due_instant(local_date: date, local_time: time, time_zone: str) -> datetime:
    """Resolve a local daily wall time deterministically across DST changes."""

    try:
        zone = ZoneInfo(time_zone)
    except ZoneInfoNotFoundError as exc:
        raise BriefingValidationError("timeZone must be a supported IANA timezone") from exc
    naive = datetime.combine(local_date, local_time)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and candidate not in candidates:
            candidates.append(candidate)
    if candidates:
        return min(candidates).astimezone(UTC)
    # A spring-forward gap has no exact instant.  The contract moves it to the
    # first valid instant after the requested wall time.
    probe = naive
    for _ in range(24 * 60 + 1):
        probe += timedelta(minutes=1)
        for fold in (0, 1):
            candidate = probe.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == probe:
                return candidate.astimezone(UTC)
    raise BriefingValidationError("unable to resolve local daily due time")


def next_due(after: datetime, cadence: BriefingDailyCadence) -> tuple[date, datetime]:
    local = after.astimezone(ZoneInfo(cadence.time_zone))
    candidate_date = local.date()
    candidate = due_instant(candidate_date, cadence.local_time, cadence.time_zone)
    if candidate <= after.astimezone(UTC):
        candidate_date += timedelta(days=1)
        candidate = due_instant(candidate_date, cadence.local_time, cadence.time_zone)
    return candidate_date, candidate


def command_fingerprint(operation: str, payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _event_id(event_type: str, aggregate_id: UUID, version: int) -> UUID:
    return uuid5(BRIEFING_ID_NAMESPACE, f"event:{event_type}:{aggregate_id}:{version}")


def _event_base(event_type: str, event_id: UUID, occurred_at: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "eventId": str(event_id),
        "eventType": event_type,
        "occurredAt": occurred_at.isoformat(),
    }


def as_schedule_event(
    schedule: BriefingSchedule, event_type: str, *, event_id: UUID | None = None
) -> dict[str, object]:
    event_id = event_id or _event_id(event_type, schedule.id, schedule.version)
    return {
        **_event_base(event_type, event_id, schedule.updated_at),
        "scheduleId": str(schedule.id),
        "scheduleStatus": schedule.status.value,
        "scheduleVersion": schedule.version,
    }


def as_occurrence_event(
    occurrence: BriefingOccurrence, *, event_id: UUID | None = None
) -> dict[str, object]:
    event_type = f"briefing.occurrence.{occurrence.status.value}"
    event_id = event_id or _event_id(event_type, occurrence.id, occurrence.version)
    return {
        **_event_base(event_type, event_id, occurrence.updated_at),
        "scheduleId": str(occurrence.schedule_id),
        "occurrenceId": str(occurrence.id),
        "occurrenceStatus": occurrence.status.value,
        "occurrenceVersion": occurrence.version,
        "dueAt": occurrence.due_at.isoformat(),
        "sourceCompleteness": occurrence.source_completeness.value,
        "runId": str(occurrence.run_id) if occurrence.run_id else None,
        "resultId": str(occurrence.result_id) if occurrence.result_id else None,
    }


def as_result_event(result: BriefingResult, *, event_id: UUID | None = None) -> dict[str, object]:
    event_type = f"briefing.result.{result.status.value}"
    event_id = event_id or _event_id(event_type, result.id, result.version)
    return {
        **_event_base(event_type, event_id, result.updated_at),
        "scheduleId": str(result.schedule_id),
        "occurrenceId": str(result.occurrence_id),
        "resultId": str(result.id),
        "runId": str(result.run_id) if result.run_id else None,
        "resultStatus": result.status.value,
        "resultVersion": result.version,
        "sourceCompleteness": result.source_completeness.value,
    }


def as_deletion_event(
    receipt: BriefingDeletionReceipt, *, event_id: UUID | None = None
) -> dict[str, object]:
    event_type = (
        "briefing.history.deleted"
        if receipt.scope is BriefingDeletionScope.OCCURRENCE_HISTORY
        else "briefing.result-content.deleted"
    )
    event_id = event_id or _event_id(event_type, receipt.deletion_id, 1)
    return {
        **_event_base(event_type, event_id, receipt.deleted_at),
        "deletionId": str(receipt.deletion_id),
        "scope": receipt.scope.value,
        "scheduleId": str(receipt.schedule_id),
        "occurrenceId": str(receipt.occurrence_id) if receipt.occurrence_id else None,
        "resultId": str(receipt.result_id) if receipt.result_id else None,
    }


def as_schedule_payload(item: BriefingSchedule) -> dict[str, object]:
    return {
        "id": str(item.id),
        "displayName": item.display_name,
        "status": item.status.value,
        "version": item.version,
        "cadence": item.cadence.as_payload(),
        "sources": item.sources.as_payload(),
        "agentRevisionId": str(item.agent_revision_id),
        "executionPolicies": item.execution_policies.as_payload(),
        "nextDueAt": item.next_due_at.isoformat() if item.next_due_at else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "deletedAt": item.deleted_at.isoformat() if item.deleted_at else None,
    }


def as_occurrence_payload(item: BriefingOccurrence) -> dict[str, object]:
    return {
        "id": str(item.id),
        "scheduleId": str(item.schedule_id),
        "sequence": item.sequence,
        "status": item.status.value,
        "version": item.version,
        "dueAt": item.due_at.isoformat(),
        "dueLocalDate": item.due_local_date.isoformat(),
        "dueLocalTime": item.due_local_time.strftime("%H:%M:%S"),
        "timeZone": item.time_zone,
        "sourceCompleteness": item.source_completeness.value,
        "runId": str(item.run_id) if item.run_id else None,
        "resultId": str(item.result_id) if item.result_id else None,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "startedAt": item.started_at.isoformat() if item.started_at else None,
        "finishedAt": item.finished_at.isoformat() if item.finished_at else None,
    }


def as_result_payload(item: BriefingResult) -> dict[str, object]:
    return {
        "id": str(item.id),
        "scheduleId": str(item.schedule_id),
        "occurrenceId": str(item.occurrence_id),
        "runId": str(item.run_id) if item.run_id else None,
        "status": item.status.value,
        "version": item.version,
        "sourceCompleteness": item.source_completeness.value,
        "content": item.content,
        "createdAt": item.created_at.isoformat(),
        "updatedAt": item.updated_at.isoformat(),
        "deletedAt": item.deleted_at.isoformat() if item.deleted_at else None,
    }


class BriefingApplicationService:
    """Use-case facade used by HTTP, scheduler and worker entrypoints."""

    def __init__(self, repository: BriefingRepository) -> None:
        self.repository = repository
        self.cancellation_port: BriefingCancellationPort | None = None
        self.execution_application: object | None = None

    def set_cancellation_port(self, port: BriefingCancellationPort) -> None:
        self.cancellation_port = port

    def set_execution_application(self, application: object) -> None:
        """Bind the worker's execution application at the composition root."""
        self.execution_application = application

    async def get_capabilities(self) -> BriefingCapabilities:
        """Return the canonical, metadata-only briefing capability resource."""

        # Keep the fixed policy in the execution boundary.  The application
        # facade exposes only its deliberately closed public projection.
        from aura_core.domains.automation.briefings.execution import (
            canonical_briefing_capabilities,
        )

        return canonical_briefing_capabilities()

    async def process_command(self, command: object) -> object:
        """Process an identifier-only wake through the composed execution app."""
        if getattr(command, "aggregate_type", None) != "automation.briefing":
            raise BriefingValidationError("briefing command aggregate is invalid")
        aggregate_id = getattr(command, "aggregate_id", None)
        identifier = getattr(command, "identifier", None)
        if not callable(identifier):
            raise BriefingValidationError("briefing command identifiers are unavailable")
        identifier_fn = cast(Callable[[str], str | None], identifier)
        schedule_id = identifier_fn("scheduleId")
        occurrence_id_value = identifier_fn("occurrenceId")
        event_id = identifier_fn("eventId")
        try:
            if not isinstance(aggregate_id, UUID) or not schedule_id or not occurrence_id_value:
                raise ValueError
            UUID(str(schedule_id))
            UUID(str(occurrence_id_value))
            UUID(str(event_id))
        except (TypeError, ValueError) as error:
            raise BriefingValidationError("briefing command identifiers are invalid") from error
        application = self.execution_application
        method = getattr(application, "process_command", None)
        if not callable(method):
            raise BriefingTransitionError("briefing execution application is unavailable")
        result = method(command)
        if hasattr(result, "__await__"):
            return await cast(Awaitable[object], result)
        return result

    async def poll_due(self, *, limit: int = 100) -> object:
        claim = getattr(self.repository, "claim_due_occurrences", None)
        if not callable(claim):
            raise BriefingValidationError("briefing due-claim boundary is unavailable")
        claim_fn = cast(
            Callable[..., Awaitable[tuple[BriefingOccurrence, ...]]],
            claim,
        )
        items = await claim_fn(datetime.now(UTC), limit=limit)
        return type(
            "BriefingScheduleScan",
            (),
            {"claimed_count": len(items), "admitted_count": len(items)},
        )()

    async def recover_stale_claims(self, *, limit: int = 100) -> object:
        method = getattr(self.repository, "recover_stale_claims", None)
        if not callable(method):
            raise BriefingTransitionError("briefing recovery boundary is unavailable")
        result = method(limit=limit)
        if hasattr(result, "__await__"):
            return await cast(Awaitable[object], result)
        return result

    async def create(self, issuer: str, subject: str, **kwargs: object) -> BriefingSchedule:
        owner_key(issuer, subject)
        return await self.repository.create_schedule(issuer, subject, **kwargs)  # type: ignore[arg-type]

    async def list(self, issuer: str, subject: str, **kwargs: object) -> BriefingSchedulePage:
        owner_key(issuer, subject)
        return await self.repository.list_schedules(issuer, subject, **kwargs)  # type: ignore[arg-type]

    async def get(self, issuer: str, subject: str, schedule_id: UUID) -> BriefingSchedule:
        return await self.repository.get_schedule(issuer, subject, schedule_id)

    async def pause(
        self, issuer: str, subject: str, schedule_id: UUID, **kwargs: object
    ) -> BriefingSchedule:
        return await self.repository.pause_schedule(issuer, subject, schedule_id, **kwargs)  # type: ignore[arg-type]

    async def resume(
        self, issuer: str, subject: str, schedule_id: UUID, **kwargs: object
    ) -> BriefingSchedule:
        return await self.repository.resume_schedule(issuer, subject, schedule_id, **kwargs)  # type: ignore[arg-type]

    async def delete(
        self, issuer: str, subject: str, schedule_id: UUID, **kwargs: object
    ) -> BriefingDeletionReceipt:
        kwargs.setdefault("cancel_run", self.cancellation_port)
        return await self.repository.delete_schedule(issuer, subject, schedule_id, **kwargs)  # type: ignore[arg-type]

    async def occurrences(
        self, issuer: str, subject: str, schedule_id: UUID, **kwargs: object
    ) -> BriefingOccurrencePage:
        return await self.repository.list_occurrences(issuer, subject, schedule_id, **kwargs)  # type: ignore[arg-type]

    async def occurrence(
        self, issuer: str, subject: str, schedule_id: UUID, occurrence_id_value: UUID
    ) -> BriefingOccurrence:
        return await self.repository.get_occurrence(
            issuer, subject, schedule_id, occurrence_id_value
        )

    async def occurrence_by_id(self, occurrence_id_value: UUID) -> BriefingOccurrence:
        """Resolve an identifier-only worker wake through the public state port."""

        method = getattr(self.repository, "get_occurrence_by_id", None)
        if not callable(method):
            raise BriefingTransitionError("briefing occurrence wake boundary is unavailable")
        result = method(occurrence_id_value)
        if hasattr(result, "__await__"):
            return await cast(Awaitable[BriefingOccurrence], result)
        return cast(BriefingOccurrence, result)

    async def delete_occurrence_history(
        self, issuer: str, subject: str, schedule_id: UUID, **kwargs: object
    ) -> BriefingDeletionReceipt:
        return await self.repository.delete_history(issuer, subject, schedule_id, **kwargs)  # type: ignore[arg-type]

    async def result(self, issuer: str, subject: str, result_id: UUID) -> BriefingResult:
        return await self.repository.get_result(issuer, subject, result_id)

    async def delete_result(
        self, issuer: str, subject: str, result_id: UUID, **kwargs: object
    ) -> BriefingDeletionReceipt:
        return await self.repository.delete_result_content(issuer, subject, result_id, **kwargs)  # type: ignore[arg-type]
