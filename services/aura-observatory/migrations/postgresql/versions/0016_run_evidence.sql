-- AURA-0119: Observatory-owned governed run-evidence state.
-- Content bytes live only in the dedicated Garage bucket.  PostgreSQL stores
-- bounded manifests, deletion truth, collision identities, and content-free
-- audit rows.
CREATE TABLE IF NOT EXISTS observatory_run_evidence_manifests (
    evidence_id UUID PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    run_id UUID NOT NULL,
    trace_id CHAR(32) NOT NULL,
    span_id CHAR(16) NOT NULL,
    parent_span_id CHAR(16),
    sequence BIGINT NOT NULL CHECK (sequence >= 0),
    step_category VARCHAR(32) NOT NULL,
    evidence_kind VARCHAR(64) NOT NULL,
    capture_state VARCHAR(16) NOT NULL,
    media_classification VARCHAR(16) NOT NULL,
    media_type VARCHAR(64) NOT NULL,
    digest CHAR(71),
    byte_count INTEGER NOT NULL DEFAULT 0 CHECK (byte_count >= 0 AND byte_count <= 1048576),
    captured_at TIMESTAMPTZ NOT NULL,
    reason VARCHAR(64),
    -- Cleared during manual deletion; retaining this content-derived verifier
    -- would create a post-deletion dictionary oracle.
    fingerprint CHAR(64),
    object_key VARCHAR(160),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((capture_state = 'captured' AND digest IS NOT NULL AND byte_count >= 0 AND reason IS NULL)
        OR (capture_state <> 'captured' AND digest IS NULL AND byte_count = 0 AND reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_run_sequence_idx
    ON observatory_run_evidence_manifests(run_id, sequence, captured_at, evidence_id);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_run_state_idx
    ON observatory_run_evidence_manifests(run_id, capture_state);
CREATE UNIQUE INDEX IF NOT EXISTS observatory_run_evidence_event_fingerprint_idx
    ON observatory_run_evidence_manifests(event_id, fingerprint);

-- Make an already-created installation restart-safe when upgrading from the
-- initial NOT NULL definition.
ALTER TABLE observatory_run_evidence_manifests
    ALTER COLUMN fingerprint DROP NOT NULL;
UPDATE observatory_run_evidence_manifests
SET fingerprint = NULL
WHERE capture_state = 'deleted' AND fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS observatory_run_evidence_deliveries (
    delivery_id UUID PRIMARY KEY,
    event_id UUID NOT NULL,
    evidence_id UUID NOT NULL,
    producer_id VARCHAR(128) NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 100),
    first_delivery_id UUID NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, attempt),
    UNIQUE(evidence_id, attempt)
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_delivery_event_idx
    ON observatory_run_evidence_deliveries(event_id, attempt);

CREATE TABLE IF NOT EXISTS observatory_run_evidence_rejections (
    rejection_id UUID PRIMARY KEY,
    reason VARCHAR(64) NOT NULL,
    byte_count INTEGER NOT NULL DEFAULT 0 CHECK (byte_count >= 0 AND byte_count <= 1500000),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_rejection_time_idx
    ON observatory_run_evidence_rejections(occurred_at);

CREATE TABLE IF NOT EXISTS observatory_run_evidence_collisions (
    collision_id UUID PRIMARY KEY,
    stable_id UUID NOT NULL,
    run_id UUID,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome VARCHAR(32) NOT NULL DEFAULT 'duplicate_collision'
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_collision_stable_idx
    ON observatory_run_evidence_collisions(stable_id, observed_at);

CREATE TABLE IF NOT EXISTS observatory_run_evidence_orphans (
    orphan_id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    evidence_id UUID NOT NULL,
    object_key VARCHAR(160) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'recovered')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    recovered_at TIMESTAMPTZ,
    UNIQUE(run_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_orphan_status_idx
    ON observatory_run_evidence_orphans(status, created_at);

CREATE TABLE IF NOT EXISTS observatory_run_evidence_deletions (
    deletion_id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    idempotency_key UUID NOT NULL,
    status VARCHAR(16) NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    deleted_evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (deleted_evidence_count >= 0),
    audit_mode VARCHAR(32) NOT NULL DEFAULT 'content_free',
    failure_code VARCHAR(64),
    UNIQUE(run_id, idempotency_key),
    CHECK (status IN ('in_progress', 'completed', 'failed'))
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_deletion_run_idx
    ON observatory_run_evidence_deletions(run_id, requested_at);

CREATE TABLE IF NOT EXISTS observatory_run_evidence_audit (
    audit_id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    operation VARCHAR(32) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    -- Deliberately no request body, owner, evidence ID, digest, or object key.
    content_free BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_audit_run_idx
    ON observatory_run_evidence_audit(run_id, occurred_at);

-- Content-free invalidation cursor for the existing Observatory SSE stream.
-- The table contains no manifest bytes, digest, or provider content.
CREATE TABLE IF NOT EXISTS observatory_run_evidence_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL,
    run_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id)
);
CREATE INDEX IF NOT EXISTS observatory_run_evidence_events_run_idx
    ON observatory_run_evidence_events(run_id, sequence);

INSERT INTO observatory_migration_ledger(version, applied_at)
SELECT '0016_run_evidence', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM observatory_migration_ledger
    WHERE version = '0016_run_evidence'
);
