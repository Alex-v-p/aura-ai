"""Evolve notification delivery to a closed reminder-or-briefing source.

This migration is additive and forward-only. Existing reminder rows retain
their reminder identifier and uniqueness constraint. Briefing notifications
use an opaque durable-result identifier and never receive a fabricated
reminder identifier; briefing-owned tables are intentionally not created here.
"""

from alembic import op
from sqlalchemy import text

revision = "0025_notification_brief_source"
down_revision = "0024_calendar_read_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            ALTER TABLE notifications
                ALTER COLUMN reminder_id DROP NOT NULL,
                ADD COLUMN IF NOT EXISTS source_kind VARCHAR(16) NOT NULL DEFAULT 'reminder',
                ADD COLUMN IF NOT EXISTS briefing_schedule_id UUID,
                ADD COLUMN IF NOT EXISTS briefing_occurrence_id UUID,
                ADD COLUMN IF NOT EXISTS briefing_result_id UUID
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE notification_events
                ALTER COLUMN reminder_id DROP NOT NULL,
                ADD COLUMN IF NOT EXISTS source_kind VARCHAR(16) NOT NULL DEFAULT 'reminder',
                ADD COLUMN IF NOT EXISTS briefing_schedule_id UUID,
                ADD COLUMN IF NOT EXISTS briefing_occurrence_id UUID,
                ADD COLUMN IF NOT EXISTS briefing_result_id UUID
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE notifications
                ADD CONSTRAINT ck_notifications_source
                CHECK (
                    (source_kind = 'reminder' AND reminder_id IS NOT NULL
                        AND briefing_schedule_id IS NULL
                        AND briefing_occurrence_id IS NULL AND briefing_result_id IS NULL)
                    OR
                    (source_kind = 'briefing' AND reminder_id IS NULL
                        AND briefing_schedule_id IS NOT NULL
                        AND briefing_occurrence_id IS NOT NULL AND briefing_result_id IS NOT NULL)
                )
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE notification_events
                ADD CONSTRAINT ck_notification_events_source
                CHECK (
                    (source_kind = 'reminder' AND reminder_id IS NOT NULL
                        AND briefing_schedule_id IS NULL
                        AND briefing_occurrence_id IS NULL AND briefing_result_id IS NULL)
                    OR
                    (source_kind = 'briefing' AND reminder_id IS NULL
                        AND briefing_schedule_id IS NOT NULL
                        AND briefing_occurrence_id IS NOT NULL AND briefing_result_id IS NOT NULL)
                )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_briefing_result
                ON notifications (principal_issuer, principal_subject, briefing_result_id)
                WHERE source_kind = 'briefing'
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notification_source_owner
                ON notifications (
                    principal_issuer, principal_subject, source_kind, created_at DESC, id DESC
                )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Notification source evolution downgrade is disabled; use a forward migration"
    )
