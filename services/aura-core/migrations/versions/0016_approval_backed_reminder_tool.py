"""Add private reminder proposals and durable owner approvals."""

# ruff: noqa: E501

import uuid

from alembic import op
from sqlalchemy import text

revision = "0016_approval_reminder_tool"
down_revision = "0015_notification_delivery"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_SUCCESSOR_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-2")
_GENERAL_SUCCESSOR_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-2")


def upgrade() -> None:
    # Conversation/run admission and durable waiting state are part of the
    # same additive approval contract.  IF NOT EXISTS keeps upgrades from the
    # previous governed-tool schema safe while blank installs receive the
    # complete mapping expected by the current Core adapters.
    op.execute(text("""
        ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS client_time_zone VARCHAR(128)
    """))
    op.execute(text("""
        ALTER TABLE tool_policy_revisions
            ADD COLUMN IF NOT EXISTS permission_scopes JSONB NOT NULL DEFAULT '[]'::jsonb
    """))
    # The column may have been introduced by a partially applied deployment
    # without its default or NOT NULL constraint. Normalize that shape before
    # any AURA-0056 policy seeding; historical backfills may omit the column
    # from INSERT statements and must therefore see a safe default here.
    op.execute(text("""
        UPDATE tool_policy_revisions
        SET permission_scopes = '[]'::jsonb
        WHERE permission_scopes IS NULL
    """))
    op.execute(text("""
        ALTER TABLE tool_policy_revisions
            ALTER COLUMN permission_scopes SET DEFAULT '[]'::jsonb
    """))
    op.execute(text("""
        ALTER TABLE tool_policy_revisions
            ALTER COLUMN permission_scopes SET NOT NULL
    """))
    op.execute(text("""
        ALTER TABLE runs
            ADD COLUMN IF NOT EXISTS client_time_zone VARCHAR(128),
            ADD COLUMN IF NOT EXISTS approval_id UUID,
            ADD COLUMN IF NOT EXISTS pending_tool_call_id UUID,
            ADD COLUMN IF NOT EXISTS tool_lifecycle_status VARCHAR(32)
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_approval_id ON runs (approval_id)"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS reminder_proposals (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            content TEXT NOT NULL CONSTRAINT ck_reminder_proposals_content_length CHECK (char_length(content) BETWEEN 1 AND 2000),
            due_at TIMESTAMPTZ NOT NULL,
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            tool_call_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_reminder_proposals_owner ON reminder_proposals (principal_issuer, principal_subject, created_at DESC, id DESC)"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS approvals (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            proposal_id UUID NOT NULL,
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            tool_call_id UUID NOT NULL,
            tool_id VARCHAR(128) NOT NULL,
            tool_version INTEGER NOT NULL CHECK (tool_version > 0),
            status VARCHAR(16) NOT NULL DEFAULT 'pending' CONSTRAINT ck_approvals_status CHECK (status IN ('pending','approved','rejected','expired','canceled')),
            execution_status VARCHAR(16) NOT NULL DEFAULT 'not_started' CONSTRAINT ck_approvals_execution_status CHECK (execution_status IN ('not_started','queued','executing','completed','failed','denied')),
            version INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_approvals_version_positive CHECK (version > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL,
            decided_at TIMESTAMPTZ
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_approvals_owner_updated ON approvals (principal_issuer, principal_subject, updated_at DESC, id DESC)"))
    # Replay cursors are externally represented by event UUIDs, but the
    # server resolves them to this append position.  A sequence is used rather
    # than timestamps or UUID ordering so equal-timestamp events remain
    # append-stable across approvals and reconnects.
    op.execute(text("CREATE SEQUENCE IF NOT EXISTS approval_events_replay_position_seq"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS approval_events (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            approval_id UUID NOT NULL,
            proposal_id UUID NOT NULL,
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            tool_id VARCHAR(128) NOT NULL,
            tool_version INTEGER NOT NULL CHECK (tool_version > 0),
            status VARCHAR(16) NOT NULL CONSTRAINT ck_approval_events_status CHECK (status IN ('pending','approved','rejected','expired','canceled')),
            execution_status VARCHAR(16) NOT NULL CONSTRAINT ck_approval_events_execution_status CHECK (execution_status IN ('not_started','queued','executing','completed','failed','denied')),
            version INTEGER NOT NULL CHECK (version > 0),
            replay_position BIGINT NOT NULL DEFAULT nextval('approval_events_replay_position_seq'::regclass),
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    # Normalize partially applied schemas and historical rows before enforcing
    # uniqueness.  Existing order is retained when present; otherwise the
    # timestamp/UUID order provides a deterministic one-time backfill.  New
    # inserts use the sequence and are append-stable thereafter.
    op.execute(text("""
        ALTER TABLE approval_events
            ADD COLUMN IF NOT EXISTS replay_position BIGINT
    """))
    op.execute(text("""
        WITH ordered AS (
            SELECT id,
                   row_number() OVER (
                       ORDER BY replay_position NULLS LAST, occurred_at, id
                   )::BIGINT AS replay_position
            FROM approval_events
        )
        UPDATE approval_events AS events
        SET replay_position = ordered.replay_position
        FROM ordered
        WHERE events.id = ordered.id
    """))
    op.execute(text("""
        ALTER TABLE approval_events
            ALTER COLUMN replay_position SET DEFAULT nextval('approval_events_replay_position_seq'::regclass),
            ALTER COLUMN replay_position SET NOT NULL
    """))
    op.execute(text("""
        ALTER SEQUENCE approval_events_replay_position_seq
            OWNED BY approval_events.replay_position
    """))
    op.execute(text("""
        SELECT setval(
            'approval_events_replay_position_seq'::regclass,
            GREATEST(COALESCE((SELECT MAX(replay_position) FROM approval_events), 0), 1),
            (SELECT COUNT(*) > 0 FROM approval_events)
        )
    """))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_events_replay_position ON approval_events (replay_position)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_approval_events_owner_replay_position ON approval_events (principal_issuer, principal_subject, replay_position)"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS approval_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            approval_id UUID NOT NULL,
            response JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
        )
    """))

    # Install the immutable built-in successor after all legacy revisions have
    # been admitted.  The source revision is never updated, so conversations
    # already pinned to it retain their historical deny/read-only behavior.
    bind = op.get_bind()
    source = bind.execute(
        text(
            "SELECT current_revision_id FROM agent_profiles "
            "WHERE id = :agent"
        ),
        {"agent": _GENERAL_AGENT},
    ).scalar_one_or_none()
    if source is not None:
        source_row = bind.execute(
            text(
                "SELECT display_name, system_prompt, purpose, instructions, "
                "persona_revision_id, prompt_bundle_revision_id, "
                "model_policy_revision_id, memory_policy_revision_id "
                "FROM agent_revisions WHERE id = :revision"
            ),
            {"revision": source},
        ).mappings().first()
        if source_row is not None:
            bind.execute(
                text(
                    "INSERT INTO tool_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, "
                    "revision, max_calls_per_run, timeout_ms, retry_limit) "
                    "SELECT :id, '', '', :agent, COALESCE(MAX(revision), 0) + 1, "
                    "1, 10000, 0 FROM tool_policy_revisions "
                    "WHERE agent_profile_id = :agent "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": _GENERAL_SUCCESSOR_POLICY, "agent": _GENERAL_AGENT},
            )
            for tool_id in ("memory.search", "reminder.create"):
                bind.execute(
                    text(
                        "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                        "VALUES (:policy, :tool, 1) ON CONFLICT DO NOTHING"
                    ),
                    {"policy": _GENERAL_SUCCESSOR_POLICY, "tool": tool_id},
                )
            bind.execute(
                text(
                    "UPDATE tool_policy_revisions SET permission_scopes = "
                    "jsonb_build_array('memory:read', 'reminders:write') "
                    "WHERE id = :policy"
                ),
                {"policy": _GENERAL_SUCCESSOR_POLICY},
            )
            bind.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, "
                    "purpose, instructions, persona_revision_id, "
                    "prompt_bundle_revision_id, model_policy_revision_id, "
                    "memory_policy_revision_id, tool_policy_revision_id) "
                    "SELECT :id, :agent, COALESCE(MAX(revision), 0) + 1, "
                    ":display_name, :system_prompt, :purpose, :instructions, "
                    ":persona_revision_id, :prompt_bundle_revision_id, "
                    ":model_policy_revision_id, :memory_policy_revision_id, :tool_policy "
                    "FROM agent_revisions WHERE agent_profile_id = :agent "
                    "AND NOT EXISTS (SELECT 1 FROM agent_revisions WHERE id = :id)"
                ),
                {
                    "id": _GENERAL_SUCCESSOR_REVISION,
                    "agent": _GENERAL_AGENT,
                    **dict(source_row),
                    "tool_policy": _GENERAL_SUCCESSOR_POLICY,
                },
            )
            bind.execute(
                text(
                    "UPDATE agent_profiles SET current_revision_id = :successor "
                    "WHERE id = :agent AND current_revision_id = :source"
                ),
                {
                    "successor": _GENERAL_SUCCESSOR_REVISION,
                    "agent": _GENERAL_AGENT,
                    "source": source,
                },
            )


def downgrade() -> None:
    raise RuntimeError("Approval workflow downgrade is disabled; use a forward migration")
