"""Core briefing application composition behind the worker entrypoint.

The worker only wires public services and transport.  Admission, settlement,
revalidation, and recovery live here so a second entrypoint cannot accidentally
create a less restrictive briefing execution path.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid5

from aura_core.domains.automation.briefings.context import (
    BriefingContext,
    BriefingContextAssembler,
    BriefingContextRequest,
    BriefingEvidenceItem,
    BriefingSourceSelection,
    SourceCompleteness,
)
from aura_core.domains.automation.briefings.delivery import (
    BriefingDelivery,
    BriefingDeliveryRequest,
)
from aura_core.domains.automation.briefings.execution import (
    BRIEFING_EXECUTION_POLICY_ID,
    BRIEFING_EXECUTION_POLICY_REVISION,
    BriefingExecutionPolicy,
    BriefingExecutionService,
    BriefingRunEvidenceAdapter,
    BriefingRunHandle,
    BriefingRunLineage,
    BriefingRunRequest,
    BriefingRunResult,
    BriefingRunStatePort,
    BriefingRunStatus,
    PublicBriefingRunRepository,
    RunCoordinatorBriefingAdapter,
    assemble_and_execute,
)
from aura_core.domains.automation.briefings.public import (
    BriefingApplicationService,
    BriefingResult,
    BriefingResultStatus,
    BriefingScheduleStatus,
    BriefingSourceCompleteness,
    briefing_phase_span_id,
)
from aura_core.domains.automation.briefings.worker import validate_briefing_command
from aura_core.domains.interaction.agents.public import GENERAL_POLICY_ID
from aura_core.platform.outbox import OutboxCommand
from aura_core.platform.telemetry import MetadataMetrics

_RESULT_NAMESPACE = UUID("5a2cc0df-341e-40fb-bd6d-d7e086d9b88c")


def _result_id(occurrence_id: UUID) -> UUID:
    return uuid5(_RESULT_NAMESPACE, f"briefing-result:{occurrence_id}")


def _authoritative_run_result(
    lineage: BriefingRunLineage,
    durable: BriefingResult,
    *,
    attempt: int = 1,
    error_class: str | None = None,
) -> BriefingRunResult:
    """Translate the state repository's result without inventing a status."""

    try:
        status = BriefingRunStatus(durable.status.value)
    except ValueError as error:
        raise RuntimeError("briefing persistence returned an invalid durable status") from error
    return BriefingRunResult(
        lineage,
        status,
        SourceCompleteness(durable.source_completeness.value),
        durable.content,
        durable.created_at,
        durable.updated_at,
        error_class,
        attempt,
        settled=True,
    )


