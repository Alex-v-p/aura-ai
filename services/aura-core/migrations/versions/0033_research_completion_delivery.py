"""Add durable conversational research completion delivery state."""
# ruff: noqa: E501

import uuid

from alembic import op
from sqlalchemy import text

revision = "0033_research_delivery"
down_revision = "0032_governed_research"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-5")
_GENERAL_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-5")


def upgrade() -> None:
    # These fields are additive and intentionally JSON metadata.  They carry
    # only opaque identifiers and validated citation metadata; research text
    # remains owned by the research/artifact domains.
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS web_citations JSONB"))
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS research_activity JSONB"))
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS artifact_refs JSONB"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS web_citations JSONB"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS research_origin JSONB"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS research_activity JSONB"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS artifact_refs JSONB"))
    op.execute(text("ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS synthesized_answer TEXT"))
    op.execute(text("ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS delivery_claim_token VARCHAR(64)"))
    op.execute(text("ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS delivery_claim_phase VARCHAR(24)"))
    op.execute(text("ALTER TABLE research_tasks ADD COLUMN IF NOT EXISTS delivery_claim_expires_at TIMESTAMPTZ"))

    # Keep the document-only revision four immutable.  The new web grant is an
    # additive policy and agent revision so historical runs remain resolvable.
    bind = op.get_bind()
    source = bind.execute(
        text("SELECT id FROM agent_revisions WHERE agent_profile_id = :agent AND revision = 4"),
        {"agent": _GENERAL_AGENT},
    ).scalar_one_or_none()
    if source is not None:
        source_row = bind.execute(
            text(
                "SELECT display_name, system_prompt, purpose, instructions, "
                "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
                "memory_policy_revision_id FROM agent_revisions WHERE id = :revision"
            ),
            {"revision": source},
        ).mappings().first()
        if source_row is not None:
            bind.execute(
                text(
                    "INSERT INTO tool_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
                    "VALUES (:id, '', '', :agent, 5, 1, 30000, 0, "
                    "jsonb_build_array('memory:read','documents:read','research:run',"
                    "'reminders:read','reminders:write')) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": _GENERAL_POLICY, "agent": _GENERAL_AGENT},
            )
            for tool_id in (
                "memory.search", "document.search", "web.research", "reminder.create",
                "reminder.list", "reminder.inspect", "reminder.series.create",
                "reminder.reschedule", "reminder.series.update", "reminder.series.pause",
                "reminder.series.resume", "reminder.cancel",
            ):
                bind.execute(
                    text(
                        "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                        "VALUES (:policy, :tool, 1) ON CONFLICT DO NOTHING"
                    ),
                    {"policy": _GENERAL_POLICY, "tool": tool_id},
                )
            bind.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, instructions, "
                    "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
                    "memory_policy_revision_id, tool_policy_revision_id) "
                    "SELECT :id, :agent, 5, :display_name, :system_prompt, :purpose, :instructions, "
                    ":persona_revision_id, :prompt_bundle_revision_id, :model_policy_revision_id, "
                    ":memory_policy_revision_id, :policy "
                    "WHERE NOT EXISTS (SELECT 1 FROM agent_revisions WHERE id = :id)"
                ),
                {"id": _GENERAL_REVISION, "agent": _GENERAL_AGENT, "policy": _GENERAL_POLICY, **dict(source_row)},
            )
            bind.execute(
                text(
                    "UPDATE agent_profiles SET current_revision_id = :revision, version = version + 1 "
                    "WHERE id = :agent AND current_revision_id = :source"
                ),
                {"revision": _GENERAL_REVISION, "agent": _GENERAL_AGENT, "source": source},
            )

    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS research_completion_deliveries (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                research_id UUID NOT NULL,
                conversation_id UUID,
                origin_kind VARCHAR(16) NOT NULL DEFAULT 'conversation',
                origin JSONB NOT NULL DEFAULT '{}'::jsonb,
                branch_id UUID,
                originating_run_id UUID NOT NULL,
                terminal_version INTEGER NOT NULL CHECK (terminal_version > 0),
                append_key VARCHAR(255) NOT NULL,
                notification_key VARCHAR(255) NOT NULL,
                append_status VARCHAR(24) NOT NULL DEFAULT 'pending',
                append_message_id UUID,
                notification_status VARCHAR(24) NOT NULL DEFAULT 'pending',
                last_error VARCHAR(128),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (research_id, conversation_id),
                UNIQUE (append_key),
                UNIQUE (notification_key)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_research_completion_owner
                ON research_completion_deliveries
                    (principal_issuer, principal_subject, updated_at DESC)
            """
        )
    )
    op.execute(text("ALTER TABLE research_completion_deliveries ALTER COLUMN conversation_id DROP NOT NULL"))
    op.execute(text("ALTER TABLE research_completion_deliveries ADD COLUMN IF NOT EXISTS origin_kind VARCHAR(16) NOT NULL DEFAULT 'conversation'"))
    op.execute(text("ALTER TABLE research_completion_deliveries ADD COLUMN IF NOT EXISTS origin JSONB NOT NULL DEFAULT '{}'::jsonb"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_research_completion_origin ON research_completion_deliveries (research_id, origin_kind)"))

    # Extend the notification source union without changing existing reminder,
    # briefing, or workflow records.  The old constraints are replaced by a
    # closed four-way union with research carrying one opaque task identity.
    op.execute(text("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS ck_notifications_source"))
    op.execute(
        text(
            "ALTER TABLE notification_events "
            "DROP CONSTRAINT IF EXISTS ck_notification_events_source"
        )
    )
    op.execute(text("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS research_id UUID"))
    op.execute(text("ALTER TABLE notification_events ADD COLUMN IF NOT EXISTS research_id UUID"))
    op.execute(
        text(
            """
            ALTER TABLE notifications ADD CONSTRAINT ck_notifications_source
            CHECK (
                (source_kind = 'reminder' AND reminder_id IS NOT NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_id IS NULL
                    AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                    AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                    AND workflow_result_id IS NULL
                    AND research_id IS NULL)
                OR (source_kind = 'briefing' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NOT NULL AND briefing_occurrence_id IS NOT NULL
                    AND briefing_result_id IS NOT NULL AND workflow_id IS NULL
                    AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                    AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                    AND workflow_result_id IS NULL
                    AND research_id IS NULL)
                OR (source_kind = 'workflow' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_id IS NOT NULL
                    AND workflow_revision_id IS NOT NULL AND workflow_instance_id IS NOT NULL
                    AND workflow_step_ordinal BETWEEN 1 AND 8 AND workflow_step_execution_id IS NOT NULL
                    AND workflow_result_id IS NOT NULL
                    AND research_id IS NULL)
                OR (source_kind = 'research' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_result_id IS NULL
                    AND workflow_id IS NULL AND workflow_revision_id IS NULL
                    AND workflow_instance_id IS NULL AND workflow_step_ordinal IS NULL
                    AND workflow_step_execution_id IS NULL
                    AND research_id IS NOT NULL)
            )
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE notification_events ADD CONSTRAINT ck_notification_events_source
            CHECK (
                (source_kind = 'reminder' AND reminder_id IS NOT NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_id IS NULL
                    AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                    AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                    AND workflow_result_id IS NULL
                    AND research_id IS NULL)
                OR (source_kind = 'briefing' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NOT NULL AND briefing_occurrence_id IS NOT NULL
                    AND briefing_result_id IS NOT NULL AND workflow_id IS NULL
                    AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                    AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                    AND workflow_result_id IS NULL
                    AND research_id IS NULL)
                OR (source_kind = 'workflow' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_id IS NOT NULL
                    AND workflow_revision_id IS NOT NULL AND workflow_instance_id IS NOT NULL
                    AND workflow_step_ordinal BETWEEN 1 AND 8 AND workflow_step_execution_id IS NOT NULL
                    AND workflow_result_id IS NOT NULL
                    AND research_id IS NULL)
                OR (source_kind = 'research' AND reminder_id IS NULL
                    AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                    AND briefing_result_id IS NULL AND workflow_result_id IS NULL
                    AND workflow_id IS NULL AND workflow_revision_id IS NULL
                    AND workflow_instance_id IS NULL AND workflow_step_ordinal IS NULL
                    AND workflow_step_execution_id IS NULL
                    AND research_id IS NOT NULL)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_research_result
                ON notifications (principal_issuer, principal_subject, research_id)
                WHERE source_kind = 'research'
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "research completion delivery downgrade is disabled; use a forward migration"
    )
