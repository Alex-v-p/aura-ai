"""Owner-authenticated HTTP translation for bounded manual workflows."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008, E501

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.bootstrap.workflow_uow import (
    AdmissionGatedWorkflowApplication,
    WorkflowAdmissionUnavailable,
    WorkflowLifecycleCursorExpired,
    WorkflowLifecycleCursorNotFound,
)
from aura_core.domains.automation.workflows.public import (
    AgentRunStep,
    InputField,
    OwnerApprovalStep,
    OwnerNotificationStep,
    Selector,
    WaitUntilStep,
    WorkflowAuthoritySnapshot,
    WorkflowCreateDraftCommand,
    WorkflowDeletionScope,
    WorkflowIdempotencyConflict,
    WorkflowNotFound,
    WorkflowRevision,
    WorkflowStep,
    WorkflowStepKind,
    WorkflowTraceContext,
    WorkflowTransitionError,
    WorkflowValidationError,
    WorkflowValueType,
    WorkflowVersionConflict,
)
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/workflows", tags=["Workflows"])


class WorkflowCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    revision_id: UUID = Field(alias="revisionId")
    input: dict[str, str] = Field(default_factory=dict)


class ResolveWorkflowRequest(WorkflowCommand):
    action: str
    step_execution_id: UUID = Field(alias="stepExecutionId")


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    display_name: str = Field(alias="displayName", min_length=1, max_length=160)
    input_fields: list[dict[str, str]] = Field(alias="inputFields", max_length=16)
    steps: list[dict[str, object]] = Field(min_length=1, max_length=8)


class CloneWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    revision_id: UUID = Field(alias="revisionId")
    expected_definition_version: int = Field(alias="expectedDefinitionVersion", ge=1)


class UpdateWorkflowRequest(CreateWorkflowRequest):
    expected_version: int = Field(alias="expectedVersion", ge=1)


class DeleteWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    scope: str
    expected_version: int = Field(alias="expectedVersion", ge=1)


def _state(request: Request) -> AppState:
    return cast(AppState, request.app.state.aura)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _json_safe(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_safe(item) for key, item in mapping.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in cast(Sequence[object], value)]
    return value


def _camel(value: object) -> object:
    rendered = _json_safe(value)
    if isinstance(rendered, Mapping):
        mapping = cast(Mapping[object, object], rendered)
        result: dict[str, object] = {}
        for raw_key, item in mapping.items():
            key = str(raw_key)
            parts = key.split("_")
            result[parts[0] + "".join(part.title() for part in parts[1:])] = _camel(item)
        return result
    if isinstance(rendered, list):
        return [_camel(item) for item in cast(list[object], rendered)]
    return rendered


def _public(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        source = cast(dict[str, object], value)
    elif callable(getattr(value, "as_dict", None)):
        source = cast(dict[str, object], cast(Any, value).as_dict())
    else:
        source = {
            name: getattr(value, name)
            for name in dir(value)
            if not name.startswith("_") and name in {
                "id", "workflow_id", "revision_id", "status", "version", "current_ordinal",
                "terminal_outcome", "admitted_at", "deadline", "input_values", "replay_count",
                "valid", "issues", "validated_version", "display_name",
                "latest_revision_id", "created_at", "updated_at", "retired_at", "revision_number",
                "published_at", "limits", "input_fields", "steps", "workflow_revision_id",
                "terminal_at", "completed_effects", "checkpoints", "result",
            }
        }
    source.pop("issuer", None)
    source.pop("subject", None)
    return cast(dict[str, object], _camel(source))


def _workflow_fields(source: Mapping[str, object]) -> list[dict[str, object]]:
    """Normalize Core's value-type spelling to the public workflow contract."""

    fields: list[dict[str, object]] = []
    for raw in cast(Sequence[object], source.get("inputFields", [])):
        if not isinstance(raw, Mapping):
            continue
        raw_mapping = cast(Mapping[object, object], raw)
        field: dict[str, object] = {
            str(key): item for key, item in raw_mapping.items()
        }
        if "type" in field and "valueType" not in field:
            field["valueType"] = field.pop("type")
        fields.append(field)
    return fields


