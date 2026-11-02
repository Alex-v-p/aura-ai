"""Grant document search to a new immutable built-in Aura revision."""

import uuid

from alembic import op
from sqlalchemy import text

revision = "0020_document_search_citations"
down_revision = "0019_document_library"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_GENERAL_AGENT = uuid.uuid5(_NAMESPACE, "general-assistant")
_GENERAL_DOCUMENT_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-4")
_GENERAL_DOCUMENT_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-4")


def upgrade() -> None:
    op.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS citations JSONB"))
    op.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS citations JSONB"))
    op.execute(
        text(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS document_search_used BOOLEAN "
            "NOT NULL DEFAULT FALSE"
        )
    )

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
            "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
            "max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
            "SELECT :id, '', '', :agent, COALESCE(MAX(revision), 0) + 1, "
            "1, 30000, 0, jsonb_build_array('memory:read', 'documents:read', "
            "'reminders:read', 'reminders:write') "
            "FROM tool_policy_revisions WHERE agent_profile_id = :agent "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": _GENERAL_DOCUMENT_POLICY, "agent": _GENERAL_AGENT},
    )
    for tool_id in (
        "memory.search",
        "document.search",
        "reminder.create",
        "reminder.list",
        "reminder.inspect",
        "reminder.series.create",
        "reminder.reschedule",
        "reminder.series.update",
        "reminder.series.pause",
        "reminder.series.resume",
        "reminder.cancel",
    ):
        bind.execute(
            text(
                "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                "VALUES (:policy, :tool, 1) ON CONFLICT DO NOTHING"
            ),
            {"policy": _GENERAL_DOCUMENT_POLICY, "tool": tool_id},
        )
    bind.execute(
        text(
            "INSERT INTO agent_revisions "
            "(id, agent_profile_id, revision, display_name, system_prompt, purpose, instructions, "
            "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
            "memory_policy_revision_id, tool_policy_revision_id) "
            "SELECT :id, :agent, COALESCE(MAX(revision), 0) + 1, :display_name, :system_prompt, "
            ":purpose, :instructions, :persona_revision_id, :prompt_bundle_revision_id, "
            ":model_policy_revision_id, :memory_policy_revision_id, :tool_policy "
            "FROM agent_revisions WHERE agent_profile_id = :agent "
            "AND NOT EXISTS (SELECT 1 FROM agent_revisions WHERE id = :id)"
        ),
        {
            "id": _GENERAL_DOCUMENT_REVISION,
            "agent": _GENERAL_AGENT,
            **dict(source_row),
            "tool_policy": _GENERAL_DOCUMENT_POLICY,
        },
    )
    bind.execute(
        text(
            "UPDATE agent_profiles SET current_revision_id = :successor "
            "WHERE id = :agent AND current_revision_id = :source"
        ),
        {
            "successor": _GENERAL_DOCUMENT_REVISION,
            "agent": _GENERAL_AGENT,
            "source": source,
        },
    )


def downgrade() -> None:
    raise RuntimeError("Document search policy downgrade is disabled; use a forward migration")
