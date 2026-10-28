"""Create owner-scoped scheduled briefing state and run lineage.

Briefing rows are retained until an explicit owner deletion command.  Foreign
keys intentionally do not cascade: schedule, occurrence history and result
content have separate lifecycle semantics.
"""

from alembic import op
from sqlalchemy import text

revision = "0026_briefing_execution"
down_revision = "0025_notification_brief_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS briefing_schedules (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            display_name VARCHAR(120) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            local_time VARCHAR(8) NOT NULL,
            time_zone VARCHAR(128) NOT NULL,
            include_reminders BOOLEAN NOT NULL DEFAULT TRUE,
            calendar_ids JSONB NOT NULL,
            document_ids JSONB NOT NULL,
            agent_revision_id UUID NOT NULL,
            execution_policies JSONB NOT NULL,
            next_due_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (char_length(display_name) BETWEEN 1 AND 120),
            CHECK (version > 0),
            CHECK (char_length(local_time) = 8),
            CHECK (status IN ('active', 'paused', 'deleted')),
            CHECK (include_reminders = TRUE)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS briefing_occurrences (
            id UUID PRIMARY KEY,
            schedule_id UUID NOT NULL REFERENCES briefing_schedules(id),
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            occurrence_sequence INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'scheduled',
            due_at TIMESTAMPTZ NOT NULL,
            due_local_date DATE NOT NULL,
            due_local_time VARCHAR(8) NOT NULL,
            time_zone VARCHAR(128) NOT NULL,
            source_completeness VARCHAR(16) NOT NULL DEFAULT 'unknown',
            run_id UUID,
            result_id UUID,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            UNIQUE (schedule_id, due_local_date),
            UNIQUE (schedule_id, occurrence_sequence),
            CHECK (occurrence_sequence > 0),
            CHECK (version > 0),
            CHECK (status IN ('scheduled', 'admitted', 'running', 'completed', 'partial', 'failed', 'canceled', 'skipped', 'unknown')),
            CHECK (source_completeness IN ('complete', 'partial', 'unavailable', 'unknown'))
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS briefing_results (
            id UUID PRIMARY KEY,
            schedule_id UUID NOT NULL REFERENCES briefing_schedules(id),
            occurrence_id UUID NOT NULL REFERENCES briefing_occurrences(id),
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            run_id UUID,
            status VARCHAR(16) NOT NULL,
            original_status VARCHAR(16),
            version INTEGER NOT NULL DEFAULT 1,
            source_completeness VARCHAR(16) NOT NULL,
            content TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            UNIQUE (occurrence_id),
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (version > 0),
            CHECK (status IN ('completed', 'partial', 'failed', 'canceled', 'unknown', 'deleted')),
            CHECK (original_status IS NULL OR original_status IN ('completed', 'partial', 'failed', 'canceled', 'unknown')),
            CHECK (source_completeness IN ('complete', 'partial', 'unavailable', 'unknown')),
            CHECK (content IS NULL OR char_length(content) <= 100000)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS briefing_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            response JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS briefing_run_lineage (
            occurrence_id UUID PRIMARY KEY REFERENCES briefing_occurrences(id),
            schedule_id UUID NOT NULL REFERENCES briefing_schedules(id),
            run_id UUID NOT NULL UNIQUE,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_briefing_schedule_owner ON briefing_schedules (principal_issuer, principal_subject, created_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_briefing_occurrence_due_claim ON briefing_occurrences (status, due_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_briefing_occurrence_owner ON briefing_occurrences (principal_issuer, principal_subject, schedule_id, occurrence_sequence DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_briefing_result_owner ON briefing_results (principal_issuer, principal_subject, updated_at DESC)"))


def downgrade() -> None:
    # Only remove objects created by this revision, in dependency order.
    op.execute(text("DROP TABLE IF EXISTS briefing_run_lineage"))
    op.execute(text("DROP TABLE IF EXISTS briefing_command_idempotency"))
    op.execute(text("DROP TABLE IF EXISTS briefing_results"))
    op.execute(text("DROP TABLE IF EXISTS briefing_occurrences"))
    op.execute(text("DROP TABLE IF EXISTS briefing_schedules"))