def _draft_public(value: object) -> dict[str, object]:
    source = _public(value)
    return {
        key: source[key]
        for key in (
            "id", "workflowId", "displayName", "inputFields", "steps", "version",
            "createdAt", "updatedAt",
        )
        if key in source
    } | {
        "inputFields": _workflow_fields(source),
        "createdAt": source.get("createdAt"),
        "updatedAt": source.get("updatedAt"),
    }


def _revision_public(value: object) -> dict[str, object]:
    source = _public(value)
    return {
        key: source[key]
        for key in (
            "id", "workflowId", "revision", "displayName", "inputFields", "steps",
            "limits", "status", "version", "publishedAt", "retiredAt",
        )
        if key in source
    } | {"inputFields": _workflow_fields(source)}


def _step_state_public(value: Mapping[str, object]) -> dict[str, object]:
    """Map the domain's closed step snapshot to the OpenAPI step state."""

    # Domain snapshots deliberately use private/Python spellings.  The HTTP
    # envelope is assembled here so no runtime payload can leak across the
    # boundary and the response remains an exact closed contract shape.
    capabilities = value.get("recoveryCapabilities", value.get("recovery_capabilities", ()))
    if not isinstance(capabilities, (tuple, list)):
        capabilities = ()
    result: dict[str, object] = {
        "ordinal": value.get("ordinal"),
        "kind": value.get("kind"),
        "status": value.get("status"),
        "effectOutcome": value.get("effectOutcome", value.get("effect_outcome")),
        "recoveryCapabilities": list(cast(Sequence[object], capabilities)),
        "replayUsed": bool(value.get("replayUsed", value.get("replay_used", False))),
        "result": value.get("result", value.get("output")),
        "startedAt": value.get("startedAt", value.get("started_at")),
        "settledAt": value.get("settledAt", value.get("settled_at")),
    }
    execution_id = value.get(
        "stepExecutionId", value.get("execution_id", value.get("executionId"))
    )
    if execution_id is not None:
        result["stepExecutionId"] = execution_id
    return cast(dict[str, object], _camel(result))


def _instance_summary_public(value: object) -> dict[str, object]:
    source = _public(value)
    admitted = source.get("admittedAt")
    updated = source.get("updatedAt") or admitted
    return {
        "id": source.get("id"),
        "workflowId": source.get("workflowId"),
        "revisionId": source.get("revisionId"),
        "status": source.get("status"),
        "terminalOutcome": source.get("terminalOutcome"),
        "currentStepOrdinal": source.get(
            "currentStepOrdinal", source.get("currentOrdinal")
        ),
        "completedEffects": _completed_effects_public(source),
        "version": source.get("version"),
        "admittedAt": admitted,
        "deadlineAt": source.get("deadlineAt", source.get("deadline")),
        "updatedAt": updated,
        "terminalAt": source.get("terminalAt"),
    }


def _instance_public(value: object) -> dict[str, object]:
    source = _public(value)
    steps_raw = source.get("steps", [])
    steps = [
        _step_state_public(cast(Mapping[str, object], step))
        for step in cast(Sequence[object], steps_raw)
        if isinstance(step, Mapping)
    ]
    admitted = source.get("admittedAt")
    return {
        "id": source.get("id"),
        "workflowId": source.get("workflowId"),
        "revisionId": source.get("revisionId"),
        "input": source.get("input", source.get("inputValues", {})),
        "status": source.get("status"),
        "terminalOutcome": source.get("terminalOutcome"),
        "currentStepOrdinal": source.get(
            "currentStepOrdinal", source.get("currentOrdinal")
        ),
        "steps": steps,
        "completedEffects": _completed_effects_public(source),
        "checkpoints": source.get("checkpoints", []),
        "result": source.get("result"),
        "version": source.get("version"),
        "admittedAt": admitted,
        "deadlineAt": source.get("deadlineAt", source.get("deadline")),
        "updatedAt": source.get("updatedAt") or admitted,
        "terminalAt": source.get("terminalAt"),
    }


