"""Owner-authenticated scheduled briefing HTTP translation."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from __future__ import annotations

from datetime import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.automation.briefings.public import (
    BriefingApplicationService,
    BriefingDailyCadence,
    BriefingExecutionPolicyReferences,
    BriefingIdempotencyConflict,
    BriefingNotFound,
    BriefingScheduleStatus,
    BriefingSourceGrants,
    BriefingTransitionError,
    BriefingValidationError,
    BriefingVersionConflict,
    as_occurrence_payload,
    as_result_payload,
    as_schedule_payload,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(tags=["Briefings"])


class BriefingCadenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kind: str = "daily"
    local_time: str = Field(
        alias="localTime", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$"
    )
    time_zone: str = Field(alias="timeZone", min_length=1, max_length=128)

    def to_domain(self) -> BriefingDailyCadence:
        return BriefingDailyCadence(time.fromisoformat(self.local_time), self.time_zone, self.kind)


class BriefingSourcesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    include_reminders: bool = Field(alias="includeReminders")
    calendar_ids: list[UUID] = Field(alias="calendarIds", min_length=1, max_length=50)
    document_ids: list[UUID] = Field(alias="documentIds", max_length=50)

    def to_domain(self) -> BriefingSourceGrants:
        return BriefingSourceGrants(
            self.include_reminders, tuple(self.calendar_ids), tuple(self.document_ids)
        )


class BriefingPoliciesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    execution_policy_revision_id: UUID = Field(alias="executionPolicyRevisionId")
    prompt_bundle_revision_id: UUID = Field(alias="promptBundleRevisionId")
    model_policy_revision_id: UUID = Field(alias="modelPolicyRevisionId")
    tool_policy_revision_id: UUID = Field(alias="toolPolicyRevisionId")

    def to_domain(self) -> BriefingExecutionPolicyReferences:
        return BriefingExecutionPolicyReferences(
            self.execution_policy_revision_id,
            self.prompt_bundle_revision_id,
            self.model_policy_revision_id,
            self.tool_policy_revision_id,
        )


class CreateBriefingScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    cadence: BriefingCadenceRequest
    sources: BriefingSourcesRequest
    agent_revision_id: UUID = Field(alias="agentRevisionId")
    execution_policies: BriefingPoliciesRequest = Field(alias="executionPolicies")


class BriefingVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class DeleteScheduleRequest(BriefingVersionRequest):
    scope: Literal["schedule"]


class DeleteHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    scope: Literal["occurrence_history"]
    expected_schedule_version: int = Field(alias="expectedScheduleVersion", ge=1)


class DeleteResultRequest(BriefingVersionRequest):
    scope: Literal["result_content"]


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _service(request: Request) -> BriefingApplicationService:
    return _state(request).briefing_service


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, BriefingNotFound):
        return HTTPException(404, "briefing resource not found")
    if isinstance(
        exc, (BriefingVersionConflict, BriefingIdempotencyConflict, BriefingTransitionError)
    ):
        return HTTPException(409, str(exc))
    if isinstance(exc, (BriefingValidationError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "briefing operation failed")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/api/v1/briefing-schedules")
async def list_briefing_schedules(
    request: Request,
    response: Response,
    schedule_status: BriefingScheduleStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        page = await _service(request).list(
            session.principal.issuer,
            session.principal.subject,
            status=schedule_status,
            cursor=cursor,
            limit=limit,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [as_schedule_payload(x) for x in page.items], "nextCursor": page.next_cursor}


@router.get("/api/v1/briefing-capabilities")
async def get_briefing_capabilities(
    request: Request,
    response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    """Return the owner-authenticated, fixed briefing capability projection."""

    del session
    _no_store(response)
    try:
        capabilities = await _service(request).get_capabilities()
    except Exception as exc:
        raise _error(exc) from exc
    return capabilities.as_payload()


@router.post("/api/v1/briefing-schedules", status_code=status.HTTP_201_CREATED)
async def create_briefing_schedule(
    request: Request,
    response: Response,
    body: CreateBriefingScheduleRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).create(
            session.principal.issuer,
            session.principal.subject,
            display_name=body.display_name,
            cadence=body.cadence.to_domain(),
            sources=body.sources.to_domain(),
            agent_revision_id=body.agent_revision_id,
            execution_policies=body.execution_policies.to_domain(),
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_schedule_payload(item)


@router.get("/api/v1/briefing-schedules/{schedule_id}")
async def get_briefing_schedule(
    request: Request,
    response: Response,
    schedule_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).get(
            session.principal.issuer, session.principal.subject, schedule_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_schedule_payload(item)


async def _transition(
    request: Request,
    response: Response,
    schedule_id: UUID,
    body: BriefingVersionRequest,
    session: Session,
    idempotency_key: UUID,
    operation: str,
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await getattr(_service(request), operation)(
            session.principal.issuer,
            session.principal.subject,
            schedule_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_schedule_payload(item)


@router.post("/api/v1/briefing-schedules/{schedule_id}/pause")
async def pause_briefing_schedule(
    request: Request,
    response: Response,
    schedule_id: UUID,
    body: BriefingVersionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(
        request, response, schedule_id, body, session, idempotency_key, "pause"
    )


@router.post("/api/v1/briefing-schedules/{schedule_id}/resume")
async def resume_briefing_schedule(
    request: Request,
    response: Response,
    schedule_id: UUID,
    body: BriefingVersionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    return await _transition(
        request, response, schedule_id, body, session, idempotency_key, "resume"
    )


@router.post("/api/v1/briefing-schedules/{schedule_id}/deletions")
async def delete_briefing_schedule(
    request: Request,
    response: Response,
    schedule_id: UUID,
    body: DeleteScheduleRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).delete(
            session.principal.issuer,
            session.principal.subject,
            schedule_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return item.as_payload()


@router.get("/api/v1/briefing-schedules/{schedule_id}/occurrences")
async def list_briefing_occurrences(
    request: Request,
    response: Response,
    schedule_id: UUID,
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        page = await _service(request).occurrences(
            session.principal.issuer,
            session.principal.subject,
            schedule_id,
            cursor=cursor,
            limit=limit,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"items": [as_occurrence_payload(x) for x in page.items], "nextCursor": page.next_cursor}


@router.get("/api/v1/briefing-schedules/{schedule_id}/occurrences/{occurrence_id}")
async def get_briefing_occurrence(
    request: Request,
    response: Response,
    schedule_id: UUID,
    occurrence_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).occurrence(
            session.principal.issuer, session.principal.subject, schedule_id, occurrence_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_occurrence_payload(item)


@router.post("/api/v1/briefing-schedules/{schedule_id}/history-deletions")
async def delete_briefing_history(
    request: Request,
    response: Response,
    schedule_id: UUID,
    body: DeleteHistoryRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        receipt = await _service(request).delete_occurrence_history(
            session.principal.issuer,
            session.principal.subject,
            schedule_id,
            expected_schedule_version=body.expected_schedule_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return receipt.as_payload()


@router.get("/api/v1/briefing-results/{result_id}")
async def get_briefing_result(
    request: Request,
    response: Response,
    result_id: UUID,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).result(
            session.principal.issuer, session.principal.subject, result_id
        )
    except Exception as exc:
        raise _error(exc) from exc
    return as_result_payload(item)


@router.post("/api/v1/briefing-results/{result_id}/deletions")
async def delete_briefing_result(
    request: Request,
    response: Response,
    result_id: UUID,
    body: DeleteResultRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        receipt = await _service(request).delete_result(
            session.principal.issuer,
            session.principal.subject,
            result_id,
            expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return receipt.as_payload()
