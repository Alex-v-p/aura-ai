"""Add owner-scoped recurring reminder series and occurrence history."""

# SQL DDL is kept readable as executable statements; line-length linting is
# not meaningful inside these schema literals.
# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0017_recurring_reminders"
down_revision = "0016_approval_reminder_tool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS reminder_series (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            content TEXT NOT NULL CONSTRAINT ck_reminder_series_content_length CHECK (char_length(content) BETWEEN 1 AND 2000),
            frequency VARCHAR(32) NOT NULL,
            interval_value INTEGER NOT NULL CONSTRAINT ck_reminder_series_interval_positive CHECK (interval_value > 0),
            weekdays JSONB NOT NULL DEFAULT '[]'::jsonb,
            month_day INTEGER,
            count_limit INTEGER,
            end_date DATE,
            timezone VARCHAR(128) NOT NULL,
            local_start TIMESTAMP NOT NULL,
            next_due_at TIMESTAMPTZ,
            next_reminder_id UUID REFERENCES reminders(id),
            status VARCHAR(16) NOT NULL DEFAULT 'active' CONSTRAINT ck_reminder_series_status CHECK (status IN ('active', 'paused', 'completed', 'canceled')),
            version INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_reminder_series_version_positive CHECK (version > 0),
            occurrence_count INTEGER NOT NULL DEFAULT 0,
            missed_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_reminder_series_missed_count CHECK (missed_count >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paused_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            CONSTRAINT uq_reminder_series_owner UNIQUE (id, principal_issuer, principal_subject)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS reminder_occurrences (
            id UUID PRIMARY KEY,
            series_id UUID NOT NULL REFERENCES reminder_series(id),
            reminder_id UUID NOT NULL UNIQUE REFERENCES reminders(id),
            occurrence_sequence INTEGER NOT NULL CONSTRAINT ck_reminder_occurrence_sequence_positive CHECK (occurrence_sequence > 0),
            status VARCHAR(24) NOT NULL DEFAULT 'scheduled' CONSTRAINT ck_reminder_occurrence_status CHECK (status IN ('scheduled','fired','completed','skipped','replaced','paused-window','series-canceled')),
            local_due_at TIMESTAMP NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            skipped_at TIMESTAMPTZ,
            fired_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_reminder_occurrence_sequence UNIQUE (series_id, occurrence_sequence)
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_reminder_series_owner_status ON reminder_series (principal_issuer, principal_subject, status, updated_at DESC, id DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_reminder_series_due ON reminder_series (status, next_due_at, id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_reminder_occurrences_series ON reminder_occurrences (series_id, occurrence_sequence DESC)"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_scheduled_occurrence_per_series ON reminder_occurrences (series_id) WHERE status = 'scheduled'"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS reminder_series_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            series_id UUID NOT NULL,
            response JSONB NOT NULL,
            operation VARCHAR(64) NOT NULL DEFAULT 'series.create',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
        )
    """))


def downgrade() -> None:
    raise RuntimeError("Recurring reminder downgrade is disabled; use a forward migration")