def _completed_effects_public(source: Mapping[str, object]) -> list[dict[str, object]]:
    raw = source.get("completedEffects", [])
    if not isinstance(raw, (tuple, list)):
        return []
    steps = source.get("steps", [])
    step_kinds: dict[object, object] = {}
    if isinstance(steps, (tuple, list)):
        for raw_step in cast(Sequence[object], steps):
            step = raw_step
            if not isinstance(step, Mapping):
                continue
            step_mapping = cast(Mapping[object, object], step)
            ordinal = step_mapping.get("ordinal")
            if ordinal is not None:
                step_kinds[ordinal] = step_mapping.get("kind")
    result: list[dict[str, object]] = []
    for raw_item in cast(Sequence[object], raw):
        item = raw_item
        if not isinstance(item, Mapping):
            continue
        mapping = cast(Mapping[object, object], item)
        ordinal = mapping.get("stepOrdinal", mapping.get("step_ordinal"))
        kind = mapping.get("stepKind", mapping.get("step_kind")) or step_kinds.get(ordinal)
        result.append({"stepOrdinal": ordinal, "stepKind": kind})
    return result


def _definition_public(value: object) -> dict[str, object]:
    source = _public(value)
    return {
        key: source[key]
        for key in (
            "id", "displayName", "status", "latestRevisionId", "version",
            "createdAt", "updatedAt", "retiredAt",
        )
        if key in source
    }


def _validation_public(value: object) -> dict[str, object]:
    source = _public(value)
    issues: list[object] = []
    for issue in cast(Sequence[object], source.get("issues", [])):
        if isinstance(issue, Mapping):
            issues.append(dict(cast(Mapping[object, object], issue)))
        else:
            issues.append({"code": "invalid_value", "path": "/"})
    return {
        "valid": bool(source.get("valid", False)),
        "issues": issues,
        "validatedVersion": source.get("validatedVersion", 1),
    }


def _event_public(value: Mapping[str, object]) -> dict[str, object]:
    """Keep SSE output to the closed metadata-only lifecycle envelope."""
    # Repository adapters may use their storage-oriented lineage names.  The
    # transport normalizes those aliases, then selects exactly one closed
    # OpenAPI lifecycle variant.  A union of all variant properties is not
    # safe here: the schemas deliberately use ``additionalProperties: false``
    # to prevent instance/step metadata from crossing the stream boundary.
    aliases = {
        "workflowRevisionId": "revisionId",
        "workflowInstanceId": "instanceId",
        "status": "instanceStatus",
    }
    canonical: dict[str, object] = {}
    for raw_key, item in value.items():
        key = aliases.get(str(raw_key), str(raw_key))
        if key not in canonical:
            canonical[key] = item
    event_type = canonical.get("eventType")
    variant_fields: dict[str, set[str]] = {
        "workflow.revision.published": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "version", "occurredAt",
        },
        "workflow.revision.retired": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "version", "occurredAt",
        },
        "workflow.definition.retired": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "version", "occurredAt",
        },
        "workflow.instance.admitted": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "instanceStatus", "terminalOutcome", "currentStepOrdinal",
            "version", "occurredAt",
        },
        "workflow.instance.status_changed": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "instanceStatus", "terminalOutcome", "currentStepOrdinal",
            "version", "occurredAt",
        },
        "workflow.instance.terminal": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "instanceStatus", "terminalOutcome", "currentStepOrdinal",
            "version", "occurredAt",
        },
        "workflow.step.status_changed": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "stepExecutionId", "stepOrdinal", "stepKind", "stepStatus",
            "effectOutcome", "recoveryCapabilities", "instanceVersion", "occurredAt",
        },
        "workflow.step.reconciled": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "stepExecutionId", "stepOrdinal", "stepKind", "stepStatus",
            "effectOutcome", "recoveryCapabilities", "instanceVersion", "occurredAt",
        },
        "workflow.retention.deleted": {
            "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
            "instanceId", "deletionScope", "tombstoneRetained", "version", "occurredAt",
        },
        "heartbeat": {"schemaVersion", "eventType", "occurredAt"},
    }
    allowed = variant_fields.get(str(event_type), {"schemaVersion", "eventType", "occurredAt"})
    if event_type == "workflow.definition.retired":
        canonical["revisionId"] = None
    return _public({key: item for key, item in canonical.items() if key in allowed})


