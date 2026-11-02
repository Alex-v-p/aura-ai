"""Partition command idempotency by immutable OIDC issuer and subject."""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0003_idempotency_issuer"
down_revision = "0002_run_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("command_idempotency")}
    if "principal_issuer" not in columns:
        op.add_column(
            "command_idempotency",
            Column("principal_issuer", String(1024), nullable=False, server_default=""),
        )
        op.alter_column("command_idempotency", "principal_issuer", server_default=None)
    op.drop_constraint("command_idempotency_pkey", "command_idempotency", type_="primary")
    op.create_primary_key(
        "command_idempotency_pkey",
        "command_idempotency",
        ["principal_issuer", "principal_subject", "idempotency_key"],
    )


def downgrade() -> None:
    raise RuntimeError("Issuer-partitioned idempotency downgrade is disabled")
