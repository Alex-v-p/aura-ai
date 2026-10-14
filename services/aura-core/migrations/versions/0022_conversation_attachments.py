"""Add owner-private image attachments and immutable message links."""

from alembic import op
from sqlalchemy import text

revision = "0022_conversation_attachments"
down_revision = "0021_conversation_branching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Source IDs intentionally have no cross-domain foreign keys: documents
    # are owned by knowledge.documents while images are owned here.
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS artifact_attachments (
                id UUID PRIMARY KEY,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                filename VARCHAR(255) NOT NULL,
                media_type VARCHAR(96) NOT NULL,
                byte_count INTEGER NOT NULL,
                digest VARCHAR(64) NOT NULL,
                original_object_key VARCHAR(512) NOT NULL,
                derivative_object_key VARCHAR(512) NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                status VARCHAR(24) NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                failure_code VARCHAR(64),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (principal_issuer, principal_subject, digest)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_artifact_attachments_owner "
            "ON artifact_attachments (principal_issuer, principal_subject, created_at)"
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS artifact_attachment_upload_sessions (
                id UUID PRIMARY KEY,
                attachment_id UUID NOT NULL,
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                filename VARCHAR(255) NOT NULL,
                media_type VARCHAR(96) NOT NULL,
                byte_count INTEGER NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                fingerprint VARCHAR(128) NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'uploading',
                version INTEGER NOT NULL DEFAULT 1,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (principal_issuer, principal_subject, idempotency_key)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS artifact_attachment_delete_requests (
                principal_issuer VARCHAR(1024) NOT NULL,
                principal_subject VARCHAR(255) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL,
                attachment_id UUID NOT NULL,
                expected_version INTEGER,
                result_version INTEGER NOT NULL,
                result_status VARCHAR(24) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS artifact_attachment_tombstones (
                attachment_id UUID PRIMARY KEY,
                version INTEGER NOT NULL,
                deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS artifact_attachment_lifecycle_events (
                event_id UUID PRIMARY KEY,
                attachment_id UUID NOT NULL,
                upload_id UUID,
                status VARCHAR(24) NOT NULL,
                attachment_version INTEGER NOT NULL,
                stored_object_count INTEGER NOT NULL DEFAULT 0,
                occurred_at TIMESTAMPTZ NOT NULL,
                correlation_id UUID NOT NULL,
                causation_id UUID NOT NULL
            )
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachments JSONB
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE runs ADD COLUMN IF NOT EXISTS attachments JSONB
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS message_attachments (
                id UUID PRIMARY KEY,
                message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                kind VARCHAR(16) NOT NULL,
                source_id UUID NOT NULL,
                display_filename VARCHAR(255) NOT NULL,
                media_type VARCHAR(96) NOT NULL,
                byte_count INTEGER NOT NULL,
                availability VARCHAR(16) NOT NULL DEFAULT 'available',
                width INTEGER,
                height INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (message_id, kind, source_id)
            )
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_message_attachments_message "
            "ON message_attachments (message_id, created_at)"
        )
    )


def downgrade() -> None:
    raise RuntimeError("conversation attachment history is intentionally forward-only")
