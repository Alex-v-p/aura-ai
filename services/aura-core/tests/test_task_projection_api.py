"""Transport-shape and authorization tests for the Task Center route."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.execution.tasks.contracts import (
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskSourceKind,
)
from aura_core.domains.knowledge.documents import public as document_public
from aura_core.domains.knowledge.research import public as research_public
from aura_core.entrypoints.api.routes import tasks


def test_summary_and_detail_serializers_match_closed_public_shapes() -> None:
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    row = TaskProjectionSnapshot(
        owner_scope_id=uuid4(),
        source_kind=TaskSourceKind.DOCUMENT_PROCESSING,
        source_id=uuid4(),
        source_version=2,
        status=TaskNormalizedStatus.COMPLETED,
        source_status="ready",
        source_outcome="completed",
        stage=None,
        progress=1,
        capabilities=TaskProjectionCapability(),
        route_key="document_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
    )
    summary = tasks._payload(row)  # pyright: ignore[reportPrivateUsage]
    detail = tasks._detail_payload(  # pyright: ignore[reportPrivateUsage]
        row, display_label="Document processing", recovery_action=None, artifact_refs=[]
    )
    assert "ownerScopeId" not in summary
    assert set(detail) == {
        "id",
        "sourceKind",
        "sourceId",
        "sourceVersion",
        "status",
        "sourceStatus",
        "sourceOutcome",
        "stage",
        "progress",
        "capabilities",
        "routeKey",
        "createdAt",
        "updatedAt",
        "displayLabel",
        "failureClass",
        "recoveryAction",
        "artifactRefs",
    }


@pytest.mark.parametrize(
    ("source_kind", "route_key", "has_source_detail"),
    (
        (TaskSourceKind.RESEARCH, "research_detail", True),
        (TaskSourceKind.DOCUMENT_PROCESSING, "document_detail", True),
        (TaskSourceKind.WORKFLOW_INSTANCE, "workflow_instance_detail", False),
        (TaskSourceKind.BRIEFING_OCCURRENCE, "briefing_occurrence_detail", False),
        (TaskSourceKind.MEMORY_REINDEX, "memory_reindex_detail", False),
    ),
)
def test_serializers_preserve_source_detail_capability_matrix(
    source_kind: TaskSourceKind, route_key: str, has_source_detail: bool
) -> None:
    """Summary and detail transports must expose the source-owned capability."""

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    owner_scope_id = uuid4()
    source_id = uuid4()
    if source_kind is TaskSourceKind.RESEARCH:
        research = research_public.ResearchTask(
            id=source_id,
            issuer="https://issuer.example",
            subject="api-capability-owner",
            question="Which source route is supported?",
            mode=research_public.ResearchMode.QUICK,
            stage=research_public.ResearchStage.QUEUED,
            outcome=None,
            content_state=research_public.ResearchContentState.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
            finished_at=None,
            admitted_budget=research_public.ResearchBudget(90, 6, 1, 1, 1),
        )
        service = research_public.ResearchApplicationService(
            cast(research_public.ResearchRepository, object())
        )
        row = cast(
            TaskProjectionSnapshot,
            service._snapshot(research),  # pyright: ignore[reportPrivateUsage]
        )
        assert row is not None
    elif source_kind is TaskSourceKind.DOCUMENT_PROCESSING:
        document = document_public.Document(
            id=source_id,
            issuer="https://issuer.example",
            subject="api-capability-owner",
            filename="capability.txt",
            media_type="text/plain",
            byte_count=1,
            digest="a" * 64,
            object_key="documents/capability",
            status=document_public.DocumentStatus.READY,
            version=1,
            created_at=now,
            updated_at=now,
        )
        row = cast(
            TaskProjectionSnapshot,
            document_public.DocumentTaskProjectionApplication._snapshot(  # pyright: ignore[reportPrivateUsage]
                document, owner_scope_id
            ),
        )
    else:
        row = TaskProjectionSnapshot(
            owner_scope_id=owner_scope_id,
            source_kind=source_kind,
            source_id=source_id,
            source_version=1,
            status=TaskNormalizedStatus.RUNNING,
            source_status="running",
            source_outcome=None,
            stage="executing",
            progress=0.25,
            capabilities=TaskProjectionCapability(has_source_detail=has_source_detail),
            route_key=route_key,
            failure_class=None,
            created_at=now,
            updated_at=now,
        )

    assert row.source_kind is source_kind
    assert row.capabilities.has_source_detail is has_source_detail
    summary = tasks._payload(row)  # pyright: ignore[reportPrivateUsage]
    detail = tasks._detail_payload(  # pyright: ignore[reportPrivateUsage]
        row, display_label="Task", recovery_action=None, artifact_refs=[]
    )
    summary_capabilities = cast(dict[str, Any], summary["capabilities"])
    detail_capabilities = cast(dict[str, Any], detail["capabilities"])
    assert summary_capabilities["hasSourceDetail"] is has_source_detail
    assert detail_capabilities["hasSourceDetail"] is has_source_detail
