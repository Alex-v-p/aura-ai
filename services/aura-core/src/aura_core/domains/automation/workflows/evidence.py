"""Closed, metadata-only workflow run-evidence serializers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from aura_core.domains.execution.runs.public import RunEvidenceItem, RunEvidencePort

from .public import WorkflowEvidenceSerializer, validate_opaque_identifier


@dataclass(frozen=True, slots=True)
class WorkflowEvidenceItem:
    phase: str
    outcome: str
    workflow_id: UUID
    revision_id: UUID
    instance_id: UUID
    step_ordinal: int | None = None
    execution_id: str | None = None
    error_class: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in {
            "admission", "step", "approval", "settlement", "cancellation",
            "definition", "checkpoint", "wait", "notification", "agent_run",
        }:
            raise ValueError("workflow evidence phase is closed")
        if self.outcome not in {
            "accepted", "completed", "failed", "rejected", "unknown", "canceled"
        }:
            raise ValueError("workflow evidence outcome is closed")
        if self.step_ordinal is not None and not 1 <= self.step_ordinal <= 8:
            raise ValueError("workflow evidence ordinal is out of bounds")
        if self.execution_id is not None:
            validate_opaque_identifier(self.execution_id)
        if self.error_class is not None:
            validate_opaque_identifier(self.error_class)


def serialize_workflow_evidence(item: WorkflowEvidenceItem) -> Mapping[str, object]:
    """Return only the closed ADR-0017 automation evidence vocabulary."""

    evidence_kind = "automation_input" if item.outcome == "accepted" else "automation_output"
    payload: dict[str, object] = {
        "kind": evidence_kind,
        "content": {
            "name": f"workflow.{item.phase}",
            "content": item.outcome,
        },
    }
    return payload


class WorkflowRunEvidenceSerializer(WorkflowEvidenceSerializer):
    """Best-effort adapter over the existing Core-owned evidence port."""

    def __init__(self, recorder: RunEvidencePort, run_id: UUID) -> None:
        self.recorder = recorder
        self.run_id = run_id

    def serialize(self, **metadata: object) -> Mapping[str, object]:
        item = WorkflowEvidenceItem(
            phase=str(metadata["phase"]), outcome=str(metadata["outcome"]),
            workflow_id=cast(UUID, metadata["workflow_id"]),
            revision_id=cast(UUID, metadata["revision_id"]),
            instance_id=cast(UUID, metadata["instance_id"]),
            step_ordinal=cast(int | None, metadata.get("step_ordinal")),
            execution_id=cast(str | None, metadata.get("execution_id")),
            # Error classes are telemetry dimensions, not run-evidence
            # payload fields.  The closed automation evidence schema has no
            # errorClass member and must remain content-only.
            error_class=None,
        )
        payload = serialize_workflow_evidence(item)
        sequence = self.recorder.allocate_sequence(self.run_id)
        self.recorder.record(RunEvidenceItem(
            run_id=self.run_id, trace_id=self.run_id.hex,
            span_id=uuid5(NAMESPACE_URL, f"workflow-evidence:{self.run_id}:{sequence}").hex[:16],
            parent_span_id=None, sequence=sequence, step_category="automation",
            evidence_kind=str(payload["kind"]), media_classification="structured",
            media_type="application/json", payload=payload, observed_at=datetime.now(UTC),
            capture_state="captured", reason=None,
        ))
        return payload


__all__ = ["WorkflowEvidenceItem", "serialize_workflow_evidence", "WorkflowRunEvidenceSerializer"]
