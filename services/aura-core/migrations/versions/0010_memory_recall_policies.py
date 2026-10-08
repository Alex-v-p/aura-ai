"""Add immutable agent memory policies and fallback grants."""

import uuid

from alembic import op
from sqlalchemy import text

revision = "0010_memory_recall_policies"
down_revision = "0009_memory_processing"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-memory-policy-1")
_GENERAL_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-1")


def upgrade() -> None:
    op.execute(
        text("ALTER TABLE agent_revisions ADD COLUMN IF NOT EXISTS memory_policy_revision_id UUID")
    )
    op.execute(
        text("ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS model_digest VARCHAR(64)")
    )
    op.execute(
        text(
            "ALTER TABLE memory_processing_jobs ADD COLUMN IF NOT EXISTS "
            "allow_shared_user_promotion BOOLEAN NOT NULL DEFAULT FALSE"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_processing_jobs ADD COLUMN IF NOT EXISTS "
            "memory_policy_revision_id UUID"
        )
    )
    # Run admission snapshots the exact policy and active embedding generation;
    # recall metadata is identifier/score-only and is persisted before provider
    # invocation.  These columns intentionally remain nullable for legacy runs.
    op.execute(
        text(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS memory_policy_revision_id UUID, "
            "ADD COLUMN IF NOT EXISTS memory_embedding_generation_id UUID, "
            "ADD COLUMN IF NOT EXISTS memory_recall_metadata JSONB"
        )
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS memory_policy_revisions (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            agent_profile_id UUID NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            shared_user_read BOOLEAN NOT NULL DEFAULT TRUE,
            current_agent_read BOOLEAN NOT NULL DEFAULT TRUE,
            fallback_relevance_threshold DOUBLE PRECISION NOT NULL
                CHECK (fallback_relevance_threshold BETWEEN 0 AND 1),
            max_memories INTEGER NOT NULL DEFAULT 8
                CHECK (max_memories BETWEEN 1 AND 8),
            context_budget_fraction DOUBLE PRECISION NOT NULL DEFAULT 0.2
                CHECK (context_budget_fraction > 0 AND context_budget_fraction <= 0.2),
            allow_shared_user_promotion BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (principal_issuer, principal_subject, agent_profile_id, revision)
        )
    """)
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS memory_policy_fallback_grants (
            policy_id UUID NOT NULL REFERENCES memory_policy_revisions(id) ON DELETE CASCADE,
            foreign_agent_profile_id UUID NOT NULL,
            PRIMARY KEY (policy_id, foreign_agent_profile_id),
            CHECK (foreign_agent_profile_id IS NOT NULL)
        )
    """)
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_memory_policy_owner_agent "
            "ON memory_policy_revisions(principal_issuer, principal_subject, agent_profile_id)"
        )
    )
    seed_policy = text(
        "INSERT INTO memory_policy_revisions "
        "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
        "shared_user_read, current_agent_read, fallback_relevance_threshold, "
        "max_memories, context_budget_fraction, allow_shared_user_promotion, created_at) "
        "VALUES (:id, '', '', :agent, 1, TRUE, TRUE, 0.5, 8, 0.2, FALSE, CURRENT_TIMESTAMP) "
        "ON CONFLICT (principal_issuer, principal_subject, agent_profile_id, revision) "
        "DO UPDATE SET agent_profile_id = EXCLUDED.agent_profile_id "
        "RETURNING id"
    )
    bind = op.get_bind()
    # Agent profiles predate owner-scoped memory policies, so legacy rows have
    # no authenticated principal to copy.  Seed one deterministic policy per
    # profile (rather than sharing one policy across unrelated agents) and keep
    # the platform policy as the explicit blank-principal compatibility seed.
    bind.execute(seed_policy, {"id": _GENERAL_POLICY, "agent": _GENERAL_AGENT})
    profile_ids = bind.execute(
        text("SELECT DISTINCT agent_profile_id FROM agent_revisions")
    ).scalars().all()
    for profile_id in profile_ids:
        policy_id = uuid.uuid5(_NAMESPACE, f"agent-memory-policy:{profile_id}:1")
        policy_id = bind.execute(
            seed_policy, {"id": policy_id, "agent": profile_id}
        ).scalar_one()
        bind.execute(
            text(
                "UPDATE agent_revisions SET memory_policy_revision_id = :policy "
                "WHERE agent_profile_id = :agent"
            ),
            {"policy": policy_id, "agent": profile_id},
        )
    # Existing deployments can only be made non-null after the deterministic
    # backfill above has completed.
    op.execute(
        text("ALTER TABLE agent_revisions ALTER COLUMN memory_policy_revision_id SET NOT NULL")
    )
    op.execute(
        text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'agent_revision_memory_policy_fkey'
            ) THEN
                ALTER TABLE agent_revisions ADD CONSTRAINT agent_revision_memory_policy_fkey
                FOREIGN KEY (memory_policy_revision_id) REFERENCES memory_policy_revisions(id);
            END IF;
        END $$
    """)
    )


def downgrade() -> None:
    raise RuntimeError("Memory recall policy downgrade is disabled; use a forward migration")
