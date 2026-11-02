"""Persist reversible conversation archive metadata."""

from typing import Any

from alembic import op
from sqlalchemy import Column, DateTime, inspect, text

revision = "0007_conversation_metadata"
down_revision = "0006_title_generation"
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
        Column("archived_at", DateTime(timezone=True), nullable=True),
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversations_principal_archived "
            "ON conversations (principal_id, archived_at)"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Conversation metadata downgrade is disabled; use a forward migration"
    )
