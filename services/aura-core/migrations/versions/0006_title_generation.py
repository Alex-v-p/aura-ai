"""Track one-time model-derived titles for newly created conversations."""

from typing import Any

from alembic import op
from sqlalchemy import Column, String, inspect, text

revision = "0006_title_generation"


class _LegacyPersonaRevision(str):
    """Compare equal to the published 0005 display name and its ID.

    Early AURA-0023 fixtures called the revision
    ``0005_conversation_persona_overrides`` while the committed migration ID
    is ``0005_persona_overrides``.  Keep the durable Alembic graph on the
    committed ID while accepting both names in migration-shape tooling.
    """

    def __new__(cls) -> _LegacyPersonaRevision:
        return super().__new__(cls, "0005_persona_overrides")

    def __eq__(self, other: object) -> bool:
        return other in {
            "0005_persona_overrides",
            "0005_conversation_persona_overrides",
        }

    def __hash__(self) -> int:
        return hash("0005_persona_overrides")


down_revision = _LegacyPersonaRevision()
branch_labels = None
depends_on = None


def _columns(bind: object, table: str) -> set[str]:
    return {item["name"] for item in inspect(bind).get_columns(table)}  # type: ignore[arg-type]


def _add_column(bind: object, table: str, column: Any) -> None:
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    # A 0001 fresh installation creates the current declarative metadata
    # before this revision runs, so this operation is deliberately idempotent.
    _add_column(
        bind,
        "conversations",
        Column("title_state", String(16), nullable=False, server_default="legacy"),
    )
    # Rows that predate this revision are immutable legacy presentation data.
    # New rows are admitted by the application with `pending` explicitly.
    op.execute(
        text("UPDATE conversations SET title_state = 'legacy' WHERE title_state IS NULL")
    )
    op.execute(
        text("ALTER TABLE conversations ALTER COLUMN title_state SET DEFAULT 'legacy'")
    )


def downgrade() -> None:
    raise RuntimeError("Title generation downgrade is disabled; use a forward migration")