class BriefingRunStateAdapter(BriefingRunStatePort):
    """Adapt public briefing state to the typed execution settlement port."""

    def __init__(
        self,
        service: BriefingApplicationService,
        *,
        run_evidence: object | None = None,
    ) -> None:
        self.service = service
        self.repository = service.repository
        self.run_evidence = run_evidence
        self._owners: dict[UUID, tuple[str, str]] = {}
        self._lineages: dict[UUID, BriefingRunLineage] = {}

    async def admit_briefing_run(self, request: BriefingRunRequest) -> BriefingRunHandle:
        occurrence = await self.service.occurrence_by_id(request.lineage.occurrence_id)
        if (
            occurrence.owner != request.owner
            or occurrence.schedule_id != request.lineage.schedule_id
        ):
            raise RuntimeError("briefing run owner or lineage is invalid")
        self._owners[request.lineage.occurrence_id] = request.owner
        self._lineages[request.lineage.run_id] = request.lineage
        if occurrence.run_id is not None:
            if occurrence.run_id != request.lineage.run_id:
                raise RuntimeError("briefing occurrence is bound to another run")
            status = (
                BriefingRunStatus(occurrence.status.value)
                if occurrence.status.value in {item.value for item in BriefingRunStatus}
                else BriefingRunStatus.UNKNOWN
            )
            return BriefingRunHandle(request.lineage, status, 1)
        await self.repository.admit_occurrence(
            request.owner[0],
            request.owner[1],
            request.lineage.schedule_id,
            request.lineage.occurrence_id,
            run_id=request.lineage.run_id,
            expected_version=occurrence.version,
        )
        return BriefingRunHandle(request.lineage, BriefingRunStatus.ADMITTED, 1)

    async def settle_briefing_run(self, result: BriefingRunResult) -> BriefingRunResult:
        occurrence = await self.service.occurrence_by_id(result.lineage.occurrence_id)
        owner = self._owners.setdefault(result.lineage.occurrence_id, occurrence.owner)
        durable = await self.repository.create_result(
            BriefingResult(
                _result_id(result.lineage.occurrence_id),
                result.lineage.schedule_id,
                result.lineage.occurrence_id,
                owner[0],
                owner[1],
                result.lineage.run_id,
                BriefingResultStatus(result.status.value),
                1,
                BriefingSourceCompleteness(result.source_completeness.value),
                result.content,
                result.started_at,
                result.finished_at,
            )
        )
        authoritative = _authoritative_run_result(
            result.lineage, durable, attempt=result.attempt
        )
        if self.run_evidence is not None:
            outcome = authoritative.status.value
            if outcome not in {"completed", "partial", "failed", "canceled", "unknown"}:
                outcome = "unknown"
            BriefingRunEvidenceAdapter(self.run_evidence, authoritative.lineage.run_id).record(
                BriefingEvidenceItem(
                    phase="persistence",
                    outcome=outcome,
                    source_completeness=result.source_completeness.value,
                    error_class=(
                        "persistence"
                        if authoritative.status is BriefingRunStatus.UNKNOWN
                        else None
                    ),
                    trace_id=authoritative.lineage.run_id.hex,
                    run_id=authoritative.lineage.run_id,
                )
            )
        return authoritative

    async def cancel_briefing_run(self, lineage: BriefingRunLineage) -> BriefingRunResult:
        occurrence = await self.service.occurrence_by_id(lineage.occurrence_id)
        if occurrence.run_id != lineage.run_id or occurrence.schedule_id != lineage.schedule_id:
            raise RuntimeError("briefing cancellation lineage is invalid")
        now = datetime.now(UTC)
        durable = await self.repository.create_result(
            BriefingResult(
                _result_id(lineage.occurrence_id),
                lineage.schedule_id,
                lineage.occurrence_id,
                occurrence.issuer,
                occurrence.subject,
                lineage.run_id,
                BriefingResultStatus.CANCELED,
                1,
                BriefingSourceCompleteness.UNKNOWN,
                None,
                occurrence.started_at or now,
                now,
            )
        )
        authoritative = _authoritative_run_result(
            lineage,
            durable,
            error_class=(
                "cancellation"
                if durable.status is BriefingResultStatus.CANCELED
                else None
            ),
        )
        if self.run_evidence is not None:
            BriefingRunEvidenceAdapter(self.run_evidence, lineage.run_id).record(
                BriefingEvidenceItem(
                    phase="persistence",
                    outcome=(
                        authoritative.status.value
                        if authoritative.status is not BriefingRunStatus.ADMITTED
                        else "unknown"
                    ),
                    source_completeness=durable.source_completeness.value,
                    error_class=authoritative.error_class,
                    trace_id=lineage.run_id.hex,
                    run_id=lineage.run_id,
                )
            )
        return authoritative

    async def resolve_briefing_lineage(
        self, run_id: UUID, issuer: str, subject: str
    ) -> BriefingRunLineage | None:
        lineage = self._lineages.get(run_id)
        if lineage is None:
            return None
        try:
            occurrence = await self.service.occurrence_by_id(lineage.occurrence_id)
        except Exception:
            return None
        if occurrence.run_id != run_id or occurrence.owner != (issuer, subject):
            return None
        return lineage

    async def recover_briefing_run(self, lineage: BriefingRunLineage) -> BriefingRunResult | None:
        del lineage
        return None

    async def settle_unknown_occurrence(
        self, occurrence: object, *, error_class: str
    ) -> BriefingRunResult | None:
        run_id = getattr(occurrence, "run_id", None)
        schedule_id = cast(Any, getattr(occurrence, "schedule_id", None))
        occurrence_id = cast(Any, getattr(occurrence, "id", None))
        if not all(isinstance(value, UUID) for value in (schedule_id, occurrence_id)):
            return None
        # Admission/context failures can happen before the occurrence receives
        # a run id.  Derive the same deterministic lineage used by normal
        # admission so the terminal UNKNOWN result remains durable and
        # idempotent without fabricating content.
        if not isinstance(run_id, UUID):
            run_id = BriefingRunLineage.for_occurrence(schedule_id, occurrence_id).run_id
        started = getattr(occurrence, "started_at", None) or datetime.now(UTC)
        finished = datetime.now(UTC)
        result = BriefingRunResult(
            BriefingRunLineage.for_occurrence(schedule_id, occurrence_id, run_id),
            BriefingRunStatus.UNKNOWN,
            SourceCompleteness.UNKNOWN,
            None,
            started,
            finished,
            error_class,
        )
        return await self.settle_briefing_run(result)


