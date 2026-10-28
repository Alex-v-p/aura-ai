"""Add the Core-owned generated text artifact aggregate.

This migration is additive and intentionally stops at the artifact boundary.
Task projection owns the following revision (0030).
"""

# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0029_generated_artifacts"
down_revision = "0028_workflow_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifacts (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            kind VARCHAR(64) NOT NULL CHECK (kind IN ('research_report', 'research_source_manifest', 'generated_text', 'generated_data')),
            title VARCHAR(300),
            lifecycle VARCHAR(24) NOT NULL CHECK (lifecycle IN ('active', 'deletion_pending', 'deletion_failed', 'cleanup_unknown', 'deleted')),
            current_revision INTEGER CHECK (current_revision IS NULL OR current_revision > 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            workspace_id UUID,
            task_id UUID,
            run_id UUID,
            conversation_id UUID,
            deletion_audit_id UUID,
            integrity_revision INTEGER CHECK (integrity_revision IS NULL OR integrity_revision > 0),
            integrity_sha256 VARCHAR(64),
            integrity_byte_count INTEGER CHECK (integrity_byte_count IS NULL OR integrity_byte_count >= 0),
            integrity_created_at TIMESTAMPTZ,
            failure_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (lifecycle = 'active' OR title IS NULL),
            CHECK (lifecycle = 'active' OR integrity_revision IS NOT NULL OR current_revision IS NULL)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_revisions (
            artifact_id UUID NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            media_type VARCHAR(64) NOT NULL CHECK (media_type IN ('text/markdown; charset=utf-8', 'text/plain; charset=utf-8', 'application/json')),
            sha256 VARCHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            byte_count INTEGER NOT NULL CHECK (byte_count > 0 AND byte_count <= 10000000),
            object_key VARCHAR(512) NOT NULL,
            creator_component VARCHAR(128) NOT NULL,
            previous_revision INTEGER CHECK (previous_revision IS NULL OR previous_revision > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (artifact_id, revision),
            UNIQUE (artifact_id, sha256),
            CHECK (previous_revision IS NULL OR previous_revision < revision)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            operation VARCHAR(32) NOT NULL CHECK (operation IN ('create', 'append', 'delete', 'retry_cleanup', 'reconcile_cleanup')),
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(128) NOT NULL,
            artifact_id UUID NOT NULL,
            result_version INTEGER NOT NULL CHECK (result_version > 0),
            result_lifecycle VARCHAR(24) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_lifecycle_events (
            event_id UUID PRIMARY KEY,
            artifact_id UUID NOT NULL,
            artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
            event_type VARCHAR(64) NOT NULL CHECK (event_type IN ('artifact.created', 'artifact.revision_published', 'artifact.deletion_changed')),
            lifecycle VARCHAR(24) NOT NULL,
            current_revision INTEGER CHECK (current_revision IS NULL OR current_revision > 0),
            occurred_at TIMESTAMPTZ NOT NULL,
            correlation_id UUID NOT NULL,
            causation_id UUID NOT NULL
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_tombstones (
            artifact_id UUID PRIMARY KEY,
            final_version INTEGER NOT NULL CHECK (final_version > 0),
            deleted_at TIMESTAMPTZ NOT NULL,
            integrity_revision INTEGER NOT NULL CHECK (integrity_revision > 0),
            integrity_sha256 VARCHAR(64) NOT NULL CHECK (integrity_sha256 ~ '^[0-9a-f]{64}$'),
            integrity_byte_count INTEGER NOT NULL CHECK (integrity_byte_count >= 0),
            integrity_created_at TIMESTAMPTZ NOT NULL
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_cleanup_work (
            artifact_id UUID NOT NULL,
            operation VARCHAR(32) NOT NULL CHECK (operation IN ('delete', 'retry_cleanup', 'reconcile_cleanup')),
            expected_version INTEGER NOT NULL CHECK (expected_version > 0),
            status VARCHAR(24) NOT NULL CHECK (status IN ('pending', 'claimed', 'failed', 'unknown', 'complete')),
            fingerprint VARCHAR(128) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            claim_token VARCHAR(64),
            lease_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (artifact_id, operation)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_append_reservations (
            artifact_id UUID PRIMARY KEY,
            source_version INTEGER NOT NULL CHECK (source_version > 0),
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255),
            fingerprint VARCHAR(128) NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            media_type VARCHAR(64) NOT NULL,
            sha256 VARCHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            object_key VARCHAR(512) NOT NULL,
            creator_component VARCHAR(128) NOT NULL,
            previous_revision INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS generated_artifact_create_reservations (
            artifact_id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255),
            fingerprint VARCHAR(128) NOT NULL,
            media_type VARCHAR(64) NOT NULL,
            sha256 VARCHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            object_key VARCHAR(512) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_generated_artifacts_owner_created ON generated_artifacts (principal_issuer, principal_subject, created_at DESC, id DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_generated_artifacts_owner_lifecycle ON generated_artifacts (principal_issuer, principal_subject, lifecycle, updated_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_generated_artifact_revisions_artifact ON generated_artifact_revisions (artifact_id, revision DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_generated_artifact_events_replay ON generated_artifact_lifecycle_events (artifact_id, occurred_at, event_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_generated_artifact_cleanup_work_status ON generated_artifact_cleanup_work (status, updated_at)"))


def downgrade() -> None:
    # Local rollback removes only the tables introduced by this revision.  It
    # never touches unrelated Core rows.
    op.execute(text("DROP INDEX IF EXISTS ix_generated_artifact_events_replay"))
    op.execute(text("DROP INDEX IF EXISTS ix_generated_artifact_cleanup_work_status"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_cleanup_work"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_append_reservations"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_create_reservations"))
    op.execute(text("DROP INDEX IF EXISTS ix_generated_artifact_revisions_artifact"))
    op.execute(text("DROP INDEX IF EXISTS ix_generated_artifacts_owner_lifecycle"))
    op.execute(text("DROP INDEX IF EXISTS ix_generated_artifacts_owner_created"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_tombstones"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_lifecycle_events"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_idempotency"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifact_revisions"))
    op.execute(text("DROP TABLE IF EXISTS generated_artifacts"))
