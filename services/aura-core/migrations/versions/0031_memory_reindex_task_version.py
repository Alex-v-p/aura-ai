"""Add the source-owned memory reindex lifecycle version."""

from alembic import op
from sqlalchemy import text

revision = "0031_memory_reindex_task_version"
down_revision = "0030_task_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE memory_embedding_generations "
            "ADD COLUMN IF NOT EXISTS task_projection_version INTEGER NOT NULL DEFAULT 1"
        )
    )
    op.execute(
        text(
            "UPDATE memory_embedding_generations SET task_projection_version = 1 "
            "WHERE task_projection_version IS NULL OR task_projection_version < 1"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'ck_memory_embedding_generations_task_projection_version') "
            "THEN ALTER TABLE memory_embedding_generations "
            "ADD CONSTRAINT ck_memory_embedding_generations_task_projection_version "
            "CHECK (task_projection_version > 0); END IF; END $$"
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE memory_embedding_generations "
            "DROP CONSTRAINT IF EXISTS ck_memory_embedding_generations_task_projection_version"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_embedding_generations "
            "DROP COLUMN IF EXISTS task_projection_version"
        )
    )
