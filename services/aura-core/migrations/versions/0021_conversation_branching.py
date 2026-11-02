"""Add immutable conversation branch and regeneration lineage."""

from alembic import op
from sqlalchemy import text

revision = "0021_conversation_branching"
down_revision = "0020_document_search_citations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS conversation_branches (
                id UUID PRIMARY KEY,
                conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                parent_branch_id UUID NULL,
                forked_from_message_id UUID NULL,
                head_message_id UUID NULL,
                origin VARCHAR(32) NOT NULL DEFAULT 'root',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_branches_conversation "
            "ON conversation_branches (conversation_id, created_at)"
        )
    )
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS branch_id UUID"))
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS parent_message_id UUID"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS branch_id UUID"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS regenerated_from_run_id UUID"))
    op.execute(
        text(
            "ALTER TABLE conversation_branches ADD COLUMN IF NOT EXISTS origin "
            "VARCHAR(32) NOT NULL DEFAULT 'root'"
        )
    )
    # Existing linear conversations receive one opaque root branch.  The
    # deterministic digest keeps the migration repeatable without relying on
    # a provider-specific UUID extension.
    op.execute(
        text(
            """
            INSERT INTO conversation_branches (id, conversation_id, head_message_id, created_at)
            SELECT md5(c.id::text || chr(58) || 'root')::uuid, c.id,
                   (SELECT m.id FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1),
                   COALESCE(c.created_at, CURRENT_TIMESTAMP)
            FROM conversations c
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_branches b WHERE b.conversation_id = c.id
            )
            """
        )
    )
    op.execute(
        text(
            """
            UPDATE messages m SET branch_id = b.id
            FROM conversation_branches b
            WHERE m.conversation_id = b.conversation_id AND b.parent_branch_id IS NULL
              AND m.branch_id IS NULL
            """
        )
    )
    op.execute(
        text(
            """
            UPDATE runs r SET branch_id = m.branch_id
            FROM messages m
            WHERE r.user_message_id = m.id AND r.branch_id IS NULL
            """
        )
    )
    op.execute(
        text("CREATE INDEX IF NOT EXISTS ix_messages_branch ON messages (branch_id, created_at)")
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_branch ON runs (branch_id, created_at)"))


def downgrade() -> None:
    # Branch lineage is historical data.  Keep the migration forward-only so
    # an accidental downgrade cannot silently rewrite conversation history.
    raise RuntimeError("conversation branching downgrade is intentionally disabled")
