"""Add durable conversational research completion delivery state."""
# ruff: noqa: E501

import uuid
from collections.abc import Mapping
from typing import Any, NoReturn, cast

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
_GENERAL_DOCUMENT_POLICY = uuid.uuid5(_NAMESPACE, "general-assistant-tool-policy-4")
_GENERAL_DOCUMENT_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-4")

_DOCUMENT_GRANTS = (
    ("memory.search", 1),
    ("document.search", 1),
    ("reminder.create", 1),
    ("reminder.list", 1),
    ("reminder.inspect", 1),
    ("reminder.series.create", 1),
    ("reminder.reschedule", 1),
    ("reminder.series.update", 1),
    ("reminder.series.pause", 1),
    ("reminder.series.resume", 1),
    ("reminder.cancel", 1),
)
_RESEARCH_GRANTS = (*_DOCUMENT_GRANTS, ("web.research", 1))
_DOCUMENT_SCOPES = ("memory:read", "documents:read", "reminders:read", "reminders:write")
_RESEARCH_SCOPES = ("memory:read", "documents:read", "research:run", "reminders:read", "reminders:write")


def _fail_closed(reason: str) -> NoReturn:
    raise RuntimeError(f"0033 research seed refused unsafe state: {reason}")


def _ordered_grants(bind: Any, policy_id: uuid.UUID) -> list[tuple[str, int]]:
    rows = bind.execute(
        text(
            "SELECT tool_id, tool_version FROM tool_policy_grants "
            "WHERE policy_id = :policy ORDER BY tool_id, tool_version"
        ),
        {"policy": policy_id},
    ).all()
    return [(str(row.tool_id), int(row.tool_version)) for row in rows]


def _scopes(row: Mapping[Any, Any]) -> tuple[str, ...]:
    value = row["permission_scopes"]
    if not isinstance(value, list):
        _fail_closed("canonical document policy scopes are not a string array")
    values = cast(list[Any], value)
    if any(not isinstance(item, str) for item in values):
        _fail_closed("canonical document policy scopes are not a string array")
    return tuple(cast(list[str], values))


def _validate_document_predecessor(bind: Any, source: Mapping[Any, Any]) -> Mapping[Any, Any]:
    if source["id"] != _GENERAL_DOCUMENT_REVISION:
        _fail_closed("profile is not pinned to the canonical document predecessor")
    if source["agent_profile_id"] != _GENERAL_AGENT:
        _fail_closed("document predecessor belongs to another agent profile")
    if source["tool_policy_revision_id"] != _GENERAL_DOCUMENT_POLICY:
        _fail_closed("document predecessor is not pinned to the canonical policy")

    policy = cast(Mapping[Any, Any] | None, bind.execute(
        text(
            "SELECT id, principal_issuer, principal_subject, agent_profile_id, revision, "
            "max_calls_per_run, timeout_ms, retry_limit, permission_scopes "
            "FROM tool_policy_revisions WHERE id = :id"
        ),
        {"id": _GENERAL_DOCUMENT_POLICY},
    ).mappings().first())
    if policy is None:
        _fail_closed("canonical document policy is missing")
    if (
        policy["principal_issuer"] != ""
        or policy["principal_subject"] != ""
        or policy["agent_profile_id"] != _GENERAL_AGENT
        or policy["revision"] <= 0
        or policy["max_calls_per_run"] != 1
        or policy["timeout_ms"] != 30_000
        or policy["retry_limit"] != 0
        or _scopes(policy) != _DOCUMENT_SCOPES
    ):
        _fail_closed("canonical document policy owner, limits, or scopes are inconsistent")
    if _ordered_grants(bind, _GENERAL_DOCUMENT_POLICY) != sorted(_DOCUMENT_GRANTS):
        _fail_closed("canonical document policy grants are inconsistent")
    return policy


