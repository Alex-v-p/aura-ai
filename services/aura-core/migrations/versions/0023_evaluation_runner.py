"""Add the Core-owned content-free evaluation runner ledger."""

from alembic import op
from sqlalchemy import text

revision = "0023_evaluation_runner"
down_revision = "0022_conversation_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS evaluation_runner_attempts (
                attempt_id UUID PRIMARY KEY,
                request_id UUID NOT NULL,
                job_id UUID NOT NULL,
                idempotency_key UUID NOT NULL,
                request_digest VARCHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL,
                lease_owner VARCHAR(128),
                lease_expires_at TIMESTAMPTZ,
                cancellation_id UUID,
                cancellation_reason VARCHAR(32),
                lease_generation UUID,
                request_projection JSONB,
                result_id UUID,
                result_digest VARCHAR(64),
                terminal_message_id UUID,
                terminal_payload JSONB,
                publication_status VARCHAR(16) NOT NULL DEFAULT 'pending',
                business_accounted BOOLEAN NOT NULL DEFAULT FALSE,
                failure_code VARCHAR(48),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ,
                execution_started_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (idempotency_key),
                CHECK (status IN ('queued', 'leased', 'completed', 'cancelled', 'failed')),
                CHECK (publication_status IN ('pending', 'published'))
            )
            """
        )
    )
    for column in (
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS lease_generation UUID",
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS request_projection JSONB",
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS terminal_payload JSONB",
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS attempt_count "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS business_accounted "
        "BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE evaluation_runner_attempts ADD COLUMN IF NOT EXISTS execution_started_at "
        "TIMESTAMPTZ",
    ):
        op.execute(text(column))
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS evaluation_runner_terminal_outbox (
                -- The runner role is deliberately not a database-owner role.
                -- The terminal transition and outbox insert are committed in
                -- one Core-owned transaction, so a database foreign key here
                -- adds a runtime REFERENCES privilege dependency without
                -- adding an integrity guarantee beyond that transaction.
                attempt_id UUID PRIMARY KEY,
                message_id UUID NOT NULL UNIQUE,
                payload JSONB NOT NULL,
                publication_status VARCHAR(16) NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMPTZ,
                CHECK (publication_status IN ('pending', 'published'))
            )
            """
        )
    )
    # Older disposable schemas created the FK above.  Remove it on upgrade so
    # terminal settlement has the same least-privilege behavior on fresh and
    # existing state; the ledger transaction remains the integrity boundary.
    op.execute(
        text(
            "ALTER TABLE evaluation_runner_terminal_outbox "
            "DROP CONSTRAINT IF EXISTS evaluation_runner_terminal_outbox_attempt_id_fkey"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS evaluation_runner_cancellations (
                attempt_id UUID PRIMARY KEY,
                cancellation_id UUID NOT NULL,
                request_id UUID NOT NULL,
                job_id UUID NOT NULL,
                idempotency_key UUID NOT NULL,
                request_digest VARCHAR(64) NOT NULL,
                deadline_at TIMESTAMPTZ NOT NULL,
                reason VARCHAR(32) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (reason IN ('OWNER_REQUESTED', 'SUPERSEDED', 'DEADLINE_EXCEEDED', 'SHUTDOWN'))
            )
            """
        )
    )
    for column in (
        "ALTER TABLE evaluation_runner_cancellations ADD COLUMN IF NOT EXISTS request_id UUID",
        "ALTER TABLE evaluation_runner_cancellations ADD COLUMN IF NOT EXISTS job_id UUID",
        "ALTER TABLE evaluation_runner_cancellations ADD COLUMN IF NOT EXISTS idempotency_key UUID",
        "ALTER TABLE evaluation_runner_cancellations ADD COLUMN IF NOT EXISTS request_digest "
        "VARCHAR(64)",
        "ALTER TABLE evaluation_runner_cancellations ADD COLUMN IF NOT EXISTS deadline_at "
        "TIMESTAMPTZ",
    ):
        op.execute(text(column))
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_evaluation_runner_status_lease "
            "ON evaluation_runner_attempts (status, lease_expires_at)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_evaluation_runner_request "
            "ON evaluation_runner_attempts (request_id, job_id)"
        )
    )


def downgrade() -> None:
    raise RuntimeError("evaluation runner state is intentionally forward-only")