class BriefingAdmissionRevalidator:
    """Fail closed against current public configuration and source grants."""

    def __init__(
        self,
        schedule: object,
        occurrence: object,
        *,
        agent_service: object,
        calendar: object,
        documents: object,
    ) -> None:
        self.schedule = schedule
        self.occurrence = occurrence
        self.agent_service = agent_service
        self.calendar = calendar
        self.documents = documents

    async def _current_agent_and_policies(self, owner: tuple[str, str]) -> bool:
        execution = getattr(self.schedule, "execution_policies", None)
        revision_id = getattr(self.schedule, "agent_revision_id", None)
        resolver = getattr(self.agent_service, "resolve_active_revision", None)
        if not callable(resolver) or not isinstance(revision_id, UUID) or execution is None:
            return False
        try:
            if getattr(execution, "execution_policy_revision_id", None) != (
                BRIEFING_EXECUTION_POLICY_ID
            ):
                return False
            if getattr(execution, "model_policy_revision_id", None) != GENERAL_POLICY_ID:
                return False
            revision = resolver(revision_id)
            if inspect.isawaitable(revision):
                revision = await revision
            if getattr(revision, "id", None) != revision_id:
                return False
            if getattr(revision, "model_policy_revision_id", None) != getattr(
                execution, "model_policy_revision_id", None
            ):
                return False
            if getattr(revision, "prompt_bundle_revision_id", None) != getattr(
                execution, "prompt_bundle_revision_id", None
            ):
                return False
            pinned_tool = getattr(execution, "tool_policy_revision_id", None)
            if pinned_tool != getattr(revision, "tool_policy_revision_id", None):
                return False
            getter = getattr(self.agent_service, "get_tool_policy", None)
            if pinned_tool is None or not callable(getter):
                return pinned_tool is None
            policy = getter(owner[0], owner[1], pinned_tool)
            if inspect.isawaitable(policy):
                policy = await policy
            return (
                getattr(policy, "id", None) == pinned_tool
                and getattr(policy, "max_calls_per_run", 0) == 0
            )
        except Exception:
            return False

    async def _current_sources(self, owner: tuple[str, str]) -> bool:
        grants = getattr(self.schedule, "sources", None)
        if grants is None:
            return False
        calendar_ids: tuple[Any, ...] = tuple(cast(Any, getattr(grants, "calendar_ids", ())))
        if calendar_ids:
            connection = getattr(self.calendar, "connection", None)
            discover = getattr(self.calendar, "discover", None)
            if not callable(connection) or not callable(discover):
                return False
            try:
                current = connection(owner)
                if inspect.isawaitable(current):
                    current = await current
                if getattr(
                    getattr(current, "status", None), "value", None
                ) != "connected" or not getattr(current, "reads_enabled", False):
                    return False
                page = discover(owner, limit=100)
                if inspect.isawaitable(page):
                    page = await page
                selected = {
                    getattr(item, "id", None)
                    for item in getattr(page, "items", ())
                    if getattr(item, "selected", False)
                }
                if any(calendar_id not in selected for calendar_id in calendar_ids):
                    return False
            except Exception:
                return False
        getter = getattr(self.documents, "get", None)
        for document_id in tuple(getattr(grants, "document_ids", ())):
            if not callable(getter):
                return False
            try:
                document = getter(owner[0], owner[1], document_id)
                if inspect.isawaitable(document):
                    document = await document
                if (
                    document is None
                    or getattr(getattr(document, "status", None), "value", None) != "ready"
                ):
                    return False
            except Exception:
                return False
        return True

    async def revalidate(self, request: object) -> bool:
        owner = getattr(self.schedule, "owner", None)
        grants = getattr(self.schedule, "sources", None)
        selection = getattr(request, "sources", None)
        if selection is None and hasattr(request, "context") and grants is not None:
            # Execution requests carry the already-authorized context rather
            # than a second source-selection DTO.  Reuse the schedule's
            # immutable selection only after context admission has succeeded.
            selection = BriefingSourceSelection.from_grants(grants)
        if (
            owner is None
            or getattr(self.occurrence, "owner", None) != owner
            or getattr(self.schedule, "status", None) is not BriefingScheduleStatus.ACTIVE
        ):
            return False
        if getattr(request, "owner", None) != owner or grants is None or selection is None:
            return False
        if tuple(getattr(selection, "calendar_ids", ())) != tuple(
            getattr(grants, "calendar_ids", ())
        ) or tuple(getattr(selection, "document_ids", ())) != tuple(
            getattr(grants, "document_ids", ())
        ):
            return False
        if not await self._current_agent_and_policies(owner) or not await self._current_sources(
            owner
        ):
            return False
        execution = getattr(self.schedule, "execution_policies", None)
        request_policy = getattr(request, "policy_revision", None)
        if request_policy is None:
            request_policy = getattr(getattr(request, "policy", None), "policy_revision", None)
        if execution is None or request_policy != BRIEFING_EXECUTION_POLICY_REVISION:
            return False
        # Context admission carries only the source/policy revision snapshot;
        # the complete immutable revision tuple is checked again on the
        # execution request immediately before durable run admission.
        if not hasattr(request, "agent_revision_id"):
            return True
        return (
            getattr(request, "agent_revision_id", None)
            == getattr(self.schedule, "agent_revision_id", None)
            and getattr(request, "model_policy_revision_id", None)
            == GENERAL_POLICY_ID
            and getattr(request, "prompt_bundle_revision_id", None)
            == getattr(execution, "prompt_bundle_revision_id", None)
            and getattr(request, "tool_policy_revision_id", None)
            == getattr(execution, "tool_policy_revision_id", None)
            and getattr(request, "execution_policy_revision_id", None)
            == BRIEFING_EXECUTION_POLICY_ID
        )