def _validate_existing_research_seed(bind: Any, source: Mapping[Any, Any]) -> bool:
    """Validate an already-created canonical seed without mutating it."""

    policy = cast(Mapping[Any, Any] | None, bind.execute(
        text(
            "SELECT id, principal_issuer, principal_subject, agent_profile_id, revision, "
            "max_calls_per_run, timeout_ms, retry_limit, permission_scopes "
            "FROM tool_policy_revisions WHERE id = :id"
        ),
        {"id": _GENERAL_POLICY},
    ).mappings().first())
    revision = cast(Mapping[Any, Any] | None, bind.execute(
        text(
            "SELECT id, agent_profile_id, revision, display_name, system_prompt, purpose, "
            "instructions, persona_revision_id, prompt_bundle_revision_id, "
            "model_policy_revision_id, memory_policy_revision_id, tool_policy_revision_id "
            "FROM agent_revisions WHERE id = :id"
        ),
        {"id": _GENERAL_REVISION},
    ).mappings().first())
    if policy is None and revision is None:
        return False
    if policy is None or revision is None:
        _fail_closed("canonical research policy and revision are only partially present")
    if (
        policy["principal_issuer"] != ""
        or policy["principal_subject"] != ""
        or policy["agent_profile_id"] != _GENERAL_AGENT
        or policy["revision"] <= 0
        or policy["max_calls_per_run"] != 1
        or policy["timeout_ms"] != 30_000
        or policy["retry_limit"] != 0
        or _scopes(policy) != _RESEARCH_SCOPES
        or _ordered_grants(bind, _GENERAL_POLICY) != sorted(_RESEARCH_GRANTS)
    ):
        _fail_closed("canonical research policy is inconsistent")
    source_fields = (
        "display_name",
        "system_prompt",
        "purpose",
        "instructions",
        "persona_revision_id",
        "prompt_bundle_revision_id",
        "model_policy_revision_id",
        "memory_policy_revision_id",
    )
    if (
        revision["agent_profile_id"] != _GENERAL_AGENT
        or revision["tool_policy_revision_id"] != _GENERAL_POLICY
        or any(revision[field] != source[field] for field in source_fields)
    ):
        _fail_closed("canonical research agent revision is inconsistent")
    return True


def _next_owner_revision(bind: Any, table: str) -> int:
    if table not in {"agent_revisions", "tool_policy_revisions"}:
        raise ValueError("unsupported revision table")
    owner_filter = (
        "principal_issuer = '' AND principal_subject = '' AND "
        "agent_profile_id = :agent"
        if table == "tool_policy_revisions"
        else "agent_profile_id = :agent"
    )
    return int(
        bind.execute(
            text(f"SELECT COALESCE(MAX(revision), 0) + 1 FROM {table} WHERE {owner_filter}"),
            {"agent": _GENERAL_AGENT},
        ).scalar_one()
    )


