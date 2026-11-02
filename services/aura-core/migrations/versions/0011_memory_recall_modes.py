"""Add immutable automatic/off memory recall modes.

This migration deliberately creates a successor policy and agent revision for
each profile's current configuration.  Existing conversations and runs keep
their pinned revisions; only the profile's *current* pointer moves forward.
"""

import uuid

from alembic import op
from sqlalchemy import text

revision = "0011_memory_recall_modes"
down_revision = "0010_memory_recall_policies"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")


def _successor_policy_id(profile_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"agent-memory-recall-policy:{profile_id}:2")


def _successor_revision_id(profile_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"agent-memory-recall-revision:{profile_id}:2")


def upgrade() -> None:
    # Defaults make the change safe for fresh installs and for deployments
    # where an earlier interrupted migration added one of these columns.
    op.execute(
        text(
            "ALTER TABLE memory_policy_revisions "
            "ADD COLUMN IF NOT EXISTS recall_mode VARCHAR(16) NOT NULL DEFAULT 'automatic'"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_policy_revisions "
            "ADD COLUMN IF NOT EXISTS automatic_recall_threshold DOUBLE PRECISION "
            "NOT NULL DEFAULT 0.7"
        )
    )
    # Historical rows are hydratable as automatic policies.  Do not overwrite
    # valid values if this migration is replayed against a partially upgraded
    # database.
    op.execute(
        text(
            "UPDATE memory_policy_revisions SET recall_mode = 'automatic' "
            "WHERE recall_mode IS NULL OR recall_mode NOT IN ('off', 'automatic')"
        )
    )
    op.execute(
        text(
            "UPDATE memory_policy_revisions SET automatic_recall_threshold = 0.7 "
            "WHERE automatic_recall_threshold IS NULL "
            "OR automatic_recall_threshold < 0 OR automatic_recall_threshold > 1"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_policy_revisions ALTER COLUMN recall_mode SET NOT NULL"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_policy_revisions "
            "ALTER COLUMN automatic_recall_threshold SET NOT NULL"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'memory_policy_recall_mode_check') THEN "
            "ALTER TABLE memory_policy_revisions ADD CONSTRAINT "
            "memory_policy_recall_mode_check CHECK (recall_mode IN ('off', 'automatic')); "
            "END IF; END $$"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'memory_policy_automatic_threshold_check') THEN "
            "ALTER TABLE memory_policy_revisions ADD CONSTRAINT "
            "memory_policy_automatic_threshold_check "
            "CHECK (automatic_recall_threshold BETWEEN 0 AND 1); "
            "END IF; END $$"
        )
    )

    bind = op.get_bind()
    profiles = bind.execute(
        text(
            "SELECT p.id AS profile_id, p.current_revision_id, "
            "r.memory_policy_revision_id "
            "FROM agent_profiles p "
            "JOIN agent_revisions r ON r.id = p.current_revision_id "
            "WHERE p.current_revision_id IS NOT NULL"
        )
    ).mappings()

    for profile in profiles:
        profile_id = profile["profile_id"]
        current_revision_id = profile["current_revision_id"]
        source_policy_id = profile["memory_policy_revision_id"]
        source = bind.execute(
            text(
                "SELECT id, principal_issuer, principal_subject, agent_profile_id, "
                "revision, shared_user_read, current_agent_read, "
                "fallback_relevance_threshold, max_memories, context_budget_fraction, "
                "allow_shared_user_promotion, automatic_recall_threshold "
                "FROM memory_policy_revisions WHERE id = :id"
            ),
            {"id": source_policy_id},
        ).mappings().first()
        if source is None:
            # 0010 guarantees this for a valid database.  Skipping an invalid
            # legacy profile is safer than widening access or inventing an
            # owner for a policy that cannot be proven to exist.
            continue

        successor_policy_id = _successor_policy_id(profile_id)
        successor_revision_id = _successor_revision_id(profile_id)
        policy_exists = bind.execute(
            text("SELECT 1 FROM memory_policy_revisions WHERE id = :id"),
            {"id": successor_policy_id},
        ).scalar_one_or_none()
        if policy_exists is None:
            next_policy_revision = bind.execute(
                text(
                    "SELECT COALESCE(MAX(revision), 0) + 1 "
                    "FROM memory_policy_revisions "
                    "WHERE principal_issuer = :issuer "
                    "AND principal_subject = :subject "
                    "AND agent_profile_id = :agent"
                ),
                {
                    "issuer": source["principal_issuer"],
                    "subject": source["principal_subject"],
                    "agent": profile_id,
                },
            ).scalar_one()
            bind.execute(
                text(
                    "INSERT INTO memory_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "shared_user_read, current_agent_read, fallback_relevance_threshold, "
                    "max_memories, context_budget_fraction, allow_shared_user_promotion, "
                    "recall_mode, automatic_recall_threshold, created_at) "
                    "VALUES (:id, :issuer, :subject, :agent, :revision, "
                    ":shared, :current, :fallback_threshold, "
                    "LEAST(:max_memories, 2), LEAST(:context_fraction, 0.05), :promote, "
                    "'automatic', GREATEST(:automatic_threshold, 0.7), CURRENT_TIMESTAMP)"
                ),
                {
                    "id": successor_policy_id,
                    "issuer": source["principal_issuer"],
                    "subject": source["principal_subject"],
                    "agent": profile_id,
                    "revision": next_policy_revision,
                    "shared": source["shared_user_read"],
                    "current": source["current_agent_read"],
                    "fallback_threshold": source["fallback_relevance_threshold"],
                    "max_memories": source["max_memories"],
                    "context_fraction": source["context_budget_fraction"],
                    "promote": source["allow_shared_user_promotion"],
                    "automatic_threshold": source["automatic_recall_threshold"],
                },
            )
        # Grants are copied independently so a retry also repairs a policy
        # row that was present before an interrupted migration.
        bind.execute(
            text(
                "INSERT INTO memory_policy_fallback_grants "
                "(policy_id, foreign_agent_profile_id) "
                "SELECT :new_policy, foreign_agent_profile_id "
                "FROM memory_policy_fallback_grants WHERE policy_id = :old_policy "
                "ON CONFLICT (policy_id, foreign_agent_profile_id) DO NOTHING"
            ),
            {"new_policy": successor_policy_id, "old_policy": source_policy_id},
        )

        successor_revision_exists = bind.execute(
            text("SELECT 1 FROM agent_revisions WHERE id = :id"),
            {"id": successor_revision_id},
        ).scalar_one_or_none()
        if successor_revision_exists is None:
            bind.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                    "instructions, persona_revision_id, prompt_bundle_revision_id, "
                    "model_policy_revision_id, memory_policy_revision_id, created_at) "
                    "SELECT :new_id, agent_profile_id, "
                    "(SELECT COALESCE(MAX(revision), 0) + 1 FROM agent_revisions "
                    "WHERE agent_profile_id = :profile), display_name, "
                    "system_prompt, purpose, instructions, persona_revision_id, "
                    "prompt_bundle_revision_id, model_policy_revision_id, :policy, "
                    "CURRENT_TIMESTAMP FROM agent_revisions WHERE id = :old_id"
                ),
                {
                    "new_id": successor_revision_id,
                    "old_id": current_revision_id,
                    "profile": profile_id,
                    "policy": successor_policy_id,
                },
            )
        # The profile pointer is the only mutable configuration state changed
        # here.  The inequality makes a retry after a partially completed
        # successor insert repair the pointer once, while a normal replay
        # (where the pointer already is the successor) does not bump version.
        # Conversation and run rows retain their pins.
        bind.execute(
            text(
                "UPDATE agent_profiles SET current_revision_id = :revision, "
                "version = version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = :profile AND current_revision_id = :old_revision "
                "AND current_revision_id <> :revision"
            ),
            {
                "revision": successor_revision_id,
                "profile": profile_id,
                "old_revision": current_revision_id,
            },
        )


def downgrade() -> None:
    raise RuntimeError("Memory recall mode downgrade is disabled; use a forward migration")
