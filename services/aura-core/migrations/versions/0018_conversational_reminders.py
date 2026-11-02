"""Add typed conversational reminder proposals and Aura policy revision three."""

# The migration intentionally keeps private command arguments in the
# reminder-owned proposal table. Approval events and wakeups remain metadata-only.
# ruff: noqa: E501

import uuid

from alembic import op
from sqlalchemy import text

revision = "0018_conversational_reminders"
down_revision = "0017_recurring_reminders"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_CONVERSATIONAL_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-3")
_GENERAL_CONVERSATIONAL_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-3")
_TOOL_IDS = (
    "memory.search",
    "reminder.create",
    "reminder.list",
    "reminder.inspect",
    "reminder.series.create",
    "reminder.reschedule",
    "reminder.series.update",
    "reminder.series.pause",
    "reminder.series.resume",
    "reminder.cancel",
)


def upgrade() -> None:
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS reminder_command_proposals (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            operation VARCHAR(64) NOT NULL CONSTRAINT ck_reminder_command_proposals_operation CHECK (
                operation IN (
                    'series.create', 'reschedule', 'series.update',
                    'series.pause', 'series.resume', 'cancel'
                )
            ),
            tool_id VARCHAR(128) NOT NULL,
            tool_version INTEGER NOT NULL CONSTRAINT ck_reminder_command_proposals_tool_version CHECK (tool_version > 0),
            target_type VARCHAR(16) CONSTRAINT ck_reminder_command_proposals_target_type CHECK (
                target_type IS NULL OR target_type IN ('occurrence', 'series')
            ),
            target_id UUID,
            expected_version INTEGER CONSTRAINT ck_reminder_command_proposals_version CHECK (
                expected_version IS NULL OR expected_version > 0
            ),
            normalized_arguments JSONB NOT NULL,
            client_time_zone VARCHAR(128),
            private_summary TEXT,
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            tool_call_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_reminder_command_proposals_target_pair CHECK (
                (target_id IS NULL AND expected_version IS NULL)
                OR (target_id IS NOT NULL AND expected_version IS NOT NULL)
            )
        )
    """)
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_reminder_command_proposals_owner "
            "ON reminder_command_proposals "
            "(principal_issuer, principal_subject, created_at DESC, id DESC)"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_command_proposals_tool_call "
            "ON reminder_command_proposals (run_id, tool_call_id)"
        )
    )

    # Install a new immutable built-in policy and agent revision. Historical
    # revisions and conversations retain their pinned policy identifiers, and
    # no custom-agent row is updated or granted access.
    bind = op.get_bind()
    source = bind.execute(
        text("SELECT current_revision_id FROM agent_profiles WHERE id = :agent"),
        {"agent": _GENERAL_AGENT},
    ).scalar_one_or_none()
    if source is None:
        return
    source_row = (
        bind.execute(
            text(
                "SELECT display_name, system_prompt, purpose, instructions, "
                "persona_revision_id, prompt_bundle_revision_id, "
                "model_policy_revision_id, memory_policy_revision_id "
                "FROM agent_revisions WHERE id = :revision"
            ),
            {"revision": source},
        )
        .mappings()
        .first()
    )
    if source_row is None:
        return
    bind.execute(
        text(
            "INSERT INTO tool_policy_revisions "
            "(id, principal_issuer, principal_subject, agent_profile_id, "
            "revision, max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
            "SELECT :id, '', '', :agent, COALESCE(MAX(revision), 0) + 1, "
            "1, 10000, 0, jsonb_build_array('memory:read', 'reminders:read', 'reminders:write') "
            "FROM tool_policy_revisions WHERE agent_profile_id = :agent "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": _GENERAL_CONVERSATIONAL_POLICY, "agent": _GENERAL_AGENT},
    )
    for tool_id in _TOOL_IDS:
        bind.execute(
            text(
                "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                "VALUES (:policy, :tool, 1) ON CONFLICT DO NOTHING"
            ),
            {"policy": _GENERAL_CONVERSATIONAL_POLICY, "tool": tool_id},
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
            "id": _GENERAL_CONVERSATIONAL_REVISION,
            "agent": _GENERAL_AGENT,
            **dict(source_row),
            "tool_policy": _GENERAL_CONVERSATIONAL_POLICY,
        },
    )
    bind.execute(
        text(
            "UPDATE agent_profiles SET current_revision_id = :successor "
            "WHERE id = :agent AND current_revision_id = :source"
        ),
        {
            "successor": _GENERAL_CONVERSATIONAL_REVISION,
            "agent": _GENERAL_AGENT,
            "source": source,
        },
    )


def downgrade() -> None:
    raise RuntimeError("Conversational reminder downgrade is disabled; use a forward migration")