def _seed_research_delivery(bind: Any) -> None:
    """Seed the immutable web-research successor under the canonical profile."""

    bind.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": "aura:0033:general-assistant-research-seed"},
    )
    profile = bind.execute(
        text(
            "SELECT id, current_revision_id FROM agent_profiles "
            "WHERE id = :agent FOR UPDATE"
        ),
        {"agent": _GENERAL_AGENT},
    ).mappings().first()
    if profile is None:
        _fail_closed("canonical general-assistant profile is missing")
    if profile["current_revision_id"] is None:
        _fail_closed("canonical general-assistant profile has no current revision")

    current_revision_id = profile["current_revision_id"]
    source_row = cast(
        Mapping[Any, Any] | None,
        bind.execute(
            text(
                "SELECT id, agent_profile_id, display_name, system_prompt, purpose, instructions, "
                "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
                "memory_policy_revision_id, tool_policy_revision_id "
                "FROM agent_revisions WHERE id = :revision"
            ),
            {"revision": current_revision_id},
        ).mappings().first(),
    )
    if current_revision_id == _GENERAL_REVISION:
        if source_row is None:
            _fail_closed("profile points to a missing canonical research revision")
        document_source = cast(
            Mapping[Any, Any] | None,
            bind.execute(
                text(
                    "SELECT id, agent_profile_id, tool_policy_revision_id, display_name, "
                    "system_prompt, purpose, instructions, persona_revision_id, "
                    "prompt_bundle_revision_id, model_policy_revision_id, memory_policy_revision_id "
                    "FROM agent_revisions WHERE id = :revision"
                ),
                {"revision": _GENERAL_DOCUMENT_REVISION},
            ).mappings().first(),
        )
        if document_source is None:
            _fail_closed("canonical document predecessor is missing")
        _validate_document_predecessor(bind, document_source)
        _validate_existing_research_seed(bind, document_source)
        return
    if current_revision_id != _GENERAL_DOCUMENT_REVISION:
        _fail_closed("built-in profile is not pinned to the canonical document predecessor")
    if source_row is None:
        _fail_closed("canonical document predecessor is missing")

    _validate_document_predecessor(bind, source_row)
    if _validate_existing_research_seed(bind, source_row):
        # A partially applied migration may have created both immutable rows but
        # not yet advanced the profile. Only the canonical predecessor is
        # eligible for this pointer transition.
        bind.execute(
            text(
                "UPDATE agent_profiles SET current_revision_id = :revision, "
                "version = version + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = :agent AND current_revision_id = :source"
            ),
            {
                "revision": _GENERAL_REVISION,
                "agent": _GENERAL_AGENT,
                "source": _GENERAL_DOCUMENT_REVISION,
            },
        )
        return

    source_grants = sorted(_DOCUMENT_GRANTS)
    if (
        bind.execute(
            text("SELECT id FROM tool_policy_revisions WHERE id = :id"),
            {"id": _GENERAL_POLICY},
        ).scalar_one_or_none()
        is not None
    ):
        _fail_closed("canonical research policy appeared without a complete seed")

    policy_revision = _next_owner_revision(bind, "tool_policy_revisions")
    bind.execute(
        text(
            "INSERT INTO tool_policy_revisions "
            "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
            "max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
            "VALUES (:id, '', '', :agent, :revision, 1, 30000, 0, "
            "CAST(:scopes AS JSONB))"
        ),
        {
            "id": _GENERAL_POLICY,
            "agent": _GENERAL_AGENT,
            "revision": policy_revision,
            "scopes": '["memory:read","documents:read","research:run",'
            '"reminders:read","reminders:write"]',
        },
    )
    for tool_id, tool_version in source_grants + [("web.research", 1)]:
        bind.execute(
            text(
                "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                "VALUES (:policy, :tool, :version)"
            ),
            {"policy": _GENERAL_POLICY, "tool": tool_id, "version": tool_version},
        )

    revision_number = _next_owner_revision(bind, "agent_revisions")
    bind.execute(
        text(
            "INSERT INTO agent_revisions "
            "(id, agent_profile_id, revision, display_name, system_prompt, purpose, instructions, "
            "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
            "memory_policy_revision_id, tool_policy_revision_id) "
            "VALUES (:id, :agent, :revision, :display_name, :system_prompt, :purpose, :instructions, "
            ":persona_revision_id, :prompt_bundle_revision_id, :model_policy_revision_id, "
            ":memory_policy_revision_id, :policy)"
        ),
        {
            "id": _GENERAL_REVISION,
            "agent": _GENERAL_AGENT,
            "revision": revision_number,
            "policy": _GENERAL_POLICY,
            "display_name": source_row["display_name"],
            "system_prompt": source_row["system_prompt"],
            "purpose": source_row["purpose"],
            "instructions": source_row["instructions"],
            "persona_revision_id": source_row["persona_revision_id"],
            "prompt_bundle_revision_id": source_row["prompt_bundle_revision_id"],
            "model_policy_revision_id": source_row["model_policy_revision_id"],
            "memory_policy_revision_id": source_row["memory_policy_revision_id"],
        },
    )
    bind.execute(
        text(
            "UPDATE agent_profiles SET current_revision_id = :revision, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :agent AND current_revision_id = :source"
        ),
        {
            "revision": _GENERAL_REVISION,
            "agent": _GENERAL_AGENT,
            "source": _GENERAL_DOCUMENT_REVISION,
        },
    )


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

    # Keep the document-only revision immutable. The new web grant is an
    # additive policy and agent revision so historical runs remain resolvable.
    _seed_research_delivery(op.get_bind())

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
