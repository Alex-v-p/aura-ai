# ruff: noqa: B008, E501

"""Thin owner-authorized HTTP translation for governed research."""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.knowledge.research.public import (
    ResearchApplicationService,
    ResearchBudget,
    ResearchConflict,
    ResearchMode,
    ResearchNotFound,
    ResearchOutcome,
    ResearchTask,
    ResearchValidationError,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/research", tags=["Research"])


class BudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    wall_time_seconds: int = Field(alias="wallTimeSeconds", ge=1, le=3600)
    fetched_sources: int = Field(alias="fetchedSources", ge=1, le=60)
    search_requests: int = Field(alias="searchRequests", ge=1, le=20)
    browser_pages: int = Field(alias="browserPages", ge=0, le=15)
    model_calls: int = Field(alias="modelCalls", ge=1, le=32)

    def value(self) -> ResearchBudget:
        return ResearchBudget(self.wall_time_seconds, self.fetched_sources, self.search_requests, self.browser_pages, self.model_calls)


class CreateResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    question: str = Field(min_length=1, max_length=10_000)
    mode: ResearchMode
    budget: BudgetBody | None = None
    seed_urls: list[str] = Field(default_factory=list, alias="seedUrls", max_length=20)
    domains: list[str] = Field(default_factory=list, max_length=20)


class VersionedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class ExtendResearchRequest(VersionedCommand):
    cumulative_budget: BudgetBody = Field(alias="cumulativeBudget")


class DeleteResearchRequest(VersionedCommand):
    delete_linked_artifacts: bool = Field(default=False, alias="deleteLinkedArtifacts")


class ResearchSettingsPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    mode: ResearchMode
    defaults: BudgetBody


class ReplaceResearchSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)
    presets: list[ResearchSettingsPreset] = Field(min_length=4, max_length=4)


def _service(request: Request) -> ResearchApplicationService:
    service = getattr(request.app.state.aura, "research_service", None)
    if not isinstance(service, ResearchApplicationService):
        raise HTTPException(503, "research service is unavailable")
    return service


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _summary(service: ResearchApplicationService, task: ResearchTask) -> dict[str, object]:
    return service.summary(task)


def _service_payload(service: Any, task: ResearchTask | None = None, *, detail: bool) -> dict[str, object]:
    if task is None:
        return ResearchApplicationService.serialize(service, detail=detail)
    return service.detail(task) if detail else service.summary(task)


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, ResearchNotFound):
        raise HTTPException(404, "research task not found") from exc
    if isinstance(exc, ResearchConflict):
        raise HTTPException(409, "research task state conflict") from exc
    if isinstance(exc, ResearchValidationError):
        raise HTTPException(422, "invalid research request") from exc
    raise HTTPException(503, "research operation failed") from exc


@router.get("")
async def list_research(request: Request, response: Response, mode: ResearchMode | None = None, outcome: ResearchOutcome | None = None, cursor: str | None = Query(default=None, max_length=1024), limit: int = Query(default=25, ge=1, le=100), session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    try:
        values = await _service(request).list(*_owner(session), mode=mode, outcome=outcome, limit=limit + 1, offset=offset)
    except Exception as exc:
        _raise(exc)
    service = _service(request)
    return {"items": [_summary(service, item) for item in values[:limit]], "nextCursor": str(offset + limit) if len(values) > limit else None}


@router.post("", status_code=202)
async def create_research(request: Request, body: CreateResearchRequest, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        admission = await _service(request).admit(*_owner(session), question=body.question, mode=body.mode, budget=body.budget.value() if body.budget else None, seed_urls=body.seed_urls, domains=body.domains, idempotency_key=idempotency_key)
        return admission.as_dict()
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/settings")
async def get_research_settings(request: Request, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        return ResearchApplicationService.settings_payload(await _service(request).get_settings(*_owner(session)))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.put("/settings")
async def replace_research_settings(request: Request, body: ReplaceResearchSettingsRequest, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        defaults = {preset.mode: preset.defaults.value() for preset in body.presets}
        if len(defaults) != len(body.presets):
            raise ResearchValidationError("research settings contain duplicate modes")
        return ResearchApplicationService.settings_payload(await _service(request).replace_settings(*_owner(session), expected_version=body.expected_version, defaults=defaults, idempotency_key=idempotency_key))
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/{research_id}")
async def get_research(request: Request, research_id: UUID, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.get(*_owner(session), research_id), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/{research_id}/evidence")
async def list_evidence(request: Request, research_id: UUID, response: Response, cursor: str | None = Query(default=None, max_length=1024), limit: int = Query(default=25, ge=1, le=100), session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    try:
        values = await _service(request).evidence(*_owner(session), research_id, limit=limit + 1, offset=offset)
    except Exception as exc:
        _raise(exc)
    return {"items": [{"sourceId": str(item.source_id), "label": item.label, "url": item.url, "publisher": item.publisher, "retrievedAt": item.retrieved_at.isoformat(), "mediaType": item.media_type, "extractionMethod": item.extraction_method, "freshnessAt": item.freshness_at.isoformat() if item.freshness_at else None, "locators": [item.locator] if item.locator else []} for item in values[:limit]], "nextCursor": str(offset + limit) if len(values) > limit else None}


@router.post("/{research_id}/cancel", status_code=202)
async def cancel_research(request: Request, research_id: UUID, body: VersionedCommand, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.cancel(*_owner(session), research_id, expected_version=body.expected_version, idempotency_key=idempotency_key), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{research_id}/extensions")
async def extend_research(request: Request, research_id: UUID, body: ExtendResearchRequest, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.extend(*_owner(session), research_id, expected_version=body.expected_version, cumulative_budget=body.cumulative_budget.value(), idempotency_key=idempotency_key), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{research_id}/deletions", status_code=202)
async def delete_research(request: Request, research_id: UUID, body: DeleteResearchRequest, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.delete(*_owner(session), research_id, expected_version=body.expected_version, delete_linked_artifacts=body.delete_linked_artifacts, idempotency_key=idempotency_key), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{research_id}/deletions/retry", status_code=202)
async def retry_cleanup(request: Request, research_id: UUID, body: VersionedCommand, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.retry_cleanup(*_owner(session), research_id, expected_version=body.expected_version, idempotency_key=idempotency_key), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{research_id}/deletions/reconcile", status_code=202)
async def reconcile_cleanup(request: Request, research_id: UUID, body: VersionedCommand, response: Response, idempotency_key: str = Header(..., alias="Idempotency-Key"), session: Session = Depends(require_csrf)) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        return _service_payload(service, await service.reconcile_cleanup(*_owner(session), research_id, expected_version=body.expected_version, idempotency_key=idempotency_key), detail=True)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


__all__ = ["router"]
