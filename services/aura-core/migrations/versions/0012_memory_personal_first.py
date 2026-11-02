"""Enable personal-first promotion for the built-in Aura agent.

The migration creates one immutable successor policy and agent revision for
Aura's built-in profile.  Existing conversations and runs retain their
historical revision pins; only the profile's current pointer advances.  The
legacy incomplete-provider-output rows are content-free diagnostics and are
therefore settled as rejected so they cannot appear as actionable review
items.
"""

import uuid

from alembic import op
from sqlalchemy import text

revision = "0012_memory_personal_first"
down_revision = "0011_memory_recall_modes"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_SUCCESSOR_POLICY = uuid.uuid5(
    _NAMESPACE, "general-assistant-memory-personal-first-policy-3"
)
_SUCCESSOR_REVISION = uuid.uuid5(
    _NAMESPACE, "general-assistant-memory-personal-first-revision-3"
)


def upgrade() -> None:
    bind = op.get_bind()

    # Retention basis is part of the durable candidate contract.  Existing
    # deployments predate the field, so backfill them to the safe non-retaining
    # value before enforcing the constrained, non-null representation.
    op.execute(
        text(
            "ALTER TABLE memory_candidates ADD COLUMN IF NOT EXISTS "
            "retention_basis VARCHAR(16) NOT NULL DEFAULT 'none'"
        )
    )
    op.execute(
        text(
            "UPDATE memory_candidates SET retention_basis = 'none' "
            "WHERE retention_basis IS NULL "
            "OR retention_basis NOT IN ('personal', 'explicit_request', 'none')"
        )
    )
    op.execute(
        text(
            "ALTER TABLE memory_candidates ALTER COLUMN retention_basis SET NOT NULL"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'memory_candidate_retention_basis_check') THEN "
            "ALTER TABLE memory_candidates ADD CONSTRAINT "
            "memory_candidate_retention_basis_check CHECK "
            "(retention_basis IN ('personal', 'explicit_request', 'none')); "
            "END IF; END $$"
        )
    )

    # A provider response with no content cannot be reviewed or corrected.
    # Keep the row and its reason as an audit diagnostic, but settle it so the
    # owner-facing review queue contains only actionable proposals.
    bind.execute(
        text(
            "UPDATE memory_candidates "
            "SET state = 'rejected', decided_at = COALESCE(decided_at, CURRENT_TIMESTAMP) "
            "WHERE state = 'review' "
            "AND action = 'review' "
            "AND decision_reason = 'invalid_provider_output' "
            "AND content IS NULL"
        )
    )

    profile = bind.execute(
        text(
            "SELECT id, current_revision_id "
            "FROM agent_profiles "
            "WHERE id = :profile"
        ),
        {"profile": _GENERAL_AGENT},
    ).mappings().first()
    if profile is None or profile["current_revision_id"] is None:
        return

    current_revision_id = profile["current_revision_id"]
    # A replay after the profile pointer was advanced must be a no-op.  This
    # also avoids inventing another immutable revision on a partially applied
    # migration retry.
    if current_revision_id == _SUCCESSOR_REVISION:
        return

    source = bind.execute(
        text(
            "SELECT r.id AS revision_id, r.memory_policy_revision_id, "
            "p.principal_issuer, p.principal_subject, p.agent_profile_id, "
            "p.revision, p.shared_user_read, p.current_agent_read, "
            "p.fallback_relevance_threshold, p.max_memories, "
            "p.context_budget_fraction, p.allow_shared_user_promotion, "
            "p.recall_mode, p.automatic_recall_threshold "
            "FROM agent_revisions r "
            "JOIN memory_policy_revisions p "
            "ON p.id = r.memory_policy_revision_id "
            "WHERE r.id = :revision"
        ),
        {"revision": current_revision_id},
    ).mappings().first()
    if source is None:
        # 0010/0011 guarantee the join for a valid installation.  A malformed
        # legacy profile must not receive an invented policy or widened scope.
        return

    policy_exists = bind.execute(
        text("SELECT 1 FROM memory_policy_revisions WHERE id = :id"),
        {"id": _SUCCESSOR_POLICY},
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
                "agent": _GENERAL_AGENT,
            },
        ).scalar_one()
        bind.execute(
            text(
                "INSERT INTO memory_policy_revisions "
                "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                "shared_user_read, current_agent_read, fallback_relevance_threshold, "
                "max_memories, context_budget_fraction, allow_shared_user_promotion, "
                "recall_mode, automatic_recall_threshold, created_at) "
                "VALUES (:id, :issuer, :subject, :agent, :revision, :shared, :current, "
                ":fallback_threshold, :max_memories, :context_fraction, TRUE, "
                ":recall_mode, :automatic_threshold, CURRENT_TIMESTAMP)"
            ),
            {
                "id": _SUCCESSOR_POLICY,
                "issuer": source["principal_issuer"],
                "subject": source["principal_subject"],
                "agent": _GENERAL_AGENT,
                "revision": next_policy_revision,
                "shared": source["shared_user_read"],
                "current": source["current_agent_read"],
                "fallback_threshold": source["fallback_relevance_threshold"],
                "max_memories": source["max_memories"],
                "context_fraction": source["context_budget_fraction"],
                "recall_mode": source["recall_mode"],
                "automatic_threshold": source["automatic_recall_threshold"],
            },
        )

    # Copy fallback grants as independent rows.  Replaying after an
    # interrupted insert repairs the grant set without duplicating entries.
    bind.execute(
        text(
            "INSERT INTO memory_policy_fallback_grants "
            "(policy_id, foreign_agent_profile_id) "
            "SELECT :new_policy, foreign_agent_profile_id "
            "FROM memory_policy_fallback_grants "
            "WHERE policy_id = :old_policy "
            "ON CONFLICT (policy_id, foreign_agent_profile_id) DO NOTHING"
        ),
        {"new_policy": _SUCCESSOR_POLICY, "old_policy": source["memory_policy_revision_id"]},
    )

    successor_exists = bind.execute(
        text("SELECT 1 FROM agent_revisions WHERE id = :id"),
        {"id": _SUCCESSOR_REVISION},
    ).scalar_one_or_none()
    if successor_exists is None:
        next_agent_revision = bind.execute(
            text(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM agent_revisions "
                "WHERE agent_profile_id = :profile"
            ),
            {"profile": _GENERAL_AGENT},
        ).scalar_one()
        bind.execute(
            text(
                "INSERT INTO agent_revisions "
                "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                "instructions, persona_revision_id, prompt_bundle_revision_id, "
                "model_policy_revision_id, memory_policy_revision_id, created_at) "
                "SELECT :new_id, agent_profile_id, :revision, display_name, system_prompt, "
                "purpose, instructions, persona_revision_id, prompt_bundle_revision_id, "
                "model_policy_revision_id, :policy, CURRENT_TIMESTAMP "
                "FROM agent_revisions WHERE id = :old_id"
            ),
            {
                "new_id": _SUCCESSOR_REVISION,
                "revision": next_agent_revision,
                "policy": _SUCCESSOR_POLICY,
                "old_id": current_revision_id,
            },
        )

    # Only the mutable current pointer advances.  Conversations and runs are
    # intentionally untouched and remain pinned to their original revisions.
    bind.execute(
        text(
            "UPDATE agent_profiles SET current_revision_id = :successor, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :profile AND current_revision_id = :source"
        ),
        {
            "successor": _SUCCESSOR_REVISION,
            "profile": _GENERAL_AGENT,
            "source": current_revision_id,
        },
    )


def downgrade() -> None:
    raise RuntimeError(
        "Memory personal-first migration downgrade is disabled; use a forward migration"
    )
