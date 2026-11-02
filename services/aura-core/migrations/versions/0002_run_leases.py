"""Persist worker attempt identity and execution leases."""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, inspect
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_run_leases"
down_revision = "0001_core_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 creates metadata from the current mappings for new installations;
    # the guards keep this migration compatible with those databases and with
    # installations upgraded from the original schema.
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("runs")}
    if "attempt_id" not in columns:
        op.add_column("runs", Column("attempt_id", UUID(as_uuid=True), nullable=True))
    if "attempt_count" not in columns:
        op.add_column(
            "runs", Column("attempt_count", Integer(), nullable=False, server_default="0")
        )
    if "lease_expires_at" not in columns:
        op.add_column("runs", Column("lease_expires_at", DateTime(timezone=True), nullable=True))
    indexes = {index["name"] for index in inspect(op.get_bind()).get_indexes("runs")}
    if "ix_runs_attempt_id" not in indexes:
        op.create_index("ix_runs_attempt_id", "runs", ["attempt_id"])
    if "ix_runs_lease_expires_at" not in indexes:
        op.create_index("ix_runs_lease_expires_at", "runs", ["lease_expires_at"])


def downgrade() -> None:
    raise RuntimeError("Run lease downgrade is disabled; use a forward migration")
