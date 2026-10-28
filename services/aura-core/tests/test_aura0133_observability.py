"""Metadata-only observability evaluations for scheduled briefings."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4, uuid5

import pytest
import yaml
from aura_core.domains.automation.briefings.application import (
    BriefingExecutionApplication,
    BriefingRunStateAdapter,
)
from aura_core.domains.automation.briefings.context import (
    BriefingAuthorizationError,
    BriefingContext,
    BriefingContextAssembler,
    BriefingContextRequest,
    BriefingEvidenceItem,
    BriefingEvidenceRejected,
    BriefingSourceSelection,
    SourceCompleteness,
    serialize_briefing_run_evidence,
)
from aura_core.domains.automation.briefings.delivery import (
    BriefingDelivery,
    BriefingDeliveryRequest,
)
from aura_core.domains.automation.briefings.execution import (
    BRIEFING_EXECUTION_POLICY_ID,
    BriefingExecutionError,
    BriefingExecutionPolicy,
    BriefingExecutionService,
    BriefingRunCancellationAdapter,
    BriefingRunEvidenceAdapter,
    BriefingRunHandle,
    BriefingRunLineage,
    BriefingRunRequest,
    BriefingRunResult,
    BriefingRunStatus,
    PublicBriefingRunRepository,
)
from aura_core.domains.automation.briefings.public import (
    BRIEFING_ID_NAMESPACE,
    briefing_phase_span_id,
)
from aura_core.domains.automation.briefings.recovery import BriefingRecovery
from aura_core.domains.automation.briefings.scheduler import (
    BriefingScheduleScanResult,
)
from aura_core.domains.automation.briefings.scheduler import (
    run_once as scheduler_run_once,
)
from aura_core.domains.automation.briefings.worker import BRIEFING_EVENT_TOPIC
from aura_core.domains.automation.notifications.public import (
    BriefingNotificationSource,
    Notification,
    NotificationStatus,
)
from aura_core.domains.execution.runs.public import RunCoordinator, RunEvidenceItem
from aura_core.domains.interaction.agents.public import GENERAL_POLICY_ID
from aura_core.platform.outbox import make_aggregate_command
from aura_core.platform.outbox.nats import BRIEFING_EVENT_SUBJECT, NatsBriefingConsumer
from aura_core.platform.run_evidence import (
    BoundedRunEvidenceRecorder,
    DisabledRunEvidenceRecorder,
    RunEvidencePolicy,
)
from aura_core.platform.telemetry import MetadataMetrics

ROOT = Path(__file__).parents[1]


def _notification() -> Notification:
    now = datetime.now(UTC)
    return Notification(
        uuid4(),
        "https://authentik.example",
        "briefing-owner",
        BriefingNotificationSource("briefing", uuid4(), uuid4(), uuid4()),
        NotificationStatus.DELIVERED,
        1,
        now,
        now,
        now,
    )


def _context() -> BriefingContext:
    now = datetime.now(UTC)
    return BriefingContext(
        serialized="<AURA_UNTRUSTED_EVIDENCE></AURA_UNTRUSTED_EVIDENCE>",
        evidence=(),
        reports=(),
        completeness=SourceCompleteness.COMPLETE,
        source_provenance=(),
        assembled_at=now,
        policy_revision="briefing-execution-v1",
    )
EXPECTED = {
    "briefing-scheduler.yaml": (
        "aura.automation.briefing_scheduler",
        {
            "briefing_schedule_lag_ms",
            "briefing_schedule_claim_duration_ms",
            "briefing_outcome_count",
            "consumer_received",
            "consumer_acknowledged",
            "consumer_nacked",
            "consumer_rejected",
            "consumer_redelivered",
            "outbox_duplicates",
            "errors",
        },
        {"briefing.schedule-admission.v1"},
    ),
    "briefing-admission.yaml": (
        "aura.execution.briefing_admission",
        {
            "briefing_admission_duration_ms",
            "briefing_budget_exhaustion_count",
            "briefing_outcome_count",
        },
        {"briefing.authorization.v1", "briefing.execution.v1"},
    ),
    "briefing-context.yaml": (
        "aura.execution.briefing_context",
        {"briefing_context_assembly_duration_ms", "briefing_outcome_count"},
        {"briefing.source-completeness.v1"},
    ),
    "briefing-persistence.yaml": (
        "aura.automation.briefing_persistence",
        {"briefing_result_persistence_duration_ms", "briefing_outcome_count"},
        {"briefing.result-persistence.v1"},
    ),
    "briefing-cancellation.yaml": (
        "aura.automation.briefing_cancellation",
        {"briefing_cancellation_duration_ms", "briefing_outcome_count"},
        {"briefing.cancellation.v1"},
    ),
    "briefing-delivery.yaml": (
        "aura.automation.briefing_delivery",
        {"briefing_delivery_duration_ms", "briefing_outcome_count"},
        {"briefing.delivery.v1"},
    ),
}

BRIEFING_FIXTURE_REVISION = "aura-0133-v1"
BRIEFING_EVALUATOR_COMPONENTS = {
    "briefing.schedule-admission.v1": ("aura.automation.briefing_scheduler", "1.0.0"),
    "briefing.authorization.v1": ("aura.execution.briefing_context", "1.0.0"),
    "briefing.source-completeness.v1": ("aura.execution.briefing_context", "1.0.0"),
    "briefing.execution.v1": ("aura.execution.briefing_admission", "1.0.0"),
    "briefing.result-persistence.v1": ("aura.automation.briefing_persistence", "1.0.0"),
    "briefing.cancellation.v1": ("aura.automation.briefing_cancellation", "1.0.0"),
    "briefing.delivery.v1": ("aura.automation.briefing_delivery", "1.0.0"),
}
BRIEFING_CONTENT_FIELDS = frozenset(
    {"content", "prompt", "response", "owner", "owner_subject", "credential"}
)


@dataclass(frozen=True)
class BriefingEvaluatorResult:
    """Stable, content-free result from a repository-local briefing fixture."""

    evaluator_id: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    scenario_id: str
    case_id: str
    score: bool
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, object]:
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


def _briefing_result(
    evaluator_id: str, case_id: str, score: bool, metadata: Mapping[str, str]
) -> BriefingEvaluatorResult:
    component_id, component_version = BRIEFING_EVALUATOR_COMPONENTS[evaluator_id]
    bounded_metadata = tuple(sorted(metadata.items()))
    assert not BRIEFING_CONTENT_FIELDS.intersection(metadata)
    assert all(len(key) <= 32 and len(value) <= 64 for key, value in bounded_metadata)
    return BriefingEvaluatorResult(
        evaluator_id,
        component_id,
        component_version,
        BRIEFING_FIXTURE_REVISION,
        f"{evaluator_id}:{component_id}@{component_version}:{BRIEFING_FIXTURE_REVISION}:{case_id}",
        case_id.split(".", 1)[0],
        case_id,
        score,
        "pass" if score else "fail",
        bounded_metadata,
    )


async def _evaluate_schedule_admission() -> BriefingEvaluatorResult:
    class Application:
        async def poll_due(self, *, limit: int) -> BriefingScheduleScanResult:
            return BriefingScheduleScanResult(claimed_count=1, admitted_count=1, due_lag_ms=4.0)

    accepted = await scheduler_run_once(Application())
    bounded = False
    try:
        await scheduler_run_once(Application(), batch_size=101)
    except ValueError:
        bounded = True
    return _briefing_result(
        "briefing.schedule-admission.v1",
        "schedule-admission.success-and-bounded-scan",
        accepted.admitted_count == 1 and bounded,
        {"admitted": str(accepted.admitted_count), "batch_bound": str(bounded).lower()},
    )


async def _evaluate_authorization() -> BriefingEvaluatorResult:
    reads = 0

    class Revoked:
        async def revalidate(self, request: BriefingContextRequest) -> bool:
            del request
            return False

    class MustNotRead:
        async def events(self, *args: Any, **kwargs: Any) -> object:
            nonlocal reads
            reads += 1
            del args, kwargs
            raise AssertionError("revoked source was read")

    revoked = False
    try:
        await BriefingContextAssembler(
            calendar=cast(Any, MustNotRead()), authorization=Revoked()
        ).assemble(
            BriefingContextRequest(
                owner=("https://authentik.example", "briefing-owner"),
                local_date=date(2026, 1, 1),
                time_zone="UTC",
                sources=BriefingSourceSelection(calendar_ids=(uuid4(),)),
                policy_revision="briefing-execution-v1",
                deadline_monotonic=monotonic() + 10,
                trace_id="a" * 32,
            )
        )
    except BriefingAuthorizationError:
        revoked = True
    return _briefing_result(
        "briefing.authorization.v1",
        "authorization.revocation-before-source-read",
        revoked and reads == 0,
        {"revoked": str(revoked).lower(), "source_reads": str(reads)},
    )


async def _evaluate_source_completeness() -> BriefingEvaluatorResult:
    class Allowed:
        async def revalidate(self, request: BriefingContextRequest) -> bool:
            del request
            return True

    assembler = BriefingContextAssembler(authorization=Allowed())
    def request(sources: BriefingSourceSelection) -> BriefingContextRequest:
        return BriefingContextRequest(
            owner=("https://authentik.example", "briefing-owner"),
            local_date=date(2026, 1, 1),
            time_zone="UTC",
            sources=sources,
            policy_revision="briefing-execution-v1",
            deadline_monotonic=monotonic() + 10,
            trace_id="b" * 32,
        )

    unknown = await assembler.assemble(request(BriefingSourceSelection()))
    unavailable = await BriefingContextAssembler(authorization=Allowed()).assemble(
        request(BriefingSourceSelection(calendar_ids=(uuid4(),)))
    )
    return _briefing_result(
        "briefing.source-completeness.v1",
        "source-completeness.complete-and-unavailable",
        unknown.completeness
        in {SourceCompleteness.UNKNOWN, SourceCompleteness.UNAVAILABLE}
        and unavailable.completeness is SourceCompleteness.UNAVAILABLE,
        {
            "empty": unknown.completeness.value,
            "unavailable": unavailable.completeness.value,
        },
    )


async def _evaluate_execution() -> BriefingEvaluatorResult:
    class Repository:
        async def admit(self, request: BriefingRunRequest) -> BriefingRunHandle:
            return BriefingRunHandle(request.lineage)

        async def settle(self, result: BriefingRunResult) -> BriefingRunResult:
            return BriefingRunResult(
                result.lineage,
                result.status,
                result.source_completeness,
                result.content,
                result.started_at,
                result.finished_at,
                result.error_class,
                result.attempt,
                settled=True,
            )

        async def cancel(self, lineage: BriefingRunLineage) -> BriefingRunResult:
            del lineage
            raise AssertionError("cancellation was not requested")

    class Allowed:
        async def revalidate(self, request: BriefingRunRequest) -> bool:
            del request
            return True

    class Coordinator:
        async def execute_briefing(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            del args, kwargs
            yield "briefing"

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    lineage = BriefingRunLineage.for_occurrence(uuid4(), uuid4())
    request = BriefingRunRequest(
        owner=("https://authentik.example", "briefing-owner"),
        lineage=lineage,
        agent_revision_id=uuid4(),
        model_policy_revision_id=uuid4(),
        tool_policy_revision_id=uuid4(),
        model_id="model-v1",
        instructions="Summarize the bounded evidence.",
        context=_context(),
        policy=BriefingExecutionPolicy(),
        prompt_bundle_revision_id=uuid4(),
        execution_policy_revision_id=BRIEFING_EXECUTION_POLICY_ID,
    )
    result = await BriefingExecutionService(
        cast(Any, Repository()), coordinator=cast(Any, Coordinator()), authorization=Allowed()
    ).execute(request)
    return _briefing_result(
        "briefing.execution.v1",
        "execution.success-and-durable-settlement",
        result.status is BriefingRunStatus.COMPLETED and result.durably_settled,
        {"status": result.status.value, "settled": str(result.durably_settled).lower()},
    )


async def _evaluate_persistence() -> BriefingEvaluatorResult:
    from aura_core.domains.automation.briefings.public import (
        BriefingDailyCadence,
        BriefingExecutionPolicyReferences,
        BriefingResult,
        BriefingResultStatus,
        BriefingSourceCompleteness,
        BriefingSourceGrants,
    )
    from aura_core.domains.automation.briefings.repository import MemoryBriefingRepository

    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    repository = MemoryBriefingRepository()
    schedule = await repository.create_schedule(
        "https://authentik.example",
        "briefing-owner",
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
        agent_revision_id=uuid4(),
        execution_policies=BriefingExecutionPolicyReferences(uuid4(), uuid4(), uuid4(), uuid4()),
        idempotency_key="evaluator-schedule",
        now=now,
    )
    occurrence = (
        await repository.list_occurrences(
            "https://authentik.example", "briefing-owner", schedule.id
        )
    ).items[0]
    result = BriefingResult(
        uuid4(), schedule.id, occurrence.id, "https://authentik.example", "briefing-owner", uuid4(),
        BriefingResultStatus.PARTIAL, 1, BriefingSourceCompleteness.PARTIAL,
        "private result fixture", now, now,
    )
    first = await repository.create_result(result)
    duplicate = await repository.create_result(result)
    return _briefing_result(
        "briefing.result-persistence.v1",
        "persistence.duplicate-settlement-and-private-content",
        first == duplicate,
        {"duplicate": str(first == duplicate).lower(), "version": str(first.version)},
    )


async def _evaluate_cancellation() -> BriefingEvaluatorResult:
    class Coordinator:
        async def cancel(self, run_id: object, subject: str, issuer: str, key: str) -> str:
            assert run_id and subject and issuer and key
            return "canceled"

    result = await BriefingRecovery(cast(Any, object()), Coordinator()).cancel_run(
        uuid4(),
        issuer="https://authentik.example",
        subject="briefing-owner",
        idempotency_key="cancel",
    )
    return _briefing_result(
        "briefing.cancellation.v1",
        "cancellation.terminal-settlement",
        result == "canceled",
        {"outcome": str(result)},
    )


async def _evaluate_delivery() -> BriefingEvaluatorResult:
    calls: list[Any] = []

    class Notifications:
        async def create_briefing_notification(self, command: Any) -> Notification:
            calls.append(command)
            return _notification()

    request = BriefingDeliveryRequest(
        "https://authentik.example", "briefing-owner", uuid4(), uuid4(), uuid4()
    )
    await BriefingDelivery(Notifications()).deliver(request)
    await BriefingDelivery(Notifications()).deliver(request)
    return _briefing_result(
        "briefing.delivery.v1",
        "delivery.replay-idempotency-and-content-free-command",
        len(calls) == 2 and calls[0].idempotency_key == calls[1].idempotency_key
        and not hasattr(calls[0], "content"),
        {"replays": str(len(calls)), "idempotency": "stable"},
    )


BRIEFING_EVALUATOR_BINDINGS = {
    "briefing.schedule-admission.v1": _evaluate_schedule_admission,
    "briefing.authorization.v1": _evaluate_authorization,
    "briefing.source-completeness.v1": _evaluate_source_completeness,
    "briefing.execution.v1": _evaluate_execution,
    "briefing.result-persistence.v1": _evaluate_persistence,
    "briefing.cancellation.v1": _evaluate_cancellation,
    "briefing.delivery.v1": _evaluate_delivery,
}


async def _all_briefing_evaluator_results() -> tuple[BriefingEvaluatorResult, ...]:
    results: list[BriefingEvaluatorResult] = []
    for evaluator in sorted(BRIEFING_EVALUATOR_BINDINGS):
        results.append(await BRIEFING_EVALUATOR_BINDINGS[evaluator]())
    return tuple(results)


@pytest.mark.asyncio
async def test_evaluator_bindings_execute_replayable_structured_results() -> None:
    """These are repository evaluation bindings, not a production evaluator registry."""

    first = await _all_briefing_evaluator_results()
    second = await _all_briefing_evaluator_results()
    assert first == second
    assert {item.evaluator_id for item in first} == set(BRIEFING_EVALUATOR_COMPONENTS)
    assert all(item.score and item.outcome == "pass" for item in first), [
        (item.evaluator_id, item.score, dict(item.metadata))
        for item in first
        if not item.score
    ]
    assert len({item.replay_key for item in first}) == len(first)
    for item in first:
        payload = item.payload()
        assert set(payload) == {
            "evaluatorId", "componentId", "componentVersion", "fixtureRevision",
            "replayKey", "scenarioId", "caseId", "score", "outcome", "metadata",
        }
        assert not BRIEFING_CONTENT_FIELDS.intersection(payload)
        assert not BRIEFING_CONTENT_FIELDS.intersection(
            cast(Mapping[str, object], payload["metadata"])
        )


def test_all_declared_briefing_components_are_metadata_only_and_complete() -> None:
    for filename, (component_id, metrics, evaluators) in EXPECTED.items():
        manifest = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
        assert manifest["component"]["id"] == component_id
        assert manifest["capture_policy"] == "metadata_only"
        assert set(manifest["metrics"]) == metrics
        assert set(manifest["evaluators"]) == evaluators
        prohibited = {str(value).lower() for value in manifest["cardinality"]["prohibited"]}
        assert {"content", "prompt", "response", "token", "credential"} <= prohibited
        assert "owner_identity" in prohibited
        assert "owner_subject" in prohibited


def test_briefing_metric_dimensions_are_low_cardinality_and_trace_only_ids() -> None:
    allowed = {"operation", "outcome", "error_class", "dependency", "status"}
    for filename in EXPECTED:
        manifest = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
        dimensions = set(manifest["cardinality"]["metric_attributes"])
        assert dimensions <= allowed
        for value in manifest["cardinality"]["trace_only"]:
            assert "content" not in value.lower()
            assert "owner" not in value.lower()
            assert "token" not in value.lower()


def test_all_seven_briefing_evaluator_behaviors_are_declared() -> None:
    evaluators = {
        evaluator
        for _component, _metrics, declared in EXPECTED.values()
        for evaluator in declared
    }
    assert evaluators == {
        "briefing.schedule-admission.v1",
        "briefing.authorization.v1",
        "briefing.source-completeness.v1",
        "briefing.execution.v1",
        "briefing.result-persistence.v1",
        "briefing.cancellation.v1",
        "briefing.delivery.v1",
    }


def test_disabled_adr0017_capture_is_inert_and_content_free() -> None:
    recorder = DisabledRunEvidenceRecorder()
    assert recorder.enabled is False
    assert recorder.record(cast(Any, {"content": "private"})) is False
    assert recorder.allocate_sequence(uuid4()) == 0


@pytest.mark.asyncio
async def test_metadata_export_failure_is_bounded_and_never_blocks_the_application() -> None:
    class FailingExporter:
        async def export(self, batch: object) -> None:
            del batch
            raise RuntimeError("telemetry sink unavailable")

    metrics = MetadataMetrics(exporter=cast(Any, FailingExporter()), max_export_attempts=1)
    metrics.increment(
        "aura.automation.briefing_delivery",
        "briefing_outcome_count",
        outcome="ok",
    )
    assert await metrics.flush() is False
    assert metrics.stats().export_failures == 1
    assert metrics.stats().abandoned_batches == 1


@pytest.mark.asyncio
async def test_scheduler_runtime_emits_bounded_metadata_and_trace_only_correlation() -> None:
    class Application:
        async def poll_due(self, *, limit: int) -> BriefingScheduleScanResult:
            assert limit == 100
            return BriefingScheduleScanResult(
                claimed_count=1, admitted_count=1, due_lag_ms=12.0
            )

    metrics = MetadataMetrics(capacity=32)
    await scheduler_run_once(Application(), metrics=metrics)
    items = [
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.automation.briefing_scheduler"
    ]
    assert {item.metric for item in items} >= {
        "briefing_schedule_lag_ms",
        "briefing_outcome_count",
    }
    assert all(
        set(dict(item.dimensions)) <= {"dependency", "outcome", "error_class"}
        for item in items
    )
    assert all(
        key not in repr(item).lower()
        for item in items
        for key in ("content", "prompt", "response", "owner_subject", "credential")
    )


@pytest.mark.asyncio
async def test_context_runtime_emits_completion_and_closed_evidence() -> None:
    metrics = MetadataMetrics(capacity=32)
    request = BriefingContextRequest(
        owner=("https://authentik.example", "briefing-owner"),
        local_date=date(2026, 1, 1),
        time_zone="UTC",
        sources=BriefingSourceSelection(),
        policy_revision="briefing-execution-v1",
        deadline_monotonic=monotonic() + 10,
        trace_id="a" * 32,
    )
    class Authorization:
        async def revalidate(self, request: BriefingContextRequest) -> bool:
            del request
            return True

    context = await BriefingContextAssembler(
        metrics=metrics, authorization=Authorization()
    ).assemble(request)
    spans = [
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.execution.briefing_context"
    ]
    assert any(item.metric == "briefing_context_assembly_duration_ms" for item in spans)
    assert all("source_value" not in repr(item).lower() for item in spans)
    span = next(item for item in spans if item.kind == "span")
    assert context.span_id == briefing_phase_span_id("a" * 32, "context")
    assert span.parent_span_id is None


@pytest.mark.asyncio
async def test_persistence_and_nats_runtime_metrics_are_registered_and_nonblocking() -> None:
    """The real persistence and briefing consumer bindings must not silently reject health."""

    from aura_core.domains.automation.briefings.public import (
        BriefingDailyCadence,
        BriefingExecutionPolicyReferences,
        BriefingResult,
        BriefingResultStatus,
        BriefingSourceCompleteness,
        BriefingSourceGrants,
    )
    from aura_core.domains.automation.briefings.repository import MemoryBriefingRepository

    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    metrics = MetadataMetrics(capacity=64)
    repository = MemoryBriefingRepository(metrics=metrics)
    schedule = await repository.create_schedule(
        "https://authentik.example",
        "briefing-owner",
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
        agent_revision_id=uuid4(),
        execution_policies=BriefingExecutionPolicyReferences(uuid4(), uuid4(), uuid4(), uuid4()),
        idempotency_key="metrics-schedule",
        now=now,
    )
    occurrence = (await repository.list_occurrences(
        "https://authentik.example", "briefing-owner", schedule.id
    )).items[0]
    result = BriefingResult(
        uuid4(),
        schedule.id,
        occurrence.id,
        "https://authentik.example",
        "briefing-owner",
        uuid4(),
        BriefingResultStatus.PARTIAL,
        1,
        BriefingSourceCompleteness.PARTIAL,
        "private result must not enter metadata",
        now,
        now,
    )
    await repository.create_result(result)
    await repository.create_result(result)

    class Metadata:
        num_delivered = 2

    class Message:
        data = json.dumps(
            make_aggregate_command(
                BRIEFING_EVENT_SUBJECT,
                uuid4(),
                "automation.briefing",
                schedule.id,
                identifiers=(
                    ("eventId", uuid4()),
                    ("scheduleId", schedule.id),
                    ("occurrenceId", occurrence.id),
                    ("occurrenceStatus", "admitted"),
                ),
            ).payload()
        ).encode()
        metadata = Metadata()

        async def nak(self, **kwargs: object) -> None:
            self.nak_args = kwargs

    class Subscription:
        async def fetch(self, count: int, timeout: float) -> list[Message]:
            assert count == 1
            assert timeout == 30
            return [Message()]

    consumer = NatsBriefingConsumer("nats://unused", metrics=metrics)
    cast(Any, consumer)._subscription = Subscription()
    assert await consumer.receive() is not None
    await consumer.nack(retry_delay=1.0, error_class="delivery")

    class BadSubscription:
        async def fetch(self, count: int, timeout: float) -> list[Message]:
            del count, timeout
            bad = Message()
            bad.data = b"{}"
            return [bad]

    cast(Any, consumer)._subscription = BadSubscription()
    with pytest.raises(ValueError, match="invalid generic command metadata"):
        await consumer.receive()

    assert metrics.stats().rejected == 0
    metric_names = {item.metric for item in metrics.snapshot()}
    assert "briefing_result_persistence_duration_ms" in metric_names
    assert {
        "consumer_received",
        "consumer_redelivered",
        "outbox_duplicates",
        "consumer_nacked",
        "errors",
    } <= metric_names


@pytest.mark.asyncio
async def test_delivery_runtime_is_idempotent_in_shape_and_correlated_by_trace_only_ids() -> None:
    calls: list[Any] = []

    class Notifications:
        async def create_briefing_notification(self, command: Any) -> Notification:
            calls.append(command)
            return _notification()

    request = BriefingDeliveryRequest(
        "https://authentik.example", "briefing-owner", uuid4(), uuid4(), uuid4()
    )
    metrics = MetadataMetrics(capacity=32)
    await BriefingDelivery(Notifications(), metrics=metrics).deliver(request)
    await BriefingDelivery(Notifications(), metrics=metrics).deliver(request)
    assert len(calls) == 2
    assert calls[0].idempotency_key == calls[1].idempotency_key
    span = next(
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.automation.briefing_delivery"
        and item.kind == "span"
    )
    trace = dict(span.trace_attributes)
    assert trace["briefing_occurrence_id"] == str(request.occurrence_id)
    assert trace["briefing_result_id"] == str(request.result_id)
    assert "owner_subject" not in trace


@pytest.mark.asyncio
async def test_cancellation_runtime_emits_terminal_outcome_without_private_dimensions() -> None:
    class Coordinator:
        async def cancel(self, run_id: object, subject: str, issuer: str, key: str) -> str:
            assert run_id and subject and issuer and key
            return "canceled"

    metrics = MetadataMetrics(capacity=32)
    await BriefingRecovery(
        cast(Any, object()), cast(Any, Coordinator()), metrics=metrics
    ).cancel_run(
        uuid4(),
        issuer="https://authentik.example",
        subject="briefing-owner",
        idempotency_key="cancel",
    )
    span = next(
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.automation.briefing_cancellation"
    )
    assert dict(span.dimensions)["outcome"] == "ok"
    assert metrics.stats().rejected == 0
    assert "owner_subject" not in repr(span).lower()


def test_briefing_adr0017_serializer_is_closed_and_rejects_private_fields() -> None:
    item = BriefingEvidenceItem(
        phase="execution",
        outcome="completed",
        source_completeness="complete",
        trace_id="a" * 32,
        run_id=uuid4(),
    )
    payload = json.loads(serialize_briefing_run_evidence(item))
    assert set(payload) == {
        "kind",
        "phase",
        "outcome",
        "sourceCompleteness",
        "errorClass",
        "traceId",
        "runId",
    }
    assert "content" not in payload
    with pytest.raises(BriefingEvidenceRejected):
        BriefingEvidenceItem("execution", "running")
    with pytest.raises(BriefingEvidenceRejected):
        serialize_briefing_run_evidence({"phase": "execution", "content": "private"})


def test_briefing_evidence_adapter_composes_with_closed_run_evidence_port() -> None:
    captured: list[object] = []
    evidence_run_id = uuid4()

    class Recorder:
        enabled = True

        def allocate_sequence(self, run_id: object) -> int:
            assert run_id == evidence_run_id
            return 7

        def record(self, item: object) -> bool:
            captured.append(item)
            return True

    adapter = BriefingRunEvidenceAdapter(Recorder(), evidence_run_id)
    assert adapter.record(
        BriefingEvidenceItem(
            phase="execution",
            outcome="completed",
            source_completeness="complete",
            trace_id="b" * 32,
            run_id=evidence_run_id,
        )
    )
    assert len(captured) == 1
    item = captured[0]
    assert isinstance(item, RunEvidenceItem)
    assert item.evidence_kind == "execution_outcome"
    assert set(item.payload or {}) == {"kind", "outcome", "detail"}
    rendered = repr(item).lower()
    assert all(value not in rendered for value in ("private", "prompt", "owner_subject"))


def test_briefing_evidence_adapter_accepts_all_authorized_phases() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True))
    adapter = BriefingRunEvidenceAdapter(recorder, uuid4())
    assert all(
        adapter.record_phase(phase, outcome)
        for phase, outcome in (
            ("context", "complete"),
            ("admission", "admitted"),
            ("execution", "partial"),
            ("persistence", "completed"),
            ("delivery", "canceled"),
        )
    )
    assert cast(Any, recorder)._queue.qsize() == 5


def test_worker_composition_injects_run_evidence_into_briefing_state() -> None:
    class Coordinator:
        def execute_briefing(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            del args, kwargs

            async def stream() -> AsyncIterator[str]:
                yield "briefing"

            return stream()

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    recorder = object()
    service = cast(Any, type("Service", (), {"repository": object()})())
    application = BriefingExecutionApplication(
        service,
        calendar=object(),
        reminders=object(),
        documents=object(),
        agent_service=object(),
        coordinator=Coordinator(),
        notifications=cast(Any, object()),
        model_id="model-v1",
        metrics=MetadataMetrics(capacity=16),
        run_evidence=recorder,
    )

    assert application.run_evidence is recorder
    assert isinstance(application.state, BriefingRunStateAdapter)
    assert application.state.run_evidence is recorder


async def _real_briefing_application_fixture(
    *, run_evidence: object | None, metrics: MetadataMetrics
) -> tuple[Any, Any, Any, Any, Any]:
    """Compose the public application with real in-memory durable briefing state."""

    from aura_core.domains.automation.briefings.public import (
        BriefingApplicationService,
        BriefingDailyCadence,
        BriefingExecutionPolicyReferences,
        BriefingSourceGrants,
    )
    from aura_core.domains.automation.briefings.repository import MemoryBriefingRepository
    from aura_core.domains.automation.reminders.public import ReminderPage
    from aura_core.domains.integrations.calendar.public import CalendarEventPage

    owner = ("https://authentik.example", "briefing-owner")
    calendar_id = uuid4()
    agent_revision_id = uuid4()
    prompt_bundle_revision_id = uuid4()
    tool_policy_revision_id = uuid4()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    repository = MemoryBriefingRepository(metrics=metrics)
    schedule = await repository.create_schedule(
        *owner,
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (calendar_id,), ()),
        agent_revision_id=agent_revision_id,
        execution_policies=BriefingExecutionPolicyReferences(
            BRIEFING_EXECUTION_POLICY_ID,
            prompt_bundle_revision_id,
            GENERAL_POLICY_ID,
            tool_policy_revision_id,
        ),
        idempotency_key="real-application-fixture",
        now=now,
    )
    occurrence = (await repository.list_occurrences(*owner, schedule.id)).items[0]

    class Calendar:
        def connection(self, requested_owner: tuple[str, str]) -> object:
            assert requested_owner == owner
            return SimpleNamespace(
                status=SimpleNamespace(value="connected"), reads_enabled=True
            )

        def discover(self, requested_owner: tuple[str, str], *, limit: int) -> object:
            assert requested_owner == owner
            assert limit == 100
            return SimpleNamespace(
                items=(SimpleNamespace(id=calendar_id, selected=True),)
            )

        async def events(self, *args: Any, **kwargs: Any) -> CalendarEventPage:
            del args, kwargs
            return CalendarEventPage(
                now,
                now,
                "UTC",
                (),
                None,
                False,
                (),
                now,
            )

    class Reminders:
        async def list(self, *args: Any, **kwargs: Any) -> ReminderPage:
            del args, kwargs
            return ReminderPage((), None)

    class Documents:
        async def get(self, *args: Any, **kwargs: Any) -> object:
            del args, kwargs
            raise AssertionError("no document grant should be read")

    class Agents:
        def resolve_active_revision(self, identifier: object) -> object:
            assert identifier == agent_revision_id
            return SimpleNamespace(
                id=agent_revision_id,
                model_policy_revision_id=GENERAL_POLICY_ID,
                prompt_bundle_revision_id=prompt_bundle_revision_id,
                tool_policy_revision_id=tool_policy_revision_id,
            )

        def get_tool_policy(self, issuer: str, subject: str, identifier: object) -> object:
            assert (issuer, subject, identifier) == (*owner, tool_policy_revision_id)
            return SimpleNamespace(id=tool_policy_revision_id, max_calls_per_run=0)

    class Coordinator:
        def execute_briefing(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            del args, kwargs

            async def stream() -> AsyncIterator[str]:
                yield "briefing"

            return stream()

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    class Notifications:
        def __init__(self) -> None:
            self.delivered: list[Any] = []

        async def create_briefing_notification(self, command: Any) -> Notification:
            self.delivered.append(command)
            return _notification()

    notifications = Notifications()
    service = BriefingApplicationService(repository)
    application = BriefingExecutionApplication(
        service,
        calendar=Calendar(),
        reminders=Reminders(),
        documents=Documents(),
        agent_service=Agents(),
        coordinator=Coordinator(),
        notifications=notifications,
        model_id="configured-core-model",
        metrics=metrics,
        run_evidence=run_evidence,
    )
    command_id = uuid5(BRIEFING_ID_NAMESPACE, f"wake:{occurrence.id}")
    command = make_aggregate_command(
        BRIEFING_EVENT_TOPIC,
        command_id,
        "automation.briefing",
        schedule.id,
        identifiers=(
            ("eventId", command_id),
            ("scheduleId", schedule.id),
            ("occurrenceId", occurrence.id),
            ("occurrenceStatus", "admitted"),
        ),
    )
    return application, repository, command, occurrence, notifications


@pytest.mark.asyncio
async def test_real_application_emits_correlated_phase_spans_and_all_evidence_phases() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True))
    metrics = MetadataMetrics(capacity=64)
    application, repository, command, occurrence, notifications = (
        await _real_briefing_application_fixture(run_evidence=recorder, metrics=metrics)
    )
    result = await application.process_command(command)

    assert isinstance(result, BriefingRunResult)
    assert result.durably_settled
    assert len(notifications.delivered) == 1
    stored = await repository.get_occurrence_by_id(occurrence.id)
    assert stored.result_id is not None
    phase_operations = {
        "briefing.context.assemble": "context",
        "briefing.admission": "admission",
        "briefing.result.persist": "persistence",
        "briefing.delivery": "delivery",
    }
    spans = {
        phase_operations[item.trace_attributes[0][1]]: item
        for item in metrics.snapshot()
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") in phase_operations
    }
    assert set(spans) == set(phase_operations.values())
    assert {item.trace_id for item in spans.values()} == {result.lineage.run_id.hex}
    emitted_span_ids = {item.span_id for item in spans.values()}
    assert all(
        item.parent_span_id is None or item.parent_span_id in emitted_span_ids
        for item in spans.values()
    )
    # The context assembler receives the public trace-id string, while the
    # later application phases receive the lineage UUID.  Assert the IDs at
    # the edges of the real chain through the one public helper rather than
    # coupling this evaluation to the private telemetry implementation.
    trace_id = result.lineage.run_id.hex
    assert spans["context"].span_id == briefing_phase_span_id(
        trace_id, "context"
    )
    assert spans["admission"].span_id == briefing_phase_span_id(
        result.lineage.run_id, "admission"
    )
    assert spans["persistence"].span_id == briefing_phase_span_id(
        result.lineage.run_id, "persistence"
    )
    assert spans["admission"].parent_span_id == spans["context"].span_id
    assert spans["admission"].parent_span_id == briefing_phase_span_id(
        trace_id, "context"
    )
    assert spans["persistence"].parent_span_id == spans["admission"].span_id
    assert spans["persistence"].parent_span_id == briefing_phase_span_id(
        result.lineage.run_id, "admission"
    )
    assert spans["delivery"].parent_span_id == spans["persistence"].span_id
    assert spans["delivery"].parent_span_id == briefing_phase_span_id(
        result.lineage.run_id, "persistence"
    )
    assert metrics.stats().rejected == 0

    evidence_payloads: list[dict[str, object]] = []
    queue = cast(Any, recorder)._queue
    while not queue.empty():
        evidence_payloads.append(json.loads(queue.get_nowait()))
    assert {
        str(cast(dict[str, object], payload["payload"])["detail"])
        for payload in evidence_payloads
        if isinstance(payload.get("payload"), dict)
    } == {
        "briefing.context",
        "briefing.admission",
        "briefing.execution",
        "briefing.persistence",
        "briefing.delivery",
    }


@pytest.mark.asyncio
async def test_rejecting_run_evidence_does_not_change_real_settlement_or_delivery() -> None:
    class RejectingRecorder:
        enabled = True

        def allocate_sequence(self, run_id: object) -> int:
            del run_id
            return 0

        def record(self, item: object) -> bool:
            del item
            raise RuntimeError("evidence sink unavailable")

    metrics = MetadataMetrics(capacity=64)
    application, repository, command, occurrence, notifications = (
        await _real_briefing_application_fixture(
            run_evidence=RejectingRecorder(), metrics=metrics
        )
    )
    result = await application.process_command(command)

    assert isinstance(result, BriefingRunResult)
    assert result.durably_settled
    assert result.status is BriefingRunStatus.COMPLETED
    stored = await repository.get_occurrence_by_id(occurrence.id)
    assert stored.result_id is not None
    assert stored.status.value == "completed"
    assert len(notifications.delivered) == 1
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_briefing_cancel_callback_settles_typed_state_without_generic_run_cancel() -> None:
    class Coordinator:
        def execute_briefing(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            del args, kwargs

            async def stream() -> AsyncIterator[str]:
                yield "briefing"

            return stream()

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    class State:
        def __init__(self) -> None:
            self.called = False

        async def cancel_briefing_run(self, lineage: BriefingRunLineage) -> BriefingRunResult:
            self.called = True
            now = datetime.now(UTC)
            return BriefingRunResult(
                lineage,
                BriefingRunStatus.CANCELED,
                SourceCompleteness.UNKNOWN,
                None,
                now,
                now,
                settled=True,
            )

    state = State()
    application = BriefingExecutionApplication(
        cast(Any, type("Service", (), {"repository": object()})()),
        calendar=object(),
        reminders=object(),
        documents=object(),
        agent_service=object(),
        coordinator=Coordinator(),
        notifications=cast(Any, object()),
        model_id="model-v1",
        metrics=MetadataMetrics(capacity=16),
        state=cast(Any, state),
    )
    await application.cancel(BriefingRunLineage.for_occurrence(uuid4(), uuid4()))
    assert state.called


@pytest.mark.asyncio
async def test_pre_admission_failure_gets_stable_unknown_result_lineage() -> None:
    schedule_id = uuid4()
    occurrence_id = uuid4()
    stored: list[object] = []

    class Repository:
        async def create_result(self, result: object) -> object:
            stored.append(result)
            return result

    class Service:
        repository = Repository()

        async def occurrence_by_id(self, identifier: object) -> object:
            del identifier
            return type(
                "Occurrence",
                (),
                {
                    "id": occurrence_id,
                    "schedule_id": schedule_id,
                    "owner": ("https://authentik.example", "briefing-owner"),
                    "run_id": None,
                    "started_at": None,
                },
            )()

    result = await BriefingRunStateAdapter(cast(Any, Service())).settle_unknown_occurrence(
        await Service().occurrence_by_id(occurrence_id),
        error_class="authorization",
    )

    assert result is not None
    assert result.status is BriefingRunStatus.UNKNOWN
    assert result.durably_settled
    assert result.lineage.run_id == BriefingRunLineage.for_occurrence(
        schedule_id, occurrence_id
    ).run_id
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_execution_evidence_capture_failure_is_nonblocking_and_admission_is_observable(
) -> None:
    class Repository:
        async def admit(self, request: BriefingRunRequest) -> BriefingRunHandle:
            return BriefingRunHandle(request.lineage)

        async def settle(self, result: BriefingRunResult) -> BriefingRunResult:
            return result

        async def cancel(self, lineage: BriefingRunLineage) -> None:
            del lineage

    class Authorization:
        async def revalidate(self, request: BriefingRunRequest) -> bool:
            del request
            return True

    class Coordinator:
        async def execute_briefing(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            del args, kwargs
            yield "briefing"

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    class FailingEvidence:
        def record(self, item: object) -> bool:
            del item
            raise RuntimeError("evidence exporter unavailable")

    lineage = BriefingRunLineage.for_occurrence(uuid4(), uuid4())
    request = BriefingRunRequest(
        owner=("https://authentik.example", "briefing-owner"),
        lineage=lineage,
        agent_revision_id=uuid4(),
        model_policy_revision_id=uuid4(),
        tool_policy_revision_id=uuid4(),
        model_id="model-v1",
        instructions="Summarize the evidence.",
        context=_context(),
        policy=BriefingExecutionPolicy(),
        prompt_bundle_revision_id=uuid4(),
        execution_policy_revision_id=BRIEFING_EXECUTION_POLICY_ID,
    )
    metrics = MetadataMetrics(capacity=32)
    result = await BriefingExecutionService(
        cast(Any, Repository()),
        coordinator=cast(Any, Coordinator()),
        authorization=Authorization(),
        metrics=metrics,
        evidence=FailingEvidence(),
    ).execute(request)
    assert result.status is BriefingRunStatus.COMPLETED
    assert any(
        item.metric == "briefing_admission_duration_ms"
        for item in metrics.snapshot()
        if item.component_id == "aura.execution.briefing_admission"
    )


@pytest.mark.asyncio
async def test_execution_emits_admission_before_generation_and_execution_after_settlement() -> None:
    phases: list[str] = []

    class Metrics:
        admission_observed = False

        spans: list[dict[str, object]] = []

        def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
            del value, attributes
            if component == "aura.execution.briefing_admission":
                assert name == "briefing_admission_duration_ms"
                self.admission_observed = True

        def increment(self, component: str, name: str, **attributes: str) -> None:
            del component, name, attributes

        def record_span(self, *args: object, **kwargs: object) -> None:
            del args
            self.spans.append(kwargs)

    metrics = Metrics()

    class Evidence:
        def record(self, item: BriefingEvidenceItem) -> bool:
            phases.append(item.phase)
            return True

    class Repository:
        async def admit(self, request: BriefingRunRequest) -> BriefingRunHandle:
            return BriefingRunHandle(request.lineage)

        async def settle(self, result: BriefingRunResult) -> BriefingRunResult:
            assert phases == ["admission"]
            return replace(result, settled=True)

        async def cancel(self, lineage: BriefingRunLineage) -> BriefingRunResult:
            del lineage
            raise AssertionError("cancellation was not requested")

    class Authorization:
        async def revalidate(self, request: BriefingRunRequest) -> bool:
            del request
            return True

    class Coordinator:
        def execute_briefing(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[str]:
            del args, kwargs
            assert metrics.admission_observed

            async def stream() -> AsyncIterator[str]:
                yield "briefing"

            return stream()

        async def cancel_briefing(self, lineage: BriefingRunLineage) -> None:
            del lineage

    lineage = BriefingRunLineage.for_occurrence(uuid4(), uuid4())
    request = BriefingRunRequest(
        owner=("https://authentik.example", "briefing-owner"),
        lineage=lineage,
        agent_revision_id=uuid4(),
        model_policy_revision_id=uuid4(),
        tool_policy_revision_id=uuid4(),
        model_id="model-v1",
        instructions="Summarize the evidence.",
        context=replace(
            _context(),
            trace_id=lineage.run_id.hex,
            span_id=briefing_phase_span_id(lineage.run_id, "context"),
        ),
        policy=BriefingExecutionPolicy(),
        prompt_bundle_revision_id=uuid4(),
        execution_policy_revision_id=BRIEFING_EXECUTION_POLICY_ID,
    )
    result = await BriefingExecutionService(
        cast(Any, Repository()),
        coordinator=cast(Any, Coordinator()),
        authorization=Authorization(),
        metrics=cast(Any, metrics),
        evidence=cast(Any, Evidence()),
    ).execute(request)
    assert result.status is BriefingRunStatus.COMPLETED
    assert phases == ["admission", "execution"]
    assert metrics.spans == [
        {
            "trace_id": lineage.run_id.hex,
            "span_id": briefing_phase_span_id(lineage.run_id, "admission"),
            "parent_span_id": briefing_phase_span_id(lineage.run_id, "context"),
            "dependency": "postgresql",
            "outcome": "accepted",
            "error_class": None,
            "run_id": str(lineage.run_id),
        }
    ]


@pytest.mark.asyncio
async def test_run_coordinator_fallback_enforces_token_budget() -> None:
    class Provider:
        async def stream_chat(self, model_id: str, messages: object) -> AsyncIterator[str]:
            del model_id, messages
            yield "123456789"

    coordinator = RunCoordinator(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        metrics=MetadataMetrics(capacity=32),
        briefing_provider=cast(Any, Provider()),
    )
    request = type(
        "BriefingRequest",
        (),
        {
            "model_id": "model-v1",
            "lineage": BriefingRunLineage.for_occurrence(uuid4(), uuid4()),
        },
    )()
    stream = coordinator.execute_briefing(
        request,
        (),
        deadline_monotonic=monotonic() + 10,
        cancelled=None,
        max_output_tokens=2,
    )
    with pytest.raises(RuntimeError, match="output budget exceeded"):
        async for _chunk in stream:
            pass


@pytest.mark.asyncio
async def test_run_coordinator_fallback_rejects_dense_unicode_bytes() -> None:
    class Provider:
        async def stream_chat(self, model_id: str, messages: object) -> AsyncIterator[str]:
            del model_id, messages
            # Two characters fit the fallback character ceiling for two
            # tokens, but their UTF-8 representation exceeds the byte bound.
            yield "💣💣"

    coordinator = RunCoordinator(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        metrics=MetadataMetrics(capacity=32),
        briefing_provider=cast(Any, Provider()),
    )
    request = type(
        "BriefingRequest",
        (),
        {
            "model_id": "model-v1",
            "lineage": BriefingRunLineage.for_occurrence(uuid4(), uuid4()),
        },
    )()
    stream = coordinator.execute_briefing(
        request,
        (),
        deadline_monotonic=monotonic() + 10,
        cancelled=None,
        max_output_tokens=2,
    )
    with pytest.raises(RuntimeError, match="output budget exceeded"):
        async for _chunk in stream:
            pass


@pytest.mark.asyncio
async def test_run_coordinator_briefing_cancellation_is_typed_and_idempotent() -> None:
    generic_cancellation_calls = 0

    async def generic_cancellation(lineage: object) -> None:
        nonlocal generic_cancellation_calls
        generic_cancellation_calls += 1
        del lineage

    class Provider:
        async def stream_chat(self, model_id: str, messages: object) -> AsyncIterator[str]:
            del model_id, messages
            yield "first"
            await asyncio.sleep(3600)
            yield "never"

    lineage = BriefingRunLineage.for_occurrence(uuid4(), uuid4())
    request = type("BriefingRequest", (), {"model_id": "model-v1", "lineage": lineage})()
    coordinator = RunCoordinator(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        briefing_provider=cast(Any, Provider()),
        briefing_cancellation=generic_cancellation,
    )
    stream = coordinator.execute_briefing(
        request,
        (),
        deadline_monotonic=monotonic() + 30,
        cancelled=None,
        max_output_tokens=32,
    )
    assert await anext(stream) == "first"
    await coordinator.cancel_briefing(lineage)
    await coordinator.cancel_briefing(lineage)
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert generic_cancellation_calls == 0


@pytest.mark.asyncio
async def test_briefing_cancellation_preserves_authoritative_completion_result() -> None:
    lineage = BriefingRunLineage.for_occurrence(uuid4(), uuid4())
    coordinator_calls: list[BriefingRunLineage] = []

    class Coordinator:
        async def cancel_briefing(self, requested: BriefingRunLineage) -> None:
            coordinator_calls.append(requested)

    class State:
        async def resolve_briefing_lineage(
            self, run_id: Any, issuer: str, subject: str
        ) -> BriefingRunLineage | None:
            assert run_id == lineage.run_id
            assert issuer and subject
            return lineage

        async def cancel_briefing_run(self, requested: BriefingRunLineage) -> BriefingRunResult:
            now = datetime.now(UTC)
            return BriefingRunResult(
                requested,
                BriefingRunStatus.COMPLETED,
                SourceCompleteness.COMPLETE,
                "authoritative completion",
                now,
                now,
                settled=True,
            )

    adapter = BriefingRunCancellationAdapter(
        cast(Any, Coordinator()),
        PublicBriefingRunRepository(cast(Any, State())),
    )
    with pytest.raises(BriefingExecutionError, match="non-canceled"):
        await adapter.cancel(lineage.run_id, "briefing-owner", "https://authentik.example", "key")
    assert coordinator_calls == [lineage]
