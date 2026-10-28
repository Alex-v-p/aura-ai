"""Small persistence-neutral records used by workflow repositories."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkflowDraft:
    id: UUID
    workflow_id: UUID
    version: int


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    id: UUID
    workflow_id: UUID
    revision_id: UUID


__all__ = ["WorkflowDraft", "WorkflowRecord"]
