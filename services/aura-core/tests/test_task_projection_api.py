"""Transport-shape and authorization tests for the Task Center route."""

from datetime import UTC, datetime
from uuid import uuid4

from aura_core.domains.execution.tasks.contracts import (
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskSourceKind,
)
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
