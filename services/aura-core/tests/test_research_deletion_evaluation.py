"""Deletion truth and privacy evaluations for research content."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aura_core.domains.knowledge.research.public import (
    ResearchConflict,
    ResearchContentState,
    ResearchMode,
    ResearchNotFound,
    ResearchOutcome,
    ResearchStage,
)
from test_research_lifecycle_evaluation import OWNER, _providers, _service


class _PendingArtifactCleanup:
    def __init__(self, *, fail_lookup: bool = False, lifecycle: str = "deletion_pending") -> None:
        self.fail_lookup = fail_lookup
        self.lifecycle = lifecycle
        self.admitted: list[tuple[object, object]] = []

    async def get(self, issuer: str, subject: str, artifact_id: object) -> object:
        del issuer, subject, artifact_id
        if self.fail_lookup:
            raise RuntimeError("artifact cleanup proof unavailable")
        return SimpleNamespace(version=1)

    async def admit_delete(
        self, issuer: str, subject: str, artifact_id: object, **kwargs: object
    ) -> object:
        del issuer, subject
        self.admitted.append((artifact_id, kwargs))
        return SimpleNamespace(lifecycle=self.lifecycle, version=kwargs["expected_version"] + 1)


@pytest.mark.asyncio
async def test_delete_makes_private_content_unreadable_before_cleanup() -> None:
    service, repository = _service(_providers())
    admission = await service.admit(*OWNER, question="private question", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    pending = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=False,
    )
    assert pending.content_state is ResearchContentState.DELETED
    assert pending.question == ""
    assert pending.seed_urls == ()
    assert pending.evidence == ()
    assert pending.citations == ()
    assert service.detail(pending).get("tombstoneLabel") == "Research source deleted"
    assert "question" not in service.detail(pending)
    assert await service.evidence(*OWNER, pending.id) == []
    assert repository.cleanup[pending.id]["status"] == "deleted"
    assert repository.cleanup[pending.id]["evidenceStatus"] == "deleted"


@pytest.mark.asyncio
async def test_evidence_only_delete_reaches_deleted_after_private_cleanup_proof() -> None:
    service, repository = _service(_providers())
    admission = await service.admit(*OWNER, question="delete proof", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)
    request_id = uuid4()
    repository.operations[(terminal.id, "fetch", request_id)] = {
        "status": "settled",
        "response": {
            "url": "https://example.org/private",
            "excerpt": "private evidence excerpt",
        },
    }

    deleted = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=False,
    )

    assert deleted.content_state is ResearchContentState.DELETED
    assert all(
        not any(key in repr(value).casefold() for key in ("url", "excerpt"))
        for value in repository.operations.values()
    )


@pytest.mark.asyncio
async def test_delete_requires_terminal_state_and_expected_version() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="still running", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)

    with pytest.raises(ResearchConflict):
        await service.delete(*OWNER, task.id, expected_version=task.version)

    terminal = await service.settle(
        task, ResearchOutcome.FAILED, failure_class="provider_unavailable"
    )
    with pytest.raises(ResearchConflict):
        await service.delete(*OWNER, terminal.id, expected_version=terminal.version - 1)


@pytest.mark.asyncio
async def test_deleted_content_cannot_be_resurrected_by_a_duplicate_checkpoint() -> None:
    service, repository = _service(_providers())
    admission = await service.admit(*OWNER, question="never resurrect", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)
    deleted = await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    with pytest.raises(ResearchConflict):
        await service.checkpoint(deleted, ResearchStage.SYNTHESIZING)
    assert repository.tasks[deleted.id].content_state is ResearchContentState.DELETED


@pytest.mark.asyncio
async def test_deletion_is_owner_scoped() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="owner only", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    with pytest.raises(ResearchNotFound):
        await service.delete(
            "other-issuer", "other-subject", terminal.id, expected_version=terminal.version
        )


@pytest.mark.asyncio
async def test_research_artifact_lineage_survives_terminal_reload_before_deletion() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="lineage", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=(
            {"artifactId": "artifact-1", "kind": "research_report", "revision": 1},
        ),
    )
    reloaded = await service.get(*OWNER, terminal.id)
    assert reloaded.artifact_refs == terminal.artifact_refs
    assert service.detail(reloaded)["artifactRefs"] == list(terminal.artifact_refs)


@pytest.mark.asyncio
async def test_cleanup_retry_and_unknown_reconciliation_are_distinct_states() -> None:
    service, repository = _service(_providers())
    admission = await service.admit(*OWNER, question="cleanup", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    failed = replace(
        terminal,
        content_state=ResearchContentState.DELETION_FAILED,
        version=terminal.version + 1,
    )
    await repository.save(failed, expected_version=terminal.version)
    repository.cleanup[failed.id] = {
        "status": "failed",
        "evidenceStatus": "deleted",
        "artifactStatus": "not_requested",
    }
    retried = await service.retry_cleanup(*OWNER, failed.id, expected_version=failed.version)
    assert retried.content_state is ResearchContentState.DELETED
    assert repository.cleanup[retried.id]["status"] == "deleted"

    unknown = replace(
        retried,
        content_state=ResearchContentState.CLEANUP_UNKNOWN,
        version=retried.version + 1,
    )
    await repository.save(unknown, expected_version=retried.version)
    reconciled = await service.reconcile_cleanup(
        *OWNER, unknown.id, expected_version=unknown.version
    )
    assert reconciled.content_state is ResearchContentState.DELETED
    assert repository.cleanup[reconciled.id]["status"] == "deleted"


@pytest.mark.asyncio
async def test_cleanup_retry_and_reconcile_idempotency_survive_service_recreation() -> None:
    providers = _providers()
    service, repository = _service(providers)
    admission = await service.admit(*OWNER, question="cleanup replay", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    failed = replace(
        terminal,
        content_state=ResearchContentState.DELETION_FAILED,
        version=terminal.version + 1,
    )
    await repository.save(failed, expected_version=terminal.version)
    repository.cleanup[failed.id] = {
        "status": "failed",
        "evidenceStatus": "deleted",
        "artifactStatus": "not_requested",
    }
    first_retry = await service.retry_cleanup(
        *OWNER, failed.id, expected_version=failed.version, idempotency_key="retry-restart"
    )
    recreated = type(service)(repository, provider_clients=providers)
    replay_retry = await recreated.retry_cleanup(
        *OWNER, failed.id, expected_version=failed.version, idempotency_key="retry-restart"
    )
    assert replay_retry == first_retry

    unknown = replace(
        first_retry,
        content_state=ResearchContentState.CLEANUP_UNKNOWN,
        version=first_retry.version + 1,
    )
    await repository.save(unknown, expected_version=first_retry.version)
    repository.cleanup[unknown.id] = {
        "status": "unknown",
        "evidenceStatus": "deleted",
        "artifactStatus": "not_requested",
    }
    first_reconcile = await recreated.reconcile_cleanup(
        *OWNER, unknown.id, expected_version=unknown.version, idempotency_key="reconcile-restart"
    )
    recreated_again = type(service)(repository, provider_clients=providers)
    replay_reconcile = await recreated_again.reconcile_cleanup(
        *OWNER, unknown.id, expected_version=unknown.version, idempotency_key="reconcile-restart"
    )
    assert replay_reconcile == first_reconcile


@pytest.mark.asyncio
async def test_empty_content_deletion_aggregates_and_cleanup_ledger_settle_deleted() -> None:
    service, repository = _service(_providers())
    admission = await service.admit(*OWNER, question="empty deletion", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    deleted = await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    assert deleted.content_state is ResearchContentState.DELETED
    assert repository.tasks[deleted.id].content_state is ResearchContentState.DELETED
    assert repository.cleanup[deleted.id]["status"] == "deleted"
    assert repository.cleanup[deleted.id]["evidenceStatus"] == "deleted"


@pytest.mark.asyncio
async def test_pending_deletion_emits_a_truthful_observation_before_final_settlement() -> None:
    from aura_core.platform.telemetry import MetadataMetrics

    providers = _providers()
    metrics = MetadataMetrics()
    service, repository = _service(providers)
    service.metrics = metrics
    admission = await service.admit(*OWNER, question="pending observation", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    deletion_records = [
        item
        for item in metrics.measurements
        if item.metric == "research_deletion_outcome_count"
    ]
    assert len(deletion_records) == 1
    assert dict(deletion_records[0].dimensions)["status"] == ResearchContentState.DELETED.value
    assert repository.cleanup[terminal.id]["status"] == "deleted"


@pytest.mark.asyncio
async def test_linked_artifact_cleanup_can_remain_pending_with_truthful_ledger_state() -> None:
    from aura_core.platform.telemetry import MetadataMetrics

    artifact = _PendingArtifactCleanup(lifecycle="deletion_pending")
    service, repository = _service(_providers())
    metrics = MetadataMetrics()
    service.metrics = metrics
    service.artifact_service = artifact
    admission = await service.admit(*OWNER, question="linked pending", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=({"artifactId": str(uuid4()), "kind": "research_report", "revision": 1},),
    )

    pending = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=True,
    )

    assert pending.content_state is ResearchContentState.DELETION_PENDING
    assert repository.cleanup[pending.id]["status"] == "pending"
    assert repository.cleanup[pending.id]["artifactStatus"] == "pending"
    outcomes = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_outcome_count"
    ]
    latencies = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_latency_ms"
    ]
    spans = [
        item
        for item in metrics.measurements
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "research.delete"
    ]
    assert len(outcomes) == len(latencies) == len(spans) == 1
    assert dict(outcomes[0].dimensions)["status"] == ResearchContentState.DELETION_PENDING.value
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_linked_artifact_cleanup_exception_has_one_failed_telemetry_tuple() -> None:
    from aura_core.platform.telemetry import MetadataMetrics

    artifact = _PendingArtifactCleanup(fail_lookup=True)
    metrics = MetadataMetrics()
    service, repository = _service(_providers())
    service.metrics = metrics
    service.artifact_service = artifact
    admission = await service.admit(*OWNER, question="linked failure", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=({"artifactId": str(uuid4()), "kind": "research_report", "revision": 1},),
    )

    with pytest.raises(RuntimeError, match="artifact cleanup proof unavailable"):
        await service.delete(
            *OWNER,
            terminal.id,
            expected_version=terminal.version,
            delete_linked_artifacts=True,
        )

    current = repository.tasks[terminal.id]
    assert current.content_state is ResearchContentState.CLEANUP_UNKNOWN
    outcomes = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_outcome_count"
    ]
    latencies = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_latency_ms"
    ]
    spans = [
        item
        for item in metrics.measurements
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "research.delete"
    ]
    assert len(outcomes) == len(latencies) == len(spans) == 1
    assert dict(outcomes[0].dimensions)["outcome"] == "failed"
    assert dict(outcomes[0].dimensions)["error_class"] == "deletion_failed"
    assert dict(latencies[0].dimensions)["outcome"] == "failed"
    assert dict(latencies[0].dimensions)["error_class"] == "deletion_failed"
    span_dimensions = dict(spans[0].dimensions)
    assert span_dimensions["outcome"] == "failed"
    assert span_dimensions["error_class"] == "deletion_failed"
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_one_deletion_command_has_one_terminal_outcome_latency_and_span() -> None:
    from aura_core.platform.telemetry import MetadataMetrics

    metrics = MetadataMetrics()
    service, _ = _service(_providers())
    service.metrics = metrics
    admission = await service.admit(*OWNER, question="one deletion", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)
    await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    outcomes = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_outcome_count"
    ]
    latencies = [
        item
        for item in metrics.measurements
        if item.kind == "metric" and item.metric == "research_deletion_latency_ms"
    ]
    spans = [
        item
        for item in metrics.measurements
        if item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "research.delete"
    ]
    assert len(outcomes) == len(latencies) == len(spans) == 1


@pytest.mark.asyncio
async def test_delete_command_replay_returns_the_original_cleanup_decision() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="delete replay", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)

    first = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=False,
        idempotency_key="delete-replay",
    )
    second = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=False,
        idempotency_key="delete-replay",
    )
    assert second.version == first.version
    assert second.content_state is first.content_state


@pytest.mark.asyncio
async def test_retry_cleanup_rejects_pending_without_a_known_failure() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="pending retry", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(task, ResearchOutcome.COMPLETED)
    pending = await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    with pytest.raises(ResearchConflict):
        await service.retry_cleanup(*OWNER, pending.id, expected_version=pending.version)


@pytest.mark.asyncio
async def test_cleanup_unknown_needs_proof_before_research_is_marked_deleted() -> None:
    artifact = _PendingArtifactCleanup(fail_lookup=True)
    service, repository = _service(_providers())
    service.artifact_service = artifact
    artifact_id = uuid4()
    admission = await service.admit(*OWNER, question="unknown cleanup", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=({"artifactId": str(artifact_id), "kind": "research_report", "revision": 1},),
    )

    with pytest.raises(RuntimeError, match="proof unavailable"):
        await service.delete(
            *OWNER,
            terminal.id,
            expected_version=terminal.version,
            delete_linked_artifacts=True,
        )
    unknown = await service.get(*OWNER, terminal.id)
    assert unknown.content_state is ResearchContentState.CLEANUP_UNKNOWN

    with pytest.raises((ResearchConflict, RuntimeError)):
        await service.reconcile_cleanup(*OWNER, unknown.id, expected_version=unknown.version)
    assert repository.tasks[unknown.id].content_state is ResearchContentState.CLEANUP_UNKNOWN


@pytest.mark.asyncio
async def test_reconcile_cleanup_rejects_pending_linked_artifact_without_blind_deletion() -> None:
    artifact = _PendingArtifactCleanup(lifecycle="deletion_pending")
    service, repository = _service(_providers())
    service.artifact_service = artifact
    artifact_id = uuid4()
    admission = await service.admit(*OWNER, question="reconcile proof", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=(
            {"artifactId": str(artifact_id), "kind": "research_report", "revision": 1},
        ),
    )
    pending = replace(
        terminal,
        content_state=ResearchContentState.CLEANUP_UNKNOWN,
        question="",
        evidence=(),
        citations=(),
        version=terminal.version + 1,
    )
    await repository.save(pending, expected_version=terminal.version)

    with pytest.raises(ResearchConflict):
        await service.reconcile_cleanup(*OWNER, pending.id, expected_version=pending.version)
    assert repository.tasks[pending.id].content_state is ResearchContentState.CLEANUP_UNKNOWN


@pytest.mark.asyncio
async def test_linked_artifact_pending_substate_blocks_research_deleted_claim() -> None:
    artifact = _PendingArtifactCleanup()
    service, repository = _service(_providers())
    service.artifact_service = artifact
    artifact_id = uuid4()
    admission = await service.admit(*OWNER, question="linked cleanup", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    terminal = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        artifact_refs=({"artifactId": str(artifact_id), "kind": "research_report", "revision": 1},),
    )

    pending = await service.delete(
        *OWNER,
        terminal.id,
        expected_version=terminal.version,
        delete_linked_artifacts=True,
    )

    assert artifact.admitted
    assert pending.content_state is not ResearchContentState.DELETED
    assert repository.tasks[pending.id].content_state is not ResearchContentState.DELETED
