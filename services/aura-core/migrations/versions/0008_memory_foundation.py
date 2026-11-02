"""Owner-scoped memory records, immutable revisions, provenance, and embeddings."""

# SQL migration statements intentionally mirror PostgreSQL DDL.
# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0008_memory_foundation"
down_revision = "0007_conversation_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector is an explicit Core dependency.  It remains behind the memory
    # repository port and is never exposed as a provider API.
    op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memories (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            kind VARCHAR(32) NOT NULL CHECK (kind IN ('episodic','semantic','procedural','preference','system')),
            scope_type VARCHAR(16) NOT NULL CHECK (scope_type IN ('user','agent')),
            agent_profile_id UUID NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','dormant','archived','disabled','disputed','superseded')),
            pinned BOOLEAN NOT NULL DEFAULT FALSE,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            current_revision_id UUID NOT NULL,
            reinforced_at TIMESTAMPTZ NULL,
            dormant_at TIMESTAMPTZ NULL,
            archived_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK ((scope_type = 'user' AND agent_profile_id IS NULL) OR (scope_type = 'agent' AND agent_profile_id IS NOT NULL))
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_revisions (
            id UUID PRIMARY KEY,
            memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            kind VARCHAR(32) NOT NULL CHECK (kind IN ('episodic','semantic','procedural','preference','system')),
            content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 32768),
            confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            importance DOUBLE PRECISION NOT NULL CHECK (importance BETWEEN 0 AND 1),
            half_life_days DOUBLE PRECISION NOT NULL CHECK (half_life_days BETWEEN 0.25 AND 3650),
            observed_at TIMESTAMPTZ NOT NULL,
            valid_from TIMESTAMPTZ NULL,
            valid_to TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            provenance_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            correction_reason TEXT NULL,
            UNIQUE(memory_id, revision),
            CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_provenance (
            id UUID PRIMARY KEY,
            memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            source_type VARCHAR(64) NOT NULL,
            source_id UUID NULL,
            conversation_id UUID NULL,
            run_id UUID NULL,
            message_id UUID NULL,
            evidence_digest VARCHAR(128) NULL,
            evidence TEXT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_embedding_generations (
            id UUID PRIMARY KEY,
            generation INTEGER NOT NULL UNIQUE,
            model_id VARCHAR(255) NOT NULL,
            model_revision VARCHAR(255) NULL,
            model_digest VARCHAR(64) NULL,
            dimension INTEGER NOT NULL CHECK (dimension > 0),
            status VARCHAR(16) NOT NULL DEFAULT 'building' CHECK (status IN ('building','active','retired','failed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMPTZ NULL
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            id UUID PRIMARY KEY,
            revision_id UUID NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL REFERENCES memory_embedding_generations(generation),
            generation_id UUID NOT NULL REFERENCES memory_embedding_generations(id),
            model_id VARCHAR(255) NOT NULL,
            model_revision VARCHAR(255) NULL,
            dimension INTEGER NOT NULL CHECK (dimension > 0),
            digest VARCHAR(128) NOT NULL,
            vector vector NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(revision_id, generation)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_relations (
            memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            related_memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            relation VARCHAR(32) NOT NULL CHECK (relation IN ('supersedes','superseded_by','disputes','disputed_by')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(memory_id, related_memory_id, relation),
            CHECK (memory_id <> related_memory_id)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_purge_audit (
            id UUID PRIMARY KEY,
            memory_id UUID NOT NULL,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            action VARCHAR(16) NOT NULL DEFAULT 'purge',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_command_idempotency (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            fingerprint VARCHAR(64) NOT NULL,
            memory_id UUID NOT NULL,
            audit_id UUID NULL,
            tombstone BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(principal_issuer, principal_subject, idempotency_key)
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_owner_updated ON memories(principal_issuer, principal_subject, updated_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_owner_scope ON memories(principal_issuer, principal_subject, scope_type, agent_profile_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_revisions_search ON memory_revisions USING GIN (to_tsvector('simple', content))"))


def downgrade() -> None:
    raise RuntimeError("Memory foundation downgrade is disabled; use a forward migration")
