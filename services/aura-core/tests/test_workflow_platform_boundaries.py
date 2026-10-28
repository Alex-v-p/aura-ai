"""Focused platform boundary checks for workflow telemetry and SSE envelopes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import aura_core.entrypoints.api.routes.workflows as workflow_routes
import pytest
from aura_core.bootstrap.workflow_uow import AdmissionGatedWorkflowApplication
from aura_core.domains.automation.workflows.public import (
    WorkflowDeletionReceipt,
    WorkflowDeletionScope,
)
from aura_core.platform.telemetry import MetadataMetrics

_event_public = cast(Any, workflow_routes._event_public)  # pyright: ignore[reportPrivateUsage]


def test_workflow_sse_variants_emit_only_closed_properties() -> None:
    identifiers = {name: str(uuid4()) for name in ("workflowId", "revisionId", "instanceId")}
    step = _event_public(
        {
            "schemaVersion": 1,
            "eventId": str(uuid4()),
            "eventType": "workflow.step.status_changed",
            **identifiers,
            "instanceStatus": "running",
            "terminalOutcome": None,
            "stepExecutionId": str(uuid4()),
            "stepOrdinal": 1,
            "stepKind": "approval",
            "stepStatus": "running",
            "effectOutcome": "not_attempted",
            "recoveryCapabilities": ["cancel"],
            "instanceVersion": 2,
            "version": 2,
            "occurredAt": "2026-01-01T00:00:00+00:00",
        }
    )
    assert set(step) == {
        "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
        "instanceId", "stepExecutionId", "stepOrdinal", "stepKind", "stepStatus",
        "effectOutcome", "recoveryCapabilities", "instanceVersion", "occurredAt",
    }
    retired = _event_public(
        {
            "schemaVersion": 1,
            "eventId": str(uuid4()),
            "eventType": "workflow.revision.retired",
            "workflowId": identifiers["workflowId"],
            "revisionId": identifiers["revisionId"],
            "instanceId": identifiers["instanceId"],
            "version": 3,
            "occurredAt": "2026-01-01T00:00:00+00:00",
        }
    )
    assert set(retired) == {
        "schemaVersion", "eventId", "eventType", "workflowId", "revisionId",
        "version", "occurredAt",
    }


def test_workflow_telemetry_accepts_closed_terminal_and_waiting_statuses() -> None:
    metrics = MetadataMetrics()
    for status in ("waiting_for_time", "terminal"):
        metrics.observe(
            "aura.automation.workflow_state",
            "workflow_transition_duration_ms",
            1.0,
            trace_id="0" * 32,
            operation="workflow.transition",
            outcome="completed",
            dependency="application_validation",
            status=status,
        )
    assert metrics.stats().rejected == 0
    assert all(item.trace_id and set(item.trace_id) != {"0"} for item in metrics.snapshot())


def test_workflow_deletion_receipt_uses_authoritative_persisted_timestamp() -> None:
    deleted_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    receipt = workflow_routes._deletion_receipt(  # pyright: ignore[reportPrivateUsage]
        WorkflowDeletionScope.DRAFT,
        WorkflowDeletionReceipt(True, deleted_at),
    )
    assert receipt == {
        "scope": "draft",
        "deleted": True,
        "tombstoneRetained": True,
        "deletedAt": "2026-01-02T03:04:05+00:00",
    }


@pytest.mark.asyncio
async def test_clone_forwards_expected_definition_version_to_repository() -> None:
    class Repository:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        async def get_definition(self, *_args: object, **_kwargs: object) -> object:
            return type("Definition", (), {"version": 7})()

        async def clone_draft(self, *_args: object, **kwargs: object) -> object:
            self.kwargs = kwargs
            return object()

    repository = Repository()
    service = AdmissionGatedWorkflowApplication.__new__(AdmissionGatedWorkflowApplication)
    service.repository = repository  # type: ignore[attr-defined]
    await service.clone_draft_in_workflow(
        "issuer", "subject", uuid4(), revision_id=uuid4(),
        expected_definition_version=7, idempotency_key="key",
    )
    assert repository.kwargs["expected_definition_version"] == 7
