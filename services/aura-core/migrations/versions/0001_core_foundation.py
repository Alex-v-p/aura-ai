"""Core conversation foundation.

Revision ID: 0001_core_foundation
"""

from alembic import op

revision = "0001_core_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Declarative mappings own tables and intra-domain relationships. Deployment
    # runs Alembic for durable environments; cross-domain constraints are added
    # explicitly below so runtime repositories do not share mapping ownership.
    from aura_core.bootstrap.database import metadata

    bind = op.get_bind()
    metadata().create_all(bind=bind)
    # Cross-domain identifiers deliberately remain plain UUIDs in the runtime
    # mappings.  That keeps an owning repository's flush independent of whether
    # another domain's mapping module has been imported, while PostgreSQL stays
    # responsible for referential integrity.
    cross_domain_foreign_keys = (
        ("fk_conversations_principal_id_principals", "conversations", "principals", "principal_id"),
        (
            "fk_conversations_agent_profile_id_agent_profiles",
            "conversations",
            "agent_profiles",
            "agent_profile_id",
        ),
        (
            "fk_conversations_agent_revision_id_agent_revisions",
            "conversations",
            "agent_revisions",
            "agent_revision_id",
        ),
        ("fk_runs_conversation_id_conversations", "runs", "conversations", "conversation_id"),
        ("fk_runs_user_message_id_messages", "runs", "messages", "user_message_id"),
        (
            "fk_runs_agent_revision_id_agent_revisions",
            "runs",
            "agent_revisions",
            "agent_revision_id",
        ),
        (
            "fk_runs_model_policy_revision_id_model_policy_revisions",
            "runs",
            "model_policy_revisions",
            "model_policy_revision_id",
        ),
        (
            "fk_run_events_conversation_id_conversations",
            "run_events",
            "conversations",
            "conversation_id",
        ),
    )
    for name, source_table, referent_table, column in cross_domain_foreign_keys:
        op.create_foreign_key(name, source_table, referent_table, [column], ["id"])


def downgrade() -> None:
    raise RuntimeError(
        "Core foundation downgrade is intentionally disabled; use a forward migration "
        "to preserve conversations"
    )
