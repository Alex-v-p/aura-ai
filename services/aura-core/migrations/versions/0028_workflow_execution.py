"""Add runtime metadata required by bounded workflow execution.

The workflow contract/state migration owns the durable aggregates.  This
revision only adds execution timestamps and replay indexes that are needed by
the application coordinator and metadata-only lifecycle replay.  Every
operation is additive and safe to apply to a database that already contains
the AURA-0136 workflow tables.
"""

# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0028_workflow_execution"
down_revision = "0027_workflow_contract_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A terminal timestamp is distinct from updated_at: consumers can
    # calculate bounded lifecycle durations without inferring terminality from
    # a mutable status column.
    op.execute(text("""
        ALTER TABLE workflow_instances
            ADD COLUMN IF NOT EXISTS terminal_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS next_due_at TIMESTAMPTZ
    """))
    op.execute(text("""
        ALTER TABLE workflow_instance_steps
            ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ
    """))

    # Persisted lifecycle events are the SSE source of truth.  Keep the
    # existing UUID identity and add a bounded owner-local sequence for
    # deterministic replay ordering when multiple events share a timestamp.
    op.execute(text("""
        ALTER TABLE workflow_lifecycle_events
            ALTER COLUMN instance_id DROP NOT NULL
    """))
    op.execute(text("""
        ALTER TABLE workflow_lifecycle_events
            ADD COLUMN IF NOT EXISTS event_sequence BIGSERIAL
    """))
    op.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_lifecycle_event_sequence
            ON workflow_lifecycle_events (principal_issuer, principal_subject, event_sequence)
    """))
    op.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_workflow_lifecycle_replay
            ON workflow_lifecycle_events (
                principal_issuer,
                principal_subject,
                occurred_at,
                id
            )
    """))
    op.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_workflow_instance_step_execution
            ON workflow_instance_steps (instance_id, execution_id)
            WHERE execution_id IS NOT NULL
    """))
    op.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_workflow_instance_due_recovery
            ON workflow_instances (status, next_due_at, updated_at, id)
            WHERE status = 'waiting_for_time' AND next_due_at IS NOT NULL
    """))


def downgrade() -> None:
    # This is intentionally reversible for local migration tests.  No
    # workflow business rows are removed by the downgrade.
    op.execute(text("DROP INDEX IF EXISTS ix_workflow_instance_step_execution"))
    op.execute(text("DROP INDEX IF EXISTS ix_workflow_instance_due_recovery"))
    op.execute(text("DROP INDEX IF EXISTS ix_workflow_lifecycle_replay"))
    op.execute(text("DROP INDEX IF EXISTS uq_workflow_lifecycle_event_sequence"))
    op.execute(text("ALTER TABLE workflow_lifecycle_events DROP COLUMN IF EXISTS event_sequence"))
    op.execute(text("ALTER TABLE workflow_instance_steps DROP COLUMN IF EXISTS settled_at"))
    op.execute(text("ALTER TABLE workflow_instance_steps DROP COLUMN IF EXISTS started_at"))
    op.execute(text("ALTER TABLE workflow_instances DROP COLUMN IF EXISTS terminal_at"))
    op.execute(text("ALTER TABLE workflow_instances DROP COLUMN IF EXISTS next_due_at"))
