"""Forward-only migration contract for AURA-0137 runtime additions."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

MIGRATION = Path(__file__).parents[1] / "migrations" / "versions" / "0028_workflow_execution.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "aura_migration_0028_workflow_execution", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0028_is_a_single_forward_child_of_workflow_contract_state() -> None:
    module = _load()
    assert module.revision == "0028_workflow_execution"
    assert module.down_revision == "0027_workflow_contract_state"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_0028_is_additive_and_contains_runtime_fields_required_for_restart_recovery() -> None:
    source = MIGRATION.read_text(encoding="utf-8").casefold()
    assert "alter table" in source
    assert "add column" in source
    for field in (
        "started_at",
        "settled_at",
        "terminal_at",
        "next_due_at",
        "event_sequence",
    ):
        assert field in source, field

    # A forward runtime migration may add constraints/indexes, but it must not
    # erase existing workflow truth or introduce an implicit destructive down.
    for forbidden in ("drop table", "truncate ", "delete from"):
        assert forbidden not in source


def test_0028_does_not_create_a_scheduler_branch() -> None:
    source = MIGRATION.read_text(encoding="utf-8").casefold()
    assert "workflow_scheduler" not in source
    assert "schedule_lag" not in source


def test_0028_makes_aggregate_lifecycle_events_instance_nullable() -> None:
    source = MIGRATION.read_text(encoding="utf-8").casefold()
    assert "alter column instance_id drop not null" in source

    from aura_core.domains.automation.workflows.persistence import (
        WorkflowInstanceRow,
        WorkflowLifecycleEventRow,
        WorkflowRevisionRow,
    )

    assert WorkflowLifecycleEventRow.__table__.c.instance_id.nullable is True
    assert WorkflowInstanceRow.__table__.c.next_due_at.nullable is True
    # Retired revisions remain immutable rows: lifecycle timestamps are
    # nullable until retirement, while status/version stay required and
    # positive for replay and optimistic locking.
    assert WorkflowRevisionRow.__table__.c.retired_at.nullable is True
    assert WorkflowRevisionRow.__table__.c.status.nullable is False
    assert WorkflowRevisionRow.__table__.c.version.nullable is False
    table = cast(Any, WorkflowRevisionRow.__table__)
    checks: dict[str, str] = {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if constraint.name and hasattr(constraint, "sqltext")
    }
    assert "ck_workflow_revision_version_positive" in checks
    assert "ck_workflow_revision_status" in checks
