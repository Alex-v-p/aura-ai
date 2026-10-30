"""Add the Core-owned durable governed research aggregate."""

from alembic import op
from sqlalchemy import text

revision = "0032_governed_research"
down_revision = "0031_memory_reindex_task_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_settings (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            defaults JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (principal_issuer, principal_subject)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_tasks (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            question TEXT NOT NULL,
            mode VARCHAR(16) NOT NULL,
            stage VARCHAR(32) NOT NULL,
            outcome VARCHAR(24),
            content_state VARCHAR(24) NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            admitted_budget JSONB NOT NULL,
            consumed_budget JSONB NOT NULL,
            extension_accepted BOOLEAN NOT NULL DEFAULT FALSE,
            failure_class VARCHAR(32),
            seed_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
            domains JSONB NOT NULL DEFAULT '[]'::jsonb,
            artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            capability_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            origin JSONB NOT NULL DEFAULT '{}'::jsonb,
            policy_revision VARCHAR(64) NOT NULL DEFAULT 'research.v1',
            deadline_at TIMESTAMPTZ,
            cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
            cancellation_requested_at TIMESTAMPTZ,
            deletion_audit_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
    """))
    op.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_research_tasks_owner_created "
        "ON research_tasks (principal_issuer, principal_subject, created_at DESC)"
    ))
    op.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_research_tasks_owner_state "
        "ON research_tasks (principal_issuer, principal_subject, stage, outcome)"
    ))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_checkpoints (
            id UUID PRIMARY KEY,
            research_id UUID NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK (version > 0),
            stage VARCHAR(32) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL
        )
    """))
    op.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_research_checkpoint_version "
        "ON research_checkpoints (research_id, version)"
    ))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_operations (
            id UUID PRIMARY KEY,
            research_id UUID NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            operation VARCHAR(48) NOT NULL,
            request_id UUID NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
            status VARCHAR(24) NOT NULL,
            admitted_at TIMESTAMPTZ NOT NULL,
            settled_at TIMESTAMPTZ,
            response JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (research_id, operation, request_id)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_fallback_cohorts (
            component VARCHAR(96) NOT NULL,
            cohort_start TIMESTAMPTZ NOT NULL,
            static_count INTEGER NOT NULL DEFAULT 0,
            browser_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (component, cohort_start)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_evidence (
            id UUID PRIMARY KEY,
            research_id UUID NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            label VARCHAR(300) NOT NULL,
            url VARCHAR(2048) NOT NULL,
            publisher VARCHAR(300),
            retrieved_at TIMESTAMPTZ NOT NULL,
            media_type VARCHAR(64) NOT NULL,
            extraction_method VARCHAR(16) NOT NULL,
            freshness_at TIMESTAMPTZ,
            excerpts JSONB NOT NULL DEFAULT '[]'::jsonb,
            content_sha256 VARCHAR(64) NOT NULL,
            locator VARCHAR(255)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_deletion_cleanup (
            research_id UUID PRIMARY KEY REFERENCES research_tasks(id) ON DELETE CASCADE,
            audit_id UUID NOT NULL,
            delete_linked_artifacts BOOLEAN NOT NULL DEFAULT FALSE,
            evidence_status VARCHAR(24) NOT NULL DEFAULT 'pending',
            artifact_status VARCHAR(24) NOT NULL DEFAULT 'not_requested',
            status VARCHAR(24) NOT NULL DEFAULT 'pending',
            last_error VARCHAR(128),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    op.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_research_evidence_task "
        "ON research_evidence (research_id, id)"
    ))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_citations (
            id UUID PRIMARY KEY,
            research_id UUID NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            source_id UUID NOT NULL REFERENCES research_evidence(id) ON DELETE CASCADE,
            label VARCHAR(300) NOT NULL,
            url VARCHAR(2048) NOT NULL,
            retrieved_at TIMESTAMPTZ NOT NULL,
            locator VARCHAR(255),
            CONSTRAINT uq_research_citation_source UNIQUE (research_id, source_id)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            research_id UUID NOT NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS research_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            command VARCHAR(48) NOT NULL,
            research_id UUID NULL REFERENCES research_tasks(id) ON DELETE CASCADE,
            source_version INTEGER NOT NULL,
            result JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
        )
    """))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS research_idempotency"))
    op.execute(text("DROP TABLE IF EXISTS research_command_idempotency"))
    op.execute(text("DROP TABLE IF EXISTS research_deletion_cleanup"))
    op.execute(text("DROP TABLE IF EXISTS research_operations"))
    op.execute(text("DROP TABLE IF EXISTS research_fallback_cohorts"))
    op.execute(text("DROP TABLE IF EXISTS research_citations"))
    op.execute(text("DROP TABLE IF EXISTS research_evidence"))
    op.execute(text("DROP TABLE IF EXISTS research_checkpoints"))
    op.execute(text("DROP TABLE IF EXISTS research_tasks"))
    op.execute(text("DROP TABLE IF EXISTS research_settings"))
