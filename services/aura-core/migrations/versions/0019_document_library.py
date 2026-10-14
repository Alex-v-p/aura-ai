"""Add owner-private Garage-backed document metadata and chunks."""
# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0019_document_library"
down_revision = "0018_conversational_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS documents (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            media_type VARCHAR(255) NOT NULL,
            byte_count INTEGER NOT NULL CHECK (byte_count >= 1 AND byte_count <= 100000000),
            digest VARCHAR(64),
            object_key VARCHAR(512),
            status VARCHAR(16) NOT NULL CHECK (status IN ('uploading','queued','processing','ready','failed','deleting')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            failure_code VARCHAR(64),
            extracted_text_length INTEGER NOT NULL DEFAULT 0 CHECK (extracted_text_length >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_documents_owner_digest UNIQUE (principal_issuer, principal_subject, digest)
        )
    """)
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            text TEXT NOT NULL,
            locator VARCHAR(255) NOT NULL,
            digest VARCHAR(64) NOT NULL,
            embedding vector(768),
            model_id VARCHAR(255),
            model_revision VARCHAR(255),
            model_digest VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_document_chunk_ordinal UNIQUE (document_id, ordinal)
        )
    """)
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_documents_owner_created ON documents (principal_issuer, principal_subject, created_at DESC)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_documents_owner_status ON documents (principal_issuer, principal_subject, status)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_document ON document_chunks (document_id, ordinal)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_lexical ON document_chunks USING GIN (to_tsvector('simple', text))"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_upload_sessions (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            media_type VARCHAR(255) NOT NULL,
            byte_count INTEGER NOT NULL CHECK (byte_count >= 1 AND byte_count <= 100000000),
            idempotency_key VARCHAR(255),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            status VARCHAR(16) NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading','consumed','completed')),
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        )
    """)
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_document_upload_sessions_owner ON document_upload_sessions (principal_issuer, principal_subject, status)"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_upload_session_idempotency ON document_upload_sessions (principal_issuer, principal_subject, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_tombstones (
            document_id UUID PRIMARY KEY,
            deleted_at TIMESTAMPTZ NOT NULL,
            final_version INTEGER NOT NULL CHECK (final_version > 0)
        )
    """)
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_upload_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, idempotency_key)
        )
    """)
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_command_receipts (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            operation VARCHAR(32) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            document_id UUID NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            status VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key)
        )
    """)
    )
    op.execute(
        text("""
        CREATE TABLE IF NOT EXISTS document_lifecycle_events (
            event_id UUID PRIMARY KEY,
            document_id UUID NOT NULL,
            document_version INTEGER NOT NULL CHECK (document_version > 0),
            event_type VARCHAR(64) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            correlation_id UUID NOT NULL,
            causation_id UUID NOT NULL
        )
    """)
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS document_tombstones"))
    op.execute(text("DROP TABLE IF EXISTS document_upload_sessions"))
    op.execute(text("DROP TABLE IF EXISTS document_upload_idempotency"))
    op.execute(text("DROP TABLE IF EXISTS document_command_receipts"))
    op.execute(text("DROP TABLE IF EXISTS document_lifecycle_events"))
    op.execute(text("DROP TABLE IF EXISTS document_chunks"))
    op.execute(text("DROP TABLE IF EXISTS documents"))
