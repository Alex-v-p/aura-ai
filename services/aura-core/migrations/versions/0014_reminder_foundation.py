"""Add owner-scoped one-shot reminder persistence and generic outbox identity."""

from alembic import op
from sqlalchemy import text

revision = "0014_reminder_foundation"
down_revision = "0013_governed_tool_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS aggregate_type VARCHAR(64)"
        )
    )
    op.execute(text("UPDATE outbox SET aggregate_type = 'run' WHERE aggregate_type IS NULL"))
    op.execute(text("ALTER TABLE outbox ALTER COLUMN aggregate_type SET NOT NULL"))
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                content TEXT NOT NULL CONSTRAINT ck_reminders_content_length
                    CHECK (char_length(content) BETWEEN 1 AND 2000),
                due_at TIMESTAMPTZ NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'scheduled'
                    CONSTRAINT ck_reminders_status
                    CHECK (status IN ('scheduled', 'fired', 'completed', 'canceled')),
                version INTEGER NOT NULL DEFAULT 1
                    CONSTRAINT ck_reminders_version_positive CHECK (version > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                fired_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                canceled_at TIMESTAMPTZ,
                CONSTRAINT uq_reminder_owner UNIQUE (id, principal_issuer, principal_subject)
            )
            """
        )
    )
    # Keep upgraded databases aligned with the declarative mapping used by a
    # fresh 0001->0014 install.  Constraint names are stable so this block is
    # safe to replay in disposable and already-partially-upgraded databases.
    for name, definition in (
        (
            "ck_reminders_content_length",
            "char_length(content) BETWEEN 1 AND 2000",
        ),
        (
            "ck_reminders_status",
            "status IN ('scheduled', 'fired', 'completed', 'canceled')",
        ),
        ("ck_reminders_version_positive", "version > 0"),
    ):
        op.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                f"WHERE c.conname = '{name}' AND t.relname = 'reminders'"
                ") THEN "
                f"ALTER TABLE reminders ADD CONSTRAINT {name} CHECK ({definition}); "
                "END IF; END $$"
            )
        )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_reminders_owner_due ON reminders "
            "(principal_issuer, principal_subject, due_at, id)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_reminders_owner_updated ON reminders "
            "(principal_issuer, principal_subject, updated_at, id)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS reminder_command_idempotency (
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                reminder_id UUID NOT NULL,
                response JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
            )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("Reminder foundation downgrade is disabled; use a forward migration")