def _page_items(page: object) -> tuple[object, ...]:
    raw: object = getattr(page, "items", None)
    if raw is None and isinstance(page, (tuple, list)):
        raw = cast(Sequence[object], page)
    if not isinstance(raw, Iterable):
        return ()
    return tuple(cast(Iterable[object], raw))


def _next_cursor(page: object) -> object:
    return getattr(page, "next_cursor", None)


def _selector(value: object) -> Selector:
    if not isinstance(value, dict):
        raise WorkflowValidationError("workflow selector must be an object")
    selector = cast(dict[str, object], value)
    source = str(selector.get("source"))
    field = str(selector.get("field"))
    ordinal = selector.get("stepOrdinal", selector.get("step_ordinal"))
    ordinal_value = int(str(ordinal)) if ordinal is not None else None
    return Selector(source, field, ordinal_value)


def _revision(
    body: CreateWorkflowRequest, *, workflow_id: UUID, issuer: str, subject: str,
    revision_id: UUID | None = None,
) -> WorkflowRevision:
    fields, steps = _workflow_parts(body)
    return WorkflowRevision(
        revision_id or uuid4(), workflow_id, issuer, subject, body.display_name,
        fields, steps,
    )


def _workflow_parts(
    body: CreateWorkflowRequest,
) -> tuple[tuple[InputField, ...], tuple[WorkflowStep, ...]]:
    fields = tuple(
        InputField(str(item["name"]), WorkflowValueType(str(item.get("valueType", item.get("value_type")))))
        for item in body.input_fields
    )
    steps: list[WorkflowStep] = []
    for raw in body.steps:
        kind = WorkflowStepKind(str(raw.get("kind")))
        if kind is WorkflowStepKind.AGENT_RUN:
            steps.append(AgentRunStep(str(raw["agentRevisionId"]), str(raw["instructions"]), _selector(raw["input"])))
        elif kind is WorkflowStepKind.OWNER_APPROVAL:
            steps.append(OwnerApprovalStep())
        elif kind is WorkflowStepKind.OWNER_NOTIFICATION:
            steps.append(OwnerNotificationStep(_selector(raw["content"])))
        elif kind is WorkflowStepKind.WAIT_UNTIL:
            steps.append(WaitUntilStep(_selector(raw["dueAt"])))
    return fields, tuple(steps)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowNotFound):
        return HTTPException(404, "workflow resource not found")
    if isinstance(exc, WorkflowAdmissionUnavailable):
        return HTTPException(503, "workflow admission is disabled")
    if isinstance(exc, WorkflowLifecycleCursorNotFound):
        return HTTPException(404, "workflow lifecycle cursor was not found")
    if isinstance(exc, WorkflowLifecycleCursorExpired):
        return HTTPException(410, "workflow lifecycle cursor has expired")
    if isinstance(exc, (WorkflowTransitionError, WorkflowVersionConflict, WorkflowIdempotencyConflict)):
        return HTTPException(409, str(exc))
    if isinstance(exc, (WorkflowValidationError, ValueError, TypeError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "workflow operation failed")


def _service(request: Request) -> AdmissionGatedWorkflowApplication:
    return _state(request).workflow_service


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _trace_context(request: Request) -> WorkflowTraceContext:
    """Create one opaque trace parent for each production HTTP command."""

    del request
    return WorkflowTraceContext(trace_id=uuid4().hex)


@router.get("")
async def list_workflows(request: Request, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        page = await _service(request).list_definitions(
            *_owner(session), trace_context=_trace_context(request)
        )
    except Exception as exc:
        raise _error(exc) from exc
    items = _page_items(page)
    return {"items": [_definition_public(item) for item in items], "nextCursor": _next_cursor(page)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    request: Request, response: Response, body: CreateWorkflowRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    issuer, subject = _owner(session)
    try:
        service = _service(request)
        fields, steps = _workflow_parts(body)
        result = await service.create_draft_command(
            WorkflowCreateDraftCommand(
                issuer=issuer, subject=subject,
                display_name=body.display_name,
                input_fields=fields,
                steps=steps,
            ),
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        item = result.revision
    except Exception as exc:
        raise _error(exc) from exc
    return _draft_public(item)


@router.get("/stream")
async def stream_workflow_events(
    request: Request,
    session: Session = Depends(require_session),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = _state(request)
    issuer, subject = _owner(session)
    trace_context = _trace_context(request)
    cursor: UUID | None = None
    if last_event_id is not None:
        try:
            cursor = UUID(last_event_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Last-Event-ID must be a UUID") from exc
    try:
        await state.workflow_lifecycle.replay(
            issuer, subject, cursor, trace_context=trace_context
        )
    except Exception as exc:
        raise _error(exc) from exc
    async def generate() -> AsyncIterator[str]:
        replay_cursor: object | None = cursor
        try:
            while True:
                events = await state.workflow_lifecycle.replay(
                    issuer, subject, replay_cursor,
                    trace_context=trace_context,
                )
                for event in events:
                    event_id = event.get("eventId") or event.get("event_id")
                    if event_id is None:
                        continue
                    replay_cursor = UUID(str(event_id))
                    payload = _event_public(event)
                    yield f"id: {event_id}\nevent: {payload.get('eventType', 'workflow.lifecycle')}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    await asyncio.sleep(0)
                if not await state.workflow_lifecycle.wait(15.0):
                    heartbeat = {"schemaVersion": 1, "eventType": "heartbeat", "occurredAt": datetime.now(UTC).isoformat()}
                    yield f"event: heartbeat\ndata: {json.dumps(heartbeat, separators=(',', ':'))}\n\n"
        except asyncio.CancelledError:
            raise
    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{workflow_id:uuid}")
async def get_workflow(request: Request, workflow_id: UUID, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        return _definition_public(await _service(request).get_definition(*_owner(session), workflow_id))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/drafts", status_code=status.HTTP_201_CREATED)
async def clone_workflow_draft(
    request: Request, workflow_id: UUID, body: CloneWorkflowRequest, response: Response,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        item = await _service(request).clone_draft_in_workflow(
            *_owner(session), workflow_id, revision_id=body.revision_id,
            expected_definition_version=body.expected_definition_version,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        return _draft_public(item)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{workflow_id:uuid}/drafts/{draft_id:uuid}")
async def get_workflow_draft(
    request: Request, workflow_id: UUID, draft_id: UUID, response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        return _draft_public(await _service(request).get_revision_in_workflow(
            *_owner(session), workflow_id, draft_id
        ))
    except Exception as exc:
        raise _error(exc) from exc


@router.patch("/{workflow_id:uuid}/drafts/{draft_id:uuid}")
async def update_workflow_draft(
    request: Request, workflow_id: UUID, draft_id: UUID, body: UpdateWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    issuer, subject = _owner(session)
    try:
        updated = await _service(request).update_draft_in_workflow(
            _revision(body, workflow_id=workflow_id, issuer=issuer, subject=subject, revision_id=draft_id),
            workflow_id=workflow_id, expected_version=body.expected_version,
            issuer=issuer, subject=subject, idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        return _draft_public(updated)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/drafts/{draft_id:uuid}/validate")
async def validate_workflow_draft(
    request: Request, workflow_id: UUID, draft_id: UUID, body: WorkflowCommand,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        await _service(request).get_revision_in_workflow(*_owner(session), workflow_id, draft_id)
        result = await _service(request).validate_draft(
            *_owner(session), draft_id, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        return _validation_public(result)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/drafts/{draft_id:uuid}/publish", status_code=status.HTTP_201_CREATED)
async def publish_workflow_draft(
    request: Request, workflow_id: UUID, draft_id: UUID, body: WorkflowCommand,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        await _service(request).get_revision_in_workflow(*_owner(session), workflow_id, draft_id)
        return _revision_public(await _service(request).publish(
            draft_id, expected_version=body.expected_version, issuer=session.principal.issuer,
            subject=session.principal.subject,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        ))
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{workflow_id:uuid}/revisions/{revision_id:uuid}")
async def get_workflow_revision(
    request: Request, workflow_id: UUID, revision_id: UUID, response: Response,
    session: Session = Depends(require_session),
) -> dict[str, object]:
    _no_store(response)
    try:
        return _revision_public(await _service(request).get_revision_in_workflow(
            *_owner(session), workflow_id, revision_id
        ))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/retire")
async def retire_workflow(request: Request, workflow_id: UUID, body: WorkflowCommand, response: Response, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    _no_store(response)
    try:
        await _service(request).retire_definition(
            *_owner(session), workflow_id, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        return _definition_public(await _service(request).get_definition(*_owner(session), workflow_id))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/revisions/{revision_id:uuid}/retire")
async def retire_revision(request: Request, workflow_id: UUID, revision_id: UUID, body: WorkflowCommand, response: Response, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    _no_store(response)
    try:
        await _service(request).get_revision_in_workflow(*_owner(session), workflow_id, revision_id)
        return _revision_public(await _service(request).retire_revision(
            *_owner(session), revision_id, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        ))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances", status_code=status.HTTP_202_ACCEPTED)
async def start_workflow(request: Request, workflow_id: UUID, body: StartWorkflowRequest, response: Response, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    _no_store(response)
    try:
        issuer, subject = _owner(session)
        await _service(request).get_revision_in_workflow(
            issuer, subject, workflow_id, body.revision_id
        )
        item = await _service(request).admit(
            issuer,
            subject,
            body.revision_id,
            input_values=body.input,
            idempotency_key=str(idempotency_key),
            authority=WorkflowAuthoritySnapshot(issuer, subject),
            trace_context=_trace_context(request),
        )
        state = await _service(request).get_state_in_workflow(
            issuer, subject, workflow_id, item.id
        )
        return _instance_public(state)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{workflow_id:uuid}/instances")
async def list_workflow_instances(
    request: Request, workflow_id: UUID, response: Response,
    session: Session = Depends(require_session),
    limit: int = 50, cursor: str | None = None,
) -> dict[str, object]:
    _no_store(response)
    if not 1 <= limit <= 100:
        raise HTTPException(422, "workflow page limit is out of bounds")
    try:
        page = await _service(request).list_instances_in_workflow(
            *_owner(session), workflow_id, limit=limit, cursor=cursor,
        )
        items = _page_items(page)
        return {"items": [_instance_summary_public(item) for item in items], "nextCursor": _next_cursor(page)}
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{workflow_id:uuid}/instances/{instance_id:uuid}")
async def get_instance(request: Request, workflow_id: UUID, instance_id: UUID, response: Response, session: Session = Depends(require_session)) -> dict[str, object]:
    _no_store(response)
    try:
        return _instance_public(await _service(request).get_state_in_workflow(
            *_owner(session), workflow_id, instance_id
        ))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_instance(request: Request, workflow_id: UUID, instance_id: UUID, body: WorkflowCommand, response: Response, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    _no_store(response)
    try:
        await _service(request).get_instance_in_workflow(*_owner(session), workflow_id, instance_id)
        result = await _service(request).cancel(
            *_owner(session), instance_id, expected_version=body.expected_version,
            idempotency_key=str(idempotency_key),
            trace_context=_trace_context(request),
        )
        return _instance_public(getattr(result, "instance", result))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/resolve")
async def resolve_instance(request: Request, workflow_id: UUID, instance_id: UUID, body: ResolveWorkflowRequest, response: Response, session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key")) -> dict[str, object]:
    _no_store(response)
    try:
        service = _service(request)
        await service.get_instance_in_workflow(*_owner(session), workflow_id, instance_id)
        if body.action == "request_replay":
            result = await service.request_replay(
                *_owner(session), instance_id, expected_version=body.expected_version,
                step_execution_id=str(body.step_execution_id),
                idempotency_key=str(idempotency_key),
                trace_context=_trace_context(request),
            )
        elif body.action == "settle_failed":
            result = await service.settle_failed(
                *_owner(session), instance_id, expected_version=body.expected_version,
                step_execution_id=str(body.step_execution_id),
                idempotency_key=str(idempotency_key),
                trace_context=_trace_context(request),
            )
        elif body.action in {"reconcile_completed", "reconcile_failed"}:
            result = await service.reconcile(
                *_owner(session), instance_id, expected_version=body.expected_version,
                confirmed=body.action == "reconcile_completed",
                step_execution_id=str(body.step_execution_id),
                idempotency_key=str(idempotency_key),
                trace_context=_trace_context(request),
            )
        else:
            raise WorkflowValidationError("unsupported workflow resolution action")
        return _instance_public(getattr(result, "instance", result))
    except Exception as exc:
        raise _error(exc) from exc


def _deletion_receipt(scope: WorkflowDeletionScope, receipt: object) -> dict[str, object]:
    contract_scope = {
        WorkflowDeletionScope.TERMINAL_INSTANCE: "terminal_instance_state",
        WorkflowDeletionScope.INSTANCE_HISTORY: "terminal_instance_history",
        WorkflowDeletionScope.INSTANCE_RESULT: "terminal_instance_result",
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS: "terminal_instance_checkpoints",
    }.get(scope, scope.value)
    deleted = getattr(receipt, "deleted", None)
    deleted_at = getattr(receipt, "deleted_at", None)
    if not isinstance(deleted, bool) or not isinstance(deleted_at, datetime):
        raise WorkflowValidationError("workflow deletion returned an invalid receipt")
    return {
        "scope": contract_scope,
        "deleted": deleted,
        "tombstoneRetained": True,
        "deletedAt": deleted_at.astimezone(UTC).isoformat(),
    }


async def _delete_workflow_resource(
    request: Request, workflow_id: UUID, target_id: UUID, body: DeleteWorkflowRequest,
    scope: WorkflowDeletionScope, session: Session, idempotency_key: UUID,
) -> dict[str, object]:
    service = _service(request)
    issuer, subject = _owner(session)
    expected_scope = {
        WorkflowDeletionScope.TERMINAL_INSTANCE: "terminal_instance_state",
        WorkflowDeletionScope.INSTANCE_HISTORY: "terminal_instance_history",
        WorkflowDeletionScope.INSTANCE_RESULT: "terminal_instance_result",
        WorkflowDeletionScope.INSTANCE_CHECKPOINTS: "terminal_instance_checkpoints",
    }.get(scope, scope.value)
    if body.scope != expected_scope:
        raise WorkflowValidationError("workflow deletion scope is invalid")
    receipt = await service.delete_receipt(
        issuer, subject, workflow_id, target_id, scope=scope,
        expected_version=body.expected_version,
        idempotency_key=str(idempotency_key),
        trace_context=_trace_context(request),
    )
    return _deletion_receipt(scope, receipt)


@router.post("/{workflow_id:uuid}/drafts/{draft_id:uuid}/deletions")
async def delete_workflow_draft(
    request: Request, workflow_id: UUID, draft_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, draft_id, body, WorkflowDeletionScope.DRAFT, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/deletions")
async def delete_workflow_definition(
    request: Request, workflow_id: UUID, body: DeleteWorkflowRequest, response: Response,
    session: Session = Depends(require_csrf), idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, workflow_id, body, WorkflowDeletionScope.RETIRED_DEFINITION, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/revisions/{revision_id:uuid}/deletions")
async def delete_workflow_revision(
    request: Request, workflow_id: UUID, revision_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, revision_id, body, WorkflowDeletionScope.PUBLISHED_REVISION, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/deletions")
async def delete_workflow_instance_state(
    request: Request, workflow_id: UUID, instance_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, instance_id, body, WorkflowDeletionScope.TERMINAL_INSTANCE, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/history-deletions")
async def delete_workflow_instance_history(
    request: Request, workflow_id: UUID, instance_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, instance_id, body, WorkflowDeletionScope.INSTANCE_HISTORY, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/result-deletions")
async def delete_workflow_instance_result(
    request: Request, workflow_id: UUID, instance_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, instance_id, body, WorkflowDeletionScope.INSTANCE_RESULT, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/{workflow_id:uuid}/instances/{instance_id:uuid}/checkpoint-deletions")
async def delete_workflow_instance_checkpoints(
    request: Request, workflow_id: UUID, instance_id: UUID, body: DeleteWorkflowRequest,
    response: Response, session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> dict[str, object]:
    _no_store(response)
    try:
        return await _delete_workflow_resource(request, workflow_id, instance_id, body, WorkflowDeletionScope.INSTANCE_CHECKPOINTS, session, idempotency_key)
    except Exception as exc:
        raise _error(exc) from exc


__all__ = ["router"]
