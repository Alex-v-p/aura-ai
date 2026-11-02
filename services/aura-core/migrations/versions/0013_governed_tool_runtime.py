"""Add immutable, deny-by-default agent tool policies.

The migration is additive and forward-only.  Existing agent revisions retain
their historical behavior through deterministic empty policies; only the
current built-in Aura revision receives the explicitly granted read-only
``memory.search@1`` policy successor.
"""

import uuid

from alembic import op
from sqlalchemy import text

revision = "0013_governed_tool_runtime"
down_revision = "0012_memory_personal_first"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-1")
_GENERAL_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-tool-revision-4")


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS tool_policy_revision_id UUID, "
            "ADD COLUMN IF NOT EXISTS tool_call_limit INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS tool_timeout_ms INTEGER NOT NULL DEFAULT 30000, "
            "ADD COLUMN IF NOT EXISTS tool_retry_limit INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS tool_calls_used INTEGER NOT NULL DEFAULT 0"
        )
    )
    op.execute(
        text(
            "UPDATE runs SET tool_call_limit = 0, tool_timeout_ms = 30000, "
            "tool_retry_limit = 0, tool_calls_used = 0 "
            "WHERE tool_policy_revision_id IS NULL"
        )
    )
    op.execute(
        text(
            "ALTER TABLE agent_revisions ADD COLUMN IF NOT EXISTS "
            "tool_policy_revision_id UUID"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tool_policy_revisions (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                agent_profile_id UUID NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                max_calls_per_run INTEGER NOT NULL DEFAULT 0
                    CHECK (max_calls_per_run BETWEEN 0 AND 1),
                timeout_ms INTEGER NOT NULL DEFAULT 30000
                    CHECK (timeout_ms BETWEEN 1 AND 30000),
                retry_limit INTEGER NOT NULL DEFAULT 0 CHECK (retry_limit = 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (principal_issuer, principal_subject, agent_profile_id, revision)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tool_policy_grants (
                policy_id UUID NOT NULL REFERENCES tool_policy_revisions(id) ON DELETE CASCADE,
                tool_id VARCHAR(128) NOT NULL,
                tool_version INTEGER NOT NULL CHECK (tool_version > 0),
                PRIMARY KEY (policy_id, tool_id, tool_version)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tool_policy_owner_agent ON "
            "tool_policy_revisions(principal_issuer, principal_subject, agent_profile_id)"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_policy_owner_revision ON "
            "tool_policy_revisions(principal_issuer, principal_subject, agent_profile_id, revision)"
        )
    )

    bind = op.get_bind()
    profiles = bind.execute(text("SELECT id FROM agent_profiles")).scalars().all()
    # Legacy profiles have no authenticated owner.  Empty-principal policy IDs
    # are deterministic and are only accepted by the runtime for the exact
    # migrated profile/revision that pins them.
    for profile_id in profiles:
        policy_id = uuid.uuid5(_NAMESPACE, f"agent-tool-policy:{profile_id}:1")
        bind.execute(
            text(
                "INSERT INTO tool_policy_revisions "
                "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                "max_calls_per_run, timeout_ms, retry_limit) "
                "VALUES (:id, '', '', :agent, 1, 0, 30000, 0) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": policy_id, "agent": profile_id},
        )
        bind.execute(
            text(
                "UPDATE agent_revisions SET tool_policy_revision_id = :policy "
                "WHERE agent_profile_id = :agent AND tool_policy_revision_id IS NULL"
            ),
            {"policy": policy_id, "agent": profile_id},
        )

    # Runs are immutable historical executions. Pin every legacy run to the
    # deterministic policy carried by its already-pinned agent revision; the
    # built-in successor below must not widen historical runs.
    bind.execute(
        text(
            "UPDATE runs AS run SET tool_policy_revision_id = revision.tool_policy_revision_id, "
            "tool_call_limit = 0, tool_timeout_ms = 30000, tool_retry_limit = 0, "
            "tool_calls_used = 0 FROM agent_revisions AS revision "
            "WHERE run.agent_revision_id = revision.id "
            "AND run.tool_policy_revision_id IS NULL"
        )
    )

    profile = bind.execute(
        text("SELECT current_revision_id FROM agent_profiles WHERE id = :agent"),
        {"agent": _GENERAL_AGENT},
    ).mappings().first()
    if (
        profile is not None
        and profile["current_revision_id"] is not None
        and profile["current_revision_id"] != _GENERAL_REVISION
    ):
        source_id = profile["current_revision_id"]
        source = bind.execute(
            text("SELECT * FROM agent_revisions WHERE id = :id"), {"id": source_id}
        ).mappings().first()
        if source is not None:
            bind.execute(
                text(
                    "INSERT INTO tool_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "max_calls_per_run, timeout_ms, retry_limit) "
                    "VALUES (:id, '', '', :agent, 2, 1, 30000, 0) "
                    "ON CONFLICT (id) DO UPDATE SET revision = 2, max_calls_per_run = 1"
                ),
                {"id": _GENERAL_POLICY, "agent": _GENERAL_AGENT},
            )
            bind.execute(
                text(
                    "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                    "VALUES (:policy, 'memory.search', 1) "
                    "ON CONFLICT (policy_id, tool_id, tool_version) DO NOTHING"
                ),
                {"policy": _GENERAL_POLICY},
            )
            successor_exists = bind.execute(
                text("SELECT 1 FROM agent_revisions WHERE id = :id"),
                {"id": _GENERAL_REVISION},
            ).scalar_one_or_none()
            if successor_exists is None:
                next_revision = bind.execute(
                    text(
                        "SELECT COALESCE(MAX(revision), 0) + 1 FROM agent_revisions "
                        "WHERE agent_profile_id = :agent"
                    ),
                    {"agent": _GENERAL_AGENT},
                ).scalar_one()
                bind.execute(
                    text(
                        "INSERT INTO agent_revisions "
                        "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                        "instructions, persona_revision_id, prompt_bundle_revision_id, "
                        "model_policy_revision_id, memory_policy_revision_id, "
                        "tool_policy_revision_id, created_at) "
                        "SELECT :new_id, agent_profile_id, :revision, display_name, system_prompt, "
                        "purpose, instructions, persona_revision_id, prompt_bundle_revision_id, "
                        "model_policy_revision_id, memory_policy_revision_id, :policy, "
                        "CURRENT_TIMESTAMP "
                        "FROM agent_revisions WHERE id = :old_id"
                    ),
                    {
                        "new_id": _GENERAL_REVISION,
                        "revision": next_revision,
                        "policy": _GENERAL_POLICY,
                        "old_id": source_id,
                    },
                )
            bind.execute(
                text(
                    "UPDATE agent_profiles SET current_revision_id = :successor, "
                    "version = version + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :agent AND current_revision_id = :source"
                ),
                {"successor": _GENERAL_REVISION, "agent": _GENERAL_AGENT, "source": source_id},
            )

    op.execute(
        text(
            "ALTER TABLE agent_revisions ALTER COLUMN tool_policy_revision_id SET NOT NULL"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Governed tool runtime downgrade is disabled; use a forward migration")
