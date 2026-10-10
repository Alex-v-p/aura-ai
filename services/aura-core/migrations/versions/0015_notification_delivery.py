"""Add durable in-app notifications and owner-scoped replay events."""

from alembic import op
from sqlalchemy import text

revision = "0015_notification_delivery"
down_revision = "0014_reminder_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                reminder_id UUID NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'delivered'
                    CONSTRAINT ck_notifications_status
                    CHECK (status IN ('delivered', 'acknowledged')),
                version INTEGER NOT NULL DEFAULT 1
                    CONSTRAINT ck_notifications_version_positive CHECK (version > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at TIMESTAMPTZ,
                CONSTRAINT uq_notification_reminder UNIQUE (reminder_id)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_notifications_owner_updated ON notifications "
            "(principal_issuer, principal_subject, updated_at DESC, id DESC)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                notification_id UUID NOT NULL,
                reminder_id UUID NOT NULL,
                status VARCHAR(16) NOT NULL
                    CONSTRAINT ck_notification_events_status
                    CHECK (status IN ('delivered', 'acknowledged')),
                version INTEGER NOT NULL CONSTRAINT ck_notification_events_version_positive CHECK (version > 0),
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_notification_events_owner_cursor ON notification_events "
            "(principal_issuer, principal_subject, occurred_at, id)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS notification_command_idempotency (
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                notification_id UUID NOT NULL,
                response JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
            )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("Notification delivery downgrade is disabled; use a forward migration")