class BriefingExecutionApplication:
    """Concrete briefing command, settlement, delivery, and recovery seam."""

    def __init__(
        self,
        service: BriefingApplicationService,
        *,
        calendar: object,
        reminders: object,
        documents: object,
        agent_service: object,
        coordinator: object,
        notifications: object,
        model_id: str,
        metrics: MetadataMetrics,
        state: BriefingRunStateAdapter | None = None,
        run_evidence: object | None = None,
    ) -> None:
        if not model_id:
            raise RuntimeError("briefing model selection is unavailable")
        self.service = service
        self.calendar = calendar
        self.reminders = reminders
        self.documents = documents
        self.agent_service = agent_service
        self.coordinator = RunCoordinatorBriefingAdapter(coordinator)
        self.notifications = BriefingDelivery(cast(Any, notifications), metrics=metrics)
        self.model_id = model_id
        self.metrics = metrics
        self.run_evidence = run_evidence
        self.state = state or BriefingRunStateAdapter(service, run_evidence=run_evidence)

    async def process_command(self, command: OutboxCommand) -> object:
        schedule_id, occurrence_id = validate_briefing_command(command)
        occurrence = await self.service.occurrence_by_id(occurrence_id)
        if occurrence.schedule_id != schedule_id:
            raise RuntimeError("briefing command lineage is invalid")
        schedule = await self.service.get(occurrence.issuer, occurrence.subject, schedule_id)
        validator = BriefingAdmissionRevalidator(
            schedule,
            occurrence,
            agent_service=self.agent_service,
            calendar=self.calendar,
            documents=self.documents,
        )
        lineage = BriefingRunLineage.for_occurrence(schedule_id, occurrence_id)
        evidence = (
            BriefingRunEvidenceAdapter(self.run_evidence, lineage.run_id)
            if self.run_evidence is not None
            else None
        )
        context_request = BriefingContextRequest(
            owner=occurrence.owner,
            local_date=occurrence.due_local_date,
            time_zone=occurrence.time_zone,
            sources=BriefingSourceSelection.from_grants(schedule.sources),
            policy_revision=BRIEFING_EXECUTION_POLICY_REVISION,
            deadline_monotonic=monotonic() + 300.0,
            trace_id=lineage.run_id.hex,
        )
        assembler = BriefingContextAssembler(
            cast(Any, self.calendar),
            cast(Any, self.reminders),
            cast(Any, self.documents),
            authorization=validator,
            metrics=self.metrics,
            evidence=evidence,
        )
        execution = BriefingExecutionService(
            PublicBriefingRunRepository(self.state),
            coordinator=self.coordinator,
            authorization=validator,
            metrics=self.metrics,
            evidence=evidence,
        )

        def request_factory(context: BriefingContext) -> BriefingRunRequest:
            return BriefingRunRequest(
                owner=occurrence.owner,
                lineage=lineage,
                agent_revision_id=schedule.agent_revision_id,
                model_policy_revision_id=schedule.execution_policies.model_policy_revision_id,
                tool_policy_revision_id=schedule.execution_policies.tool_policy_revision_id,
                model_id=self.model_id,
                instructions=(
                    "Prepare the owner's scheduled daily briefing from the supplied evidence."
                ),
                context=context,
                policy=BriefingExecutionPolicy(
                    policy_revision=BRIEFING_EXECUTION_POLICY_REVISION
                ),
                prompt_bundle_revision_id=schedule.execution_policies.prompt_bundle_revision_id,
                execution_policy_revision_id=schedule.execution_policies.execution_policy_revision_id,
            )

        result = await assemble_and_execute(assembler, context_request, execution, request_factory)
        if (
            result.status in {BriefingRunStatus.COMPLETED, BriefingRunStatus.PARTIAL}
            and result.durably_settled
        ):
            await self.notifications.deliver(
                BriefingDeliveryRequest(
                    occurrence.issuer,
                    occurrence.subject,
                    schedule_id,
                    occurrence_id,
                    _result_id(occurrence_id),
                    trace_id=result.lineage.run_id.hex,
                    parent_span_id=briefing_phase_span_id(result.lineage.run_id, "persistence"),
                    evidence=evidence,
                )
            )
        return result

    async def fail_command(self, command: OutboxCommand, *, error_class: str) -> BriefingRunResult:
        schedule_id, occurrence_id = validate_briefing_command(command)
        occurrence = await self.service.occurrence_by_id(occurrence_id)
        if occurrence.schedule_id != schedule_id:
            raise RuntimeError("briefing command lineage is invalid")
        result = await self.state.settle_unknown_occurrence(occurrence, error_class=error_class)
        if result is None:
            raise RuntimeError("briefing command cannot be durably settled")
        return result

    async def recover_stale_claims(self, *, limit: int) -> object:
        occurrences = await self.service.recover_stale_claims(limit=limit)
        settled: list[BriefingRunResult] = []
        for occurrence in cast(tuple[object, ...], occurrences):
            result = await self.state.settle_unknown_occurrence(
                occurrence, error_class="stale_claim"
            )
            if result is not None:
                settled.append(result)
        return tuple(settled)

    async def cancel(self, lineage: BriefingRunLineage) -> None:
        """Settle the typed briefing cancellation callback.

        ``RunCoordinator.cancel_briefing`` invokes this composition callback
        while the public cancellation adapter owns the coordinator boundary.
        Calling the generic coordinator ``cancel`` here would target a
        conversation Run that scheduled briefings deliberately never create.
        """

        settled = await self.state.cancel_briefing_run(lineage)
        if not settled.durably_settled or settled.status is not BriefingRunStatus.CANCELED:
            raise RuntimeError("briefing cancellation was not durably settled")


__all__ = [
    "BriefingAdmissionRevalidator",
    "BriefingExecutionApplication",
    "BriefingRunStateAdapter",
]
