"""Deterministic evaluators for approval-backed reminder creation."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_core.domains.automation.reminders.proposals import (
    MemoryReminderProposalRepository,
    ReminderProposalNotFound,
)
from aura_core.domains.automation.reminders.public import (
    ReminderApplicationService,
    ReminderCommandProposal,
    ReminderView,
)
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository
from aura_core.domains.execution.runs.public import RunCoordinator, RunRepository, RunStatus
from aura_core.domains.governance.approvals.public import (
    APPROVAL_COMPONENT,
    Approval,
    ApprovalNotFound,
    ApprovalStatus,
    ApprovalTransitionError,
    ApprovalVersionConflict,
    CreateWorkflowApproval,
)
from aura_core.domains.governance.approvals.repository import MemoryApprovalRepository
from aura_core.domains.interaction.agents.public import AgentCatalog, ToolGrant, ToolPolicy
from aura_core.domains.interaction.conversations.public import (
    LEGACY_TIME_CLARIFICATION,
    ConversationStore,
    InvalidConversationMetadata,
    server_local_time,
    validate_client_time_zone,
)
from aura_core.entrypoints.api.app import create_app
from aura_core.entrypoints.api.routes.approvals import stream_approval_events
from aura_core.platform.auth import Principal, Settings
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.telemetry import COMPONENT_VERSIONS, MetadataMetrics
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import ModelDescriptor, ModelStreamCompleted, ModelToolCall
from aura_core.runtime.streaming.publisher import EventPublisher
from aura_core.runtime.tools import (
    ToolCall,
    ToolExecutionError,
    ToolPolicyEvaluator,
    default_tool_registry,
    validate_tool_arguments,
)
from starlette.requests import Request

NOW = datetime(2027, 1, 10, 12, tzinfo=UTC)
ISSUER = "https://authentik.example"
OWNER = "owner"
FIXTURE_REVISION = "aura-0066-conversational-reminders-v1"
REQUIRED_EVALUATORS = frozenset(
    {
        "reminder_tool_selection_and_arguments",
        "reminder_tool_permission_enforcement",
        "confirmation_side_effect_prevention",
        "approval_owner_authorization",
        "approval_expiry_correctness",
        "approval_race_exactly_once",
        "reminder_creation_exactly_once",
        "approval_reconnect_reconciliation",
        "timezone_interpretation_and_clarification",
        "conversational_reminder_owner_isolation",
        "conversational_reminder_approval_before_side_effect",
        "conversational_reminder_metadata_only_capture",
    }
)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    scenario_id: str
    case_id: str
    score: float
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "scenarioId": self.scenario_id,
            "caseId": self.case_id,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


EVALUATOR_COMPONENTS = {
    "reminder_tool_selection_and_arguments": (
        "aura.runtime.policy_evaluation",
        COMPONENT_VERSIONS["aura.runtime.policy_evaluation"],
    ),
    "reminder_tool_permission_enforcement": (
        "aura.runtime.policy_evaluation",
        COMPONENT_VERSIONS["aura.runtime.policy_evaluation"],
    ),
    "confirmation_side_effect_prevention": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "approval_owner_authorization": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "approval_expiry_correctness": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "approval_race_exactly_once": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "reminder_creation_exactly_once": (
        "aura.automation.reminder_management",
        COMPONENT_VERSIONS["aura.automation.reminder_management"],
    ),
    "approval_reconnect_reconciliation": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "timezone_interpretation_and_clarification": (
        "aura.runtime.prompt_compilation",
        COMPONENT_VERSIONS["aura.runtime.prompt_compilation"],
    ),
    "conversational_reminder_owner_isolation": (
        "aura.automation.reminder_management",
        COMPONENT_VERSIONS["aura.automation.reminder_management"],
    ),
    "conversational_reminder_approval_before_side_effect": (
        APPROVAL_COMPONENT,
        COMPONENT_VERSIONS[APPROVAL_COMPONENT],
    ),
    "conversational_reminder_metadata_only_capture": (
        "aura.runtime.tool_execution",
        COMPONENT_VERSIONS["aura.runtime.tool_execution"],
    ),
}


def _result(evaluator_id: str, passed: bool, **metadata: str) -> EvaluationResult:
    assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(metadata)
    component_id, component_version = EVALUATOR_COMPONENTS[evaluator_id]
    case_id = f"{evaluator_id}.public-boundary"
    return EvaluationResult(
        evaluator_id,
        component_id,
        component_version,
        FIXTURE_REVISION,
        f"{evaluator_id}:{component_id}@{component_version}:{FIXTURE_REVISION}",
        evaluator_id,
        case_id,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        tuple(sorted(metadata.items())),
    )


class _ApprovalRunProvider:
    """Provider fake exposing selection and tools-disabled synthesis separately."""

    def __init__(self) -> None:
        self.selection_calls = 0
        self.synthesis_calls = 0

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (
            ModelDescriptor("approval-model", "Approval fixture", "fake", ("chat", "tool_calling")),
        )

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "approval-model")

    async def stream_chat_with_tools(
        self, model_id: str, messages: Sequence[object], tools: Sequence[object]
    ) -> AsyncIterator[object]:
        del model_id, messages, tools
        self.selection_calls += 1
        yield ModelToolCall(
            "reminder.create",
            {"content": "private proposal", "dueAt": "2027-01-11T12:00:00Z"},
        )
        yield ModelStreamCompleted()

    async def stream_chat(self, model_id: str, messages: Sequence[object]) -> AsyncIterator[str]:
        del model_id, messages
        self.synthesis_calls += 1
        yield "Reminder request handled."


async def _approved_run_fixture() -> tuple[bool, bool, bool, MetadataMetrics]:
    """Exercise proposal, durable wait, approval resume, and duplicate delivery."""

    provider = _ApprovalRunProvider()
    metrics = MetadataMetrics()
    models = await provider.list_models()
    store = ConversationStore()
    _, _, run = await store.create(
        ISSUER,
        OWNER,
        "Create a reminder",
        "approval-model",
        models,
        str(uuid4()),
        client_time_zone="Europe/Brussels",
    )
    approvals = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: NOW), clock=lambda: NOW, metrics=metrics
    )
    reminders = MemoryReminderRepository(clock=lambda: NOW, metrics=metrics)
    coordinator = RunCoordinator(
        cast(RunRepository, store),
        EventPublisher(),
        InMemoryOutbox(),
        metrics=metrics,
        approval_service=approvals,
        reminder_service=ReminderApplicationService(reminders, clock=lambda: NOW),
    )

    await coordinator.execute(run.id, provider)
    _, waiting = await store.find_run_any(run.id)
    pending = waiting.approval_id is not None and (
        waiting.status is RunStatus.WAITING_FOR_CONFIRMATION
    )
    before = await reminders.list(ISSUER, OWNER, view=ReminderView.UPCOMING, limit=10)
    no_preapproval_mutation = pending and before.items == ()
    assert waiting.approval_id is not None
    detail = await approvals.get(ISSUER, OWNER, waiting.approval_id)
    approved = await approvals.approve(
        ISSUER,
        OWNER,
        waiting.approval_id,
        expected_version=detail.approval.version,
        idempotency_key=f"approve:{waiting.approval_id}",
    )
    await store.resume_waiting_run(run.id, waiting.approval_id, approved=True)
    await coordinator.execute(run.id, provider)
    await coordinator.execute(run.id, provider)
    _, completed = await store.find_run_any(run.id)
    after = await reminders.list(ISSUER, OWNER, view=ReminderView.UPCOMING, limit=10)
    exactly_once = (
        approved.status is ApprovalStatus.APPROVED
        and completed.status is RunStatus.COMPLETED
        and len(after.items) == 1
    )
    tools_disabled = provider.selection_calls == 1 and provider.synthesis_calls == 1
    rejected = await approvals.create_pending(
        ISSUER,
        OWNER,
        content="rejected proposal",
        due_at=NOW + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    await approvals.reject(
        ISSUER,
        OWNER,
        rejected.approval.id,
        expected_version=rejected.approval.version,
        idempotency_key=f"reject:{rejected.approval.id}",
    )
    try:
        await reminders.create(
            ISSUER,
            OWNER,
            content="failed reminder",
            due_at=NOW - timedelta(minutes=1),
            idempotency_key="failed-reminder",
        )
    except Exception:
        pass
    return no_preapproval_mutation, exactly_once, tools_disabled, metrics


async def _workflow_approval_fixture() -> dict[str, bool]:
    """Exercise workflow approval lineage, replay, authorization, and expiry."""

    clock = [NOW]
    approvals = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: clock[0]), clock=lambda: clock[0]
    )

    def command(
        key: str, *, subject: str = OWNER, expires_at: datetime | None = None
    ) -> CreateWorkflowApproval:
        return CreateWorkflowApproval(
            ISSUER,
            subject,
            uuid4(),
            uuid4(),
            uuid4(),
            2,
            uuid4(),
            "Daily plan",
            "owner_notification",
            "continue_to_owner_notification",
            "Approve this step to continue to the private notification.",
            "owner-scope",
            "workspace-scope",
            expires_at or NOW + timedelta(hours=1),
            key,
            NOW + timedelta(days=1),
        )

    approved_detail = await approvals.create_workflow_pending(command("workflow-approve"))
    foreign_denied = False
    try:
        await approvals.get(ISSUER, "foreign-owner", approved_detail.approval.id)
    except ApprovalNotFound:
        foreign_denied = True
    created_events = await approvals.replay(ISSUER, OWNER)
    approved = await approvals.approve(
        ISSUER,
        OWNER,
        approved_detail.approval.id,
        expected_version=1,
        idempotency_key="workflow-decision",
    )
    replayed = await approvals.approve(
        ISSUER,
        OWNER,
        approved_detail.approval.id,
        expected_version=1,
        idempotency_key="workflow-decision",
    )
    rejected_detail = await approvals.create_workflow_pending(command("workflow-reject"))
    rejected = await approvals.reject(
        ISSUER,
        OWNER,
        rejected_detail.approval.id,
        expected_version=1,
        idempotency_key="workflow-reject-decision",
    )
    canceled_detail = await approvals.create_workflow_pending(command("workflow-cancel"))
    canceled = await approvals.cancel(
        ISSUER,
        OWNER,
        canceled_detail.approval.id,
        expected_version=1,
        idempotency_key="workflow-cancel-decision",
    )
    expiring_detail = await approvals.create_workflow_pending(command("workflow-expire"))
    clock[0] = expiring_detail.approval.expires_at
    expired = (await approvals.expire_due(now=clock[0]))[0]
    events = await approvals.replay(ISSUER, OWNER)
    owner_cursor = await approvals.cursor_is_authorized(ISSUER, OWNER, created_events[0].id)
    foreign_cursor = await approvals.cursor_is_authorized(
        ISSUER, "foreign-owner", created_events[0].id
    )
    payloads = [event.payload() for event in events]
    return {
        "owner": foreign_denied and owner_cursor and not foreign_cursor,
        "decision": approved.origin == approved_detail.approval.origin
        and replayed == approved
        and rejected.origin == rejected_detail.approval.origin
        and canceled.origin == canceled_detail.approval.origin,
        "expiry": expired.origin == expiring_detail.approval.origin,
        "replay": len(events) == 8 and all(event.origin is not None for event in events),
        "metadata": all(
            not any(
                forbidden in repr(payload).lower()
                for forbidden in ("content", "prompt", "response", "private")
            )
            for payload in payloads
        ),
    }


async def _authenticated_sse_reconnect_fixture() -> bool:
    """Exercise owner-authenticated Last-Event-ID replay at the HTTP boundary."""

    settings = Settings(
        public_origin="https://aura.dev.example",
        oidc_issuer=ISSUER,
        owner_subject=OWNER,
        default_model="fake",
        secure_cookies=False,
    )
    app = create_app(settings, testing=True)
    fake_provider = FakeChatModel()
    app.state.aura.provider = fake_provider
    app.state.aura.gateway = ModelGateway(fake_provider)
    owner_session_id, _owner_session = await app.state.aura.sessions.create(
        Principal(ISSUER, OWNER, "Fixture owner")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        headers={"Origin": settings.public_origin},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, owner_session_id)
    try:
        service = app.state.aura.approval_service
        due_at = datetime.now(UTC) + timedelta(hours=2)
        owner_detail = await service.create_pending(
            ISSUER,
            OWNER,
            content="private reconnect proposal",
            due_at=due_at,
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        await service.approve(
            ISSUER,
            OWNER,
            owner_detail.approval.id,
            expected_version=1,
            idempotency_key="sse-approve",
        )
        owner_events = await service.replay(ISSUER, OWNER)
        assert len(owner_events) == 2

        foreign_detail = await service.create_pending(
            ISSUER,
            "other-owner",
            content="private foreign proposal",
            due_at=due_at,
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        foreign_event = (await service.replay(ISSUER, "other-owner"))[0]
        del foreign_detail
        unauthorized = await client.get(
            "/api/v1/approvals/stream",
            headers={"Last-Event-ID": str(foreign_event.id)},
        )
        if unauthorized.status_code != 410:
            return False

        response = await stream_approval_events(
            Request({"type": "http", "app": app, "headers": []}),
            _owner_session,
            owner_events[0].id,
        )
        body_iterator = cast(AsyncGenerator[str], response.body_iterator)
        chunk = await anext(body_iterator)
        await body_iterator.aclose()
        return (
            f"id: {owner_events[1].id}" in chunk
            and "data: " in chunk
            and "private reconnect proposal" not in chunk
        )
    finally:
        await client.aclose()


async def _timezone_telemetry_fixture() -> bool:
    """Exercise API timezone success/failure telemetry at the transport seam."""

    settings = Settings(
        public_origin="https://aura.dev.example",
        oidc_issuer=ISSUER,
        owner_subject=OWNER,
        default_model="fake",
        secure_cookies=False,
    )
    app = create_app(settings, testing=True)
    fake_provider = FakeChatModel()
    app.state.aura.provider = fake_provider
    app.state.aura.gateway = ModelGateway(fake_provider)
    session_id, session = await app.state.aura.sessions.create(
        Principal(ISSUER, OWNER, "Fixture owner")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        headers={"Origin": settings.public_origin},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    try:
        headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        created = await client.post(
            "/api/v1/conversations",
            json={
                "message": "Timezone telemetry fixture",
                "modelId": "fake",
                "clientTimeZone": "Europe/Brussels",
            },
            headers=headers,
        )
        if created.status_code != 202:
            return False
        conversation = cast(dict[str, object], created.json()["conversation"])
        conversation_id = conversation["id"]
        version = conversation["version"]
        valid_run = await client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={
                "message": "Valid timezone telemetry fixture",
                "conversationVersion": version,
                "clientTimeZone": "Europe/Brussels",
            },
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        if valid_run.status_code not in {202, 409}:
            return False
        invalid_conversation = await client.post(
            "/api/v1/conversations",
            json={"message": "Invalid timezone run fixture", "modelId": "fake"},
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        if invalid_conversation.status_code != 202:
            return False
        invalid_detail = cast(dict[str, object], invalid_conversation.json()["conversation"])
        conversation_id = invalid_detail["id"]
        version = invalid_detail["version"]
        invalid = await client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={
                "message": "Invalid timezone telemetry fixture",
                "conversationVersion": version,
                "clientTimeZone": "not/an-iana-zone",
            },
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        omitted_conversation = await client.post(
            "/api/v1/conversations",
            json={"message": "Omitted timezone run fixture", "modelId": "fake"},
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        if omitted_conversation.status_code != 202:
            return False
        omitted_detail = cast(dict[str, object], omitted_conversation.json()["conversation"])
        omitted = await client.post(
            f"/api/v1/conversations/{omitted_detail['id']}/runs",
            json={
                "message": "Omitted timezone telemetry fixture",
                "conversationVersion": omitted_detail["version"],
            },
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        outcomes = [
            dict(measurement.dimensions).get("outcome")
            for measurement in app.state.aura.metrics.snapshot()
            if measurement.metric == "client_timezone_validation_outcome"
        ]
        return (
            invalid.status_code == 409
            and omitted.status_code == 409
            and Counter(outcomes) == Counter({"valid": 1, "invalid": 1, "omitted": 1})
            and app.state.aura.metrics.stats().rejected == 0
        )
    finally:
        await client.aclose()


async def _results() -> tuple[EvaluationResult, ...]:
    registry = default_tool_registry()
    descriptor = registry.get("reminder.create", 1)
    assert descriptor is not None
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    evaluator = ToolPolicyEvaluator(registry)
    valid_call = ToolCall(
        "reminder.create",
        {"content": "private", "dueAt": "2027-01-11T12:00:00Z"},
    )
    selection = evaluator.decide(policy, valid_call)
    try:
        validate_tool_arguments(descriptor, valid_call.arguments)
        arguments_valid = True
    except ToolExecutionError:
        arguments_valid = False

    denied_policy = ToolPolicy(
        uuid4(),
        aura.id,
        1,
        grants=(ToolGrant("reminder.create", 1),),
        max_calls_per_run=1,
        timeout_ms=10_000,
        permission_scopes=frozenset(),
    )
    denied = evaluator.decide(denied_policy, valid_call)
    assert denied.error_code == "TOOL_PERMISSION_DENIED"

    approvals = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: NOW), clock=lambda: NOW
    )
    pending = await approvals.create_pending(
        ISSUER,
        OWNER,
        content="private",
        due_at=NOW + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=valid_call.call_id,
    )
    foreign_denied = False
    try:
        await approvals.get(ISSUER, "other-owner", pending.approval.id)
    except ApprovalNotFound:
        foreign_denied = True

    clock = [NOW]
    expiring = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: clock[0]), clock=lambda: clock[0]
    )
    due = NOW + timedelta(minutes=15)
    expiring_item = await expiring.create_pending(
        ISSUER,
        OWNER,
        content="soon",
        due_at=due,
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    clock[0] = due
    try:
        await expiring.decide(
            ISSUER,
            OWNER,
            expiring_item.approval.id,
            decision=ApprovalStatus.APPROVED,
            expected_version=1,
            idempotency_key="late",
        )
    except (ApprovalVersionConflict, ApprovalTransitionError):
        expiry_correct = (
            (await expiring.get(ISSUER, OWNER, expiring_item.approval.id)).approval.status
            is ApprovalStatus.EXPIRED
        )
    else:
        expiry_correct = False

    async def race(decision: ApprovalStatus) -> object:
        try:
            return await approvals.decide(
                ISSUER,
                OWNER,
                pending.approval.id,
                decision=decision,
                expected_version=1,
                idempotency_key=f"race-{decision.value}",
            )
        except (ApprovalVersionConflict, ApprovalTransitionError) as exc:
            return exc

    raced = await asyncio.gather(race(ApprovalStatus.APPROVED), race(ApprovalStatus.REJECTED))
    race_winners = [item for item in raced if isinstance(item, Approval)]
    race_exactly_once = len(race_winners) == 1
    run_no_preapproval, run_creation_exactly_once, tools_disabled_synthesis, runtime_metrics = (
        await _approved_run_fixture()
    )
    reconnect = await _authenticated_sse_reconnect_fixture()
    workflow = await _workflow_approval_fixture()
    timezone_telemetry = await _timezone_telemetry_fixture()
    measurements = runtime_metrics.snapshot()
    metric_names = {measurement.metric for measurement in measurements}
    metric_outcomes = {
        (measurement.metric, dict(measurement.dimensions).get("outcome"))
        for measurement in measurements
    }
    telemetry_contract = (
        runtime_metrics.stats().rejected == 0
        and "approval_creation_outcome" in metric_names
        and "approval_decision_outcome" in metric_names
        and "reminder_create_idempotency_outcome" in metric_names
        and ("approval_decision_outcome", "rejected") in metric_outcomes
        and ("reminder_create_idempotency_outcome", "failed") in metric_outcomes
        and timezone_telemetry
    )
    due_at_schema = cast(dict[str, object], descriptor.input_schema["properties"])["dueAt"]
    timezone_schema_valid = cast(dict[str, object], due_at_schema).get("format") == "date-time"
    timezone_valid = validate_client_time_zone("Europe/Brussels") == "Europe/Brussels"
    timezone_missing_is_legacy = validate_client_time_zone(None) is None
    try:
        validate_client_time_zone("not/an-iana-zone")
    except InvalidConversationMetadata:
        invalid_timezone_rejected = True
    else:
        invalid_timezone_rejected = False
    dst_equivalent = server_local_time(
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC), "Europe/Brussels"
    ) == "2026-03-29T03:30:00+02:00"
    legacy_ambiguity_is_explicit = "RFC3339" in LEGACY_TIME_CLARIFICATION
    scoped_reminders = MemoryReminderRepository(clock=lambda: NOW)
    await scoped_reminders.create(
        ISSUER,
        OWNER,
        content="owner scoped fixture",
        due_at=NOW + timedelta(hours=2),
        idempotency_key="owner-scoped-fixture",
    )
    scoped_service = ReminderApplicationService(scoped_reminders, clock=lambda: NOW)
    owned = await scoped_service.resolve_target(ISSUER, OWNER, "owner scoped fixture")
    foreign = await scoped_service.resolve_target(
        ISSUER, "other-owner", "owner scoped fixture"
    )
    owner_isolation = (
        getattr(owned, "status", None) == "resolved"
        and len(getattr(owned, "matches", ())) == 1
        and getattr(foreign, "status", None) != "resolved"
        and not getattr(foreign, "matches", ())
    )
    metadata_values = [
        repr((measurement.dimensions, measurement.trace_attributes))
        for measurement in measurements
    ]
    metadata_only_capture = all(
        not any(
            forbidden in value.casefold()
            for forbidden in ("private proposal", "failed reminder", "2027-01-11", "arguments")
        )
        for value in metadata_values
    ) and runtime_metrics.stats().rejected == 0
    return (
        _result("reminder_tool_selection_and_arguments", selection.allowed and arguments_valid),
        _result("reminder_tool_permission_enforcement", not denied.allowed),
        _result("confirmation_side_effect_prevention", run_no_preapproval),
        _result(
            "approval_owner_authorization",
            foreign_denied and workflow["owner"],
            workflow_owner_isolation=str(workflow["owner"]).lower(),
        ),
        _result(
            "approval_expiry_correctness",
            expiry_correct and workflow["expiry"],
            workflow_expiry=str(workflow["expiry"]).lower(),
        ),
        _result(
            "approval_race_exactly_once",
            race_exactly_once and workflow["decision"],
            workflow_decision=str(workflow["decision"]).lower(),
        ),
        _result(
            "reminder_creation_exactly_once",
            run_creation_exactly_once and telemetry_contract,
            duplicate_resume="true",
            tools_disabled_synthesis=str(tools_disabled_synthesis).lower(),
            telemetry_contract=str(telemetry_contract).lower(),
        ),
        _result(
            "approval_reconnect_reconciliation",
            reconnect and workflow["replay"],
            last_event_id="authorized",
            workflow_cursor_replay=str(workflow["replay"]).lower(),
        ),
        _result(
            "timezone_interpretation_and_clarification",
            timezone_schema_valid
            and timezone_valid
            and timezone_missing_is_legacy
            and invalid_timezone_rejected
            and dst_equivalent
            and legacy_ambiguity_is_explicit,
        ),
        _result(
            "conversational_reminder_owner_isolation",
            owner_isolation,
            unique_match="true" if owner_isolation else "false",
            foreign_match="none" if not getattr(foreign, "matches", ()) else "unexpected",
        ),
        _result(
            "conversational_reminder_approval_before_side_effect",
            run_no_preapproval,
            pending="true" if run_no_preapproval else "false",
            mutation="after_approval",
        ),
        _result(
            "conversational_reminder_metadata_only_capture",
            metadata_only_capture,
            telemetry="metadata_only",
            rejected=str(runtime_metrics.stats().rejected),
            workflow_metadata_only=str(workflow["metadata"]).lower(),
        ),
    )


@pytest.mark.asyncio
async def test_approval_evaluators_are_replayable_and_content_free() -> None:
    first = await _results()
    second = await _results()
    assert first == second
    assert {item.evaluator_id for item in first} == REQUIRED_EVALUATORS
    assert all(item.score == 1.0 and item.outcome == "pass" for item in first)
    for result in first:
        payload = result.payload()
        assert set(payload) == {
            "evaluatorId",
            "componentId",
            "componentVersion",
            "fixtureRevision",
            "replayKey",
            "scenarioId",
            "caseId",
            "score",
            "outcome",
            "metadata",
        }
        component_id = cast(str, payload["componentId"])
        assert payload["componentVersion"] == COMPONENT_VERSIONS[component_id]
        assert not any(
            forbidden in repr(payload["metadata"]).lower()
            for forbidden in ("private", "content", "dueat", "arguments", "prompt", "response")
        )


@pytest.mark.asyncio
async def test_typed_reminder_command_proposals_pin_target_version_operation_and_timezone() -> None:
    repository = MemoryReminderProposalRepository(clock=lambda: NOW)
    target_id = uuid4()
    arguments = {
        "content": "private command content",
        "recurrence": {"frequency": "daily", "interval": 1},
    }
    proposal = await repository.create_command(
        ISSUER,
        OWNER,
        operation="series.update",
        tool_id="reminder.series.update",
        tool_version=1,
        target_id=target_id,
        expected_version=7,
        normalized_arguments=arguments,
        timezone="Europe/Brussels",
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
        now=NOW,
    )
    assert proposal.operation == "series.update"
    assert proposal.tool_id == "reminder.series.update"
    assert proposal.tool_version == 1
    assert proposal.target_id == target_id
    assert proposal.expected_version == 7
    assert proposal.target_type == "series"
    assert proposal.timezone == "Europe/Brussels"
    assert proposal.normalized_arguments == arguments
    with pytest.raises(FrozenInstanceError):
        proposal.expected_version = 8  # type: ignore[misc]

    replay = await repository.get_command(ISSUER, OWNER, proposal.id)
    assert replay == proposal
    with pytest.raises(ReminderProposalNotFound):
        await repository.get_command(ISSUER, "other-owner", proposal.id)


@pytest.mark.asyncio
async def test_typed_command_approval_replay_is_owner_scoped_and_metadata_only() -> None:
    approvals = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: NOW), clock=lambda: NOW
    )
    detail = await approvals.create_command_pending(
        ISSUER,
        OWNER,
        operation="series.pause",
        tool_id="reminder.series.pause",
        tool_version=1,
        target_id=uuid4(),
        expected_version=3,
        normalized_arguments={"target": "private reminder"},
        timezone="Europe/Brussels",
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
        now=NOW,
    )
    assert detail.approval.status.value == "pending"
    command = cast(ReminderCommandProposal, detail.proposal)
    assert command.operation == "series.pause"
    with pytest.raises(ApprovalNotFound):
        await approvals.get(ISSUER, "other-owner", detail.approval.id)

    events = await approvals.replay(ISSUER, OWNER)
    assert len(events) == 1
    payload = events[0].payload()
    assert "private reminder" not in repr(payload)
    assert "Europe/Brussels" not in repr(payload)
    assert "target" not in repr(payload)
