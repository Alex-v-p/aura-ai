"""Create the bounded manual workflow state owned by Aura Core.

This migration is intentionally forward-only in spirit.  Workflow retention
uses independent deletion scopes, so none of the relationships below use
database cascades. Existing approval and notification tables are evolved
by their owning adapters in the same revision when those additive contracts
are enabled; this revision owns the workflow tables and their state checks.
"""

# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0027_workflow_contract_state"
down_revision = "0026_briefing_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _upgrade_approval_lineage()
    _upgrade_notification_source()
    _upgrade_run_lineage()
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            display_name VARCHAR(512) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (version > 0),
            CHECK (char_length(display_name) BETWEEN 1 AND 512),
            CHECK (status IN ('active','retired','deleted'))
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_drafts (
            id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            configuration JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ,
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (version > 0),
            CHECK (octet_length(configuration::text) <= 65536)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_revisions (
            id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL,
            draft_id UUID,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            display_name VARCHAR(512) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'draft',
            revision_number INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            input_schema JSONB NOT NULL,
            configuration JSONB NOT NULL,
            published_at TIMESTAMPTZ,
            retired_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (workflow_id, revision_number),
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (revision_number > 0),
            CHECK (version > 0),
            CHECK (status IN ('draft','published','retired')),
            CHECK (char_length(display_name) BETWEEN 1 AND 512),
            CHECK (jsonb_typeof(configuration) = 'object'),
            CHECK (octet_length(configuration::text) <= 65536)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_instances (
            id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL,
            revision_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            household_id VARCHAR(128),
            workspace_id VARCHAR(128),
            current_ordinal INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            terminal_outcome VARCHAR(16),
            version INTEGER NOT NULL DEFAULT 1,
            admitted_at TIMESTAMPTZ NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            input_values JSONB NOT NULL,
            replay_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (id, principal_issuer, principal_subject),
            CHECK (current_ordinal BETWEEN 1 AND 8),
            CHECK (version > 0),
            CHECK (replay_count BETWEEN 0 AND 1),
            CHECK (deadline > admitted_at AND deadline <= admitted_at + INTERVAL '30 days'),
            CHECK (status IN ('queued','running','waiting_for_time','waiting_for_approval','waiting_for_resolution','resolution_required','cancel_requested','terminal')),
            CHECK (terminal_outcome IS NULL OR terminal_outcome IN ('completed','canceled','failed')),
            CHECK ((status = 'terminal') = (terminal_outcome IS NOT NULL)),
            CHECK (octet_length(input_values::text) <= 32768)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_instance_steps (
            id UUID PRIMARY KEY,
            instance_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            ordinal INTEGER NOT NULL,
            kind VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            effect_outcome VARCHAR(24) NOT NULL DEFAULT 'not_attempted',
            execution_id VARCHAR(128),
            output JSONB,
            recovery_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (instance_id, ordinal),
            CHECK (ordinal BETWEEN 1 AND 8),
            CHECK (version > 0),
            CHECK (kind IN ('agent_run','owner_approval','owner_notification','wait_until')),
            CHECK (status IN ('pending','running','waiting_for_time','waiting_for_approval','failed_replayable','resolution_required','completed','rejected','expired','canceled','failed')),
            CHECK (effect_outcome IN ('not_attempted','completed','definitively_failed','unknown')),
            CHECK (output IS NULL OR octet_length(output::text) <= 32768)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_checkpoints (
            id UUID PRIMARY KEY,
            instance_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            ordinal INTEGER NOT NULL,
            checkpoint_status VARCHAR(16) NOT NULL,
            payload JSONB NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (instance_id, ordinal, checkpoint_status),
            CHECK (ordinal BETWEEN 1 AND 8),
            CHECK (checkpoint_status IN ('current','settled')),
            CHECK (octet_length(payload::text) <= 8192)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_results (
            id UUID PRIMARY KEY,
            instance_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            outcome VARCHAR(16) NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (instance_id),
            CHECK (outcome IN ('completed','canceled','failed'))
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_instance_history (
            id UUID PRIMARY KEY,
            instance_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            ordinal INTEGER,
            event_kind VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            version INTEGER NOT NULL,
            metadata JSONB NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (ordinal IS NULL OR ordinal BETWEEN 1 AND 8),
            CHECK (octet_length(metadata::text) <= 8192)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_tombstones (
            instance_id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            workflow_id UUID NOT NULL,
            revision_id UUID NOT NULL,
            terminal_outcome VARCHAR(16) NOT NULL,
            version INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (terminal_outcome IN ('completed','canceled','failed')),
            CHECK (version > 0)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_definition_tombstones (
            workflow_id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            version INTEGER NOT NULL,
            deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (version > 0)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_revision_tombstones (
            revision_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            revision_number INTEGER NOT NULL,
            version INTEGER NOT NULL,
            deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (revision_number > 0),
            CHECK (version > 0)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            response JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS workflow_lifecycle_events (
            id UUID PRIMARY KEY,
            instance_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            ordinal INTEGER,
            event_kind VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            version INTEGER NOT NULL,
            metadata JSONB NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (ordinal IS NULL OR ordinal BETWEEN 1 AND 8),
            CHECK (octet_length(metadata::text) <= 8192)
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_definition_owner ON workflow_definitions (principal_issuer, principal_subject, updated_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_instance_owner ON workflow_instances (principal_issuer, principal_subject, updated_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_history_owner ON workflow_instance_history (principal_issuer, principal_subject, instance_id, occurred_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_lifecycle_owner ON workflow_lifecycle_events (principal_issuer, principal_subject, occurred_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_definition_tombstone_owner ON workflow_definition_tombstones (principal_issuer, principal_subject)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_revision_tombstone_owner ON workflow_revision_tombstones (principal_issuer, principal_subject)"))


def downgrade() -> None:
    # Explicitly ordered and non-cascading so a rollback cannot silently
    # remove an owner’s retained history or results.
    for table in (
        "workflow_lifecycle_events",
        "workflow_command_idempotency",
        "workflow_revision_tombstones",
        "workflow_definition_tombstones",
        "workflow_tombstones",
        "workflow_instance_history",
        "workflow_results",
        "workflow_checkpoints",
        "workflow_instance_steps",
        "workflow_instances",
        "workflow_revisions",
        "workflow_drafts",
        "workflow_definitions",
    ):
        op.execute(text(f"DROP TABLE IF EXISTS {table}"))
    _downgrade_run_lineage()
    _downgrade_notification_source()
    _downgrade_approval_lineage()


def _upgrade_approval_lineage() -> None:
    """Add the typed workflow approval origin without changing reminders."""

    op.execute(text("""
        ALTER TABLE approvals
            ALTER COLUMN run_id DROP NOT NULL,
            ALTER COLUMN conversation_id DROP NOT NULL,
            ALTER COLUMN tool_call_id DROP NOT NULL,
            ADD COLUMN IF NOT EXISTS origin VARCHAR(64),
            ADD COLUMN IF NOT EXISTS workflow_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_revision_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_instance_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_step_ordinal INTEGER,
            ADD COLUMN IF NOT EXISTS workflow_step_execution_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_display_name VARCHAR(512),
            ADD COLUMN IF NOT EXISTS next_step_kind VARCHAR(32),
            ADD COLUMN IF NOT EXISTS consequence VARCHAR(64),
            ADD COLUMN IF NOT EXISTS consequence_copy VARCHAR(128),
            ADD COLUMN IF NOT EXISTS owner_scope_id VARCHAR(128),
            ADD COLUMN IF NOT EXISTS workspace_scope_id VARCHAR(128)
    """))
    op.execute(text("""
        ALTER TABLE approval_events
            ALTER COLUMN run_id DROP NOT NULL,
            ALTER COLUMN conversation_id DROP NOT NULL,
            ADD COLUMN IF NOT EXISTS origin VARCHAR(64),
            ADD COLUMN IF NOT EXISTS workflow_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_revision_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_instance_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_step_ordinal INTEGER,
            ADD COLUMN IF NOT EXISTS workflow_step_execution_id UUID
    """))
    op.execute(text("ALTER TABLE approvals DROP CONSTRAINT IF EXISTS ck_approvals_workflow_lineage"))
    op.execute(text("""
        ALTER TABLE approvals ADD CONSTRAINT ck_approvals_workflow_lineage CHECK (
            (
                origin IS NULL AND run_id IS NOT NULL AND conversation_id IS NOT NULL
                AND tool_call_id IS NOT NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                AND workflow_display_name IS NULL AND next_step_kind IS NULL
                AND consequence IS NULL AND consequence_copy IS NULL
                AND owner_scope_id IS NULL AND workspace_scope_id IS NULL
            ) OR (
                origin = 'manual_workflow_instance' AND run_id IS NULL
                AND conversation_id IS NULL AND tool_call_id IS NULL
                AND workflow_id IS NOT NULL AND workflow_revision_id IS NOT NULL
                AND workflow_instance_id IS NOT NULL
                AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL
                AND workflow_display_name IS NOT NULL AND next_step_kind IS NOT NULL
                AND consequence IS NOT NULL AND consequence_copy IS NOT NULL
                AND owner_scope_id IS NOT NULL AND workspace_scope_id IS NOT NULL
            )
        )
    """))
    op.execute(text("ALTER TABLE approval_events DROP CONSTRAINT IF EXISTS ck_approval_events_workflow_lineage"))
    op.execute(text("""
        ALTER TABLE approval_events ADD CONSTRAINT ck_approval_events_workflow_lineage CHECK (
            (
                origin IS NULL AND run_id IS NOT NULL AND conversation_id IS NOT NULL
                AND workflow_id IS NULL AND workflow_revision_id IS NULL
                AND workflow_instance_id IS NULL AND workflow_step_ordinal IS NULL
                AND workflow_step_execution_id IS NULL
            ) OR (
                origin = 'manual_workflow_instance' AND run_id IS NULL
                AND conversation_id IS NULL AND workflow_id IS NOT NULL
                AND workflow_revision_id IS NOT NULL AND workflow_instance_id IS NOT NULL
                AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL
            )
        )
    """))
    op.execute(text("ALTER TABLE approvals DROP CONSTRAINT IF EXISTS ck_approvals_status"))
    op.execute(text("""
        ALTER TABLE approvals ADD CONSTRAINT ck_approvals_status CHECK (
            status IN ('pending','approved','rejected','expired','canceled')
            AND execution_status IN ('not_started','queued','executing','completed','failed','denied')
        )
    """))
    op.execute(text("ALTER TABLE approval_events DROP CONSTRAINT IF EXISTS ck_approval_events_status"))
    op.execute(text("""
        ALTER TABLE approval_events ADD CONSTRAINT ck_approval_events_status CHECK (
            status IN ('pending','approved','rejected','expired','canceled')
            AND execution_status IN ('not_started','queued','executing','completed','failed','denied')
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_approvals_workflow_instance ON approvals (principal_issuer, principal_subject, workflow_instance_id) WHERE origin = 'manual_workflow_instance'"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_approval_events_workflow_instance ON approval_events (principal_issuer, principal_subject, workflow_instance_id, workflow_step_ordinal) WHERE origin = 'manual_workflow_instance'"))


def _upgrade_notification_source() -> None:
    """Extend the existing reminder/briefing source union with workflow lineage."""

    for table in ("notifications", "notification_events"):
        op.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_source"))
        op.execute(text(f"""
            ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS workflow_id UUID,
                ADD COLUMN IF NOT EXISTS workflow_revision_id UUID,
                ADD COLUMN IF NOT EXISTS workflow_instance_id UUID,
                ADD COLUMN IF NOT EXISTS workflow_step_ordinal INTEGER,
                ADD COLUMN IF NOT EXISTS workflow_step_execution_id UUID,
                ADD COLUMN IF NOT EXISTS workflow_result_id UUID
        """))
    op.execute(text("""
        ALTER TABLE notifications ADD CONSTRAINT ck_notifications_source CHECK (
            (
                source_kind = 'reminder' AND reminder_id IS NOT NULL
                AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                AND briefing_result_id IS NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                AND workflow_result_id IS NULL
            ) OR (
                source_kind = 'briefing' AND reminder_id IS NULL
                AND briefing_schedule_id IS NOT NULL AND briefing_occurrence_id IS NOT NULL
                AND briefing_result_id IS NOT NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                AND workflow_result_id IS NULL
            ) OR (
                source_kind = 'workflow' AND reminder_id IS NULL
                AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                AND briefing_result_id IS NULL AND workflow_id IS NOT NULL
                AND workflow_revision_id IS NOT NULL AND workflow_instance_id IS NOT NULL
                AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL AND workflow_result_id IS NOT NULL
            )
        )
    """))
    op.execute(text("""
        ALTER TABLE notification_events ADD CONSTRAINT ck_notification_events_source CHECK (
            (
                source_kind = 'reminder' AND reminder_id IS NOT NULL
                AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                AND briefing_result_id IS NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                AND workflow_result_id IS NULL
            ) OR (
                source_kind = 'briefing' AND reminder_id IS NULL
                AND briefing_schedule_id IS NOT NULL AND briefing_occurrence_id IS NOT NULL
                AND briefing_result_id IS NOT NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
                AND workflow_result_id IS NULL
            ) OR (
                source_kind = 'workflow' AND reminder_id IS NULL
                AND briefing_schedule_id IS NULL AND briefing_occurrence_id IS NULL
                AND briefing_result_id IS NULL AND workflow_id IS NOT NULL
                AND workflow_revision_id IS NOT NULL AND workflow_instance_id IS NOT NULL
                AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL AND workflow_result_id IS NOT NULL
            )
        )
    """))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_workflow_result ON notifications (principal_issuer, principal_subject, workflow_result_id) WHERE source_kind = 'workflow'"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_notification_workflow_instance ON notifications (principal_issuer, principal_subject, workflow_instance_id, workflow_step_ordinal) WHERE source_kind = 'workflow'"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_notification_event_workflow_instance ON notification_events (principal_issuer, principal_subject, workflow_instance_id, workflow_step_ordinal) WHERE source_kind = 'workflow'"))


def _upgrade_run_lineage() -> None:
    """Permit null conversation lineage only with complete workflow lineage."""

    op.execute(text("""
        ALTER TABLE runs
            ALTER COLUMN conversation_id DROP NOT NULL,
            ALTER COLUMN user_message_id DROP NOT NULL,
            ADD COLUMN IF NOT EXISTS workflow_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_revision_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_instance_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_step_ordinal INTEGER,
            ADD COLUMN IF NOT EXISTS workflow_step_execution_id UUID
    """))
    op.execute(text("""
        ALTER TABLE run_events
            ALTER COLUMN conversation_id DROP NOT NULL,
            ADD COLUMN IF NOT EXISTS workflow_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_revision_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_instance_id UUID,
            ADD COLUMN IF NOT EXISTS workflow_step_ordinal INTEGER,
            ADD COLUMN IF NOT EXISTS workflow_step_execution_id UUID
    """))
    op.execute(text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_workflow_lineage"))
    op.execute(text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_closed_lineage"))
    op.execute(text("""
        ALTER TABLE runs ADD CONSTRAINT ck_runs_closed_lineage CHECK (
            (
                conversation_id IS NOT NULL AND user_message_id IS NOT NULL
                AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
            ) OR (
                conversation_id IS NULL AND user_message_id IS NULL
                AND workflow_id IS NOT NULL AND workflow_revision_id IS NOT NULL
                AND workflow_instance_id IS NOT NULL AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL
            )
        )
    """))
    op.execute(text("ALTER TABLE run_events DROP CONSTRAINT IF EXISTS ck_run_events_workflow_lineage"))
    op.execute(text("ALTER TABLE run_events DROP CONSTRAINT IF EXISTS ck_run_events_closed_lineage"))
    op.execute(text("""
        ALTER TABLE run_events ADD CONSTRAINT ck_run_events_closed_lineage CHECK (
            (
                conversation_id IS NOT NULL AND workflow_id IS NULL
                AND workflow_revision_id IS NULL AND workflow_instance_id IS NULL
                AND workflow_step_ordinal IS NULL AND workflow_step_execution_id IS NULL
            ) OR (
                conversation_id IS NULL AND workflow_id IS NOT NULL
                AND workflow_revision_id IS NOT NULL
                AND workflow_instance_id IS NOT NULL AND workflow_step_ordinal BETWEEN 1 AND 8
                AND workflow_step_execution_id IS NOT NULL
            )
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_workflow_instance ON runs (workflow_instance_id, workflow_step_ordinal) WHERE workflow_instance_id IS NOT NULL"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_run_events_workflow_instance ON run_events (workflow_instance_id, workflow_step_ordinal) WHERE workflow_instance_id IS NOT NULL"))


def _downgrade_approval_lineage() -> None:
    for table, constraint in (
        ("approval_events", "ck_approval_events_status"),
        ("approvals", "ck_approvals_status"),
        ("approval_events", "ck_approval_events_workflow_lineage"),
        ("approvals", "ck_approvals_workflow_lineage"),
    ):
        op.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}"))
    op.execute(text("DROP INDEX IF EXISTS ix_approval_events_workflow_instance"))
    op.execute(text("DROP INDEX IF EXISTS ix_approvals_workflow_instance"))
    op.execute(text("ALTER TABLE approval_events DROP COLUMN IF EXISTS origin, DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id"))
    op.execute(text("ALTER TABLE approvals DROP COLUMN IF EXISTS origin, DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id, DROP COLUMN IF EXISTS workflow_display_name, DROP COLUMN IF EXISTS next_step_kind, DROP COLUMN IF EXISTS consequence, DROP COLUMN IF EXISTS consequence_copy, DROP COLUMN IF EXISTS owner_scope_id, DROP COLUMN IF EXISTS workspace_scope_id"))


def _downgrade_notification_source() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_notification_event_workflow_instance"))
    op.execute(text("DROP INDEX IF EXISTS ix_notification_workflow_instance"))
    op.execute(text("DROP INDEX IF EXISTS uq_notification_workflow_result"))
    op.execute(text("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS ck_notifications_source"))
    op.execute(text("ALTER TABLE notification_events DROP CONSTRAINT IF EXISTS ck_notification_events_source"))
    op.execute(text("ALTER TABLE notifications DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id, DROP COLUMN IF EXISTS workflow_result_id"))
    op.execute(text("ALTER TABLE notification_events DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id, DROP COLUMN IF EXISTS workflow_result_id"))


def _downgrade_run_lineage() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_run_events_workflow_instance"))
    op.execute(text("DROP INDEX IF EXISTS ix_runs_workflow_instance"))
    op.execute(text("ALTER TABLE run_events DROP CONSTRAINT IF EXISTS ck_run_events_closed_lineage"))
    op.execute(text("ALTER TABLE run_events DROP CONSTRAINT IF EXISTS ck_run_events_workflow_lineage"))
    op.execute(text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_closed_lineage"))
    op.execute(text("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_workflow_lineage"))
    op.execute(text("ALTER TABLE run_events DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id"))
    op.execute(text("ALTER TABLE runs DROP COLUMN IF EXISTS workflow_id, DROP COLUMN IF EXISTS workflow_revision_id, DROP COLUMN IF EXISTS workflow_instance_id, DROP COLUMN IF EXISTS workflow_step_ordinal, DROP COLUMN IF EXISTS workflow_step_execution_id"))
