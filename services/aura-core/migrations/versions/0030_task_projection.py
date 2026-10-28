"""Create the execution-owned, rebuildable durable task projection."""

# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0030_task_projection"
down_revision = "0029_generated_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately no foreign keys: source domains own their rows and the
    # projection must remain rebuildable after source retention/deletion.
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS task_projection (
            owner_scope_id UUID NOT NULL,
            source_kind VARCHAR(32) NOT NULL,
            source_id UUID NOT NULL,
            source_version INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL,
            source_status VARCHAR(64) NOT NULL,
            source_outcome VARCHAR(64),
            stage VARCHAR(64),
            progress DOUBLE PRECISION,
            can_cancel BOOLEAN NOT NULL DEFAULT FALSE,
            can_retry BOOLEAN NOT NULL DEFAULT FALSE,
            has_source_detail BOOLEAN NOT NULL DEFAULT TRUE,
            route_key VARCHAR(64) NOT NULL,
            failure_class VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            source_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            projection_version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source_kind, source_id),
            CHECK (source_kind IN ('research','workflow_instance','briefing_occurrence','document_processing','memory_reindex')),
            CHECK (source_version > 0),
            CHECK (projection_version > 0),
            CHECK (status IN ('queued','running','waiting','completed','failed','canceled','unknown')),
            CHECK (source_status ~ '^[a-z][a-z0-9_]*$'),
            CHECK (source_outcome IS NULL OR source_outcome ~ '^[a-z][a-z0-9_]*$'),
            CHECK (stage IS NULL OR stage ~ '^[a-z][a-z0-9_]*$'),
            CHECK (failure_class IS NULL OR failure_class ~ '^[a-z][a-z0-9_]*$'),
            CHECK (progress IS NULL OR (progress >= 0 AND progress <= 1)),
            CHECK (route_key IN ('research_detail','workflow_instance_detail','briefing_occurrence_detail','document_detail','memory_reindex_detail')),
            CHECK (updated_at >= created_at),
            CHECK (source_deleted OR source_status <> 'deleted')
        )
    """)
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_task_projection_owner_updated ON task_projection (owner_scope_id, updated_at DESC, source_id DESC)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_task_projection_owner_status ON task_projection (owner_scope_id, status, updated_at DESC)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_task_projection_source_version ON task_projection (source_kind, source_version)"
        )
    )
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS task_projection_rebuilds (
            source_kind VARCHAR(32) PRIMARY KEY,
            pass_id UUID NOT NULL,
            cursor VARCHAR(1024),
            started_at TIMESTAMPTZ NOT NULL,
            baseline_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            baseline_projection_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
            seen_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(seen_source_ids) = 'array'),
            CHECK (jsonb_typeof(baseline_source_ids) = 'array'),
            CHECK (jsonb_typeof(baseline_projection_versions) = 'object')
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS task_projection_command_receipts (
            owner_scope_id UUID NOT NULL,
            source_kind VARCHAR(32) NOT NULL,
            source_id UUID NOT NULL,
            command VARCHAR(16) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL,
            state VARCHAR(16) NOT NULL,
            source_version INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (owner_scope_id, source_kind, source_id, command, idempotency_key),
            CHECK (command IN ('cancel','retry')),
            CHECK (state IN ('accepted','rejected','unknown')),
            CHECK (source_version > 0)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS task_projection_reconciliation_queue (
            source_kind VARCHAR(32) NOT NULL,
            source_id UUID NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claimed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            PRIMARY KEY (source_kind, source_id),
            CHECK (attempts >= 0)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS task_projection_events (
            event_sequence BIGSERIAL UNIQUE,
            event_id UUID PRIMARY KEY,
            owner_scope_id UUID NOT NULL,
            task_id UUID NOT NULL,
            source_kind VARCHAR(32) NOT NULL,
            source_id UUID NOT NULL,
            source_version INTEGER NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (source_version > 0)
        )
    """))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS task_projection_command_receipts"))
    op.execute(text("DROP TABLE IF EXISTS task_projection_reconciliation_queue"))
    op.execute(text("DROP TABLE IF EXISTS task_projection_events"))
    op.execute(text("DROP TABLE IF EXISTS task_projection_rebuilds"))
    op.execute(text("DROP INDEX IF EXISTS ix_task_projection_source_version"))
    op.execute(text("DROP INDEX IF EXISTS ix_task_projection_owner_status"))
    op.execute(text("DROP INDEX IF EXISTS ix_task_projection_owner_updated"))
    op.execute(text("DROP TABLE IF EXISTS task_projection"))
