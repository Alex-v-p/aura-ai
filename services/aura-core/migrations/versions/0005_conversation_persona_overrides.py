"""Persist independent conversation persona overrides and history."""

from typing import Any

from alembic import op
from sqlalchemy import Column, DateTime, String, inspect, text
from sqlalchemy.dialects.postgresql import UUID

revision = "0005_persona_overrides"
down_revision = "0004_agent_persona_revisions"
branch_labels = None
depends_on = None


def _columns(bind: object, table: str) -> set[str]:
    return {item["name"] for item in inspect(bind).get_columns(table)}  # type: ignore[arg-type]


def _add_column(bind: object, table: str, column: Any) -> None:
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add_column(
        bind,
        "conversations",
        Column("persona_override_revision_id", UUID(as_uuid=True), nullable=True),
    )
    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "conversation_persona_assignments" not in tables:
        op.create_table(
            "conversation_persona_assignments",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("conversation_id", UUID(as_uuid=True), nullable=False),
            Column("persona_revision_id", UUID(as_uuid=True), nullable=False),
            Column("source", String(32), nullable=False),
            Column("reason", String(40), nullable=False),
            Column("effective_after_message_id", UUID(as_uuid=True)),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=text("CURRENT_TIMESTAMP"),
            ),
        )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_persona_assignments_conversation_id "
            "ON conversation_persona_assignments (conversation_id)"
        )
    )

    # Backfill from immutable agent assignment history.  The agent revision's
    # persona is the effective default unless a future row records an explicit
    # override.  The window expression collapses consecutive equal revisions
    # while retaining every actual transition and stable historical IDs.
    op.execute(
        text(
            """
            INSERT INTO conversation_persona_assignments
              (id, conversation_id, persona_revision_id, source, reason,
               effective_after_message_id, created_at)
            SELECT md5(a.conversation_id::text || ':' || a.id::text)::uuid,
                   a.conversation_id,
                   r.persona_revision_id,
                   'agent_default',
                   CASE a.reason
                     WHEN 'initial' THEN 'initial'
                     WHEN 'revision_upgrade' THEN 'agent_revision_upgrade'
                     ELSE 'agent_switch'
                   END,
                   a.effective_after_message_id,
                   a.created_at
            FROM conversation_agent_assignments a
            JOIN agent_revisions r ON r.id = a.agent_revision_id
            WHERE r.persona_revision_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM conversation_persona_assignments existing
                WHERE existing.id = md5(a.conversation_id::text || ':' || a.id::text)::uuid
              )
              AND NOT EXISTS (
                SELECT 1
                FROM conversation_agent_assignments prior
                JOIN agent_revisions prior_revision ON prior_revision.id = prior.agent_revision_id
                WHERE prior.conversation_id = a.conversation_id
                  AND (prior.created_at, prior.id) < (a.created_at, a.id)
                  AND prior_revision.persona_revision_id = r.persona_revision_id
                  AND NOT EXISTS (
                    SELECT 1
                    FROM conversation_agent_assignments between_assignment
                    JOIN agent_revisions between_revision
                      ON between_revision.id = between_assignment.agent_revision_id
                    WHERE between_assignment.conversation_id = a.conversation_id
                      AND (between_assignment.created_at, between_assignment.id)
                        > (prior.created_at, prior.id)
                      AND (between_assignment.created_at, between_assignment.id)
                        < (a.created_at, a.id)
                      AND between_revision.persona_revision_id <> r.persona_revision_id
                  )
              )
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Conversation persona override downgrade is disabled; use a forward migration"
    )
