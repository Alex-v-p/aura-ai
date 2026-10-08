"""Durable memory extraction, model configuration, and maintenance state."""

# SQL migration statements intentionally mirror PostgreSQL DDL.
# ruff: noqa: E501

from alembic import op
from sqlalchemy import text

revision = "0009_memory_processing"
down_revision = "0008_memory_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generation metadata is private to the owner that selected the model.
    op.execute(text("ALTER TABLE memory_embedding_generations ADD COLUMN IF NOT EXISTS principal_issuer VARCHAR(1024) NOT NULL DEFAULT ''"))
    op.execute(text("ALTER TABLE memory_embedding_generations ADD COLUMN IF NOT EXISTS principal_subject VARCHAR(255) NOT NULL DEFAULT ''"))
    # 0008 used the integer generation as the referenced key.  PostgreSQL
    # cannot drop that key while the legacy FK still depends on its backing
    # index.  Add owner columns to the embedding rows first, then replace the
    # legacy FK with an owner-scoped one in the same transaction.  Legacy rows
    # intentionally use the empty owner sentinel, preserving their existing
    # relationship while allowing each owner to reuse a generation number.
    op.execute(text("ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS principal_issuer VARCHAR(1024) NOT NULL DEFAULT ''"))
    op.execute(text("ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS principal_subject VARCHAR(255) NOT NULL DEFAULT ''"))
    op.execute(text("ALTER TABLE memory_embeddings DROP CONSTRAINT IF EXISTS memory_embeddings_generation_fkey"))
    op.execute(text("ALTER TABLE memory_embedding_generations DROP CONSTRAINT IF EXISTS memory_embedding_generations_generation_key"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_generation_owner ON memory_embedding_generations(generation, principal_issuer, principal_subject)"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_generation_id_owner ON memory_embedding_generations(id, principal_issuer, principal_subject)"))
    op.execute(text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'memory_embeddings_generation_owner_fkey'
                  AND conrelid = 'memory_embeddings'::regclass
            ) THEN
                ALTER TABLE memory_embeddings
                    ADD CONSTRAINT memory_embeddings_generation_owner_fkey
                    FOREIGN KEY (generation, principal_issuer, principal_subject)
                    REFERENCES memory_embedding_generations(generation, principal_issuer, principal_subject);
            END IF;
        END $$
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_embedding_generations_owner ON memory_embedding_generations(principal_issuer, principal_subject, status)"))
    # Conversation completion stores only identifiers in the outbox.  These
    # tables hold the private payload after the worker has read the owned run.
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_model_configurations (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            extraction_model_id VARCHAR(255) NOT NULL,
            extraction_model_revision VARCHAR(255) NULL,
            embedding_model_id VARCHAR(255) NOT NULL,
            embedding_model_revision VARCHAR(255) NULL,
            embedding_generation UUID NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject)
        )
    """))
    op.execute(text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'memory_model_configurations'
                  AND column_name = 'embedding_generation' AND data_type = 'integer'
            ) THEN
                ALTER TABLE memory_model_configurations
                    ALTER COLUMN embedding_generation TYPE UUID USING NULL;
            END IF;
        END $$
    """))
    op.execute(text("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_model_config_generation_owner_fkey') THEN
                ALTER TABLE memory_model_configurations ADD CONSTRAINT memory_model_config_generation_owner_fkey
                    FOREIGN KEY (embedding_generation, principal_issuer, principal_subject)
                    REFERENCES memory_embedding_generations(id, principal_issuer, principal_subject);
            END IF;
        END $$
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_processing_jobs (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            run_id UUID NOT NULL,
            conversation_id UUID NOT NULL,
            causation_id UUID NULL,
            correlation_id UUID NULL,
            agent_revision_id UUID NULL,
            agent_profile_id UUID NULL,
            user_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            assistant_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_digest VARCHAR(64) NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','completed','retryable','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_id UUID NULL,
            lease_until TIMESTAMPTZ NULL,
            last_error_class VARCHAR(64) NULL,
            completed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (principal_issuer, principal_subject, run_id)
        )
    """))
    op.execute(text("ALTER TABLE memory_processing_jobs ADD COLUMN IF NOT EXISTS agent_profile_id UUID"))
    op.execute(text("ALTER TABLE memory_processing_jobs ADD COLUMN IF NOT EXISTS memory_id UUID"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id UUID PRIMARY KEY,
            job_id UUID NOT NULL REFERENCES memory_processing_jobs(id) ON DELETE CASCADE,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            action VARCHAR(16) NOT NULL CHECK (action IN ('ignore','create','reinforce','supersede','dispute','review')),
            content TEXT NULL,
            kind VARCHAR(32) NULL,
            scope_type VARCHAR(16) NULL CHECK (scope_type IS NULL OR scope_type IN ('user','agent')),
            agent_profile_id UUID NULL,
            confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            importance DOUBLE PRECISION NULL CHECK (importance IS NULL OR importance BETWEEN 0 AND 1),
            half_life_days DOUBLE PRECISION NULL CHECK (half_life_days IS NULL OR half_life_days BETWEEN 0.25 AND 3650),
            valid_to TIMESTAMPTZ NULL,
            sensitivity VARCHAR(16) NOT NULL DEFAULT 'ordinary',
            grounded_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            related_memory_id UUID NULL,
            memory_id UUID NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'proposed'
                CHECK (state IN ('proposed','accepted','rejected','review','retryable')),
            decision_reason VARCHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMPTZ NULL,
            UNIQUE (job_id, id)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_action_outcomes (
            id UUID PRIMARY KEY,
            candidate_id UUID NULL REFERENCES memory_candidates(id) ON DELETE SET NULL,
            job_id UUID NOT NULL REFERENCES memory_processing_jobs(id) ON DELETE CASCADE,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            action VARCHAR(16) NOT NULL,
            outcome VARCHAR(16) NOT NULL CHECK (outcome IN ('created','reinforced','disputed','superseded','review','ignored','retryable','failed')),
            memory_id UUID NULL,
            revision_id UUID NULL,
            error_class VARCHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (job_id, action)
        )
    """))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_embedding_jobs (
            id UUID PRIMARY KEY,
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            memory_id UUID NOT NULL,
            revision_id UUID NOT NULL,
            generation_id UUID NOT NULL REFERENCES memory_embedding_generations(id),
            status VARCHAR(16) NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','completed','retryable','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_error_class VARCHAR(64) NULL,
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_id UUID NULL,
            lease_until TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (revision_id, generation_id)
        )
    """))
    op.execute(text("ALTER TABLE memory_embedding_jobs ADD COLUMN IF NOT EXISTS lease_id UUID"))
    op.execute(text("ALTER TABLE memory_embedding_jobs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_maintenance_state (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            last_run_at TIMESTAMPTZ NULL,
            last_reindex_generation INTEGER NULL,
            reindex_generation_id UUID NULL,
            reindex_cursor UUID NULL,
            reindex_completed INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject)
        )
    """))
    op.execute(text("ALTER TABLE memory_maintenance_state ADD COLUMN IF NOT EXISTS reindex_generation_id UUID"))
    op.execute(text("ALTER TABLE memory_maintenance_state ADD COLUMN IF NOT EXISTS reindex_cursor UUID"))
    op.execute(text("ALTER TABLE memory_maintenance_state ADD COLUMN IF NOT EXISTS reindex_completed INTEGER NOT NULL DEFAULT 0"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_purge_fences (
            principal_issuer VARCHAR(1024) NOT NULL,
            principal_subject VARCHAR(255) NOT NULL,
            memory_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (principal_issuer, principal_subject, memory_id)
        )
    """))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_jobs_due ON memory_processing_jobs(status, available_at)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_candidates_owner ON memory_candidates(principal_issuer, principal_subject, state, created_at DESC)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_embedding_jobs_due ON memory_embedding_jobs(status, available_at)"))
    # Every durable link carries the same owner as its parent.  Replace the
    # legacy global embedding-job key so two owners may reindex the same
    # revision/generation number independently.
    op.execute(text("ALTER TABLE memory_embedding_jobs DROP CONSTRAINT IF EXISTS memory_embedding_jobs_revision_id_generation_id_key"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_embedding_job_owner ON memory_embedding_jobs(revision_id, generation_id, principal_issuer, principal_subject)"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_processing_job_owner ON memory_processing_jobs(id, principal_issuer, principal_subject)"))
    op.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_candidate_owner ON memory_candidates(id, principal_issuer, principal_subject)"))
    op.execute(text("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_candidates_job_owner_fkey') THEN
                ALTER TABLE memory_candidates ADD CONSTRAINT memory_candidates_job_owner_fkey
                    FOREIGN KEY (job_id, principal_issuer, principal_subject)
                    REFERENCES memory_processing_jobs(id, principal_issuer, principal_subject);
            END IF;
        END $$
    """))
    op.execute(text("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_embedding_jobs_memory_owner_fkey') THEN
                ALTER TABLE memory_embedding_jobs ADD CONSTRAINT memory_embedding_jobs_memory_owner_fkey
                    FOREIGN KEY (memory_id, principal_issuer, principal_subject)
                    REFERENCES memories(id, principal_issuer, principal_subject);
            END IF;
        END $$
    """))


def downgrade() -> None:
    raise RuntimeError("Memory processing downgrade is disabled; use a forward migration")
